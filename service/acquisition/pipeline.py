"""Acquisition pipeline: download → identify (needs_review) → approve → place → index → scan.

Phase 1 (run_acquisition): download, remux, fingerprint, MB lookup, place in staging,
store resolved_metadata_json on job row, set state needs_review.

Phase 2 (place_approved_track): called from the API when the user approves the review.
Writes final tags, moves file from staging to /music, indexes, triggers Navidrome scan.
"""
from __future__ import annotations

import asyncio
import json
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
from service.library.tagger import has_cover_art, read_tags, write_cover_jpg, write_tags
from service.library.writer import atomic_place
from service.metadata.quality import compute_quality_score
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
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Phase 1 (identify): download, fingerprint, MB lookup, stage for review.

    Places the file in /music-staging and stores resolved_metadata_json on the
    job row. Sets state to needs_review. Never raises — errors go to the job row.
    """
    # ── 0. Dedup check ────────────────────────────────────────────────────────
    try:
        local_match = await _find_local_match(session, candidate)
        if local_match is not None:
            logger.info("Dedup: skipping — local match exists: %s", local_match)
            await _set_state(session, job_id, "done", track_id=local_match)
            return
    except Exception as exc:
        logger.debug("Dedup check failed (continuing): %s", exc)

    tmp_acquire_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=tmp_acquire_dir) as tmp_str:
        tmp_dir = Path(tmp_str)

        # ── 1. Download ────────────────────────────────────────────────────────
        await _set_state(session, job_id, "downloading")
        try:
            fetch_result = await provider.fetch(provider_ref, tmp_dir, on_progress=on_progress)
        except Exception as exc:
            fc, err = classify_failure(exc)
            await _set_state(session, job_id, "failed", failure_class=fc, error=err)
            logger.error("Download failed [%s] %s: %s", fc, job_id, err)
            return

        audio_path = fetch_result.file_path
        _rm = fetch_result.raw_metadata

        def _rm_str(key: str) -> str | None:
            v = _rm.get(key)
            s = str(v).strip() if v is not None else ""
            return s if s and s.lower() not in ("none", "unknown") else None

        _artist_from_meta = _rm_str("artist")
        _title_raw = _rm_str("track") or _rm_str("title")
        _uploader = _rm_str("uploader") or _rm_str("channel")

        if _artist_from_meta:
            _fetch_title = _title_raw
            _fetch_artist = _artist_from_meta
        elif _title_raw and " - " in _title_raw:
            _split = _title_raw.split(" - ", 1)
            _fetch_artist = _split[0].strip()
            _fetch_title = _split[1].strip()
        else:
            _fetch_title = _title_raw
            _fetch_artist = _uploader

        _fetch_album = _rm_str("album")
        _ry = _rm.get("release_year")
        _fetch_year: int | None = int(_ry) if isinstance(_ry, (int, float)) and _ry else None

        # ── 2. Remux if needed ─────────────────────────────────────────────────
        await _set_state(session, job_id, "processing")
        if audio_path.suffix.lower() in _REMUX_CONTAINERS:
            try:
                audio_path = await _remux_to_ogg(audio_path, tmp_dir)
            except Exception as exc:
                await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
                logger.error("Remux failed %s: %s", job_id, exc)
                return

        # ── 3. Read tags + merge ───────────────────────────────────────────────
        await _set_state(session, job_id, "tagging")
        tagged = read_tags(audio_path)
        title = (tagged.title if tagged else None) or _fetch_title or candidate.title
        artist = (tagged.artist if tagged else None) or _fetch_artist or candidate.artist
        album = (tagged.album if tagged else None) or _fetch_album or candidate.album
        year = (tagged.year if tagged else None) or _fetch_year
        track_number = (tagged.track_number if tagged else None)
        disc_number = (tagged.disc_number if tagged else None)
        duration = (tagged.duration_seconds if tagged else None) or candidate.duration_seconds

        # When album coordinator locked album+track_number, treat them as authoritative
        candidate_album_locked = bool(candidate.album and candidate.track_number)

        # ── 3a. Wrong-track detection (duration delta) ─────────────────────────
        force_staging_reason: str | None = None
        _got_dur = tagged.duration_seconds if tagged else None
        if candidate_album_locked and candidate.duration_seconds and _got_dur:
            _delta = abs(_got_dur - candidate.duration_seconds)
            _tol = max(30, int(candidate.duration_seconds * 0.2))
            if _delta > _tol:
                force_staging_reason = (
                    f"Duration mismatch: expected ~{candidate.duration_seconds}s, "
                    f"got {_got_dur}s — may be wrong track"
                )
                logger.warning("Job %s %r: %s", job_id, candidate.title, force_staging_reason)

        # Apply candidate's pre-resolved fields (from album coordinator)
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
        mb_release_group_id: str | None = None
        acoustid_confidence: float | None = None
        mb_match_source: str | None = None

        # ── 3b. AcoustID fingerprint + MB lookup ───────────────────────────────
        try:
            from service.metadata.acoustid import acoustid_to_mbid
            from service.metadata.musicbrainz import get_recording_by_id, lookup_recording

            mb: object = None
            mb_from_acoustid = False

            if settings.acoustid_api_key:
                acoustid_result = await acoustid_to_mbid(audio_path, settings.acoustid_api_key)
                if acoustid_result:
                    acoustid_mbid, acoustid_confidence = acoustid_result
                    mb = await asyncio.to_thread(
                        get_recording_by_id, acoustid_mbid, settings.cache_dir
                    )
                    if mb is not None:
                        mb_from_acoustid = True
                        mb_match_source = "acoustid"
                    # Fingerprint says different recording than album coordinator expected
                    if candidate.mb_recording_id and acoustid_mbid != candidate.mb_recording_id:
                        mismatch_note = (
                            f"Fingerprint mismatch: expected {candidate.mb_recording_id[:8]}…, "
                            f"got {acoustid_mbid[:8]}…"
                        )
                        force_staging_reason = (
                            f"{force_staging_reason} | {mismatch_note}"
                            if force_staging_reason else mismatch_note
                        )
                        logger.warning(
                            "Job %s %r: AcoustID mismatch (expected %s, got %s)",
                            job_id, title, candidate.mb_recording_id, acoustid_mbid,
                        )

            if mb is None:
                mb = await asyncio.to_thread(
                    lookup_recording, title, artist, duration, cache_dir=settings.cache_dir,
                )
                if mb is not None:
                    mb_match_source = "text_search"

            if mb is not None:
                resolved_recording_id = mb.recording_id  # type: ignore[union-attr]
                # When album coordinator locked a recording ID, only override via AcoustID fingerprint
                if candidate.mb_recording_id and not mb_from_acoustid:
                    if resolved_recording_id != candidate.mb_recording_id:
                        logger.info(
                            "Text search returned different recording %s (expected %s) for %r "
                            "— keeping locked recording_id",
                            resolved_recording_id, candidate.mb_recording_id, title,
                        )
                        resolved_recording_id = candidate.mb_recording_id

                mb_recording_id = resolved_recording_id
                mb_release_id = mb_release_id or mb.release_id  # type: ignore[union-attr]
                mb_artist_id = mb.artist_id  # type: ignore[union-attr]
                mb_artist_sort = mb.artist_sort  # type: ignore[union-attr]
                mb_original_year = mb.original_year  # type: ignore[union-attr]
                mb_release_group_id = mb.release_group_id  # type: ignore[union-attr]
                title = mb.title or title  # type: ignore[union-attr]
                artist = mb.artist or artist  # type: ignore[union-attr]
                if not candidate_album_locked:
                    album = mb.album or album  # type: ignore[union-attr]
                    year = mb.year or year  # type: ignore[union-attr]
                    track_number = mb.track_number or track_number  # type: ignore[union-attr]

        except Exception as mb_exc:
            logger.debug("MB lookup skipped: %s", mb_exc)

        # Fetch MB folksonomy genres for the review card
        mb_genres: list[str] = []
        if mb_release_group_id:
            try:
                from service.metadata.musicbrainz import get_release_group_genres
                mb_genres = await asyncio.to_thread(
                    get_release_group_genres, mb_release_group_id, settings.cache_dir
                )
            except Exception as genre_exc:
                logger.debug("Genre fetch skipped: %s", genre_exc)

        albumartist = candidate.artist if candidate_album_locked else artist
        is_compilation = (album is not None) and albumartist.lower() in ("various artists", "various")

        quality_score = compute_quality_score(
            title=title,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            musicbrainz_recording_id=mb_recording_id,
            has_cover_art=False,
        )

        # ── 4. Place in staging (holding area for review) ─────────────────────
        await _set_state(session, job_id, "importing")

        ext = audio_path.suffix.lstrip(".")
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
            await asyncio.to_thread(atomic_place, audio_path, staging_dest)
        except Exception as exc:
            await _set_state(session, job_id, "failed", failure_class="transient", error=str(exc))
            logger.error("Staging placement failed %s: %s", job_id, exc)
            return

        # ── 5. Store resolved metadata → needs_review ─────────────────────────
        resolved_metadata = {
            "title": title,
            "artist": artist,
            "albumartist": albumartist,
            "album": album,
            "year": year,
            "original_year": mb_original_year,
            "track_number": track_number,
            "disc_number": disc_number,
            "duration_seconds": duration,
            "ext": ext,
            "mb_recording_id": mb_recording_id,
            "mb_release_id": mb_release_id,
            "mb_artist_id": mb_artist_id,
            "mb_artist_sort": mb_artist_sort,
            "acoustid_confidence": acoustid_confidence,
            "mb_match_source": mb_match_source,
            "is_compilation": is_compilation,
            "force_staging_reason": force_staging_reason,
            "quality_score": quality_score,
            "thumbnail_url": candidate.thumbnail_url,
            "mb_genres": mb_genres,
        }

        row = await session.get(AcquisitionJobRow, job_id)
        if row is not None:
            row.state = "needs_review"
            row.staging_path = str(staging_dest)
            row.resolved_metadata_json = json.dumps(resolved_metadata)
            if force_staging_reason:
                row.error = force_staging_reason
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await session.flush()

        logger.info(
            "Identify done (source=%s, quality=%.0f%%): %s → staged at %s",
            mb_match_source or "none", quality_score * 100, job_id, staging_dest,
        )


async def place_approved_track(
    job_id: str,
    overrides: dict[str, str | None],
    session: AsyncSession,
    scan_trigger: ScanTrigger | None = None,
) -> Path:
    """Phase 2 (place): write tags, move staging → /music, index, scan.

    Called from the API when the user approves a needs_review job.
    User overrides from the review form take precedence over stored metadata.
    Raises on file errors so the caller can keep the job in needs_review for retry.
    """
    from service.db.schema import Track as _Track
    from service.navidrome.client import trigger_scan as _trigger_scan

    if scan_trigger is None:
        scan_trigger = _trigger_scan

    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise ValueError(f"Job {job_id} not found")
    if row.state != "needs_review":
        raise ValueError(f"Job {job_id} is in state {row.state!r}, expected needs_review")
    if not row.resolved_metadata_json:
        raise ValueError(f"Job {job_id} has no resolved metadata")
    if not row.staging_path:
        raise ValueError(f"Job {job_id} has no staging path")

    staging_path = Path(row.staging_path)
    if not staging_path.exists():
        raise FileNotFoundError(f"Staged file missing: {staging_path}")

    meta: dict[str, object] = json.loads(row.resolved_metadata_json)

    # Apply user-supplied overrides — non-empty string values win
    for k in ("title", "artist", "album", "mb_recording_id", "mb_release_id", "genre"):
        if k in overrides:
            val = (overrides[k] or "").strip()
            meta[k] = val or None
    for k in ("year", "track_number", "disc_number"):
        if k in overrides:
            raw = (overrides[k] or "").strip()
            if raw:
                try:
                    meta[k] = int(raw)
                except (ValueError, TypeError):
                    pass
            else:
                meta[k] = None

    # Sync albumartist when artist was overridden but albumartist wasn't
    if "artist" in overrides and "albumartist" not in overrides:
        meta["albumartist"] = meta.get("artist")

    is_enrichment: bool = bool(meta.get("is_enrichment", False))
    title: str = str(meta.get("title") or "Unknown")
    artist: str = str(meta.get("artist") or "Unknown")
    albumartist: str = str(meta.get("albumartist") or artist)
    album: str | None = meta.get("album") or None  # type: ignore[assignment]
    year: int | None = meta.get("year")  # type: ignore[assignment]
    original_year: int | None = meta.get("original_year")  # type: ignore[assignment]
    track_number: int | None = meta.get("track_number")  # type: ignore[assignment]
    disc_number: int | None = meta.get("disc_number")  # type: ignore[assignment]
    mb_recording_id: str | None = meta.get("mb_recording_id") or None  # type: ignore[assignment]
    mb_release_id: str | None = meta.get("mb_release_id") or None  # type: ignore[assignment]
    mb_artist_id: str | None = meta.get("mb_artist_id") or None  # type: ignore[assignment]
    mb_artist_sort: str | None = meta.get("mb_artist_sort") or None  # type: ignore[assignment]
    is_compilation: bool = bool(meta.get("is_compilation", False))
    genre: str | None = meta.get("genre") or None  # type: ignore[assignment]
    duration_seconds: int | None = meta.get("duration_seconds")  # type: ignore[assignment]
    ext: str = str(meta.get("ext") or staging_path.suffix.lstrip("."))

    # Write final tags (to staging file for normal; directly to /music file for enrichment)
    try:
        await asyncio.to_thread(
            write_tags,
            staging_path,
            title=title,
            artist=artist,
            albumartist=albumartist,
            album=album,
            year=year,
            original_year=original_year,
            track_number=track_number,
            disc_number=disc_number,
            artist_sort=mb_artist_sort,
            compilation=is_compilation,
            genre=genre,
            mb_recording_id=mb_recording_id,
            mb_release_id=mb_release_id,
            mb_artist_id=mb_artist_id,
        )
    except Exception as exc:
        logger.warning("Approve: tag write failed for %s: %s", staging_path, exc)

    if is_enrichment:
        # File is already in /music — no move needed
        dest = staging_path
    else:
        # Compute final /music destination
        dest = track_path(
            settings.music_dir,
            artist=artist,
            album=album,
            year=year,
            track_number=track_number,
            disc_number=disc_number,
            title=title,
            ext=ext,
            albumartist=albumartist,
        )

        # Idempotency: file already in place
        if dest.exists():
            logger.info("Approve: track already at %s — marking done", dest)
            hash_track_id = make_id(artist=artist, title=title, duration_seconds=duration_seconds)
            row.state = "done"
            row.track_id = hash_track_id
            row.staging_path = None
            row.updated_at = datetime.now(UTC).replace(tzinfo=None)
            await session.flush()
            return dest

        # Atomic place: staging → music
        await asyncio.to_thread(atomic_place, staging_path, dest)

    # Fetch and embed artwork (cached — cheap on second call)
    artwork_bytes: bytes | None = None
    if mb_release_id:
        try:
            from service.metadata.artwork import fetch_artwork
            artwork_bytes = await fetch_artwork(
                release_mbid=mb_release_id,
                thumbnail_url=meta.get("thumbnail_url"),  # type: ignore[arg-type]
                cache_dir=settings.cache_dir,
            )
        except Exception as exc:
            logger.debug("Approve: artwork fetch failed: %s", exc)

    if artwork_bytes:
        try:
            await asyncio.to_thread(write_tags, dest, artwork_bytes=artwork_bytes)
        except Exception as exc:
            logger.debug("Approve: artwork embed failed: %s", exc)
        write_cover_jpg(dest.parent, artwork_bytes)

    # Index in DB using a savepoint so failures don't roll back the outer transaction
    hash_track_id = make_id(artist=artist, title=title, duration_seconds=duration_seconds)
    try:
        async with session.begin_nested():
            await index_file(session, dest)
        hca = await asyncio.to_thread(has_cover_art, dest)
        track_row = await session.get(_Track, hash_track_id)
        if track_row is not None:
            if mb_recording_id:
                track_row.musicbrainz_recording_id = mb_recording_id
            if genre:
                track_row.genre = genre
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
            # Store MB artist ID on Artist row for artist page discography
            if mb_artist_id and track_row.artist_id:
                from service.db.schema import Artist as _Artist
                artist_row = await session.get(_Artist, track_row.artist_id)
                if artist_row is not None and not artist_row.musicbrainz_artist_id:
                    artist_row.musicbrainz_artist_id = mb_artist_id
            await session.flush()
    except Exception as exc:
        logger.warning("Approve: DB index failed for %s: %s", dest, exc)
        # Reset session after any flush/greenlet error so subsequent operations work
        try:
            await session.rollback()
        except Exception:
            pass

    # Navidrome scan
    try:
        await scan_trigger()
    except Exception as exc:
        logger.warning("Approve: Navidrome scan failed: %s", exc)

    row = await session.get(AcquisitionJobRow, job_id)
    if row is not None:
        row.state = "done"
        row.track_id = hash_track_id
        row.staging_path = None
        row.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.flush()

    logger.info("Approved and placed: %s → %s", job_id, dest)
    return dest
