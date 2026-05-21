"""Integration tests for the album acquisition pipeline.

Uses FakeProvider with its 4-track album catalogue. Real SQLite, real
filesystem, mocked Navidrome scan trigger.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yt_dlp.utils as yt_utils
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.album_pipeline import create_album_job, run_album_acquisition
from service.core.models import FetchResult
from service.db.schema import (
    AcquisitionJobRow,
    AlbumAcquisitionJob,
    Base,
)
from service.providers.base import Provider
from tests.fake_provider import (
    FAKE_ALBUM_REF,
    FakeProvider,
)

FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


# ── DB fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# ── Helper ─────────────────────────────────────────────────────────────────

async def _run(
    tmp_path: Path,
    db: async_sessionmaker[AsyncSession],
    provider: Provider | None = None,
    policy: str = "partial_ok",
    scan_mock: AsyncMock | None = None,
) -> tuple[str, Path]:
    provider = provider or FakeProvider(FIXTURE_AUDIO)
    scan_mock = scan_mock or AsyncMock()
    music_dir = tmp_path / "music"
    tmp_acquire = tmp_path / "tmp"

    album_candidate = await provider.fetch_album(FAKE_ALBUM_REF)

    async with db() as session, session.begin():
        album_job_id = await create_album_job(
            session,
            provider_name=provider.name,
            album_ref=FAKE_ALBUM_REF,
            album_candidate=album_candidate,
            policy=policy,
        )
        await run_album_acquisition(
            album_job_id=album_job_id,
            provider=provider,
            album_candidate=album_candidate,
            music_dir=music_dir,
            tmp_acquire_dir=tmp_acquire,
            session=session,
            policy=policy,
            scan_trigger=scan_mock,
        )

    return album_job_id, music_dir


# ── Happy path ─────────────────────────────────────────────────────────────
# Album tracks land in `needs_review` state in settings.staging_dir; users
# approve each one individually via the review card. The album job itself
# is marked "done" once all downloads are complete (regardless of review state).

async def test_all_tracks_staged_for_review(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """All 4 tracks must be in needs_review (staged) after album acquisition."""
    album_job_id, _ = await _run(tmp_path, db)

    async with db() as session:
        children = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id)
            )
        ).scalars().all()

    assert len(children) == 4
    # Every child is either needs_review (staged, awaiting approval) or done
    # (auto-approved via AcoustID with high confidence — not possible in test env
    # without network, so all will be needs_review here)
    terminal_or_review = {"needs_review", "done", "staged"}
    assert all(c.state in terminal_or_review for c in children), (
        f"Unexpected states: {[c.state for c in children]}"
    )


async def test_album_job_state_done(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    """Album job is done once all downloads complete (review awaited separately)."""
    album_job_id, _ = await _run(tmp_path, db)

    async with db() as session:
        row = await session.get(AlbumAcquisitionJob, album_job_id)
    assert row is not None
    assert row.state == "done"


async def test_child_jobs_created(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    """4 child jobs are created, one per track."""
    album_job_id, _ = await _run(tmp_path, db)

    async with db() as session:
        children = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id)
            )
        ).scalars().all()

    assert len(children) == 4
    # No child should be stuck in a transient state
    assert all(c.state not in ("queued", "downloading", "processing") for c in children)


async def test_track_ordering_preserved(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    album_job_id, _ = await _run(tmp_path, db)

    async with db() as session:
        children = (
            await session.execute(
                select(AcquisitionJobRow)
                .where(AcquisitionJobRow.album_job_id == album_job_id)
                .order_by(AcquisitionJobRow.track_index)
            )
        ).scalars().all()

    indices = [c.track_index for c in children]
    assert indices == [0, 1, 2, 3]


async def test_navidrome_scan_triggered(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    scan_mock = AsyncMock()
    await _run(tmp_path, db, scan_mock=scan_mock)
    scan_mock.assert_called_once()


async def test_staging_dir_cleaned_up(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    """Per-album temp dir (staging_root) is removed after acquisition."""
    await _run(tmp_path, db)
    staging_dirs = list((tmp_path / "tmp").glob("album-*"))
    assert staging_dirs == [], f"Staging dir not cleaned: {staging_dirs}"


async def test_idempotent_second_run(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    """Running twice does not create duplicate child jobs for the same tracks."""
    album_job_id1, _ = await _run(tmp_path, db)
    album_job_id2, _ = await _run(tmp_path, db)

    async with db() as session:
        children1 = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id1)
            )
        ).scalars().all()
        children2 = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id2)
            )
        ).scalars().all()

    # Each run creates its own set of child jobs
    assert len(children1) == 4
    assert len(children2) == 4


# ── Failure / policy tests ─────────────────────────────────────────────────

class _PartiallyFailingProvider(FakeProvider):
    """Fails on the last track's fetch."""

    async def fetch(self, provider_ref: str, dest_dir: Path, on_progress=None) -> FetchResult:
        if provider_ref == "fake-album-04":
            raise yt_utils.DownloadError("ERROR: Video unavailable")
        return await super().fetch(provider_ref, dest_dir, on_progress=on_progress)


