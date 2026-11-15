"""Focused behavior tests for the unified command-bar search."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from service.api.routes.acquire import acquire_artist_results, acquire_search_results
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


def test_artist_cache_keeps_compact_and_full_result_limits_separate(monkeypatch, tmp_path) -> None:
    from service.metadata import musicbrainz

    calls = []

    def provider(**kwargs):
        calls.append(kwargs["limit"])
        return {"artist-list": [
            {"id": f"artist-{i}", "name": f"Artist {i}", "ext:score": "100"}
            for i in range(kwargs["limit"])
        ]}

    monkeypatch.setattr(musicbrainz.musicbrainzngs, "search_artists", provider)
    monkeypatch.setattr(musicbrainz, "_mb_call", lambda label, fn: fn())
    assert len(musicbrainz.search_artists("Daft Punk", 5, tmp_path)) == 5
    assert len(musicbrainz.search_artists("Daft Punk", 8, tmp_path)) == 8
    assert len(musicbrainz.search_artists("Daft Punk", 5, tmp_path)) == 5
    assert calls == [5, 8]


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


async def test_acquire_search_fans_out_and_preserves_query() -> None:
    response = await acquire_search_results(_request("/search/unified"), "Björk & friends")
    body = response.body.decode()
    assert '/search/artists?q=' in body
    assert '/search/results?q=' in body
    assert '/search/cloud?q=' in body
    assert response.headers["HX-Replace-Url"] == "/acquire?q=Bj%C3%B6rk+%26+friends"


async def test_acquire_url_skips_provider_searches() -> None:
    for url, action in [
        ("https://youtu.be/test", "Get track"),
        ("https://open.spotify.com/playlist/test", "Preview playlist"),
    ]:
        response = await acquire_search_results(_request("/search/unified"), url)
        body = response.body.decode()
        assert action in body
        assert '/search/artists' not in body
        assert '/search/cloud' not in body


async def test_acquire_artist_result_opens_inline_discography(monkeypatch) -> None:
    from types import SimpleNamespace

    from service.metadata import musicbrainz

    monkeypatch.setattr(musicbrainz, "search_artists", lambda *args: [SimpleNamespace(
        artist_id="artist-id", name="Björk", disambiguation="Icelandic musician", score=1.0,
    )])
    response = await acquire_artist_results(_request("/search/artists"), "Björk")
    body = response.body.decode()
    assert "Björk" in body
    assert '/discography/artist-id?search_q=Bj%C3%B6rk' in body
    assert 'hx-target="#acquire-results"' in body
    assert '/acquire?q=Bj%C3%B6rk&amp;artist=artist-id' in body


async def test_acquire_artist_lookup_failure_has_retry(monkeypatch) -> None:
    from service.metadata import musicbrainz

    def unavailable(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(musicbrainz, "search_artists", unavailable)
    response = await acquire_artist_results(_request("/search/artists"), "Björk")
    body = response.body.decode()
    assert "unavailable" in body
    assert "Try again" in body


async def test_inline_discography_keeps_related_artists_in_search(monkeypatch) -> None:
    from types import SimpleNamespace

    from service.api.routes.discography import discography_related_artists
    from service.metadata import musicbrainz

    monkeypatch.setattr(musicbrainz, "get_related_artists", lambda *args: [SimpleNamespace(
        artist_id="related-id", name="Related Artist", relation="member", disambiguation="",
    )])
    response = await discography_related_artists(_request("/discography/id/related"), "id", "Björk")
    body = response.body.decode()
    assert '/discography/related-id?search_q=Bj%C3%B6rk' in body
    assert 'hx-target="#acquire-results"' in body
    assert '/acquire?q=Bj%C3%B6rk&amp;artist=related-id' in body
