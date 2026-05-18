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

async def test_all_tracks_placed(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    album_job_id, music_dir = await _run(tmp_path, db)

    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 4, f"Expected 4 files, got {len(placed)}: {placed}"


async def test_album_job_state_done(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    album_job_id, _ = await _run(tmp_path, db)

    async with db() as session:
        row = await session.get(AlbumAcquisitionJob, album_job_id)
    assert row is not None
    assert row.state == "done"


async def test_child_jobs_created(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    album_job_id, _ = await _run(tmp_path, db)

    async with db() as session:
        children = (
            await session.execute(
                select(AcquisitionJobRow).where(AcquisitionJobRow.album_job_id == album_job_id)
            )
        ).scalars().all()

    assert len(children) == 4
    assert all(c.state == "done" for c in children)


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
    await _run(tmp_path, db)
    staging_dirs = list((tmp_path / "tmp").glob("album-*"))
    assert staging_dirs == [], f"Staging dir not cleaned: {staging_dirs}"


async def test_idempotent_second_run(tmp_path: Path, db: async_sessionmaker[AsyncSession]) -> None:
    """Running twice does not duplicate files."""
    await _run(tmp_path, db)
    _, music_dir = await _run(tmp_path, db)
    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 4


# ── Failure / policy tests ─────────────────────────────────────────────────

class _PartiallyFailingProvider(FakeProvider):
    """Fails on the last track's fetch."""

    async def fetch(self, provider_ref: str, dest_dir: Path) -> FetchResult:
        if provider_ref == "fake-album-04":
            raise yt_utils.DownloadError("ERROR: Video unavailable")
        return await super().fetch(provider_ref, dest_dir)


async def test_partial_failure_partial_ok_policy(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """With partial_ok, the 3 successful tracks are placed even though 1 failed."""
    album_job_id, music_dir = await _run(
        tmp_path, db, provider=_PartiallyFailingProvider(FIXTURE_AUDIO), policy="partial_ok"
    )

    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 3

    async with db() as session:
        row = await session.get(AlbumAcquisitionJob, album_job_id)
    assert row is not None
    assert row.state == "partial"


async def test_partial_failure_all_or_nothing_policy(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """With all_or_nothing, nothing moves to the library if any track fails."""
    album_job_id, music_dir = await _run(
        tmp_path, db, provider=_PartiallyFailingProvider(FIXTURE_AUDIO), policy="all_or_nothing"
    )

    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 0

    async with db() as session:
        row = await session.get(AlbumAcquisitionJob, album_job_id)
    assert row is not None
    assert row.state == "failed"


async def test_resume_interrupted_album(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Re-running after partial failure picks up remaining tracks (partial_ok)."""
    # First run: 3 succeed, 1 fails
    await _run(tmp_path, db, provider=_PartiallyFailingProvider(FIXTURE_AUDIO), policy="partial_ok")

    # Second run with full provider: the missing 4th track is placed
    _, music_dir = await _run(tmp_path, db, policy="partial_ok")
    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 4
