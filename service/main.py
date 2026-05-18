from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.config import settings
from service.core.models import SearchResult, TrackQuality, TrackRef
from service.db.schema import Artist, Track
from service.db.session import get_session

app = FastAPI(title="audioreap", version="0.1.0")


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": "0.1.0",
        "music_dir": str(settings.music_dir),
        "timestamp": datetime.utcnow().isoformat(),
    }


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
        .where(
            or_(
                Track.title.ilike(pattern),
                Artist.name.ilike(pattern),
            )
        )
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

        tracks.append(
            TrackRef(
                internal_id=row.id,
                source="local",
                status="available" if file is not None else "missing",
                title=row.title,
                artist=row.artist.name,
                album=row.album.title if row.album else None,
                duration_seconds=row.duration_seconds,
                local_path=local_path,
                musicbrainz_recording_id=row.musicbrainz_recording_id,
                quality=quality,
            )
        )

    return SearchResult(
        tracks=tracks,
        albums=[],
        artists=[],
        query_echo=q,
    )
