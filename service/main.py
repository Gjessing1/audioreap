from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.config import settings
from service.core.models import (
    AcquisitionJob,
    SearchResult,
    TrackQuality,
    TrackRef,
)
from service.db.schema import AcquisitionJobRow, Artist, Track
from service.db.session import get_session

app = FastAPI(title="audioreap", version="0.1.0")


# ── Health ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": "0.1.0",
        "music_dir": str(settings.music_dir),
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── Search ────────────────────────────────────────────────────────────────

@app.get("/api/search", response_model=SearchResult)
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> SearchResult:
    pattern = f"%{q}%"
    stmt = (
        select(Track)
        .join(Track.artist)
        .outerjoin(Track.album)
        .outerjoin(Track.file)
        .options(
            joinedload(Track.artist),
            joinedload(Track.album),
            joinedload(Track.file),
        )
        .where(or_(Track.title.ilike(pattern), Artist.name.ilike(pattern)))
        .order_by(Artist.name, Track.title)
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).unique().scalars().all()

    tracks: list[TrackRef] = []
    for row in rows:
        file = row.file
        quality: TrackQuality | None = None
        local_path: Path | None = None
        if file is not None:
            quality = TrackQuality(
                codec=file.codec,
                container=file.container,
                bitrate_kbps=file.bitrate_kbps,
                sample_rate_hz=file.sample_rate_hz,
            )
            local_path = Path(file.path)
        tracks.append(TrackRef(
            internal_id=row.id,
            source="local",
            status="available" if file else "missing",
            title=row.title,
            artist=row.artist.name,
            album=row.album.title if row.album else None,
            duration_seconds=row.duration_seconds,
            local_path=local_path,
            musicbrainz_recording_id=row.musicbrainz_recording_id,
            quality=quality,
        ))

    return SearchResult(tracks=tracks, albums=[], artists=[], query_echo=q)


# ── Acquire ───────────────────────────────────────────────────────────────

class AcquireRequest(BaseModel):
    provider_name: str = "ytdlp"
    provider_ref: str
    candidate_json: str
    query: str | None = None


class AcquireResponse(BaseModel):
    job_id: str


@app.post("/api/acquire", response_model=AcquireResponse)
async def acquire(
    req: AcquireRequest,
    session: AsyncSession = Depends(get_session),
) -> AcquireResponse:
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    candidate = TrackCandidate.model_validate_json(req.candidate_json)

    async with session.begin():
        job_id = await create_job(
            session,
            provider_name=req.provider_name,
            provider_ref=req.provider_ref,
            candidate=candidate,
            query=req.query,
        )

    try:
        from arq import create_pool
        redis = await create_pool(settings.redis_url)  # type: ignore[arg-type]
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=req.provider_name,
            provider_ref=req.provider_ref,
            candidate_json=req.candidate_json,
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Queue unavailable: {exc}") from exc

    return AcquireResponse(job_id=job_id)


# ── Jobs ──────────────────────────────────────────────────────────────────

def _job_row_to_model(row: AcquisitionJobRow) -> AcquisitionJob:
    from service.core.models import TrackCandidate, TrackRef
    label = row.query or f"{row.provider}:{row.provider_ref}"
    candidate: TrackCandidate | None = None
    if row.candidate_json:
        try:
            candidate = TrackCandidate.model_validate_json(row.candidate_json)
        except Exception:
            pass
    track_ref = TrackRef(
        internal_id=row.track_id or f"job:{row.id}",
        source="cloud",
        status="acquiring" if row.state not in ("done", "failed") else
               "available" if row.state == "done" else "failed",
        title=candidate.title if candidate else label,
        artist=candidate.artist if candidate else "Unknown",
        provider=row.provider,
        provider_ref=row.provider_ref,
    )
    return AcquisitionJob(
        id=row.id,
        track_ref=track_ref,
        state=row.state,  # type: ignore[arg-type]
        progress=row.progress,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@app.get("/api/jobs", response_model=list[AcquisitionJob])
async def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[AcquisitionJob]:
    rows = (
        await session.execute(
            select(AcquisitionJobRow)
            .order_by(AcquisitionJobRow.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_job_row_to_model(r) for r in rows]


@app.get("/api/jobs/{job_id}", response_model=AcquisitionJob)
async def get_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> AcquisitionJob:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_row_to_model(row)
