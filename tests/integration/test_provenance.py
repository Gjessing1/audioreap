"""Integration tests: acquisition provenance survives onto the track_files row.

The columns (`provider`, `provider_ref`, `source_url`) exist on `track_files` but
were NULL on every row, because the scanner — the only thing that ever built a
TrackFile — sees a placed file on disk and knows nothing about the job that
fetched it. Without them, one-click re-acquire has nothing to re-fetch and
Library Health can only offer a manual search.
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
from service.db.schema import Base, TrackFile
from tests.fake_provider import FakeProvider

FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _acquire_and_approve(
    db: async_sessionmaker[AsyncSession], tmp_path: Path, *, provider_ref: str = "fake-001"
) -> Path:
    from service.config import settings

    provider = FakeProvider(FIXTURE_AUDIO)
    candidate = TrackCandidate(
        provider="fake", provider_ref=provider_ref,
        title="Test Track One", artist="Fake Artist", duration_seconds=1,
    )
    async with db() as s, s.begin():
        job_id = await create_job(s, provider_name="fake",
                                  provider_ref=provider_ref, candidate=candidate)
    await run_acquisition(
        job_id=job_id, provider=provider, provider_ref=provider_ref,
        candidate=candidate, tmp_acquire_dir=tmp_path / "tmp",
        session_factory=db, scan_trigger=AsyncMock(),
    )
    with patch.object(settings, "music_dir", tmp_path / "music"):
        async with db() as s, s.begin():
            return await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())


async def test_approved_track_records_its_source(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """A track acquired through the pipeline carries the reference that fetched it."""
    dest = await _acquire_and_approve(db, tmp_path)

    async with db() as s:
        row = (await s.execute(
            select(TrackFile).where(TrackFile.path == str(dest))
        )).scalar_one()

    assert row.provider == "fake"
    assert row.provider_ref == "fake-001"
    assert row.source_url == "fake://media/fake-001"


async def test_scanner_walk_leaves_provenance_null(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """A file discovered cold on disk gets NULL, not an invented source."""
    from service.index.scanner import index_file

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    found = music_dir / "found.wav"
    found.write_bytes((FIXTURE_AUDIO / "tone_1s.wav").read_bytes())

    async with db() as s, s.begin():
        await index_file(s, found)

    async with db() as s:
        row = (await s.execute(
            select(TrackFile).where(TrackFile.path == str(found))
        )).scalar_one()

    assert row.provider is None
    assert row.provider_ref is None
    assert row.source_url is None


async def test_reacquire_uses_stored_provenance(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """The stored reference is what the re-acquire path needs, and it prefers the
    canonical media URL over the (possibly `ytsearch1:`) provider_ref."""
    dest = await _acquire_and_approve(db, tmp_path)

    async with db() as s:
        row = (await s.execute(
            select(TrackFile).where(TrackFile.path == str(dest))
        )).scalar_one()

    ref = row.source_url or row.provider_ref
    assert ref == "fake://media/fake-001", (
        "reacquire would 400 with 'Track has no provider reference'"
    )


async def test_index_drops_stale_sibling_file_row(
    tmp_path: Path, db: async_sessionmaker[AsyncSession]
) -> None:
    """Indexing a new file clears the track's other file rows whose file is gone.

    `Track.file` is uselist=False, so a leftover row for a vanished path makes the
    relationship ambiguous — the library would read whichever row SQLAlchemy picked.
    """
    from service.index.scanner import index_file

    # Same filename in two folders: untagged fixtures take their title from the
    # stem, so this keeps the track identity fixed while the path changes.
    music_dir = tmp_path / "music"
    original = music_dir / "old" / "Same Track.wav"
    replacement = music_dir / "new" / "Same Track.wav"
    for f in (original, replacement):
        f.parent.mkdir(parents=True)
    original.write_bytes((FIXTURE_AUDIO / "tone_1s.wav").read_bytes())

    async with db() as s, s.begin():
        track_id = await index_file(s, original)
    assert track_id

    # The replacement lands at a new path and the old file is gone — the
    # .mp3 -> .ogg replacement shape.
    replacement.write_bytes(original.read_bytes())
    original.unlink()

    async with db() as s, s.begin():
        assert await index_file(s, replacement) == track_id

    async with db() as s:
        paths = (await s.execute(
            select(TrackFile.path).where(TrackFile.track_id == track_id)
        )).scalars().all()

    assert paths == [str(replacement)], f"stale row survived: {paths}"
