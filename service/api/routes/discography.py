"""MusicBrainz discography browsing and per-track/album acquisition."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.acquisition.queue import arq_pool, enqueue_acquire_track, enqueue_album_from_mb
from service.api.shared import (
    _acquire_ctx,
    _acquisition_batch_receipt,
    _acquisition_receipt,
    _error_badge,
    templates,
)
from service.config import settings
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Album, Artist, Track
from service.db.session import get_session
from service.providers.ytdlp import yt_search_best as _yt_search_best_shared

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


async def _owned_artist_names(session: AsyncSession) -> set[str]:
    """Normalized names of every artist in the local library (owned-marker set)."""
    from service.core.normalize import normalize as _norm

    names = (await session.execute(select(Artist.name))).scalars().all()
    return {_norm(n) for n in names if n}


@router.get("/genre-search", response_class=HTMLResponse)
async def discography_genre_search(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Browse MB artists by genre/tag — the 'surprise me' discovery mode.

    Same result target as the artist-name search; each hit deep-links into the
    artist's discography. Owned artists are badged so unexplored names stand out.
    """
    if not q.strip():
        return HTMLResponse("")

    from service.core.normalize import normalize as _norm
    from service.metadata.musicbrainz import search_artists_by_tag

    artists = await asyncio.to_thread(
        search_artists_by_tag, q.strip().lower(), 20, settings.cache_dir
    )
    owned = await _owned_artist_names(session)
    return templates.TemplateResponse(
        request, "partials/genre_artist_results.html",
        {
            "artists": artists,
            "q": q,
            "owned_names": {a.artist_id for a in artists if _norm(a.name) in owned},
        },
    )


