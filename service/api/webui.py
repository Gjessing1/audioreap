"""HTMX-rendered web UI routes."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.config import settings
from service.core.models import AcquisitionJob, TrackCandidate, TrackQuality, TrackRef
from service.db.schema import AcquisitionJobRow, Album, Artist, Track, TrackFile
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
    ctx: dict[str, object] = {"request": request, "active": "search", "q": q, "tracks": []}
    return templates.TemplateResponse("search.html", ctx)


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
        "partials/local_results.html",
        {"request": request, "tracks": tracks, "q": q},
    )


@router.get("/search/cloud", response_class=HTMLResponse)
async def cloud_search(request: Request, q: str = "") -> HTMLResponse:
    candidates: list[TrackCandidate] = []
    if q:
        try:
            import service.providers.ytdlp  # noqa: F401
            from service.core.models import SearchQuery
            from service.providers import get

            provider = get("ytdlp")()
            async for c in provider.search(SearchQuery(q=q, limit=5)):
                candidates.append(c)
        except Exception as exc:
            logger.warning("Cloud search failed: %s", exc)

    return templates.TemplateResponse(
        "partials/cloud_results.html",
        {"request": request, "candidates": candidates, "q": q},
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(50)
        )
    ).scalars().all()
    jobs = [_job_to_model(r) for r in rows]
    return templates.TemplateResponse(
        "jobs.html", {"request": request, "active": "jobs", "jobs": jobs}
    )


@router.get("/jobs/list", response_class=HTMLResponse)
async def jobs_list_partial(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await session.execute(
            select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(50)
        )
    ).scalars().all()
    jobs = [_job_to_model(r) for r in rows]
    return templates.TemplateResponse(
        "partials/job_list.html", {"request": request, "jobs": jobs}
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
        "partials/job_card.html", {"request": request, "job": _job_to_model(row)}
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
        redis = await create_pool(settings.redis_url)  # type: ignore[arg-type]
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
        "partials/job_card.html", {"request": request, "job": _job_to_model(row)}
    )


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    track_count = (await session.execute(select(func.count(Track.id)))).scalar_one()
    album_count = (await session.execute(select(func.count(Album.id)))).scalar_one()
    artist_count = (await session.execute(select(func.count(Artist.id)))).scalar_one()

    recent_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .outerjoin(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(TrackFile.id.isnot(None))
            .order_by(TrackFile.created_at.desc())
            .limit(20)
        )
    ).unique().scalars().all()

    return templates.TemplateResponse(
        "library.html",
        {
            "request": request,
            "active": "library",
            "stats": {"tracks": track_count, "albums": album_count, "artists": artist_count},
            "recent": [_track_to_ref(r) for r in recent_rows],
        },
    )


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    import shutil as _shutil

    # Disk free
    try:
        disk = _shutil.disk_usage(settings.music_dir)
        disk_free_gb = round(disk.free / 1024**3, 1)
    except Exception:
        disk_free_gb = -1

    # Navidrome check
    navidrome_ok = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{settings.navidrome_url}/rest/ping.view",
                            params={"u": "x", "p": "x", "v": "1.16.1", "c": "audioreap", "f": "json"})
            navidrome_ok = r.status_code < 500
    except Exception:
        pass

    # Redis check
    redis_ok = False
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        await r.aclose()
        redis_ok = True
    except Exception:
        pass

    # Active jobs
    active_jobs = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state.notin_(["done", "failed"]))
        )
    ).scalar_one()

    return templates.TemplateResponse(
        "health.html",
        {
            "request": request,
            "active": "health",
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
