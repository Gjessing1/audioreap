"""HTMX-rendered web UI routes."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
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


def _local_album_rows(album) -> list[dict]:
    """Owned-track rows for album_tracklist.html's local (non-MB) mode.

    Sorted disc-major so multi-disc albums don't interleave (two "track 1" rows
    from different discs). Done here rather than in Jinja because disc_number is
    None on single-disc tracks and Jinja's sort can't compare None with int.
    """
    def _key(t):
        return (t.disc_number or 1, t.track_number is None, t.track_number or 0, t.title or "")
    return [
        {"status": "plain", "number": t.track_number, "title": t.title,
         "track": t, "disc": t.disc_number}
        for t in sorted(album.tracks, key=_key)
    ]


templates.env.globals["local_album_rows"] = _local_album_rows

from service.library.writer import trash_empty_album_dir as _trash_empty_album_dir

_JOBS_COMPLETED_PAGE = 50
_BROWSE_PAGE = 75
_COMPLETED_STATES = ("done", "failed", "cancelled")


def _error_badge(message: str, *, level: str = "warn") -> HTMLResponse:
    """Shared inline error partial for HTMX swap targets.

    The one error-presentation pattern for routes whose hx-target is a small
    inline element (spans/badges) — full-page routes should keep raising
    HTTPException instead. level="warn" for user-fixable conditions,
    "fail" for operations that errored. Always 200: htmx does not swap
    non-2xx responses into the target, so the message would never be seen.
    Escapes the message — exception text can embed file/MB-derived strings.
    """
    cls = "badge badge-fail" if level == "fail" else "badge badge-warn"
    return HTMLResponse(f'<span class="{cls}">{html.escape(str(message))}</span>')
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
    except Exception as exc:
        logger.debug("best-effort Navidrome scan trigger failed: %s", exc)
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
    ctx["has_active_jobs"] = bool(active_rows)

    # At-a-glance queue summary chips (in-progress states only).
    from collections import Counter as _Counter
    _sc = _Counter(r.state for r in active_rows)
    ctx["status_counts"] = {
        "downloading": _sc.get("downloading", 0),
        "working": sum(_sc.get(s, 0) for s in ("processing", "enriching", "tagging", "importing", "placing")),
        "waiting": _sc.get("waiting", 0),
        "queued": _sc.get("queued", 0),
    }

    # Album batches whose child track jobs don't exist yet (worker still fetching
    # the MB tracklist) would otherwise be invisible on /jobs. Surface a ghost
    # placeholder so the user can see the album query is working. Once any child
    # row exists, the placeholder is dropped and the real album group renders.
    #
    # Gate tightly so stale album rows don't haunt the queue: the worker stamps
    # track_count once it has fetched the tracklist, so track_count == 0/NULL is
    # the precise "still fetching" signal. Also bound by age — acquire_album_from_mb
    # leaves the row in "running" indefinitely (independent child jobs finish on
    # their own), so without a recency window every past album would reappear.
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    from sqlalchemy import or_ as _or
    from service.db.schema import AlbumAcquisitionJob as _AlbumJob
    represented_album_ids = {r.album_job_id for r in rows if r.album_job_id}
    _placeholder_cutoff = _dt.now(_UTC).replace(tzinfo=None) - _td(minutes=15)
    pending_album_rows = (await session.execute(
        select(_AlbumJob)
        .where(_AlbumJob.state.in_(("queued", "running")))
        .where(_or(_AlbumJob.track_count == 0, _AlbumJob.track_count.is_(None)))
        .where(_AlbumJob.created_at >= _placeholder_cutoff)
        .order_by(_AlbumJob.created_at.desc())
        .limit(50)
    )).scalars().all()
    ctx["album_placeholders"] = [
        {
            "album_job_id": a.id,
            "label": " — ".join(p for p in [a.album_artist, a.album_title] if p)
                     or (a.query or a.id[:8]),
        }
        for a in pending_album_rows
        if a.id not in represented_album_ids
    ]
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
    _ACTIVE = ("queued", "waiting", "downloading", "processing", "enriching", "tagging", "importing", "placing")
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
        # Keyed "active_jobs" (not "active") so it doesn't shadow the nav-highlight
        # key when jobs_page renders {"active": "jobs", **ctx}.
        "active_jobs": active,
        "completed": completed,
        "completed_has_more": False,
        "completed_next_offset": 0,
    }


def _row_resolved_meta(row: AcquisitionJobRow) -> dict:
    """Parsed resolved_metadata_json, or {} when absent/corrupt."""
    if not row.resolved_metadata_json:
        return {}
    try:
        return json.loads(row.resolved_metadata_json)
    except Exception as exc:
        logger.debug("corrupt resolved_metadata_json on job %s: %s", row.id, exc)
        return {}


def _classify_review_confidence(row: AcquisitionJobRow, m: dict) -> tuple[str, str | None]:
    """Classify a needs_review job for the job-list confidence border.

    Returns (confidence, flag_reason) where confidence is one of:
      "flagged"  — force_staging_reason set or staging file missing (red border)
      "verified" — AcoustID-confirmed or text_search_similarity ≥ 0.90 (green)
      "probable" — no flags, but below the verified threshold (amber)
    flag_reason is the human-readable reason when flagged, else None.
    """
    has_staging = bool(row.staging_path and Path(row.staging_path).exists())
    if not has_staging:
        return "flagged", "Staging file missing — use Re-download"
    if m:
        if m.get("force_staging_reason"):
            return "flagged", m["force_staging_reason"]
        source = m.get("mb_match_source")
        if source == "acoustid" or (
            source == "text_search" and (m.get("text_search_similarity") or 0) >= 0.90
        ):
            return "verified", None
    return "probable", None


def _fmt_duration(seconds: object) -> str:
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return ""
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _source_summary(row: AcquisitionJobRow, m: dict) -> dict | None:
    """Compact "what did we actually download" line for a review job card.

    Shows the raw source video title (decorations intact), channel, length, and
    version hint chips (live/clean/remix/…) so the user can spot a wrong-version
    pick from the list without opening the review card.
    """
    url = (m.get("source_url") or "").strip()
    if not url:
        pr = (row.provider_ref or "").strip()
        if pr.startswith(("http://", "https://")):
            url = pr
    title = (m.get("source_title") or "").strip()
    channel = (m.get("source_channel") or "").strip()
    if not title and not url:
        return None
    hints: list[dict[str, str]] = []
    if title:
        from service.providers.ytdlp import source_title_hints
        hints = source_title_hints(title)
    return {
        "url": url,
        "title": title,
        "channel": channel,
        "duration": _fmt_duration(m.get("source_duration_seconds")),
        "hints": hints,
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
        # Confidence drives the colour-coded left border on the job-list card so the
        # user can triage needs_review jobs without opening each review card.
        meta = _row_resolved_meta(r)
        confidence, flag_reason = _classify_review_confidence(r, meta)
        is_flagged = confidence == "flagged"

        # Safe = confident match + no flags + staging file present.
        # Only standalone (non-album-batch) jobs go into the top-level "Approve N verified"
        # button. Album-batch jobs are handled per-batch by the "Approve N clean" button.
        if confidence == "verified" and not r.album_job_id:
            safe_ids.append(j.id)

        src = _source_summary(r, meta)
        item = {
            "job": j,
            "is_flagged": is_flagged,
            "flag_reason": flag_reason,
            "confidence": confidence,
            "src": src,
        }
        if r.album_job_id:
            album_buckets.setdefault(r.album_job_id, []).append(item)
        else:
            singles.append({"type": "single", "job": j, "confidence": confidence, "src": src})

    # Within an album batch, order tracks by review status so the approvable ones
    # cluster at the top and problems escalate toward the bottom:
    # verified → probable → flagged → fingerprint-mismatch. Stable sort preserves
    # the arrival (≈track) order within each rank.
    def _item_rank(it: dict) -> int:
        conf = it.get("confidence")
        if conf == "verified":
            return 0
        if conf == "probable":
            return 1
        reason = (it.get("flag_reason") or "").lower()
        return 3 if "fingerprint mismatch" in reason else 2

    groups: list[dict] = list(singles)
    for ajid, items in album_buckets.items():
        items.sort(key=_item_rank)
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
    from service.library.tagger import primary_artist, read_tags
    from service.core.models import TrackCandidate

    staging_path = Path(row.staging_path) if row.staging_path else None
    tagged = None
    if staging_path and staging_path.exists():
        tagged = await asyncio.to_thread(read_tags, staging_path)

    candidate: TrackCandidate | None = None
    if row.candidate_json:
        try:
            candidate = TrackCandidate.model_validate_json(row.candidate_json)
        except Exception as exc:
            logger.debug("unparseable candidate_json on job row: %s", exc)

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
        "albumartist": (tagged.albumartist if tagged else None) or primary_artist(artist),
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
    resp = templates.TemplateResponse(request, "partials/job_list.html", ctx)
    if not ctx["has_active_jobs"]:
        resp.headers["HX-Trigger"] = "stopJobPoll"
    return resp


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
    ctx: dict[str, object] = {"job": _job_to_model(row)}
    if row.state == "needs_review":
        meta = _row_resolved_meta(row)
        ctx["confidence"], _ = _classify_review_confidence(row, meta)
        ctx["src"] = _source_summary(row, meta)
    resp = templates.TemplateResponse(request, "partials/job_card.html", ctx)
    # Only active jobs self-poll this endpoint, so a non-active state here means the
    # job just transitioned. Tell the page to re-group it into the correct section
    # (Needs Review / Completed) immediately instead of waiting for the 12s poll.
    if row.state in ("needs_review", *_COMPLETED_STATES):
        resp.headers["HX-Trigger"] = "jobsChanged"
    return resp


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
    except Exception as exc:
        logger.debug("best-effort dequeue failed: %s", exc)

    row.state = "cancelled"
    row.error = "Cancelled by user"
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.flush()
    await session.commit()

    # The card was self-polling from the Active section; tell the page to
    # re-group so it drops into Completed instead of lingering up top.
    resp = templates.TemplateResponse(
        request, "partials/job_card.html", {"job": _job_to_model(row)}
    )
    resp.headers["HX-Trigger"] = "jobsChanged"
    return resp


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


# (monotonic timestamp, total) — the attention rollup runs several aggregate
# queries (splits/dupes group-bys scan every album), too heavy to recompute on
# every nav poll from every open tab.
_attention_cache: tuple[float, int] | None = None
_ATTENTION_TTL_S = 300.0


@router.get("/nav/attention-count", response_class=HTMLResponse)
async def nav_attention_count(
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Badge span rolling up every library item needing attention.

    Sum of the Library Health categories (low bitrate, missing covers, no MB
    ID, duplicates, split albums, artist-credit mismatches). needs_review jobs
    are deliberately NOT included — the Jobs badge next to it already shows
    them, and one item counted in two adjacent badges reads as two problems.
    """
    global _attention_cache
    now = time.monotonic()
    if _attention_cache is not None and now - _attention_cache[0] < _ATTENTION_TTL_S:
        total = _attention_cache[1]
    else:
        counts = await _library_attention_counts(session)
        total = sum(counts.values())
        _attention_cache = (now, total)

    poll = 'hx-get="/nav/attention-count" hx-trigger="every 120s" hx-swap="outerHTML"'
    if total:
        return HTMLResponse(
            f'<span class="nav-badge nav-badge-attention" title="{total} library item(s) need attention — see Library Health" {poll}>{total}</span>'
        )
    return HTMLResponse(f"<span {poll}></span>")


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

    # Full context (source link/description, autocomplete lists, batch label,
    # consistency warnings) — same builder every other review-card render uses.
    ctx = await _review_card_ctx(request, session, job_id, row, meta)
    return templates.TemplateResponse(request, "partials/review_card.html", ctx)


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
        except Exception as exc:
            logger.debug("candidate_json parse for track number failed: %s", exc)

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
                except Exception as exc:
                    logger.debug("sibling metadata parse failed: %s", exc)
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

    # Link to the actual media the audio came from so the user can validate the
    # pick at a glance (catches wrong-artist auto-picks). Prefer the canonical URL
    # captured at fetch time; fall back to provider_ref when it's already a real URL
    # (ghost/legacy rows) but never expose a bare `ytsearch1:` query.
    source_url = (meta.get("source_url") or "").strip()
    if not source_url:
        pr = (row.provider_ref or "").strip()
        if pr.startswith(("http://", "https://")):
            source_url = pr

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
        "source_url": source_url,
        "src": _source_summary(row, meta),
        "artist_names": artist_names,
        "album_names": album_names,
        "candidate_track_number": candidate_track_number,
    }


