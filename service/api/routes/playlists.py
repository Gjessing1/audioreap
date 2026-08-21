"""Playlist import: resolve Spotify/YouTube playlists, batch acquire."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.acquisition.queue import arq_pool, enqueue_acquire_track
from service.api.shared import _acquire_ctx, _acquisition_batch_receipt, templates
from service.config import settings
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Artist, PlaylistImport, Track
from service.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/playlists")


def _failed_playlist_items(rows: list[AcquisitionJobRow]) -> list[dict[str, str]]:
    """Small retry rows for a partial playlist failure receipt."""
    items: list[dict[str, str]] = []
    for row in rows:
        try:
            candidate = TrackCandidate.model_validate_json(row.candidate_json or "")
            title = candidate.title
            artist = candidate.artist
        except Exception:
            title = row.query or row.provider_ref
            artist = ""
        items.append({"id": row.id, "title": title, "artist": artist})
    return items


@router.get("", response_class=HTMLResponse)
async def playlists_page(
    request: Request,
    url: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, "", "playlists", session)
    ctx["playlist_url"] = url.strip()
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.post("/resolve", response_class=HTMLResponse)
async def resolve_playlist(
    request: Request,
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.core.identity import make_id

    url = url.strip()
    if not url:
        return templates.TemplateResponse(
            request, "partials/playlist_preview.html", {"error": "Please enter a playlist URL."}
        )

    if "spotify.com" in url:
        try:
            title, source, candidates = await _resolve_spotify_playlist(url)
        except Exception as exc:
            logger.warning("Spotify resolve failed for %r: %s", url, exc)
            return templates.TemplateResponse(
                request, "partials/playlist_preview.html",
                {"error": f"Could not resolve Spotify playlist: {exc}"},
            )
    else:
        try:
            import service.providers.ytdlp  # noqa: F401  ensure registered
            from service.providers import get as get_provider
            provider = get_provider("ytdlp")()
            title, source, candidates = await provider.resolve_playlist(url)
        except Exception as exc:
            logger.warning("Playlist resolve failed for %r: %s", url, exc)
            return templates.TemplateResponse(
                request, "partials/playlist_preview.html",
                {"error": f"Could not resolve playlist: {exc}"},
            )

    # Dedup check against local library (hash + MB ID + fuzzy title match)
    from service.core.normalize import normalize as _norm
    track_statuses: list[dict[str, object]] = []
    for candidate in candidates:
        owned = False
        internal_id = make_id(candidate.artist, candidate.title, candidate.duration_seconds)

        # 1. Exact hash
        row = (await session.execute(
            select(Track).options(joinedload(Track.file)).where(Track.id == internal_id)
        )).unique().scalar_one_or_none()
        if row and row.file:
            owned = True

        # 2. MB recording ID if available
        if not owned and candidate.mb_recording_id:
            mb_row = (await session.execute(
                select(Track).options(joinedload(Track.file))
                .where(Track.musicbrainz_recording_id == candidate.mb_recording_id)
            )).unique().scalar_one_or_none()
            if mb_row and mb_row.file:
                owned = True

        # 3. Fuzzy title + artist match (normalized LIKE)
        if not owned:
            norm_title = _norm(candidate.title or "")
            norm_artist = _norm(candidate.artist or "")
            if norm_title and norm_artist:
                fuzzy = (await session.execute(
                    select(Track)
                    .join(Track.artist)
                    .join(Track.file)
                    .where(
                        func.lower(Track.title).contains(norm_title[:20]) if len(norm_title) > 4 else Track.title.ilike(f"%{norm_title}%"),
                        Artist.name.ilike(f"%{norm_artist.split()[0]}%") if norm_artist else True,
                    )
                    .limit(5)
                )).unique().scalars().all()
                for frow in fuzzy:
                    from service.search.matcher import track_similarity
                    sim = track_similarity(
                        candidate.title or "", candidate.artist or "", candidate.duration_seconds,
                        frow.title, frow.artist.name if frow.artist else "", frow.duration_seconds,
                    )
                    if sim >= 0.85:
                        owned = True
                        break

        track_statuses.append({
            "candidate": candidate,
            "candidate_json": candidate.model_dump_json(),
            "owned": owned,
            "internal_id": internal_id,
        })

    owned_count = sum(1 for t in track_statuses if t["owned"])
    return templates.TemplateResponse(
        request, "partials/playlist_preview.html",
        {
            "url": url,
            "title": title,
            "source": source,
            "tracks": track_statuses,
            "owned_count": owned_count,
            "total_count": len(track_statuses),
        },
    )


@router.post("/acquire", response_class=HTMLResponse)
async def acquire_playlist(
    request: Request,
    import_url: str = Form(...),
    import_title: str = Form(...),
    import_source: str = Form(default="unknown"),
    owned_count: int = Form(default=0),
    candidates: list[str] = Form(default=[]),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.acquisition.jobs import create_job, mark_enqueue_failed

    if not candidates:
        return HTMLResponse('<p class="empty">No tracks selected.</p>')

    now = datetime.now(UTC).replace(tzinfo=None)
    import_id = str(uuid.uuid4())

    pl_row = PlaylistImport(
        id=import_id,
        url=import_url,
        title=import_title or "Untitled Playlist",
        source=import_source,
        track_count=len(candidates) + max(owned_count, 0),
        enqueued_count=0,
        owned_count=max(owned_count, 0),
        state="active",
        created_at=now,
        updated_at=now,
    )
    session.add(pl_row)

    job_data: list[tuple[str, str, TrackCandidate]] = []
    for candidate_json in candidates:
        candidate = TrackCandidate.model_validate_json(candidate_json)
        job_id = await create_job(
            session,
            provider_name=candidate.provider,
            provider_ref=candidate.provider_ref,
            candidate=candidate,
            query=f"{candidate.artist} - {candidate.title}",
            playlist_import_id=import_id,
        )
        job_data.append((job_id, candidate_json, candidate))

    await session.commit()

    failed_ids: list[str] = []
    queued_ids: set[str] = set()
    queued_count = 0
    try:
        async with arq_pool() as redis:
            for job_id, candidate_json, candidate in job_data:
                try:
                    await enqueue_acquire_track(
                        redis, job_id,
                        provider_name=candidate.provider,
                        provider_ref=candidate.provider_ref,
                        candidate_json=candidate_json,
                    )
                    queued_count += 1
                    queued_ids.add(job_id)
                except Exception as exc:
                    failed_ids.append(job_id)
                    await mark_enqueue_failed(session, job_id, exc)
    except Exception as exc:
        # Pool creation failed, so none of the still-unaccounted jobs reached
        # Redis. Persist them as retryable instead of leaving false "queued"
        # cards behind.
        accounted = set(failed_ids) | queued_ids
        for job_id, _candidate_json, _candidate in job_data:
            if job_id not in accounted:
                failed_ids.append(job_id)
                await mark_enqueue_failed(session, job_id, exc)

    saved_playlist = await session.get(PlaylistImport, import_id)
    if saved_playlist is not None:
        saved_playlist.enqueued_count = queued_count
        saved_playlist.state = "failed" if failed_ids and not queued_count else "active"
        saved_playlist.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    return _acquisition_batch_receipt(
        request,
        batch_id=import_id,
        title=import_title or "Untitled Playlist",
        queued_count=queued_count,
        owned_count=max(owned_count, 0),
        failed_count=len(failed_ids),
        jobs_anchor=f"playlist-{import_id}",
        unit="track",
        retry_url=f"/playlists/{import_id}/retry-failed" if failed_ids else None,
        retry_ids=failed_ids,
        retry_field="job_ids",
        failed_items=[
            {"id": job_id, "title": candidate.title, "artist": candidate.artist}
            for job_id, _candidate_json, candidate in job_data
            if job_id in failed_ids
        ],
    )


@router.post("/{import_id}/retry-failed", response_class=HTMLResponse)
async def retry_failed_playlist(
    request: Request,
    import_id: str,
    session: AsyncSession = Depends(get_session),
    job_ids: list[str] = Form([]),
) -> HTMLResponse:
    """Retry only playlist tracks that failed before reaching the worker."""
    from service.acquisition.jobs import mark_enqueue_failed
    playlist = await session.get(PlaylistImport, import_id)
    if playlist is None:
        raise HTTPException(404, "Playlist import not found")
    # Direct route-function calls in unit tests receive FastAPI's Form default
    # object rather than a parsed list; HTTP requests always provide a list.
    if not isinstance(job_ids, list):
        job_ids = []

    failed_stmt = select(AcquisitionJobRow).where(
            AcquisitionJobRow.playlist_import_id == import_id,
            AcquisitionJobRow.state == "failed",
            AcquisitionJobRow.failure_class == "queue_unavailable",
        )
    if job_ids:
        failed_stmt = failed_stmt.where(AcquisitionJobRow.id.in_(job_ids))
    failed = (await session.execute(failed_stmt)).scalars().all()

    retried = 0
    retried_ids: set[str] = set()
    still_failed: list[str] = []
    try:
        async with arq_pool() as redis:
            for row in failed:
                try:
                    candidate = TrackCandidate.model_validate_json(row.candidate_json or "")
                    await enqueue_acquire_track(
                        redis,
                        row.id,
                        provider_name=row.provider,
                        provider_ref=row.provider_ref,
                        candidate_json=candidate.model_dump_json(),
                        unique_retry=True,
                    )
                    row.state = "queued"
                    row.failure_class = None
                    row.error = None
                    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    retried += 1
                    retried_ids.add(row.id)
                except Exception as exc:
                    still_failed.append(row.id)
                    await mark_enqueue_failed(session, row.id, exc)
    except Exception as exc:
        for row in failed:
            if row.id not in retried_ids and row.id not in still_failed:
                still_failed.append(row.id)
                await mark_enqueue_failed(session, row.id, exc)

    playlist = await session.get(PlaylistImport, import_id)
    if playlist is not None:
        playlist.enqueued_count += retried
        playlist.state = "failed" if still_failed and not playlist.enqueued_count else "active"
        playlist.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    queued_count = playlist.enqueued_count if playlist else retried
    owned = playlist.owned_count if playlist else 0
    remaining_failed = (await session.execute(
        select(AcquisitionJobRow).where(
            AcquisitionJobRow.playlist_import_id == import_id,
            AcquisitionJobRow.state == "failed",
            AcquisitionJobRow.failure_class == "queue_unavailable",
        )
    )).scalars().all()
    return _acquisition_batch_receipt(
        request,
        batch_id=import_id,
        title=(playlist.title if playlist else None) or "Untitled Playlist",
        queued_count=queued_count,
        owned_count=owned,
        failed_count=len(remaining_failed),
        jobs_anchor=f"playlist-{import_id}",
        unit="track",
        retry_url=f"/playlists/{import_id}/retry-failed" if remaining_failed else None,
        retry_ids=[row.id for row in remaining_failed],
        retry_field="job_ids",
        failed_items=_failed_playlist_items(list(remaining_failed)),
    )


async def _resolve_spotify_playlist(url: str) -> tuple[str, str, list[TrackCandidate]]:
    """Resolve a Spotify playlist to track candidates.

    Two paths:
    - **No credentials (default):** scrape the public embed widget's
      ``__NEXT_DATA__`` tracklist — no API key, works for public and editorial
      playlists (capped at the ~50 tracks the embed renders).
    - **Credentialed** (AUDIOREAP_SPOTIFY_CLIENT_ID set): official Web API
      client-credentials flow, with full pagination. Since the Feb 2026 API
      change this returns track ``items`` only for playlists the app/user owns;
      other playlists yield metadata only.
    """
    import re as _re

    match = _re.search(r"playlist/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("Could not extract Spotify playlist ID from URL")
    playlist_id = match.group(1)

    if not settings.spotify_client_id:
        return await _resolve_spotify_playlist_embed(playlist_id)

    token = await _spotify_client_token()

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; audioreap/0.1)",
            "Authorization": f"Bearer {token}",
        },
    ) as client:
        items: list[dict[str, object]] = []
        pl_title = "Spotify Playlist"

        r = await client.get(f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name")
        r.raise_for_status()
        pl_title = str(r.json().get("name") or pl_title)

        # /tracks is deprecated in favour of /items (Spotify Web API, Feb 2026);
        # both return the same item shape. Fall back to /tracks on 404.
        next_url: str | None = (
            f"https://api.spotify.com/v1/playlists/{playlist_id}/items"
            "?fields=items(track(name,artists,album,duration_ms,type)),next&limit=50"
        )
        _tried_legacy = False
        while next_url:
            r = await client.get(next_url)
            if r.status_code == 404 and not _tried_legacy and "/items" in next_url:
                _tried_legacy = True
                next_url = next_url.replace("/items", "/tracks", 1)
                continue
            r.raise_for_status()
            data = r.json()
            items.extend(data.get("items") or [])
            next_url = data.get("next")

    candidates: list[TrackCandidate] = []
    for item in items:
        track = (item.get("track") or {}) if isinstance(item, dict) else {}
        if not track or track.get("type") != "track":
            continue
        title = str(track.get("name") or "Unknown")
        artists = track.get("artists") or []
        artist = str(artists[0].get("name") if artists else "Unknown")
        album_obj = track.get("album") or {}
        album = str(album_obj.get("name")) if album_obj.get("name") else None
        duration_ms = track.get("duration_ms")
        duration_s = int(duration_ms) // 1000 if duration_ms else None

        search_q = f"{artist} {title}"
        yt_url = await asyncio.to_thread(_yt_search_one, search_q)

        candidates.append(TrackCandidate(
            provider="ytdlp",
            provider_ref=yt_url or f"ytsearch1:{search_q}",
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration_s,
            raw_metadata={},
        ))

    # With credentials, an empty item list means the Feb 2026 restriction kicked
    # in (the app doesn't own this playlist). Fall back to the keyless embed
    # scrape, which still returns public/editorial tracklists.
    if not candidates:
        logger.info(
            "Spotify API returned no items for %s (not owned by app) — "
            "falling back to embed scrape", playlist_id,
        )
        return await _resolve_spotify_playlist_embed(playlist_id)

    return pl_title, "spotify", candidates


async def _resolve_spotify_playlist_embed(
    playlist_id: str,
) -> tuple[str, str, list[TrackCandidate]]:
    """No-API-key path: parse the public embed widget's ``__NEXT_DATA__`` JSON.

    ``open.spotify.com/embed/playlist/{id}`` server-renders the tracklist
    (title, artist, duration) in a ``__NEXT_DATA__`` script tag readable without
    auth — it even covers editorial playlists the Web API now blocks. Limited to
    the tracks the embed renders (~50), which is fine for typical user playlists.
    YouTube source resolution is deferred to acquisition (``ytsearch1:`` ref) so
    the preview stays fast.
    """
    import json as _json
    import re as _re


    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; audioreap/0.1)"},
    ) as client:
        r = await client.get(embed_url)
        r.raise_for_status()
        html = r.text

    m = _re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, _re.S,
    )
    if not m:
        raise ValueError(
            "Could not read this Spotify playlist without API credentials "
            "(embed layout may have changed). Set AUDIOREAP_SPOTIFY_CLIENT_ID + "
            "AUDIOREAP_SPOTIFY_CLIENT_SECRET, or paste a YouTube playlist URL."
        )
    data = _json.loads(m.group(1))

    def _find(obj: object, key: str) -> object | None:
        """Depth-first search for the first value under ``key`` anywhere in the tree."""
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for v in obj.values():
                found = _find(v, key)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _find(v, key)
                if found is not None:
                    return found
        return None

    entity = _find(data, "entity")
    pl_title = "Spotify Playlist"
    if isinstance(entity, dict):
        pl_title = str(entity.get("name") or entity.get("title") or pl_title)

    track_list = _find(data, "trackList")
    candidates: list[TrackCandidate] = []
    if isinstance(track_list, list):
        for t in track_list:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            artist = str(t.get("subtitle") or "").strip()
            dur_ms = t.get("duration")
            duration_s = (
                int(dur_ms) // 1000
                if isinstance(dur_ms, (int, float)) and dur_ms else None
            )
            search_q = f"{artist} {title}".strip()
            candidates.append(TrackCandidate(
                provider="ytdlp",
                provider_ref=f"ytsearch1:{search_q}",
                title=title,
                artist=artist or "Unknown",
                album=None,
                duration_seconds=duration_s,
                raw_metadata={},
            ))

    if not candidates:
        raise ValueError(
            "Spotify returned no tracks for this playlist (it may be private or "
            "empty). Set AUDIOREAP_SPOTIFY_CLIENT_ID + AUDIOREAP_SPOTIFY_CLIENT_SECRET "
            "to import your own private playlists, or paste a YouTube playlist URL."
        )
    return pl_title, "spotify", candidates


async def _spotify_client_token() -> str:
    import base64

    creds = base64.b64encode(
        f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
    ).decode()
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {creds}"},
        )
        r.raise_for_status()
    return str(r.json()["access_token"])


def _yt_search_one(query: str) -> str:
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=False)
    if info and info.get("entries"):
        entry = info["entries"][0]
        vid_id = entry.get("id") or ""
        return str(entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}")
    return f"ytsearch1:{query}"
