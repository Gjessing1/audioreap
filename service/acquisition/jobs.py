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

        # Attempt MB lookup with the stored title/artist.
        # If the title looks like "Artist - Title" (common for YouTube uploads indexed
        # before the split logic was added), also try with split values.
        lookup_title = track.title
        lookup_artist = track.artist.name
        match = await asyncio.to_thread(
            _lookup, lookup_title, lookup_artist, track.duration_seconds, _settings.cache_dir,
        )
        if match is None and " - " in lookup_title:
            parts = lookup_title.split(" - ", 1)
            split_artist, split_title = parts[0].strip(), parts[1].strip()
            match = await asyncio.to_thread(
                _lookup, split_title, split_artist, track.duration_seconds, _settings.cache_dir,
            )
            if match is not None:
                lookup_title = split_title
                lookup_artist = split_artist

        if match is None:
            logger.debug("No MB match for track %s", track_id)
            return

        track.musicbrainz_recording_id = match.recording_id

        # Update title/artist in DB if they changed (e.g., after "Artist - Title" split)
        clean_title = match.title or lookup_title
        clean_artist = match.artist or lookup_artist
        if clean_title != track.title:
            track.title = clean_title
        if clean_artist != track.artist.name:
            # Find or create the correct Artist row
            from service.index.scanner import _artist_id as _aid
            from service.db.schema import Artist as _Artist
            from datetime import UTC as _UTC, datetime as _dt
            new_aid = _aid(clean_artist)
            existing = await session.get(_Artist, new_aid)
            if existing is None:
                now = _dt.now(_UTC).replace(tzinfo=None)
                session.add(_Artist(
                    id=new_aid, name=clean_artist,
                    created_at=now, updated_at=now,
                ))
            track.artist_id = new_aid

        if track.file:
            file_path = _Path(track.file.path)
            if file_path.exists():
                await asyncio.to_thread(
                    _write_tags,
                    file_path,
                    title=clean_title,
                    artist=clean_artist,
                    albumartist=clean_artist,
                    album=match.album,
                    year=match.year,
                    original_year=match.original_year,
                    track_number=match.track_number,
                    artist_sort=match.artist_sort,
                    mb_recording_id=match.recording_id,
                    mb_release_id=match.release_id,
                    mb_artist_id=match.artist_id,
                )
                hca = await asyncio.to_thread(_has_cover_art, file_path)
                track.file.has_cover_art = hca
                track.tag_quality_score = _quality(
                    title=clean_title,
                    artist=clean_artist,
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
