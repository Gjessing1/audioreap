"""HTMX-rendered web UI routes."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.config import settings
from service.core.models import AcquisitionJob, TrackCandidate, TrackQuality, TrackRef
from service.db.schema import AcquisitionJobRow, Album, Artist, DeletedTrack, PlaylistImport, Track, TrackFile
from service.library.writer import safe_trash
from service.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
# Expose settings as Jinja globals so partials (browse_row, etc.) can use them without
# requiring every calling endpoint to pass them in context explicitly.
templates.env.globals["settings"] = settings

_AUDIO_SUFFIXES = frozenset({".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav"})


def _trash_empty_album_dir(album_dir: Path, trash_dir: Path) -> None:
    """If album_dir has no audio files left, trash remaining sidecars and rmdir it.

    Called after a track is deleted so ghost directories (with only cover.jpg)
    don't cause Navidrome to show phantom albums.
    """
    if not album_dir.is_dir():
        return
    entries = list(album_dir.iterdir())
    if any(e.suffix.lower() in _AUDIO_SUFFIXES for e in entries):
        return
    for e in entries:
        try:
            safe_trash(e, trash_dir)
        except Exception:
            pass
    try:
        album_dir.rmdir()
    except OSError:
        pass

_JOBS_COMPLETED_PAGE = 50
_BROWSE_PAGE = 75
_COMPLETED_STATES = ("done", "failed", "cancelled")
_ACTIVE_STATES_EXCLUDE = _COMPLETED_STATES  # states NOT in active list

_scan_task: asyncio.Task | None = None  # deduplicate concurrent auto-scans


async def _run_auto_scan() -> None:
    """Incremental scan + orphan cleanup in a throw-away session."""
    from service.db.session import AsyncSessionLocal
    from service.index.scanner import scan as _scan
    from service.db.schema import Album as _Album, Artist as _Artist, Track as _Track, TrackFile as _TrackFile

    try:
        async with AsyncSessionLocal() as session:
            await _scan(session, settings.music_dir, incremental=True)
            # Orphan cleanup: remove Album/Artist rows that lost all their tracks
            # (e.g. after a delete_track that leaves an empty album/artist)
            await session.execute(
                sa_delete(_Album).where(
                    ~_Album.id.in_(select(_Track.album_id).where(_Track.album_id.is_not(None)))
                )
            )
            await session.execute(
                sa_delete(_Artist).where(
                    ~_Artist.id.in_(select(_Track.artist_id))
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("Auto-scan failed: %s", exc)


def _schedule_auto_scan() -> None:
    """Fire-and-forget incremental scan after a mutation.  Skips if one is already running."""
    global _scan_task
    if _scan_task is not None and not _scan_task.done():
        return  # already in progress
    try:
        loop = asyncio.get_running_loop()
        _scan_task = loop.create_task(_run_auto_scan())
    except RuntimeError:
        pass  # no running loop (tests, startup)


async def _do_scans() -> None:
    """Trigger Navidrome rescan and schedule audioreap incremental scan."""
    from service.navidrome.client import trigger_scan as _nv
    try:
        await _nv()
    except Exception:
        pass
    _schedule_auto_scan()


async def _job_list_ctx(session: AsyncSession) -> dict[str, object]:
    """Build the paginated job list template context (first page of completed)."""
    active_rows = (await session.execute(
        select(AcquisitionJobRow)
        .where(AcquisitionJobRow.state.notin_(_ACTIVE_STATES_EXCLUDE))
        .order_by(AcquisitionJobRow.created_at.desc())
        .limit(200)
    )).scalars().all()
    completed_rows = (await session.execute(
        select(AcquisitionJobRow)
        .where(AcquisitionJobRow.state.in_(_COMPLETED_STATES))
        .order_by(AcquisitionJobRow.created_at.desc(), AcquisitionJobRow.id.desc())
        .limit(_JOBS_COMPLETED_PAGE + 1)
    )).scalars().all()
    has_more = len(completed_rows) > _JOBS_COMPLETED_PAGE
    page = list(completed_rows[:_JOBS_COMPLETED_PAGE])
    rows = list(active_rows) + page
    ctx = _grouped_jobs(rows)
    ctx["completed_has_more"] = has_more
    ctx["completed_cursor_ts"] = page[-1].created_at.isoformat() if page else ""
    ctx["completed_cursor_id"] = page[-1].id if page else ""

    # Build structured review groups (album batches + solo items)
    review_rows_only = [r for r in active_rows if r.state == "needs_review"]
    review_jobs_only: list[AcquisitionJob] = ctx["review"]  # type: ignore[assignment]
    review_groups, safe_ids = await _build_review_groups(session, review_rows_only, review_jobs_only)
    ctx["review_groups"] = review_groups
    ctx["safe_review_ids"] = safe_ids
    ctx["safe_review_count"] = len(safe_ids)
    return ctx


from service.core.job_model import job_row_to_model as _job_to_model


def _track_to_ref(row: Track) -> TrackRef:
    file = row.file
    quality: TrackQuality | None = None
    local_path: Path | None = None
    if file:
        quality = TrackQuality(
            codec=file.codec, container=file.container,
            bitrate_kbps=file.bitrate_kbps, sample_rate_hz=file.sample_rate_hz,
        )
        local_path = Path(file.path)
    return TrackRef(
        internal_id=row.id,
        source="local",
        status="available" if file else "missing",
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        duration_seconds=row.duration_seconds,
        local_path=local_path,
        quality=quality,
        musicbrainz_recording_id=row.musicbrainz_recording_id,
    )


# ── Pages ─────────────────────────────────────────────────────────────────

@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    return RedirectResponse("/acquire")


async def _acquire_ctx(request: Request, q: str, active_section: str, session: AsyncSession) -> dict:
    """Build context dict shared by the /acquire page."""
    from sqlalchemy import func as sa_func
    rows = (
        await session.execute(
            select(PlaylistImport).order_by(PlaylistImport.created_at.desc()).limit(10)
        )
    ).scalars().all()
    _ACTIVE = ("queued", "downloading", "processing", "enriching", "tagging", "importing")
    import_states: dict[str, str] = {}
    if rows:
        active_counts = (await session.execute(
            select(AcquisitionJobRow.playlist_import_id, sa_func.count())
            .where(
                AcquisitionJobRow.playlist_import_id.in_([r.id for r in rows]),
                AcquisitionJobRow.state.in_(list(_ACTIVE)),
            )
            .group_by(AcquisitionJobRow.playlist_import_id)
        )).all()
        active_by_id = {pid: cnt for pid, cnt in active_counts}
        for r in rows:
            import_states[r.id] = "active" if active_by_id.get(r.id, 0) > 0 else "done"
    return {
        "active": "acquire",
        "q": q,
        "active_section": active_section,
        "imports": rows,
        "import_states": import_states,
        "spotify_enabled": bool(settings.spotify_client_id),
    }


@router.get("/acquire", response_class=HTMLResponse)
async def acquire_page(
    request: Request, q: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, q, "search", session)
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, q: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, q, "search", session)
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.get("/search/results", response_class=HTMLResponse)
async def search_results(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.core.normalize import normalize as _norm

    tracks: list[TrackRef] = []
    if q:
        # Split query into meaningful tokens (skip single-char stopwords).
        # Fetch candidates matching ANY token across title or artist, then rank
        # by how many tokens match the combined "artist title" string.
        tokens = [t for t in q.lower().split() if len(t) > 1]
        if not tokens:
            tokens = [q.lower()]

        from sqlalchemy import or_
        token_filters = or_(
            *[Track.title.ilike(f"%{tok}%") | Artist.name.ilike(f"%{tok}%") for tok in tokens]
        )
        stmt = (
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .outerjoin(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(token_filters)
            .limit(200)
        )
        rows = (await session.execute(stmt)).unique().scalars().all()

        # Score by how many tokens appear in the combined "artist title" string
        def _score(row: Track) -> int:
            haystack = _norm(f"{row.artist.name} {row.title}").lower()
            return sum(1 for tok in tokens if tok in haystack)

        rows_sorted = sorted(rows, key=_score, reverse=True)[:30]
        tracks = [_track_to_ref(r) for r in rows_sorted]

    return templates.TemplateResponse(
        request, "partials/local_results.html", {"tracks": tracks, "q": q}
    )




def _grouped_jobs(rows: list[AcquisitionJobRow]) -> dict[str, object]:
    """Split job rows into review / active / completed groups for the UI."""
    review, active, completed = [], [], []
    for r in rows:
        j = _job_to_model(r)
        if r.state == "needs_review":
            review.append(j)
        elif r.state in _COMPLETED_STATES:
            completed.append(j)
        else:
            active.append(j)
    return {
        "review": review,
        "active": active,
        "completed": completed,
        "completed_has_more": False,
        "completed_next_offset": 0,
    }


async def _build_review_groups(
    session: AsyncSession,
    review_rows: list[AcquisitionJobRow],
    review_jobs: list[AcquisitionJob],
) -> tuple[list[dict], list[str]]:
    """Group review jobs: album batches together, solo jobs individually.

    Returns (review_groups, safe_ids) where safe_ids are jobs eligible for
    one-click approval (AcoustID verified, no flags, staging file present).

    Each review_group item is either:
      {"type": "single", "job": AcquisitionJob}
      {"type": "album", "album_job_id": str, "label": str, "jobs": list,
       "clean_ids": list[str], "clean_count": int, "flagged_count": int, "total_count": int}
    """
    from service.db.schema import AlbumAcquisitionJob as _AlbumJob

    # Fetch labels for all album_job_ids in one query
    album_job_ids = {r.album_job_id for r in review_rows if r.album_job_id}
    album_labels: dict[str, str] = {}
    if album_job_ids:
        album_jobs = (await session.execute(
            select(_AlbumJob).where(_AlbumJob.id.in_(album_job_ids))
        )).scalars().all()
        for aj in album_jobs:
            parts = [p for p in [aj.album_artist, aj.album_title] if p]
            album_labels[aj.id] = " — ".join(parts) if parts else aj.id[:8]

    # Classify each review item
    album_buckets: dict[str, list[dict]] = {}
    singles: list[dict] = []
    safe_ids: list[str] = []

    for r, j in zip(review_rows, review_jobs):
        is_flagged = False
        is_acoustid_verified = False
        flag_reason: str | None = None
        has_staging = bool(r.staging_path and Path(r.staging_path).exists())

        if not has_staging:
            is_flagged = True
            flag_reason = "Staging file missing — use Re-download"
        elif r.resolved_metadata_json:
            try:
                m = json.loads(r.resolved_metadata_json)
                if m.get("force_staging_reason"):
                    is_flagged = True
                    flag_reason = m["force_staging_reason"]
                if m.get("mb_match_source") == "acoustid":
                    is_acoustid_verified = True
            except Exception:
                pass

        # Safe = AcoustID verified, no flags, staging file present
        # Only standalone (non-album-batch) jobs go into the top-level "Approve N verified"
        # button. Album-batch jobs are handled per-batch by the "Approve N clean" button.
        if has_staging and is_acoustid_verified and not is_flagged and not r.album_job_id:
            safe_ids.append(j.id)

        item = {"job": j, "is_flagged": is_flagged, "flag_reason": flag_reason}
        if r.album_job_id:
            album_buckets.setdefault(r.album_job_id, []).append(item)
        else:
            singles.append({"type": "single", "job": j})

    groups: list[dict] = list(singles)
    for ajid, items in album_buckets.items():
        clean_ids = [it["job"].id for it in items if not it["is_flagged"]]
        groups.append({
            "type": "album",
            "album_job_id": ajid,
            "label": album_labels.get(ajid, ajid[:8]),
            "jobs": items,
            "clean_ids": clean_ids,
            "clean_count": len(clean_ids),
            "flagged_count": sum(1 for it in items if it["is_flagged"]),
            "total_count": len(items),
        })
    return groups, safe_ids


async def _synthesize_review_meta(row: AcquisitionJobRow) -> dict:
    """Build resolved_metadata for staged items that pre-date Phase 13."""
    from service.library.tagger import read_tags
    from service.core.models import TrackCandidate

    staging_path = Path(row.staging_path) if row.staging_path else None
    tagged = None
    if staging_path and staging_path.exists():
        tagged = await asyncio.to_thread(read_tags, staging_path)

    candidate: TrackCandidate | None = None
    if row.candidate_json:
        try:
            candidate = TrackCandidate.model_validate_json(row.candidate_json)
        except Exception:
            pass

    title = (tagged.title if tagged else None) or (candidate.title if candidate else None) or row.query or "Unknown"
    artist = (tagged.artist if tagged else None) or (candidate.artist if candidate else None) or "Unknown"
    album = (tagged.album if tagged else None) or (candidate.album if candidate else None)
    year = (tagged.year if tagged else None) or (candidate.year if candidate else None)
    track_number = (tagged.track_number if tagged else None) or (candidate.track_number if candidate else None)
    disc_number = (tagged.disc_number if tagged else None)
    duration = (tagged.duration_seconds if tagged else None) or (candidate.duration_seconds if candidate else None)
    ext = (staging_path.suffix.lstrip(".") if staging_path else None) or "ogg"

    mb_release_id: str | None = None
    if staging_path and staging_path.exists():
        mb_release_id = _read_mb_release_id(staging_path)

    genre = (tagged.genre if tagged else None) or None

    return {
        "title": title,
        "artist": artist,
        "albumartist": (tagged.albumartist if tagged else None) or artist,
        "album": album,
        "year": year,
        "original_year": None,
        "track_number": track_number,
        "disc_number": disc_number,
        "duration_seconds": duration,
        "ext": ext,
        "mb_recording_id": None,
        "mb_release_id": mb_release_id,
        "mb_artist_id": None,
        "mb_artist_sort": None,
        "acoustid_confidence": None,
        "mb_match_source": None,
        "is_compilation": False,
        "force_staging_reason": row.error,
        "quality_score": 0.0,
        "thumbnail_url": candidate.thumbnail_url if candidate else None,
        "genre": genre,
    }


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ctx = await _job_list_ctx(session)
    return templates.TemplateResponse(
        request, "jobs.html", {"active": "jobs", **ctx}
    )


@router.get("/jobs/list", response_class=HTMLResponse)
async def jobs_list_partial(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ctx = await _job_list_ctx(session)
    return templates.TemplateResponse(
        request, "partials/job_list.html", ctx
    )


@router.get("/jobs/completed/more", response_class=HTMLResponse)
async def jobs_completed_more(
    request: Request,
    after_ts: str = "",
    after_id: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return additional completed job cards, cursor-paginated by (created_at, id)."""
    from datetime import datetime as _dt
    stmt = (
        select(AcquisitionJobRow)
        .where(AcquisitionJobRow.state.in_(_COMPLETED_STATES))
        .order_by(AcquisitionJobRow.created_at.desc(), AcquisitionJobRow.id.desc())
        .limit(_JOBS_COMPLETED_PAGE + 1)
    )
    if after_ts:
        try:
            cursor_dt = _dt.fromisoformat(after_ts)
            if after_id:
                stmt = stmt.where(
                    (AcquisitionJobRow.created_at < cursor_dt)
                    | ((AcquisitionJobRow.created_at == cursor_dt) & (AcquisitionJobRow.id < after_id))
                )
            else:
                stmt = stmt.where(AcquisitionJobRow.created_at < cursor_dt)
        except ValueError:
            pass
    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > _JOBS_COMPLETED_PAGE
    page = list(rows[:_JOBS_COMPLETED_PAGE])
    jobs = [_job_to_model(r) for r in page]
    next_ts = page[-1].created_at.isoformat() if page else ""
    next_id = page[-1].id if page else ""
    return templates.TemplateResponse(
        request, "partials/jobs_completed_more.html",
        {"completed": jobs, "completed_has_more": has_more,
         "completed_cursor_ts": next_ts, "completed_cursor_id": next_id},
    )


@router.get("/jobs/status/{job_id}", response_class=HTMLResponse)
async def job_status_partial(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )


@router.post("/jobs/retry/{job_id}", response_class=HTMLResponse)
async def retry_job(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)
    if not row.candidate_json:
        raise HTTPException(400, "No candidate data")

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        # Use a unique arq job ID per retry so arq's NX dedup doesn't block
        # re-enqueue when a prior attempt's key still lingers in Redis.
        retry_suffix = uuid.uuid4().hex[:8]
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=row.provider,
            provider_ref=row.provider_ref,
            candidate_json=row.candidate_json,
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}:{retry_suffix}",
        )
        await redis.aclose()
        row.state = "queued"
        row.failure_class = None
        row.error = None
        await session.flush()
        await session.commit()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc

    return templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )


@router.post("/jobs/cancel/{job_id}", response_class=HTMLResponse)
async def cancel_job(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)
    if row.state in _COMPLETED_STATES:
        return templates.TemplateResponse(
            request, "partials/job_card.html", {"job": _job_to_model(row)}
        )
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.zrem("arq:queue", f"acquire:{job_id}")
        await redis.aclose()
    except Exception:
        pass  # best-effort dequeue

    row.state = "cancelled"
    row.error = "Cancelled by user"
    row.updated_at = datetime.utcnow()
    await session.flush()
    await session.commit()

    return templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )


