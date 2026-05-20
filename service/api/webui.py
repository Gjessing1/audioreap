"""HTMX-rendered web UI routes."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.config import settings
from service.core.models import AcquisitionJob, TrackCandidate, TrackQuality, TrackRef
from service.db.schema import AcquisitionJobRow, Album, Artist, PlaylistImport, Track, TrackFile
from service.library.writer import safe_trash
from service.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _job_to_model(row: AcquisitionJobRow) -> AcquisitionJob:
    from service.main import _job_row_to_model
    return _job_row_to_model(row)


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
    return RedirectResponse("/search")


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        request, "search.html", {"active": "search", "q": q, "tracks": []}
    )


@router.get("/search/results", response_class=HTMLResponse)
async def search_results(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    tracks: list[TrackRef] = []
    if q:
        pattern = f"%{q}%"
        stmt = (
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .outerjoin(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(Track.title.ilike(pattern) | Artist.name.ilike(pattern))
            .order_by(Artist.name, Track.title)
            .limit(30)
        )
        rows = (await session.execute(stmt)).unique().scalars().all()
        tracks = [_track_to_ref(r) for r in rows]

    return templates.TemplateResponse(
        request, "partials/local_results.html", {"tracks": tracks, "q": q}
    )




def _grouped_jobs(rows: list[AcquisitionJobRow]) -> dict[str, list]:
    """Split job rows into review / active / completed groups for the UI."""
    review, active, completed = [], [], []
    for r in rows:
        j = _job_to_model(r)
        if r.state in ("needs_review", "staged"):
            review.append(j)
        elif r.state in ("done", "failed", "cancelled"):
            completed.append(j)
        else:
            active.append(j)
    return {"review": review, "active": active, "completed": completed}


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
    }


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(100)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "jobs.html", {"active": "jobs", **_grouped_jobs(rows)}
    )


@router.get("/jobs/list", response_class=HTMLResponse)
async def jobs_list_partial(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(100)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "partials/job_list.html", _grouped_jobs(rows)
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
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=row.provider,
            provider_ref=row.provider_ref,
            candidate_json=row.candidate_json,
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
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
    if row.state in ("done", "failed", "cancelled"):
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
    elif row.state in ("needs_review", "staged") and row.staging_path:
        # Staged item from before Phase 13 — synthesize metadata and promote
        meta = await _synthesize_review_meta(row)
        row.resolved_metadata_json = json.dumps(meta)
        row.state = "needs_review"
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
    else:
        raise HTTPException(400, "Job not reviewable")

    return templates.TemplateResponse(
        request, "partials/review_card.html",
        {"job_id": job_id, "meta": meta, "query": row.query or ""},
    )


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
            row = await session.get(AcquisitionJobRow, jid)
            if row:
                row.error = str(exc)[:200]
                await session.commit()
            fail_count += 1

    rows = (
        await session.execute(
            select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(100)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "partials/job_list.html", _grouped_jobs(rows)
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
    }

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
            return templates.TemplateResponse(
                request, "partials/review_card.html",
                {
                    "job_id": job_id, "meta": meta,
                    "query": row.query if row else "",
                    "error": str(exc),
                },
            )
        except Exception:
            return HTMLResponse(
                f'<div class="card card-review" id="job-{job_id}">'
                f'<div class="rv-form"><div class="rv-alert rv-alert--error">Approve failed: {exc}</div></div></div>'
            )

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

    if row.staging_path:
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


@router.get("/jobs/{job_id}/mb-search", response_class=HTMLResponse)
async def job_mb_search(
    request: Request,
    job_id: str,
    q: str = "",
) -> HTMLResponse:
    if not q.strip():
        return HTMLResponse("")
    from service.metadata.musicbrainz import search_recordings_free
    results = await asyncio.to_thread(
        search_recordings_free, q.strip(), 6, settings.cache_dir
    )
    return templates.TemplateResponse(
        request, "partials/mb_candidates.html",
        {"results": results, "job_id": job_id},
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

    return templates.TemplateResponse(
        request, "partials/review_card.html",
        {"job_id": job_id, "meta": meta, "query": row.query or "", "show_mb_search": True},
    )


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
    if row is not None and row.state in ("done", "failed", "cancelled"):
        await session.delete(row)
        await session.commit()
    return HTMLResponse("")


@router.delete("/jobs/clear", response_class=HTMLResponse)
async def clear_done_jobs(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    await session.execute(
        sa_delete(AcquisitionJobRow).where(
            AcquisitionJobRow.state.in_(["done", "failed", "cancelled"])
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
        .options(_joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        return HTMLResponse("")

    if row.file:
        file_path = Path(row.file.path)
        if file_path.exists():
            try:
                safe_trash(file_path, settings.music_dir / ".trash")
            except Exception as exc:
                logger.warning("Trash move failed for %s: %s", file_path, exc)
        await session.delete(row.file)

    await session.delete(row)
    await session.commit()
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

            # Sort: explicit first, clean last; stable so original order wins ties
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


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    # Count only tracks that have actual files on disk (TrackFile rows)
    track_count = (await session.execute(select(func.count(TrackFile.id)))).scalar_one()
    album_count = (await session.execute(select(func.count(Album.id)))).scalar_one()
    artist_count = (await session.execute(select(func.count(Artist.id)))).scalar_one()

    # Quality stats — only count tracks that have an actual file on disk
    no_mbid_count = (
        await session.execute(
            select(func.count(Track.id))
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.is_(None))
        )
    ).scalar_one()
    no_art_count = (
        await session.execute(
            select(func.count(TrackFile.id)).where(
                (TrackFile.has_cover_art.is_(None)) | (TrackFile.has_cover_art == 0)
            )
        )
    ).scalar_one()
    low_quality_count = (
        await session.execute(
            select(func.count(Track.id))
            .join(Track.file)
            .where(
                (Track.tag_quality_score.isnot(None))
                & (Track.tag_quality_score < LOW_QUALITY_THRESHOLD)
            )
        )
    ).scalar_one()

    # Low-quality tracks to surface (with file, worst first)
    low_quality_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(
                Track.tag_quality_score.isnot(None),
                Track.tag_quality_score < LOW_QUALITY_THRESHOLD,
            )
            .order_by(Track.tag_quality_score.asc())
            .limit(30)
        )
    ).unique().scalars().all()

    low_quality = []
    for row in low_quality_rows:
        low_quality.append({
            "internal_id": row.id,
            "title": row.title,
            "artist": row.artist.name,
            "album": row.album.title if row.album else None,
            "quality_score": row.tag_quality_score,
            "has_mbid": bool(row.musicbrainz_recording_id),
            "has_art": bool(row.file and row.file.has_cover_art),
        })

    recent_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)  # inner join — only tracks with actual files
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

    return templates.TemplateResponse(
        request, "library.html",
        {
            "active": "library",
            "stats": {"tracks": track_count, "albums": album_count, "artists": artist_count},
            "quality": {
                "no_mbid": no_mbid_count,
                "no_art": no_art_count,
                "low_quality": low_quality_count,
            },
            "low_quality_tracks": low_quality,
            "recent": [_track_to_ref(r) for r in recent_rows],
            "settings_music_dir": str(settings.music_dir),
            "needs_review_count": needs_review_count,
        },
    )


@router.post("/library/rescan", response_class=HTMLResponse)
async def library_rescan(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Full rescan of /music: adds new files, removes missing ones from DB."""
    from service.index.scanner import scan
    from service.navidrome.client import trigger_scan

    try:
        result = await scan(session, settings.music_dir, incremental=False)
        await session.commit()
    except Exception as exc:
        logger.error("Library rescan failed: %s", exc)
        return HTMLResponse(f'<span class="badge badge-fail">Rescan failed: {exc}</span>')

    try:
        await trigger_scan()
    except Exception:
        pass

    return HTMLResponse(
        f'<span class="badge badge-done">'
        f'Rescan done — {result.added} added, {result.removed} removed, {result.updated} updated'
        f'</span>'
    )


