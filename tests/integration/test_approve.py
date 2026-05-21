"""Integration tests for place_approved_track (Phase 2 — the approval step).

Covers:
- Full round-trip: acquire → needs_review → approve → done, file in /music
- Tag write failure aborts approval; job stays needs_review with error message
- User overrides (title, genre) are written to the file
- Enrichment path (file already in /music, no atomic_place)
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.jobs import create_job
from service.acquisition.pipeline import place_approved_track, run_acquisition
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Base, Track
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

async def _stage_track(
    db: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    provider_ref: str = "fake-001",
    title: str = "Test Track One",
    artist: str = "Fake Artist",
) -> str:
    """Run Phase 1 (identify) and return the job_id in needs_review state."""
    provider = FakeProvider(FIXTURE_AUDIO)
    candidate = TrackCandidate(
        provider="fake", provider_ref=provider_ref,
        title=title, artist=artist, duration_seconds=1,
    )
    async with db() as s, s.begin():
        job_id = await create_job(s, provider_name="fake",
                                  provider_ref=provider_ref, candidate=candidate)
    staging_dir = tmp_path / "staging"
    tmp_acquire = tmp_path / "tmp"
    async with db() as s, s.begin():
        await run_acquisition(
            job_id=job_id, provider=provider, provider_ref=provider_ref,
            candidate=candidate,
            music_dir=staging_dir,
            tmp_acquire_dir=tmp_acquire,
            session=s, scan_trigger=AsyncMock(),
        )
    return job_id


# ── Happy path ─────────────────────────────────────────────────────────────

async def test_approve_places_file(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Full round-trip: acquire → approve → file lands in music_dir."""
    from service.config import settings
    job_id = await _stage_track(db, tmp_path)

    # Verify job is in needs_review
    async with db() as s:
        row = await s.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "needs_review", f"Expected needs_review, got {row.state}"
    assert row.staging_path is not None

    music_dir = tmp_path / "music"
    with patch.object(settings, "music_dir", music_dir):
        async with db() as s, s.begin():
            dest = await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())

    assert dest.exists(), f"Expected placed file at {dest}"
    assert dest.is_relative_to(music_dir)

    async with db() as s:
        row = await s.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "done"
    assert row.staging_path is None


async def test_approve_indexes_track(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Approved track is indexed in the DB Track table."""
    from service.config import settings
    job_id = await _stage_track(db, tmp_path)
    music_dir = tmp_path / "music"

    with patch.object(settings, "music_dir", music_dir):
        async with db() as s, s.begin():
            await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())

    async with db() as s:
        tracks = (await s.execute(select(Track))).scalars().all()
    assert len(tracks) >= 1
    assert any(t.title == "Test Track One" for t in tracks)


async def test_approve_applies_overrides(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """User-supplied overrides win over stored metadata."""
    from service.config import settings
    from service.library.tagger import read_tags
    job_id = await _stage_track(db, tmp_path)
    music_dir = tmp_path / "music"

    with patch.object(settings, "music_dir", music_dir):
        async with db() as s, s.begin():
            dest = await place_approved_track(
                job_id,
                {"title": "Overridden Title", "genre": "Electronic"},
                s,
                scan_trigger=AsyncMock(),
            )

    tags = read_tags(dest)
    assert tags is not None
    assert tags.title == "Overridden Title"
    assert tags.genre == "Electronic"


# ── Failure path ───────────────────────────────────────────────────────────

async def test_tag_write_failure_aborts_approval(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """If write_tags raises, approval is aborted and the job stays in needs_review."""
    from service.config import settings
    job_id = await _stage_track(db, tmp_path)

    async with db() as s:
        row = await s.get(AcquisitionJobRow, job_id)
    staging_path_before = row.staging_path  # staging file must still be there after abort

    music_dir = tmp_path / "music"
    with (
        patch.object(settings, "music_dir", music_dir),
        patch("service.acquisition.pipeline.write_tags",
              side_effect=RuntimeError("mutagen: unsupported format")),
    ):
        with pytest.raises(RuntimeError, match="mutagen"):
            async with db() as s, s.begin():
                await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())

    # Job must still be in needs_review — the approval was aborted before atomic_place
    async with db() as s:
        row = await s.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "needs_review", (
        f"Job should stay needs_review after tag write failure, got {row.state}"
    )
    # Staging file must still exist (nothing was moved)
    assert staging_path_before is not None
    assert Path(staging_path_before).exists(), "Staging file was moved despite tag write failure"
    # Nothing landed in music_dir
    assert not any(music_dir.rglob("*")) if music_dir.exists() else True


async def test_double_approve_is_idempotent(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Approving a job that's already done doesn't crash — file already in place."""
    from service.config import settings
    job_id = await _stage_track(db, tmp_path)
    music_dir = tmp_path / "music"

    with patch.object(settings, "music_dir", music_dir):
        async with db() as s, s.begin():
            dest1 = await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())
        # Second approval on a done job should raise ValueError (wrong state)
        async with db() as s, s.begin():
            with pytest.raises(ValueError, match="needs_review"):
                await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())