@router.get("/for-you", response_class=HTMLResponse)
async def discography_for_you(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Proactive suggestion rail: un-owned artists connected to the library.

    Seeds = the most-owned artists with MB IDs plus the library's top genres;
    ranking happens in metadata/suggest.py. Loaded lazily when the Discover tab
    is first shown — a cold cache costs ~1 MB request/second, so the rail can
    take a few seconds on first build, then it's disk-cached for 24 h.
    """
    from service.metadata.suggest import MAX_SEED_ARTISTS, MAX_SEED_GENRES, build_for_you

    seed_rows = (await session.execute(
        select(Artist.musicbrainz_artist_id, Artist.name)
        .join(Track, Track.artist_id == Artist.id)
        .where(Artist.musicbrainz_artist_id.is_not(None))
        .group_by(Artist.id)
        .order_by(func.count(Track.id).desc())
        .limit(MAX_SEED_ARTISTS)
    )).all()
    seeds = [(mbid, name) for mbid, name in seed_rows if mbid]

    genres = (await session.execute(
        select(Track.genre)
        .where(Track.genre.is_not(None), Track.genre != "")
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc())
        .limit(MAX_SEED_GENRES)
    )).scalars().all()

    if not seeds and not genres:
        return HTMLResponse("")  # empty/unlinked library — the rail simply doesn't exist

    owned = await _owned_artist_names(session)
    suggestions = await asyncio.to_thread(
        build_for_you, seeds, list(genres), owned, settings.cache_dir
    )
    return templates.TemplateResponse(
        request, "partials/for_you_rail.html", {"suggestions": suggestions}
    )


@router.get("/{artist_mbid}/related", response_class=HTMLResponse)
async def discography_related_artists(
    request: Request,
    artist_mbid: str,
) -> HTMLResponse:
    """Lazy-loaded rail of musically-related artists (MB artist relationships)."""
    from service.metadata.musicbrainz import get_related_artists

    related = await asyncio.to_thread(
        get_related_artists, artist_mbid, settings.cache_dir
    )
    return templates.TemplateResponse(
        request, "partials/related_artists.html", {"related": related}
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
        from service.core.normalize import normalize as _norm
        from service.search.matcher import title_similarity as _tsim
        local_artists = (await session.execute(
            select(Artist).where(Artist.name.ilike(f"%{artist.split()[0]}%")) if artist else select(Artist).where(False)
        )).scalars().all()
        best_album: Album | None = None
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


async def _create_ghost_review_job(
    session: AsyncSession,
    candidate: TrackCandidate,
    provider_ref: str,
    yt_score: float,
    query: str,
) -> tuple[str, bool]:
    """Park an unmatched discography track in needs_review with no staging file.

    The review card will have the source search panel open so the user can
    paste a URL or search manually. This keeps the track visible in the
    queue — never silently skipped.
    """
    import json as _json

    from service.acquisition.jobs import ACTIVE_ACQUISITION_STATES, _now

    existing = (await session.execute(
        select(AcquisitionJobRow).where(
            AcquisitionJobRow.provider == "ytdlp",
            AcquisitionJobRow.provider_ref == provider_ref,
            AcquisitionJobRow.state.in_(ACTIVE_ACQUISITION_STATES),
        ).order_by(AcquisitionJobRow.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return existing.id, False

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
    job_id = str(uuid.uuid4())
    session.add(AcquisitionJobRow(
        id=job_id,
        provider="ytdlp",
        provider_ref=provider_ref,
        state="needs_review",
        query=query,
        candidate_json=candidate.model_dump_json(),
        resolved_metadata_json=_json.dumps(ghost_meta),
        staging_path=None,
        created_at=_now(),
        updated_at=_now(),
    ))
    await session.commit()
    return job_id, True


def _single_track_candidate(
    release_group_id: str,
    provider_ref: str,
    *,
    title: str,
    artist: str,
    album: str,
    track_number: str,
    disc_number: str,
    duration_seconds: int | None,
    recording_id: str,
) -> TrackCandidate:
    """Build the locked TrackCandidate for a single discography-tracklist row."""
    return TrackCandidate(
        provider="ytdlp",
        provider_ref=provider_ref,
        title=title or "Unknown",
        artist=artist or "Unknown",
        album=album or None,
        track_number=int(track_number) if track_number.isdigit() else None,
        disc_number=int(disc_number) if disc_number.isdigit() else None,
        duration_seconds=duration_seconds,
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


@router.get("/{artist_mbid}/{release_group_id}/source-preview", response_class=HTMLResponse)
async def discography_source_preview(
    request: Request,
    artist_mbid: str,
    release_group_id: str,
    title: str = "",
    artist: str = "",
    duration_seconds: str = "",
    row_id: str = "",
) -> HTMLResponse:
    """Score the YouTube source for one tracklist row *without* downloading it.

    Runs the same scorer the acquire-track auto-picker uses (yt_search_ranked is
    a superset pool of yt_search_best) and renders the winning candidate inline,
    so the user sees source quality before committing to a download.
    """
    from service.providers.ytdlp import yt_search_ranked

    dur_s = int(duration_seconds) if duration_seconds.isdigit() else None
    ranked = await asyncio.to_thread(
        yt_search_ranked,
        artist or "Unknown",
        title or "Unknown",
        dur_s,
        prefer_explicit=settings.prefer_explicit,
    )
    return templates.TemplateResponse(
        request, "partials/yt_source_preview.html",
        {"best": ranked[0] if ranked else None, "row_id": row_id},
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
    from service.acquisition.jobs import create_or_get_active_job, mark_enqueue_failed

    dur_s = int(duration_seconds) if duration_seconds.isdigit() else None

    # Pre-search YouTube Music for the best-matching studio result, filtering
    # out live concerts, tributes, and covers.
    provider_ref, yt_score = await asyncio.to_thread(
        _yt_search_best,
        artist or "Unknown",
        title or "Unknown",
        dur_s,
    )

    candidate = _single_track_candidate(
        release_group_id, provider_ref,
        title=title, artist=artist, album=album,
        track_number=track_number, disc_number=disc_number,
        duration_seconds=dur_s, recording_id=recording_id,
    )

    # No candidate scored above the confidence floor — park it for review.
    if yt_score < 0.35:
        job_id, created = await _create_ghost_review_job(
            session, candidate, provider_ref, yt_score, query=f"{artist} – {title}"
        )
        return _acquisition_receipt(
            request,
            job_id=job_id,
            title=candidate.title,
            artist=candidate.artist,
            state="needs_review",
            created=created,
        )

    job_id, created = await create_or_get_active_job(
        session,
        provider_name="ytdlp",
        provider_ref=candidate.provider_ref,
        candidate=candidate,
        query=f"{artist} – {title}",
    )
    await session.commit()

    if created:
        try:
            async with arq_pool() as redis:
                await enqueue_acquire_track(
                    redis, job_id,
                    provider_name="ytdlp",
                    provider_ref=candidate.provider_ref,
                    candidate_json=candidate.model_dump_json(),
                )
        except Exception as exc:
            await mark_enqueue_failed(session, job_id, exc)
            raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    row = await session.get(AcquisitionJobRow, job_id)
    return _acquisition_receipt(
        request,
        job_id=job_id,
        title=candidate.title,
        artist=candidate.artist,
        state=row.state if row else "queued",
        created=created,
    )


@router.post("/{artist_mbid}/{release_group_id}/acquire", response_class=HTMLResponse)
async def discography_acquire_album(
    request: Request,
    artist_mbid: str,
    release_group_id: str,
    artist: str = Form(""),
    album_title: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a coordinated album acquisition via the acquire_album_from_mb job.

    All tracks get the album metadata locked into their candidate so they land
    in the correct folder regardless of which MB release shows first in search.
    """
    from service.acquisition.album_pipeline import create_or_get_active_album_job
    from service.core.models import AlbumCandidate

    # Create the coordinator row before Redis sees its ID. The Jobs page can
    # now acknowledge the album even while the worker is fetching its tracklist.
    album_candidate = AlbumCandidate(
        provider="ytdlp",
        provider_ref=f"mbid:{release_group_id}",
        album_title=album_title,
        album_artist=artist or "Unknown",
        tracks=[],
    )
    album_job_id, created = await create_or_get_active_album_job(
        session,
        provider_name="ytdlp",
        album_ref=f"mbid:{release_group_id}",
        album_candidate=album_candidate,
        query=f"{artist} — {album_title or 'album'}",
    )
    await session.commit()

    failed = False
    if created:
        try:
            async with arq_pool() as redis:
                await enqueue_album_from_mb(
                    redis, album_job_id,
                    release_group_id=release_group_id,
                    artist_name=artist or "Unknown",
                    job_key_prefix="album_mb",
                )
        except Exception as exc:
            logger.error("Discography acquire failed: %s", exc)
            from service.db.schema import AlbumAcquisitionJob
            row = await session.get(AlbumAcquisitionJob, album_job_id)
            if row is not None:
                row.state = "failed"
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()
            failed = True

    return _acquisition_batch_receipt(
        request,
        batch_id=album_job_id,
        title=album_title or f"{artist or 'Unknown artist'} album",
        queued_count=0 if failed else 1,
        failed_count=1 if failed else 0,
        jobs_anchor=f"album-{album_job_id}",
        unit="album",
        retry_url="/discography/retry-albums" if failed else None,
        retry_ids=[album_job_id] if failed else None,
        failed_items=([
            {
                "id": album_job_id,
                "title": album_title or "Album",
                "artist": artist or "Unknown artist",
            }
        ] if failed else None),
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


async def _release_entries(
    session: AsyncSession,
    artist_mbid: str,
    selected_types: set[str],
) -> tuple[str, list[dict], list[str]]:
    """Fetch an artist's MB release groups and mark which are locally owned.

    Returns (artist_name, release_entries, all_types). Ownership is anchored on
    release-group ID when the local Album row has one, falling back to title
    similarity — the same matching the artist page uses.
    """
    from service.core.normalize import normalize as _normalize
    from service.metadata.musicbrainz import get_artist_release_groups
    from service.search.matcher import title_similarity

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

    all_types = sorted({rg.release_type for rg in release_groups})
    return artist_name, release_entries, all_types


@router.post("/{artist_mbid}/acquire-missing", response_class=HTMLResponse)
async def discography_acquire_missing(
    request: Request,
    artist_mbid: str,
    types: list[str] = Form([]),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue album jobs for every un-owned release group shown in the Discography view.

    Unlike the artist-page variant this needs no local Artist row — it works
    straight from MB search results, before anything by the artist is owned.
    Respects the active type-filter chips: only the release types currently
    shown are queued.
    """
    try:
        artist_name, release_entries, _ = await _release_entries(
            session, artist_mbid, set(types)
        )
    except Exception as exc:
        return _error_badge(f"MB lookup failed: {exc}")

    unowned = [r for r in release_entries if not r["owned"]]
    if not unowned:
        return HTMLResponse('<span class="badge badge-done">All shown releases already owned ✓</span>')

    from service.acquisition.album_pipeline import create_or_get_active_album_job
    from service.core.models import AlbumCandidate
    from service.db.schema import AlbumAcquisitionJob

    batches: list[tuple[str, dict[str, object], bool]] = []
    for release in unowned:
        candidate = AlbumCandidate(
            provider="ytdlp",
            provider_ref=f"mbid:{release['release_group_id']}",
            album_title=str(release["title"]),
            album_artist=artist_name,
            tracks=[],
        )
        album_job_id, created = await create_or_get_active_album_job(
            session,
            provider_name="ytdlp",
            album_ref=candidate.provider_ref,
            album_candidate=candidate,
            query=f"{artist_name} — {candidate.album_title}",
        )
        batches.append((album_job_id, release, created))
    await session.commit()

    queued_ids = {album_job_id for album_job_id, _release, created in batches if not created}
    failed_ids: list[str] = []
    try:
        async with arq_pool() as redis:
            for album_job_id, release, created in batches:
                if not created:
                    continue
                try:
                    await enqueue_album_from_mb(
                        redis,
                        album_job_id,
                        release_group_id=str(release["release_group_id"]),
                        artist_name=artist_name,
                    )
                    queued_ids.add(album_job_id)
                except Exception:
                    failed_ids.append(album_job_id)
    except Exception:
        failed_ids.extend(
            album_job_id for album_job_id, _release, created in batches
            if created
            and album_job_id not in queued_ids and album_job_id not in failed_ids
        )

    if failed_ids:
        now = datetime.now(UTC).replace(tzinfo=None)
        failed_rows = (await session.execute(
            select(AlbumAcquisitionJob).where(AlbumAcquisitionJob.id.in_(failed_ids))
        )).scalars().all()
        for row in failed_rows:
            row.state = "failed"
            row.updated_at = now
        await session.commit()

    first_id = batches[0][0]
    return _acquisition_batch_receipt(
        request,
        batch_id=first_id,
        title=f"{artist_name} discography",
        queued_count=len(queued_ids),
        owned_count=len(release_entries) - len(unowned),
        failed_count=len(failed_ids),
        jobs_anchor=f"album-{first_id}",
        unit="release",
        retry_url="/discography/retry-albums" if failed_ids else None,
        retry_ids=failed_ids,
        failed_items=[
            {
                "id": album_job_id,
                "title": str(release["title"]),
                "artist": artist_name,
            }
            for album_job_id, release, _created in batches
            if album_job_id in failed_ids
        ],
    )


@router.post("/retry-albums", response_class=HTMLResponse)
async def discography_retry_albums(
    request: Request,
    batch_ids: list[str] = Form([]),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Retry album coordinators that failed before reaching the worker."""
    from service.db.schema import AlbumAcquisitionJob

    rows = (await session.execute(
        select(AlbumAcquisitionJob).where(
            AlbumAcquisitionJob.id.in_(batch_ids),
            AlbumAcquisitionJob.state == "failed",
        )
    )).scalars().all()
    if not rows:
        return _error_badge("No failed album batches to retry")

    queued_ids: set[str] = set()
    failed_ids: list[str] = []
    try:
        async with arq_pool() as redis:
            for row in rows:
                release_group_id = row.album_ref.removeprefix("mbid:")
                try:
                    await enqueue_album_from_mb(
                        redis,
                        row.id,
                        release_group_id=release_group_id,
                        artist_name=row.album_artist or "Unknown",
                        job_key_prefix=f"album_retry_{uuid.uuid4().hex[:8]}",
                    )
                    row.state = "queued"
                    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                    queued_ids.add(row.id)
                except Exception:
                    failed_ids.append(row.id)
    except Exception:
        failed_ids.extend(
            row.id for row in rows
            if row.id not in queued_ids and row.id not in failed_ids
        )
    await session.commit()

    first = rows[0]
    return _acquisition_batch_receipt(
        request,
        batch_id=first.id,
        title=(
            f"{first.album_artist} album batch"
            if len(rows) == 1
            else f"{first.album_artist or 'Artist'} discography"
        ),
        queued_count=len(queued_ids),
        failed_count=len(failed_ids),
        jobs_anchor=f"album-{first.id}",
        unit="release",
        retry_url="/discography/retry-albums" if failed_ids else None,
        retry_ids=failed_ids,
        failed_items=[
            {
                "id": row.id,
                "title": row.album_title or "Album",
                "artist": row.album_artist or "Unknown artist",
            }
            for row in rows
            if row.id in failed_ids
        ],
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
    selected_types = set(types)
    artist_name, release_entries, all_types = await _release_entries(
        session, artist_mbid, selected_types
    )
    owned_count = sum(1 for r in release_entries if r["owned"])

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
