"""arq job definitions for the acquisition pipeline."""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from service.acquisition.pipeline import run_acquisition
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow
from service.providers.base import Provider

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def create_job(
    session: AsyncSession,
    *,
    provider_name: str,
    provider_ref: str,
    candidate: TrackCandidate,
    query: str | None = None,
    playlist_import_id: str | None = None,
) -> str:
    """Insert a queued job row and return its ID."""
    job_id = str(uuid.uuid4())
    row = AcquisitionJobRow(
        id=job_id,
        provider=provider_name,
        provider_ref=provider_ref,
        state="queued",
        query=query or f"{candidate.artist} - {candidate.title}",
        candidate_json=candidate.model_dump_json(),
        playlist_import_id=playlist_import_id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    await session.flush()
    return job_id


async def acquire_album(
    ctx: dict[str, object],
    *,
    album_job_id: str,
    provider_name: str,
    album_ref: str,
    candidate_json: str,
    music_dir: str,
    tmp_acquire_dir: str,
    policy: str = "partial_ok",
) -> None:
    """arq job: orchestrate full album acquisition."""
    from service.acquisition.album_pipeline import run_album_acquisition
    from service.core.models import AlbumCandidate

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]
    provider_registry: dict[str, Provider] = ctx["providers"]  # type: ignore[assignment]

    provider = provider_registry.get(provider_name)
    if provider is None:
        logger.error("Unknown provider %r for album job %s", provider_name, album_job_id)
        return

    album_candidate = AlbumCandidate.model_validate_json(candidate_json)

    async with session_factory() as session, session.begin():
        await run_album_acquisition(
            album_job_id=album_job_id,
            provider=provider,
            album_candidate=album_candidate,
            music_dir=Path(music_dir),
            tmp_acquire_dir=Path(tmp_acquire_dir),
            session=session,
            policy=policy,
        )


async def enrich_track(
    ctx: dict[str, object],
    *,
    track_id: str,
) -> None:
    """arq job: attempt MusicBrainz enrichment for a track without a Recording ID."""
    import asyncio
    from pathlib import Path as _Path

    from sqlalchemy import select as _select
    from sqlalchemy.orm import joinedload as _joinedload

    from service.config import settings as _settings
    from service.db.schema import Track as _Track
    from service.library.tagger import has_cover_art as _has_cover_art, write_tags as _write_tags
    from service.metadata.musicbrainz import lookup_recording as _lookup
    from service.metadata.quality import compute_quality_score as _quality

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]

    async with session_factory() as session, session.begin():
        stmt = (
            _select(_Track)
            .options(
                _joinedload(_Track.artist),
                _joinedload(_Track.album),
                _joinedload(_Track.file),
            )
            .where(_Track.id == track_id)
        )
        track = (await session.execute(stmt)).unique().scalar_one_or_none()
        if track is None or track.musicbrainz_recording_id:
            return

        match = await asyncio.to_thread(
            _lookup,
            track.title,
            track.artist.name,
            track.duration_seconds,
            _settings.cache_dir,
        )
        if match is None:
            logger.debug("No MB match for track %s", track_id)
            return

        track.musicbrainz_recording_id = match.recording_id

        if track.file:
            file_path = _Path(track.file.path)
            if file_path.exists():
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
                track.file.has_cover_art = hca
                track.tag_quality_score = _quality(
                    title=match.title or track.title,
                    artist=match.artist or track.artist.name,
                    album=match.album or (track.album.title if track.album else None),
                    year=match.year,
                    track_number=match.track_number,
                    musicbrainz_recording_id=match.recording_id,
                    has_cover_art=hca,
                )

    logger.info("Enriched track %s → MB %s", track_id, match.recording_id)


async def acquire_track(
    ctx: dict[str, object],
    *,
    job_id: str,
    provider_name: str,
    provider_ref: str,
    candidate_json: str,
    music_dir: str,
    tmp_acquire_dir: str,
) -> None:
    """arq job: run the full acquisition pipeline for one track."""
    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]
    provider_registry: dict[str, Provider] = ctx["providers"]  # type: ignore[assignment]

    provider = provider_registry.get(provider_name)
    if provider is None:
        logger.error("Unknown provider %r for job %s", provider_name, job_id)
        return

    candidate = TrackCandidate.model_validate_json(candidate_json)

    async with session_factory() as session:
        row = await session.get(AcquisitionJobRow, job_id)
        if row is not None and row.state == "cancelled":
            logger.info("Job %s was cancelled before pickup; skipping", job_id)
            return

    async with session_factory() as session, session.begin():
        await run_acquisition(
            job_id=job_id,
            provider=provider,
            provider_ref=provider_ref,
            candidate=candidate,
            music_dir=Path(music_dir),
            tmp_acquire_dir=Path(tmp_acquire_dir),
            session=session,
        )
