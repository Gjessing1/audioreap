"""Acquisition pipeline: download → remux → tag → place → index → scan.

Each stage updates the job row so the UI can show live progress.
All filesystem operations happen under /tmp-acquire, then a single atomic
os.rename moves the finished file into /music.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from service.acquisition.states import classify_failure
from service.config import settings
from service.core.identity import make_id
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow
from service.index.scanner import index_file
from service.library.layout import track_path
from service.library.tagger import has_cover_art, read_tags, write_tags
from service.metadata.quality import compute_quality_score
from service.library.writer import atomic_place
from service.providers.base import Provider

logger = logging.getLogger(__name__)

ScanTrigger = Callable[[], Awaitable[None]]

_REMUX_CONTAINERS = frozenset({".webm", ".weba"})


async def _set_state(
    session: AsyncSession,
    job_id: str,
    state: str,
    *,
    progress: float | None = None,
    failure_class: str | None = None,
    error: str | None = None,
    track_id: str | None = None,
) -> None:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        return
    row.state = state
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    if progress is not None:
        row.progress = progress
    if failure_class is not None:
        row.failure_class = failure_class
    if error is not None:
        row.error = error
    if track_id is not None:
        row.track_id = track_id
    await session.flush()


async def _remux_to_ogg(src: Path, dest_dir: Path) -> Path:
    """Remux WebM/WebA container to OGG without re-encoding the audio stream."""
    out = dest_dir / (src.stem + ".ogg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(src), "-c", "copy", str(out), "-y", "-loglevel", "error",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
    except TimeoutError:
        proc.kill()
        raise TimeoutError("ffmpeg remux timed out after 120s")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed: {stderr.decode().strip()}")
    return out


async def _find_local_match(
    session: AsyncSession,
    candidate: TrackCandidate,
) -> str | None:
    """Return the internal_id of a local track that confidently matches candidate."""
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from service.core.normalize import normalize
    from service.db.schema import Track
    from service.search.matcher import is_confident_match

    # Normalize the candidate title to strip noise before searching the DB
    norm_title = normalize(candidate.title)
    first_word = norm_title.split()[0] if norm_title.split() else norm_title
    stmt = (
        select(Track)
        .join(Track.artist)
        .options(joinedload(Track.artist), joinedload(Track.file))
        .where(Track.title.ilike(f"%{first_word}%"))
    )
    rows = (await session.execute(stmt)).unique().scalars().all()

    for row in rows:
        if is_confident_match(
            candidate.title, candidate.artist, candidate.duration_seconds,
            row.title, row.artist.name, row.duration_seconds,
        ):
            return row.id
    return None


async def run_acquisition(
    *,
    job_id: str,
    provider: Provider,
    provider_ref: str,
    candidate: TrackCandidate,
    music_dir: Path,
    tmp_acquire_dir: Path,
    session: AsyncSession,
    scan_trigger: ScanTrigger | None = None,
) -> None:
    """Execute the full acquisition pipeline for one track.

    Updates the AcquisitionJobRow at each stage. Never raises — all errors
    are written to the job row so the caller can move on.
    """
    if scan_trigger is None:
        from service.navidrome.client import trigger_scan
        scan_trigger = trigger_scan

    tmp_acquire_dir.mkdir(parents=True, exist_ok=True)

    # ── 0. Dedup check — skip if confident local match exists ──────────────
    try:
        local_match = await _find_local_match(session, candidate)
        if local_match is not None:
            logger.info(
                "Dedup: skipping acquisition — local match exists: %s", local_match
            )
            await _set_state(session, job_id, "done", track_id=local_match)
            return
    except Exception as dedup_exc:
        logger.debug("Dedup check failed (continuing): %s", dedup_exc)

    with tempfile.TemporaryDirectory(dir=tmp_acquire_dir) as tmp_str:
        tmp_dir = Path(tmp_str)

        # ── 1. Download ────────────────────────────────────────────────────
        await _set_state(session, job_id, "downloading")
        try:
            fetch_result = await provider.fetch(provider_ref, tmp_dir)
        except Exception as exc:
            fc, err = classify_failure(exc)
            await _set_state(session, job_id, "failed", failure_class=fc, error=err)
            logger.error("Download failed [%s] %s: %s", fc, job_id, err)
            return

        audio_path = fetch_result.file_path

        # Extract richer metadata from yt-dlp's full download info dict.
        # The flat search extract often lacks artist/album; full info is authoritative.
        _rm = fetch_result.raw_metadata

        def _rm_str(key: str) -> str | None:
            v = _rm.get(key)
            s = str(v).strip() if v is not None else ""
            return s if s and s.lower() not in ("none", "unknown") else None

        _artist_from_meta = _rm_str("artist")   # only set for YouTube Music
        _title_raw = _rm_str("track") or _rm_str("title")
        _uploader = _rm_str("uploader") or _rm_str("channel")

        if _artist_from_meta:
            # YouTube Music: trust the dedicated artist/track fields
            _fetch_title = _title_raw
            _fetch_artist = _artist_from_meta
        elif _title_raw and " - " in _title_raw:
            # Regular YouTube: split "Artist - Title" convention in video title
            _split = _title_raw.split(" - ", 1)
            _fetch_artist = _split[0].strip()
            _fetch_title = _split[1].strip()
        else:
            _fetch_title = _title_raw
            _fetch_artist = _uploader

        _fetch_album = _rm_str("album")
        _ry = _rm.get("release_year")
        _fetch_year: int | None = int(_ry) if isinstance(_ry, (int, float)) and _ry else None

        # ── 2. Remux if needed ─────────────────────────────────────────────
        await _set_state(session, job_id, "processing")
        if audio_path.suffix.lower() in _REMUX_CONTAINERS:
            try:
                audio_path = await _remux_to_ogg(audio_path, tmp_dir)
            except Exception as exc:
                await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
                logger.error("Remux failed %s: %s", job_id, exc)
                return

        # ── 3. Read tags and decide final metadata ─────────────────────────
        # Priority: file tags > yt-dlp full info > search candidate
        await _set_state(session, job_id, "tagging")
        tagged = read_tags(audio_path)
        title = (tagged.title if tagged else None) or _fetch_title or candidate.title
        artist = (tagged.artist if tagged else None) or _fetch_artist or candidate.artist
        album = (tagged.album if tagged else None) or _fetch_album or candidate.album
        year = (tagged.year if tagged else None) or _fetch_year
        track_number = (tagged.track_number if tagged else None)
        disc_number = (tagged.disc_number if tagged else None)
        duration = (tagged.duration_seconds if tagged else None) or candidate.duration_seconds

        # ── 3a. MusicBrainz lookup ────────────────────────────────────────────
        mb_recording_id: str | None = None
        mb_release_id: str | None = None
        artwork_bytes: bytes | None = None
        try:
            from service.metadata.musicbrainz import lookup_recording
            mb = await asyncio.to_thread(
                lookup_recording, title, artist, duration,
                cache_dir=settings.cache_dir,
            )
            if mb is not None:
                mb_recording_id = mb.recording_id
                mb_release_id = mb.release_id
                title = mb.title or title
                artist = mb.artist or artist
                album = mb.album or album
                year = mb.year or year
                track_number = mb.track_number or track_number
                try:
                    from service.metadata.artwork import fetch_artwork
                    artwork_bytes = await fetch_artwork(
                        release_mbid=mb_release_id,
                        thumbnail_url=candidate.thumbnail_url,
                        cache_dir=settings.cache_dir,
                    )
                except Exception as art_exc:
                    logger.debug("Artwork fetch failed: %s", art_exc)
        except Exception as mb_exc:
            logger.debug("MB lookup skipped: %s", mb_exc)

        # Always write the final resolved tags (candidate fallback or MB-enriched)
        try:
            write_tags(
                audio_path,
                title=title,
                artist=artist,
                album=album,
                year=year,
                track_number=track_number,
            )
        except Exception as tag_exc:
            logger.warning("Tag write failed for %s: %s", audio_path, tag_exc)

        ext = audio_path.suffix.lstrip(".")
        dest = track_path(
            music_dir,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            disc_number=disc_number,
            title=title,
            ext=ext,
        )

        # ── 4. Idempotency check ───────────────────────────────────────────
        if dest.exists():
            logger.info("Track already exists at %s, skipping", dest)
            track_id = make_id(artist=artist, title=title, duration_seconds=duration)
            await _set_state(session, job_id, "done", track_id=track_id)
            return

        # ── 5. Atomic place ────────────────────────────────────────────────
        await _set_state(session, job_id, "importing")
        try:
            atomic_place(audio_path, dest)
        except Exception as exc:
            await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
            logger.error("Placement failed %s: %s", job_id, exc)
            return

        # ── 5b. Embed artwork after placement ──────────────────────────────
        if artwork_bytes:
            try:
                write_tags(dest, artwork_bytes=artwork_bytes)
            except Exception as art_exc:
                logger.debug("Artwork embed failed: %s", art_exc)

        # ── 6. Index in DB ─────────────────────────────────────────────────
        # Scanner creates the row with a hash-based ID; we backfill MB fields after.
        hash_track_id = make_id(artist=artist, title=title, duration_seconds=duration)
        try:
            await index_file(session, dest)
            await session.flush()

            if mb_recording_id or artwork_bytes is not None:
                from service.db.schema import Track as _Track
                track_row = await session.get(_Track, hash_track_id)
                if track_row is not None:
                    if mb_recording_id:
                        track_row.musicbrainz_recording_id = mb_recording_id
                    hca = await asyncio.to_thread(has_cover_art, dest)
                    if track_row.file:
                        track_row.file.has_cover_art = hca
                    track_row.tag_quality_score = compute_quality_score(
                        title=title,
                        artist=artist,
                        album=album,
                        year=year,
                        track_number=track_number,
                        musicbrainz_recording_id=mb_recording_id,
                        has_cover_art=hca,
                    )
                    await session.flush()
        except Exception as exc:
            logger.warning("DB index failed for %s: %s", dest, exc)

        # ── 7. Trigger Navidrome scan ──────────────────────────────────────
        try:
            await scan_trigger()
        except Exception as exc:
            logger.warning("Navidrome scan trigger failed: %s", exc)

        await _set_state(session, job_id, "done", track_id=hash_track_id)
        logger.info("Acquisition done: %s → %s", job_id, dest)
