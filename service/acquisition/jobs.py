"""arq job definitions for the acquisition pipeline."""
from __future__ import annotations

import asyncio
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
    """arq job: find MusicBrainz match for a track without a Recording ID.

    Instead of auto-applying, creates a needs_review job so the user can
    inspect and approve (or reject) the suggested metadata.
    """
    import asyncio
    import json
    from pathlib import Path as _Path

    from sqlalchemy import select as _select
    from sqlalchemy.orm import joinedload as _joinedload

    from service.config import settings as _settings
    from service.db.schema import AcquisitionJobRow as _JobRow, Track as _Track
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
        if not track.file:
            return

        file_path = _Path(track.file.path)
        if not file_path.exists():
            return

        # Check if a pending enrichment suggestion already exists for this track
        existing_enrich = (await session.execute(
            _select(_JobRow).where(
                _JobRow.provider == "enrich",
                _JobRow.provider_ref == track_id,
                _JobRow.state == "needs_review",
            )
        )).scalar_one_or_none()
        if existing_enrich:
            logger.debug("Enrichment suggestion already pending for track %s", track_id)
            return

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

        if match is None:
            logger.debug("No MB match for track %s", track_id)
            return

        clean_title = match.title or lookup_title
        clean_artist = match.artist or lookup_artist
        hca = bool(track.file.has_cover_art)
        quality = _quality(
            title=clean_title,
            artist=clean_artist,
            album=match.album or (track.album.title if track.album else None),
            year=match.year,
            track_number=match.track_number,
            musicbrainz_recording_id=match.recording_id,
            has_cover_art=hca,
        )

        resolved_metadata = {
            "title": clean_title,
            "artist": clean_artist,
            "albumartist": clean_artist,
            "album": match.album,
            "year": match.year,
            "original_year": match.original_year,
            "track_number": match.track_number,
            "disc_number": track.disc_number,
            "duration_seconds": track.duration_seconds,
            "ext": file_path.suffix.lstrip("."),
            "mb_recording_id": match.recording_id,
            "mb_release_id": match.release_id,
            "mb_artist_id": match.artist_id,
            "mb_artist_sort": match.artist_sort,
            "mb_match_source": "text_search",
            "is_enrichment": True,
            "current_title": track.title,
            "current_artist": track.artist.name,
            "quality_score": quality,
        }

        job_id = str(uuid.uuid4())
        job_row = _JobRow(
            id=job_id,
            provider="enrich",
            provider_ref=track_id,
            state="needs_review",
            query=f"{track.artist.name} – {track.title}",
            staging_path=str(file_path),
            resolved_metadata_json=json.dumps(resolved_metadata),
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(job_row)

    logger.info(
        "Created enrichment suggestion %s for track %s → MB %s",
        job_id, track_id, match.recording_id,
    )


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


_RETRY_DELAYS = [30, 120, 600]  # seconds: 30s, 2 min, 10 min
_MAX_RETRIES = len(_RETRY_DELAYS)


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
    from datetime import timedelta

    from arq import create_pool
    from arq.connections import RedisSettings

    from service.config import settings as _settings

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

    # Progress callback: writes row.progress to DB (throttled by ytdlp hook to ~5% steps)
    async def _write_progress(fraction: float) -> None:
        try:
            async with session_factory() as s, s.begin():
                r = await s.get(AcquisitionJobRow, job_id)
                if r and r.state == "downloading":
                    r.progress = fraction
        except Exception:
            pass

    def on_progress(fraction: float) -> None:
        asyncio.create_task(_write_progress(fraction))

    async with session_factory() as session, session.begin():
        await run_acquisition(
            job_id=job_id,
            provider=provider,
            provider_ref=provider_ref,
            candidate=candidate,
            tmp_acquire_dir=Path(tmp_acquire_dir),
            session=session,
            on_progress=on_progress,
        )

    # Auto-retry on transient failures
    retry_attempt: int | None = None
    delay_seconds: int = 0
    async with session_factory() as session, session.begin():
        row = await session.get(AcquisitionJobRow, job_id)
        if row is None or row.state != "failed" or row.failure_class != "transient":
            return
        if row.retry_count >= _MAX_RETRIES:
            logger.warning("Job %s: max retries reached (%d), leaving as failed", job_id, row.retry_count)
            return

        delay_seconds = _RETRY_DELAYS[row.retry_count]
        row.retry_count += 1
        retry_attempt = row.retry_count
        row.state = "queued"
        row.error = f"Transient failure — retry {retry_attempt}/{_MAX_RETRIES} in {delay_seconds}s"
        row.updated_at = _now()

    if retry_attempt is None:
        return

    logger.info("Job %s: transient failure, scheduling retry %d/%d in %ds", job_id, retry_attempt, _MAX_RETRIES, delay_seconds)
    redis = await create_pool(RedisSettings.from_dsn(_settings.redis_url))
    try:
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=provider_name,
            provider_ref=provider_ref,
            candidate_json=candidate_json,
            music_dir=music_dir,
            tmp_acquire_dir=tmp_acquire_dir,
            _job_id=f"acquire:{job_id}:r{retry_attempt}",
            _defer_by=timedelta(seconds=delay_seconds),
        )
    finally:
        await redis.aclose()


