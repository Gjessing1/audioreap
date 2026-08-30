"""Integration test for the enrichment job's album grouping (jobs.py::enrich_track).

Enrichment retags a file that is already in /music and never moves it, so the
album artist it suggests has to be the one that album already groups under.
Suggesting the matched recording's performer instead is what used to split a
compilation into one album per track.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.acquisition.jobs import enrich_track
from service.db.schema import AcquisitionJobRow, Album, Artist, Base, Track, TrackFile
from service.library.cohesion import _artist_id
from service.metadata.musicbrainz import MBRecording

MB_ARTIST = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"
VA_MBID = "89ad4ac3-39f7-470e-963a-56509c546377"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _ogg(path: Path) -> Path:
    wav = Path(__file__).parent.parent / "fixtures" / "audio" / "tone_1s.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav), "-c:a", "libvorbis", str(path)],
        capture_output=True, timeout=60, check=True,
    )
    return path


async def _seed_compilation_track(
    db: async_sessionmaker[AsyncSession], file_path: Path
) -> str:
    """One compilation track: filed under Various Artists, performed by someone else."""
    track_id = "track:silent-night"
    async with db() as session, session.begin():
        va = Artist(
            id=_artist_id("Various Artists"), name="Various Artists",
            musicbrainz_artist_id=VA_MBID, created_at=_now(), updated_at=_now(),
        )
        album = Album(
            id="album:christmas", title="Christmas", year=1994,
            artist_id=va.id, created_at=_now(), updated_at=_now(),
        )
        track = Track(
            id=track_id, title="Silent Night", artist_id=va.id, album_id=album.id,
            artist_credit="Mahalia Jackson", duration_seconds=180,
            created_at=_now(), updated_at=_now(),
        )
        session.add_all([va, album, track, TrackFile(
            track_id=track_id, path=str(file_path), codec="vorbis",
            container="ogg", created_at=_now(),
        )])
    return track_id


def _match() -> MBRecording:
    return MBRecording(
        recording_id="11111111-1111-1111-1111-111111111111",
        title="Silent Night",
        artist="Mahalia Jackson",
        album="Christmas",
        year=1994,
        track_number=5,
        score=1.0,
        artist_id=MB_ARTIST,
        duration_seconds=180,
    )


@pytest.mark.requires_ffmpeg
async def test_enrichment_keeps_the_compilation_album_artist(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    from service.library.tagger import write_tags

    f = _ogg(tmp_path / "music" / "Compilations" / "Christmas (1994)" / "05 - Silent Night.ogg")
    write_tags(f, album="Christmas", albumartist="Various Artists", artist="Mahalia Jackson")
    track_id = await _seed_compilation_track(db, f)

    with patch("service.metadata.musicbrainz.lookup_recording", return_value=_match()):
        await enrich_track({"session_factory": db}, track_id=track_id)

    async with db() as session:
        row = (await session.execute(
            select(AcquisitionJobRow).where(AcquisitionJobRow.provider == "enrich")
        )).scalar_one()
    meta = json.loads(row.resolved_metadata_json or "{}")
    # ARTIST still gets the performer — it is the album artist that must not move.
    assert meta["artist"] == "Mahalia Jackson"
    assert meta["albumartist"] == "Various Artists"
    assert meta["mb_albumartist_id"] == VA_MBID
    assert meta["mb_artist_id"] == MB_ARTIST
    assert meta["is_compilation"] is True
    assert meta["albumartist_locked"] is True


@pytest.mark.requires_ffmpeg
async def test_enrichment_single_uses_the_matched_artist(
    db: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A single has no album to protect, so the match's artist leads both tags."""
    from service.library.tagger import write_tags

    f = _ogg(tmp_path / "music" / "Singles" / "Mahalia Jackson" / "Silent Night.ogg")
    write_tags(f, artist="Mahalia Jackson")
    track_id = "track:single"
    async with db() as session, session.begin():
        artist = Artist(
            id=_artist_id("Mahalia Jackson"), name="Mahalia Jackson",
            created_at=_now(), updated_at=_now(),
        )
        session.add_all([artist, Track(
            id=track_id, title="Silent Night", artist_id=artist.id,
            duration_seconds=180, created_at=_now(), updated_at=_now(),
        ), TrackFile(
            track_id=track_id, path=str(f), codec="vorbis",
            container="ogg", created_at=_now(),
        )])

    with patch("service.metadata.musicbrainz.lookup_recording", return_value=_match()):
        await enrich_track({"session_factory": db}, track_id=track_id)

    async with db() as session:
        row = (await session.execute(
            select(AcquisitionJobRow).where(AcquisitionJobRow.provider == "enrich")
        )).scalar_one()
    meta = json.loads(row.resolved_metadata_json or "{}")
    assert meta["albumartist"] == "Mahalia Jackson"
    assert meta["mb_albumartist_id"] == MB_ARTIST
    assert meta["is_compilation"] is False
    assert meta["albumartist_locked"] is False
