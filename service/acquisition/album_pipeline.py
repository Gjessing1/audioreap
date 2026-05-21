"""Album acquisition pipeline.

Orchestrates multiple child track acquisitions into a staging directory, then
atomically moves the completed album into the music library. Supports two
policies:
  partial_ok       — move completed tracks even if some failed
  all_or_nothing   — abort and leave staging intact if any track fails
"""
from __future__ import annotations

import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from service.acquisition.jobs import create_job
from service.acquisition.pipeline import ScanTrigger, run_acquisition
from service.core.models import AlbumCandidate
from service.db.schema import AcquisitionJobRow, AlbumAcquisitionJob
from service.library.layout import track_path
from service.library.writer import atomic_place
from service.providers.base import Provider

logger = logging.getLogger(__name__)

AlbumPolicy = str  # "partial_ok" | "all_or_nothing"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _set_album_state(session: AsyncSession, album_job_id: str, state: str) -> None:
    row = await session.get(AlbumAcquisitionJob, album_job_id)
    if row:
        row.state = state
        row.updated_at = _now()
        await session.flush()


async def create_album_job(
    session: AsyncSession,
    *,
    provider_name: str,
    album_ref: str,
    album_candidate: AlbumCandidate,
    query: str | None = None,
    policy: AlbumPolicy = "partial_ok",
) -> str:

    album_job_id = str(uuid.uuid4())
    row = AlbumAcquisitionJob(
        id=album_job_id,
        provider=provider_name,
        album_ref=album_ref,
        album_title=album_candidate.album_title,
        album_artist=album_candidate.album_artist,
        track_count=len(album_candidate.tracks),
        state="queued",
        policy=policy,
        query=query or f"{album_candidate.album_artist} — {album_candidate.album_title}",
        candidate_json=album_candidate.model_dump_json(),
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(row)
    await session.flush()
    return album_job_id


async def run_album_acquisition(
    *,
    album_job_id: str,
    provider: Provider,
    album_candidate: AlbumCandidate,
    music_dir: Path,
    tmp_acquire_dir: Path,
    session: AsyncSession,
    policy: AlbumPolicy = "partial_ok",
    scan_trigger: ScanTrigger | None = None,
) -> None:
    """Acquire all tracks in an album.

    Tracks are downloaded into a staging directory, then moved atomically into
    the music library once the policy condition is met.
    """
    if scan_trigger is None:
        from service.navidrome.client import trigger_scan
        scan_trigger = trigger_scan

    await _set_album_state(session, album_job_id, "running")

    staging_root = tmp_acquire_dir / f"album-{album_job_id}"
    staging_root.mkdir(parents=True, exist_ok=True)

    tracks = album_candidate.tracks
    if not tracks:
        await _set_album_state(session, album_job_id, "failed")
        return

    child_job_ids: list[str] = []

    # ── Acquire each track into staging ───────────────────────────────────
    for idx, candidate in enumerate(tracks):
        child_id = await create_job(
            session,
            provider_name=provider.name,
            provider_ref=candidate.provider_ref,
            candidate=candidate,
            query=candidate.title,
        )
        # Stamp the album relationship
        child_row = await session.get(AcquisitionJobRow, child_id)
        if child_row:
            child_row.album_job_id = album_job_id
            child_row.track_index = idx
        await session.flush()
        child_job_ids.append(child_id)

        logger.info(
            "Album %s: acquiring track %d/%d — %s",
            album_job_id, idx + 1, len(tracks), candidate.title,
        )
        await run_acquisition(
            job_id=child_id,
            provider=provider,
            provider_ref=candidate.provider_ref,
            candidate=candidate,
            music_dir=staging_root,
            tmp_acquire_dir=tmp_acquire_dir,
            session=session,
            scan_trigger=lambda: None,  # type: ignore[arg-type,return-value]
        )

    # ── Evaluate outcomes ─────────────────────────────────────────────────
    # needs_review counts as success — track downloaded OK, awaiting user approval
    failed_ids: list[str] = []
    for child_id in child_job_ids:
        row = await session.get(AcquisitionJobRow, child_id)
        if row and row.state == "failed":
            failed_ids.append(child_id)

    success_count = len(child_job_ids) - len(failed_ids)

    if policy == "all_or_nothing" and failed_ids:
        logger.warning(
            "Album %s: %d/%d tracks failed — aborting (all_or_nothing policy)",
            album_job_id, len(failed_ids), len(tracks),
        )
        await _set_album_state(session, album_job_id, "failed")
        return

    if success_count == 0:
        await _set_album_state(session, album_job_id, "failed")
        return

    # ── Move staging into music library ───────────────────────────────────
    # staging_root contains: Artist/[Year - ]Album/NN - Title.ext
    # We move the entire subtree item by item so existing tracks are preserved.
    moved = 0
    for staged_file in staging_root.rglob("*"):
        if not staged_file.is_file():
            continue
        rel = staged_file.relative_to(staging_root)
        dest = music_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            logger.info("Album: skipping existing %s", dest)
            continue
        try:
            atomic_place(staged_file, dest)
            moved += 1
        except OSError as exc:
            logger.warning("Album: failed to move %s: %s", staged_file, exc)

    # Clean up empty staging dir
    try:
        shutil.rmtree(staging_root, ignore_errors=True)
    except Exception:
        pass

    logger.info(
        "Album %s: moved %d files into library", album_job_id, moved
    )

    # ── Index the moved files in DB ───────────────────────────────────────
    from service.index.scanner import scan
    try:
        album_dir = track_path(
            music_dir,
            artist=album_candidate.album_artist,
            album=album_candidate.album_title,
            year=None,
            track_number=None,
            disc_number=None,
            title="",
            ext="",
        ).parent
        if album_dir.exists():
            await scan(session, album_dir, incremental=False)
    except Exception as exc:
        logger.warning("Album: DB indexing failed: %s", exc)

    # ── Navidrome scan ────────────────────────────────────────────────────
    try:
        await scan_trigger()
    except Exception as exc:
        logger.warning("Album: Navidrome scan trigger failed: %s", exc)

    final_state = "done" if not failed_ids else "partial"
    await _set_album_state(session, album_job_id, final_state)
    logger.info(
        "Album acquisition %s: %s (%d/%d tracks)",
        album_job_id, final_state, success_count, len(tracks),
    )