_GC_TERMINAL_STATES = frozenset({"done", "failed", "rejected", "cancelled"})
_GC_MAX_AGE_DAYS = 7


async def gc_staging(ctx: dict[str, object]) -> None:
    """Periodic job: trash staging files for terminal jobs older than 7 days."""
    import os
    from datetime import timedelta

    from sqlalchemy import select as _select

    from service.config import settings as _settings
    from service.library.writer import safe_trash

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]
    cutoff = _now() - timedelta(days=_GC_MAX_AGE_DAYS)
    trash_dir = _settings.music_dir / ".trash"
    trashed = 0
    cleared = 0

    async with session_factory() as session, session.begin():
        rows = (await session.execute(
            _select(AcquisitionJobRow).where(
                AcquisitionJobRow.state.in_(list(_GC_TERMINAL_STATES)),
                AcquisitionJobRow.staging_path.isnot(None),
                AcquisitionJobRow.updated_at < cutoff,
            )
        )).scalars().all()

        for row in rows:
            path = Path(row.staging_path)  # type: ignore[arg-type]
            if path.exists():
                try:
                    safe_trash(path, trash_dir)
                    trashed += 1
                except Exception as exc:
                    logger.warning("gc_staging: could not trash %s: %s", path, exc)
            row.staging_path = None
            cleared += 1

    logger.info("gc_staging: cleared %d staging paths (%d files trashed)", cleared, trashed)


async def fetch_missing_covers(ctx: dict[str, object]) -> None:
    """arq job: fetch cover art from CAA for every album that has a MB release ID but no cover."""
    from pathlib import Path

    from service.config import settings as _s
    from service.db.schema import Album as _Album, Track as _Track, TrackFile as _TF
    from service.library.tagger import write_cover_jpg as _write_cover_jpg
    from service.metadata.artwork import fetch_artwork as _fetch_artwork
    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory: async_sessionmaker[AsyncSession] = ctx["session_factory"]  # type: ignore[assignment]

    async with session_factory() as session:
        # Albums that have a MB release ID but no cover.jpg on disk
        albums = (await session.execute(
            _select(_Album)
            .join(_Album.tracks)
            .join(_Track.file)
            .where(_TF.has_cover_art == 0)
            .where(_Album.musicbrainz_release_id.isnot(None))
            .distinct()
        )).unique().scalars().all()

    fetched = 0
    skipped = 0
    for album in albums:
        # Find first track file to locate the album directory
        async with session_factory() as session:
            tracks = (await session.execute(
                _select(_Track)
                .join(_Track.file)
                .where(_Track.album_id == album.id)
                .limit(1)
            )).unique().scalars().all()
        if not tracks or not tracks[0].file:
            continue
        album_dir = Path(tracks[0].file.path).parent
        cover_dest = album_dir / "cover.jpg"
        if cover_dest.exists():
            skipped += 1
            continue
        try:
            art = await _fetch_artwork(
                release_mbid=album.musicbrainz_release_id,
                cache_dir=_s.cache_dir,
            )
            if art:
                _write_cover_jpg(album_dir, art)
                fetched += 1
        except Exception as exc:
            logger.debug("fetch_missing_covers: %s failed: %s", album.title, exc)

    logger.info("fetch_missing_covers: fetched=%d skipped=%d", fetched, skipped)