@router.get("/jobs/{job_id}/dest-preview", response_class=HTMLResponse)
async def job_dest_preview(
    request: Request,
    job_id: str,
    title: str | None = Query(None),
    artist: str | None = Query(None),
    album: str | None = Query(None),
    year: str | None = Query(None),
    track_number: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Read-only preview of where an approved track will land and how it will group.

    Computes the destination path via track_path() and the album-cohesion outcome
    via find_canonical_album()/stable_albumartist() — all pure / DB-read-only, no
    side effects. The review card hx-gets this on load and on metadata edits so the
    user sees the destination before approving.
    """
    from service.library.cohesion import find_canonical_album, stable_albumartist
    from service.library.layout import track_path

    row = await session.get(AcquisitionJobRow, job_id)
    if row is None or not row.resolved_metadata_json:
        return HTMLResponse("")
    try:
        meta = json.loads(row.resolved_metadata_json)
    except Exception as exc:
        logger.debug("corrupt resolved_metadata_json on job %s: %s", job_id, exc)
        return HTMLResponse("")

    # Enrichment edits a file already in /music — no move/grouping preview applies.
    if meta.get("is_enrichment"):
        return HTMLResponse("")

    def _int(v: str | None) -> int | None:
        try:
            return int((v or "").strip()) if (v or "").strip() else None
        except (ValueError, TypeError):
            return None

    # Form values (if supplied) override stored metadata; blank falls back to stored.
    eff_title = (title if title is not None else meta.get("title")) or "Unknown"
    eff_artist = (artist if artist is not None else meta.get("artist")) or "Unknown"
    eff_album = (album if album is not None else meta.get("album")) or None
    if eff_album is not None and not str(eff_album).strip():
        eff_album = None
    eff_year = _int(year) if year is not None else meta.get("year")
    eff_track = _int(track_number) if track_number is not None else meta.get("track_number")
    eff_disc = meta.get("disc_number")
    ext = meta.get("ext") or "ogg"

    from service.library.tagger import primary_artist as _primary_artist
    albumartist = meta.get("albumartist") or _primary_artist(eff_artist)
    mb_artist_id = meta.get("mb_artist_id") or None
    mb_release_group_id = meta.get("mb_release_group_id") or None

    # Album cohesion (read-only): does this join an existing album, and will the
    # albumartist be normalised to a locally-established name?
    joins_existing = False
    canonical_album: str | None = None
    normalised_aa: str | None = None
    if eff_album:
        stable_aa = await stable_albumartist(session, albumartist, mb_artist_id)
        if stable_aa != albumartist:
            normalised_aa = stable_aa
        albumartist = stable_aa
        canonical = await find_canonical_album(session, eff_album, albumartist, mb_release_group_id)
        if canonical is not None:
            joins_existing = True
            canonical_album, albumartist, c_year, _ = canonical
            if c_year is not None:
                eff_year = c_year
            eff_album = canonical_album

    dest = track_path(
        settings.music_dir,
        artist=eff_artist,
        album=eff_album,
        year=eff_year,
        track_number=eff_track,
        disc_number=eff_disc,
        title=eff_title,
        ext=ext,
        albumartist=albumartist,
    )

    return templates.TemplateResponse(
        request, "partials/dest_preview.html",
        {
            "dest": str(dest),
            "joins_existing": joins_existing,
            "canonical_album": canonical_album,
            "normalised_aa": normalised_aa,
            "is_single": eff_album is None,
        },
    )


@asynccontextmanager
async def _approval_redis() -> AsyncIterator[object | None]:
    """arq redis pool for per-job approval locks; yields None when Redis is
    unavailable (approvals then proceed unlocked, best-effort)."""
    pool = None
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    except Exception:
        pool = None
    try:
        yield pool
    finally:
        if pool is not None:
            try:
                await pool.aclose()
            except Exception as exc:
                logger.debug("redis pool close failed: %s", exc)


@asynccontextmanager
async def _approval_lock(redis, job_id: str) -> AsyncIterator[bool]:
    """Hold the per-job approval lock while the body runs.

    Guards against concurrent approvals of the same job (a double-clicked
    Approve button, or a batch racing a single approve): the winner moves the
    staging file and sets state=done, an unguarded loser would then hit
    "Staged file missing" and bounce the row back to needs_review. Yields False
    when another approval already holds the lock — the caller must skip.
    Redis being unavailable (or erroring) degrades to True: proceed unlocked.
    The lock is released on exit rather than left to the 60s expiry so a
    failed job can be re-approved immediately.
    """
    key = f"approve_lock:{job_id}"
    locked = False
    acquired = True
    if redis is not None:
        try:
            locked = bool(await redis.set(key, "1", ex=60, nx=True))
            acquired = locked
        except Exception:
            acquired = True
    try:
        yield acquired
    finally:
        if locked:
            try:
                await redis.delete(key)
            except Exception as exc:
                logger.debug("approval lock release failed: %s", exc)


async def _rollback_failed_approval(
    session: AsyncSession, job_id: str, exc: Exception
) -> None:
    """After place_approved_track raised: roll back the partial transaction and
    undo the intermediate "placing" state so the job returns to the review
    queue (with the error recorded) instead of being stuck mid-import."""
    try:
        await session.rollback()
        row = await session.get(AcquisitionJobRow, job_id)
        if row:
            if row.state in ("placing", "importing"):
                row.state = "needs_review"
            row.error = str(exc)[:200]
            await session.commit()
    except Exception:
        logger.debug("Rollback after failed approval of %s failed", job_id, exc_info=True)


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

    async def _noop_scan() -> None:
        # One Navidrome scan at the end of the batch instead of one per track.
        return None

    # Album identities of approved discography-batch tracks — healed for Navidrome
    # splits once the batch lands (a source-swap or year/MBID drift can fragment it).
    heal_targets: set[tuple[str, str]] = set()
    async with _approval_redis() as _redis:
        for jid in job_ids:
            async with _approval_lock(_redis, jid) as acquired:
                if not acquired:
                    # Another approval for this job is already in flight — skip it
                    # rather than racing (and clobbering) that placement.
                    continue
                try:
                    await place_approved_track(
                        jid, {}, session, scan_trigger=_noop_scan, mark_progress=True
                    )
                    await session.commit()
                    try:
                        _r = await session.get(AcquisitionJobRow, jid)
                        if _r and _r.album_job_id and _r.resolved_metadata_json:
                            _m = json.loads(_r.resolved_metadata_json)
                            _alb = (_m.get("album") or "").strip()
                            _aa = (_m.get("albumartist") or _m.get("artist") or "").strip()
                            if _alb and _aa:
                                heal_targets.add((_alb, _aa))
                    except Exception as exc:
                        logger.debug("heal-target metadata parse failed: %s", exc)
                    done_count += 1
                except Exception as exc:
                    logger.error("Batch approve failed for %s: %s", jid, exc)
                    await _rollback_failed_approval(session, jid, exc)
                    fail_count += 1

    # Auto-heal Navidrome album splits for any discography batch that just landed.
    scanned = False
    if heal_targets:
        from service.library.cohesion import auto_heal_album_splits
        healed = 0
        for _alb, _aa in heal_targets:
            try:
                healed += await auto_heal_album_splits(
                    session, _alb, _aa, settings.music_dir / ".trash", settings.music_dir
                )
                await session.commit()
            except Exception as exc:
                logger.warning("Auto-heal failed for %r / %r: %s", _alb, _aa, exc)
                try:
                    await session.rollback()
                except Exception as rb_exc:
                    logger.debug("rollback after auto-heal failure also failed: %s", rb_exc)
        if healed:
            await _do_scans()
            scanned = True

    # Single Navidrome scan for the whole batch (per-track scans were suppressed).
    if done_count and not scanned:
        await _do_scans()

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

    async with _approval_redis() as _redis:
        async with _approval_lock(_redis, job_id) as acquired:
            if not acquired:
                return HTMLResponse(
                    f'<div class="card card-review" id="job-{job_id}">'
                    f'<div class="rv-form"><div class="rv-alert rv-alert--error">'
                    f'Approval already in progress for this job.</div></div></div>'
                )
            try:
                await place_approved_track(job_id, overrides, session, mark_progress=True)
                await session.commit()
            except Exception as exc:
                logger.error("Approve job %s failed: %s", job_id, exc)
                await _rollback_failed_approval(session, job_id, exc)
                try:
                    row = await session.get(AcquisitionJobRow, job_id)
                    meta = json.loads(row.resolved_metadata_json) if row and row.resolved_metadata_json else {}
                    ctx = await _review_card_ctx(request, session, job_id, row, meta)
                    ctx["error"] = str(exc)
                    return templates.TemplateResponse(request, "partials/review_card.html", ctx)
                except Exception:
                    import html as _html
                    return HTMLResponse(
                        f'<div class="card card-review" id="job-{job_id}">'
                        f'<div class="rv-form"><div class="rv-alert rv-alert--error">'
                        f'Approve failed: {_html.escape(str(exc))}</div></div></div>'
                    )

    # ReplayGain already ran inside place_approved_track — no second ffmpeg pass.

    # Build a brief fade-out confirmation instead of showing the done card
    row = await session.get(AcquisitionJobRow, job_id)
    meta: dict = {}
    if row and row.resolved_metadata_json:
        try:
            meta = json.loads(row.resolved_metadata_json)
        except Exception as exc:
            logger.debug("resolved_metadata_json parse for confirmation banner failed: %s", exc)
    import html as _html
    # Title/album originate from free-text search or the downloaded file's own
    # tags — escape before interpolating into HTML.
    placed_title = meta.get("title") or (row.query if row else "") or "Track"
    placed_album = meta.get("album") or ""
    dest_hint = f" → {placed_album}" if placed_album else ""
    return HTMLResponse(
        f'<div id="job-{job_id}" class="job-placed-feedback">'
        f'✓ {_html.escape(placed_title + dest_hint)} · placed'
        f'</div>'
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
        except Exception as exc:
            logger.debug("is_enrichment probe failed: %s", exc)

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
                    except Exception as exc:
                        logger.debug("is_enrichment probe failed: %s", exc)
                if row.staging_path and not is_enrichment:
                    try:
                        p = Path(row.staging_path)
                        if p.exists():
                            safe_trash(p, settings.staging_dir / ".trash")
                    except Exception as exc:
                        logger.warning("trashing staged file on reject failed: %s", exc)
                row.state = "failed"
                row.failure_class = "permanent"
                row.error = "Rejected (bulk)"
                row.staging_path = None
            elif action == "cancel" and row.state in ("queued", "waiting", "downloading", "processing", "tagging", "importing"):
                try:
                    from arq import create_pool
                    from arq.connections import RedisSettings
                    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
                    await redis.zrem("arq:queue", f"acquire:{jid}")
                    await redis.aclose()
                except Exception as exc:
                    logger.debug("best-effort dequeue failed: %s", exc)
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
            except Exception as rb_exc:
                logger.debug("rollback after bulk-action failure also failed: %s", rb_exc)

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
        except Exception as exc:
            logger.debug("carrying resolved MB id into candidate_json failed: %s", exc)

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


async def _mb_recording_search(
    request: Request, q: str, limit: int, duration: int | None, **ctx: object
) -> HTMLResponse:
    """Shared MB recording search → mb_candidates.html partial.

    Used by both the review-card (job) and library-track edit-card search
    boxes; ctx carries the route-specific keys (job_id / track_id).
    """
    if not q.strip():
        return HTMLResponse("")
    from service.metadata.musicbrainz import search_recordings_free
    results = await asyncio.to_thread(
        search_recordings_free, q.strip(), limit, settings.cache_dir, duration
    )
    return templates.TemplateResponse(
        request, "partials/mb_candidates.html",
        {"results": results, "q": q.strip(), "limit": limit, "duration": duration, **ctx},
    )


@router.get("/jobs/{job_id}/mb-search", response_class=HTMLResponse)
async def job_mb_search(
    request: Request,
    job_id: str,
    q: str = "",
    limit: int = 10,
    duration: int | None = None,
) -> HTMLResponse:
    return await _mb_recording_search(request, q, limit, duration, job_id=job_id)


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
    from service.library.tagger import primary_artist as _primary_artist
    meta["title"] = mb.title or meta.get("title")
    meta["artist"] = mb.artist or meta.get("artist")
    # Sans featuring credit — "A feat. B" as ALBUMARTIST would split the album
    # into a separate featuring artist.
    meta["albumartist"] = _primary_artist(mb.artist) if mb.artist else meta.get("albumartist")
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


from service.providers.ytdlp import explicit_score as _explicit_score


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
    # Count albums/artists/genres by the tracks that actually exist (with a file),
    # not by raw Album/Artist row counts. Empty rows left behind by edits that move
    # the last track out of an album/artist would otherwise inflate these until a
    # manual Rescan ran the scanner's cascade cleanup — the recurring "counts don't
    # match reality" complaint. Counting through tracks-with-files keeps the overview
    # correct immediately, regardless of which mutation path forgot to prune.
    album_count = (await session.execute(
        select(func.count(func.distinct(Track.album_id)))
        .join(Track.file).where(Track.album_id.isnot(None))
    )).scalar_one()
    artist_count = (await session.execute(
        select(func.count(func.distinct(Track.artist_id))).join(Track.file)
    )).scalar_one()
    genre_count = (await session.execute(
        select(func.count(func.distinct(Track.genre)))
        .join(Track.file).where(Track.genre.isnot(None))
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
    quality_suppressed_count = (await session.execute(
        select(func.count(Track.id)).where(Track.quality_suppressed == 1)
    )).scalar_one()
    bitrate_suppressed_count = (await session.execute(
        select(func.count(Track.id)).where(Track.bitrate_suppressed == 1)
    )).scalar_one()
    return {
        "stats": {"tracks": track_count, "albums": album_count, "artists": artist_count, "genres": genre_count},
        "quality": {
            "no_mbid": no_mbid_count, "no_art": no_art_count,
            "low_quality": low_quality_count, "low_bitrate": low_bitrate_count, "dupes": dupe_count,
            "quality_suppressed": quality_suppressed_count, "bitrate_suppressed": bitrate_suppressed_count,
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
    """Ranked YouTube source pool for the review card's "Different source" panel.

    Auto-loads (no ``q``) with the track's own artist/title when the panel opens, or
    re-searches with a free-text ``q``. Either way the results are scored and sorted
    with the same logic the auto-picker used (`yt_search_ranked`), so the user can
    validate or swap the pick and see what the model had to choose from.
    """
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)

    meta: dict = {}
    if row.resolved_metadata_json:
        try:
            meta = json.loads(row.resolved_metadata_json)
        except Exception:
            meta = {}

    # What we *want* (drives scoring); fall back to the original candidate.
    want_artist = (meta.get("artist") or "").strip()
    want_title = (meta.get("title") or "").strip()
    want_dur = meta.get("duration_seconds")
    if not want_title and row.candidate_json:
        try:
            from service.core.models import TrackCandidate as _TC
            cand = _TC.model_validate_json(row.candidate_json)
            want_artist = want_artist or (cand.artist or "")
            want_title = want_title or (cand.title or "")
            want_dur = want_dur or cand.duration_seconds
        except Exception as exc:
            logger.debug("candidate_json parse for source-search hints failed: %s", exc)

    # The source currently in use — mark it so it isn't offered as a "swap to".
    current_url = (meta.get("source_url") or "").strip()
    if not current_url:
        pr = (row.provider_ref or "").strip()
        if pr.startswith(("http://", "https://")):
            current_url = pr

    query = q.strip() or f"{want_artist} {want_title}".strip()
    candidates: list[dict] = []
    if query:
        try:
            from service.providers.ytdlp import yt_search_ranked
            candidates = await asyncio.to_thread(
                yt_search_ranked, want_artist, want_title, want_dur,
                query=query, prefer_explicit=settings.prefer_explicit,
            )
        except Exception as exc:
            logger.warning("Source search failed: %s", exc)
    for c in candidates:
        c["is_current"] = bool(current_url) and c.get("provider_ref") == current_url

    return templates.TemplateResponse(
        request, "partials/source_replace_results.html",
        {"candidates": candidates, "q": q, "job_id": job_id,
         "source_codec": meta.get("source_codec"),
         "source_bitrate_kbps": meta.get("source_bitrate_kbps")},
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
        except Exception as exc:
            logger.warning("trashing previous staged file failed: %s", exc)
    # Preserve the ORIGINAL candidate's identity (locked recording ID, album,
    # track number, release group) and swap only the download pointer. Overwriting
    # candidate_json wholesale with the raw YouTube search result dropped the album
    # lock — re-acquired album tracks then lost their MB link and fragmented the
    # album (the "song I replaced lost its musicbrainz link" report). Only the
    # source pointer and its quality hints should change.
    from service.core.models import TrackCandidate as _TC
    base: _TC | None = None
    if row.candidate_json:
        try:
            base = _TC.model_validate_json(row.candidate_json)
        except Exception:
            base = None
    if base is not None:
        update: dict[str, object] = {"provider_ref": provider_ref}
        if candidate_json:
            try:
                picked = _TC.model_validate_json(candidate_json)
                update["thumbnail_url"] = picked.thumbnail_url
                update["raw_metadata"] = picked.raw_metadata
            except Exception as exc:
                logger.debug("carrying thumbnail/raw_metadata from picked source failed: %s", exc)
        row.candidate_json = base.model_copy(update=update).model_dump_json()
    elif candidate_json:
        # No original candidate to anchor to — fall back to the picked source.
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


@router.get("/jobs/{job_id}/fix-source", response_class=HTMLResponse)
async def fix_failed_source(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Manual intervention for a permanently failed job.

    A job that resolved to a dead/unavailable video will fail every retry against
    the same ``provider_ref``. This exposes the same source-replacement panel the
    review card uses, so the user can swap to a different YouTube version (or paste
    a URL) without losing the track's locked metadata. The panel POSTs to the
    existing ``/jobs/{id}/replace-source`` endpoint, which re-queues the download.
    """
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(404)

    meta: dict = {}
    if row.resolved_metadata_json:
        try:
            meta = json.loads(row.resolved_metadata_json)
        except Exception:
            meta = {}

    want_artist = (meta.get("artist") or "").strip()
    want_title = (meta.get("title") or "").strip()
    if row.candidate_json:
        try:
            from service.core.models import TrackCandidate as _TC
            cand = _TC.model_validate_json(row.candidate_json)
            want_artist = want_artist or (cand.artist or "")
            want_title = want_title or (cand.title or "")
        except Exception as exc:
            logger.debug("candidate_json parse for source-search hints failed: %s", exc)

    source_url = (meta.get("source_url") or "").strip()
    if not source_url:
        pr = (row.provider_ref or "").strip()
        if pr.startswith(("http://", "https://")):
            source_url = pr

    return templates.TemplateResponse(
        request, "partials/failed_source_card.html",
        {"job_id": job_id, "want_artist": want_artist, "want_title": want_title,
         "source_url": source_url, "error": row.error},
    )


def _layout_view(request: Request, view: str, cookie: str) -> str:
    """Resolve a list/grid layout preference: explicit ?view= wins, else the
    cookie set on every list render. Grid is the default for fresh visitors."""
    if view in ("list", "grid"):
        return view
    return "list" if request.cookies.get(cookie) == "list" else "grid"


@router.get("/library/albums", response_class=HTMLResponse)
async def library_albums_page(
    request: Request,
    q: str = "",
    sort: str = "",
    view: str = "",
    open_id: str = Query("", alias="open"),
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # `open` makes the drill-down bookmarkable: album rows hx-push-url this
    # page with ?open=<album id>, so refresh/back restores the open album.
    ctx = {"active": "library", "q": q, "sort": sort, "open_id": open_id,
           "view": _layout_view(request, view, "album_view")}
    tmpl = "partials/view_albums.html" if embed else "library_albums.html"
    return templates.TemplateResponse(request, tmpl, ctx)


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
    sort: str = "artist",
    view: str = "",
    open_id: str = Query("", alias="open"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _jl

    _album_sort_map = {
        "artist":  (Artist.sort_name, Artist.name, Album.year, Album.title),
        "title":   (Album.title, Artist.name),
        "year":    (Album.year.desc().nulls_last(), Album.title, Artist.name),
        "quality": None,  # handled in Python after fetch
    }
    sort_cols = _album_sort_map.get(sort) or _album_sort_map["artist"]

    stmt = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .limit(500)
    )
    if sort_cols:
        stmt = stmt.order_by(*sort_cols)

    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Album.title.ilike(pattern) | Artist.name.ilike(pattern))
    albums = (await session.execute(stmt)).unique().scalars().all()
    # Compute per-album quality from owned tracks (no extra query needed — tracks already loaded)
    album_quality: dict[str, float | None] = {}
    for alb in albums:
        scores = [t.tag_quality_score for t in alb.tracks if t.tag_quality_score is not None]
        album_quality[alb.id] = round(sum(scores) / len(scores), 3) if scores else None
    # Sort by quality in Python when requested (no SQL column for this)
    if sort == "quality":
        albums = sorted(albums, key=lambda a: album_quality.get(a.id) or 0.0)

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
    view = _layout_view(request, view, "album_view")
    tmpl = "partials/album_grid.html" if view == "grid" else "partials/album_list.html"
    resp = templates.TemplateResponse(
        request, tmpl,
        {"albums": albums, "q": q, "sort": sort, "album_quality": album_quality,
         "singles_count": singles_count, "singles_cover_id": singles_cover_id,
         "open_id": open_id, "view": view},
    )
    resp.set_cookie("album_view", view, max_age=365 * 24 * 3600, samesite="lax")
    return resp


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
    from service.library.cohesion import apply_album_tags

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    # Update DB, then rewrite album/albumartist/year + canonical MB album ID on every
    # track file so Navidrome groups them as one album.
    album.title = title.strip() or album.title
    album.year = int(year) if year.strip().isdigit() else album.year
    album.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await apply_album_tags(album)

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


@router.get("/library/albums/{album_id}/tracklist", response_class=HTMLResponse)
async def album_tracklist(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unified album tracklist.

    When the album is linked to MusicBrainz, the MB release tracklist is the
    backbone: each row is tagged ``here`` (owned in this album), ``elsewhere``
    (owned on another album), or ``missing``, and any local track absent from
    the MB list is appended as ``extra``. Unlinked albums degrade to a plain
    owned-track list. Replaces the old three-section layout (local list + full
    MB list + local list again) with one status-annotated list.
    """
    from sqlalchemy.orm import joinedload as _jl
    from service.search.matcher import title_similarity as _tsim

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file), _jl(Album.artist))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        return HTMLResponse('<p class="muted" style="font-size:12px">Album not found.</p>')

    local_tracks = sorted(
        album.tracks, key=lambda t: (t.track_number is None, t.track_number or 0)
    )
    linked = bool(album.mb_release_group_id or album.musicbrainz_release_id)

    def _local_only(note: str | None = None) -> HTMLResponse:
        # Owned tracks only — no MB status. Used for unlinked albums and as the
        # fallback when MusicBrainz is unavailable, so the user's tracks always show.
        return templates.TemplateResponse(
            request, "partials/album_tracklist.html",
            {"album": album, "local_only": True, "note": note},
        )

    # ── Unlinked: plain owned list, no MB backbone ────────────────────────────
    if not linked:
        return _local_only()

    # ── Linked: reconcile against the MB tracklist ────────────────────────────
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
        # MB down/unreachable — keep showing the owned tracks rather than an error.
        logger.warning("album_tracklist: MB fetch failed for %s: %s", album_id, exc)
        return _local_only("MusicBrainz unavailable — showing your tracks only.")

    if not mb_tracks:
        return _local_only("No MusicBrainz tracklist available — showing your tracks only.")

    # Map recording ID → local track, preferring a file-bearing row. Replacements
    # can leave a fileless ghost Track sharing the same recording ID; without this
    # the ghost could win the slot and bump the real (playable) file to "extra".
    local_by_rid: dict[str, Track] = {}
    for t in local_tracks:
        rid = t.musicbrainz_recording_id
        if not rid:
            continue
        cur = local_by_rid.get(rid)
        if cur is None or (t.file and not cur.file):
            local_by_rid[rid] = t
    local_rids = set(local_by_rid)

    # Recording IDs owned ANYWHERE in the library — those not in this album = "elsewhere"
    from service.library.cohesion import get_owned_recording_ids as _owned_rids
    mb_recording_ids = [t.recording_id for t in mb_tracks if t.recording_id]
    all_owned_rids = await _owned_rids(session, mb_recording_ids) if mb_recording_ids else set()
    elsewhere_rids = all_owned_rids - local_rids
    mb_rid_set = set(mb_recording_ids)
    mb_titles = [t.title for t in mb_tracks]

    rows: list[dict] = []
    matched_ids: set[str] = set()
    here = elsewhere = missing = 0

    for mt in mb_tracks:
        track = None
        status = None
        if mt.recording_id and mt.recording_id in local_by_rid:
            track = local_by_rid[mt.recording_id]
            status = "here"
        else:
            # Title match against not-yet-matched local tracks (recording ID absent).
            # Prefer a file-bearing candidate so a ghost doesn't claim the slot.
            unmatched = [lt for lt in local_tracks if lt.id not in matched_ids]
            unmatched.sort(key=lambda lt: lt.file is None)  # file-bearing first
            for lt in unmatched:
                if _tsim(mt.title, lt.title) >= 0.80:
                    track, status = lt, "here"
                    break
            if status is None:
                status = "elsewhere" if (mt.recording_id and mt.recording_id in elsewhere_rids) else "missing"

        owner_track_id = None
        if status == "here" and track is not None:
            matched_ids.add(track.id)
            here += 1
        elif status == "elsewhere":
            elsewhere += 1
            owner = (await session.execute(
                select(Track).where(Track.musicbrainz_recording_id == mt.recording_id).limit(1)
            )).scalar_one_or_none()
            owner_track_id = owner.id if owner else None
        else:
            missing += 1

        rows.append({
            "status": status,
            "number": mt.number,
            "disc": mt.disc,
            "title": (track.title if track is not None else mt.title),
            "track": track,
            "recording_id": mt.recording_id,
            "duration_seconds": mt.duration_seconds,
            "owner_track_id": owner_track_id,
        })

    # Genuinely-extra local tracks: unmatched AND not corresponding to any MB
    # track by recording ID or title. A duplicate/ghost of an MB track (same rid
    # or matching title) is NOT "not in MB" — skip it so it isn't mislabeled and
    # doesn't dump a stray track number at the bottom of the list.
    extra = 0
    for lt in local_tracks:
        if lt.id in matched_ids:
            continue
        rid_in_mb = bool(lt.musicbrainz_recording_id and lt.musicbrainz_recording_id in mb_rid_set)
        title_in_mb = any(_tsim(lt.title, mt) >= 0.80 for mt in mb_titles)
        if rid_in_mb or title_in_mb:
            continue  # duplicate of an MB track already shown above
        extra += 1
        rows.append({"status": "extra", "number": None, "disc": None, "title": lt.title, "track": lt})

    if not rows:
        return HTMLResponse('<p class="muted" style="font-size:12px">No tracks found.</p>')

    total = len(mb_tracks)
    # Persist the MB track count so the album list can show "N/total" without re-fetching
    if album.track_count != total:
        album.track_count = total
        await session.commit()

    return templates.TemplateResponse(
        request, "partials/album_tracklist.html",
        {"album": album, "rows": rows, "linked": True,
         "here": here, "elsewhere": elsewhere, "missing": missing,
         "extra": extra, "total": total,
         "artist_mbid": (album.artist.musicbrainz_artist_id
                         if album.artist and album.artist.musicbrainz_artist_id else "unknown"),
         "release_ref": album.mb_release_group_id or album.musicbrainz_release_id or album.id},
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

    # MB fields are external free text — the Jinja partial autoescapes them.
    return templates.TemplateResponse(
        request, "partials/mb_rg_search_results.html",
        {"results": results, "album_id": album_id},
    )


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
        return _error_badge("File not found on disk", level="fail")

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
        return _error_badge(f"Collision: {dst.name} already exists in target", level="fail")

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


@router.post("/library/albums/{album_id}/fix-discs", response_class=HTMLResponse)
async def album_fix_discs(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Repair disc/track numbers on a multi-disc album from its MB tracklist.

    Albums acquired before disc awareness existed had every disc flattened with
    per-disc positions and no DISCNUMBER tag — two "track 1" rows, two "track 2"
    rows, and so on. Matches owned tracks to the MB tracklist (recording ID
    first, title fallback), writes disc + track number tags, and renames files
    into the disc-aware layout (disc 2 track 1 → "201 - Title.ext").
    """
    from sqlalchemy.orm import joinedload as _jl
    from service.library.layout import track_path as _tp
    from service.library.tagger import write_tags as _wt
    from service.library.writer import atomic_place as _ap
    from service.search.matcher import title_similarity as _tsim

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file),
                 _jl(Album.tracks).joinedload(Track.artist))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)
    if not (album.mb_release_group_id or album.musicbrainz_release_id):
        return _error_badge("Not linked to MusicBrainz — link the album first")

    try:
        if album.mb_release_group_id:
            from service.metadata.musicbrainz import get_release_group_tracks as _get_tracks
            _, _, _, mb_tracks = await asyncio.to_thread(
                _get_tracks, album.mb_release_group_id, settings.cache_dir
            )
        else:
            from service.metadata.musicbrainz import get_release_tracks_by_id as _get_rel
            _, _, _, mb_tracks = await asyncio.to_thread(
                _get_rel, album.musicbrainz_release_id, settings.cache_dir
            )
    except Exception as exc:
        return _error_badge(f"MusicBrainz unavailable: {exc}")

    if not any(t.disc for t in mb_tracks):
        return HTMLResponse('<span class="badge badge-done">MusicBrainz lists a single disc — nothing to fix ✓</span>')

    # Match owned tracks to MB slots: recording ID first, then title similarity
    # among unclaimed slots (same fallback the tracklist reconciliation uses).
    by_rid = {t.recording_id: t for t in mb_tracks if t.recording_id}
    used: set[int] = set()
    matches: list[tuple[Track, object]] = []
    for track in album.tracks:
        rid = track.musicbrainz_recording_id
        mt = by_rid.get(rid) if rid else None
        if mt is not None and id(mt) not in used:
            used.add(id(mt))
            matches.append((track, mt))
    matched_ids = {t.id for t, _ in matches}
    for track in album.tracks:
        if track.id in matched_ids:
            continue
        best, best_s = None, 0.0
        for mt in mb_tracks:
            if id(mt) in used:
                continue
            s = _tsim(track.title, mt.title)
            if s > best_s:
                best_s, best = s, mt
        if best is not None and best_s >= 0.80:
            used.add(id(best))
            matches.append((track, best))

    if not matches:
        return _error_badge("No owned track matched the MB tracklist")

    albumartist = album.artist.name if album.artist else "Unknown"
    fixed = moved = 0
    for track, mt in matches:
        new_disc = mt.disc
        new_num = mt.number or track.track_number
        fp = Path(track.file.path) if track.file else None
        if fp is None or not fp.exists():
            track.disc_number = new_disc
            track.track_number = new_num
            continue
        try:
            await asyncio.to_thread(_wt, fp, track_number=new_num, disc_number=new_disc)
        except Exception as exc:
            logger.warning("fix-discs: tag write failed for %s: %s", fp, exc)
            continue
        if track.disc_number != new_disc or track.track_number != new_num:
            fixed += 1
        track.disc_number = new_disc
        track.track_number = new_num
        dst = _tp(
            settings.music_dir,
            artist=(track.artist.name if track.artist else albumartist),
            album=album.title,
            year=album.year,
            track_number=new_num,
            disc_number=new_disc,
            title=track.title,
            ext=fp.suffix.lstrip("."),
            albumartist=albumartist,
        )
        if dst != fp and not dst.exists():
            try:
                await asyncio.to_thread(_ap, fp, dst)
                # Keep the .lrc lyrics sidecar next to its audio file
                lrc = fp.with_suffix(".lrc")
                if lrc.exists():
                    try:
                        lrc.rename(dst.with_suffix(".lrc"))
                    except OSError:
                        pass
                track.file.path = str(dst)
                moved += 1
            except Exception as exc:
                logger.warning("fix-discs: rename failed %s → %s: %s", fp, dst, exc)

    await session.commit()
    await _do_scans()
    return HTMLResponse(
        f'<span class="badge badge-done">Disc numbers written to {fixed} track(s), '
        f'{moved} file(s) renamed ✓ — reopen the album to see discs</span>'
    )


