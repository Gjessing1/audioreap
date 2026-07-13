"""Integration tests for the album-cohesion functions in service/library/cohesion.py.

Covers the load-bearing (and previously untested) functions that prevent
Navidrome album splits: stable_albumartist, find_canonical_album,
get_owned_recording_ids, merge_albums, and auto_heal_album_splits.
Runs against a throwaway SQLite DB + tmp_path filesystem — no network,
no audio decoding (merge tag-rewrites are best-effort and skip missing /
unreadable files by design, which is exactly what the DB-level assertions
here rely on).
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.db.schema import Album, Artist, Base, Track, TrackFile
from service.library.cohesion import (
    _artist_id,
    auto_heal_album_splits,
    find_canonical_album,
    get_owned_recording_ids,
    merge_albums,
    stable_albumartist,
)

MB_ARTIST = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"
RG = "b1392450-e666-3926-a536-22c65f834433"
REL = "9162580e-5df4-32de-80cc-f45a8d8ca9c2"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _seed_artist(
    session: AsyncSession, name: str, mb_artist_id: str | None = None
) -> Artist:
    artist = Artist(
        id=_artist_id(name),
        name=name,
        musicbrainz_artist_id=mb_artist_id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(artist)
    return artist


async def _seed_album(
    session: AsyncSession,
    artist: Artist,
    title: str,
    *,
    album_id: str | None = None,
    year: int | None = None,
    rg: str | None = None,
    release_id: str | None = None,
) -> Album:
    album = Album(
        id=album_id or f"album:{title}:{artist.name}",
        title=title,
        year=year,
        artist_id=artist.id,
        mb_release_group_id=rg,
        musicbrainz_release_id=release_id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(album)
    return album


async def _seed_track(
    session: AsyncSession,
    artist: Artist,
    album: Album | None,
    title: str,
    *,
    recording_id: str | None = None,
    file_path: Path | None = None,
) -> Track:
    track = Track(
        id=f"track:{title}:{album.id if album else 'single'}",
        title=title,
        artist_id=artist.id,
        album_id=album.id if album else None,
        musicbrainz_recording_id=recording_id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(track)
    if file_path is not None:
        session.add(
            TrackFile(
                track_id=track.id,
                path=str(file_path),
                codec="vorbis",
                container="ogg",
                created_at=_now(),
            )
        )
    return track


# ---------------------------------------------------------------- stable_albumartist


async def test_stable_albumartist_prefers_local_name(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session, session.begin():
        await _seed_artist(session, "The Beatles", MB_ARTIST)
    async with db() as session:
        assert await stable_albumartist(session, "Beatles, The", MB_ARTIST) == "The Beatles"


async def test_stable_albumartist_passthrough_without_mb_id(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session, session.begin():
        await _seed_artist(session, "The Beatles", MB_ARTIST)
    async with db() as session:
        # No MB artist ID to match on → name goes through unchanged.
        assert await stable_albumartist(session, "Beatles, The", None) == "Beatles, The"


async def test_stable_albumartist_passthrough_for_unknown_artist(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session:
        assert await stable_albumartist(session, "Aphex Twin", MB_ARTIST) == "Aphex Twin"


# ---------------------------------------------------------------- find_canonical_album


async def test_canonical_album_by_release_group_wins_over_title(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        await _seed_album(
            session, artist, "OK Computer", year=1997, rg=RG, release_id=REL
        )
    async with db() as session:
        # Completely different incoming title/artist — the RG anchor still wins.
        got = await find_canonical_album(
            session, "OK Computer OKNOTOK 1997 2017", "Radiohead & Friends", RG
        )
        assert got == ("OK Computer", "Radiohead", 1997, REL)


async def test_canonical_album_by_normalized_title(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "The Beatles")
        await _seed_album(
            session, artist, "Abbey Road", year=1969, release_id=REL
        )
    async with db() as session:
        got = await find_canonical_album(session, "Abbey Road (Remaster)", "The Beatles", None)
        assert got == ("Abbey Road", "The Beatles", 1969, REL)


async def test_canonical_album_prefers_entry_with_year(
    db: async_sessionmaker[AsyncSession],
) -> None:
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Metallica")
        await _seed_album(session, artist, "Metallica", album_id="album:noyear", year=None)
        await _seed_album(session, artist, "Metallica", album_id="album:withyear", year=1991)
    async with db() as session:
        got = await find_canonical_album(session, "Metallica", "Metallica", None)
        assert got is not None
        assert got[2] == 1991  # anchored to the better-tagged (year-bearing) row


async def test_canonical_album_no_match(db: async_sessionmaker[AsyncSession]) -> None:
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        await _seed_album(session, artist, "OK Computer", rg=RG)
    async with db() as session:
        assert await find_canonical_album(session, "Kid A", "Radiohead", None) is None
        assert await find_canonical_album(session, None, "Radiohead", None) is None
        # Same title under a different AlbumArtist must not anchor.
        assert await find_canonical_album(session, "OK Computer", "Muse", None) is None


# ---------------------------------------------------------------- get_owned_recording_ids


async def test_get_owned_recording_ids(db: async_sessionmaker[AsyncSession]) -> None:
    owned = "aaaaaaaa-0000-0000-0000-000000000001"
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Boards of Canada")
        album = await _seed_album(session, artist, "Geogaddi")
        await _seed_track(session, artist, album, "Music Is Math", recording_id=owned)
    async with db() as session:
        got = await get_owned_recording_ids(
            session, [owned, "aaaaaaaa-0000-0000-0000-000000000002"]
        )
        assert got == {owned}
        assert await get_owned_recording_ids(session, []) == set()


# ---------------------------------------------------------------- merge_albums


async def test_merge_albums_moves_files_and_deletes_source_row(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    music = tmp_path / "music"
    trash = music / ".trash"
    canon_dir = music / "Radiohead" / "OK Computer (1997)"
    split_dir = music / "Radiohead" / "OK Computer (Remaster) (2017)"
    canon_dir.mkdir(parents=True)
    split_dir.mkdir(parents=True)
    kept = canon_dir / "01 - Airbag.ogg"
    moved_src = split_dir / "02 - Paranoid Android.ogg"
    kept.write_bytes(b"fake-audio-1")
    moved_src.write_bytes(b"fake-audio-2")

    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        canon = await _seed_album(
            session, artist, "OK Computer", album_id="album:canon", year=1997, release_id=REL
        )
        split = await _seed_album(
            session, artist, "OK Computer (Remaster)", album_id="album:split", year=2017
        )
        await _seed_track(session, artist, canon, "Airbag", file_path=kept)
        await _seed_track(session, artist, split, "Paranoid Android", file_path=moved_src)

    async with db() as session:
        result = await merge_albums(session, "album:canon", "album:split", trash, music)

    assert result == {"moved": 1, "already_there": 0, "collisions": 0}
    dst = canon_dir / moved_src.name
    assert dst.exists() and not moved_src.exists()
    assert not split_dir.exists()  # emptied source dir is trashed

    async with db() as session:
        assert await session.get(Album, "album:split") is None
        tracks = (
            (await session.execute(select(Track).where(Track.album_id == "album:canon")))
            .scalars()
            .all()
        )
        assert len(tracks) == 2
        moved_file = (
            (await session.execute(select(TrackFile).where(TrackFile.path == str(dst))))
            .scalars()
            .one_or_none()
        )
        assert moved_file is not None


async def test_merge_albums_identity_and_missing_are_noops(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        await _seed_album(session, artist, "OK Computer", album_id="album:canon")
    async with db() as session:
        zero = {"moved": 0, "already_there": 0, "collisions": 0}
        assert await merge_albums(session, "album:canon", "album:canon", tmp_path, tmp_path) == zero
        assert await merge_albums(session, "album:canon", "album:gone", tmp_path, tmp_path) == zero


# ---------------------------------------------------------------- auto_heal_album_splits


async def test_auto_heal_merges_split_into_mbid_bearing_canonical(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    music = tmp_path / "music"
    a_dir = music / "Radiohead" / "In Rainbows (2007)"
    b_dir = music / "Radiohead" / "In Rainbows (Deluxe) (2007)"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    a_file = a_dir / "01 - 15 Step.ogg"
    b_file = b_dir / "02 - Bodysnatchers.ogg"
    a_file.write_bytes(b"fake-audio-1")
    b_file.write_bytes(b"fake-audio-2")

    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        # Fewer tracks but has the MB release ID → must still be picked as canonical.
        with_mbid = await _seed_album(
            session, artist, "In Rainbows", album_id="album:mbid", year=2007, release_id=REL
        )
        bare = await _seed_album(
            session, artist, "In Rainbows (Deluxe)", album_id="album:bare", year=2007
        )
        await _seed_track(session, artist, with_mbid, "15 Step", file_path=a_file)
        await _seed_track(session, artist, bare, "Bodysnatchers", file_path=b_file)

    async with db() as session:
        merged = await auto_heal_album_splits(
            session, "In Rainbows", "Radiohead", music / ".trash", music
        )
    assert merged == 1

    async with db() as session:
        assert await session.get(Album, "album:bare") is None
        assert await session.get(Album, "album:mbid") is not None
    assert (a_dir / b_file.name).exists()


async def test_auto_heal_noop_without_split(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        await _seed_album(session, artist, "Kid A", year=2000)
    async with db() as session:
        assert await auto_heal_album_splits(session, "Kid A", "Radiohead", tmp_path, tmp_path) == 0
        assert await auto_heal_album_splits(session, "", "Radiohead", tmp_path, tmp_path) == 0


# ---------------------------------------------------------------- apply_album_tags


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
async def test_apply_album_tags_rewrites_grouping_tags(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from sqlalchemy.orm import joinedload

    from service.library.cohesion import apply_album_tags
    from service.library.tagger import read_tags, write_tags

    wav = Path(__file__).parent.parent / "fixtures" / "audio" / "tone_1s.wav"
    album_dir = tmp_path / "music" / "Radiohead" / "Amnesiac (2001)"
    album_dir.mkdir(parents=True)
    f1 = album_dir / "01 - Packt.ogg"
    f2 = album_dir / "02 - Pyramid Song.ogg"
    for f in (f1, f2):
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav), "-c:a", "libvorbis", str(f)],
            capture_output=True, timeout=60, check=True,
        )
    # Drifted grouping tags on the second file — the split-causing state.
    write_tags(f1, album="Amnesiac", albumartist="Radiohead", year=2001, mb_release_id=REL)
    write_tags(f2, album="Amnesiac (Deluxe)", albumartist="radiohead", year=2009)

    async with db() as session, session.begin():
        artist = await _seed_artist(session, "Radiohead")
        album = await _seed_album(
            session, artist, "Amnesiac", album_id="album:amnesiac", year=2001, release_id=REL
        )
        await _seed_track(session, artist, album, "Packt", file_path=f1)
        await _seed_track(session, artist, album, "Pyramid Song", file_path=f2)

    async with db() as session:
        loaded = (
            (await session.execute(
                select(Album)
                .options(joinedload(Album.tracks).joinedload(Track.file), joinedload(Album.artist))
                .where(Album.id == "album:amnesiac")
            ))
            .unique()
            .scalar_one()
        )
        assert await apply_album_tags(loaded) == 2

    for f in (f1, f2):
        tagged = read_tags(f)
        assert tagged is not None
        assert tagged.album == "Amnesiac"
        assert tagged.albumartist == "Radiohead"
        assert tagged.year == 2001
        assert tagged.mb_release_id == REL