@router.get("/library/browse", response_class=HTMLResponse)
async def library_browse(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Browse and edit all tracks in the library."""
    return templates.TemplateResponse(
        request, "library_browse.html",
        {"active": "library", "q": q},
    )


@router.get("/library/browse/results", response_class=HTMLResponse)
async def library_browse_results(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    stmt = (
        select(Track)
        .join(Track.artist)
        .outerjoin(Track.album)
        .join(Track.file)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .order_by(Artist.name, Track.title)
        .limit(200)
    )
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Track.title.ilike(pattern) | Artist.name.ilike(pattern))
    rows = (await session.execute(stmt)).unique().scalars().all()
    return templates.TemplateResponse(
        request, "partials/browse_results.html",
        {"tracks": rows, "q": q},
    )


@router.get("/library/tracks/{internal_id}/edit-card", response_class=HTMLResponse)
async def track_edit_card(
    request: Request,
    internal_id: str,
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
    return templates.TemplateResponse(
        request, "partials/track_edit_card.html",
        {"track": row},
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
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.library.tagger import write_tags as _write_tags, has_cover_art as _has_cover_art
    from service.metadata.quality import compute_quality_score
    from service.index.scanner import _upsert_artist, _upsert_album
    from service.navidrome.client import trigger_scan

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
        )
    except Exception as exc:
        logger.warning("save-tags write failed for %s: %s", file_path, exc)

    # Update DB — update existing rows in-place to avoid hash ID churn
    row.title = title_val
    row.track_number = track_num_val
    row.musicbrainz_recording_id = mbid_val

    if artist_val != row.artist.name:
        new_artist_id = await _upsert_artist(session, artist_val)
        row.artist_id = new_artist_id

    if album_val and (not row.album or album_val != row.album.title):
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

    try:
        await trigger_scan()
    except Exception:
        pass

    # Reload fresh row
    stmt2 = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    updated = (await session.execute(stmt2)).unique().scalar_one_or_none()
    return templates.TemplateResponse(
        request, "partials/track_edit_card.html",
        {"track": updated, "saved": True},
    )


@router.get("/library/tracks/{internal_id}/mb-search", response_class=HTMLResponse)
async def library_track_mb_search(
    request: Request,
    internal_id: str,
    q: str = "",
) -> HTMLResponse:
    """Inline MB search for the library editor — reuses the same candidates partial
    but targets the library editor's result div instead of job's."""
    if not q.strip():
        return HTMLResponse("")
    from service.metadata.musicbrainz import search_recordings_free
    results = await asyncio.to_thread(
        search_recordings_free, q.strip(), 6, settings.cache_dir
    )
    return templates.TemplateResponse(
        request, "partials/mb_candidates.html",
        {"results": results, "job_id": None, "track_id": internal_id},
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
        f'<div id="qtrack-{internal_id}" class="card" style="opacity:0.6">'
        f'<div class="card-info">'
        f'<div class="card-title">{row.title}</div>'
        f'<div class="card-sub">{row.artist.name} · Re-tagged from MusicBrainz · Quality {pct}%</div>'
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
            f'<div id="art-{internal_id}" class="card-sub" style="color:var(--warn)">'
            f"No artwork found in Cover Art Archive for this track.</div>"
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

    return HTMLResponse(
        f'<div id="art-{internal_id}" class="badge badge-done">Art embedded ✓</div>'
    )


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
    try:
        import redis.asyncio as aioredis
        rc = aioredis.from_url(settings.redis_url)
        await rc.ping()
        await rc.aclose()
        redis_ok = True
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
    rows = (
        await session.execute(
            select(PlaylistImport).order_by(PlaylistImport.created_at.desc()).limit(20)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "playlists.html",
        {
            "active": "playlists",
            "imports": rows,
            "spotify_enabled": bool(settings.spotify_client_id),
        },
    )


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
        if not settings.spotify_client_id:
            return templates.TemplateResponse(
                request, "partials/playlist_preview.html",
                {
                    "error": (
                        "Spotify playlist support requires AUDIOREAP_SPOTIFY_CLIENT_ID and "
                        "AUDIOREAP_SPOTIFY_CLIENT_SECRET environment variables."
                    )
                },
            )
        title, source, candidates = await _resolve_spotify_playlist(url)
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

    # Dedup check against local library
    track_statuses: list[dict[str, object]] = []
    for candidate in candidates:
        internal_id = make_id(candidate.artist, candidate.title, candidate.duration_seconds)
        stmt = (
            select(Track)
            .options(joinedload(Track.file))
            .where(Track.id == internal_id)
        )
        row = (await session.execute(stmt)).unique().scalar_one_or_none()
        owned = row is not None and row.file is not None
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
    """Resolve a Spotify playlist via the Spotify Web API + YouTube search fallback."""
    import re as _re

    match = _re.search(r"playlist/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("Could not extract Spotify playlist ID from URL")
    playlist_id = match.group(1)

    token = await _spotify_client_token()

    import httpx
    async with httpx.AsyncClient(timeout=30.0) as client:
        items: list[dict[str, object]] = []
        next_url: str | None = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks?limit=50"
        pl_title = "Spotify Playlist"

        # Fetch playlist name
        r = await client.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        r.raise_for_status()
        pl_title = str(r.json().get("name") or pl_title)

        while next_url:
            r = await client.get(next_url, headers={"Authorization": f"Bearer {token}"})
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

        # Use yt-dlp YouTube search to get a provider_ref
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


# ── Discography ───────────────────────────────────────────────────────────

@router.get("/discography", response_class=HTMLResponse)
async def discography_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "discography.html", {"active": "discography"}
    )


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
    owned_recording_ids: set[str] = set()
    if tracks:
        rids = [t.recording_id for t in tracks if t.recording_id]
        if rids:
            rows = (await session.execute(
                select(Track).where(Track.musicbrainz_recording_id.in_(rids))
            )).scalars().all()
            owned_recording_ids = {r.musicbrainz_recording_id for r in rows if r.musicbrainz_recording_id}

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
        },
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

    msg = "Album queued"
    return HTMLResponse(f'<span class="badge-ok">{msg} — <a href="/jobs">View jobs</a></span>')


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

    selected_types = set(types) or {"Album", "EP", "Single"}

    artist_name, release_groups = await asyncio.to_thread(
        get_artist_release_groups, artist_mbid, settings.cache_dir
    )

    filtered = [rg for rg in release_groups if rg.release_type in selected_types]

    # Find artist in local DB by fuzzy name match
    local_album_titles: set[str] = set()
    all_local_artists = (
        await session.execute(
            select(Artist).where(Artist.name.ilike(f"%{artist_name.split()[0]}%"))
        )
    ).scalars().all()
    for la in all_local_artists:
        if title_similarity(la.name, artist_name) >= 0.85:
            local_albums = (
                await session.execute(select(Album).where(Album.artist_id == la.id))
            ).scalars().all()
            local_album_titles = {_normalize(a.title) for a in local_albums}
            break

    release_entries = []
    for rg in filtered:
        normalized_title = _normalize(rg.title)
        owned = any(
            title_similarity(normalized_title, local_t) >= 0.80
            for local_t in local_album_titles
        )
        release_entries.append({
            "release_group_id": rg.release_group_id,
            "title": rg.title,
            "year": rg.year,
            "release_type": rg.release_type,
            "owned": owned,
        })

    owned_count = sum(1 for r in release_entries if r["owned"])
    all_types = sorted({rg.release_type for rg in release_groups})

    return templates.TemplateResponse(
        request, "discography.html",
        {
            "active": "discography",
            "artist_name": artist_name,
            "artist_mbid": artist_mbid,
            "releases": release_entries,
            "owned_count": owned_count,
            "total_count": len(release_entries),
            "all_types": all_types,
            "selected_types": selected_types,
        },
    )


# ── Staging ───────────────────────────────────────────────────────────────────

@router.get("/staging", response_class=HTMLResponse)
async def staging_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(AcquisitionJobRow)
            .where(AcquisitionJobRow.state == "staged")
            .order_by(AcquisitionJobRow.updated_at.desc())
        )
    ).scalars().all()
    needs_review_count = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state == "needs_review")
        )
    ).scalar_one()
    return templates.TemplateResponse(
        request, "staging.html",
        {
            "active": "staging",
            "staged": rows,
            "threshold": settings.staging_quality_threshold,
            "needs_review_count": needs_review_count,
        },
    )


@router.post("/staging/{job_id}/approve", response_class=HTMLResponse)
async def staging_approve(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Move a staged track into /music and index it."""
    import shutil

    from service.acquisition.pipeline import _set_state
    from service.index.scanner import index_file
    from service.navidrome.client import trigger_scan

    row = await session.get(AcquisitionJobRow, job_id)
    if not row or row.state != "staged" or not row.staging_path:
        raise HTTPException(404, "Staged job not found")

    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        row.state = "failed"
        row.error = "Staged file no longer exists"
        await session.commit()
        raise HTTPException(410, "Staged file missing")

    # Compute music_dir destination by replacing staging_dir prefix
    try:
        rel = staging_path.relative_to(settings.staging_dir)
    except ValueError:
        raise HTTPException(500, "staging_path not under staging_dir")

    dest = settings.music_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        try:
            import os
            os.rename(staging_path, dest)
        except OSError:
            shutil.move(str(staging_path), str(dest))
    except Exception as exc:
        raise HTTPException(500, f"Move failed: {exc}")

    # Index and scan
    try:
        await index_file(session, dest)
        await session.flush()
    except Exception as exc:
        logger.warning("Staging approve: index failed for %s: %s", dest, exc)

    row.state = "done"
    row.staging_path = None
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()

    try:
        await trigger_scan()
    except Exception:
        pass

    logger.info("Staging approved: %s → %s", job_id, dest)
    return HTMLResponse('<span class="badge-ok">Approved — moved to library</span>')


@router.post("/staging/{job_id}/reenrich", response_class=HTMLResponse)
async def staging_reenrich(
    request: Request,
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Re-run MB enrichment on a staged file; auto-approve if quality improves."""
    from service.config import settings as _s
    from service.index.scanner import index_file
    from service.library.tagger import write_cover_jpg, write_tags
    from service.library.writer import atomic_place
    from service.metadata.artwork import fetch_artwork
    from service.metadata.musicbrainz import lookup_recording
    from service.metadata.quality import compute_quality_score
    from service.navidrome.client import trigger_scan

    row = await session.get(AcquisitionJobRow, job_id)
    if not row or row.state != "staged" or not row.staging_path:
        raise HTTPException(404)

    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        raise HTTPException(410, "Staged file missing")

    # Parse candidate for current title/artist
    from service.core.models import TrackCandidate
    candidate: TrackCandidate | None = None
    if row.candidate_json:
        try:
            candidate = TrackCandidate.model_validate_json(row.candidate_json)
        except Exception:
            pass

    title = (candidate.title if candidate else None) or staging_path.stem
    artist = (candidate.artist if candidate else None) or "Unknown"

    mb = await asyncio.to_thread(
        lookup_recording, title, artist, None, cache_dir=_s.cache_dir
    )
    if mb is None:
        return HTMLResponse('<span class="badge-warn">No MB match found — still in staging</span>')

    # Write improved tags to staged file
    artwork_bytes: bytes | None = None
    try:
        artwork_bytes = await fetch_artwork(
            release_mbid=mb.release_id,
            thumbnail_url=None,
            cache_dir=_s.cache_dir,
        )
    except Exception:
        pass

    await asyncio.to_thread(
        write_tags,
        staging_path,
        title=mb.title or title,
        artist=mb.artist or artist,
        albumartist=mb.artist or artist,
        album=mb.album,
        year=mb.year,
        original_year=mb.original_year,
        track_number=mb.track_number,
        artist_sort=mb.artist_sort,
        mb_recording_id=mb.recording_id,
        mb_release_id=mb.release_id,
        mb_artist_id=mb.artist_id,
        artwork_bytes=artwork_bytes,
    )

    new_score = compute_quality_score(
        title=mb.title or title,
        artist=mb.artist or artist,
        album=mb.album,
        year=mb.year,
        track_number=mb.track_number,
        musicbrainz_recording_id=mb.recording_id,
        has_cover_art=artwork_bytes is not None,
    )

    if new_score >= _s.staging_quality_threshold:
        # Auto-promote to library
        try:
            rel = staging_path.relative_to(_s.staging_dir)
        except ValueError:
            return HTMLResponse('<span class="badge-warn">Enriched but path error — approve manually</span>')
        dest = _s.music_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_place(staging_path, dest)
        if artwork_bytes:
            write_cover_jpg(dest.parent, artwork_bytes)
        try:
            await index_file(session, dest)
            await session.flush()
        except Exception as exc:
            logger.warning("Re-enrich auto-promote index failed: %s", exc)
        row.state = "done"
        row.staging_path = None
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
        try:
            await trigger_scan()
        except Exception:
            pass
        return HTMLResponse(
            f'<span class="badge-ok">Enriched (quality {new_score:.0%}) — auto-promoted to library</span>'
        )

    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    return HTMLResponse(
        f'<span class="badge-warn">Enriched (quality {new_score:.0%}) — still below threshold, approve manually</span>'
    )


@router.post("/staging/{job_id}/reject", response_class=HTMLResponse)
async def staging_reject(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Delete the staged file and mark the job failed."""
    row = await session.get(AcquisitionJobRow, job_id)
    if not row or row.state != "staged":
        raise HTTPException(404)

    if row.staging_path:
        try:
            Path(row.staging_path).unlink(missing_ok=True)
            # Remove empty parent dirs up to staging_dir
            p = Path(row.staging_path).parent
            for _ in range(3):
                if p == settings.staging_dir or not p.exists():
                    break
                try:
                    p.rmdir()
                except OSError:
                    break
                p = p.parent
        except Exception as exc:
            logger.debug("Staging reject cleanup: %s", exc)

    row.state = "failed"
    row.failure_class = "permanent"
    row.error = "Rejected from staging queue"
    row.staging_path = None
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    return HTMLResponse("")


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

    return templates.TemplateResponse(
        request, "library_health.html",
        {
            "active": "lib-health",
            "dupe_count": dupe_count,
            "no_cover_count": no_cover_count,
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

    for track in album.tracks:
        if track.file:
            fp = Path(track.file.path)
            if fp.exists():
                try:
                    safe_trash(fp, settings.music_dir / ".trash")
                except Exception as exc:
                    logger.warning("Trash failed for %s: %s", fp, exc)
            await session.delete(track.file)
        await session.delete(track)

    await session.delete(album)
    await session.commit()

    try:
        from service.navidrome.client import trigger_scan
        await trigger_scan()
    except Exception:
        pass

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
    """Move all files from source album into canonical album folder, then rescan."""
    import os
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

    # Determine the canonical album directory
    canonical_dir: Path | None = None
    for t in canonical.tracks:
        if t.file and Path(t.file.path).exists():
            canonical_dir = Path(t.file.path).parent
            break
    if canonical_dir is None:
        return HTMLResponse('<span class="badge-warn">Canonical album has no files on disk</span>')

    moved = 0
    skipped = 0
    for track in source.tracks:
        if not track.file:
            continue
        src = Path(track.file.path)
        if not src.exists():
            continue
        dst = canonical_dir / src.name
        if dst.exists():
            skipped += 1
            continue
        try:
            os.rename(src, dst)
            moved += 1
        except OSError as exc:
            logger.warning("Merge: failed to move %s → %s: %s", src, dst, exc)

    # Remove now-empty source directory
    try:
        src_dir = Path(source.tracks[0].file.path).parent if source.tracks and source.tracks[0].file else None
        if src_dir and src_dir.exists() and not list(src_dir.iterdir()):
            src_dir.rmdir()
    except Exception:
        pass

    # Re-scan canonical dir to update DB
    try:
        from service.index.scanner import scan
        await scan(session, canonical_dir, incremental=False)
        await session.commit()
    except Exception as exc:
        logger.warning("Merge: scan failed: %s", exc)

    try:
        from service.navidrome.client import trigger_scan
        await trigger_scan()
    except Exception:
        pass

    return HTMLResponse(
        f'<span class="badge-ok">Merged {moved} tracks ✓'
        + (f" ({skipped} skipped — already existed)" if skipped else "")
        + "</span>"
    )
