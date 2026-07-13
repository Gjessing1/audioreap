"""MusicBrainz discography browsing and per-track/album acquisition."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from service.config import settings
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Album, Artist, Track
from service.db.session import get_session
from service.providers.ytdlp import yt_search_best as _yt_search_best_shared

from service.api.shared import _acquire_ctx, _error_badge, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discography")


def _yt_search_best(
    artist: str,
    title: str,
    duration_seconds: int | None = None,
    n_candidates: int = 10,
    prefer_ytm: bool = True,
) -> tuple[str, float]:
    return _yt_search_best_shared(
        artist, title, duration_seconds, n_candidates, prefer_ytm,
        prefer_explicit=settings.prefer_explicit,
    )


@router.get("", response_class=HTMLResponse)
async def discography_page(
    request: Request, q: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, "", "discover", session)
    # ?q= prefills the artist search and runs it on load (artist-page deep link).
    ctx["disco_q"] = q.strip()
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.get("/search", response_class=HTMLResponse)
async def discography_search(
    request: Request,
    q: str = "",
) -> HTMLResponse:
    if not q.strip():
        return HTMLResponse("")

    from service.metadata.musicbrainz import search_artists

    artists = await asyncio.to_thread(
        search_artists, q.strip(), 8, settings.cache_dir
    )
    return templates.TemplateResponse(
        request, "partials/artist_candidates.html", {"artists": artists, "q": q}
    )


@router.get("/{artist_mbid}/{release_group_id}/tracks", response_class=HTMLResponse)
async def discography_tracklist(
    request: Request,
    artist_mbid: str,
    release_group_id: str,
    artist: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return an HTML partial with the MB tracklist for a release group."""
    from service.metadata.musicbrainz import get_release_group_tracks

    album_title, release_id, _year, tracks = await asyncio.to_thread(
        get_release_group_tracks, release_group_id, settings.cache_dir
    )

    # Check which tracks are already in local library by MB recording ID
    from service.library.cohesion import get_owned_recording_ids
    owned_recording_ids = await get_owned_recording_ids(
        session, [t.recording_id for t in tracks if t.recording_id]
    )

    # Title fallback: find local tracks for this album so unmatched recordings
    # can still be matched by title similarity (different pressings share titles
    # but may have different MB recording IDs)
    owned_titles: set[str] = set()
    local_album_tracks = (await session.execute(
        select(Track).join(Track.album).where(Album.mb_release_group_id == release_group_id)
    )).scalars().all()
    if not local_album_tracks and album_title:
        # Fall back: find artist + album by name similarity
        from service.search.matcher import title_similarity as _tsim
        from service.core.normalize import normalize as _norm
        local_artists = (await session.execute(
            select(Artist).where(Artist.name.ilike(f"%{artist.split()[0]}%")) if artist else select(Artist).where(False)
        )).scalars().all()
        best_album: "Album | None" = None
        best_score = 0.0
        for la in local_artists:
            if _tsim(la.name, artist) < 0.80:
                continue
            albums = (await session.execute(
                select(Album).options(joinedload(Album.tracks)).where(Album.artist_id == la.id)
            )).unique().scalars().all()
            for alb in albums:
                s = _tsim(_norm(alb.title), _norm(album_title))
                if s > best_score:
                    best_score, best_album = s, alb
        if best_album and best_score >= 0.75:
            local_album_tracks = list(best_album.tracks)
    owned_titles = {t.title.lower().strip() for t in local_album_tracks}

    return templates.TemplateResponse(
        request, "partials/release_tracklist.html",
        {
            "artist": artist,
            "artist_mbid": artist_mbid,
            "release_group_id": release_group_id,
            "album_title": album_title,
            "release_id": release_id,
            "tracks": tracks,
            "owned_recording_ids": owned_recording_ids,
            "owned_titles": owned_titles,
        },
    )


