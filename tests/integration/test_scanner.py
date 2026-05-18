"""Integration tests for the library scanner.

Uses real SQLite (tmp_path), real filesystem, and tagged WAV fixtures.
No mocks — tests the actual tagger + scanner + DB pipeline.
"""
import struct
import wave
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.db.schema import Base, Track, TrackFile
from service.index.scanner import scan

# ── DB fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
async def db_session(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


# ── Audio fixture helpers ──────────────────────────────────────────────────

def _make_wav(path: Path) -> None:
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(struct.pack("<44100h", *([0] * 44100)))


def _tag_wav(path: Path, title: str, artist: str, album: str | None = None) -> None:
    from mutagen.id3 import TALB, TIT2, TPE1
    from mutagen.wave import WAVE

    audio = WAVE(str(path))
    if audio.tags is None:
        audio.add_tags()
    audio.tags.add(TIT2(encoding=3, text=[title]))  # type: ignore[union-attr]
    audio.tags.add(TPE1(encoding=3, text=[artist]))  # type: ignore[union-attr]
    if album:
        audio.tags.add(TALB(encoding=3, text=[album]))  # type: ignore[union-attr]
    audio.save()


def make_track(
    music_dir: Path, subpath: str, title: str, artist: str, album: str | None = None
) -> Path:
    path = music_dir / subpath
    path.parent.mkdir(parents=True, exist_ok=True)
    _make_wav(path)
    _tag_wav(path, title, artist, album)
    return path


# ── Tests ──────────────────────────────────────────────────────────────────

async def test_full_scan_adds_tracks(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "Artist/song1.wav", "Song One", "Test Artist", "Test Album")
    make_track(music_dir, "Artist/song2.wav", "Song Two", "Test Artist", "Test Album")

    async with db_session() as session, session.begin():
        result = await scan(session, music_dir)

    assert result.added == 2
    assert result.errors == 0

    async with db_session() as session:
        tracks = (await session.execute(select(Track))).scalars().all()
    assert len(tracks) == 2
    assert {t.title for t in tracks} == {"Song One", "Song Two"}


async def test_full_scan_is_idempotent(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "song.wav", "Song", "Artist")

    async with db_session() as session, session.begin():
        r1 = await scan(session, music_dir)
    async with db_session() as session, session.begin():
        r2 = await scan(session, music_dir)

    assert r1.added == 1
    assert r2.added == 0
    assert r2.skipped == 1

    async with db_session() as session:
        tracks = (await session.execute(select(Track))).scalars().all()
    assert len(tracks) == 1


async def test_incremental_skips_unchanged(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "song.wav", "Song", "Artist")

    async with db_session() as session, session.begin():
        await scan(session, music_dir)
    async with db_session() as session, session.begin():
        result = await scan(session, music_dir, incremental=True)

    assert result.skipped == 1
    assert result.added == 0


async def test_incremental_picks_up_new_file(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "song1.wav", "Song One", "Artist")

    async with db_session() as session, session.begin():
        await scan(session, music_dir)

    make_track(music_dir, "song2.wav", "Song Two", "Artist")

    async with db_session() as session, session.begin():
        result = await scan(session, music_dir, incremental=True)

    assert result.added == 1
    assert result.skipped == 1


async def test_full_scan_removes_deleted_file(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    p = make_track(music_dir, "song.wav", "Song", "Artist")

    async with db_session() as session, session.begin():
        await scan(session, music_dir)

    p.unlink()

    async with db_session() as session, session.begin():
        result = await scan(session, music_dir)

    assert result.removed == 1

    async with db_session() as session:
        files = (await session.execute(select(TrackFile))).scalars().all()
    assert len(files) == 0


async def test_identity_stable_across_scans(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "song.wav", "Around the World", "Daft Punk", "Homework")

    async with db_session() as session, session.begin():
        await scan(session, music_dir)
    async with db_session() as session:
        id1 = (await session.execute(select(Track))).scalar_one().id

    async with db_session() as session, session.begin():
        await scan(session, music_dir)
    async with db_session() as session:
        id2 = (await session.execute(select(Track))).scalar_one().id

    assert id1 == id2


async def test_non_audio_files_ignored(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    (music_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    (music_dir / "info.txt").write_text("hello")
    make_track(music_dir, "song.wav", "Song", "Artist")

    async with db_session() as session, session.begin():
        result = await scan(session, music_dir)

    assert result.added == 1
    assert result.errors == 0


async def test_track_without_album(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "song.wav", "Standalone", "Artist", album=None)

    async with db_session() as session, session.begin():
        await scan(session, music_dir)
    async with db_session() as session:
        track = (await session.execute(select(Track))).scalar_one()
    assert track.album_id is None


async def test_nested_directory_scan(
    tmp_path: Path, db_session: async_sessionmaker[AsyncSession]
) -> None:
    music_dir = tmp_path / "music"
    make_track(music_dir, "A/B/C/deep.wav", "Deep Track", "Artist", "Deep Album")

    async with db_session() as session, session.begin():
        result = await scan(session, music_dir)

    assert result.added == 1
