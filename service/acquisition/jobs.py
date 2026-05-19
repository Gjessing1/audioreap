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


async def acquire_album_from_mb(
    ctx: dict[str, object],
    *,
    album_job_id: str,
    release_group_id: str,
    artist_name: str,
    music_dir: str,
    tmp_acquire_dir: str,
) -> None:
    """arq job: acquire all tracks of an MB release group as a coordinated album.

    Unlike independent acquire_track jobs, this job:
    - Fetches the definitive track list from MB (title, position, recording ID)
    - Creates child jobs with album metadata locked into the candidate, preventing
      the pipeline from re-routing tracks to a different album folder based on
      which MB release shows up first in text search results.
    - Skips tracks already owned by MB recording ID.
    """
    import asyncio as _asyncio
    from arq import create_pool
    from arq.connections import RedisSettings
    from service.config import settings as _settings
    from service.db.schema import AcquisitionJobRow as _JobRow, AlbumAcquisitionJob as _AlbumJob, Track as _Track
    from service.metadata.musicbrainz import get_release_group_tracks
    from sqlalchemy import select as _select

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]

    # ── 1. Fetch track list from MB (blocking, cached) ─────────────────────
    try:
        album_title, release_id, mb_year, mb_tracks = await _asyncio.to_thread(
            get_release_group_tracks, release_group_id, _settings.cache_dir
        )
    except Exception as exc:
        logger.error("Album job %s: MB track fetch failed: %s", album_job_id, exc)
        async with session_factory() as session, session.begin():
            row = await session.get(_AlbumJob, album_job_id)
            if row:
                row.state = "failed"
        return

    if not mb_tracks:
        logger.warning("Album job %s: no tracks found for release group %s", album_job_id, release_group_id)
        async with session_factory() as session, session.begin():
            row = await session.get(_AlbumJob, album_job_id)
            if row:
                row.state = "failed"
        return

    # Year: MB release group is authoritative; fall back to whatever was stored in the job row
    year_val: int | None = mb_year
    if year_val is None:
        async with session_factory() as session:
            album_row = await session.get(_AlbumJob, album_job_id)
            if album_row and album_row.candidate_json:
                try:
                    import json
                    data = json.loads(album_row.candidate_json)
                    year_val = data.get("year")
                except Exception:
                    pass

    # ── 2. Check which tracks are already owned by MB recording ID ─────────
    owned_recording_ids: set[str] = set()
    rids = [t.recording_id for t in mb_tracks if t.recording_id]
    if rids:
        async with session_factory() as session:
            existing = (await session.execute(
                _select(_Track).where(_Track.musicbrainz_recording_id.in_(rids))
            )).scalars().all()
            owned_recording_ids = {r.musicbrainz_recording_id for r in existing if r.musicbrainz_recording_id}

    # ── 3. Create child jobs with locked album metadata ─────────────────────
    redis = await create_pool(RedisSettings.from_dsn(_settings.redis_url))
    queued_count = 0

    async with session_factory() as session, session.begin():
        for t in mb_tracks:
            if t.recording_id and t.recording_id in owned_recording_ids:
                continue
            search_ref = f"ytsearch1:{artist_name} {t.title}"
            candidate = TrackCandidate(
                provider="ytdlp",
                provider_ref=search_ref,
                title=t.title,
                artist=artist_name,
                album=album_title,
                year=year_val,
                track_number=t.number,
                duration_seconds=t.duration_seconds,
                mb_release_id=release_id,
                mb_recording_id=t.recording_id,
            )
            job_id = await create_job(
                session,
                provider_name="ytdlp",
                provider_ref=search_ref,
                candidate=candidate,
                query=f"{artist_name} - {t.title}",
            )
            # Stamp album relationship
            child_row = await session.get(_JobRow, job_id)
            if child_row:
                child_row.album_job_id = album_job_id

            await redis.enqueue_job(
                "acquire_track",
                job_id=job_id,
                provider_name="ytdlp",
                provider_ref=search_ref,
                candidate_json=candidate.model_dump_json(),
                music_dir=music_dir,
                tmp_acquire_dir=tmp_acquire_dir,
                _job_id=f"acquire:{job_id}",
            )
            queued_count += 1

        # Update album job state
        album_row = await session.get(_AlbumJob, album_job_id)
        if album_row:
            album_row.state = "running"
            album_row.track_count = len(mb_tracks)

    await redis.aclose()
    logger.info(
        "Album job %s (%s): queued %d tracks, %d already owned",
        album_job_id, album_title, queued_count, len(owned_recording_ids),
    )


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

    dest_path: Path | None = None
    async with session_factory() as session, session.begin():
        dest_path = await run_acquisition(
            job_id=job_id,
            provider=provider,
            provider_ref=provider_ref,
            candidate=candidate,
            music_dir=Path(music_dir),
            tmp_acquire_dir=Path(tmp_acquire_dir),
            session=session,
        )

    # ReplayGain runs after the session is committed — avoids the SQLAlchemy
    # greenlet conflict that happens when subprocess.run() is called inside a session.
    if dest_path is not None and dest_path.exists():
        try:
            from service.library.tagger import compute_replaygain, write_replaygain
            import asyncio as _asyncio
            rg_gain = await _asyncio.to_thread(compute_replaygain, dest_path)
            if rg_gain is not None:
                await _asyncio.to_thread(write_replaygain, dest_path, rg_gain)
                logger.debug("ReplayGain: %s gain=%+.2f dB", dest_path.name, rg_gain)
        except Exception as rg_exc:
            logger.debug("ReplayGain failed for %s: %s", dest_path, rg_exc)