@router.get("/nav/review-count", response_class=HTMLResponse)
async def nav_review_count(
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Returns a badge span with the needs_review job count for the nav bar."""
    count = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state == "needs_review")
        )
    ).scalar_one()
    if count:
        return HTMLResponse(f'<span class="nav-badge" hx-get="/nav/review-count" hx-trigger="every 30s" hx-swap="outerHTML">{count}</span>')
    return HTMLResponse('<span hx-get="/nav/review-count" hx-trigger="every 30s" hx-swap="outerHTML"></span>')


# ── Review workflow (needs_review state) ─────────────────────────────────────


@router.get("/jobs/{job_id}/review-card", response_class=HTMLResponse)
async def review_card(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)

    if row.state == "needs_review" and row.resolved_metadata_json:
        meta = json.loads(row.resolved_metadata_json)
    elif row.state == "needs_review" and row.staging_path:
        meta = await _synthesize_review_meta(row)
        row.resolved_metadata_json = json.dumps(meta)
        row.state = "needs_review"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    else:
        raise HTTPException(400, "Job not reviewable")

    staging_exists = bool(row.staging_path and Path(row.staging_path).exists())
    is_enrichment = bool(meta.get("is_enrichment"))

    from sqlalchemy import distinct as _distinct
    genre_rows = (await session.execute(
        select(_distinct(Track.genre)).where(Track.genre.isnot(None)).order_by(Track.genre)
    )).scalars().all()
    genres = [g for g in genre_rows if g]

    # Album consistency check: warn if albumartist differs from sibling tracks in the same album job
    album_consistency_warning: str | None = None
    if row.album_job_id and not is_enrichment:
        siblings = (await session.execute(
            select(AcquisitionJobRow.resolved_metadata_json)
            .where(
                AcquisitionJobRow.album_job_id == row.album_job_id,
                AcquisitionJobRow.id != job_id,
                AcquisitionJobRow.state == "needs_review",
                AcquisitionJobRow.resolved_metadata_json.isnot(None),
            )
        )).scalars().all()
        this_aa = (meta.get("albumartist") or "").lower().strip()
        if this_aa and siblings:
            sibling_aas = set()
            for sib_json in siblings:
                try:
                    sib_meta = json.loads(sib_json)
                    sib_aa = (sib_meta.get("albumartist") or "").lower().strip()
                    if sib_aa:
                        sibling_aas.add(sib_aa)
                except Exception:
                    pass
            other_aas = sibling_aas - {this_aa}
            if other_aas:
                album_consistency_warning = (
                    f"Album artist mismatch: this track has \"{meta.get('albumartist')}\""
                    f" but {len(other_aas)} other track(s) in this batch differ."
                )

    from service.library.tagger import parse_artists as _parse_artists
    parsed_artists = _parse_artists(meta.get("artist") or "")
    show_multi_artists = len(parsed_artists) > 1

    return templates.TemplateResponse(
        request, "partials/review_card.html",
        {"job_id": job_id, "meta": meta, "query": row.query or "",
         "staging_exists": staging_exists, "genres": genres,
         "is_enrichment": is_enrichment,
         "album_consistency_warning": album_consistency_warning,
         "parsed_artists": parsed_artists,
         "show_multi_artists": show_multi_artists,
         "show_mb_search": False},
    )


async def _review_card_ctx(
    request: Request,
    session: AsyncSession,
    job_id: str,
    row: AcquisitionJobRow,
    meta: dict,
    *,
    show_mb_search: bool = False,
) -> dict:
    """Build complete template context for review_card.html."""
    staging_exists = bool(row.staging_path and Path(row.staging_path).exists())
    is_enrichment = bool(meta.get("is_enrichment"))

    from sqlalchemy import distinct as _distinct
    genre_rows = (await session.execute(
        select(_distinct(Track.genre)).where(Track.genre.isnot(None)).order_by(Track.genre)
    )).scalars().all()
    genres = [g for g in genre_rows if g]

    # Artist name autocomplete
    artist_name_rows = (await session.execute(
        select(_distinct(Artist.name)).order_by(Artist.name)
    )).scalars().all()
    artist_names = [n for n in artist_name_rows if n]

    # Album name autocomplete — scoped to the current artist where possible
    current_aa = (meta.get("albumartist") or meta.get("artist") or "").strip()
    if current_aa:
        scoped = (await session.execute(
            select(_distinct(Album.title))
            .join(Artist, Album.artist_id == Artist.id)
            .where(Artist.name == current_aa)
            .order_by(Album.title)
        )).scalars().all()
        album_names = [t for t in scoped if t] if scoped else []
    else:
        album_names = []
    if not album_names:
        all_albums = (await session.execute(
            select(_distinct(Album.title)).order_by(Album.title)
        )).scalars().all()
        album_names = [t for t in all_albums if t]

    # Expected track number from the original candidate (for album batch reference)
    candidate_track_number: int | None = None
    if row.album_job_id and row.candidate_json:
        try:
            from service.core.models import TrackCandidate as _TC
            _cand = _TC.model_validate_json(row.candidate_json)
            candidate_track_number = _cand.track_number
        except Exception:
            pass

    album_consistency_warning: str | None = None
    if row.album_job_id and not is_enrichment:
        siblings = (await session.execute(
            select(AcquisitionJobRow.resolved_metadata_json)
            .where(
                AcquisitionJobRow.album_job_id == row.album_job_id,
                AcquisitionJobRow.id != job_id,
                AcquisitionJobRow.state == "needs_review",
                AcquisitionJobRow.resolved_metadata_json.isnot(None),
            )
        )).scalars().all()
        this_aa = (meta.get("albumartist") or "").lower().strip()
        if this_aa and siblings:
            sibling_aas = set()
            for sib_json in siblings:
                try:
                    sib_meta = json.loads(sib_json)
                    sib_aa = (sib_meta.get("albumartist") or "").lower().strip()
                    if sib_aa:
                        sibling_aas.add(sib_aa)
                except Exception:
                    pass
            other_aas = sibling_aas - {this_aa}
            if other_aas:
                album_consistency_warning = (
                    f"Album artist mismatch: this track has \"{meta.get('albumartist')}\""
                    f" but {len(other_aas)} other track(s) in this batch differ."
                )

    from service.library.tagger import parse_artists as _parse_artists
    parsed_artists = _parse_artists(meta.get("artist") or "")
    show_multi_artists = len(parsed_artists) > 1

    album_batch_label: str | None = None
    if row.album_job_id:
        from service.db.schema import AlbumAcquisitionJob as _AlbumJob
        album_job = await session.get(_AlbumJob, row.album_job_id)
        if album_job:
            parts = [p for p in [album_job.album_artist, album_job.album_title] if p]
            album_batch_label = " — ".join(parts) if parts else row.album_job_id[:8]

    force_reason = meta.get("force_staging_reason") or ""
    show_src_panel = not is_enrichment and (
        (staging_exists and "title mismatch" in force_reason.lower())
        or (not staging_exists and "no confident" in force_reason.lower())
    )

    return {
        "job_id": job_id,
        "meta": meta,
        "query": row.query or "",
        "staging_exists": staging_exists,
        "genres": genres,
        "is_enrichment": is_enrichment,
        "album_consistency_warning": album_consistency_warning,
        "album_batch_label": album_batch_label,
        "parsed_artists": parsed_artists,
        "show_multi_artists": show_multi_artists,
        "show_mb_search": show_mb_search,
        "show_src_panel": show_src_panel,
        "artist_names": artist_names,
        "album_names": album_names,
        "candidate_track_number": candidate_track_number,
    }


@router.post("/jobs/batch-approve", response_class=HTMLResponse)
async def batch_approve(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Approve multiple needs_review jobs at once using their stored metadata."""
    from service.acquisition.pipeline import place_approved_track

    form = await request.form()
    job_ids: list[str] = list(form.getlist("job_id"))  # type: ignore[arg-type]

    done_count = 0
    fail_count = 0
    for jid in job_ids:
        try:
            dest = await place_approved_track(jid, {}, session)
            await session.commit()
            if dest is not None and dest.exists():
                try:
                    from service.library.tagger import compute_replaygain, write_replaygain
                    rg = await asyncio.to_thread(compute_replaygain, dest)
                    if rg is not None:
                        await asyncio.to_thread(write_replaygain, dest, rg)
                except Exception:
                    pass
            done_count += 1
        except Exception as exc:
            logger.error("Batch approve failed for %s: %s", jid, exc)
            # Must rollback before reusing the session
            try:
                await session.rollback()
                row = await session.get(AcquisitionJobRow, jid)
                if row:
                    row.error = str(exc)[:200]
                    await session.commit()
            except Exception:
                pass
            fail_count += 1

    return templates.TemplateResponse(
        request, "partials/job_list.html", await _job_list_ctx(session)
    )


@router.post("/jobs/{job_id}/approve", response_class=HTMLResponse)
async def approve_job(
    request: Request,
    job_id: str,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    year: str = Form(""),
    track_number: str = Form(""),
    mb_recording_id: str = Form(""),
    genre: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.acquisition.pipeline import place_approved_track

    overrides: dict[str, str | None] = {
        "title": title or None,
        "artist": artist or None,
        "album": album or None,
        "year": year or None,
        "track_number": track_number or None,
        "mb_recording_id": mb_recording_id or None,
        "genre": genre or None,
    }

    # Guard against concurrent approvals of the same job (race condition between
    # the state check and the file move).  A short-lived Redis lock is sufficient.
    lock_key = f"approve_lock:{job_id}"
    _redis = None
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        _redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        acquired = await _redis.set(lock_key, "1", ex=60, nx=True)
        if not acquired:
            return HTMLResponse(
                f'<div class="card card-review" id="job-{job_id}">'
                f'<div class="rv-form"><div class="rv-alert rv-alert--error">'
                f'Approval already in progress for this job.</div></div></div>'
            )
    except Exception:
        acquired = True  # if Redis is unavailable, proceed without the lock

    dest: Path | None = None
    try:
        dest = await place_approved_track(job_id, overrides, session)
        await session.commit()
    except Exception as exc:
        logger.error("Approve job %s failed: %s", job_id, exc)
        # Rollback any partial transaction before using the session again
        try:
            await session.rollback()
            row = await session.get(AcquisitionJobRow, job_id)
            if row:
                row.error = str(exc)[:200]
                await session.commit()
        except Exception:
            pass
        try:
            row = await session.get(AcquisitionJobRow, job_id)
            meta = json.loads(row.resolved_metadata_json) if row and row.resolved_metadata_json else {}
            ctx = await _review_card_ctx(request, session, job_id, row, meta)
            ctx["error"] = str(exc)
            return templates.TemplateResponse(request, "partials/review_card.html", ctx)
        except Exception:
            return HTMLResponse(
                f'<div class="card card-review" id="job-{job_id}">'
                f'<div class="rv-form"><div class="rv-alert rv-alert--error">Approve failed: {exc}</div></div></div>'
            )
    finally:
        if _redis is not None:
            try:
                await _redis.delete(lock_key)
                await _redis.aclose()
            except Exception:
                pass

    # ReplayGain after commit — subprocess inside a session causes greenlet conflict
    if dest is not None and dest.exists():
        try:
            from service.library.tagger import compute_replaygain, write_replaygain
            rg_gain = await asyncio.to_thread(compute_replaygain, dest)
            if rg_gain is not None:
                await asyncio.to_thread(write_replaygain, dest, rg_gain)
                logger.debug("ReplayGain: %s gain=%+.2f dB", dest.name, rg_gain)
        except Exception as rg_exc:
            logger.debug("ReplayGain failed for %s: %s", dest, rg_exc)

    row = await session.get(AcquisitionJobRow, job_id)
    return templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )


@router.post("/jobs/{job_id}/reject", response_class=HTMLResponse)
async def reject_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)

    # Enrichment suggestions point to the real /music file — never trash it
    is_enrichment = False
    if row.resolved_metadata_json:
        try:
            is_enrichment = bool(json.loads(row.resolved_metadata_json).get("is_enrichment"))
        except Exception:
            pass

    if row.staging_path and not is_enrichment:
        try:
            p = Path(row.staging_path)
            if p.exists():
                safe_trash(p, settings.staging_dir / ".trash")
            parent = p.parent
            for _ in range(3):
                if parent == settings.staging_dir or not parent.exists():
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        except Exception as exc:
            logger.debug("Reject cleanup failed: %s", exc)

    row.state = "failed"
    row.failure_class = "permanent"
    row.error = "Rejected by user"
    row.staging_path = None
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    return HTMLResponse("")


@router.post("/jobs/bulk-action", response_class=HTMLResponse)
async def jobs_bulk_action(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Reject or dismiss multiple selected jobs at once."""
    form = await request.form()
    job_ids: list[str] = list(form.getlist("job_id"))  # type: ignore[arg-type]
    action: str = str(form.get("action", "reject"))

    for jid in job_ids:
        try:
            row = await session.get(AcquisitionJobRow, jid)
            if row is None:
                continue
            if action == "reject" and row.state == "needs_review":
                is_enrichment = False
                if row.resolved_metadata_json:
                    try:
                        is_enrichment = bool(json.loads(row.resolved_metadata_json).get("is_enrichment"))
                    except Exception:
                        pass
                if row.staging_path and not is_enrichment:
                    try:
                        p = Path(row.staging_path)
                        if p.exists():
                            safe_trash(p, settings.staging_dir / ".trash")
                    except Exception:
                        pass
                row.state = "failed"
                row.failure_class = "permanent"
                row.error = "Rejected (bulk)"
                row.staging_path = None
            elif action == "cancel" and row.state in ("queued", "downloading", "processing", "tagging", "importing"):
                try:
                    from arq import create_pool
                    from arq.connections import RedisSettings
                    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
                    await redis.zrem("arq:queue", f"acquire:{jid}")
                    await redis.aclose()
                except Exception:
                    pass
                row.state = "cancelled"
                row.error = "Cancelled (bulk)"
            elif action == "dismiss" and row.state in _COMPLETED_STATES:
                await session.delete(row)
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await session.flush()
        except Exception as exc:
            logger.error("Bulk action %s failed for %s: %s", action, jid, exc)
            try:
                await session.rollback()
            except Exception:
                pass

    await session.commit()
    return templates.TemplateResponse(
        request, "partials/job_list.html", await _job_list_ctx(session)
    )


@router.post("/jobs/{job_id}/requeue", response_class=HTMLResponse)
async def requeue_job(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Re-download a job whose staging file is missing. Resets to queued and enqueues."""
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)
    if not row.candidate_json:
        raise HTTPException(400, "No candidate data to re-queue")

    # Carry forward any MB recording ID the user set during review so the
    # pipeline can lock onto it rather than running a fresh text search.
    candidate_json = row.candidate_json
    if row.resolved_metadata_json:
        try:
            from service.core.models import TrackCandidate as _TC
            resolved = json.loads(row.resolved_metadata_json)
            mb_id = resolved.get("mb_recording_id")
            if mb_id:
                cand = _TC.model_validate_json(candidate_json)
                cand = cand.model_copy(update={"musicbrainz_recording_id": mb_id})
                candidate_json = cand.model_dump_json()
        except Exception:
            pass

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=row.provider,
            provider_ref=row.provider_ref,
            candidate_json=candidate_json,
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
        row.state = "queued"
        row.staging_path = None
        row.resolved_metadata_json = None
        row.error = None
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc

    return templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )


@router.get("/jobs/{job_id}/mb-search", response_class=HTMLResponse)
async def job_mb_search(
    request: Request,
    job_id: str,
    q: str = "",
    limit: int = 10,
    duration: int | None = None,
) -> HTMLResponse:
    if not q.strip():
        return HTMLResponse("")
    from service.metadata.musicbrainz import search_recordings_free
    results = await asyncio.to_thread(
        search_recordings_free, q.strip(), limit, settings.cache_dir, duration
    )
    return templates.TemplateResponse(
        request, "partials/mb_candidates.html",
        {"results": results, "job_id": job_id, "q": q.strip(), "limit": limit, "duration": duration},
    )


