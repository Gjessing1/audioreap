"""Integration tests for the acquisition pipeline (Phase 1 — identify/stage).

Uses FakeProvider and real SQLite + real filesystem (tmp_path).
No arq, no Redis — calls run_acquisition() directly.

Post-review-gate behavior:
- run_acquisition() stages the file and sets state to "needs_review"
- The Track table is NOT populated until place_approved_track() is called
- scan_trigger is only called by place_approved_track(), not run_acquisition()
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yt_dlp.utils as yt_utils
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.pipeline import run_acquisition
from service.core.models import FetchResult, ProviderHealth, SearchQuery, TrackCandidate
from service.db.schema import AcquisitionJobRow, Base
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


async def _run(
    db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    candidate: TrackCandidate,
    provider: Provider | None = None,
    provider_ref: str = "fake-001",
    scan_mock: AsyncMock | None = None,
) -> str:
    provider = provider or FakeProvider(FIXTURE_AUDIO)
    scan_mock = scan_mock or AsyncMock()
    job_id = await _insert_job(db, candidate, provider_ref)
    async with db() as session, session.begin():
        await run_acquisition(
            job_id=job_id,
            provider=provider,
            provider_ref=provider_ref,
            candidate=candidate,
            tmp_acquire_dir=tmp_path / "tmp",
            session=session,
            scan_trigger=scan_mock,
        )
    return job_id


_CANDIDATE = TrackCandidate(
    provider="fake", provider_ref="fake-001",
    title="Test Track One", artist="Fake Artist", duration_seconds=1,
)


# ── Happy path (review gate) ───────────────────────────────────────────────

async def test_pipeline_stages_file(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """run_acquisition stages the file and sets state=needs_review."""
    job_id = await _run(db, tmp_path, _CANDIDATE)

    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "needs_review", f"Expected needs_review, got {row.state}"
    assert row.staging_path is not None
    assert Path(row.staging_path).exists(), "Staged file must exist on disk"
    assert row.resolved_metadata_json is not None, "Metadata must be stored for review"


async def test_pipeline_metadata_stored(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """resolved_metadata_json is populated for the review card."""
    import json
    job_id = await _run(db, tmp_path, _CANDIDATE)

    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    meta = json.loads(row.resolved_metadata_json)
    assert meta["title"] == "Test Track One"
    assert meta["artist"] == "Fake Artist"


async def test_pipeline_does_not_place_in_music_dir(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Phase 1 must NOT place files in /music — that is Phase 2 (approval)."""
    music_dir = tmp_path / "music"
    job_id = await _run(db, tmp_path, _CANDIDATE)
    placed = list(music_dir.rglob("*")) if music_dir.exists() else []
    assert placed == [], f"Files must not land in /music until approved: {placed}"


async def test_pipeline_no_track_row_until_approved(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Track is not indexed in the Track table until place_approved_track() runs."""
    from service.db.schema import Track
    job_id = await _run(db, tmp_path, _CANDIDATE)
    async with db() as session:
        tracks = (await session.execute(select(Track))).scalars().all()
    assert tracks == [], "Track table must be empty before approval"


async def test_pipeline_dedup_skips_already_owned(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """If a confident local match exists in Track table, acquisition is skipped."""
    from service.index.scanner import index_file
    from tests.integration.test_dedup import _make_wav, _tag_wav
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    existing = music_dir / "track.wav"
    _make_wav(existing)
    _tag_wav(existing, "Test Track One", "Fake Artist")
    async with db() as s, s.begin():
        await index_file(s, existing)

    provider = FakeProvider(FIXTURE_AUDIO)
    fetch_mock = AsyncMock(wraps=provider.fetch)

    with patch.object(provider, "fetch", fetch_mock):
        job_id = await _run(db, tmp_path, _CANDIDATE, provider=provider)

    fetch_mock.assert_not_called()
    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "done"


async def test_pipeline_two_runs_create_two_staged_jobs(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Without an approved track in Track table, two runs stage two files.

    Dedup only skips if the track is already in the Track table (approved).
    During review it's only staged — so a second download is allowed.
    """
    job_id1 = await _run(db, tmp_path, _CANDIDATE)
    job_id2 = await _run(db, tmp_path, _CANDIDATE)

    async with db() as session:
        row1 = await session.get(AcquisitionJobRow, job_id1)
        row2 = await session.get(AcquisitionJobRow, job_id2)
    assert row1 is not None and row1.state == "needs_review"
    assert row2 is not None and row2.state == "needs_review"


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
        yield

    async def fetch(self, provider_ref: str, dest_dir: Path, on_progress=None) -> FetchResult:
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
            candidate=candidate,
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


async def test_failure_does_not_place_files(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """A failed download must not leave any files in /music or staging."""
    music_dir = tmp_path / "music"
    await _run_with_failing(
        tmp_path, db, yt_utils.DownloadError("ERROR: Video unavailable")
    )
    placed = list(music_dir.rglob("*")) if music_dir.exists() else []
    assert placed == []
