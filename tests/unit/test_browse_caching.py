"""Browsing hot paths: nav badge polling and thumbnail revalidation."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from service.api import shared
from service.api.routes import artists, health, jobs


def _request(headers: dict[str, str] | None = None) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    })


class _CountSession:
    def __init__(self, count: int) -> None:
        self._count = count

    async def execute(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one=lambda: self._count)


# ── Nav badges ───────────────────────────────────────────────────────────────

async def test_review_badge_feeds_tab_bar_out_of_band() -> None:
    html = (await jobs.nav_review_count(session=_CountSession(3))).body.decode()
    assert 'id="nav-review-badge"' in html
    assert '<span id="tab-review-badge" class="nav-badge" hx-swap-oob="true">3</span>' in html
    # Only the top nav's span polls — the tab bar copy must not start a second poll.
    assert html.count("hx-get=") == 1


async def test_empty_review_badge_clears_tab_bar() -> None:
    html = (await jobs.nav_review_count(session=_CountSession(0))).body.decode()
    assert '<span id="tab-review-badge" hx-swap-oob="true"></span>' in html
    assert html.count("hx-get=") == 1


async def test_attention_badge_feeds_tab_bar_out_of_band() -> None:
    counts = AsyncMock(return_value={"dupes": 2, "no_cover": 1})
    with patch.object(health, "_attention_cache", None), \
            patch.object(health, "_library_attention_counts", counts):
        html = (await health.nav_attention_count(session=object())).body.decode()
    assert 'id="nav-attention-badge"' in html
    assert 'id="tab-attention-badge" class="nav-badge nav-badge-attention"' in html
    assert 'hx-swap-oob="true">3</span>' in html
    assert html.count("hx-get=") == 1


# ── Thumbnail validators ─────────────────────────────────────────────────────

def test_thumb_etag_changes_when_regenerated(tmp_path: Path) -> None:
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"a" * 10)
    first = shared._thumb_etag(thumb)
    thumb.write_bytes(b"b" * 12)
    assert first is not None
    assert shared._thumb_etag(thumb) != first
    assert shared._thumb_etag(tmp_path / "missing.jpg") is None


@pytest.mark.parametrize(("header", "expected"), [
    ('"abc-1"', True),
    ('W/"abc-1"', True),          # a proxy may weaken the validator
    ('"zzz", "abc-1"', True),
    ('"abc-2"', False),
    (None, False),
])
def test_etag_matches(header: str | None, expected: bool) -> None:
    assert shared._etag_matches(header, '"abc-1"') is expected


def test_etag_never_matches_without_a_validator() -> None:
    assert shared._etag_matches('"abc-1"', None) is False


async def test_artist_thumbnail_revalidates_with_304(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    music, cache = tmp_path / "music", tmp_path / "cache"
    src = music / "Artist" / "artist.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"full-size portrait")
    os.utime(src, (1, 1))  # the thumbnail below is newer than its source
    thumb = cache / "thumbs" / "artist_artist:1_256.jpg"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(b"thumb")
    monkeypatch.setattr(artists.settings, "music_dir", music)
    monkeypatch.setattr(artists.settings, "cache_dir", cache)
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(name="Artist")

    first = await artists.artist_image(_request(), "artist:1", 256, session)
    assert first.status_code == 200
    assert first.body == b"thumb"
    assert "stale-while-revalidate" in first.headers["cache-control"]

    again = await artists.artist_image(
        _request({"If-None-Match": first.headers["etag"]}), "artist:1", 256, session,
    )
    assert again.status_code == 304
    assert again.body == b""
