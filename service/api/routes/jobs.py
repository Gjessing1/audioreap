"""Acquisition job queue: status, review workflow, approval, source replacement."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from service.config import settings
from service.core.models import AcquisitionJob, TrackCandidate
from service.db.schema import AcquisitionJobRow, Album, Artist, Track
from service.library.writer import safe_trash
from service.db.session import get_session
from service.core.job_model import job_row_to_model as _job_to_model
from service.library.tagger import read_mb_release_id as _read_mb_release_id

from service.api.routes.artwork import _fetch_user_art
from service.acquisition.queue import arq_pool, enqueue_acquire_track
from service.api.shared import _ACTIVE_STATES_EXCLUDE, _COMPLETED_STATES, _JOBS_COMPLETED_PAGE, _do_scans, _mb_recording_search, templates

logger = logging.getLogger(__name__)
router = APIRouter()


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


def _enrichment_change_count(meta: dict) -> int | None:
    """How many fields an enrichment suggestion would change, or None if not
    an enrichment job. Mirrors the per-field "← was" conditions in
    review_card.html so the badge and the diff lines always agree."""
    if not meta.get("is_enrichment"):
        return None
    n = 0
    if meta.get("current_title") and meta.get("current_title") != meta.get("title"):
        n += 1
    if meta.get("current_artist") and meta.get("current_artist") != meta.get("artist"):
        n += 1
    if meta.get("current_album") is not None and meta.get("current_album") != meta.get("album"):
        n += 1
    if meta.get("current_year") is not None and meta.get("current_year") != meta.get("year"):
        n += 1
    if (meta.get("current_track_number") is not None
            and meta.get("current_track_number") != meta.get("track_number")):
        n += 1
    if meta.get("mb_recording_id") and meta.get("current_mb_recording_id") != meta.get("mb_recording_id"):
        n += 1
    return n


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
            singles.append({
                "type": "single", "job": j, "confidence": confidence, "src": src,
                "enrich_changes": _enrichment_change_count(meta),
            })

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
        async with arq_pool() as redis:
            await enqueue_acquire_track(
                redis, job_id,
                provider_name=row.provider,
                provider_ref=row.provider_ref,
                candidate_json=row.candidate_json,
                unique_retry=True,
            )
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
        async with arq_pool() as redis:
            await redis.zrem("arq:queue", f"acquire:{job_id}")
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


async def _review_autocomplete_names(
    session: AsyncSession, meta: dict
) -> tuple[list[str], list[str], list[str]]:
    """Datalist options for the review form: (genres, artist names, album names).

    Album names are scoped to the card's albumartist/artist when that artist is
    already in the library, falling back to all album titles."""
    from sqlalchemy import distinct as _distinct

    genre_rows = (await session.execute(
        select(_distinct(Track.genre)).where(Track.genre.isnot(None)).order_by(Track.genre)
    )).scalars().all()
    genres = [g for g in genre_rows if g]

    artist_name_rows = (await session.execute(
        select(_distinct(Artist.name)).order_by(Artist.name)
    )).scalars().all()
    artist_names = [n for n in artist_name_rows if n]

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
    return genres, artist_names, album_names


async def _album_batch_review_ctx(
    session: AsyncSession,
    job_id: str,
    row: AcquisitionJobRow,
    meta: dict,
    is_enrichment: bool,
) -> tuple[int | None, str | None, str | None]:
    """Album-batch extras for a review card:
    (expected track number from the original candidate, albumartist-consistency
    warning against sibling tracks still in review, batch label)."""
    if not row.album_job_id:
        return None, None, None

    candidate_track_number: int | None = None
    if row.candidate_json:
        try:
            from service.core.models import TrackCandidate as _TC
            candidate_track_number = _TC.model_validate_json(row.candidate_json).track_number
        except Exception as exc:
            logger.debug("candidate_json parse for track number failed: %s", exc)

    album_consistency_warning: str | None = None
    if not is_enrichment:
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

    album_batch_label: str | None = None
    from service.db.schema import AlbumAcquisitionJob as _AlbumJob
    album_job = await session.get(_AlbumJob, row.album_job_id)
    if album_job:
        parts = [p for p in [album_job.album_artist, album_job.album_title] if p]
        album_batch_label = " — ".join(parts) if parts else row.album_job_id[:8]

    return candidate_track_number, album_consistency_warning, album_batch_label


def _source_link(row: AcquisitionJobRow, meta: dict) -> str:
    """Link to the actual media the audio came from so the user can validate the
    pick at a glance (catches wrong-artist auto-picks). Prefer the canonical URL
    captured at fetch time; fall back to provider_ref when it's already a real URL
    (ghost/legacy rows) but never expose a bare `ytsearch1:` query."""
    source_url = (meta.get("source_url") or "").strip()
    if not source_url:
        pr = (row.provider_ref or "").strip()
        if pr.startswith(("http://", "https://")):
            source_url = pr
    return source_url


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

    genres, artist_names, album_names = await _review_autocomplete_names(session, meta)
    candidate_track_number, album_consistency_warning, album_batch_label = (
        await _album_batch_review_ctx(session, job_id, row, meta, is_enrichment)
    )

    from service.library.tagger import parse_artists as _parse_artists
    parsed_artists = _parse_artists(meta.get("artist") or "")
    show_multi_artists = len(parsed_artists) > 1

    source_url = _source_link(row, meta)

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
        "enrich_change_count": _enrichment_change_count(meta),
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
                    async with arq_pool() as redis:
                        await redis.zrem("arq:queue", f"acquire:{jid}")
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
        async with arq_pool() as redis:
            await enqueue_acquire_track(
                redis, job_id,
                provider_name=row.provider,
                provider_ref=row.provider_ref,
                candidate_json=candidate_json,
            )
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
            async with arq_pool() as redis:
                for row in rows:
                    await enqueue_acquire_track(
                        redis, row.id,
                        provider_name=row.provider,
                        provider_ref=row.provider_ref,
                        candidate_json=row.candidate_json,
                        unique_retry=True,
                    )
                    row.state = "queued"
                    row.failure_class = None
                    row.error = None
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
        async with arq_pool() as redis:
            await enqueue_acquire_track(
                redis, job_id,
                provider_name=row.provider,
                provider_ref=provider_ref,
                candidate_json=row.candidate_json or "{}",
            )
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
