"""Shared vs private library placement (service/library/visibility.py).

Covers the rule (artist folder, override, compilations, existing album wins),
approval landing in the chosen root, and moving an artist between the roots.
"""
from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.pipeline import place_approved_track
from service.config import settings
from service.db.schema import AcquisitionJobRow, Album, Artist, Base, Track, TrackFile
from service.library import visibility
from service.library.cohesion import _artist_id
from tests.integration.test_approve import _stage_track


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def music(tmp_path: Path):
    music_dir = tmp_path / "music"
    (music_dir / "_private").mkdir(parents=True)
    with patch.object(settings, "music_dir", music_dir), \
         patch.object(settings, "private_subdir", "_private"):
        yield music_dir


async def _add_file(
    s: AsyncSession, path: Path, *, artist: str, album: str | None, n: int
) -> None:
    now = datetime(2026, 10, 16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    aid = _artist_id(artist)
    if await s.get(Artist, aid) is None:
        s.add(Artist(id=aid, name=artist, created_at=now, updated_at=now))
    album_id = None
    if album:
        album_id = f"album:{artist}:{album}"
        if await s.get(Album, album_id) is None:
            s.add(Album(id=album_id, title=album, artist_id=aid, created_at=now, updated_at=now))
    s.add(Track(id=f"t{n}", title=path.stem, artist_id=aid, album_id=album_id,
                created_at=now, updated_at=now))
    s.add(TrackFile(track_id=f"t{n}", path=str(path), codec="opus", container="ogg",
                    created_at=now))
    await s.flush()


# ── Config ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("", None),
    ("_private", "_private"),
    ("/_private/", "_private"),
    (".private", None),       # the scanner skips dot-directories
    ("Singles", None),        # owned by the layout
    ("a/b", None),
])
def test_private_subdir_validation(raw: str, expected: str | None) -> None:
    with patch.object(settings, "private_subdir", raw):
        assert visibility.private_subdir() == expected


# ── The rule ───────────────────────────────────────────────────────────────

async def test_rule_shared_by_default_private_by_folder(music: Path, db) -> None:
    (music / "_private" / "Eminem").mkdir()
    (music / "_private" / "Singles" / "Clipse").mkdir(parents=True)
    async with db() as s:
        assert await visibility.resolve_placement(
            s, album="X", albumartist="Nirvana", override=None) == (False, "default")
        assert await visibility.resolve_placement(
            s, album="X", albumartist="Eminem", override=None) == (True, "artist_folder")
        assert await visibility.resolve_placement(
            s, album=None, albumartist="Clipse", override=None) == (True, "artist_folder")
        assert await visibility.resolve_placement(
            s, album="X", albumartist="Eminem", override="shared") == (False, "chosen")
        assert await visibility.resolve_placement(
            s, album="X", albumartist="Nirvana", override="private") == (True, "chosen")


async def test_compilations_stay_shared(music: Path, db) -> None:
    async with db() as s:
        assert await visibility.resolve_placement(
            s, album="Now 1", albumartist="Various Artists", override="private",
        ) == (False, "compilation")


async def test_existing_album_location_beats_override(music: Path, db) -> None:
    """An album never straddles both libraries."""
    async with db() as s:
        await _add_file(s, music / "Nick Cave" / "Ghosteen (2019)" / "01 - Spinning Song.ogg",
                        artist="Nick Cave", album="Ghosteen", n=1)
        assert await visibility.resolve_placement(
            s, album="Ghosteen", albumartist="Nick Cave", override="private",
        ) == (False, "existing_album")


def test_feature_off_is_always_shared(tmp_path: Path) -> None:
    with patch.object(settings, "private_subdir", ""):
        assert visibility.library_root(True, tmp_path) == tmp_path
        assert not visibility.artist_is_private("Eminem", tmp_path)


# ── Approval lands in the chosen root ──────────────────────────────────────