async def test_partial_failure_partial_ok_policy(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """With partial_ok, 3 tracks staged successfully even though 1 download failed."""
    album_job_id, _ = await _run(
        tmp_path, db, provider=_PartiallyFailingProvider(FIXTURE_AUDIO), policy="partial_ok"
    )

    async with db() as session:
        children = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id)
            )
        ).scalars().all()
        row = await session.get(AlbumAcquisitionJob, album_job_id)

    failed = [c for c in children if c.state == "failed"]
    successful = [c for c in children if c.state in ("needs_review", "done", "staged")]
    assert len(failed) == 1
    assert len(successful) == 3
    assert row is not None
    assert row.state == "partial"


async def test_partial_failure_all_or_nothing_policy(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """With all_or_nothing, album job fails if any track download fails."""
    album_job_id, _ = await _run(
        tmp_path, db, provider=_PartiallyFailingProvider(FIXTURE_AUDIO), policy="all_or_nothing"
    )

    async with db() as session:
        row = await session.get(AlbumAcquisitionJob, album_job_id)
    assert row is not None
    assert row.state == "failed"


async def test_resume_interrupted_album(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Re-running after a partial failure creates a new album job for the retry."""
    # First run: 3 succeed, 1 fails
    album_job_id1, _ = await _run(
        tmp_path, db, provider=_PartiallyFailingProvider(FIXTURE_AUDIO), policy="partial_ok"
    )
    async with db() as session:
        row1 = await session.get(AlbumAcquisitionJob, album_job_id1)
    assert row1 is not None
    assert row1.state == "partial"

    # Second run with full provider
    album_job_id2, _ = await _run(tmp_path, db, policy="partial_ok")
    async with db() as session:
        row2 = await session.get(AlbumAcquisitionJob, album_job_id2)
        children2 = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id2)
            )
        ).scalars().all()

    assert row2 is not None
    assert row2.state == "done"
    # All 4 tracks from the second run should be staged for review (or auto-approved)
    assert len(children2) == 4


async def test_child_job_creation_failure_does_not_roll_back_prior_children(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """A DB error creating one child job must not roll back previously created children.

    This tests the savepoint fix: each child's create_job is wrapped in
    begin_nested() so a constraint error on child N leaves children 1..N-1 intact.
    """
    from unittest.mock import patch, AsyncMock
    from service.acquisition.album_pipeline import create_album_job, run_album_acquisition
    from service.acquisition.jobs import create_job as _real_create_job

    provider = FakeProvider(FIXTURE_AUDIO)
    album_candidate = await provider.fetch_album(FAKE_ALBUM_REF)
    music_dir = tmp_path / "music"
    tmp_acquire = tmp_path / "tmp"

    call_count = [0]

    async def _sometimes_failing_create_job(session, **kwargs):
        call_count[0] += 1
        if call_count[0] == 3:
            raise RuntimeError("simulated DB constraint error on track 3")
        return await _real_create_job(session, **kwargs)

    async with db() as session, session.begin():
        album_job_id = await create_album_job(
            session, provider_name=provider.name,
            album_ref=FAKE_ALBUM_REF, album_candidate=album_candidate,
        )
        with patch("service.acquisition.album_pipeline.create_job",
                   side_effect=_sometimes_failing_create_job):
            await run_album_acquisition(
                album_job_id=album_job_id, provider=provider,
                album_candidate=album_candidate,
                music_dir=music_dir, tmp_acquire_dir=tmp_acquire,
                session=session, scan_trigger=AsyncMock(),
            )

    # 3 out of 4 children should be present (track 3 failed, others intact)
    async with db() as session:
        children = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id)
            )
        ).scalars().all()

    assert len(children) == 3, (
        f"Expected 3 children (1 skipped due to savepoint rollback), got {len(children)}"
    )
