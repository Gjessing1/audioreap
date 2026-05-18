"""Integration tests for MusicBrainz cache — no real network calls."""
import json
import time
from pathlib import Path
from unittest.mock import patch

from service.metadata.musicbrainz import _cache_key, _cache_path, _save_cache, lookup_recording

# ── Fixtures ───────────────────────────────────────────────────────────────

_FIXTURE_RESPONSE: dict[str, object] = {
    "recording-list": [
        {
            "id": "abc-123-recording",
            "title": "Around the World",
            "ext:score": "100",
            "artist-credit": [
                {"artist": {"id": "xyz-artist", "name": "Daft Punk"}}
            ],
            "release-list": [
                {
                    "id": "release-999",
                    "title": "Homework",
                    "date": "1997",
                    "medium-list": [
                        {
                            "track-list": [
                                {"position": "2", "number": "2"}
                            ]
                        }
                    ],
                }
            ],
        }
    ]
}


# ── Cache tests ────────────────────────────────────────────────────────────

def test_cache_hit_avoids_network(tmp_path: Path) -> None:
    key = _cache_key("Around the World", "Daft Punk")
    _save_cache(tmp_path, key, _FIXTURE_RESPONSE)

    with patch("musicbrainzngs.search_recordings") as mock_mb:
        result = lookup_recording("Around the World", "Daft Punk", cache_dir=tmp_path)
        mock_mb.assert_not_called()

    assert result is not None
    assert result.recording_id == "abc-123-recording"


def test_cache_miss_calls_api(tmp_path: Path) -> None:
    with patch("musicbrainzngs.search_recordings", return_value=_FIXTURE_RESPONSE) as mock_mb:
        result = lookup_recording("Around the World", "Daft Punk", cache_dir=tmp_path)
        mock_mb.assert_called_once()

    assert result is not None


def test_cache_written_after_api_call(tmp_path: Path) -> None:
    with patch("musicbrainzngs.search_recordings", return_value=_FIXTURE_RESPONSE):
        lookup_recording("Around the World", "Daft Punk", cache_dir=tmp_path)

    key = _cache_key("Around the World", "Daft Punk")
    cache_file = _cache_path(tmp_path, key)
    assert cache_file.exists()
    data = json.loads(cache_file.read_text())
    assert "recording-list" in data


def test_stale_cache_triggers_api(tmp_path: Path) -> None:
    key = _cache_key("Around the World", "Daft Punk")
    _save_cache(tmp_path, key, _FIXTURE_RESPONSE)
    # Back-date the cache file
    cache_file = _cache_path(tmp_path, key)
    old_time = time.time() - 90000  # 25 hours ago
    import os
    os.utime(cache_file, (old_time, old_time))

    with patch("musicbrainzngs.search_recordings", return_value=_FIXTURE_RESPONSE) as mock_mb:
        lookup_recording("Around the World", "Daft Punk", cache_dir=tmp_path)
        mock_mb.assert_called_once()


# ── Match quality tests ────────────────────────────────────────────────────

def test_correct_fields_returned(tmp_path: Path) -> None:
    with patch("musicbrainzngs.search_recordings", return_value=_FIXTURE_RESPONSE):
        result = lookup_recording("Around the World", "Daft Punk", cache_dir=tmp_path)

    assert result is not None
    assert result.recording_id == "abc-123-recording"
    assert result.title == "Around the World"
    assert result.artist == "Daft Punk"
    assert result.album == "Homework"
    assert result.year == 1997
    assert result.track_number == 2


def test_no_results_returns_none(tmp_path: Path) -> None:
    with patch("musicbrainzngs.search_recordings", return_value={"recording-list": []}):
        result = lookup_recording("XYZ Unknown Track", "XYZ Unknown Artist", cache_dir=tmp_path)
    assert result is None


def test_api_failure_returns_none(tmp_path: Path) -> None:
    with patch("musicbrainzngs.search_recordings", side_effect=Exception("network error")):
        result = lookup_recording("Song", "Artist", cache_dir=tmp_path)
    assert result is None


def test_no_cache_dir_bypasses_cache() -> None:
    with patch("musicbrainzngs.search_recordings", return_value=_FIXTURE_RESPONSE) as mock_mb:
        result = lookup_recording("Around the World", "Daft Punk", cache_dir=None)
        mock_mb.assert_called_once()
    assert result is not None
