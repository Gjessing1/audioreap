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
from service.core.identity import make_id
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow
from service.index.scanner import index_file
from service.library.layout import track_path
from service.library.tagger import read_tags
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
        await _set_state(session, job_id, "tagging")
        tagged = read_tags(audio_path)
        title = (tagged.title if tagged else None) or candidate.title
        artist = (tagged.artist if tagged else None) or candidate.artist
        album = (tagged.album if tagged else None) or candidate.album
        year = (tagged.year if tagged else None)
        track_number = (tagged.track_number if tagged else None)
        disc_number = (tagged.disc_number if tagged else None)
        duration = (tagged.duration_seconds if tagged else None) or candidate.duration_seconds

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

        # ── 6. Index in DB ─────────────────────────────────────────────────
        try:
            await index_file(session, dest)
            await session.flush()
        except Exception as exc:
            logger.warning("DB index failed for %s: %s", dest, exc)

        track_id = make_id(artist=artist, title=title, duration_seconds=duration)

        # ── 7. Trigger Navidrome scan ──────────────────────────────────────
        try:
            await scan_trigger()
        except Exception as exc:
            logger.warning("Navidrome scan trigger failed: %s", exc)

        await _set_state(session, job_id, "done", track_id=track_id)
        logger.info("Acquisition done: %s → %s", job_id, dest)
