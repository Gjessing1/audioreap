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
from service.library.tagger import (
    compute_replaygain, has_cover_art, read_tags,
    write_cover_jpg, write_replaygain, write_tags,
)
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
) -> Path | None:
    """Returns the final library path on success, None on failure/staging."""
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

        # ── 3a. MusicBrainz lookup — AcoustID first, text search fallback ────
        #
        # When the candidate already has album+track_number (set by the album
        # coordinator job from MB discography data), those are authoritative for
        # path placement. MB match is still done for recording_id/artwork, but
        # we never let it move the file to a different album folder.
        candidate_album_locked = bool(candidate.album and candidate.track_number)

        # Apply candidate's pre-resolved fields as defaults before MB lookup
        if candidate.album:
            album = candidate.album
        if candidate.year:
            year = candidate.year
        if candidate.track_number:
            track_number = candidate.track_number

        mb_recording_id: str | None = candidate.mb_recording_id
        mb_release_id: str | None = candidate.mb_release_id
        mb_artist_id: str | None = None
        mb_artist_sort: str | None = None
        mb_original_year: int | None = None
        artwork_bytes: bytes | None = None
        try:
            from service.metadata.acoustid import acoustid_to_mbid
            from service.metadata.musicbrainz import get_recording_by_id, lookup_recording

            mb: object = None
            if settings.acoustid_api_key:
                acoustid_mbid = await acoustid_to_mbid(audio_path, settings.acoustid_api_key)
                if acoustid_mbid:
                    mb = await asyncio.to_thread(
                        get_recording_by_id, acoustid_mbid, settings.cache_dir
                    )

            if mb is None:
                mb = await asyncio.to_thread(
                    lookup_recording, title, artist, duration,
                    cache_dir=settings.cache_dir,
                )

            if mb is not None:
                mb_recording_id = mb.recording_id  # type: ignore[union-attr]
                mb_release_id = mb_release_id or mb.release_id  # type: ignore[union-attr]
                mb_artist_id = mb.artist_id  # type: ignore[union-attr]
                mb_artist_sort = mb.artist_sort  # type: ignore[union-attr]
                mb_original_year = mb.original_year  # type: ignore[union-attr]
                # Always take title/artist corrections from MB
                title = mb.title or title  # type: ignore[union-attr]
                artist = mb.artist or artist  # type: ignore[union-attr]
                # Only take album/year/track_number from MB when candidate doesn't
                # have them locked (i.e., came from a discography album acquire).
                if not candidate_album_locked:
                    album = mb.album or album  # type: ignore[union-attr]
                    year = mb.year or year  # type: ignore[union-attr]
                    track_number = mb.track_number or track_number  # type: ignore[union-attr]
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

        # Determine albumartist (always set — prevents Navidrome album splits)
        albumartist = artist
        is_compilation = (album is not None) and albumartist.lower() in ("various artists", "various")

        # Always write the final resolved tags (candidate fallback or MB-enriched)
        try:
            write_tags(
                audio_path,
                title=title,
                artist=artist,
                albumartist=albumartist,
                album=album,
                year=year,
                original_year=mb_original_year,
                track_number=track_number,
                disc_number=disc_number,
                artist_sort=mb_artist_sort,
                compilation=is_compilation,
                mb_recording_id=mb_recording_id,
                mb_release_id=mb_release_id,
                mb_artist_id=mb_artist_id,
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
            albumartist=albumartist,
        )

        # ── 4. Idempotency check ───────────────────────────────────────────
        if dest.exists():
            logger.info("Track already exists at %s, skipping", dest)
            track_id = make_id(artist=artist, title=title, duration_seconds=duration)
            await _set_state(session, job_id, "done", track_id=track_id)
            return

        # ── 4b. Pre-placement quality score for staging decision ───────────
        pre_score = compute_quality_score(
            title=title,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            musicbrainz_recording_id=mb_recording_id,
            has_cover_art=artwork_bytes is not None,
        )
        threshold = settings.staging_quality_threshold
        use_staging = (
            threshold > 0
            and pre_score < threshold
            and settings.staging_dir != music_dir
        )

        # ── 5. Atomic place (music or staging) ────────────────────────────
        await _set_state(session, job_id, "importing")

        if use_staging:
            # Place in staging — Navidrome won't see this until approved
            staging_dest = track_path(
                settings.staging_dir,
                artist=artist,
                album=album,
                year=year,
                track_number=track_number,
                disc_number=disc_number,
                title=title,
                ext=ext,
                albumartist=albumartist,
            )
            try:
                atomic_place(audio_path, staging_dest)
            except Exception as exc:
                await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
                logger.error("Staging placement failed %s: %s", job_id, exc)
                return
            if artwork_bytes:
                try:
                    write_tags(staging_dest, artwork_bytes=artwork_bytes)
                except Exception:
                    pass
                write_cover_jpg(staging_dest.parent, artwork_bytes)
            row = await session.get(AcquisitionJobRow, job_id)
            if row is not None:
                row.state = "staged"
                row.staging_path = str(staging_dest)
                row.updated_at = datetime.now(UTC).replace(tzinfo=None)
                await session.flush()
            logger.info(
                "Staged (quality=%.0f%% < %.0f%%): %s → %s",
                pre_score * 100, threshold * 100, job_id, staging_dest,
            )
            return

        try:
            atomic_place(audio_path, dest)
        except Exception as exc:
            await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
            logger.error("Placement failed %s: %s", job_id, exc)
            return

        # ── 5b. Embed artwork + write cover.jpg sidecar ────────────────────
        if artwork_bytes:
            try:
                write_tags(dest, artwork_bytes=artwork_bytes)
            except Exception as art_exc:
                logger.debug("Artwork embed failed: %s", art_exc)
            write_cover_jpg(dest.parent, artwork_bytes)

        # ── 6. Index in DB ─────────────────────────────────────────────────
        # Use a savepoint so index failures don't roll back the outer transaction
        # (which is needed for the final _set_state call).
        hash_track_id = make_id(artist=artist, title=title, duration_seconds=duration)
        try:
            async with session.begin_nested():
                await index_file(session, dest)

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
        return dest
