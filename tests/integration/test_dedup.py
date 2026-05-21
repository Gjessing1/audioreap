"""Integration test: dedup check identifies a local match before acquisition."""
from __future__ import annotations

import struct
import wave
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.jobs import create_job
from service.acquisition.pipeline import run_acquisition
from service.core.models import TrackCandidate
from service.db.schema import AcquisitionJobRow, Base
from tests.fake_provider import FakeProvider

FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _make_wav(path: Path) -> None:
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(struct.pack("<44100h", *([0] * 44100)))


def _tag_wav(path: Path, title: str, artist: str) -> None:
    from mutagen.id3 import TIT2, TPE1
    from mutagen.wave import WAVE
    audio = WAVE(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[title]))  # type: ignore[union-attr]
    audio.tags.add(TPE1(encoding=3, text=[artist]))  # type: ignore[union-attr]
    audio.save()


async def _scan_file(db: async_sessionmaker[AsyncSession], music_dir: Path, path: Path) -> None:
    from service.index.scanner import index_file
    async with db() as session, session.begin():
        await index_file(session, path)


async def test_dedup_skips_acquisition_for_local_match(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """If a local track matches the candidate, acquisition is skipped."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # Seed local library with "Around the World" by Daft Punk
    existing = music_dir / "daft_punk.wav"
    _make_wav(existing)
    _tag_wav(existing, "Around the World", "Daft Punk")
    await _scan_file(db, music_dir, existing)

    # Candidate: same track from provider (with noise in title).
    # duration_seconds=None because the provider search result may not have it;
    # the dedup check should still match on title+artist confidence.
    candidate = TrackCandidate(
        provider="fake",
        provider_ref="fake-001",
        title="Around the World (Official Video)",
        artist="Daft Punk",
        duration_seconds=None,
    )

    provider = FakeProvider(FIXTURE_AUDIO)
    fetch_mock = AsyncMock(wraps=provider.fetch)

    async with db() as session, session.begin():
        job_id = await create_job(
            session,
            provider_name="fake",
            provider_ref="fake-001",
            candidate=candidate,
            query="Around the World Daft Punk",
        )
        with patch.object(provider, "fetch", fetch_mock):
            await run_acquisition(
                job_id=job_id,
                provider=provider,
                provider_ref="fake-001",
                candidate=candidate,
                music_dir=music_dir,
                tmp_acquire_dir=tmp_path / "tmp",
                session=session,
                scan_trigger=AsyncMock(),
            )

    # fetch() must NOT have been called — dedup caught it first
    fetch_mock.assert_not_called()

    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    assert row.state == "done"

    # No new files added
    wav_files = list(music_dir.rglob("*.wav"))
    assert len(wav_files) == 1


async def test_dedup_does_not_skip_different_track(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """If no confident local match, acquisition proceeds normally."""
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    # Seed local with a completely different track
    existing = music_dir / "other.wav"
    _make_wav(existing)
    _tag_wav(existing, "One More Time", "Daft Punk")
    await _scan_file(db, music_dir, existing)

    candidate = TrackCandidate(
        provider="fake",
        provider_ref="fake-001",
        title="Test Track One",
        artist="Fake Artist",
        duration_seconds=1,
    )
    provider = FakeProvider(FIXTURE_AUDIO)

    async with db() as session, session.begin():
        job_id = await create_job(
            session,
            provider_name="fake",
            provider_ref="fake-001",
            candidate=candidate,
        )
        await run_acquisition(
            job_id=job_id,
            provider=provider,
            provider_ref="fake-001",
            candidate=candidate,
            music_dir=music_dir,
            tmp_acquire_dir=tmp_path / "tmp",
            session=session,
            scan_trigger=AsyncMock(),
        )

    async with db() as session:
        row = await session.get(AcquisitionJobRow, job_id)
    assert row is not None
    # With the review gate: track staged for review, not auto-placed
    assert row.state == "needs_review", f"Expected needs_review, got {row.state}"
    assert row.staging_path is not None
    assert Path(row.staging_path).exists(), "Staged file must exist on disk"
    # music_dir still only has the seeded track (new track is in staging, not music)
    wav_files = list(music_dir.rglob("*.wav"))
    assert len(wav_files) == 1
