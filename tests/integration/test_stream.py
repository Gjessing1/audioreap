"""Integration tests for /api/stream/<internal_id> with range-request support."""
from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from service.db.schema import Base
from service.main import app

FIXTURE_AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"
WAV_FIXTURE = FIXTURE_AUDIO / "tone_1s.wav"


@pytest.fixture
async def client(tmp_path: Path):  # type: ignore[misc]
    """FastAPI test client wired to a temp DB containing one track+file."""
    from mutagen.id3 import TIT2, TPE1
    from mutagen.wave import WAVE

    # Copy fixture to tmp music dir
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    track_file = music_dir / "tone_1s.wav"
    import shutil
    shutil.copy2(WAV_FIXTURE, track_file)

    # Tag it
    w = WAVE(str(track_file))
    if w.tags is None:
        w.add_tags()
    w.tags.add(TIT2(encoding=3, text=["Stream Test Track"]))
    w.tags.add(TPE1(encoding=3, text=["Stream Artist"]))
    w.save()

    # Build DB
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        from service.index.scanner import index_file
        await index_file(session, track_file)

    from service.db import session as session_module

    async def override_session():  # type: ignore[override]
        async with factory() as s:
            yield s

    app.dependency_overrides[session_module.get_session] = override_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # Find the internal_id
        from sqlalchemy import select

        from service.db.schema import Track
        async with factory() as session:
            row = (await session.execute(select(Track))).scalar_one()
            internal_id = row.id

        yield c, internal_id

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_stream_full_file(client):  # type: ignore[misc]
    c, iid = client
    resp = await c.get(f"/api/stream/{iid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/")
    assert int(resp.headers["content-length"]) > 0
    assert resp.headers.get("accept-ranges") == "bytes"


async def test_stream_range_request(client):  # type: ignore[misc]
    c, iid = client
    resp = await c.get(f"/api/stream/{iid}", headers={"Range": "bytes=0-1023"})
    assert resp.status_code == 206
    assert "content-range" in resp.headers
    cr = resp.headers["content-range"]
    assert cr.startswith("bytes 0-1023/")
    assert len(resp.content) == 1024


async def test_stream_open_end_range(client):  # type: ignore[misc]
    c, iid = client
    # First get total size
    full = await c.get(f"/api/stream/{iid}")
    total = int(full.headers["content-length"])

    # Request last 512 bytes
    start = total - 512
    resp = await c.get(f"/api/stream/{iid}", headers={"Range": f"bytes={start}-"})
    assert resp.status_code == 206
    assert len(resp.content) == 512


async def test_stream_not_found(client):  # type: ignore[misc]
    c, _ = client
    resp = await c.get("/api/stream/nonexistent-id")
    assert resp.status_code == 404
