"""Focused behavior tests for the unified command-bar search."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from service.api.routes.library import _classify_search_url, nav_jump
from service.api.routes.playlists import playlists_page
from service.db.schema import Artist, Base, PlaylistImport, Track, TrackFile


def _request(path: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 123),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    })


def test_classifies_spotify_playlist() -> None:
    url = "https://open.spotify.com/intl-de/playlist/37i9dQZF1DX4JAvHpjipBk"
    assert _classify_search_url(url) == (url, None)


def test_classifies_youtube_playlist_before_direct_video() -> None:
    url = "https://www.youtube.com/watch?v=abc123&list=PLxyz"
    assert _classify_search_url(url) == (url, None)


def test_classifies_youtube_video() -> None:
    url = "https://youtu.be/abc123"
    assert _classify_search_url(url) == (None, url)


def test_rejects_lookalike_and_non_http_urls() -> None:
    assert _classify_search_url("https://youtube.com.evil.test/watch?v=x") == (None, None)
    assert _classify_search_url("javascript://youtube.com/watch?v=x") == (None, None)


def test_plain_search_is_not_a_url_action() -> None:
    assert _classify_search_url("daft punk harder better") == (None, None)


async def test_local_shell_searches_library_and_imported_playlists() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC).replace(tzinfo=None)
    async with sessions() as session:
        artist = Artist(
            id="artist-1", name="Daft Punk", created_at=now, updated_at=now,
        )
        track = Track(
            id="track-1", title="Harder Better Faster Stronger", artist=artist,
            created_at=now, updated_at=now,
        )
        track_file = TrackFile(
            track=track, path="/music/test.opus", codec="opus", container="ogg",
            created_at=now,
        )
        playlist = PlaylistImport(
            id="playlist-1", url="https://open.spotify.com/playlist/test",
            title="Daft Punk Mix", source="spotify", track_count=12,
            created_at=now, updated_at=now,
        )
        session.add_all([artist, track, track_file, playlist])
        await session.commit()

        response = await nav_jump(_request("/nav/jump"), "daft punk", session)
        body = response.body.decode()

    await engine.dispose()
    assert "Daft Punk" in body
    assert "Harder Better Faster Stronger" in body
    assert "Daft Punk Mix" in body
    assert "/nav/jump/musicbrainz" in body
    assert "/nav/jump/youtube" in body


async def test_playlist_url_handoff_prefills_and_auto_previews() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    url = "https://open.spotify.com/playlist/test"
    async with sessions() as session:
        response = await playlists_page(_request("/playlists"), url, session)
        body = response.body.decode()

    await engine.dispose()
    assert f'value="{url}"' in body
    assert 'hx-trigger="load"' in body
