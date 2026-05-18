"""Recursive library scanner.

Walks /music, reads tags, upserts Artist/Album/Track/TrackFile rows.
Full scan processes every file. Incremental skips files whose mtime is
unchanged since the last index run.
"""
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from service.core.identity import make_id
from service.core.normalize import normalize
from service.db.schema import Album, Artist, Track, TrackFile
from service.library.tagger import SUPPORTED_EXTENSIONS, TaggedFile, read_tags

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    errors: int = 0


def _artist_id(name: str) -> str:
    digest = hashlib.sha1(normalize(name).encode()).hexdigest()
    return f"artist:{digest}"


def _album_id(artist_name: str, album_title: str, year: int | None) -> str:
    key = f"{normalize(artist_name)}|{normalize(album_title)}|{year or ''}"
    digest = hashlib.sha1(key.encode()).hexdigest()
    return f"album:{digest}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _upsert_artist(session: AsyncSession, name: str) -> str:
    aid = _artist_id(name)
    row = await session.get(Artist, aid)
    if row is None:
        row = Artist(
            id=aid,
            name=name,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(row)
    return aid


async def _upsert_album(
    session: AsyncSession,
    artist_id: str,
    title: str,
    year: int | None,
    artist_name: str,
) -> str:
    alid = _album_id(artist_name, title, year)
    row = await session.get(Album, alid)
    if row is None:
        row = Album(
            id=alid,
            title=title,
            year=year,
            artist_id=artist_id,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(row)
    return alid


async def _upsert_track_file(
    session: AsyncSession,
    tagged: TaggedFile,
    track_id: str,
    file_mtime: float,
) -> bool:
    """Return True if this was a new/updated record."""
    path_str = str(tagged.path)
    result = await session.execute(select(TrackFile).where(TrackFile.path == path_str))
    row = result.scalar_one_or_none()

    if row is not None:
        if row.file_mtime == file_mtime:
            return False  # unchanged
        row.codec = tagged.codec
        row.container = tagged.container
        row.bitrate_kbps = tagged.bitrate_kbps
        row.sample_rate_hz = tagged.sample_rate_hz
        row.file_mtime = file_mtime
        return True

    new_file = TrackFile(
        track_id=track_id,
        path=path_str,
        codec=tagged.codec,
        container=tagged.container,
        bitrate_kbps=tagged.bitrate_kbps,
        sample_rate_hz=tagged.sample_rate_hz,
        file_mtime=file_mtime,
        created_at=_now(),
    )
    session.add(new_file)
    return True


async def index_file(session: AsyncSession, path: Path) -> str:
    """Index a single newly-placed file. Convenience wrapper for the pipeline."""
    return await _process_file(session, path, incremental=False)


async def _process_file(
    session: AsyncSession,
    path: Path,
    incremental: bool,
) -> str:
    """Process a single audio file. Returns 'added'|'updated'|'skipped'|'error'."""
    try:
        stat = path.stat()
    except OSError:
        return "error"

    file_mtime = stat.st_mtime

    if incremental:
        result = await session.execute(
            select(TrackFile).where(TrackFile.path == str(path))
        )
        existing = result.scalar_one_or_none()
        if existing is not None and existing.file_mtime == file_mtime:
            return "skipped"

    tagged = read_tags(path)
    if tagged is None:
        return "error"

    artist_name = tagged.albumartist or tagged.artist or "Unknown Artist"
    title = tagged.title or path.stem
    album_title = tagged.album

    artist_id = await _upsert_artist(session, artist_name)

    album_id: str | None = None
    if album_title:
        album_id = await _upsert_album(session, artist_id, album_title, tagged.year, artist_name)

    track_id = make_id(
        artist=tagged.artist or artist_name,
        title=title,
        duration_seconds=tagged.duration_seconds,
    )

    track_row = await session.get(Track, track_id)
    track_is_new = track_row is None
    if track_row is None:
        track_row = Track(
            id=track_id,
            title=title,
            artist_id=artist_id,
            album_id=album_id,
            duration_seconds=tagged.duration_seconds,
            track_number=tagged.track_number,
            disc_number=tagged.disc_number,
            created_at=_now(),
            updated_at=_now(),
        )
        session.add(track_row)
    else:
        track_row.album_id = album_id
        track_row.track_number = tagged.track_number
        track_row.updated_at = _now()

    is_new_or_updated = await _upsert_track_file(session, tagged, track_id, file_mtime)
    if not is_new_or_updated:
        return "skipped"
    return "added" if track_is_new else "updated"


async def _collect_known_paths(session: AsyncSession) -> set[str]:
    result = await session.execute(select(TrackFile.path))
    return {row for (row,) in result}


async def scan(
    session: AsyncSession,
    music_dir: Path,
    *,
    incremental: bool = False,
    batch_size: int = 100,
) -> ScanResult:
    """Walk music_dir and upsert all audio files into the DB.

    incremental=True skips files whose path+mtime matches an existing record.
    Files deleted from disk are marked missing (TrackFile removed) on full scan.
    """
    result = ScanResult()

    known_paths = await _collect_known_paths(session) if not incremental else set()
    seen_paths: set[str] = set()

    batch: list[Path] = []

    async def flush() -> None:
        for p in batch:
            status = await _process_file(session, p, incremental)
            if status == "added":
                result.added += 1
            elif status == "updated":
                result.updated += 1
            elif status == "skipped":
                result.skipped += 1
            else:
                result.errors += 1
                logger.warning("Could not read tags: %s", p)
        await session.flush()
        batch.clear()

    for dirpath, _, filenames in os.walk(music_dir):
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            seen_paths.add(str(path))
            batch.append(path)
            if len(batch) >= batch_size:
                await flush()

    if batch:
        await flush()

    if not incremental:
        removed_paths = known_paths - seen_paths
        for path_str in removed_paths:
            await session.execute(delete(TrackFile).where(TrackFile.path == path_str))
            result.removed += 1
            logger.info("Removed missing file from index: %s", path_str)
        if removed_paths:
            await session.flush()

    logger.info(
        "Scan complete: added=%d updated=%d skipped=%d removed=%d errors=%d",
        result.added,
        result.updated,
        result.skipped,
        result.removed,
        result.errors,
    )
    return result