async def test_approve_follows_private_artist_folder(tmp_path: Path, music: Path, db) -> None:
    (music / "_private" / "Singles" / "Fake Artist").mkdir(parents=True)
    job_id = await _stage_track(db, tmp_path)
    async with db() as s, s.begin():
        dest = await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())
    assert dest.is_relative_to(music / "_private")


async def test_approve_honours_review_card_choice(tmp_path: Path, music: Path, db) -> None:
    job_id = await _stage_track(db, tmp_path)
    async with db() as s, s.begin():
        row = await s.get(AcquisitionJobRow, job_id)
        meta = json.loads(row.resolved_metadata_json)
        meta["visibility"] = "private"
        row.resolved_metadata_json = json.dumps(meta)
    async with db() as s, s.begin():
        dest = await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())
    assert dest.is_relative_to(music / "_private")


async def test_approve_new_artist_goes_shared(tmp_path: Path, music: Path, db) -> None:
    job_id = await _stage_track(db, tmp_path)
    async with db() as s, s.begin():
        dest = await place_approved_track(job_id, {}, s, scan_trigger=AsyncMock())
    assert dest.is_relative_to(music)
    assert not dest.is_relative_to(music / "_private")


# ── Moving an existing artist ──────────────────────────────────────────────

async def test_move_artist_round_trip(music: Path, db) -> None:
    album_file = music / "Lil Wayne" / "Tha Carter (2004)" / "01 - Walk In.ogg"
    single_file = music / "Singles" / "Lil Wayne" / "Fireman.ogg"
    async with db() as s, s.begin():
        await _add_file(s, album_file, artist="Lil Wayne", album="Tha Carter", n=1)
        await _add_file(s, single_file, artist="Lil Wayne", album=None, n=2)
        (album_file.parent / "cover.jpg").write_bytes(b"jpg")

    async with db() as s, s.begin():
        result = await visibility.move_artist(s, "Lil Wayne", private=True)
    assert result == {"moved": 3, "collisions": 0, "reindexed": 2}
    priv = music / "_private"
    assert (priv / "Lil Wayne" / "Tha Carter (2004)" / "cover.jpg").exists()
    assert not (music / "Lil Wayne").exists()
    assert not (music / "Singles").exists()  # emptied, pruned
    assert visibility.artist_location("Lil Wayne") == "private"
    async with db() as s:
        paths = set((await s.execute(select(TrackFile.path))).scalars())
    assert paths == {
        str(priv / "Lil Wayne" / "Tha Carter (2004)" / "01 - Walk In.ogg"),
        str(priv / "Singles" / "Lil Wayne" / "Fireman.ogg"),
    }

    async with db() as s, s.begin():
        await visibility.move_artist(s, "Lil Wayne", private=False)
    assert album_file.exists() and single_file.exists()
    assert priv.is_dir()  # the private root itself is never removed
    async with db() as s:
        paths = set((await s.execute(select(TrackFile.path))).scalars())
    assert paths == {str(album_file), str(single_file)}


async def test_move_artist_merges_split_and_keeps_collisions(music: Path, db) -> None:
    """An artist split across both libraries is gathered; nothing is overwritten."""
    shared = music / "Eminem" / "Recovery (2010)"
    private = music / "_private" / "Eminem" / "Recovery (2010)"
    async with db() as s, s.begin():
        await _add_file(s, shared / "01 - Cold Wind Blows.ogg", artist="Eminem",
                        album="Recovery", n=1)
        await _add_file(s, private / "02 - Talkin' 2 Myself.ogg", artist="Eminem",
                        album="Recovery", n=2)
        (shared / "cover.jpg").write_bytes(b"new")
        (private / "cover.jpg").write_bytes(b"old")

    async with db() as s, s.begin():
        result = await visibility.move_artist(s, "Eminem", private=True)
    assert result["moved"] == 1 and result["collisions"] == 1
    assert (private / "01 - Cold Wind Blows.ogg").exists()
    assert (private / "cover.jpg").read_bytes() == b"old"
    assert (shared / "cover.jpg").exists()  # left in place, not overwritten
