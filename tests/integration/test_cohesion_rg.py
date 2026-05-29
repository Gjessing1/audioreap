"""Integration test: Phase 5 find_local_release_group cohesion lookup."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from service.db.schema import Album, Artist, Base
from service.library.cohesion import _artist_id, find_local_release_group

RG = "b1392450-e666-3926-a536-22c65f834433"


@pytest.fixture
async def db(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_album(
    db: async_sessionmaker[AsyncSession],
    artist_name: str,
    album_title: str,
    rg: str | None,
) -> None:
    aid = _artist_id(artist_name)
    now = datetime.now(UTC)
    async with db() as session, session.begin():
        session.add(Artist(id=aid, name=artist_name, created_at=now, updated_at=now))
        session.add(Album(
            id=f"album:{album_title}",
            title=album_title,
            artist_id=aid,
            mb_release_group_id=rg,
            created_at=now,
            updated_at=now,
        ))


async def test_finds_release_group_by_normalized_title(
    db: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_album(db, "Radiohead", "OK Computer", RG)
    async with db() as session:
        # Remaster edition title still normalizes to the owned album.
        assert await find_local_release_group(session, "OK Computer (Remaster)", "Radiohead") == RG


async def test_no_match_returns_none(db: async_sessionmaker[AsyncSession]) -> None:
    await _seed_album(db, "Radiohead", "OK Computer", RG)
    async with db() as session:
        assert await find_local_release_group(session, "Kid A", "Radiohead") is None
        assert await find_local_release_group(session, "OK Computer", "Muse") is None
        assert await find_local_release_group(session, None, "Radiohead") is None


async def test_album_without_rg_is_skipped(db: async_sessionmaker[AsyncSession]) -> None:
    await _seed_album(db, "Radiohead", "OK Computer", None)
    async with db() as session:
        assert await find_local_release_group(session, "OK Computer", "Radiohead") is None