@router.get("/library/stats-fragment", response_class=HTMLResponse)
async def library_stats_fragment(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats tiles fragment for OOB update after rescan."""
    stats_ctx = await _library_stats_context(session)
    inner = templates.get_template("partials/library_stats.html").render(stats_ctx)
    return HTMLResponse(f'<div id="library-stats" hx-swap-oob="true">{inner}</div>')


@router.get("/library/stats-poll", response_class=HTMLResponse)
async def library_stats_poll(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return inner stats content for periodic polling (no OOB wrapper)."""
    stats_ctx = await _library_stats_context(session)
    return templates.TemplateResponse(request, "partials/library_stats.html", stats_ctx)


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
        return _error_badge(f"Rescan failed: {exc}", level="fail")

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


def _resize_cover(art: bytes, size: int, dest: Path) -> bytes | None:
    """Downscale cover art to `size` px wide via ffmpeg and cache it at dest.

    ffmpeg is used instead of Pillow so no new dependency enters the image.
    Never upscales (min(size, iw)). Returns None on any failure — callers
    fall back to serving the full-size art.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", "pipe:0",
                "-vf", f"scale='min({size},iw)':-1",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1",
            ],
            input=art, capture_output=True, timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout:
            logger.debug(
                "cover thumbnail resize failed (rc=%s): %s",
                proc.returncode, (proc.stderr or b"")[-200:],
            )
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(proc.stdout)
        tmp.replace(dest)
        return proc.stdout
    except Exception as exc:
        logger.debug("cover thumbnail resize failed: %s", exc)
        return None


@router.get("/library/tracks/{internal_id}/cover-art")
async def track_cover_art(
    internal_id: str,
    size: int | None = Query(None, ge=32, le=512),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return cover art for a track: embedded first, then sidecar cover.jpg.

    ?size=N serves a disk-cached thumbnail (max-width N px) instead of the
    full embedded art — list rows and any future grid view must use it so a
    screenful of cells doesn't re-download full-size art per track. The cache
    entry is regenerated whenever the audio file or sidecar is newer than it;
    browsers revalidate after 10 min so replaced art propagates same-session.
    """
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

    thumb_headers = {"Cache-Control": "public, max-age=600"}
    thumb_path: Path | None = None
    if size is not None:
        thumb_path = settings.cache_dir / "thumbs" / f"{internal_id}_{size}.jpg"
        cover_jpg = path.parent / "cover.jpg"
        src_mtime = path.stat().st_mtime
        if cover_jpg.exists():
            src_mtime = max(src_mtime, cover_jpg.stat().st_mtime)
        if thumb_path.exists() and thumb_path.stat().st_mtime >= src_mtime:
            data = await asyncio.to_thread(thumb_path.read_bytes)
            return Resp(content=data, media_type="image/jpeg", headers=thumb_headers)

    art = await asyncio.to_thread(read_cover_art_bytes, path)

    if not art:
        # Fall back to sidecar cover.jpg in the same directory
        cover_jpg = path.parent / "cover.jpg"
        if cover_jpg.exists():
            art = await asyncio.to_thread(cover_jpg.read_bytes)

    if not art:
        raise HTTPException(404)

    if thumb_path is not None:
        data = await asyncio.to_thread(_resize_cover, art, size, thumb_path)
        if data:
            return Resp(content=data, media_type="image/jpeg", headers=thumb_headers)
        # resize failed — fall through to full-size art

    return Resp(content=art, media_type="image/jpeg",
                headers={"Cache-Control": "no-cache"})


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


async def _fetch_user_art(art_url: str) -> tuple[bytes | None, HTMLResponse | None]:
    """Download + size-validate user-picked cover art from a URL.

    Returns (art_bytes, None) on success or (None, error_badge_response) on
    failure — the shared front half of the job/track/album apply-art routes.
    """
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small, fetch_from_url

    art = await fetch_from_url(art_url)
    if not art:
        return None, _error_badge("Could not download image")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return None, _error_badge("Image too small (< 300×300)")
    return art, None


async def _embed_album_art(session: AsyncSession, album_id: str, art: bytes) -> int:
    """Embed art into every track file of an album + write the cover.jpg sidecar.

    Returns the number of files embedded; raises HTTPException(404) for an
    unknown album. Commits the session and triggers scans — shared back half
    of the album apply-art and album cover-upload routes.
    """
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags

    album = (await session.execute(
        select(Album)
        .options(joinedload(Album.tracks).joinedload(Track.file))
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
            logger.debug("album art embed failed for %s: %s", fp, exc)

    if album_dir:
        write_cover_jpg(album_dir, art)

    await session.commit()
    await _do_scans()
    return embedded


@router.post("/jobs/{job_id}/apply-art", response_class=HTMLResponse)
async def job_apply_art(
    request: Request,
    job_id: str,
    art_url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download art from a URL and embed it into the staging file for a review job."""
    from service.library.tagger import write_tags as _write_tags

    row = await session.get(AcquisitionJobRow, job_id)
    if not row or not row.staging_path:
        raise HTTPException(404)
    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        raise HTTPException(404, "Staging file not on disk")

    art, err = await _fetch_user_art(art_url)
    if err is not None:
        return err

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


@router.get("/library/artists/merge-candidates", response_class=HTMLResponse)
async def artist_merge_candidates(
    request: Request,
    canonical: str = "",
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return artist rows as merge-into-canonical candidates with action buttons."""
    if not q.strip():
        return HTMLResponse('<p class="muted" style="font-size:12px">Type to search…</p>')
    pattern = f"%{q.strip()}%"
    stmt = (
        select(Artist)
        .where(Artist.name.ilike(pattern))
        .where(Artist.id != canonical)
        .order_by(Artist.name)
        .limit(20)
    )
    artists = (await session.execute(stmt)).scalars().all()
    if not artists:
        return HTMLResponse('<p class="muted" style="font-size:12px">No matching artists.</p>')

    track_counts: dict[str, int] = {}
    for a in artists:
        cnt = (await session.execute(
            select(func.count()).select_from(Track).where(Track.artist_id == a.id)
        )).scalar_one()
        track_counts[a.id] = cnt

    lines = []
    for a in artists:
        cnt = track_counts[a.id]
        lines.append(
            f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--b1)">'
            f'<div style="flex:1;min-width:0">'
            f'<div style="font-size:13px;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{a.name}</div>'
            f'<div style="font-size:11px;color:var(--t3)">{cnt} track{"s" if cnt != 1 else ""}'
            + (f' · MB: {a.musicbrainz_artist_id[:8]}…' if a.musicbrainz_artist_id else '')
            + '</div>'
            f'</div>'
            f'<button class="btn btn-sm btn-ghost" style="white-space:nowrap"'
            f' hx-post="/library/artists/{canonical}/merge/{a.id}"'
            f' hx-target="#merge-artist-result"'
            f' hx-swap="innerHTML"'
            f' hx-confirm="Merge \'{a.name}\' into this artist? All their tracks and albums will be reassigned. This cannot be undone.">'
            f'Merge in ←</button>'
            f'</div>'
        )
    return HTMLResponse('<div style="margin-top:4px">' + ''.join(lines) + '</div>')


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
        .order_by(Album.year.nullslast(), Album.title.nullslast(), Track.disc_number.nullsfirst(), Track.track_number.nullslast(), Track.title)  # type: ignore[union-attr]
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


@router.post("/library/artists/{canonical_id}/merge/{source_id}", response_class=HTMLResponse)
async def merge_artist(
    request: Request,
    canonical_id: str,
    source_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Merge source artist into canonical.

    Thin route over :func:`service.library.cohesion.merge_artists`, which does
    the DB reassignment + tag rewrite + filesystem moves (mirrors merge_album →
    cohesion.merge_albums).
    """
    from service.library.cohesion import merge_artists as _merge_artists

    result = await _merge_artists(session, canonical_id, source_id, settings.music_dir)
    if result is None:
        raise HTTPException(404)
    await session.commit()

    await _do_scans()

    return HTMLResponse(
        "",
        status_code=200,
        headers={"HX-Redirect": f"/library/artists/{canonical_id}"},
    )


# Auto-fetched artist portraits (Navidrome-style external agent behaviour):
# throttle concurrent Deezer lookups and remember misses so a library page full
# of artists doesn't hammer the API on every render.
_artist_img_fetch_sem = asyncio.Semaphore(3)
_ARTIST_IMG_MISS_TTL_SECONDS = 7 * 24 * 3600


def _artist_img_cache_paths(name: str) -> tuple[Path, Path]:
    import hashlib
    key = hashlib.sha1(name.strip().lower().encode()).hexdigest()
    base = settings.cache_dir / "artist_images"
    return base / f"{key}.jpg", base / f"{key}.miss"


async def _auto_artist_image(name: str) -> Path | None:
    """Best-effort cached artist portrait from Deezer for artists with no artist.jpg.

    Navidrome shows artist images via its external agents even when no file
    exists on disk; this mirrors that so the audioreap library doesn't look
    emptier than Navidrome. Cached in /cache/artist_images (never in /music —
    the user's explicit "Change image" flow is what writes artist.jpg). Misses
    are cached with a TTL so absent artists are retried only weekly.
    """
    import time

    import httpx

    from service.search.matcher import artist_similarity

    jpg, miss = _artist_img_cache_paths(name)
    if jpg.exists():
        return jpg
    try:
        if miss.exists() and (time.time() - miss.stat().st_mtime) < _ARTIST_IMG_MISS_TTL_SECONDS:
            return None
    except OSError:
        pass

    async with _artist_img_fetch_sem:
        if jpg.exists():  # another request fetched it while we waited
            return jpg
        url: str | None = None
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://api.deezer.com/search/artist",
                    params={"q": name, "limit": 5},
                )
                resp.raise_for_status()
                for item in resp.json().get("data", []):
                    pic = item.get("picture_xl") or item.get("picture_big") or ""
                    if not pic or "default_artist" in pic:
                        continue
                    if artist_similarity(name, item.get("name") or "") >= 0.85:
                        url = pic
                        break
                if url:
                    img = await client.get(url)
                    img.raise_for_status()
                    jpg.parent.mkdir(parents=True, exist_ok=True)
                    tmp = jpg.with_suffix(".tmp")
                    tmp.write_bytes(img.content)
                    tmp.replace(jpg)
                    return jpg
        except Exception as exc:
            logger.debug("Auto artist image fetch failed for %r: %s", name, exc)
        try:
            jpg.parent.mkdir(parents=True, exist_ok=True)
            miss.touch()
        except OSError:
            pass
        return None


@router.get("/library/artists/{artist_id}/image", response_class=HTMLResponse)
async def artist_image(
    artist_id: str,
    size: int | None = Query(None, ge=32, le=512),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the artist's portrait: artist.jpg from /music when the user saved
    one, else an auto-fetched cached image (same sources Navidrome's agents use),
    else 204.

    204 (not 404) when nothing exists: the <img> tags that request this fall
    back via onerror either way, but a 2xx keeps the browser console clean.

    ?size=N serves a disk-cached thumbnail like the track cover-art route —
    the artist grid must use it so a screenful of cells doesn't re-download
    full portraits. Regenerated when the source image is newer than the thumb.
    """
    from fastapi.responses import FileResponse, Response
    artist = await session.get(Artist, artist_id)
    if artist is None:
        return Response(status_code=204)
    img_path = settings.music_dir / artist.name / "artist.jpg"
    src = img_path if img_path.exists() else await _auto_artist_image(artist.name)
    if src is None:
        return Response(status_code=204)
    if size is not None:
        thumb_headers = {"Cache-Control": "public, max-age=600"}
        thumb_path = settings.cache_dir / "thumbs" / f"artist_{artist_id}_{size}.jpg"
        if thumb_path.exists() and thumb_path.stat().st_mtime >= src.stat().st_mtime:
            data = await asyncio.to_thread(thumb_path.read_bytes)
            return Response(content=data, media_type="image/jpeg", headers=thumb_headers)
        art = await asyncio.to_thread(src.read_bytes)
        data = await asyncio.to_thread(_resize_cover, art, size, thumb_path)
        if data:
            return Response(content=data, media_type="image/jpeg", headers=thumb_headers)
        # resize failed — fall through to full-size art
    return FileResponse(str(src), media_type="image/jpeg")


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
        return _error_badge(f"MB search failed: {exc}")

    if not candidates:
        return HTMLResponse('<p class="muted" style="font-size:12px">No results.</p>')

    return templates.TemplateResponse(
        request, "partials/artist_mb_candidates.html",
        {"candidates": candidates, "input_id": f"artist-mb-id-{safe_id}"},
    )


_ARTIST_IMG_PAGE_SIZE = 10


@router.get("/library/artists/{artist_id}/image-search", response_class=HTMLResponse)
async def artist_image_search(
    request: Request,
    artist_id: str,
    q: str = "",
    offset: int = 0,
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
                params={"q": search_name, "limit": _ARTIST_IMG_PAGE_SIZE, "index": offset},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return _error_badge(f"Image search failed: {exc}")

    results = [
        {"name": item["name"], "image_url": item.get("picture_xl") or item.get("picture_medium", ""), "deezer_id": item["id"]}
        for item in data.get("data", [])
        if item.get("picture_medium") and "default_artist" not in item.get("picture_medium", "")
    ]
    has_more = len(results) >= _ARTIST_IMG_PAGE_SIZE
    return templates.TemplateResponse(
        request, "partials/artist_image_candidates.html",
        {
            "artist_id": artist_id,
            "safe_id": artist_id.replace(":", "_"),
            "results": results,
            "q": search_name,
            "offset": offset,
            "next_offset": offset + _ARTIST_IMG_PAGE_SIZE if has_more else None,
        },
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
        return _error_badge(f"Download failed: {exc}", level="fail")

    # Trigger Navidrome rescan so the new image is picked up
    await _do_scans()

    cache_bust = int(datetime.now(UTC).timestamp())
    return HTMLResponse(
        f'<img src="/library/artists/{artist_id}/image?v={cache_bust}" '
        f'style="width:80px;height:80px;object-fit:cover;border-radius:8px;display:block;margin-bottom:6px" '
        f'alt="{artist.name}">'
        f'<p style="font-size:12px;color:var(--success)">✓ Artist image saved — Navidrome rescan triggered.</p>'
    )


@router.post("/library/artists/{artist_id}/image/upload", response_class=HTMLResponse)
async def upload_artist_image(
    request: Request,
    artist_id: str,
    image: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save a user-uploaded portrait as artist.jpg in the artist's music folder."""
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small

    if not image.content_type or not image.content_type.startswith("image/"):
        return _error_badge("Not an image file", level="fail")
    art = await image.read()
    if not art:
        return _error_badge("Empty file", level="fail")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return _error_badge("Image too small — must be at least 300×300 px", level="fail")

    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)

    artist_dir = settings.music_dir / artist.name
    artist_dir.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread((artist_dir / "artist.jpg").write_bytes, art)
    except OSError as exc:
        return _error_badge(f"Save failed: {exc}", level="fail")

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
        return _error_badge(f"MB lookup failed: {exc}")

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
        return _error_badge(f"Queue error: {exc}")

    return HTMLResponse(
        f'<span class="badge-ok">Queued {len(unowned)} album{"s" if len(unowned) != 1 else ""} → <a href="/jobs">Jobs</a></span>'
    )


@router.get("/library/artists", response_class=HTMLResponse)
async def library_artists_page(
    request: Request,
    q: str = "",
    sort: str = "name",
    view: str = "",
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy import func as _func

    _artist_sort_map = {
        "name":   (Artist.sort_name, Artist.name),
        "tracks": (_func.count(Track.id.distinct()).desc(),),
        "albums": (_func.count(Album.id.distinct()).desc(), Artist.sort_name),
    }
    sort_cols = _artist_sort_map.get(sort) or _artist_sort_map["name"]

    stmt = (
        select(
            Artist,
            _func.count(Track.id.distinct()).label("track_count"),
            _func.count(Album.id.distinct()).label("album_count"),
        )
        .outerjoin(Artist.tracks)
        .outerjoin(Track.album)
        .group_by(Artist.id)
        .order_by(*sort_cols)
        .limit(500)
    )
    if q.strip():
        stmt = stmt.where(Artist.name.ilike(f"%{q.strip()}%"))
    rows = (await session.execute(stmt)).all()
    artists = [
        {"artist": r.Artist, "track_count": r.track_count, "album_count": r.album_count}
        for r in rows
    ]
    view = _layout_view(request, view, "artist_view")
    ctx = {"active": "library", "artists": artists, "q": q, "sort": sort, "view": view}
    # embed=1: full view content for in-place loading on the /library page.
    if embed:
        resp = templates.TemplateResponse(request, "partials/view_artists.html", ctx)
    # HTMX partial reload (search form / view toggle): only the list block.
    elif request.headers.get("HX-Request"):
        tmpl = "partials/artist_grid.html" if view == "grid" else "partials/artist_list.html"
        resp = templates.TemplateResponse(request, tmpl, ctx)
    else:
        resp = templates.TemplateResponse(request, "library_artists.html", ctx)
    resp.set_cookie("artist_view", view, max_age=365 * 24 * 3600, samesite="lax")
    return resp


@router.get("/library/genres", response_class=HTMLResponse)
async def library_genres_page(
    request: Request,
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
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
    ctx = {"active": "library", "genres": genres, "untagged_count": untagged_count}
    tmpl = "partials/view_genres.html" if embed else "library_genres.html"
    return templates.TemplateResponse(request, tmpl, ctx)


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
        return _error_badge("Missing genre name")

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
        return _error_badge("Missing genre name")
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
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unified library browser: search + quality review + metadata edit.

    embed=1 returns just the view content for in-place loading into the /library
    page; otherwise the full standalone page.
    """
    genre_list = (await session.execute(
        select(Track.genre).where(Track.genre.isnot(None)).distinct().order_by(Track.genre)
    )).scalars().all()
    ctx = {"active": "library", "q": q, "f": f, "sort": sort, "genre": genre, "genre_list": genre_list}
    tmpl = "partials/view_browse.html" if embed else "library_browse.html"
    return templates.TemplateResponse(request, tmpl, ctx)


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
        return _error_badge("No tracks selected")
    if genre_val is None and year_val is None:
        return _error_badge("Enter at least one field to update")

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


async def _suppression_response(
    request: Request,
    session: AsyncSession,
    row: Track,
    *,
    from_health: bool,
    from_edit: bool,
) -> HTMLResponse:
    """Render the appropriate partial after a (un)suppress toggle.

    from_health → empty (row drops out of the health list); from_edit → re-render
    the edit card so the user stays in it; otherwise the browse row.
    """
    if from_health:
        return HTMLResponse("")
    if from_edit:
        ctx = await _edit_card_ctx(session, row)
        return templates.TemplateResponse(request, "partials/track_edit_card.html", ctx)
    return templates.TemplateResponse(request, "partials/browse_row.html", {"t": row})


@router.post("/library/tracks/{internal_id}/suppress-quality", response_class=HTMLResponse)
async def track_suppress_quality(
    request: Request,
    internal_id: str,
    from_health: bool = Query(False),
    from_edit: bool = Query(False),
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
    return await _suppression_response(request, session, row, from_health=from_health, from_edit=from_edit)


@router.post("/library/tracks/{internal_id}/suppress-bitrate", response_class=HTMLResponse)
async def track_suppress_bitrate(
    request: Request,
    internal_id: str,
    from_health: bool = Query(False),
    from_edit: bool = Query(False),
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
    return await _suppression_response(request, session, row, from_health=from_health, from_edit=from_edit)


@router.post("/library/tracks/{internal_id}/unsuppress-bitrate", response_class=HTMLResponse)
async def track_unsuppress_bitrate(
    request: Request,
    internal_id: str,
    from_edit: bool = Query(False),
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
    return await _suppression_response(request, session, row, from_health=False, from_edit=from_edit)


@router.post("/library/tracks/{internal_id}/unsuppress-quality", response_class=HTMLResponse)
async def track_unsuppress_quality(
    request: Request,
    internal_id: str,
    from_edit: bool = Query(False),
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
    return await _suppression_response(request, session, row, from_health=False, from_edit=from_edit)


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


async def _edit_card_ctx(
    session: AsyncSession,
    row: Track,
    *,
    source_album_id: str = "",
    open_art: bool = False,
) -> dict:
    """Build the template context for track_edit_card.html.

    Shared by the edit-card route and the suppression handlers (which re-render
    the edit card so quality/bitrate-OK toggles keep the user in the card).
    """
    from sqlalchemy import distinct as _distinct

    # Use genre stored in DB (populated by scanner and save-tags); fall back to file.
    genre: str | None = row.genre
    if not genre and row.file:
        from service.library.tagger import read_tags as _read_tags
        fp = Path(row.file.path)
        if fp.exists():
            tagged = await asyncio.to_thread(_read_tags, fp)
            if tagged:
                genre = tagged.genre
    # Autocomplete datalists
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
    # Lyrics sidecar status for the badge (cheap: two stats + a small read)
    lyrics_status: str | None = None
    if row.file:
        from service.metadata.lyrics import has_lyrics_sidecar, sidecar_is_synced
        fp = Path(row.file.path)
        if has_lyrics_sidecar(fp):
            lyrics_status = "synced" if sidecar_is_synced(fp) else "plain"

    return {
        "track": row,
        "genre": genre,
        "genres": list(genres),
        "artist_names": list(artist_names),
        "album_names": list(album_names),
        "provider_ref": row.file.provider_ref if row.file else None,
        "bitrate_kbps": row.file.bitrate_kbps if row.file else None,
        "min_bitrate_kbps": settings.min_bitrate_kbps,
        "source_album_id": source_album_id,
        "open_art": open_art,
        "lyrics_status": lyrics_status,
    }


@router.get("/library/tracks/{internal_id}/edit-card", response_class=HTMLResponse)
async def track_edit_card(
    request: Request,
    internal_id: str,
    album_id: str = Query(""),
    open_art: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    ctx = await _edit_card_ctx(session, row, source_album_id=album_id, open_art=open_art)
    return templates.TemplateResponse(request, "partials/track_edit_card.html", ctx)


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
        # Do NOT update the DB when the file write failed — a partial "save" would
        # leave DB and on-disk tags silently disagreeing from then on.
        logger.warning("save-tags write failed for %s: %s", file_path, exc)
        import html as _html
        return HTMLResponse(
            f'<div style="color:var(--danger);font-size:12px;padding:6px 0">'
            f'✗ Tag write failed — nothing was saved: {_html.escape(str(exc))}</div>'
        )

    # Update DB — update existing rows in-place to avoid hash ID churn
    row.title = title_val
    row.track_number = track_num_val
    row.musicbrainz_recording_id = mbid_val
    row.genre = genre_val

    old_artist_id: str | None = None
    old_album_id: str | None = row.album_id
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

    # Prune the old album if the edit moved this track's last occupant out of it
    # (album-only change, where the old-artist block below wouldn't run). Leaving
    # the empty Album row behind is what inflated the library album count until a
    # manual Rescan.
    if old_album_id and old_album_id != row.album_id:
        remaining_album = (await session.execute(
            select(func.count(Track.id)).where(Track.album_id == old_album_id)
        )).scalar_one()
        if remaining_album == 0:
            await session.execute(sa_delete(Album).where(Album.id == old_album_id))
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


@router.post("/library/tracks/{internal_id}/save-tags/preview", response_class=HTMLResponse)
async def preview_track_tags(
    request: Request,
    internal_id: str,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    year: str = Form(""),
    track_number: str = Form(""),
    mb_recording_id: str = Form(""),
    genre: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Read-only before/after preview for a library metadata edit (dry-run).

    Mirrors save-tags' input parsing and DB regrouping logic but writes nothing.
    Classifies each changed field as safe (retag only) or structural (regroups
    artist/album in Navidrome) and surfaces the album-split consequence — only
    this track moves, siblings stay put. save-tags re-tags the file in place; it
    does not relocate it, so we say so when structure changes.
    """
    from service.library.tagger import read_tags as _read_tags

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)

    file_path = Path(row.file.path)

    # Current ("before") state
    cur_title = row.title
    cur_artist = row.artist.name
    cur_album = row.album.title if row.album else None
    cur_year = row.album.year if row.album and row.album.year else None
    cur_track = row.track_number
    cur_genre = row.genre
    if not cur_genre and file_path.exists():
        tagged = await asyncio.to_thread(_read_tags, file_path)
        if tagged:
            cur_genre = tagged.genre
    cur_mbid = row.musicbrainz_recording_id

    # Proposed ("after") state — same parsing as save-tags
    year_val: int | None = int(year) if year.strip().isdigit() else None
    track_num_val: int | None = int(track_number) if track_number.strip().isdigit() else None
    new_title = title.strip() or cur_title
    new_artist = artist.strip() or cur_artist
    new_album = album.strip() or None
    new_mbid = mb_recording_id.strip() or None
    new_genre = genre.strip() or None

    changes: list[dict] = []

    def _add(field: str, before: object, after: object, structural: bool) -> None:
        if str(before or "") != str(after or ""):
            changes.append({
                "field": field,
                "before": before if (before is not None and before != "") else "—",
                "after": after if (after is not None and after != "") else "—",
                "structural": structural,
            })

    _add("Title", cur_title, new_title, False)
    _add("Artist", cur_artist, new_artist, True)
    _add("Album", cur_album, new_album, True)
    _add("Year", cur_year, year_val, False)
    _add("Track #", cur_track, track_num_val, False)
    _add("Genre", cur_genre, new_genre, False)
    _add("MB Recording ID", cur_mbid, new_mbid, True)

    structural = any(c["structural"] for c in changes)
    warnings: list[str] = []
    notes: list[str] = []

    artist_changed = new_artist != cur_artist
    album_changed = (new_album or None) != (cur_album or None)

    if artist_changed:
        existing_artist = (await session.execute(
            select(Artist).where(Artist.name == new_artist)
        )).scalars().first()
        if existing_artist:
            notes.append(f"Artist “{new_artist}” already exists — track merges into it.")
        else:
            notes.append(f"New artist “{new_artist}” will be created.")

    if album_changed and row.album_id:
        siblings = (await session.execute(
            select(func.count(Track.id)).where(
                Track.album_id == row.album_id, Track.id != row.id
            )
        )).scalar_one()
        if siblings > 0:
            warnings.append(
                f"Only this track moves. {siblings} other track"
                f"{'s' if siblings != 1 else ''} stay in “{cur_album}”."
            )
    if album_changed:
        if new_album:
            notes.append(f"Track regroups under album “{new_album}”.")
        else:
            notes.append("Album cleared — track becomes a Single.")

    if structural:
        notes.append("The audio file is re-tagged in place — it is not moved to a new folder.")

    return templates.TemplateResponse(
        request, "partials/edit_preview.html",
        {
            "changes": changes,
            "structural": structural,
            "warnings": warnings,
            "notes": notes,
        },
    )


@router.get("/library/tracks/{internal_id}/mb-search", response_class=HTMLResponse)
async def library_track_mb_search(
    request: Request,
    internal_id: str,
    q: str = "",
    limit: int = 10,
    duration: int | None = None,
) -> HTMLResponse:
    return await _mb_recording_search(
        request, q, limit, duration, job_id=None, track_id=internal_id
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


@router.get("/library/quality")
async def quality_review_page() -> RedirectResponse:
    """Legacy quality-review page — superseded by Library Health, which covers the
    same data (low bitrate / missing art / missing files) with richer remediation."""
    return RedirectResponse("/library/health", status_code=301)


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
        .options(
            joinedload(Track.artist),
            joinedload(Track.album),
            joinedload(Track.file),
        )
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
        "replace_path": row.file.path if row.file else None,
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
        .options(
            joinedload(Track.artist),
            joinedload(Track.album),
            joinedload(Track.file),
        )
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
        replace_path=row.file.path if row.file else None,
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
        return _error_badge("No artwork found on Cover Art Archive")

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
        async with httpx.AsyncClient(timeout=10.0) as client:
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


async def _search_deezer_art(q: str, offset: int = 0) -> list[dict]:
    """Search Deezer for album artwork. Returns list of {url, label, source} dicts."""
    import urllib.parse
    results: list[dict] = []
    try:
        encoded = urllib.parse.quote(q)
        url = f"https://api.deezer.com/search/album?q={encoded}&limit=12&index={offset}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return results
            for item in resp.json().get("data", []):
                full_url = item.get("cover_xl") or item.get("cover_big", "")
                thumb_url = item.get("cover_medium") or item.get("cover_big", "")
                if not full_url:
                    continue
                artist = (item.get("artist") or {}).get("name", "")
                album = item.get("title", "")
                results.append({
                    "thumb": thumb_url,
                    "full": full_url,
                    "label": f"{artist} — {album}" if artist else album,
                    "source": "Deezer",
                })
    except Exception as exc:
        logger.debug("Deezer art search failed: %s", exc)
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
        except Exception as exc:
            logger.debug("CAA art probe failed: %s", exc)
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


_ART_PAGE_SIZE = 12


@router.get("/art/search", response_class=HTMLResponse)
async def art_search(
    request: Request,
    q: str = "",
    release_id: str = "",
    release_group_id: str = "",
    apply_url: str = "",
    result_target: str = "",
    offset: int = 0,
    page_key: str = "",
) -> HTMLResponse:
    """Return a thumbnail grid from iTunes + Deezer + CAA editions for the given query."""
    results: list[dict] = []
    first_page = offset == 0
    if q.strip():
        import asyncio as _asyncio
        itunes, deezer = await _asyncio.gather(
            _search_itunes_art(q.strip()),
            _search_deezer_art(q.strip(), offset),
        )
        results.extend(itunes)
        results.extend(deezer)
    # CAA results are release-specific — only fetch on first page
    if first_page:
        if release_id.strip():
            caa = await _search_caa_editions(release_id.strip())
            results.extend(caa)
        elif release_group_id.strip():
            caa = await _search_caa_by_rg(release_group_id.strip())
            results.extend(caa)

    if not results and first_page:
        return HTMLResponse('<p class="empty" style="font-size:12px;padding:8px 0">No results found.</p>')
    if not results:
        return HTMLResponse("")

    # Show load-more button if either iTunes or Deezer returned a full page
    has_more = len([r for r in results if r["source"] in ("iTunes", "Deezer")]) >= _ART_PAGE_SIZE
    next_offset = offset + _ART_PAGE_SIZE if has_more else None

    return templates.TemplateResponse(
        request, "partials/art_search_results.html",
        {
            "results": results,
            "apply_url": apply_url,
            "result_target": result_target,
            "next_offset": next_offset,
            "q": q,
            "release_id": release_id,
            "release_group_id": release_group_id,
            "page_key": page_key,
        },
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

    art, err = await _fetch_user_art(art_url)
    if err is not None:
        return err

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
    art, err = await _fetch_user_art(art_url)
    if err is not None:
        return err

    embedded = await _embed_album_art(session, album_id, art)
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
        return _error_badge("Not an image file")

    art = await cover.read()
    if not art:
        return _error_badge("Empty file")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return _error_badge("Image too small — must be at least 300×300 px")

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
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small

    if not cover.content_type or not cover.content_type.startswith("image/"):
        return _error_badge("Not an image file")

    art = await cover.read()
    if not art:
        return _error_badge("Empty file")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return _error_badge("Image too small — must be at least 300×300 px")

    embedded = await _embed_album_art(session, album_id, art)
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
    except Exception as exc:
        logger.debug("Navidrome ping failed: %s", exc)

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
    except Exception as exc:
        logger.debug("worker heartbeat probe failed: %s", exc)

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

    import httpx

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
from service.providers.ytdlp import yt_search_best as _yt_search_best_shared


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


# ── Discography ───────────────────────────────────────────────────────────

@router.get("/discography", response_class=HTMLResponse)
async def discography_page(
    request: Request, q: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, "", "discover", session)
    # ?q= prefills the artist search and runs it on load (artist-page deep link).
    ctx["disco_q"] = q.strip()
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
        return _error_badge(f"Error: {exc}")

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


@router.get("/discography/{artist_mbid}", response_class=HTMLResponse)
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


from service.library.tagger import read_mb_release_id as _read_mb_release_id


# ── Library Health / Management ───────────────────────────────────────────


async def _album_split_groups(session: AsyncSession) -> list[list[dict]]:
    """Albums split across multiple rows due to artist/title name variants.

    Each group is sorted most-tracks-first (canonical candidate first).
    """
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

    split_groups = [albums for albums in key_to_albums.values() if len(albums) > 1]
    for g in split_groups:
        g.sort(key=lambda a: a["ntracks"], reverse=True)
    return split_groups


async def _library_attention_counts(session: AsyncSession) -> dict[str, int]:
    """Per-category counts of library items needing attention.

    Shared by the Library Health page and the nav attention badge.
    """
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

    return {
        "dupes": dupe_count,
        "no_cover": no_cover_count,
        "no_mbid": no_mbid_count,
        "low_bitrate": low_bitrate_count,
        "splits": len(await _album_split_groups(session)),
        "artist_credits": len(await _artist_credit_mismatches(session)),
    }


@router.get("/library/health", response_class=HTMLResponse)
async def library_health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Library health overview — duplicates, split albums, missing covers."""
    global _attention_cache
    counts = await _library_attention_counts(session)
    # Fresh numbers were just computed — keep the nav badge consistent with
    # what this page shows instead of waiting out the TTL.
    _attention_cache = (time.monotonic(), sum(counts.values()))

    return templates.TemplateResponse(
        request, "library_health.html",
        {
            "active": "lib-health",
            "dupe_count": counts["dupes"],
            "no_cover_count": counts["no_cover"],
            "no_mbid_count": counts["no_mbid"],
            "low_bitrate_count": counts["low_bitrate"],
            "artist_credit_count": counts["artist_credits"],
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
    return templates.TemplateResponse(
        request, "partials/health_splits.html",
        {"groups": await _album_split_groups(session)},
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


@router.get("/library/health/low-quality", response_class=HTMLResponse)
async def library_health_low_quality(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks with low metadata quality score."""
    from service.metadata.quality import LOW_QUALITY_THRESHOLD
    from sqlalchemy.orm import joinedload as _jl

    _not_suppressed = (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0)
    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album))
        .where(
            Track.tag_quality_score.isnot(None),
            Track.tag_quality_score < LOW_QUALITY_THRESHOLD,
            _not_suppressed,
        )
        .order_by(Track.tag_quality_score.asc().nullslast())
        .limit(100)
    )).unique().scalars().all()

    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else None,
            "quality_score": t.tag_quality_score,
        }
        for t in rows
    ]
    return templates.TemplateResponse(
        request, "partials/health_low_quality.html",
        {"tracks": tracks},
    )


@router.get("/library/health/missing-files", response_class=HTMLResponse)
async def library_health_missing_files(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks indexed in the DB whose file is gone from disk."""
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.file)
        .order_by(Track.title)
    )).unique().scalars().all()

    def _find_missing() -> list[Track]:
        return [t for t in rows if t.file and not Path(t.file.path).exists()][:100]

    missing = await asyncio.to_thread(_find_missing)
    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else None,
            "provider_ref": t.file.provider_ref if t.file else None,
        }
        for t in missing
    ]
    return templates.TemplateResponse(
        request, "partials/health_missing_files.html",
        {"tracks": tracks},
    )


async def _artist_credit_mismatches(session: AsyncSession) -> list[Track]:
    """Tracks whose per-file ARTIST tag differs from the album artist.

    The scanner keys Artist rows on ALBUMARTIST, so these credits are invisible
    as artists in audioreap — but Subsonic clients read the ARTIST tag directly
    and surface them as separate artists (e.g. "Vitamin String Quartet" on a
    Ramin Djawadi album). Featuring credits ("Main feat. Guest") and
    compilations (Various Artists) are intentional and excluded.
    """
    from service.core.normalize import normalize as _norm
    from service.library.tagger import primary_artist as _primary_artist
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.artist)
        .where(
            Track.artist_credit.is_not(None),
            Track.album_id.is_not(None),
            Track.artist_credit != Artist.name,
        )
        .order_by(Artist.name, Track.title)
    )).unique().scalars().all()

    out: list[Track] = []
    for t in rows:
        credit = (t.artist_credit or "").strip()
        albumartist = (t.artist.name or "").strip() if t.artist else ""
        if not credit or not albumartist:
            continue
        if _norm(albumartist) == "various artists":
            continue  # compilation — per-track credits are the point
        if _norm(credit) == _norm(albumartist):
            continue  # case/punctuation-only difference
        if _norm(_primary_artist(credit)) == _norm(albumartist):
            continue  # "Main feat. Guest" under Main — by design
        out.append(t)
    return out


@router.get("/library/health/artist-credits", response_class=HTMLResponse)
async def library_health_artist_credits(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks whose ARTIST tag credit differs from the album artist."""
    mismatches = await _artist_credit_mismatches(session)
    credits_populated = (await session.execute(
        select(func.count(Track.id)).where(Track.artist_credit.is_not(None))
    )).scalar_one()
    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "credit": t.artist_credit,
            "albumartist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else None,
        }
        for t in mismatches
    ]
    return templates.TemplateResponse(
        request, "partials/health_artist_credits.html",
        {"tracks": tracks, "credits_populated": credits_populated},
    )


async def _fix_artist_credit(session: AsyncSession, track: Track) -> str | None:
    """Set the file's ARTIST tag to the album artist. Returns an error or None.

    Writes ONLY the ARTIST tag (never ALBUMARTIST — album grouping is already
    correct for these tracks) and mirrors the change into Track.artist_credit.
    """
    from service.library.tagger import write_tags as _write_tags

    if not track.file:
        return "no file"
    albumartist = track.artist.name if track.artist else None
    if not albumartist:
        return "no album artist"
    fp = Path(track.file.path)
    if not fp.exists():
        return "file missing on disk"
    try:
        await asyncio.to_thread(_write_tags, fp, artist=albumartist)
    except Exception as exc:  # mutagen failures are per-file, keep going
        return str(exc)
    track.artist_credit = albumartist
    track.updated_at = datetime.now(UTC).replace(tzinfo=None)
    try:
        track.file.file_mtime = fp.stat().st_mtime
    except OSError:
        pass
    return None


@router.post("/library/health/artist-credits/{internal_id}/fix", response_class=HTMLResponse)
async def fix_artist_credit(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """One-click: rewrite this track's ARTIST tag to the album artist."""
    track = await _get_track_with_file(session, internal_id)
    err = await _fix_artist_credit(session, track)
    if err:
        return HTMLResponse(
            f'<div class="card" style="padding:8px 14px"><span style="font-size:12px;color:var(--warn)">Failed: {err}</span></div>'
        )
    await session.commit()
    try:
        from service.navidrome.client import trigger_scan
        await trigger_scan()
    except Exception as exc:
        logger.debug("best-effort Navidrome scan trigger failed: %s", exc)
    return HTMLResponse("")  # row disappears from the list


@router.post("/library/health/artist-credits/fix-all", response_class=HTMLResponse)
async def fix_all_artist_credits(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Rewrite the ARTIST tag to the album artist for every mismatched track."""
    mismatches = await _artist_credit_mismatches(session)
    fixed, failed = 0, 0
    for t in mismatches:
        if await _fix_artist_credit(session, t) is None:
            fixed += 1
        else:
            failed += 1
    await session.commit()
    if fixed:
        try:
            from service.navidrome.client import trigger_scan
            await trigger_scan()
        except Exception as exc:
            logger.debug("best-effort Navidrome scan trigger failed: %s", exc)
    # Re-render the list (anything that failed stays visible)
    return await library_health_artist_credits(request, session)


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
        return _error_badge(f"Error: {exc}", level="fail")

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
    """Fetch cover art from Cover Art Archive and embed in all tracks + write cover.jpg."""
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
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
        except Exception as exc:
            logger.debug("release-group tracklist lookup for release id failed: %s", exc)

    if not release_id:
        return _error_badge("No MusicBrainz release ID — cannot fetch cover")
    if album_dir is None:
        return _error_badge("No files found for this album")

    art = await fetch_from_caa(release_id)
    if art is None:
        return _error_badge("Cover not found on Cover Art Archive")

    try:
        write_cover_jpg(album_dir, art)
    except Exception as exc:
        return _error_badge(f"Write failed: {exc}")

    # Embed art in every track file and update DB
    embedded = 0
    for track in album.tracks:
        if not track.file:
            continue
        fp = Path(track.file.path)
        if not fp.exists():
            continue
        try:
            await asyncio.to_thread(_write_tags, fp, artwork_bytes=art)
            track.file.has_cover_art = await asyncio.to_thread(_has_cover_art, fp)
            embedded += 1
        except Exception as exc:
            logger.debug("fetch_album_cover: embed failed for %s: %s", fp, exc)

    await session.commit()
    await _do_scans()

    return HTMLResponse(f'<span class="badge-ok">Cover saved to {embedded} track(s) ✓</span>')


@router.post("/library/albums/{canonical_id}/merge/{source_id}", response_class=HTMLResponse)
async def merge_album(
    request: Request,
    canonical_id: str,
    source_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Move source album files into canonical album folder and reassign all DB records.

    Thin route over :func:`service.library.cohesion.merge_albums`, which does the
    filesystem move + tag normalization + DB merge. Returns the refreshed album list.
    """
    from sqlalchemy.orm import joinedload as _jl

    from service.library.cohesion import merge_albums as _merge_albums

    await _merge_albums(
        session, canonical_id, source_id,
        settings.music_dir / ".trash", settings.music_dir,
    )
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
    view = _layout_view(request, "", "album_view")
    tmpl = "partials/album_grid.html" if view == "grid" else "partials/album_list.html"
    return templates.TemplateResponse(
        request, tmpl,
        {"albums": albums, "q": "", "album_quality": album_quality, "view": view},
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
                except Exception as exc:
                    logger.warning("reading restore_path sidecar failed: %s", exc)
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
    except Exception as exc:
        logger.warning("post-restore indexing failed (file restored, will appear on next scan): %s", exc)

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
        return _error_badge(f"Queue unavailable: {exc}")
    return HTMLResponse('<span class="badge-ok">Cover art fetch queued — check back in a few minutes</span>')


# ── Bulk ReplayGain backfill ───────────────────────────────────────────────────


@router.post("/library/health/backfill-replaygain", response_class=HTMLResponse)
async def backfill_replaygain_route(request: Request, full: bool = False, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Enqueue a background arq job to write ReplayGain tags across the whole library.

    full=True forces every file to be re-analyzed and retagged, even ones that
    already carry ReplayGain info — use after changing the target loudness.
    """
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("backfill_replaygain", full=full)
        await redis.aclose()
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    label = "Full ReplayGain retag" if full else "ReplayGain backfill"
    return HTMLResponse(f'<span class="badge-ok">{label} queued — check back in a few minutes</span>')


# ── Bulk lyrics fetch ─────────────────────────────────────────────────────────


@router.post("/library/health/fetch-missing-lyrics", response_class=HTMLResponse)
async def fetch_missing_lyrics(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Enqueue a background arq job to fetch LRCLIB lyrics for tracks missing them."""
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("fetch_missing_lyrics")
        await redis.aclose()
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    return HTMLResponse('<span class="badge-ok">Lyrics fetch queued — runs in the background (large libraries take a while)</span>')


@router.post("/library/health/upgrade-plain-lyrics", response_class=HTMLResponse)
async def upgrade_plain_lyrics(request: Request) -> HTMLResponse:
    """Enqueue a job that upgrades plain-text .lrc sidecars to synced when LRCLIB has one."""
    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("fetch_missing_lyrics", upgrade_plain=True)
        await redis.aclose()
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    return HTMLResponse('<span class="badge-ok">Synced-lyrics upgrade queued — re-checks plain tracks in the background</span>')


@router.post("/library/health/reset-lyrics-misses", response_class=HTMLResponse)
async def reset_lyrics_misses(request: Request) -> HTMLResponse:
    """Delete cached LRCLIB miss markers so previously-missed tracks are retried.

    A miss marker (``\\x00MISS``) is written when LRCLIB has no lyrics for a track,
    so the next backfill skips re-hitting the API. Clearing them forces a fresh
    lookup — useful after LRCLIB gains new lyrics, or to recover from any markers
    written before transient errors were excluded from caching. Real lyric files
    are left untouched.
    """
    from pathlib import Path

    lyrics_cache = settings.cache_dir / "lyrics"
    cleared = 0
    try:
        def _purge() -> int:
            n = 0
            if not lyrics_cache.is_dir():
                return 0
            for p in lyrics_cache.glob("*.lrc"):
                try:
                    if p.read_text(encoding="utf-8") == "\x00MISS":
                        p.unlink()
                        n += 1
                except OSError:
                    continue
            return n
        cleared = await asyncio.to_thread(_purge)
    except Exception as exc:
        return _error_badge(f"Reset failed: {exc}")
    return HTMLResponse(
        f'<span class="badge-ok">Cleared {cleared} cached miss marker'
        f'{"" if cleared == 1 else "s"} — run “Fetch all” to retry those tracks</span>'
    )


# ── Admin ─────────────────────────────────────────────────────────────────────


# ── Per-track lyrics (view / edit / fetch / delete the .lrc sidecar) ─────────


async def _get_track_with_file(session: AsyncSession, internal_id: str) -> Track:
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    return row


def _lyrics_panel_ctx(
    track: Track, *, message: str = "", message_kind: str = "ok", sync_open: bool = False,
) -> dict:
    from service.metadata.lyrics import lrc_sidecar_path, sidecar_is_synced

    status: str | None = None
    text = ""
    if track.file:
        audio = Path(track.file.path)
        lrc = lrc_sidecar_path(audio)
        try:
            if lrc.exists() and lrc.stat().st_size > 0:
                text = lrc.read_text(encoding="utf-8")
                status = "synced" if sidecar_is_synced(audio) else "plain"
        except OSError:
            pass
    return {
        "track": track,
        "safe_id": track.id.replace(":", "_"),
        "lyrics_status": status,
        "lyrics_text": text,
        "message": message,
        "message_kind": message_kind,
        "sync_open": sync_open,
    }


@router.get("/library/tracks/{internal_id}/stream")
async def stream_library_track(
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream a library track's audio — used by the lyrics sync preview player."""
    from fastapi.responses import FileResponse
    track = await _get_track_with_file(session, internal_id)
    if not track.file:
        raise HTTPException(404)
    path = Path(track.file.path)
    if not path.exists():
        raise HTTPException(404)
    ext = path.suffix.lower()
    media_map = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".flac": "audio/flac",
                 ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac"}
    return FileResponse(path, media_type=media_map.get(ext, "audio/ogg"))


@router.get("/library/tracks/{internal_id}/lyrics-panel", response_class=HTMLResponse)
async def track_lyrics_panel(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    track = await _get_track_with_file(session, internal_id)
    return templates.TemplateResponse(
        request, "partials/lyrics_panel.html", _lyrics_panel_ctx(track)
    )


@router.post("/library/tracks/{internal_id}/lyrics", response_class=HTMLResponse)
async def track_lyrics_action(
    request: Request,
    internal_id: str,
    action: str = Form("save"),
    lyrics: str = Form(""),
    offset: float = Form(0.0),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save, delete, (re)fetch, or time-shift the .lrc sidecar for one track."""
    from service.metadata.lyrics import fetch_lyrics, lrc_sidecar_path, shift_lrc, write_lrc_sidecar

    track = await _get_track_with_file(session, internal_id)
    if not track.file:
        raise HTTPException(400, "Track has no file")
    audio = Path(track.file.path)
    lrc = lrc_sidecar_path(audio)

    message, kind = "", "ok"
    if action == "delete":
        try:
            lrc.unlink(missing_ok=True)
            message = "Lyrics sidecar deleted."
        except OSError as exc:
            message, kind = f"Delete failed: {exc}", "warn"
    elif action == "fetch":
        # Bypass the disk cache (incl. miss markers) — this is an explicit
        # user request, so always ask LRCLIB fresh.
        result = await fetch_lyrics(
            artist=track.artist.name if track.artist else None,
            title=track.title,
            album=track.album.title if track.album else None,
            duration_seconds=track.duration_seconds,
            cache_dir=None,
        )
        if result is not None and result.instrumental:
            message, kind = "LRCLIB marks this track as instrumental — no lyrics to write.", "warn"
        elif result is not None and result.best:
            if write_lrc_sidecar(audio, result.best):
                message = "Fetched from LRCLIB" + (" (synced)." if result.synced else " (plain text).")
            else:
                message, kind = "Fetched, but writing the sidecar failed.", "warn"
        else:
            message, kind = "No lyrics found on LRCLIB for this track.", "warn"
    elif action == "offset":
        # Shift every timestamp in the submitted text (keeps unsaved edits) and save.
        text = shift_lrc(lyrics.replace("\r\n", "\n"), offset).strip()
        if not text:
            message, kind = "Nothing to shift — lyrics are empty.", "warn"
        elif abs(offset) < 0.001:
            message, kind = "Offset is 0 — nothing changed.", "warn"
        elif write_lrc_sidecar(audio, text + "\n"):
            direction = "later" if offset > 0 else "earlier"
            message = f"Timestamps shifted {abs(offset):.2f}s {direction} and saved."
        else:
            message, kind = "Shifted, but saving the sidecar failed.", "warn"
    else:  # save
        text = lyrics.replace("\r\n", "\n").strip()
        if not text:
            try:
                lrc.unlink(missing_ok=True)
                message = "Empty — lyrics sidecar removed."
            except OSError as exc:
                message, kind = f"Delete failed: {exc}", "warn"
        elif write_lrc_sidecar(audio, text + "\n"):
            message = "Lyrics saved."
        else:
            message, kind = "Saving the sidecar failed.", "warn"

    if kind == "ok":
        # Navidrome serves .lrc sidecars — nudge it so clients see the change.
        try:
            from service.navidrome.client import trigger_scan
            await trigger_scan()
        except Exception as exc:
            logger.debug("best-effort Navidrome scan trigger failed: %s", exc)

    return templates.TemplateResponse(
        request, "partials/lyrics_panel.html",
        _lyrics_panel_ctx(track, message=message, message_kind=kind,
                          sync_open=(action == "offset")),
    )


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
        return _error_badge(f"pip failed (exit {result.returncode}): {result.stderr[:200]}")
    except Exception as exc:
        return _error_badge(f"Update failed: {exc}")


def _admin_config_ctx(*, saved: bool = False) -> dict:
    from service.config import CONFIG_EDITABLE_KEYS
    from service.providers.ytdlp import active_cookies_file
    try:
        import yt_dlp
        ytdlp_version = yt_dlp.version.__version__
    except Exception:
        ytdlp_version = None
    return {
        "active": "settings",
        "current": {k: getattr(settings, k) for k in CONFIG_EDITABLE_KEYS},
        "cookies_active": active_cookies_file(),
        "ytdlp_version": ytdlp_version,
        "saved": saved,
    }


@router.get("/admin/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "admin_config.html", _admin_config_ctx())


@router.post("/admin/cookies", response_class=HTMLResponse)
async def admin_cookies_save(request: Request) -> HTMLResponse:
    """Save a UI-uploaded Netscape cookies.txt to the writable /data jar (or clear it).

    Lets the user paste/upload cookies exported from a logged-in browser window so
    age-gated downloads work without editing the :ro bind-mount. Read live by the
    worker at download time — no restart needed.
    """
    from service.providers.ytdlp import managed_cookies_path

    form = await request.form()
    managed = managed_cookies_path()

    if (form.get("action") or "save") == "clear":
        try:
            managed.unlink(missing_ok=True)
        except OSError as exc:
            return _error_badge(f"Clear failed: {exc}")
        return HTMLResponse('<span class="badge-ok">Cookies cleared — downloads run anonymously.</span>')

    # Content can come from a file input or a pasted textarea.
    content = ""
    upload = form.get("file")
    if upload is not None and hasattr(upload, "read"):
        raw = await upload.read()
        if raw:
            content = raw.decode("utf-8", errors="ignore")
    if not content.strip():
        content = str(form.get("cookies") or "")
    content = content.strip()
    if not content:
        return _error_badge("Nothing to save — paste a cookies.txt or choose a file.")

    def _is_cookie(ln: str) -> bool:
        return bool(ln.strip()) and not ln.strip().startswith("#") and "\t" in ln

    lines = content.splitlines()
    n = sum(1 for ln in lines if _is_cookie(ln))
    if n == 0:
        return _error_badge(
            "That doesn’t look like a Netscape cookies.txt (no tab-separated cookie "
            "lines). Export it with a “Get cookies.txt” browser extension on "
            "youtube.com and paste the whole file."
        )
    if not lines[0].startswith(("# Netscape", "# HTTP Cookie")):
        content = "# Netscape HTTP Cookie File\n" + content
    try:
        managed.parent.mkdir(parents=True, exist_ok=True)
        tmp = managed.with_name(managed.name + ".tmp")
        tmp.write_text(content + "\n", encoding="utf-8")
        tmp.replace(managed)
    except OSError as exc:
        return _error_badge(f"Save failed: {exc}")
    return HTMLResponse(
        f'<span class="badge-ok">Saved {n} cookie{"" if n == 1 else "s"} to /data/cookies.txt — '
        f'age-gated downloads will use them. No restart needed.</span>'
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
                except Exception as exc:
                    logger.debug("config override value not coercible, skipped: %s", exc)
    save_config_overrides(overrides)
    return templates.TemplateResponse(
        request, "admin_config.html", _admin_config_ctx(saved=True)
    )