@router.post("/{artist_mbid}/{release_group_id}/acquire-track", response_class=HTMLResponse)
async def discography_acquire_single_track(
    request: Request,
    artist_mbid: str,
    release_group_id: str,
    recording_id: str = Form(""),
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    track_number: str = Form(""),
    disc_number: str = Form(""),
    duration_seconds: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue acquisition of a single track from the discography tracklist."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    dur_s = int(duration_seconds) if duration_seconds.isdigit() else None

    # Pre-search YouTube Music for the best-matching studio result, filtering
    # out live concerts, tributes, and covers.
    provider_ref, yt_score = await asyncio.to_thread(
        _yt_search_best,
        artist or "Unknown",
        title or "Unknown",
        dur_s,
    )

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref=provider_ref,
        title=title or "Unknown",
        artist=artist or "Unknown",
        album=album or None,
        track_number=int(track_number) if track_number.isdigit() else None,
        disc_number=int(disc_number) if disc_number.isdigit() else None,
        duration_seconds=dur_s,
        mb_recording_id=recording_id or None,
        # Same lock the album-batch coordinator applies: keep the track anchored
        # to this album under the main discography artist. Without it, a track
        # whose MB credit is "Main feat. Guest" becomes the albumartist and the
        # album fragments into a separate featuring artist. The path segment can
        # also be a release id or local album id (album-page fallback), so only
        # store it when it's an MBID-shaped UUID.
        mb_release_group_id=(
            release_group_id
            if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", release_group_id or "")
            else None
        ),
        album_locked=True,
    )

    # If no candidate scored above the confidence floor, create a ghost job in
    # needs_review with no staging file. The review card will have the source
    # search panel open so the user can paste a URL or search manually.
    # This keeps the track visible in the queue — never silently skipped.
    if yt_score < 0.35:
        import json as _json
        from service.db.schema import AcquisitionJobRow as _JobRow
        job_id = str(uuid.uuid4())
        ghost_meta = {
            "title": candidate.title,
            "artist": candidate.artist,
            "album": candidate.album,
            "track_number": candidate.track_number,
            "duration_seconds": candidate.duration_seconds,
            "mb_recording_id": candidate.mb_recording_id,
            "force_staging_reason": (
                f"No confident YouTube match found (score: {yt_score:.2f}) — "
                f"search for the correct track or paste a YouTube link below"
            ),
        }
        from service.acquisition.jobs import _now
        row = _JobRow(
            id=job_id,
            provider="ytdlp",
            provider_ref=provider_ref,
            state="needs_review",
            query=f"{artist} – {title}",
            candidate_json=candidate.model_dump_json(),
            resolved_metadata_json=_json.dumps(ghost_meta),
            staging_path=None,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(row)
        await session.commit()
        return HTMLResponse(
            f'<span class="badge badge-warn">No match → <a href="/jobs" style="color:inherit">Review</a></span>'
        )

    job_id = await create_job(
        session,
        provider_name="ytdlp",
        provider_ref=candidate.provider_ref,
        candidate=candidate,
        query=f"{artist} – {title}",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name="ytdlp",
            provider_ref=candidate.provider_ref,
            candidate_json=candidate.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    return HTMLResponse(
        f'<span class="badge badge-busy">Queued → <a href="/jobs" style="color:inherit">Jobs</a></span>'
    )


@router.post("/{artist_mbid}/{release_group_id}/acquire", response_class=HTMLResponse)
async def discography_acquire_album(
    request: Request,
    artist_mbid: str,
    release_group_id: str,
    artist: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a coordinated album acquisition via the acquire_album_from_mb job.

    All tracks get the album metadata locked into their candidate so they land
    in the correct folder regardless of which MB release shows first in search.
    """
    from service.acquisition.album_pipeline import create_album_job
    from service.core.models import AlbumCandidate

    try:
        from arq import create_pool
        from arq.connections import RedisSettings

        # Create an AlbumAcquisitionJob row for tracking
        album_candidate = AlbumCandidate(
            provider="ytdlp",
            provider_ref=f"mbid:{release_group_id}",
            album_title="",  # filled by worker from MB
            album_artist=artist or "Unknown",
            tracks=[],
        )
        album_job_id = await create_album_job(
            session,
            provider_name="ytdlp",
            album_ref=f"mbid:{release_group_id}",
            album_candidate=album_candidate,
            query=f"{artist} album",
        )
        await session.commit()

        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_album_from_mb",
            album_job_id=album_job_id,
            release_group_id=release_group_id,
            artist_name=artist or "Unknown",
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"album_mb:{album_job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        logger.error("Discography acquire failed: %s", exc)
        return _error_badge(f"Error: {exc}")

    return HTMLResponse(
        f'<span id="disco-status-{album_job_id}"'
        f' hx-get="/discography/album-status/{album_job_id}"'
        f' hx-trigger="load, every 5s"'
        f' hx-swap="outerHTML">'
        f'Queued…'
        f'</span>'
    )


@router.get("/album-status/{album_job_id}", response_class=HTMLResponse)
async def discography_album_status(
    request: Request,
    album_job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Polling endpoint: returns a status badge for an in-progress album acquisition."""
    from service.db.schema import AlbumAcquisitionJob as _AlbumJob

    album = await session.get(_AlbumJob, album_job_id)
    if album is None:
        return _error_badge("Not found", level="fail")

    # Count child track jobs
    child_counts = (await session.execute(
        select(AcquisitionJobRow.state, func.count(AcquisitionJobRow.id))
        .where(AcquisitionJobRow.album_job_id == album_job_id)
        .group_by(AcquisitionJobRow.state)
    )).all()
    counts: dict[str, int] = {state: cnt for state, cnt in child_counts}
    total = sum(counts.values())
    review = counts.get("needs_review", 0)
    done = counts.get("done", 0)
    active = total - review - done - counts.get("failed", 0) - counts.get("cancelled", 0)

    if album.state in ("failed", "cancelled"):
        return _error_badge("Failed", level="fail")

    if review > 0 or done > 0:
        # Terminal or near-terminal: stop polling by not including hx-trigger
        parts = []
        if done:
            parts.append(f"{done} placed")
        if review:
            parts.append(f'<a href="/jobs">{review} to review</a>')
        if active:
            parts.append(f"{active} in progress")
        label = " · ".join(parts)
        badge = "badge-done" if not review and not active else "badge-busy"
        return HTMLResponse(f'<span class="badge {badge}">{label}</span>')

    # Still running: keep polling
    if active:
        label = f"Downloading ({active}/{total or '…'})"
    else:
        label = "Queued…"
    return HTMLResponse(
        f'<span id="disco-status-{album_job_id}"'
        f' hx-get="/discography/album-status/{album_job_id}"'
        f' hx-trigger="every 5s"'
        f' hx-swap="outerHTML"'
        f' class="badge badge-queued">{label}</span>'
    )


@router.get("/{artist_mbid}", response_class=HTMLResponse)
async def discography_view(
    request: Request,
    artist_mbid: str,
    # Bare `list[str] = []` is NOT bound to repeated ?types= query params by
    # FastAPI — it needs an explicit Query default, else the filter chips are a
    # server-side no-op (every request sees an empty selection).
    types: list[str] = Query([]),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.core.normalize import normalize as _normalize
    from service.metadata.musicbrainz import get_artist_release_groups
    from service.search.matcher import title_similarity

    selected_types = set(types)

    artist_name, release_groups = await asyncio.to_thread(
        get_artist_release_groups, artist_mbid, settings.cache_dir
    )

    # Empty selection means "show all types" — matches typical filter-chip UX where
    # having nothing active is the same as having everything active.
    filtered = release_groups if not selected_types else [rg for rg in release_groups if rg.release_type in selected_types]

    # Find artist in local DB by fuzzy name match; load albums with track counts
    local_albums_list: list[Album] = []
    all_local_artists = (
        await session.execute(
            select(Artist).where(Artist.name.ilike(f"%{artist_name.split()[0]}%"))
        )
    ).scalars().all()
    for la in all_local_artists:
        if title_similarity(la.name, artist_name) >= 0.85:
            local_albums_list = (
                await session.execute(
                    select(Album)
                    .options(joinedload(Album.tracks).joinedload(Track.file))
                    .where(Album.artist_id == la.id)
                )
            ).unique().scalars().all()
            break

    # Build lookup maps: release_group_id / normalized_title → (track_count,
    # cover track id). The cover id lets owned releases show local art instead
    # of hitting Cover Art Archive.
    rg_to_local: dict[str, tuple[int, str | None]] = {}
    title_to_local: dict[str, tuple[int, str | None]] = {}
    for la in local_albums_list:
        tc = len(la.tracks)
        cover_id = next((t.id for t in la.tracks if t.file), None)
        if la.mb_release_group_id:
            rg_to_local[la.mb_release_group_id] = (tc, cover_id)
        title_to_local[_normalize(la.title)] = (tc, cover_id)

    release_entries = []
    for rg in filtered:
        normalized_title = _normalize(rg.title)
        # Prefer release-group ID match, fall back to title similarity
        if rg.release_group_id in rg_to_local:
            owned = True
            owned_track_count, cover_track_id = rg_to_local[rg.release_group_id]
        else:
            best_match = max(
                ((local, title_similarity(normalized_title, local_t))
                 for local_t, local in title_to_local.items()),
                key=lambda x: x[1],
                default=((0, None), 0.0),
            )
            owned = best_match[1] >= 0.80
            owned_track_count, cover_track_id = best_match[0] if owned else (0, None)
        release_entries.append({
            "release_group_id": rg.release_group_id,
            "title": rg.title,
            "year": rg.year,
            "release_type": rg.release_type,
            "owned": owned,
            "owned_track_count": owned_track_count,
            "cover_track_id": cover_track_id,
        })

    owned_count = sum(1 for r in release_entries if r["owned"])
    all_types = sorted({rg.release_type for rg in release_groups})

    ctx = {
        "artist_name": artist_name,
        "artist_mbid": artist_mbid,
        "releases": release_entries,
        "owned_count": owned_count,
        "total_count": len(release_entries),
        "all_types": all_types,
        "selected_types": selected_types,
    }
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(request, "partials/discography_content.html", ctx)
    # Full-page load (bookmark / refresh / artist-page link): render the acquire
    # page on the Discover tab with this artist's discography preloaded, so the
    # standalone and tab UIs are one implementation.
    from urllib.parse import urlencode as _urlencode

    page_ctx = await _acquire_ctx(request, "", "discover", session)
    preload_qs = _urlencode([("types", t) for t in sorted(selected_types)])
    page_ctx["disco_preload_url"] = (
        f"/discography/{artist_mbid}" + (f"?{preload_qs}" if preload_qs else "")
    )
    return templates.TemplateResponse(request, "acquire.html", page_ctx)