@router.post("/jobs/{job_id}/mb-apply", response_class=HTMLResponse)
async def job_mb_apply(
    request: Request,
    job_id: str,
    recording_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Fetch a specific MB recording and update this job's resolved metadata."""
    if not recording_id.strip():
        raise HTTPException(400, "recording_id required")

    row = await session.get(AcquisitionJobRow, job_id)
    if row is None or not row.resolved_metadata_json:
        raise HTTPException(404)

    from service.metadata.musicbrainz import get_recording_by_id
    mb = await asyncio.to_thread(
        get_recording_by_id, recording_id.strip(), settings.cache_dir
    )
    if mb is None:
        raise HTTPException(502, "Could not fetch recording from MusicBrainz")

    meta = json.loads(row.resolved_metadata_json)
    meta["mb_recording_id"] = mb.recording_id
    meta["mb_release_id"] = mb.release_id
    meta["mb_artist_id"] = mb.artist_id
    meta["mb_artist_sort"] = mb.artist_sort
    meta["mb_match_source"] = "manual"
    meta["title"] = mb.title or meta.get("title")
    meta["artist"] = mb.artist or meta.get("artist")
    meta["albumartist"] = mb.artist or meta.get("albumartist")
    if mb.album:
        meta["album"] = mb.album
    if mb.year:
        meta["year"] = mb.year
    if mb.track_number:
        meta["track_number"] = mb.track_number

    row.resolved_metadata_json = json.dumps(meta)
    row.error = None
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()

    ctx = await _review_card_ctx(request, session, job_id, row, meta, show_mb_search=True)
    return templates.TemplateResponse(request, "partials/review_card.html", ctx)


_EXPLICIT_RE = re.compile(r"\b(explicit|explicit version)\b", re.IGNORECASE)
_CLEAN_RE = re.compile(r"\b(clean|clean version|radio edit|censored|edited)\b", re.IGNORECASE)


def _explicit_score(title: str) -> int:
    if _EXPLICIT_RE.search(title):
        return 1
    if _CLEAN_RE.search(title):
        return -1
    return 0


@router.delete("/jobs/dismiss/{job_id}", response_class=HTMLResponse)
async def dismiss_job(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is not None and row.state in _COMPLETED_STATES:
        await session.delete(row)
        await session.commit()
    return HTMLResponse("")


@router.get("/jobs/{job_id}/suggest-track-number", response_class=HTMLResponse)
async def suggest_track_number(
    request: Request,
    job_id: str,
    album: str = "",
    title: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Find the best-matching track in an existing album and return the track number as an HTML snippet."""
    from service.search.matcher import title_similarity

    if not album or not title:
        return HTMLResponse("")

    tracks = (await session.execute(
        select(Track.title, Track.track_number)
        .join(Track.album)
        .where(Album.title.ilike(f"%{album.strip()}%"), Track.track_number.isnot(None))
    )).all()

    if not tracks:
        return HTMLResponse("")

    best = max(tracks, key=lambda r: title_similarity(r.title, title))
    if title_similarity(best.title, title) < 0.6:
        return HTMLResponse("")

    return HTMLResponse(
        f'<span style="font-size:11px;color:var(--t3);margin-left:4px">'
        f'→ track {best.track_number} in library</span>'
    )


@router.post("/jobs/retry-all-failed", response_class=HTMLResponse)
async def retry_all_failed(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(AcquisitionJobRow).where(
                AcquisitionJobRow.state == "failed",
                AcquisitionJobRow.candidate_json.isnot(None),
            )
        )
    ).scalars().all()

    if rows:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            for row in rows:
                retry_suffix = uuid.uuid4().hex[:8]
                await redis.enqueue_job(
                    "acquire_track",
                    job_id=row.id,
                    provider_name=row.provider,
                    provider_ref=row.provider_ref,
                    candidate_json=row.candidate_json,
                    music_dir=str(settings.music_dir),
                    tmp_acquire_dir=str(settings.tmp_acquire_dir),
                    _job_id=f"acquire:{row.id}:{retry_suffix}",
                )
                row.state = "queued"
                row.failure_class = None
                row.error = None
            await redis.aclose()
            await session.commit()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc

    return templates.TemplateResponse(request, "partials/job_list.html", await _job_list_ctx(session))


@router.delete("/jobs/clear", response_class=HTMLResponse)
async def clear_done_jobs(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    await session.execute(
        sa_delete(AcquisitionJobRow).where(
            AcquisitionJobRow.state.in_(_COMPLETED_STATES)
        )
    )
    await session.commit()
    rows = (
        await session.execute(
            select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(50)
        )
    ).scalars().all()
    return templates.TemplateResponse(request, "partials/job_list.html", _grouped_jobs(rows))


@router.delete("/library/tracks/{internal_id}", response_class=HTMLResponse)
async def delete_track(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _joinedload
    stmt = (
        select(Track)
        .options(_joinedload(Track.file), _joinedload(Track.artist))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        return HTMLResponse("")

    if row.file:
        file_path = Path(row.file.path)
        album_dir = file_path.parent
        if file_path.exists():
            try:
                safe_trash(file_path, settings.music_dir / ".trash")
            except Exception as exc:
                logger.warning("Trash move failed for %s: %s", file_path, exc)
        _trash_empty_album_dir(album_dir, settings.music_dir / ".trash")
        await session.delete(row.file)

    from datetime import UTC as _UTC, datetime as _dt
    tombstone = DeletedTrack(
        mb_recording_id=row.musicbrainz_recording_id,
        track_title=row.title,
        track_artist=row.artist.name if row.artist else None,
        deleted_at=_dt.now(_UTC).replace(tzinfo=None),
    )
    album_id_was = row.album_id
    artist_id_was = row.artist_id
    session.add(tombstone)
    await session.delete(row)
    await session.flush()

    # Inline orphan cleanup so the browse UI reflects changes immediately
    if album_id_was:
        remaining = (await session.execute(
            select(func.count(Track.id)).where(Track.album_id == album_id_was)
        )).scalar_one()
        if remaining == 0:
            await session.execute(sa_delete(Album).where(Album.id == album_id_was))
    remaining_artist = (await session.execute(
        select(func.count(Track.id)).where(Track.artist_id == artist_id_was)
    )).scalar_one()
    if remaining_artist == 0:
        await session.execute(sa_delete(Artist).where(Artist.id == artist_id_was))

    await session.commit()
    await _do_scans()
    return HTMLResponse("")


@router.get("/search/cloud", response_class=HTMLResponse)
async def cloud_search_page(
    request: Request,
    q: str = "",
    offset: int = 0,
) -> HTMLResponse:
    candidates: list[dict[str, object]] = []
    PAGE = 5
    if q:
        try:
            import service.providers.ytdlp  # noqa: F401
            from service.core.models import SearchQuery
            from service.providers import get

            provider = get("ytdlp")()
            # Fetch enough for this page + ranking headroom
            fetch_limit = offset + PAGE * 2
            raw: list[dict[str, object]] = []
            async for c in provider.search(SearchQuery(q=q, limit=fetch_limit)):
                raw.append({
                    "title": c.title,
                    "artist": c.artist,
                    "duration_seconds": c.duration_seconds,
                    "provider_ref": c.provider_ref,
                    "thumbnail_url": c.thumbnail_url,
                    "candidate_json": c.model_dump_json(),
                    "_score": _explicit_score(c.title),
                })

            # Sort: explicit first (when prefer_explicit is on), clean last; stable
            if settings.prefer_explicit:
                raw.sort(key=lambda x: -int(x["_score"]))  # type: ignore[arg-type]
            for item in raw:
                del item["_score"]

            candidates = raw[offset: offset + PAGE]
        except Exception as exc:
            logger.warning("Cloud search failed: %s", exc)

    return templates.TemplateResponse(
        request, "partials/cloud_results.html",
        {"candidates": candidates, "q": q, "offset": offset, "limit": PAGE},
    )


@router.post("/search/acquire-url", response_class=HTMLResponse)
async def acquire_from_url(
    request: Request,
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue an acquisition from a manually entered URL."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    url = url.strip()
    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref=url,
        title=url,
        artist="Unknown",
    )
    job_id = await create_job(
        session,
        provider_name="ytdlp",
        provider_ref=url,
        candidate=candidate,
        query=url,
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
            provider_ref=url,
            candidate_json=candidate.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    return HTMLResponse(
        '<div id="cloud-url-row" style="padding:8px 0">'
        f'<span class="badge badge-done">Queued → <a href="/jobs">Jobs ↗</a></span>'
        '</div>'
    )


async def _library_stats_context(session: AsyncSession) -> dict:
    """Compute stats and quality counts for the library overview."""
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    track_count = (await session.execute(
        select(func.count(Track.id)).join(Track.artist).join(Track.file)
    )).scalar_one()
    album_count = (await session.execute(select(func.count(Album.id)))).scalar_one()
    artist_count = (await session.execute(select(func.count(Artist.id)))).scalar_one()
    genre_count = (await session.execute(
        select(func.count(func.distinct(Track.genre))).where(Track.genre.isnot(None))
    )).scalar_one()
    no_mbid_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.musicbrainz_recording_id.is_(None))
    )).scalar_one()
    no_art_count = (await session.execute(
        select(func.count(Track.id)).join(Track.artist).join(Track.file).where(
            (TrackFile.has_cover_art.is_(None)) | (TrackFile.has_cover_art == 0)
        )
    )).scalar_one()
    _not_suppressed = (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0)
    low_quality_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(
            (Track.tag_quality_score.isnot(None))
            & (Track.tag_quality_score < LOW_QUALITY_THRESHOLD)
            & _not_suppressed
        )
    )).scalar_one()
    _bitrate_not_suppressed = (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0)
    low_bitrate_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).join(Track.artist).where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < settings.min_bitrate_kbps,
            _bitrate_not_suppressed,
        )
    )).scalar_one()
    dupe_count = (await session.execute(
        select(func.count()).select_from(
            select(Track.musicbrainz_recording_id)
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.is_not(None))
            .group_by(Track.musicbrainz_recording_id)
            .having(func.count(Track.id) > 1)
            .subquery()
        )
    )).scalar_one()
    return {
        "stats": {"tracks": track_count, "albums": album_count, "artists": artist_count, "genres": genre_count},
        "quality": {
            "no_mbid": no_mbid_count, "no_art": no_art_count,
            "low_quality": low_quality_count, "low_bitrate": low_bitrate_count, "dupes": dupe_count,
        },
        "min_bitrate_kbps": settings.min_bitrate_kbps,
    }


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    stats_ctx = await _library_stats_context(session)
    _not_suppressed = (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0)

    recent_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .order_by(TrackFile.created_at.desc())
            .limit(20)
        )
    ).unique().scalars().all()

    needs_review_count = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state == "needs_review")
        )
    ).scalar_one()

    artist_names = (await session.execute(select(Artist.name).order_by(Artist.name))).scalars().all()
    album_names = (await session.execute(select(Album.title).order_by(Album.title))).scalars().all()

    return templates.TemplateResponse(
        request, "library.html",
        {
            "active": "library",
            **stats_ctx,
            "recent": [_track_to_ref(r) for r in recent_rows],
            "settings_music_dir": str(settings.music_dir),
            "needs_review_count": needs_review_count,
            "artist_names": artist_names,
            "album_names": album_names,
        },
    )


@router.get("/jobs/{job_id}/stream")
async def stream_staged_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream a staged audio file for preview during review."""
    from fastapi.responses import FileResponse
    row = await session.get(AcquisitionJobRow, job_id)
    if not row or not row.staging_path:
        raise HTTPException(404)
    path = Path(row.staging_path)
    if not path.exists():
        raise HTTPException(404)
    ext = path.suffix.lower()
    media_map = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".flac": "audio/flac",
                 ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac"}
    return FileResponse(path, media_type=media_map.get(ext, "audio/ogg"))


@router.get("/jobs/{job_id}/search-source", response_class=HTMLResponse)
async def job_search_source(
    request: Request,
    job_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search YouTube for an alternative source to replace the staged file."""
    candidates: list[dict[str, object]] = []
    if q.strip():
        try:
            from service.core.models import SearchQuery
            from service.providers import get as get_provider
            import service.providers.ytdlp  # noqa
            provider = get_provider("ytdlp")()
            async for c in provider.search(SearchQuery(q=q.strip(), limit=8)):
                abr = c.raw_metadata.get("abr") or c.raw_metadata.get("tbr")
                acodec = c.raw_metadata.get("acodec") or c.raw_metadata.get("ext")
                candidates.append({
                    "title": c.title, "artist": c.artist,
                    "duration_seconds": c.duration_seconds,
                    "provider_ref": c.provider_ref,
                    "thumbnail_url": c.thumbnail_url,
                    "candidate_json": c.model_dump_json(),
                    "abr": int(abr) if abr else None,
                    "acodec": str(acodec) if acodec else None,
                })
        except Exception as exc:
            logger.warning("Source search failed: %s", exc)

    # Pass current source quality so the template can show what we have now
    source_codec: str | None = None
    source_bitrate_kbps: int | None = None
    row = await session.get(AcquisitionJobRow, job_id)
    if row and row.resolved_metadata_json:
        try:
            _meta = json.loads(row.resolved_metadata_json)
            source_codec = _meta.get("source_codec")
            source_bitrate_kbps = _meta.get("source_bitrate_kbps")
        except Exception:
            pass

    return templates.TemplateResponse(
        request, "partials/source_replace_results.html",
        {"candidates": candidates, "q": q, "job_id": job_id,
         "source_codec": source_codec, "source_bitrate_kbps": source_bitrate_kbps},
    )


@router.post("/jobs/{job_id}/replace-source", response_class=HTMLResponse)
async def replace_job_source(
    request: Request,
    job_id: str,
    provider_ref: str = Form(...),
    candidate_json: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Trash staged file, update provider_ref, re-queue download."""
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)
    # Trash existing staged file
    if row.staging_path:
        try:
            p = Path(row.staging_path)
            if p.exists():
                safe_trash(p, settings.staging_dir / ".trash")
        except Exception:
            pass
    # Update candidate if provided
    if candidate_json:
        row.candidate_json = candidate_json
    row.provider_ref = provider_ref
    row.staging_path = None
    row.resolved_metadata_json = None
    row.state = "queued"
    row.error = None
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.flush()
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=row.provider,
            provider_ref=provider_ref,
            candidate_json=row.candidate_json or "{}",
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
        await session.commit()
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )


@router.get("/library/albums", response_class=HTMLResponse)
async def library_albums_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "library_albums.html",
        {"active": "library", "q": q},
    )


@router.get("/library/albums/merge-candidates", response_class=HTMLResponse)
async def library_albums_merge_candidates(
    request: Request,
    q: str = "",
    canonical: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return album rows as merge-into-canonical candidates with action buttons."""
    from sqlalchemy.orm import joinedload as _jl
    if not q.strip():
        return HTMLResponse('<p class="muted" style="font-size:12px">Type to search…</p>')
    pattern = f"%{q.strip()}%"
    stmt = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .where(Album.title.ilike(pattern) | Artist.name.ilike(pattern))
        .where(Album.id != canonical)
        .order_by(Artist.name, Album.year, Album.title)
        .limit(20)
    )
    albums = (await session.execute(stmt)).unique().scalars().all()
    if not albums:
        return HTMLResponse('<p class="muted" style="font-size:12px">No matching albums.</p>')
    lines = []
    for album in albums:
        ntracks = len(album.tracks)
        lines.append(
            f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--b1)">'
            f'<div style="flex:1;min-width:0">'
            f'<div style="font-size:13px;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{album.title}</div>'
            f'<div style="font-size:11px;color:var(--t3)">{album.artist.name}'
            + (f' · {album.year}' if album.year else '')
            + f' · {ntracks} track{"s" if ntracks != 1 else ""}</div>'
            f'</div>'
            f'<button class="btn btn-sm btn-ghost" style="white-space:nowrap"'
            f' hx-post="/library/albums/{canonical}/merge/{album.id}"'
            f' hx-target="#album-list"'
            f' hx-swap="innerHTML"'
            f' hx-confirm="Merge \'{album.title}\' into the current album? This moves all its tracks and cannot be undone.">'
            f'Merge in ←</button>'
            f'</div>'
        )
    return HTMLResponse('<div style="margin-top:4px">' + ''.join(lines) + '</div>')


@router.get("/library/albums/list", response_class=HTMLResponse)
async def library_albums_list(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _jl
    stmt = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .order_by(Artist.name, Album.year, Album.title)
        .limit(300)
    )
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Album.title.ilike(pattern) | Artist.name.ilike(pattern))
    albums = (await session.execute(stmt)).unique().scalars().all()
    # Compute per-album quality from owned tracks (no extra query needed — tracks already loaded)
    album_quality: dict[str, float | None] = {}
    for alb in albums:
        scores = [t.tag_quality_score for t in alb.tracks if t.tag_quality_score is not None]
        album_quality[alb.id] = round(sum(scores) / len(scores), 3) if scores else None
    singles_count = 0
    singles_cover_id: str | None = None
    if not q.strip():
        singles_count = (await session.execute(
            select(func.count(Track.id)).join(Track.file).where(Track.album_id.is_(None))
        )).scalar_one()
        if singles_count:
            cover_row = (await session.execute(
                select(Track.id)
                .join(Track.file)
                .where(Track.album_id.is_(None), TrackFile.has_cover_art == 1)
                .limit(1)
            )).scalar_one_or_none()
            singles_cover_id = cover_row
    return templates.TemplateResponse(
        request, "partials/album_list.html",
        {"albums": albums, "q": q, "album_quality": album_quality,
         "singles_count": singles_count, "singles_cover_id": singles_cover_id},
    )


@router.get("/library/albums/{album_id}/detail", response_class=HTMLResponse)
async def album_detail(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _jl
    album = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file).joinedload(TrackFile.track))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)
    # Find cover art path (sidecar or embedded)
    cover_track = next((t for t in album.tracks if t.file and Path(t.file.path).exists()), None)
    sorted_tracks = sorted(album.tracks, key=lambda t: (t.track_number is None, t.track_number or 0))
    return templates.TemplateResponse(
        request, "partials/album_detail.html",
        {"album": album, "sorted_tracks": sorted_tracks, "cover_track": cover_track},
    )


@router.post("/library/albums/{album_id}/update-meta", response_class=HTMLResponse)
async def album_update_meta(
    request: Request,
    album_id: str,
    title: str = Form(""),
    year: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import write_tags as _write_tags

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    title_val = title.strip() or album.title
    year_val: int | None = int(year) if year.strip().isdigit() else album.year

    # Update DB
    album.title = title_val
    album.year = year_val
    album.updated_at = datetime.now(UTC).replace(tzinfo=None)

    # Write album/albumartist/year to all track files so Navidrome groups them
    # into a single album entry.
    albumartist_val = album.artist.name if album.artist else "Unknown"

    # Determine canonical MUSICBRAINZ_ALBUMID: prefer DB value, else majority from files.
    # Inconsistent MB album IDs cause Navidrome to split the album.
    canonical_mb_release_id: str | None = album.musicbrainz_release_id
    if not canonical_mb_release_id:
        from collections import Counter
        from service.library.tagger import read_tags as _read_tags
        mb_ids: list[str] = []
        for track in album.tracks:
            if track.file:
                fp = Path(track.file.path)
                if fp.exists():
                    try:
                        tagged = await asyncio.to_thread(_read_tags, fp)
                        # read_tags doesn't return mb_release_id directly; read raw tag
                        import mutagen
                        raw = mutagen.File(fp)
                        if raw and raw.tags:
                            _tags = raw.tags
                            for key in ("musicbrainz_albumid", "TXXX:MusicBrainz Album Id",
                                        "----:com.apple.iTunes:MusicBrainz Album Id"):
                                v = _tags.get(key)
                                if v:
                                    val_str = v[0] if isinstance(v, list) else str(v)
                                    if hasattr(val_str, 'text'):
                                        val_str = str(val_str.text[0]) if val_str.text else ""
                                    if val_str:
                                        mb_ids.append(str(val_str).strip())
                                    break
                    except Exception:
                        pass
        if mb_ids:
            canonical_mb_release_id = Counter(mb_ids).most_common(1)[0][0]

    for track in album.tracks:
        if track.file:
            fp = Path(track.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(
                        _write_tags, fp,
                        album=title_val,
                        albumartist=albumartist_val,
                        year=year_val,
                        mb_release_id=canonical_mb_release_id,
                    )
                except Exception as exc:
                    logger.warning("album update-meta tag write failed: %s", exc)

    await session.commit()
    await _do_scans()

    album_reloaded = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    cover_track = next((t for t in album_reloaded.tracks if t.file and Path(t.file.path).exists()), None)
    return templates.TemplateResponse(
        request, "partials/album_detail.html",
        {"album": album_reloaded, "cover_track": cover_track, "saved": True},
    )


@router.post("/library/albums/{album_id}/set-genre", response_class=HTMLResponse)
async def album_set_genre(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Set (or clear) genre on all tracks in an album — DB + file tags."""
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    genre_val = (form.get("genre") or "").strip() or None

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    updated = 0
    for track in album.tracks:
        track.genre = genre_val
        if track.file:
            fp = Path(track.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(_write_tags, fp, genre=genre_val or "")
                    updated += 1
                except Exception as exc:
                    logger.warning("album set-genre tag write failed for %s: %s", fp, exc)
    await session.commit()
    await _do_scans()

    label = f'"{genre_val}"' if genre_val else "removed"
    return HTMLResponse(
        f'<span class="badge badge-done">Genre {label} set on {updated} track(s) ✓</span>'
    )


@router.get("/library/albums/{album_id}/mb-compare", response_class=HTMLResponse)
async def album_mb_compare(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Compare local tracks in this album against the MB release tracklist."""
    from sqlalchemy.orm import joinedload as _jl
    from service.search.matcher import title_similarity as _tsim

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file), _jl(Album.artist))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None or (not album.mb_release_group_id and not album.musicbrainz_release_id):
        return HTMLResponse('<p class="muted" style="font-size:12px">No MusicBrainz release linked.</p>')

    try:
        if album.mb_release_group_id:
            from service.metadata.musicbrainz import get_release_group_tracks as _get_rg_tracks
            _, _, _, mb_tracks = await asyncio.to_thread(
                _get_rg_tracks, album.mb_release_group_id, settings.cache_dir
            )
        else:
            from service.metadata.musicbrainz import get_release_tracks_by_id as _get_rel_tracks
            _, _, _, mb_tracks = await asyncio.to_thread(
                _get_rel_tracks, album.musicbrainz_release_id, settings.cache_dir
            )
    except Exception as exc:
        return HTMLResponse(f'<p class="muted" style="font-size:12px">MB fetch failed: {exc}</p>')

    # Build lookup structures
    local_tracks = list(album.tracks)
    local_track_titles = [t.title for t in local_tracks]
    # Recording IDs for tracks IN THIS ALBUM
    local_rids = {t.musicbrainz_recording_id for t in local_tracks if t.musicbrainz_recording_id}

    # Find recording IDs owned ANYWHERE in the library (different album = "elsewhere")
    from service.library.cohesion import get_owned_recording_ids as _owned_rids
    mb_recording_ids = [t.recording_id for t in mb_tracks if t.recording_id]
    all_owned_rids = await _owned_rids(session, mb_recording_ids) if mb_recording_ids else set()
    elsewhere_rids = all_owned_rids - local_rids  # owned but not in this album

    # --- Section 1: MB tracklist ---
    lines = []
    owned_count = 0
    for mt in mb_tracks:
        if mt.recording_id and mt.recording_id in local_rids:
            status = "here"
        elif mt.recording_id and mt.recording_id in elsewhere_rids:
            status = "elsewhere"
        elif any(_tsim(mt.title, lt) >= 0.80 for lt in local_track_titles):
            status = "here"  # title match counts as owned here
        else:
            status = "missing"

        if status in ("here", "elsewhere"):
            owned_count += 1

        if status == "here":
            icon, color, label = "✓", "var(--success)", ""
        elif status == "elsewhere":
            icon, color, label = "⚠", "#e8a000", " <span style='font-size:10px;color:#e8a000'>(owned elsewhere)</span>"
        else:
            icon, color, label = "✕", "var(--danger)", ""

        acquire_btn = ""
        if status == "missing" and mt.recording_id:
            acquire_btn = (
                f'<form style="display:inline"'
                f' hx-post="/discography/{album.artist.musicbrainz_artist_id if album.artist and album.artist.musicbrainz_artist_id else "unknown"}/{album.mb_release_group_id or album.musicbrainz_release_id or album.id}/acquire-track"'
                f' hx-target="closest div" hx-swap="outerHTML">'
                f'<input type="hidden" name="recording_id" value="{mt.recording_id}">'
                f'<input type="hidden" name="title" value="{mt.title}">'
                f'<input type="hidden" name="artist" value="{album.artist.name if album.artist else ""}">'
                f'<input type="hidden" name="album" value="{album.title}">'
                f'<input type="hidden" name="track_number" value="{mt.number}">'
                + (f'<input type="hidden" name="duration_seconds" value="{mt.duration_seconds}">' if mt.duration_seconds else '')
                + f'<button type="submit" class="btn btn-sm" style="font-size:10px;padding:2px 6px">Get</button>'
                f'</form>'
            )

        move_btn = ""
        if status == "elsewhere" and mt.recording_id:
            # Find the track DB row that owns this recording
            et_row = (await session.execute(
                select(Track).where(Track.musicbrainz_recording_id == mt.recording_id).limit(1)
            )).scalar_one_or_none()
            if et_row:
                move_btn = (
                    f' <button class="btn btn-sm btn-ghost" style="font-size:10px;padding:2px 6px"'
                    f' hx-post="/library/tracks/{et_row.id}/move-to-album/{album_id}"'
                    f' hx-target="closest div" hx-swap="innerHTML">Move here</button>'
                )

        lines.append(
            f'<div style="display:flex;gap:8px;align-items:center;padding:3px 0;font-size:12px">'
            f'<span style="color:{color};font-weight:700;width:14px;text-align:center;flex-shrink:0">{icon}</span>'
            f'<span style="color:var(--t3);min-width:20px;flex-shrink:0">{mt.number}.</span>'
            f'<span style="color:{"var(--t2)" if status == "here" else "var(--t1)"};flex:1">{mt.title}{label}</span>'
            + acquire_btn + move_btn
            + '</div>'
        )

    if not lines:
        return HTMLResponse('<p class="muted" style="font-size:12px">No tracks found in MB tracklist.</p>')

    total = len(mb_tracks)
    missing = total - owned_count
    if missing == 0:
        summary = f'<div style="font-size:12px;color:var(--success);font-weight:600;margin-bottom:8px">✓ All {total} tracks owned</div>'
    else:
        summary = f'<div style="font-size:12px;color:var(--danger);font-weight:600;margin-bottom:8px">Missing {missing} of {total} tracks</div>'

    # --- Section 2: Local tracks in this album ---
    mb_rids_set = {t.recording_id for t in mb_tracks if t.recording_id}
    mb_titles = [t.title for t in mb_tracks]
    local_section_lines = []
    for lt in sorted(local_tracks, key=lambda t: t.track_number or 999):
        # Does this local track match any MB track?
        in_mb = (lt.musicbrainz_recording_id and lt.musicbrainz_recording_id in mb_rids_set) or any(
            _tsim(lt.title, mt) >= 0.80 for mt in mb_titles
        )
        icon = "✓" if in_mb else "❓"
        icon_color = "var(--t3)" if in_mb else "#e8a000"
        note = "" if in_mb else " <span style='font-size:10px;color:#e8a000'>not in MB list</span>"
        local_section_lines.append(
            f'<div style="display:flex;gap:8px;align-items:center;padding:2px 0;font-size:12px">'
            f'<span style="color:{icon_color};width:14px;text-align:center;flex-shrink:0">{icon}</span>'
            f'<span style="color:var(--t3);min-width:20px;flex-shrink:0">{lt.track_number or "—"}.</span>'
            f'<span style="color:var(--t2);flex:1">{lt.title}{note}</span>'
            f'</div>'
        )

    local_section = ""
    if local_section_lines:
        local_section = (
            '<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--b1)">'
            '<div style="font-size:11px;font-weight:600;color:var(--t3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Tracks in your library</div>'
            + "".join(local_section_lines)
            + '</div>'
        )

    return HTMLResponse(
        '<div style="border:1px solid var(--b1);border-radius:var(--radius-s);padding:10px">'
        + summary + "".join(lines) + local_section + '</div>'
    )


@router.get("/library/albums/{album_id}/mb-link-search", response_class=HTMLResponse)
async def album_mb_link_search(
    request: Request,
    album_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search MusicBrainz for release groups to link to this album."""
    from sqlalchemy.orm import joinedload as _jl
    album = (await session.execute(
        select(Album).options(_jl(Album.artist)).where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    if not q:
        q = f"{album.artist.name} {album.title}" if album.artist else album.title

    from service.metadata.musicbrainz import search_release_groups as _search_rgs
    results = await asyncio.to_thread(
        _search_rgs,
        album.artist.name if album.artist else "",
        album.title,
        8,
        settings.cache_dir,
    )

    if not results:
        return HTMLResponse('<p class="muted" style="font-size:12px">No results found.</p>')

    lines = ['<div style="display:flex;flex-direction:column;gap:6px">']
    for rg in results:
        label = rg["title"]
        if rg["year"]:
            label += f' ({rg["year"]})'
        if rg["disambiguation"]:
            label += f' [{rg["disambiguation"]}]'
        rtype = rg["type"] or "Album"
        lines.append(
            f'<div style="display:flex;align-items:center;gap:8px;font-size:12px">'
            f'<span style="color:var(--t1);flex:1">{label}</span>'
            f'<span style="color:var(--t3);font-size:11px">{rtype}</span>'
            f'<form style="display:inline"'
            f'      hx-post="/library/albums/{album_id}/link-mb-rg"'
            f'      hx-target="#album-{album_id.replace(":", "_")}"'
            f'      hx-swap="outerHTML">'
            f'  <input type="hidden" name="release_group_id" value="{rg["id"]}">'
            f'  <button type="submit" class="btn btn-sm btn-ghost" style="font-size:11px">Link</button>'
            f'</form>'
            f'</div>'
        )
    lines.append('</div>')
    return HTMLResponse("".join(lines))


@router.post("/library/albums/{album_id}/link-mb-rg", response_class=HTMLResponse)
async def album_link_mb_rg(
    request: Request,
    album_id: str,
    release_group_id: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save a MusicBrainz release group ID to this album and return the refreshed detail card."""
    from sqlalchemy.orm import joinedload as _jl2
    album = (await session.execute(
        select(Album).where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)
    album.mb_release_group_id = release_group_id
    await session.commit()

    # Return the full refreshed album detail card
    album = (await session.execute(
        select(Album)
        .options(_jl2(Album.artist), _jl2(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one()
    return templates.TemplateResponse(
        request, "partials/album_detail.html",
        {"album": album, "saved": True},
    )


@router.post("/library/tracks/{track_id}/move-to-album/{album_id}", response_class=HTMLResponse)
async def move_track_to_album(
    request: Request,
    track_id: str,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Move a misplaced track's file into this album's folder and fix its tags."""
    from service.library.tagger import write_tags as _wt
    from service.library.writer import atomic_place as _ap
    from service.library.layout import track_path as _tp

    track = (await session.execute(
        select(Track).options(joinedload(Track.file), joinedload(Track.album))
        .where(Track.id == track_id)
    )).unique().scalar_one_or_none()

    target_album = (await session.execute(
        select(Album).options(joinedload(Album.artist), joinedload(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()

    if track is None or target_album is None or track.file is None:
        raise HTTPException(404)

    src = Path(track.file.path)
    if not src.exists():
        return HTMLResponse('<span style="color:var(--danger);font-size:12px">File not found on disk</span>')

    artist_name = target_album.artist.name if target_album.artist else "Unknown"
    ext = src.suffix.lstrip(".")
    dst = _tp(
        settings.music_dir,
        artist=artist_name,
        album=target_album.title,
        year=target_album.year,
        track_number=track.track_number,
        disc_number=track.disc_number,
        title=track.title,
        ext=ext,
        albumartist=artist_name,
    )

    if dst == src:
        return HTMLResponse('<span style="color:var(--success);font-size:12px">Already in place</span>')

    if dst.exists():
        return HTMLResponse(f'<span style="color:var(--danger);font-size:12px">Collision: {dst.name} already exists in target</span>')

    # Fix tags on the file
    try:
        canonical_release_id: str | None = target_album.musicbrainz_release_id
        await asyncio.to_thread(
            _wt, src,
            album=target_album.title,
            year=target_album.year,
            albumartist=artist_name,
            track_number=track.track_number,
            mb_release_id=canonical_release_id,
        )
    except Exception as exc:
        logger.warning("move_track_to_album: tag write failed for %s: %s", src, exc)

    old_dir = src.parent
    await asyncio.to_thread(_ap, src, dst)

    # Update DB
    track.file.path = str(dst)
    track.album_id = album_id
    await session.commit()

    # Clean up old dir if empty
    _trash_empty_album_dir(old_dir, settings.music_dir / ".trash")

    await _do_scans()

    return HTMLResponse(f'<span style="color:var(--success);font-size:12px">✓ Moved to {target_album.title}</span>')


@router.get("/library/stats-fragment", response_class=HTMLResponse)
async def library_stats_fragment(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats tiles fragment for OOB update after rescan."""
    stats_ctx = await _library_stats_context(session)
    inner = templates.get_template("partials/library_stats.html").render(stats_ctx)
    return HTMLResponse(f'<div id="library-stats" hx-swap-oob="true">{inner}</div>')


@router.post("/library/rescan", response_class=HTMLResponse)
async def library_rescan(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Full rescan of /music: adds new files, removes missing ones from DB."""
    from service.index.scanner import scan

    try:
        result = await scan(session, settings.music_dir, incremental=False)
        await session.commit()
    except Exception as exc:
        logger.error("Library rescan failed: %s", exc)
        return HTMLResponse(f'<span class="badge badge-fail">Rescan failed: {exc}</span>')

    await _do_scans()

    # OOB-update the stats tiles so the user sees fresh counts without a page reload
    stats_ctx = await _library_stats_context(session)
    inner = templates.get_template("partials/library_stats.html").render(stats_ctx)
    badge = (
        f'<span class="badge badge-done">'
        f'Rescan done — {result.added} added, {result.removed} removed, {result.updated} updated'
        f'</span>'
    )
    oob = f'<div id="library-stats" hx-swap-oob="true">{inner}</div>'
    return HTMLResponse(badge + oob)


@router.get("/library/tracks/{internal_id}/cover-art")
async def track_cover_art(
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return cover art for a track: embedded first, then sidecar cover.jpg."""
    from fastapi.responses import Response as Resp
    from service.library.tagger import read_cover_art_bytes

    stmt = (
        select(Track)
        .options(joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)
    path = Path(row.file.path)
    if not path.exists():
        raise HTTPException(404)

    art = await asyncio.to_thread(read_cover_art_bytes, path)

    if not art:
        # Fall back to sidecar cover.jpg in the same directory
        cover_jpg = path.parent / "cover.jpg"
        if cover_jpg.exists():
            art = await asyncio.to_thread(cover_jpg.read_bytes)

    if not art:
        raise HTTPException(404)
    return Resp(content=art, media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"})


@router.get("/jobs/{job_id}/cover-art")
async def job_cover_art(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return embedded cover art from a staged review file."""
    from fastapi.responses import Response as Resp
    from service.library.tagger import read_cover_art_bytes

    row = await session.get(AcquisitionJobRow, job_id)
    if not row or not row.staging_path:
        raise HTTPException(404)
    path = Path(row.staging_path)
    if not path.exists():
        raise HTTPException(404)
    art = await asyncio.to_thread(read_cover_art_bytes, path)
    if not art:
        raise HTTPException(404)
    return Resp(content=art, media_type="image/jpeg",
                headers={"Cache-Control": "no-store"})


@router.post("/jobs/{job_id}/apply-art", response_class=HTMLResponse)
async def job_apply_art(
    request: Request,
    job_id: str,
    art_url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download art from a URL and embed it into the staging file for a review job."""
    from service.library.tagger import write_tags as _write_tags
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small, fetch_from_url

    row = await session.get(AcquisitionJobRow, job_id)
    if not row or not row.staging_path:
        raise HTTPException(404)
    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        raise HTTPException(404, "Staging file not on disk")

    art = await fetch_from_url(art_url)
    if not art:
        return HTMLResponse('<span class="badge badge-warn">Could not download image</span>')
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return HTMLResponse('<span class="badge badge-warn">Image too small (< 300×300)</span>')

    await asyncio.to_thread(_write_tags, staging_path, artwork_bytes=art)

    import time as _time
    cache_bust = int(_time.time())
    oob_img = (
        f'<img src="/jobs/{job_id}/cover-art?t={cache_bust}" '
        f'id="rv-cover-img-{job_id}" hx-swap-oob="true" '
        f'onerror="this.src=\'\'" '
        f'style="width:100%;height:100%;object-fit:cover;border-radius:inherit" alt="">'
    )
    return HTMLResponse(f'<span class="badge badge-done">Art applied ✓</span>{oob_img}')


@router.get("/library/artists/{artist_id}", response_class=HTMLResponse)
async def artist_page(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Artist page: owned tracks grouped by album + MB discography if MBID known."""
    from sqlalchemy.orm import joinedload as _jl

    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)

    # Owned tracks, grouped into albums
    tracks = (await session.execute(
        select(Track)
        .options(_jl(Track.album), _jl(Track.file))
        .where(Track.artist_id == artist_id)
        .order_by(Album.year.nullslast(), Album.title.nullslast(), Track.track_number.nullslast(), Track.title)  # type: ignore[union-attr]
        .outerjoin(Track.album)
        .join(Track.file)
    )).unique().scalars().all()

    # Group tracks by album
    from collections import OrderedDict
    albums_map: dict[str | None, list[Track]] = OrderedDict()
    for t in tracks:
        key = t.album_id
        if key not in albums_map:
            albums_map[key] = []
        albums_map[key].append(t)

    albums_list = []
    for album_id_key, atracks in albums_map.items():
        album_obj = atracks[0].album if atracks else None
        albums_list.append({
            "album": album_obj,
            "tracks": atracks,
        })

    # MB discography (if MBID known)
    mb_release_groups: list[dict] = []
    owned_rids: set[str] = {t.musicbrainz_recording_id for t in tracks if t.musicbrainz_recording_id}
    if artist.musicbrainz_artist_id:
        try:
            from service.core.normalize import normalize as _norm
            from service.metadata.musicbrainz import get_artist_release_groups
            _, rgs = await asyncio.to_thread(
                get_artist_release_groups, artist.musicbrainz_artist_id, settings.cache_dir
            )
            owned_album_titles = {_norm(a["album"].title) for a in albums_list if a["album"]}
            # Map normalised album title → owned track count for the completion indicator
            owned_title_counts: dict[str, int] = {
                _norm(a["album"].title): len(a["tracks"])
                for a in albums_list if a["album"]
            }
            for rg in rgs:
                owned = _norm(rg.title) in owned_album_titles
                mb_release_groups.append({
                    "release_group_id": rg.release_group_id,
                    "title": rg.title,
                    "year": rg.year,
                    "release_type": rg.release_type,
                    "owned": owned,
                    "owned_track_count": owned_title_counts.get(_norm(rg.title), 0),
                })
        except Exception as exc:
            logger.debug("Artist page MB lookup failed: %s", exc)

    return templates.TemplateResponse(
        request, "artist_page.html",
        {
            "active": "library",
            "artist": artist,
            "albums_list": albums_list,
            "mb_release_groups": mb_release_groups,
            "total_tracks": len(tracks),
        },
    )


@router.post("/library/artists/{artist_id}/delete", response_class=HTMLResponse)
async def delete_artist(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Delete an artist that has no tracks.  Removes empty albums first."""
    from sqlalchemy import delete as _sa_del

    # Purge orphaned Track rows (no TrackFile) — these ghost rows can block
    # deletion even though the artist page shows 0 tracks (it inner-joins files).
    orphan_ids = (await session.execute(
        select(Track.id)
        .outerjoin(TrackFile, TrackFile.track_id == Track.id)
        .where(Track.artist_id == artist_id)
        .where(TrackFile.id.is_(None))
    )).scalars().all()
    if orphan_ids:
        await session.execute(_sa_del(Track).where(Track.id.in_(orphan_ids)))

    track_count = (await session.execute(
        select(func.count(Track.id)).where(Track.artist_id == artist_id)
    )).scalar_one()
    if track_count > 0:
        raise HTTPException(400, "Artist still has tracks")

    # Delete empty albums for this artist
    old_albums = (await session.execute(
        select(Album).where(Album.artist_id == artist_id)
    )).scalars().all()
    for alb in old_albums:
        alb_tracks = (await session.execute(
            select(func.count(Track.id)).where(Track.album_id == alb.id)
        )).scalar_one()
        if alb_tracks == 0:
            await session.execute(_sa_del(Album).where(Album.id == alb.id))

    await session.execute(_sa_del(Artist).where(Artist.id == artist_id))
    await session.commit()

    return HTMLResponse("", status_code=200, headers={"HX-Redirect": "/library/artists"})


@router.post("/library/artists/{artist_id}/update", response_class=HTMLResponse)
async def update_artist(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Update artist name, sort name, and/or MB artist ID.  Writes tags to all track files."""
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    name_val = (form.get("name") or "").strip()
    sort_name_val = (form.get("sort_name") or "").strip() or None
    mb_artist_id_val = (form.get("musicbrainz_artist_id") or "").strip() or None

    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    if not name_val:
        raise HTTPException(400, "Artist name required")

    name_changed = name_val != artist.name
    sort_changed = sort_name_val != artist.sort_name

    artist.name = name_val
    artist.sort_name = sort_name_val
    artist.musicbrainz_artist_id = mb_artist_id_val
    artist.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()

    # Write tags to all track files if name or sort_name changed
    if name_changed or sort_changed:
        tracks = (await session.execute(
            select(Track).options(_jl(Track.file)).where(Track.artist_id == artist_id)
        )).unique().scalars().all()
        for t in tracks:
            if t.file:
                fp = Path(t.file.path)
                if fp.exists():
                    kwargs: dict = {}
                    if name_changed:
                        kwargs["artist"] = name_val
                        kwargs["albumartist"] = name_val
                    if sort_changed:
                        kwargs["artist_sort"] = sort_name_val or ""
                    try:
                        await asyncio.to_thread(_write_tags, fp, **kwargs)
                    except Exception as exc:
                        logger.warning("update_artist tag write failed for %s: %s", fp, exc)

    await _do_scans()

    # Re-render the artist page header section
    return HTMLResponse("", status_code=200, headers={"HX-Redirect": f"/library/artists/{artist_id}"})


@router.get("/library/artists/{artist_id}/image", response_class=HTMLResponse)
async def artist_image(
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve artist.jpg from the artist's music folder, or 404."""
    from fastapi.responses import FileResponse
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    img_path = settings.music_dir / artist.name / "artist.jpg"
    if img_path.exists():
        return FileResponse(str(img_path), media_type="image/jpeg")
    raise HTTPException(404)


@router.get("/library/artists/{artist_id}/mb-search", response_class=HTMLResponse)
async def artist_mb_search(
    request: Request,
    artist_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search MusicBrainz for artists by name; returns clickable candidates."""
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    search_name = q.strip() or artist.name
    safe_id = artist_id.replace(":", "_")
    try:
        import musicbrainzngs as _mb
        _mb.set_useragent("audioreap", "1.0")
        result = await asyncio.to_thread(
            lambda: _mb.search_artists(artist=search_name, limit=6)
        )
        candidates = []
        for a in result.get("artist-list", []):
            candidates.append({
                "mbid": a.get("id", ""),
                "name": a.get("name", ""),
                "sort_name": a.get("sort-name", ""),
                "type": a.get("type", ""),
                "score": a.get("ext:score", ""),
                "disambiguation": a.get("disambiguation", ""),
            })
    except Exception as exc:
        return HTMLResponse(f'<p class="muted" style="font-size:12px">MB search failed: {exc}</p>')

    if not candidates:
        return HTMLResponse('<p class="muted" style="font-size:12px">No results.</p>')

    return templates.TemplateResponse(
        request, "partials/artist_mb_candidates.html",
        {"candidates": candidates, "input_id": f"artist-mb-id-{safe_id}"},
    )


@router.get("/library/artists/{artist_id}/image-search", response_class=HTMLResponse)
async def artist_image_search(
    request: Request,
    artist_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search Deezer for artist images (no API key required)."""
    import httpx
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    search_name = q.strip() or artist.name
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.deezer.com/search/artist",
                params={"q": search_name, "limit": 6},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return HTMLResponse(f'<p class="muted" style="font-size:12px">Image search failed: {exc}</p>')

    results = [
        {"name": item["name"], "image_url": item.get("picture_medium", ""), "deezer_id": item["id"]}
        for item in data.get("data", [])
        if item.get("picture_medium") and "default_artist" not in item.get("picture_medium", "")
    ]
    return templates.TemplateResponse(
        request, "partials/artist_image_candidates.html",
        {"artist_id": artist_id, "results": results, "q": search_name},
    )


@router.post("/library/artists/{artist_id}/save-artist-image", response_class=HTMLResponse)
async def save_artist_image(
    request: Request,
    artist_id: str,
    image_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download an artist image and save as artist.jpg in the artist's music folder."""
    import httpx
    if not image_url:
        raise HTTPException(400, "image_url required")
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)

    artist_dir = settings.music_dir / artist.name
    artist_dir.mkdir(parents=True, exist_ok=True)
    img_path = artist_dir / "artist.jpg"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            img_path.write_bytes(resp.content)
    except Exception as exc:
        return HTMLResponse(f'<p style="font-size:12px;color:var(--danger)">Download failed: {exc}</p>')

    # Trigger Navidrome rescan so the new image is picked up
    await _do_scans()

    cache_bust = int(datetime.now(UTC).timestamp())
    return HTMLResponse(
        f'<img src="/library/artists/{artist_id}/image?v={cache_bust}" '
        f'style="width:80px;height:80px;object-fit:cover;border-radius:8px;display:block;margin-bottom:6px" '
        f'alt="{artist.name}">'
        f'<p style="font-size:12px;color:var(--success)">✓ Artist image saved — Navidrome rescan triggered.</p>'
    )


@router.post("/artist/{artist_id}/acquire-missing", response_class=HTMLResponse)
async def artist_acquire_missing(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue acquire_album_from_mb for every un-owned release group for this artist."""
    from service.db.schema import Artist as _Artist
    artist = await session.get(_Artist, artist_id)
    if artist is None or not artist.musicbrainz_artist_id:
        raise HTTPException(404)

    try:
        from service.core.normalize import normalize as _norm
        from service.metadata.musicbrainz import get_artist_release_groups
        _, rgs = await asyncio.to_thread(
            get_artist_release_groups, artist.musicbrainz_artist_id, settings.cache_dir
        )
    except Exception as exc:
        return HTMLResponse(f'<span class="badge-warn">MB lookup failed: {exc}</span>')

    # Find which release groups are already owned
    owned_albums = (await session.execute(
        select(Album).join(Album.tracks).join(Track.artist).where(Artist.id == artist_id)
    )).unique().scalars().all()
    owned_titles = {_norm(a.title) for a in owned_albums}
    unowned = [rg for rg in rgs if _norm(rg.title) not in owned_titles]

    if not unowned:
        return HTMLResponse('<span class="badge-done">All release groups already owned ✓</span>')

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        for rg in unowned:
            album_job_id = str(uuid.uuid4())
            await redis.enqueue_job(
                "acquire_album_from_mb",
                album_job_id=album_job_id,
                release_group_id=rg.release_group_id,
                artist_name=artist.name,
                music_dir=str(settings.music_dir),
                tmp_acquire_dir=str(settings.tmp_acquire_dir),
                _job_id=f"album:{album_job_id}",
            )
        await redis.aclose()
    except Exception as exc:
        return HTMLResponse(f'<span class="badge-warn">Queue error: {exc}</span>')

    return HTMLResponse(
        f'<span class="badge-ok">Queued {len(unowned)} album{"s" if len(unowned) != 1 else ""} → <a href="/jobs">Jobs</a></span>'
    )


@router.get("/library/artists", response_class=HTMLResponse)
async def library_artists_page(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy import func as _func
    stmt = (
        select(
            Artist,
            _func.count(Track.id.distinct()).label("track_count"),
            _func.count(Album.id.distinct()).label("album_count"),
        )
        .outerjoin(Artist.tracks)
        .outerjoin(Track.album)
        .group_by(Artist.id)
        .order_by(Artist.sort_name, Artist.name)
        .limit(500)
    )
    if q.strip():
        stmt = stmt.where(Artist.name.ilike(f"%{q.strip()}%"))
    rows = (await session.execute(stmt)).all()
    artists = [
        {"artist": r.Artist, "track_count": r.track_count, "album_count": r.album_count}
        for r in rows
    ]
    ctx = {"active": "library", "artists": artists, "q": q}
    # HTMX partial reload: return only the list block
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "partials/artist_list.html", ctx)
    return templates.TemplateResponse(request, "library_artists.html", ctx)


@router.get("/library/genres", response_class=HTMLResponse)
async def library_genres_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy import case as sa_case
    rows = (await session.execute(
        select(Track.genre, func.count(Track.id).label("track_count"))
        .join(Track.file)
        .where(Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc(), Track.genre)
    )).all()
    untagged_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.genre.is_(None))
    )).scalar_one()
    genres = [{"name": r.genre, "count": r.track_count} for r in rows]
    return templates.TemplateResponse(
        request, "library_genres.html",
        {"active": "library", "genres": genres, "untagged_count": untagged_count},
    )


@router.post("/library/genres/rename", response_class=HTMLResponse)
async def genre_rename(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Rename a genre across all tracks (DB + file tags)."""
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    old_genre = (form.get("old_genre") or "").strip()
    new_genre = (form.get("new_genre") or "").strip()
    if not old_genre:
        return HTMLResponse('<span class="badge badge-warn">Missing genre name</span>')

    target_genre = new_genre if new_genre else None  # empty new = remove genre

    rows = (await session.execute(
        select(Track).options(joinedload(Track.file)).where(Track.genre == old_genre)
    )).unique().scalars().all()
    for row in rows:
        row.genre = target_genre
        if row.file:
            fp = Path(row.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(_write_tags, fp, genre=target_genre or "")
                except Exception as exc:
                    logger.warning("genre_rename tag write failed for %s: %s", fp, exc)
    await session.commit()
    await _do_scans()

    rows2 = (await session.execute(
        select(Track.genre, func.count(Track.id).label("track_count"))
        .join(Track.file)
        .where(Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc(), Track.genre)
    )).all()
    untagged_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.genre.is_(None))
    )).scalar_one()
    genres = [{"name": r.genre, "count": r.track_count} for r in rows2]
    return templates.TemplateResponse(
        request, "partials/genre_list.html",
        {"genres": genres, "untagged_count": untagged_count},
    )


@router.post("/library/genres/remove", response_class=HTMLResponse)
async def genre_remove(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Remove a genre from all tracks that have it."""
    form = await request.form()
    genre = (form.get("genre") or "").strip()
    if not genre:
        return HTMLResponse('<span class="badge badge-warn">Missing genre name</span>')
    # Delegate to rename with empty new_genre
    # Reconstruct form-like data and call rename logic inline
    from service.library.tagger import write_tags as _write_tags

    rows = (await session.execute(
        select(Track).options(joinedload(Track.file)).where(Track.genre == genre)
    )).unique().scalars().all()
    for row in rows:
        row.genre = None
        if row.file:
            fp = Path(row.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(_write_tags, fp, genre="")
                except Exception as exc:
                    logger.warning("genre_remove tag write failed for %s: %s", fp, exc)
    await session.commit()
    await _do_scans()

    rows2 = (await session.execute(
        select(Track.genre, func.count(Track.id).label("track_count"))
        .join(Track.file)
        .where(Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc(), Track.genre)
    )).all()
    untagged_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.genre.is_(None))
    )).scalar_one()
    genres = [{"name": r.genre, "count": r.track_count} for r in rows2]
    return templates.TemplateResponse(
        request, "partials/genre_list.html",
        {"genres": genres, "untagged_count": untagged_count},
    )


@router.get("/library/browse", response_class=HTMLResponse)
async def library_browse(
    request: Request,
    q: str = "",
    f: str = "",
    sort: str = "artist",
    genre: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unified library browser: search + quality review + metadata edit."""
    return templates.TemplateResponse(
        request, "library_browse.html",
        {"active": "library", "q": q, "f": f, "sort": sort, "genre": genre},
    )


@router.get("/library/browse/results", response_class=HTMLResponse)
async def library_browse_results(
    request: Request,
    q: str = "",
    f: str = "",
    sort: str = "artist",
    offset: int = 0,
    genre: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    order = {
        "title":   Track.title,
        "quality": Track.tag_quality_score.asc().nullslast(),  # type: ignore[union-attr]
        "recent":  TrackFile.created_at.desc(),
        "album":   (Album.title.nullslast(), Track.track_number),  # type: ignore[union-attr]
    }.get(sort, (Artist.name, Track.title))

    stmt = (
        select(Track)
        .join(Track.artist)
        .outerjoin(Track.album)
        .join(Track.file)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .offset(offset)
        .limit(_BROWSE_PAGE + 1)
    )
    if isinstance(order, tuple):
        stmt = stmt.order_by(*order)
    else:
        stmt = stmt.order_by(order)
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            Track.title.ilike(pattern) | Artist.name.ilike(pattern) | Album.title.ilike(pattern)
        )

    # Filter tabs
    if f == "no_mb":
        stmt = stmt.where(Track.musicbrainz_recording_id.is_(None))
    elif f == "no_art":
        stmt = stmt.where(
            (TrackFile.has_cover_art.is_(None)) | (TrackFile.has_cover_art == 0)
        )
    elif f == "low_quality":
        stmt = stmt.where(
            Track.tag_quality_score.isnot(None),
            Track.tag_quality_score < LOW_QUALITY_THRESHOLD,
            (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0),
        )
    elif f == "low_bitrate":
        min_br = settings.min_bitrate_kbps
        stmt = stmt.where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < min_br,
            (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0),
        )
    elif f == "low_bitrate_suppressed":
        min_br = settings.min_bitrate_kbps
        stmt = stmt.where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < min_br,
            Track.bitrate_suppressed == 1,
        )
    elif f == "quality_suppressed":
        stmt = stmt.where(Track.quality_suppressed == 1)
    elif f == "dupes":
        dupe_rids_sub = (
            select(Track.musicbrainz_recording_id)
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.is_not(None))
            .group_by(Track.musicbrainz_recording_id)
            .having(func.count(Track.id) > 1)
            .scalar_subquery()
        )
        stmt = stmt.where(Track.musicbrainz_recording_id.in_(dupe_rids_sub))
    elif f == "singles":
        stmt = stmt.where(Track.album_id.is_(None))
    elif f == "no_genre":
        stmt = stmt.where(Track.genre.is_(None))

    # Genre is an orthogonal filter — stack it on top of any f tab.
    if genre:
        stmt = stmt.where(Track.genre == genre)

    all_rows = (await session.execute(stmt)).unique().scalars().all()
    has_more = len(all_rows) > _BROWSE_PAGE
    rows = all_rows[:_BROWSE_PAGE]
    return templates.TemplateResponse(
        request, "partials/browse_results.html",
        {"tracks": rows, "q": q, "f": f, "sort": sort,
         "offset": offset, "has_more": has_more, "next_offset": offset + _BROWSE_PAGE},
    )


@router.post("/library/browse/bulk-edit", response_class=HTMLResponse)
async def library_bulk_edit(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Apply genre and/or year to a batch of selected tracks."""
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    track_ids: list[str] = list(form.getlist("track_id"))  # type: ignore[arg-type]
    genre_val = (form.get("genre") or "").strip() or None  # type: ignore[union-attr]
    year_str = (form.get("year") or "").strip()  # type: ignore[union-attr]
    year_val: int | None = int(year_str) if year_str.isdigit() else None  # type: ignore[arg-type]

    if not track_ids:
        return HTMLResponse('<span class="badge-warn">No tracks selected</span>')
    if genre_val is None and year_val is None:
        return HTMLResponse('<span class="badge-warn">Enter at least one field to update</span>')

    updated = 0
    # Batch-fetch all selected tracks in one query
    all_rows = (await session.execute(
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id.in_(track_ids))
    )).unique().scalars().all()
    row_by_id = {r.id: r for r in all_rows}

    failed = 0
    for tid in track_ids:
        row = row_by_id.get(tid)
        if row is None or not row.file:
            continue
        file_path = Path(row.file.path)
        if not file_path.exists():
            continue
        try:
            kwargs: dict[str, object] = {}
            if genre_val is not None:
                kwargs["genre"] = genre_val
                row.genre = genre_val
            if year_val is not None:
                kwargs["year"] = year_val
            if kwargs:
                await asyncio.to_thread(_write_tags, file_path, **kwargs)
            updated += 1
        except Exception as exc:
            logger.warning("bulk-edit: write_tags failed for %s: %s", file_path, exc)
            failed += 1

    await session.commit()
    await _do_scans()

    msg = f"Updated {updated} track{'s' if updated != 1 else ''}"
    if failed:
        msg += f", {failed} failed"
    return HTMLResponse(f'<span class="badge-ok">{msg} ✓</span>')


@router.post("/library/tracks/{internal_id}/suppress-quality", response_class=HTMLResponse)
async def track_suppress_quality(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Mark a track's quality warning as suppressed so it no longer appears in the low-quality filter."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.quality_suppressed = True
    await session.commit()
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": row},
    )


@router.post("/library/tracks/{internal_id}/suppress-bitrate", response_class=HTMLResponse)
async def track_suppress_bitrate(
    request: Request,
    internal_id: str,
    from_health: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Mark a track's bitrate warning as suppressed so it no longer appears in the low-bitrate filter."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.bitrate_suppressed = True
    await session.commit()
    if from_health:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": row},
    )


@router.post("/library/tracks/{internal_id}/unsuppress-bitrate", response_class=HTMLResponse)
async def track_unsuppress_bitrate(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Remove bitrate suppression for a track."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.bitrate_suppressed = False
    await session.commit()
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": row},
    )


@router.post("/library/tracks/{internal_id}/unsuppress-quality", response_class=HTMLResponse)
async def track_unsuppress_quality(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Remove quality suppression for a track."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.quality_suppressed = False
    await session.commit()
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": row},
    )


@router.get("/library/tracks/{internal_id}/browse-row", response_class=HTMLResponse)
async def track_browse_row(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return a single browse-list row for a track (used by edit-card Cancel button)."""
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": row},
    )


@router.get("/library/tracks/{internal_id}/edit-card", response_class=HTMLResponse)
async def track_edit_card(
    request: Request,
    internal_id: str,
    album_id: str = Query(""),
    open_art: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy import distinct as _distinct
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    # Use genre stored in DB (populated by scanner and save-tags)
    genre: str | None = row.genre
    # Fall back to reading from file if DB has no genre yet
    if not genre and row.file:
        from service.library.tagger import read_tags as _read_tags
        fp = Path(row.file.path)
        if fp.exists():
            tagged = await asyncio.to_thread(_read_tags, fp)
            if tagged:
                genre = tagged.genre
    # Fetch all known genres, artist names, and albums for autocomplete datalists
    genre_rows = (await session.execute(
        select(_distinct(Track.genre)).where(Track.genre.isnot(None)).order_by(Track.genre)
    )).scalars().all()
    genres = [g for g in genre_rows if g]
    artist_names = (await session.execute(
        select(Artist.name).order_by(Artist.name)
    )).scalars().all()
    album_names = (await session.execute(
        select(Album.title)
        .where(Album.artist_id == row.artist_id)
        .order_by(Album.title)
    )).scalars().all()
    return templates.TemplateResponse(
        request, "partials/track_edit_card.html",
        {
            "track": row,
            "genre": genre,
            "genres": list(genres),
            "artist_names": list(artist_names),
            "album_names": list(album_names),
            "provider_ref": row.file.provider_ref if row.file else None,
            "bitrate_kbps": row.file.bitrate_kbps if row.file else None,
            "min_bitrate_kbps": settings.min_bitrate_kbps,
            "source_album_id": album_id,
            "open_art": open_art,
        },
    )


@router.post("/library/tracks/{internal_id}/save-tags", response_class=HTMLResponse)
async def save_track_tags(
    request: Request,
    internal_id: str,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    year: str = Form(""),
    track_number: str = Form(""),
    mb_recording_id: str = Form(""),
    genre: str = Form(""),
    source_album_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.library.tagger import write_tags as _write_tags, has_cover_art as _has_cover_art
    from service.metadata.quality import compute_quality_score
    from service.index.scanner import _upsert_artist, _upsert_album

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")

    # Parse inputs
    year_val: int | None = int(year) if year.strip().isdigit() else None
    track_num_val: int | None = int(track_number) if track_number.strip().isdigit() else None
    title_val = title.strip() or row.title
    artist_val = artist.strip() or row.artist.name
    album_val = album.strip() or (row.album.title if row.album else None)
    mbid_val = mb_recording_id.strip() or None
    genre_val = genre.strip() or None

    # Write tags to file
    try:
        await asyncio.to_thread(
            _write_tags,
            file_path,
            title=title_val,
            artist=artist_val,
            albumartist=artist_val,
            album=album_val,
            year=year_val,
            track_number=track_num_val,
            mb_recording_id=mbid_val,
            genre=genre_val,
        )
    except Exception as exc:
        logger.warning("save-tags write failed for %s: %s", file_path, exc)

    # Update DB — update existing rows in-place to avoid hash ID churn
    row.title = title_val
    row.track_number = track_num_val
    row.musicbrainz_recording_id = mbid_val
    row.genre = genre_val

    old_artist_id: str | None = None
    if artist_val != row.artist.name:
        old_artist_id = row.artist_id
        new_artist_id = await _upsert_artist(session, artist_val)
        row.artist_id = new_artist_id

    if album_val:
        # Re-upsert album whenever artist OR album title changed so the album
        # stays associated with the correct artist.
        artist_changed = old_artist_id is not None
        album_changed = not row.album or album_val != row.album.title
        if artist_changed or album_changed:
            artist_id_for_album = row.artist_id
            new_album_id = await _upsert_album(session, artist_id_for_album, album_val, year_val, artist_val)
            row.album_id = new_album_id
    elif not album_val:
        row.album_id = None

    hca = await asyncio.to_thread(_has_cover_art, file_path)
    if row.file:
        row.file.has_cover_art = hca
    row.tag_quality_score = compute_quality_score(
        title=title_val, artist=artist_val, album=album_val, year=year_val,
        track_number=track_num_val, musicbrainz_recording_id=mbid_val, has_cover_art=hca,
    )
    await session.commit()

    # Prune old artist: delete its empty albums first, then delete artist if it
    # now has 0 tracks.  Empty albums must be removed first or the FK prevents
    # the artist delete.
    if old_artist_id:
        remaining = (await session.execute(
            select(func.count(Track.id)).where(Track.artist_id == old_artist_id)
        )).scalar_one()
        if remaining == 0:
            from sqlalchemy import delete as _sa_del_artist
            # Remove albums that now have no tracks
            old_albums = (await session.execute(
                select(Album).where(Album.artist_id == old_artist_id)
            )).scalars().all()
            for alb in old_albums:
                alb_tracks = (await session.execute(
                    select(func.count(Track.id)).where(Track.album_id == alb.id)
                )).scalar_one()
                if alb_tracks == 0:
                    await session.execute(_sa_del_artist(Album).where(Album.id == alb.id))
            old_artist = await session.get(Artist, old_artist_id)
            if old_artist:
                await session.delete(old_artist)
            await session.commit()

    await _do_scans()

    # When called from album detail view, reload the whole album card so the
    # track list reflects the updated metadata immediately.
    if source_album_id:
        from sqlalchemy.orm import joinedload as _jl2
        album_row = (await session.execute(
            select(Album)
            .options(_jl2(Album.artist), _jl2(Album.tracks).joinedload(Track.file))
            .where(Album.id == source_album_id)
        )).unique().scalar_one_or_none()
        if album_row:
            safe_aid = source_album_id.replace(":", "_")
            sorted_tracks = sorted(
                album_row.tracks, key=lambda t: (t.track_number is None, t.track_number or 0)
            )
            cover_track = next(
                (t for t in album_row.tracks if t.file and Path(t.file.path).exists()), None
            )
            resp = templates.TemplateResponse(
                request, "partials/album_detail.html",
                {"album": album_row, "sorted_tracks": sorted_tracks,
                 "cover_track": cover_track, "saved": True},
            )
            resp.headers["HX-Retarget"] = f"#album-{safe_aid}"
            resp.headers["HX-Reswap"] = "outerHTML"
            return resp

    # Reload fresh row, collapse back to browse-row in library context
    stmt2 = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    updated = (await session.execute(stmt2)).unique().scalar_one_or_none()
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": updated},
    )


@router.get("/library/tracks/{internal_id}/mb-search", response_class=HTMLResponse)
async def library_track_mb_search(
    request: Request,
    internal_id: str,
    q: str = "",
    limit: int = 10,
    duration: int | None = None,
) -> HTMLResponse:
    if not q.strip():
        return HTMLResponse("")
    from service.metadata.musicbrainz import search_recordings_free
    results = await asyncio.to_thread(
        search_recordings_free, q.strip(), limit, settings.cache_dir, duration
    )
    return templates.TemplateResponse(
        request, "partials/mb_candidates.html",
        {"results": results, "job_id": None, "track_id": internal_id,
         "q": q.strip(), "limit": limit, "duration": duration},
    )


@router.post("/library/tracks/{internal_id}/retag", response_class=HTMLResponse)
async def retag_track(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.library.tagger import has_cover_art as _has_cover_art, write_tags as _write_tags
    from service.metadata.musicbrainz import get_recording_by_id
    from service.metadata.quality import compute_quality_score

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.musicbrainz_recording_id or not row.file:
        raise HTTPException(400, "Track not found or missing MB Recording ID")

    match = await asyncio.to_thread(
        get_recording_by_id, row.musicbrainz_recording_id, settings.cache_dir
    )
    if match is None:
        raise HTTPException(502, "MusicBrainz lookup failed")

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    await asyncio.to_thread(
        _write_tags,
        file_path,
        title=match.title or None,
        artist=match.artist or None,
        album=match.album,
        year=match.year,
        track_number=match.track_number,
    )
    hca = await asyncio.to_thread(_has_cover_art, file_path)

    row.file.has_cover_art = hca
    row.tag_quality_score = compute_quality_score(
        title=match.title or row.title,
        artist=match.artist or row.artist.name,
        album=match.album or (row.album.title if row.album else None),
        year=match.year,
        track_number=match.track_number,
        musicbrainz_recording_id=row.musicbrainz_recording_id,
        has_cover_art=hca,
    )
    await session.commit()

    pct = int((row.tag_quality_score or 0) * 100)
    return HTMLResponse(
        f'<div class="card" style="opacity:0.6">'
        f'<div class="card-info">'
        f'<div class="card-title">{row.title}</div>'
        f'<div class="card-sub">{row.artist.name} · Re-tagged · Quality {pct}%</div>'
        f"</div></div>"
    )


@router.get("/library/quality", response_class=HTMLResponse)
async def quality_review_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Dedicated quality review: low bitrate, missing art, missing files."""
    min_br = settings.min_bitrate_kbps

    # Low-bitrate tracks (has file, bitrate known and below threshold)
    low_br_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(
                TrackFile.bitrate_kbps.isnot(None),
                TrackFile.bitrate_kbps < min_br,
            )
            .order_by(TrackFile.bitrate_kbps.asc())
            .limit(30)
        )
    ).unique().scalars().all()

    # Tracks missing cover art but with MB ID (so CAA fetch may help)
    no_art_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(
                Track.musicbrainz_recording_id.isnot(None),
                (TrackFile.has_cover_art.is_(None)) | (TrackFile.has_cover_art == 0),
            )
            .order_by(Track.title)
            .limit(30)
        )
    ).unique().scalars().all()

    # Missing files: TrackFile in DB but file not on disk
    all_file_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .limit(500)
        )
    ).unique().scalars().all()
    missing_file_tracks = [
        r for r in all_file_rows
        if r.file and not Path(r.file.path).exists()
    ][:30]

    def _to_dict(row: Track, extra: dict[str, object] | None = None) -> dict[str, object]:
        d: dict[str, object] = {
            "internal_id": row.id,
            "title": row.title,
            "artist": row.artist.name,
            "album": row.album.title if row.album else None,
            "has_mbid": bool(row.musicbrainz_recording_id),
            "provider": row.file.provider if row.file else None,
            "provider_ref": row.file.provider_ref if row.file else None,
            "bitrate_kbps": row.file.bitrate_kbps if row.file else None,
            "codec": row.file.codec if row.file else None,
        }
        if extra:
            d.update(extra)
        return d

    return templates.TemplateResponse(
        request, "quality_review.html",
        {
            "active": "library",
            "min_bitrate_kbps": min_br,
            "low_bitrate": [_to_dict(r) for r in low_br_rows],
            "no_art": [_to_dict(r) for r in no_art_rows],
            "missing_files": [_to_dict(r) for r in missing_file_tracks],
        },
    )


@router.post("/library/tracks/{internal_id}/reacquire", response_class=HTMLResponse)
async def reacquire_track(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a re-acquisition for a track using its original provider_ref."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    if not row.file or not row.file.provider_ref:
        raise HTTPException(400, "Track has no provider reference — search and re-acquire manually")

    candidate = TrackCandidate(
        provider=row.file.provider or "ytdlp",
        provider_ref=row.file.provider_ref,
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        duration_seconds=row.duration_seconds,
    )

    job_id = await create_job(
        session,
        provider_name=candidate.provider,
        provider_ref=candidate.provider_ref,
        candidate=candidate,
        query=f"{candidate.artist} - {candidate.title} [re-acquire]",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=candidate.provider,
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
        f'<div class="card" style="opacity:0.5">'
        f'<div class="card-info">'
        f'<div class="card-title">{row.title}</div>'
        f'<div class="card-sub">{row.artist.name} · Re-acquisition queued → <a href="/jobs">Jobs</a></div>'
        f"</div></div>"
    )


@router.get("/library/tracks/{internal_id}/search-replacement", response_class=HTMLResponse)
async def search_replacement_sources(
    request: Request,
    internal_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search for a replacement audio source for an existing library track."""
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)

    search_q = q.strip() or f"{row.artist.name} - {row.title}"
    candidates: list[dict[str, object]] = []
    try:
        import service.providers.ytdlp  # noqa: F401
        from service.core.models import SearchQuery
        from service.providers import get

        provider = get("ytdlp")()
        raw: list[dict[str, object]] = []
        async for c in provider.search(SearchQuery(q=search_q, limit=10)):
            raw.append({
                "title": c.title,
                "artist": c.artist,
                "duration_seconds": c.duration_seconds,
                "provider_ref": c.provider_ref,
                "thumbnail_url": c.thumbnail_url,
                "candidate_json": c.model_dump_json(),
                "_score": _explicit_score(c.title),
            })
        if settings.prefer_explicit:
            raw.sort(key=lambda x: -int(x["_score"]))  # type: ignore[arg-type]
        for item in raw:
            del item["_score"]
        candidates = raw[:8]
    except Exception as exc:
        logger.warning("Replacement search failed for %s: %s", internal_id, exc)

    return templates.TemplateResponse(
        request, "partials/replacement_results.html",
        {"candidates": candidates, "track": row, "q": search_q},
    )


@router.post("/library/tracks/{internal_id}/queue-replacement", response_class=HTMLResponse)
async def queue_replacement_track(
    request: Request,
    internal_id: str,
    provider_ref: str = Form(...),
    candidate_json: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a new acquisition to replace a library track's audio source.

    Locks the existing track's album/artist/MB ID into the candidate so album
    grouping is preserved. The old track remains until the user deletes it after
    approving the replacement in the review queue.
    """
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)

    try:
        base = TrackCandidate.model_validate_json(candidate_json)
    except Exception:
        raise HTTPException(400, "Invalid candidate JSON")

    # Lock existing track's metadata so album grouping is preserved.
    # skip_dedup=True: the existing track IS the local match — we want to replace it,
    # not have the dedup check mark the job done immediately.
    locked = base.model_copy(update={
        "title": row.title,
        "artist": row.artist.name,
        "album": row.album.title if row.album else None,
        "year": row.album.year if row.album else None,
        "track_number": row.track_number,
        "mb_recording_id": row.musicbrainz_recording_id,
        "mb_release_id": row.album.musicbrainz_release_id if row.album else None,
        "skip_dedup": True,
    })

    job_id = await create_job(
        session,
        provider_name=locked.provider,
        provider_ref=provider_ref,
        candidate=locked,
        query=f"{locked.artist} - {locked.title} [replacement]",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=locked.provider,
            provider_ref=provider_ref,
            candidate_json=locked.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    safe_id = internal_id.replace(":", "_")
    return HTMLResponse(
        f'<span class="badge badge-done" id="replace-status-{safe_id}">'
        f'Queued → <a href="/jobs">Jobs ↗</a></span>'
    )


@router.post("/library/tracks/{internal_id}/queue-url-replacement", response_class=HTMLResponse)
async def queue_url_replacement(
    request: Request,
    internal_id: str,
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a replacement download from a user-supplied URL."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref=url.strip(),
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        year=row.album.year if row.album else None,
        track_number=row.track_number,
        mb_recording_id=row.musicbrainz_recording_id,
        mb_release_id=row.album.musicbrainz_release_id if row.album else None,
        skip_dedup=True,
    )

    job_id = await create_job(
        session,
        provider_name=candidate.provider,
        provider_ref=candidate.provider_ref,
        candidate=candidate,
        query=f"{candidate.artist} - {candidate.title} [url-replacement]",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=candidate.provider,
            provider_ref=candidate.provider_ref,
            candidate_json=candidate.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    safe_id = internal_id.replace(":", "_")
    return HTMLResponse(
        f'<span class="badge badge-done" id="replace-status-{safe_id}">'
        f'Queued → <a href="/jobs">Jobs ↗</a></span>'
    )


@router.post("/library/tracks/{internal_id}/fetch-art", response_class=HTMLResponse)
async def fetch_track_art(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Fetch cover art from Cover Art Archive and embed it in the track file."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_tags as _write_tags
    from service.metadata.artwork import fetch_artwork
    from service.metadata.musicbrainz import get_recording_by_id
    from service.metadata.quality import compute_quality_score

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.musicbrainz_recording_id or not row.file:
        raise HTTPException(400, "Track not found or missing MB Recording ID")

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    # Get release ID for CAA via MB recording lookup
    mb_rec = await asyncio.to_thread(
        get_recording_by_id, row.musicbrainz_recording_id, settings.cache_dir
    )
    release_id = mb_rec.release_id if mb_rec else None

    art = await fetch_artwork(
        release_mbid=release_id,
        cache_dir=settings.cache_dir,
    )
    if not art:
        return HTMLResponse(
            '<span class="badge badge-warn">No artwork found on Cover Art Archive</span>'
        )

    await asyncio.to_thread(_write_tags, file_path, artwork_bytes=art)
    hca = await asyncio.to_thread(_has_cover_art, file_path)

    row.file.has_cover_art = hca
    row.tag_quality_score = compute_quality_score(
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        year=None,
        track_number=row.track_number,
        musicbrainz_recording_id=row.musicbrainz_recording_id,
        has_cover_art=hca,
    )
    await session.commit()
    await _do_scans()

    return HTMLResponse('<span class="badge badge-done">Art embedded ✓</span>')


# ── Cover art search ──────────────────────────────────────────────────────────

async def _search_itunes_art(q: str) -> list[dict]:
    """Search iTunes Store for album artwork. Returns list of {url, label} dicts."""
    import urllib.parse
    results: list[dict] = []
    try:
        encoded = urllib.parse.quote(q)
        url = f"https://itunes.apple.com/search?term={encoded}&entity=album&limit=12&media=music"
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return results
            data = resp.json()
            for item in data.get("results", []):
                art_url = item.get("artworkUrl100", "")
                if not art_url:
                    continue
                # iTunes returns 100×100; swap to 600×600
                art_url = art_url.replace("100x100bb", "600x600bb")
                thumb_url = art_url.replace("600x600bb", "150x150bb")
                artist = item.get("artistName", "")
                album = item.get("collectionName", "")
                results.append({
                    "thumb": thumb_url,
                    "full": art_url,
                    "label": f"{artist} — {album}" if artist else album,
                    "source": "iTunes",
                })
    except Exception as exc:
        logger.debug("iTunes art search failed: %s", exc)
    return results


async def _fetch_caa_for_rg(client: "Any", rg_id: str) -> list[dict]:
    """List all releases in an MB release group and probe CAA for covers (inner helper)."""
    releases_url = f"https://musicbrainz.org/ws/2/release?release-group={rg_id}&fmt=json&limit=25"
    rels_resp = await client.get(releases_url)
    if rels_resp.status_code != 200:
        return []
    releases = rels_resp.json().get("releases", [])

    async def _fetch_caa(rel_id: str, rel_label: str) -> "dict | None":
        try:
            caa = await client.get(
                f"https://coverartarchive.org/release/{rel_id}/front-250",
                follow_redirects=True,
            )
            if caa.status_code == 200 and caa.headers.get("content-type", "").startswith("image/"):
                full = await client.get(
                    f"https://coverartarchive.org/release/{rel_id}/front",
                    follow_redirects=False,
                )
                full_url = full.headers.get("location", f"https://coverartarchive.org/release/{rel_id}/front")
                return {"thumb": f"https://coverartarchive.org/release/{rel_id}/front-250",
                        "full": full_url, "label": rel_label, "source": "CAA"}
        except Exception:
            pass
        return None

    import asyncio as _asyncio
    tasks = [
        _fetch_caa(
            r["id"],
            f"{r.get('title', '')} ({r.get('date', '')[:4] if r.get('date') else '?'})"
            f" [{r.get('country', '') or r.get('status', '')}]"
        )
        for r in releases[:15]
    ]
    return [r for r in await _asyncio.gather(*tasks) if r is not None]


async def _search_caa_editions(release_id: str) -> list[dict]:
    """Fetch all CAA covers for every edition in the same MB release group (given a release ID)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "audioreap/0.1"}) as client:
            rg_url = f"https://musicbrainz.org/ws/2/release/{release_id}?inc=release-groups&fmt=json"
            rg_resp = await client.get(rg_url)
            if rg_resp.status_code != 200:
                return []
            rg_id = (rg_resp.json().get("release-group") or {}).get("id")
            if not rg_id:
                return []
            return await _fetch_caa_for_rg(client, rg_id)
    except Exception as exc:
        logger.debug("CAA editions search failed: %s", exc)
    return []


async def _search_caa_by_rg(rg_id: str) -> list[dict]:
    """Fetch all CAA covers for every edition in an MB release group (given the group ID directly)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "audioreap/0.1"}) as client:
            return await _fetch_caa_for_rg(client, rg_id)
    except Exception as exc:
        logger.debug("CAA by-rg search failed: %s", exc)
    return []


@router.get("/art/search", response_class=HTMLResponse)
async def art_search(
    request: Request,
    q: str = "",
    release_id: str = "",
    release_group_id: str = "",
    apply_url: str = "",
    result_target: str = "",
) -> HTMLResponse:
    """Return a thumbnail grid from iTunes + CAA editions for the given query."""
    results: list[dict] = []
    if q.strip():
        itunes = await _search_itunes_art(q.strip())
        results.extend(itunes)
    if release_id.strip():
        caa = await _search_caa_editions(release_id.strip())
        results.extend(caa)
    elif release_group_id.strip():
        caa = await _search_caa_by_rg(release_group_id.strip())
        results.extend(caa)

    if not results:
        return HTMLResponse('<p class="empty" style="font-size:12px;padding:8px 0">No results found.</p>')

    return templates.TemplateResponse(
        request, "partials/art_search_results.html",
        {"results": results, "apply_url": apply_url, "result_target": result_target},
    )


@router.post("/library/tracks/{internal_id}/apply-art", response_class=HTMLResponse)
async def apply_art_to_track(
    request: Request,
    internal_id: str,
    art_url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download art from a URL and embed it in a track file."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small, fetch_from_url

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)
    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    art = await fetch_from_url(art_url)
    if not art:
        return HTMLResponse('<span class="badge badge-warn">Could not download image</span>')
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return HTMLResponse('<span class="badge badge-warn">Image too small (< 300×300)</span>')

    await asyncio.to_thread(_write_tags, file_path, artwork_bytes=art)
    # Only write sidecar cover.jpg for album tracks — singles share their parent
    # directory with other singles from the same artist, so a sidecar would
    # overwrite every sibling's cover.
    if row.album_id is not None:
        write_cover_jpg(file_path.parent, art)
    hca = await asyncio.to_thread(_has_cover_art, file_path)
    row.file.has_cover_art = hca
    await session.commit()
    await _do_scans()

    # Refresh the cover art preview in the card header via OOB swap.
    # The card uses id="edit-cover-{safe_id}" where safe_id = track.id.replace(':', '_').
    import time as _time
    safe_id = internal_id.replace(":", "_")
    cache_bust = int(_time.time())
    oob_img = (
        f'<img src="/library/tracks/{internal_id}/cover-art?t={cache_bust}" '
        f'id="edit-cover-{safe_id}" hx-swap-oob="true" '
        f'onerror="this.style.display=\'none\';'
        f'var ph=document.getElementById(\'edit-cover-placeholder-{safe_id}\');'
        f'if(ph)ph.style.display=\'flex\'" '
        f'style="width:100%;height:100%;object-fit:cover;border-radius:inherit" alt="">'
    )
    return HTMLResponse(f'<span class="badge badge-done">Art applied ✓</span>{oob_img}')


@router.post("/library/albums/{album_id}/apply-art", response_class=HTMLResponse)
async def apply_art_to_album(
    request: Request,
    album_id: str,
    art_url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download art from a URL and embed it in all tracks of an album."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small, fetch_from_url
    from sqlalchemy.orm import joinedload as _jl

    art = await fetch_from_url(art_url)
    if not art:
        return HTMLResponse('<span class="badge badge-warn">Could not download image</span>')
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return HTMLResponse('<span class="badge badge-warn">Image too small (< 300×300)</span>')

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    album_dir: Path | None = None
    embedded = 0
    for track in album.tracks:
        if not track.file:
            continue
        fp = Path(track.file.path)
        if not fp.exists():
            continue
        album_dir = album_dir or fp.parent
        try:
            await asyncio.to_thread(_write_tags, fp, artwork_bytes=art)
            hca = await asyncio.to_thread(_has_cover_art, fp)
            track.file.has_cover_art = hca
            embedded += 1
        except Exception as exc:
            logger.debug("apply_art_to_album: embed failed for %s: %s", fp, exc)

    if album_dir:
        write_cover_jpg(album_dir, art)

    await session.commit()
    await _do_scans()
    return HTMLResponse(f'<span class="badge badge-done">Art applied to {embedded} track(s) ✓</span>')


@router.post("/library/tracks/{internal_id}/upload-art", response_class=HTMLResponse)
async def upload_track_art(
    request: Request,
    internal_id: str,
    cover: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Embed a user-supplied image as cover art in a track file."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)
    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    if not cover.content_type or not cover.content_type.startswith("image/"):
        return HTMLResponse('<span class="badge badge-warn">Not an image file</span>')

    art = await cover.read()
    if not art:
        return HTMLResponse('<span class="badge badge-warn">Empty file</span>')
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return HTMLResponse('<span class="badge badge-warn">Image too small — must be at least 300×300 px</span>')

    await asyncio.to_thread(_write_tags, file_path, artwork_bytes=art)
    write_cover_jpg(file_path.parent, art)
    hca = await asyncio.to_thread(_has_cover_art, file_path)
    row.file.has_cover_art = hca
    await session.commit()

    return HTMLResponse(
        f'<img src="/library/tracks/{internal_id}/cover-art?t={int(asyncio.get_event_loop().time())}"'
        f' style="width:100%;height:100%;object-fit:cover;border-radius:inherit" alt="">'
        f'<span class="badge badge-done" style="position:absolute;bottom:4px;left:4px;font-size:10px">Saved ✓</span>'
    )


@router.post("/library/albums/{album_id}/cover/upload", response_class=HTMLResponse)
async def upload_album_cover(
    request: Request,
    album_id: str,
    cover: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Embed a user-supplied image as cover art for all tracks in an album + sidecar."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small
    from sqlalchemy.orm import joinedload as _jl

    if not cover.content_type or not cover.content_type.startswith("image/"):
        return HTMLResponse('<span class="badge badge-warn">Not an image file</span>')

    art = await cover.read()
    if not art:
        return HTMLResponse('<span class="badge badge-warn">Empty file</span>')
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return HTMLResponse('<span class="badge badge-warn">Image too small — must be at least 300×300 px</span>')

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    album_dir: Path | None = None
    embedded = 0
    for track in album.tracks:
        if not track.file:
            continue
        fp = Path(track.file.path)
        if not fp.exists():
            continue
        album_dir = album_dir or fp.parent
        try:
            await asyncio.to_thread(_write_tags, fp, artwork_bytes=art)
            hca = await asyncio.to_thread(_has_cover_art, fp)
            track.file.has_cover_art = hca
            embedded += 1
        except Exception as exc:
            logger.debug("upload_album_cover: embed failed for %s: %s", fp, exc)

    if album_dir:
        write_cover_jpg(album_dir, art)

    await session.commit()
    await _do_scans()
    return HTMLResponse(f'<span class="badge badge-done">Cover saved to {embedded} track(s) ✓</span>')


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    import shutil as _shutil

    try:
        disk = _shutil.disk_usage(settings.music_dir)
        disk_free_gb = round(disk.free / 1024**3, 1)
    except Exception:
        disk_free_gb = -1

    navidrome_ok = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{settings.navidrome_url}/rest/ping.view",
                params={"u": "x", "p": "x", "v": "1.16.1", "c": "audioreap", "f": "json"},
            )
            navidrome_ok = r.status_code < 500
    except Exception:
        pass

    redis_ok = False
    worker_ok = False
    try:
        import redis.asyncio as aioredis
        from datetime import timedelta
        rc = aioredis.from_url(settings.redis_url)
        await rc.ping()
        redis_ok = True
        hb = await rc.get("audioreap:worker:heartbeat")
        if hb:
            from datetime import datetime as _dt
            hb_time = _dt.fromisoformat(hb.decode())
            worker_ok = (_dt.utcnow() - hb_time) < timedelta(minutes=2)
        await rc.aclose()
    except Exception:
        pass

    active_jobs = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state.notin_(["done", "failed"]))
        )
    ).scalar_one()

    return templates.TemplateResponse(
        request, "health.html",
        {
            "active": "sys-health",
            "health": {
                "navidrome_ok": navidrome_ok,
                "redis_ok": redis_ok,
                "worker_ok": worker_ok,
                "disk_free_gb": disk_free_gb,
                "active_jobs": active_jobs,
                "music_dir": str(settings.music_dir),
                "version": "0.1.0",
            },
        },
    )


# ── Playlists ─────────────────────────────────────────────────────────────

@router.get("/playlists", response_class=HTMLResponse)
async def playlists_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, "", "playlists", session)
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.post("/playlists/resolve", response_class=HTMLResponse)
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


@router.post("/playlists/acquire", response_class=HTMLResponse)
async def acquire_playlist(
    request: Request,
    import_url: str = Form(...),
    import_title: str = Form(...),
    import_source: str = Form(default="unknown"),
    candidates: list[str] = Form(default=[]),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.acquisition.jobs import create_job

    if not candidates:
        return HTMLResponse('<p class="empty">No tracks selected.</p>')

    now = datetime.now(UTC).replace(tzinfo=None)
    import_id = str(uuid.uuid4())

    pl_row = PlaylistImport(
        id=import_id,
        url=import_url,
        title=import_title or "Untitled Playlist",
        source=import_source,
        track_count=len(candidates),
        enqueued_count=0,
        owned_count=0,
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

    pl_row.enqueued_count = len(job_data)
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        for job_id, candidate_json, candidate in job_data:
            await redis.enqueue_job(
                "acquire_track",
                job_id=job_id,
                provider_name=candidate.provider,
                provider_ref=candidate.provider_ref,
                candidate_json=candidate_json,
                music_dir=str(settings.music_dir),
                tmp_acquire_dir=str(settings.tmp_acquire_dir),
                _job_id=f"acquire:{job_id}",
            )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    return RedirectResponse("/jobs", status_code=303)


async def _resolve_spotify_playlist(url: str) -> tuple[str, str, list[TrackCandidate]]:
    """Resolve a Spotify playlist via the Spotify API (credential-based or anonymous).

    When AUDIOREAP_SPOTIFY_CLIENT_ID is set, uses the official client-credentials
    OAuth flow. Otherwise, falls back to Spotify's anonymous web-player token so
    that public playlists can be imported without any API key.
    """
    import re as _re

    match = _re.search(r"playlist/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("Could not extract Spotify playlist ID from URL")
    playlist_id = match.group(1)

    if settings.spotify_client_id:
        token = await _spotify_client_token()
    else:
        token = await _spotify_anonymous_token()

    import httpx
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

        next_url: str | None = (
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
            "?fields=items(track(name,artists,album,duration_ms,type)),next&limit=50"
        )
        while next_url:
            r = await client.get(next_url)
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

    return pl_title, "spotify", candidates


async def _spotify_anonymous_token() -> str:
    """Spotify blocked anonymous server-side API access entirely in 2024.

    This function now always raises with a clear message directing the user to
    configure Spotify API credentials via the admin config panel.
    """
    raise ValueError(
        "Spotify requires API credentials for server-side playlist import. "
        "Go to Settings → Config and set AUDIOREAP_SPOTIFY_CLIENT_ID and "
        "AUDIOREAP_SPOTIFY_CLIENT_SECRET (free at developer.spotify.com/dashboard)."
    )


async def _spotify_client_token() -> str:
    import base64
    import httpx

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


# Keywords that indicate a result is a live recording, cover, or tribute —
# not the original studio track we're looking for.
_LIVE_KEYWORDS = frozenset({
    "live", "concert", "in concert", "at the", "at madison", "tour",
    "tribute", "cover", "karaoke", "instrumental", "acoustic",
    "session", "radio edit", "bbc", "unplugged", "bootleg",
})


def _looks_like_live(title: str) -> bool:
    """Return True if a YouTube result title suggests a live/cover/tribute version."""
    import re
    lower = title.lower()
    # Quick check for parenthesised/bracketed qualifiers like "(live)", "[concert]"
    bracketed = re.findall(r'[\(\[\{]([^\)\]\}]+)[\)\]\}]', lower)
    for chunk in bracketed:
        words = set(chunk.split())
        if words & _LIVE_KEYWORDS:
            return True
    # Also catch plain suffixes like "- Live at ..." or "- Live Version"
    if re.search(r'\blive\b', lower) or re.search(r'\btribute\b', lower):
        return True
    return False


def _yt_search_best(
    artist: str,
    title: str,
    duration_seconds: int | None = None,
    n_candidates: int = 5,
    prefer_ytm: bool = True,
) -> tuple[str, float]:
    """Search for the best-matching studio version on YouTube (Music).

    Fetches up to *n_candidates* results, scores each by:
    - Title similarity to expected title (65 %)
    - Duration proximity (25 %)
    - Live/cover penalty (−0.30 if flagged)

    Returns (url, best_score). Score < 0.35 means no confident match was found.
    Caller should abort the download rather than queuing a likely-wrong result.
    """
    import yt_dlp
    from service.search.matcher import title_similarity as _tsim

    query = f"{artist} {title}"
    # YouTube Music tends to have cleaner studio-track results
    prefix = f"ytmsearch{n_candidates}" if prefer_ytm else f"ytsearch{n_candidates}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}

    entries = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}:{query}", download=False)
        entries = (info or {}).get("entries") or []
    except Exception:
        pass

    # Fall back to regular YouTube if YouTube Music returned nothing
    if not entries and prefer_ytm:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch{n_candidates}:{query}", download=False)
            entries = (info or {}).get("entries") or []
        except Exception:
            pass

    if not entries:
        return f"ytsearch1:{query}", 0.0

    best_url = ""
    best_score = -1.0

    for entry in entries:
        if not entry:
            continue
        vid_title = str(entry.get("track") or entry.get("title") or "")
        vid_dur = entry.get("duration")
        vid_id = entry.get("id") or ""
        url = str(entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}")

        # Title similarity: use track-only field if available (YouTube Music sets it)
        t_sim = _tsim(title, vid_title)

        # Duration proximity score (0–1)
        dur_score = 0.5  # neutral when unknown
        if duration_seconds and vid_dur:
            delta = abs(duration_seconds - int(vid_dur))
            if delta <= 5:
                dur_score = 1.0
            elif delta <= 15:
                dur_score = 0.8
            elif delta <= 30:
                dur_score = 0.6
            elif delta > 90:
                dur_score = 0.1

        score = t_sim * 0.65 + dur_score * 0.25

        # Penalise live / cover / tribute versions (unless the user is searching for one)
        if _looks_like_live(vid_title) and not _looks_like_live(title):
            score -= 0.30

        if score > best_score:
            best_score = score
            best_url = url

    return best_url or f"ytsearch1:{query}", max(best_score, 0.0)


# ── Discography ───────────────────────────────────────────────────────────

@router.get("/discography", response_class=HTMLResponse)
async def discography_page(
    request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, "", "discover", session)
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.get("/discography/search", response_class=HTMLResponse)
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


@router.get("/discography/{artist_mbid}/{release_group_id}/tracks", response_class=HTMLResponse)
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


@router.post("/discography/{artist_mbid}/{release_group_id}/acquire-track", response_class=HTMLResponse)
async def discography_acquire_single_track(
    request: Request,
    artist_mbid: str,
    release_group_id: str,
    recording_id: str = Form(""),
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    track_number: str = Form(""),
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
        duration_seconds=dur_s,
        mb_recording_id=recording_id or None,
    )

    # If no candidate scored above the confidence floor, create a ghost job in
    # needs_review with no staging file. The review card will have the source
    # search panel open so the user can paste a URL or search manually.
    # This keeps the track visible in the queue — never silently skipped.
    if yt_score < 0.35:
        import json as _json
        from service.db.schema import AcquisitionJobRow as _JobRow
        job_id = str(__import__("uuid").uuid4())
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


@router.post("/discography/{artist_mbid}/{release_group_id}/acquire", response_class=HTMLResponse)
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
        return HTMLResponse(f'<span class="badge-warn">Error: {exc}</span>')

    return HTMLResponse(
        f'<span id="disco-status-{album_job_id}"'
        f' hx-get="/discography/album-status/{album_job_id}"'
        f' hx-trigger="load, every 5s"'
        f' hx-swap="outerHTML">'
        f'Queued…'
        f'</span>'
    )


@router.get("/discography/album-status/{album_job_id}", response_class=HTMLResponse)
async def discography_album_status(
    request: Request,
    album_job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Polling endpoint: returns a status badge for an in-progress album acquisition."""
    from service.db.schema import AlbumAcquisitionJob as _AlbumJob

    album = await session.get(_AlbumJob, album_job_id)
    if album is None:
        return HTMLResponse('<span class="badge badge-fail">Not found</span>')

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
        return HTMLResponse('<span class="badge badge-fail">Failed</span>')

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


@router.get("/discography/{artist_mbid}", response_class=HTMLResponse)
async def discography_view(
    request: Request,
    artist_mbid: str,
    types: list[str] = [],
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
                    .options(joinedload(Album.tracks))
                    .where(Album.artist_id == la.id)
                )
            ).unique().scalars().all()
            break

    # Build lookup maps: release_group_id → track_count, normalized_title → track_count
    rg_to_track_count: dict[str, int] = {}
    title_to_track_count: dict[str, int] = {}
    for la in local_albums_list:
        tc = len(la.tracks)
        if la.mb_release_group_id:
            rg_to_track_count[la.mb_release_group_id] = tc
        title_to_track_count[_normalize(la.title)] = tc

    release_entries = []
    for rg in filtered:
        normalized_title = _normalize(rg.title)
        # Prefer release-group ID match, fall back to title similarity
        if rg.release_group_id in rg_to_track_count:
            owned = True
            owned_track_count = rg_to_track_count[rg.release_group_id]
        else:
            best_match = max(
                ((tc, title_similarity(normalized_title, local_t))
                 for local_t, tc in title_to_track_count.items()),
                key=lambda x: x[1],
                default=(0, 0.0),
            )
            owned = best_match[1] >= 0.80
            owned_track_count = best_match[0] if owned else 0
        release_entries.append({
            "release_group_id": rg.release_group_id,
            "title": rg.title,
            "year": rg.year,
            "release_type": rg.release_type,
            "owned": owned,
            "owned_track_count": owned_track_count,
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
    return templates.TemplateResponse(
        request, "discography.html", {"active": "discography", **ctx}
    )


def _read_mb_release_id(path: Path) -> str | None:
    """Read MUSICBRAINZ_ALBUMID from file tags using mutagen."""
    try:
        import mutagen
        f = mutagen.File(path)
        if f is None:
            return None
        # Vorbis / OGG / FLAC
        for key in ("musicbrainz_albumid", "MUSICBRAINZ_ALBUMID"):
            if key in f:
                v = f[key]
                return str(v[0]) if isinstance(v, list) and v else str(v) if v else None
        # ID3 (MP3): TXXX:MusicBrainz Album Id
        if hasattr(f, "tags") and f.tags:
            for frame_key in f.tags.keys():
                if "musicbrainz album id" in frame_key.lower():
                    frame = f.tags[frame_key]
                    if hasattr(frame, "text"):
                        return str(frame.text[0]) if frame.text else None
        # MP4
        if "----:com.apple.iTunes:MusicBrainz Album Id" in f:
            raw = f["----:com.apple.iTunes:MusicBrainz Album Id"]
            return raw[0].decode() if raw and isinstance(raw[0], bytes) else None
    except Exception:
        pass
    return None


# ── Library Health / Management ───────────────────────────────────────────


@router.get("/library/health", response_class=HTMLResponse)
async def library_health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Library health overview — duplicates, split albums, missing covers."""
    dupe_count = (await session.execute(
        select(func.count()).select_from(
            select(Track.musicbrainz_recording_id)
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.is_not(None))
            .group_by(Track.musicbrainz_recording_id)
            .having(func.count(Track.id) > 1)
            .subquery()
        )
    )).scalar_one()

    no_cover_count = (await session.execute(
        select(func.count(Album.id)).where(
            ~Album.id.in_(
                select(Track.album_id)
                .join(Track.file)
                .where(TrackFile.has_cover_art == 1)
                .where(Track.album_id.is_not(None))
            )
        ).where(
            Album.id.in_(
                select(Track.album_id).where(Track.album_id.is_not(None))
            )
        )
    )).scalar_one()

    no_mbid_count = (await session.execute(
        select(func.count(Track.id))
        .join(Track.file)
        .where(Track.musicbrainz_recording_id.is_(None))
    )).scalar_one()

    low_bitrate_count = (await session.execute(
        select(func.count(Track.id))
        .join(Track.file)
        .where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < settings.min_bitrate_kbps,
            (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0),
        )
    )).scalar_one()

    return templates.TemplateResponse(
        request, "library_health.html",
        {
            "active": "lib-health",
            "dupe_count": dupe_count,
            "no_cover_count": no_cover_count,
            "no_mbid_count": no_mbid_count,
            "low_bitrate_count": low_bitrate_count,
            "min_bitrate_kbps": settings.min_bitrate_kbps,
        },
    )


@router.get("/library/health/dupes", response_class=HTMLResponse)
async def library_health_dupes(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: duplicate tracks (same MB recording_id, multiple files)."""
    from collections import defaultdict
    from sqlalchemy.orm import joinedload as _jl

    dupe_rids = (await session.execute(
        select(Track.musicbrainz_recording_id)
        .join(Track.file)
        .where(Track.musicbrainz_recording_id.is_not(None))
        .group_by(Track.musicbrainz_recording_id)
        .having(func.count(Track.id) > 1)
    )).scalars().all()

    groups: list[dict] = []
    if dupe_rids:
        rows = (await session.execute(
            select(Track)
            .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.in_(dupe_rids))
            .order_by(Track.musicbrainz_recording_id, TrackFile.bitrate_kbps.desc().nulls_last())
        )).unique().scalars().all()

        by_rid: dict[str, list[Track]] = defaultdict(list)
        for t in rows:
            by_rid[t.musicbrainz_recording_id].append(t)  # type: ignore[index]

        for rid, tracks in by_rid.items():
            groups.append({
                "recording_id": rid,
                "title": tracks[0].title,
                "artist": tracks[0].artist.name,
                "tracks": [
                    {
                        "id": t.id,
                        "path": t.file.path if t.file else "",
                        "codec": t.file.codec if t.file else "",
                        "bitrate_kbps": t.file.bitrate_kbps if t.file else None,
                        "has_cover_art": bool(t.file.has_cover_art) if t.file else False,
                        "quality_score": t.tag_quality_score,
                    }
                    for t in tracks
                ],
            })

    return templates.TemplateResponse(
        request, "partials/health_dupes.html", {"groups": groups}
    )


@router.post("/library/health/dupes/keep-best", response_class=HTMLResponse)
async def dupes_keep_best(
    request: Request,
    recording_id: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Keep the highest-bitrate copy; trash all lower-quality duplicates."""
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.file))
        .join(Track.file)
        .where(Track.musicbrainz_recording_id == recording_id)
        .order_by(TrackFile.bitrate_kbps.desc().nulls_last(), Track.tag_quality_score.desc().nulls_last())
    )).unique().scalars().all()

    if len(rows) <= 1:
        return HTMLResponse("")  # nothing to do, remove the card

    for track in rows[1:]:  # keep rows[0], trash the rest
        if track.file:
            file_path = Path(track.file.path)
            album_dir = file_path.parent
            if file_path.exists():
                try:
                    safe_trash(file_path, settings.music_dir / ".trash")
                except Exception as exc:
                    logger.warning("Trash failed for %s: %s", file_path, exc)
            _trash_empty_album_dir(album_dir, settings.music_dir / ".trash")
            await session.delete(track.file)
        await session.delete(track)

    await session.commit()
    return HTMLResponse("")  # remove the group card on success


@router.get("/library/health/splits", response_class=HTMLResponse)
async def library_health_splits(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: albums split across multiple folders due to artist name variants."""
    from collections import defaultdict
    from service.core.normalize import normalize

    rows = (await session.execute(
        select(
            Album.id, Album.title, Album.year, Artist.name,
            func.count(Track.id).label("ntracks"),
        )
        .join(Artist, Artist.id == Album.artist_id)
        .join(Track, Track.album_id == Album.id)
        .group_by(Album.id, Album.title, Album.year, Artist.name)
    )).all()

    # Group by normalized (title, artist)
    key_to_albums: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for album_id, title, year, artist_name, ntracks in rows:
        key = (normalize(title), normalize(artist_name))
        key_to_albums[key].append({
            "id": album_id,
            "title": title,
            "year": year,
            "artist": artist_name,
            "ntracks": ntracks,
        })

    split_groups = [
        albums for albums in key_to_albums.values()
        if len(albums) > 1
    ]
    # Sort each group: most tracks first (canonical candidate)
    for g in split_groups:
        g.sort(key=lambda a: a["ntracks"], reverse=True)

    return templates.TemplateResponse(
        request, "partials/health_splits.html", {"groups": split_groups}
    )


@router.get("/library/health/no-mbid", response_class=HTMLResponse)
async def library_health_no_mbid(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks without a MusicBrainz recording ID."""
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.file)
        .where(Track.musicbrainz_recording_id.is_(None))
        .order_by(Track.tag_quality_score.asc().nulls_first())
        .limit(50)
    )).unique().scalars().all()

    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name,
            "album": t.album.title if t.album else None,
            "quality_score": t.tag_quality_score,
        }
        for t in rows
    ]
    return templates.TemplateResponse(
        request, "partials/health_no_mbid.html",
        {"tracks": tracks, "total": len(tracks)},
    )


@router.get("/library/health/low-bitrate", response_class=HTMLResponse)
async def library_health_low_bitrate(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks with bitrate below the configured threshold."""
    from sqlalchemy.orm import joinedload as _jl

    min_br = settings.min_bitrate_kbps
    _not_suppressed = (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0)
    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.file)
        .where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < min_br,
            _not_suppressed,
        )
        .order_by(TrackFile.bitrate_kbps.asc())
        .limit(100)
    )).unique().scalars().all()

    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name,
            "album": t.album.title if t.album else None,
            "bitrate_kbps": t.file.bitrate_kbps if t.file else None,
            "codec": t.file.codec if t.file else None,
            "bitrate_suppressed": bool(t.bitrate_suppressed),
        }
        for t in rows
    ]
    return templates.TemplateResponse(
        request, "partials/health_low_bitrate.html",
        {"tracks": tracks, "min_bitrate_kbps": min_br},
    )


@router.post("/library/tracks/{track_id}/enrich", response_class=HTMLResponse)
async def enrich_track_now(
    track_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Enqueue a MusicBrainz enrichment job for a specific library track."""
    row = await session.get(Track, track_id)
    if row is None:
        raise HTTPException(404)

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("enrich_track", track_id=track_id)
        await redis.aclose()
    except Exception as exc:
        return HTMLResponse(f'<span style="color:var(--danger);font-size:12px">Error: {exc}</span>')

    return HTMLResponse('<span style="color:var(--success);font-size:12px">✓ Enrichment queued — check the Jobs page</span>')


@router.post("/library/enrich", response_class=HTMLResponse)
async def library_enrich_filtered(
    request: Request,
    session: AsyncSession = Depends(get_session),
    artist: str = Form(""),
    album: str = Form(""),
) -> HTMLResponse:
    """Queue MusicBrainz enrichment for tracks without a Recording ID.

    Optional artist/album name filters narrow the scope — useful for re-enriching
    a specific artist or album rather than the entire library.
    """
    from sqlalchemy.orm import joinedload as _jl
    from service.core.normalize import normalize as _norm

    stmt = (
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album))
        .where(Track.musicbrainz_recording_id.is_(None))
    )
    rows = (await session.execute(stmt)).unique().scalars().all()

    artist_filter = _norm(artist.strip())
    album_filter = _norm(album.strip())
    if artist_filter:
        rows = [r for r in rows if artist_filter in _norm(r.artist.name)]
    if album_filter:
        rows = [r for r in rows if r.album and album_filter in _norm(r.album.title)]

    if not rows:
        return HTMLResponse('<p class="empty" style="font-size:12px;padding:4px 0">No matching tracks without a MB Recording ID.</p>')

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        for track in rows:
            await redis.enqueue_job("enrich_track", track_id=track.id)
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    label = f"{len(rows)} track{'s' if len(rows) != 1 else ''}"
    if artist_filter or album_filter:
        parts = []
        if artist_filter:
            parts.append(f'artist “{artist.strip()}”')
        if album_filter:
            parts.append(f'album “{album.strip()}”')
        label += f" matching {' + '.join(parts)}"
    return HTMLResponse(f'<p style="font-size:12px;padding:4px 0;color:var(--success)">✓ Queued enrichment for {label} — results appear in Jobs.</p>')


@router.delete("/library/albums/{album_id}", response_class=HTMLResponse)
async def delete_album(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Trash all files in an album and remove its DB records."""
    from sqlalchemy.orm import joinedload as _jl

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    album_dirs: set[Path] = set()
    for track in album.tracks:
        if track.file:
            fp = Path(track.file.path)
            album_dirs.add(fp.parent)
            if fp.exists():
                try:
                    safe_trash(fp, settings.music_dir / ".trash")
                except Exception as exc:
                    logger.warning("Trash failed for %s: %s", fp, exc)
            await session.delete(track.file)
        await session.delete(track)

    await session.delete(album)
    await session.commit()

    for d in album_dirs:
        _trash_empty_album_dir(d, settings.music_dir / ".trash")

    await _do_scans()

    return HTMLResponse("")


@router.post("/library/albums/{album_id}/cover/fetch", response_class=HTMLResponse)
async def fetch_album_cover(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Fetch cover art from Cover Art Archive and write as cover.jpg."""
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import write_cover_jpg
    from service.metadata.artwork import fetch_from_caa

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    # Find the first track with a real file to locate the album dir and release ID
    release_id: str | None = album.musicbrainz_release_id
    album_dir: Path | None = None
    for track in album.tracks:
        if track.file and Path(track.file.path).exists():
            album_dir = Path(track.file.path).parent
            if not release_id:
                release_id = _read_mb_release_id(Path(track.file.path))
            break

    # If still no release ID, resolve one from the release group (cached)
    if not release_id and album.mb_release_group_id:
        try:
            from service.metadata.musicbrainz import get_release_group_tracks
            _, release_id, _, _ = await asyncio.to_thread(
                get_release_group_tracks, album.mb_release_group_id, settings.cache_dir
            )
        except Exception:
            pass

    if not release_id:
        return HTMLResponse('<span class="badge-warn">No MusicBrainz release ID — cannot fetch cover</span>')
    if album_dir is None:
        return HTMLResponse('<span class="badge-warn">No files found for this album</span>')

    art = await fetch_from_caa(release_id)
    if art is None:
        return HTMLResponse('<span class="badge-warn">Cover not found on Cover Art Archive</span>')

    try:
        write_cover_jpg(album_dir, art)
    except Exception as exc:
        return HTMLResponse(f'<span class="badge-warn">Write failed: {exc}</span>')

    # Update has_cover_art on all track files in this album
    for track in album.tracks:
        if track.file:
            track.file.has_cover_art = True
    await session.commit()

    return HTMLResponse('<span class="badge-ok">Cover saved ✓</span>')


@router.post("/library/albums/{canonical_id}/merge/{source_id}", response_class=HTMLResponse)
async def merge_album(
    request: Request,
    canonical_id: str,
    source_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Move source album files into canonical album folder and reassign all DB records.

    Deliberately does NOT call index_file() — that would re-read file tags and
    associate tracks with a new album based on the source's tags instead of the
    canonical. Instead we update TrackFile.path and Track.album_id directly.
    """
    from service.library.writer import atomic_place as _atomic_place
    from sqlalchemy.orm import joinedload as _jl

    canonical = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == canonical_id)
    )).unique().scalar_one_or_none()
    source = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == source_id)
    )).unique().scalar_one_or_none()

    if canonical is None or source is None:
        raise HTTPException(404)

    # Determine the canonical album directory from any file that exists on disk
    canonical_dir: Path | None = None
    for t in canonical.tracks:
        if t.file and Path(t.file.path).exists():
            canonical_dir = Path(t.file.path).parent
            break
    if canonical_dir is None:
        # Canonical has no files yet — use the layout function to compute the expected dir
        from service.library.layout import track_path as _track_path
        canonical_dir = _track_path(
            settings.music_dir,
            artist=canonical.artist.name if canonical.artist else "Unknown",
            album=canonical.title,
            year=canonical.year,
            track_number=None, disc_number=None,
            title="placeholder", ext="flac",
            albumartist=canonical.artist.name if canonical.artist else None,
        ).parent
        canonical_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    already_there = 0
    collisions = 0

    # Collect source dirs BEFORE mutating track.file.path — the loop below
    # updates those paths in-memory, so collecting after would yield canonical_dir.
    src_dirs: set[Path] = set()
    for track in source.tracks:
        if track.file:
            d = Path(track.file.path).parent
            if d != canonical_dir:
                src_dirs.add(d)

    for track in source.tracks:
        if not track.file:
            # Reassign album even if no file
            track.album_id = canonical_id
            continue

        src = Path(track.file.path)
        dst = canonical_dir / src.name

        if src == dst:
            # Files are already in canonical dir — just reassign album in DB
            already_there += 1
            track.album_id = canonical_id
            continue

        if not src.exists():
            # Stale DB record — update path to where it should be, reassign album
            track.file.path = str(dst)
            track.album_id = canonical_id
            continue

        if dst.exists():
            # Name collision — reassign album, don't move the file
            collisions += 1
            logger.info("Merge: name collision at %s — keeping source %s, reassigning album", dst, src)
            track.album_id = canonical_id
            continue

        try:
            _atomic_place(src, dst)
            # Update TrackFile path in DB to reflect new location
            track.file.path = str(dst)
            track.album_id = canonical_id
            moved += 1
        except Exception as exc:
            logger.warning("Merge: failed to move %s → %s: %s", src, dst, exc)

    # Clean up source dirs: trash any remaining sidecars (cover.jpg etc.) and rmdir.
    for src_dir in src_dirs:
        try:
            _trash_empty_album_dir(src_dir, settings.music_dir / ".trash")
        except Exception:
            pass

    # Normalize album/albumartist/year/MUSICBRAINZ_ALBUMID on all merged (source)
    # tracks so Navidrome groups them into a single album.
    # File paths in track.file.path are already updated to canonical_dir above.
    canonical_release_id: str | None = canonical.musicbrainz_release_id
    if not canonical_release_id:
        for t in canonical.tracks:
            if t.file:
                fp = Path(t.file.path)
                if fp.exists():
                    canonical_release_id = _read_mb_release_id(fp)
                    if canonical_release_id:
                        break

    from service.library.tagger import write_tags as _write_tags_merge
    canonical_artist_name = canonical.artist.name if canonical.artist else "Unknown"
    for track in source.tracks:
        if track.file:
            fpath = Path(track.file.path)
            if fpath.exists():
                try:
                    await asyncio.to_thread(
                        _write_tags_merge, fpath,
                        album=canonical.title,
                        albumartist=canonical_artist_name,
                        year=canonical.year,
                        mb_release_id=canonical_release_id,
                    )
                    logger.info("Merge: normalized tags on %s", fpath.name)
                except Exception as exc:
                    logger.warning("Merge: tag update failed for %s: %s", fpath.name, exc)

    # Persist the release ID on the canonical album row if not already set
    if canonical_release_id and not canonical.musicbrainz_release_id:
        canonical.musicbrainz_release_id = canonical_release_id

    # Delete the source Album row — all its tracks have been reassigned.
    # We flush first to commit the album_id updates, then expunge source from the
    # session and use raw SQL to delete it.  If we called session.delete(source)
    # while the ORM still sees its tracks collection, SQLAlchemy would auto-NULL
    # every track's album_id (nullable FK, no cascade), undoing our reassignments.
    await session.flush()
    session.expunge(source)
    from sqlalchemy import delete as _sa_delete
    await session.execute(_sa_delete(Album).where(Album.id == source_id))

    await session.commit()

    await _do_scans()

    # Return the refreshed album list so the UI updates immediately
    stmt2 = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .order_by(Artist.name, Album.year, Album.title)
        .limit(300)
    )
    albums = (await session.execute(stmt2)).unique().scalars().all()
    album_quality: dict[str, float | None] = {}
    for alb in albums:
        scores = [t.tag_quality_score for t in alb.tracks if t.tag_quality_score is not None]
        album_quality[alb.id] = round(sum(scores) / len(scores), 3) if scores else None
    return templates.TemplateResponse(
        request, "partials/album_list.html",
        {"albums": albums, "q": "", "album_quality": album_quality},
    )


# ── Trash recovery ────────────────────────────────────────────────────────────


def _list_trash(trash_dir: Path) -> list[dict]:
    """Walk a .trash directory and return metadata for each file."""
    items: list[dict] = []
    if not trash_dir.exists():
        return items
    for ts_dir in sorted(trash_dir.iterdir(), reverse=True):
        if not ts_dir.is_dir() or ts_dir.name.startswith("."):
            continue
        for f in ts_dir.iterdir():
            if f.name.endswith(".restore_path") or not f.is_file():
                continue
            restore_path_file = ts_dir / f"{f.name}.restore_path"
            original_path: str | None = None
            if restore_path_file.exists():
                try:
                    original_path = restore_path_file.read_text(encoding="utf-8").strip()
                except Exception:
                    pass
            try:
                size_bytes = f.stat().st_size
            except OSError:
                size_bytes = 0
            items.append({
                "ts": ts_dir.name,
                "filename": f.name,
                "original_path": original_path,
                "size_mb": round(size_bytes / 1_048_576, 1),
            })
    return items


@router.get("/library/trash", response_class=HTMLResponse)
async def library_trash(request: Request) -> HTMLResponse:
    music_trash = _list_trash(settings.music_dir / ".trash")
    staging_trash = _list_trash(settings.staging_dir / ".trash")
    return templates.TemplateResponse(
        request, "partials/trash_list.html",
        {"music_trash": music_trash, "staging_trash": staging_trash},
    )


@router.post("/library/trash/{ts}/{filename}/restore", response_class=HTMLResponse)
async def trash_restore(
    request: Request,
    ts: str,
    filename: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Restore a trashed file to its original path (or music root if unknown)."""
    import urllib.parse
    filename = urllib.parse.unquote(filename)
    # Check both music and staging trash directories
    trash_file = settings.music_dir / ".trash" / ts / filename
    if not trash_file.exists():
        trash_file = settings.staging_dir / ".trash" / ts / filename
    if not trash_file.exists():
        raise HTTPException(404, "File not found in trash")

    restore_path_file = trash_file.parent / f"{filename}.restore_path"
    if restore_path_file.exists():
        try:
            dest = Path(restore_path_file.read_text(encoding="utf-8").strip())
        except Exception:
            dest = settings.music_dir / filename
    else:
        dest = settings.music_dir / filename

    try:
        from service.library.writer import atomic_place
        atomic_place(trash_file, dest)
        # Clean up sidecar
        if restore_path_file.exists():
            restore_path_file.unlink(missing_ok=True)
    except Exception as exc:
        raise HTTPException(500, f"Restore failed: {exc}") from exc

    try:
        from service.index.scanner import index_file
        await index_file(session, dest)
        await session.commit()
    except Exception:
        pass

    await _do_scans()

    return HTMLResponse(f'<span class="badge-ok">Restored → {dest.name}</span>')


@router.delete("/library/trash/{ts}/{filename}", response_class=HTMLResponse)
async def trash_delete(ts: str, filename: str) -> HTMLResponse:
    """Permanently delete a file from trash."""
    import urllib.parse
    filename = urllib.parse.unquote(filename)
    trash_file = settings.music_dir / ".trash" / ts / filename
    if not trash_file.exists():
        trash_file = settings.staging_dir / ".trash" / ts / filename
    restore_sidecar = trash_file.parent / f"{filename}.restore_path"
    try:
        if trash_file.exists():
            trash_file.unlink()
        restore_sidecar.unlink(missing_ok=True)
        # Remove empty timestamp dir
        try:
            trash_file.parent.rmdir()
        except OSError:
            pass
    except Exception as exc:
        raise HTTPException(500, f"Delete failed: {exc}") from exc
    return HTMLResponse("")


# ── Bulk cover art fetch ──────────────────────────────────────────────────────


@router.post("/library/health/fetch-missing-covers", response_class=HTMLResponse)
async def fetch_missing_covers(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Enqueue a background arq job to fetch cover art for all albums missing it."""
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("fetch_missing_covers")
        await redis.aclose()
    except Exception as exc:
        return HTMLResponse(f'<span class="badge-warn">Queue unavailable: {exc}</span>')
    return HTMLResponse('<span class="badge-ok">Cover art fetch queued — check back in a few minutes</span>')


# ── Admin ─────────────────────────────────────────────────────────────────────


@router.post("/admin/update-ytdlp", response_class=HTMLResponse)
async def admin_update_ytdlp(request: Request) -> HTMLResponse:
    """Run pip install -U yt-dlp inside the container and report the result."""
    import subprocess
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            # Extract the new version line from pip output
            for line in (result.stdout + result.stderr).splitlines():
                if "yt-dlp" in line.lower() and ("successfully installed" in line.lower() or "already" in line.lower()):
                    return HTMLResponse(f'<span class="badge-ok">yt-dlp updated: {line.strip()}</span>')
            return HTMLResponse('<span class="badge-ok">yt-dlp updated ✓</span>')
        return HTMLResponse(f'<span class="badge-warn">pip failed (exit {result.returncode}): {result.stderr[:200]}</span>')
    except Exception as exc:
        return HTMLResponse(f'<span class="badge-warn">Update failed: {exc}</span>')


@router.get("/admin/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    from service.config import CONFIG_EDITABLE_KEYS
    current = {k: getattr(settings, k) for k in CONFIG_EDITABLE_KEYS}
    return templates.TemplateResponse(
        request, "admin_config.html", {"active": "library", "current": current}
    )


@router.post("/admin/config", response_class=HTMLResponse)
async def admin_config_save(request: Request) -> HTMLResponse:
    from service.config import CONFIG_EDITABLE_KEYS, save_config_overrides
    form = await request.form()
    overrides: dict = {}
    for key in CONFIG_EDITABLE_KEYS:
        val = form.get(key)
        if val is not None:
            field_type = type(getattr(settings, key))
            if field_type is bool:
                overrides[key] = val == "true"
            else:
                try:
                    overrides[key] = field_type(val)
                except Exception:
                    pass
    save_config_overrides(overrides)
    current = {k: getattr(settings, k) for k in CONFIG_EDITABLE_KEYS}
    return templates.TemplateResponse(
        request, "admin_config.html",
        {"active": "library", "current": current, "saved": True}
    )
