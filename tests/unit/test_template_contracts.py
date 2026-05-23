"""Template variable contract tests.

Renders each partial template with the minimal required context and asserts:
1. No Jinja2 UndefinedError is raised (all required variables are provided)
2. The output is non-empty HTML

Run from host (Jinja2 is stdlib-adjacent, no FastAPI stack needed):
  PYTHONPATH=. python3 -m pytest tests/unit/test_template_contracts.py -v

If a template requires a new variable, add it to the context dict below.
These tests fail loudly when a context key is renamed or removed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "service" / "templates"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    undefined=StrictUndefined,  # raise on any missing variable
    autoescape=True,
)

# ── Shared stubs ─────────────────────────────────────────────────────────────

_JOB = {
    "id": "abc12345-0000-0000-0000-000000000000",
    "state": "needs_review",
    "progress": None,
    "error": None,
    "created_at": "2026-01-01T00:00:00",
    "updated_at": "2026-01-01T00:00:00",
    "track_ref": {
        "internal_id": "track:test",
        "source": "cloud",
        "status": "acquiring",
        "title": "Test Track",
        "artist": "Test Artist",
        "album": None,
        "duration_seconds": 180,
        "provider": "ytdlp",
        "provider_ref": "https://youtu.be/test",
        "local_path": None,
        "musicbrainz_recording_id": None,
        "quality": None,
    },
}

# Minimal job — expose attribute access as dict-like for the templates
class _Obj:
    """Simple namespace object so templates can do both job.x and job['x']."""
    def __init__(self, d: dict[str, Any]) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                setattr(self, k, _Obj(v))
            else:
                setattr(self, k, v)
    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)
    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


def _job() -> _Obj:
    return _Obj(_JOB)


def _meta() -> dict:
    return {
        "title": "Test Track",
        "artist": "Test Artist",
        "albumartist": "Test Artist",
        "album": "Test Album",
        "year": 2024,
        "original_year": 2024,
        "track_number": 1,
        "disc_number": None,
        "duration_seconds": 180,
        "ext": "ogg",
        "mb_recording_id": None,
        "mb_release_id": None,
        "mb_artist_id": None,
        "mb_artist_sort": None,
        "isrc": None,
        "acoustid_confidence": None,
        "mb_match_source": None,
        "is_compilation": False,
        "force_staging_reason": None,
        "quality_score": 0.7,
        "thumbnail_url": None,
        "mb_genres": [],
        "genre": None,
        "current_title": None,
        "current_artist": None,
        "prov_title": "candidate",
        "prov_artist": "candidate",
        "prov_album": "candidate",
        "prov_year": "candidate",
        "prov_recording": "none",
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

def _render(template_name: str, ctx: dict) -> str:
    t = env.get_template(template_name)
    return t.render(**ctx)


def test_job_card_needs_review() -> None:
    html = _render("partials/job_card.html", {"job": _job()})
    assert "Test Track" in html


def test_job_card_done() -> None:
    j = _job()
    j.state = "done"
    j.track_ref.status = "available"
    html = _render("partials/job_card.html", {"job": j})
    assert "done" in html


def test_job_card_failed() -> None:
    j = _job()
    j.state = "failed"
    j.error = "Some error occurred"
    html = _render("partials/job_card.html", {"job": j})
    assert "failed" in html


def test_review_card_minimal() -> None:
    html = _render("partials/review_card.html", {
        "job_id": "abc12345",
        "meta": _meta(),
        "query": "Test Artist - Test Track",
        "staging_exists": True,
        "genres": [],
        "is_enrichment": False,
        "album_consistency_warning": None,
        "album_batch_label": None,
        "parsed_artists": ["Test Artist"],
        "show_multi_artists": False,
        "show_mb_search": False,
        "error": None,
    })
    assert "Test Track" in html
    assert "approve-btn-abc12345" in html


def test_review_card_acoustid_verified() -> None:
    meta = _meta()
    meta["mb_match_source"] = "acoustid"
    meta["acoustid_confidence"] = 0.92
    meta["mb_recording_id"] = "aaaabbbb-0000-0000-0000-000000000000"
    html = _render("partials/review_card.html", {
        "job_id": "abc12345",
        "meta": meta,
        "query": "Test Artist - Test Track",
        "staging_exists": True,
        "genres": [],
        "is_enrichment": False,
        "album_consistency_warning": None,
        "album_batch_label": None,
        "parsed_artists": ["Test Artist"],
        "show_multi_artists": False,
        "show_mb_search": False,
        "error": None,
    })
    assert "Verified" in html


def test_review_card_text_match() -> None:
    meta = _meta()
    meta["mb_match_source"] = "text_search"
    html = _render("partials/review_card.html", {
        "job_id": "abc12345",
        "meta": meta,
        "query": "Test Artist - Test Track",
        "staging_exists": True,
        "genres": [],
        "is_enrichment": False,
        "album_consistency_warning": None,
        "album_batch_label": None,
        "parsed_artists": ["Test Artist"],
        "show_multi_artists": False,
        "show_mb_search": False,
        "error": None,
    })
    assert "Probable" in html


def test_review_card_missing_staging() -> None:
    html = _render("partials/review_card.html", {
        "job_id": "abc12345",
        "meta": _meta(),
        "query": "",
        "staging_exists": False,
        "genres": [],
        "is_enrichment": False,
        "album_consistency_warning": None,
        "album_batch_label": None,
        "parsed_artists": ["Test Artist"],
        "show_multi_artists": False,
        "show_mb_search": False,
        "error": None,
    })
    assert "Re-download" in html


def test_review_card_with_album_batch() -> None:
    html = _render("partials/review_card.html", {
        "job_id": "abc12345",
        "meta": _meta(),
        "query": "",
        "staging_exists": True,
        "genres": ["Rock", "Pop"],
        "is_enrichment": False,
        "album_consistency_warning": "Album artist mismatch: ...",
        "album_batch_label": "The Beatles — Abbey Road",
        "parsed_artists": ["Test Artist"],
        "show_multi_artists": False,
        "show_mb_search": False,
        "error": None,
    })
    assert "Abbey Road" in html


def test_review_card_flagged_with_album_batch() -> None:
    meta = _meta()
    meta["force_staging_reason"] = "Artist mismatch: expected 'Beatles', fingerprint says 'Lennon'"
    meta["album"] = "Abbey Road"
    html = _render("partials/review_card.html", {
        "job_id": "abc12345",
        "meta": meta,
        "query": "",
        "staging_exists": True,
        "genres": [],
        "is_enrichment": False,
        "album_consistency_warning": None,
        "album_batch_label": "The Beatles — Abbey Road",
        "parsed_artists": ["Test Artist"],
        "show_multi_artists": False,
        "show_mb_search": False,
        "error": None,
    })
    assert "Flagged" in html
    assert "Keep album grouping" in html
    assert "data-album" in html


def test_browse_row() -> None:
    class _Track:
        id = "test:track:1"
        title = "Test Track"
        artist_id = "artist:1"
        duration_seconds = 180
        track_number = 1
        musicbrainz_recording_id = None
        tag_quality_score = 0.75

        class artist:
            name = "Test Artist"

        class album:
            title = "Test Album"
            year = 2024

        class file:
            has_cover_art = False
            codec = "opus"
            bitrate_kbps = 160

    html = _render("partials/browse_row.html", {"t": _Track()})
    assert "Test Track" in html
    assert "Verified" in html  # quality_score 0.75 >= 0.7


def test_browse_row_no_file() -> None:
    class _Track:
        id = "test:track:2"
        title = "No File Track"
        artist_id = "artist:1"
        duration_seconds = None
        track_number = None
        musicbrainz_recording_id = None
        tag_quality_score = 0.3

        class artist:
            name = "Test Artist"

        album = None
        file = None

    html = _render("partials/browse_row.html", {"t": _Track()})
    assert "No File Track" in html
    assert "Needs Review" in html  # quality_score 0.3 < 0.4
