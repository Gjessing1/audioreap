"""Integration tests for the acquisition pipeline.

Uses FakeProvider and real SQLite + real filesystem (tmp_path).
Navidrome scan is replaced by a mock callable to verify it was triggered.
No arq, no Redis — calls run_acquisition() directly.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yt_dlp.utils as yt_utils
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.pipeline import run_acquisition
from service.core.models import FetchResult, ProviderHealth, SearchQuery, TrackCandidate
from service.db.schema import AcquisitionJobRow, Base, Track, TrackFile
from service.providers.base import Provider, ProviderCapabilities
from tests.fake_provider import FakeProvider

FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


# ── DB fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider_ref: str = "fake-001",
    query: str = "Test Track One",
) -> tuple[str, TrackCandidate]:
    return provider_ref, TrackCandidate(
        provider="fake",
        provider_ref=provider_ref,
        title="Test Track One",
        artist="Fake Artist",
        album="Fake Album",
        duration_seconds=1,
    )


async def _insert_job(
    session_factory: async_sessionmaker[AsyncSession],
    candidate: TrackCandidate,
    provider_ref: str,
) -> str:
    from service.acquisition.jobs import create_job
    async with session_factory() as s, s.begin():
        return await create_job(
            s,
            provider_name="fake",
            provider_ref=provider_ref,
            candidate=candidate,
            query="test query",
        )


# ── Happy path ─────────────────────────────────────────────────────────────

async def test_pipeline_places_file(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    tmp_acquire = tmp_path / "tmp"
    provider = FakeProvider(FIXTURE_AUDIO)
    candidate = TrackCandidate(
        provider="fake", provider_ref="fake-001",
        title="Test Track One", artist="Fake Artist", duration_seconds=1,
    )
    scan_mock = AsyncMock()

    job_id = await _insert_job(db, candidate, "fake-001")

    async with db() as session, session.begin():
        await run_acquisition(
            job_id=job_id,
            provider=provider,
            provider_ref="fake-001",
            candidate=candidate,
            music_dir=music_dir,
            tmp_acquire_dir=tmp_acquire,
            session=session,
            scan_trigger=scan_mock,
        )

    # File exists in library
    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 1

    # Job state is done
    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "done"
    assert row.failure_class is None

    # Navidrome scan was triggered
    scan_mock.assert_called_once()


async def test_pipeline_indexes_track_in_db(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    tmp_acquire = tmp_path / "tmp"
    provider = FakeProvider(FIXTURE_AUDIO)
    candidate = TrackCandidate(
        provider="fake", provider_ref="fake-001",
        title="Test Track One", artist="Fake Artist", duration_seconds=1,
    )

    job_id = await _insert_job(db, candidate, "fake-001")
    async with db() as session, session.begin():
        await run_acquisition(
            job_id=job_id, provider=provider, provider_ref="fake-001",
            candidate=candidate, music_dir=music_dir, tmp_acquire_dir=tmp_acquire,
            session=session, scan_trigger=AsyncMock(),
        )

    async with db() as session:
        tracks = (await session.execute(select(Track))).scalars().all()
        files = (await session.execute(select(TrackFile))).scalars().all()

    assert len(tracks) == 1
    assert len(files) == 1
    assert tracks[0].title == "Test Track One"


async def test_pipeline_idempotent(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Running acquisition twice for the same ref must not duplicate files or DB rows."""
    music_dir = tmp_path / "music"
    tmp_acquire = tmp_path / "tmp"
    provider = FakeProvider(FIXTURE_AUDIO)
    candidate = TrackCandidate(
        provider="fake", provider_ref="fake-001",
        title="Test Track One", artist="Fake Artist", duration_seconds=1,
    )

    for _ in range(2):
        job_id = await _insert_job(db, candidate, "fake-001")
        async with db() as session, session.begin():
            await run_acquisition(
                job_id=job_id, provider=provider, provider_ref="fake-001",
                candidate=candidate, music_dir=music_dir, tmp_acquire_dir=tmp_acquire,
                session=session, scan_trigger=AsyncMock(),
            )

    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 1

    async with db() as session:
        files = (await session.execute(select(TrackFile))).scalars().all()
    assert len(files) == 1


# ── Failure injection ──────────────────────────────────────────────────────

class _FailingProvider(Provider):
    name = "failing"
    capabilities = ProviderCapabilities(
        supports_search=True, supports_album_search=False,
        supports_quality_selection=False, search_is_async=False,
        requires_credentials=False,
    )

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def search(self, query: SearchQuery) -> AsyncGenerator[TrackCandidate, None]:  # type: ignore[override]
        return
        yield  # make it an async generator

    async def fetch(self, provider_ref: str, dest_dir: Path) -> FetchResult:
        raise self._exc

    async def health_check(self) -> ProviderHealth:
        from datetime import UTC, datetime
        return ProviderHealth(healthy=False, checked_at=datetime.now(UTC))


async def _run_with_failing(
    tmp_path: Path,
    db: async_sessionmaker[AsyncSession],
    exc: Exception,
) -> AcquisitionJobRow:
    candidate = TrackCandidate(
        provider="failing", provider_ref="x",
        title="Song", artist="Artist", duration_seconds=1,
    )
    job_id = await _insert_job(db, candidate, "x")
    provider = _FailingProvider(exc)

    async with db() as session, session.begin():
        await run_acquisition(
            job_id=job_id, provider=provider, provider_ref="x",
            candidate=candidate, music_dir=tmp_path / "music",
            tmp_acquire_dir=tmp_path / "tmp", session=session,
            scan_trigger=AsyncMock(),
        )

    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    return row


async def test_permanent_download_failure(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    row = await _run_with_failing(
        tmp_path, db, yt_utils.DownloadError("ERROR: Video unavailable")
    )
    assert row.state == "failed"
    assert row.failure_class == "permanent"
    assert row.error is not None


async def test_transient_download_failure(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    row = await _run_with_failing(
        tmp_path, db, yt_utils.DownloadError("ERROR: HTTP Error 429")
    )
    assert row.state == "failed"
    assert row.failure_class == "transient"


async def test_connection_error_is_transient(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    row = await _run_with_failing(tmp_path, db, ConnectionError("Connection reset"))
    assert row.state == "failed"
    assert row.failure_class == "transient"


async def test_scan_trigger_failure_does_not_fail_job(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Navidrome scan failure must not roll back the acquisition."""
    music_dir = tmp_path / "music"
    tmp_acquire = tmp_path / "tmp"
    provider = FakeProvider(FIXTURE_AUDIO)
    candidate = TrackCandidate(
        provider="fake", provider_ref="fake-001",
        title="Test Track One", artist="Fake Artist", duration_seconds=1,
    )

    scan_mock = AsyncMock(side_effect=RuntimeError("navidrome down"))
    job_id = await _insert_job(db, candidate, "fake-001")

    async with db() as session, session.begin():
        await run_acquisition(
            job_id=job_id, provider=provider, provider_ref="fake-001",
            candidate=candidate, music_dir=music_dir, tmp_acquire_dir=tmp_acquire,
            session=session, scan_trigger=scan_mock,
        )

    placed = list(music_dir.rglob("*.wav"))
    assert len(placed) == 1

    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "done"
