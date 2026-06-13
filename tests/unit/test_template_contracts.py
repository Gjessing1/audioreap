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
    "is_replacement": False,
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
        "text_search_similarity": None,
        "mb_match_source": None,
        "mb_release_group_id": None,
        "source_codec": "opus",
        "source_bitrate_kbps": 160,
        "is_compilation": False,
        "force_staging_reason": None,
        "quality_score": 0.7,
        "thumbnail_url": None,
        "mb_genres": [],
        "candidates": [],
        "genre": None,
        "current_title": None,
        "current_artist": None,
        "current_album": None,
        "current_year": None,
        "current_track_number": None,
        "current_mb_recording_id": None,
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


def _review_ctx(**overrides: Any) -> dict:
    """Full review_card context mirroring webui._review_card_ctx() return keys."""
    ctx: dict[str, Any] = {
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
        "show_src_panel": False,
        "source_url": "https://www.youtube.com/watch?v=test",
        "artist_names": ["Test Artist"],
        "album_names": ["Test Album"],
        "candidate_track_number": None,
        "error": None,
    }
    ctx.update(overrides)
    return ctx


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
    # Failed jobs expose the manual source-replacement entry point.
    assert "/fix-source" in html


def test_failed_source_card() -> None:
    html = _render("partials/failed_source_card.html", {
        "job_id": "abc12345",
        "want_artist": "Daft Punk",
        "want_title": "Derezzed",
        "source_url": "https://youtube.com/watch?v=dead",
        "error": "Video unavailable",
    })
    assert "Daft Punk" in html
    assert "Derezzed" in html
    assert "Video unavailable" in html
    assert "/jobs/abc12345/search-source" in html


def test_review_card_minimal() -> None:
    html = _render("partials/review_card.html", _review_ctx())
    assert "Test Track" in html
    assert "approve-btn-abc12345" in html


def test_review_card_acoustid_verified() -> None:
    meta = _meta()
    meta["mb_match_source"] = "acoustid"
    meta["acoustid_confidence"] = 0.92
    meta["mb_recording_id"] = "aaaabbbb-0000-0000-0000-000000000000"
    html = _render("partials/review_card.html", _review_ctx(meta=meta))
    assert "AcoustID confirmed" in html
    assert "92%" in html


def test_review_card_text_match() -> None:
    meta = _meta()
    meta["mb_match_source"] = "text_search"
    meta["text_search_similarity"] = 0.81
    html = _render("partials/review_card.html", _review_ctx(meta=meta))
    assert "text search" in html
    assert "81%" in html


def test_review_card_missing_staging() -> None:
    html = _render("partials/review_card.html",
                   _review_ctx(query="", staging_exists=False))
    assert "Re-download" in html


def test_review_card_with_album_batch() -> None:
    html = _render("partials/review_card.html", _review_ctx(
        query="",
        genres=["Rock", "Pop"],
        album_consistency_warning="Album artist mismatch: ...",
        album_batch_label="The Beatles — Abbey Road",
    ))
    assert "Abbey Road" in html


def test_review_card_flagged_with_album_batch() -> None:
    meta = _meta()
    meta["force_staging_reason"] = "Artist mismatch: expected 'Beatles', fingerprint says 'Lennon'"
    meta["album"] = "Abbey Road"
    html = _render("partials/review_card.html", _review_ctx(
        meta=meta,
        query="",
        album_batch_label="The Beatles — Abbey Road",
    ))
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
        genre = None
        quality_suppressed = False
        bitrate_suppressed = False
        stop = None

        class artist:
            name = "Test Artist"

        class album:
            title = "Test Album"
            year = 2024

        class file:
            has_cover_art = False
            codec = "opus"
            bitrate_kbps = 160

    html = _render("partials/browse_row.html",
                   {"t": _Track(), "settings": _Obj({"min_bitrate_kbps": 128})})
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
        genre = None
        quality_suppressed = False
        bitrate_suppressed = False
        stop = None

        class artist:
            name = "Test Artist"

        album = None
        file = None

    html = _render("partials/browse_row.html",
                   {"t": _Track(), "settings": _Obj({"min_bitrate_kbps": 128})})
    assert "No File Track" in html
    assert "Needs Review" in html  # quality_score 0.3 < 0.4


def test_job_card_confidence_border() -> None:
    """needs_review cards render a colour-coded confidence chip + data attribute."""
    for conf, label in (("verified", "verified"), ("probable", "probable"), ("flagged", "flagged")):
        html = _render("partials/job_card.html", {"job": _job(), "confidence": conf})
        assert f'data-confidence="{conf}"' in html
        assert label in html


def test_dest_preview() -> None:
    html = _render("partials/dest_preview.html", {
        "dest": "/music/Radiohead/OK Computer (1997)/03 - Paranoid Android.ogg",
        "joins_existing": True,
        "canonical_album": "OK Computer",
        "normalised_aa": None,
        "is_single": False,
    })
    assert "Paranoid Android" in html
    assert "OK Computer" in html


def test_view_browse_embeddable() -> None:
    html = _render("partials/view_browse.html",
                   {"q": "", "f": "low_quality", "sort": "artist", "genre": "", "genre_list": ["Rock"]})
    assert 'id="browse-results"' in html
    assert "filter-tab--active" in html  # low_quality tab marked active


def test_view_albums_embeddable() -> None:
    html = _render("partials/view_albums.html", {"q": "", "sort": "artist"})
    assert 'id="album-list"' in html


def test_view_artists_embeddable() -> None:
    html = _render("partials/view_artists.html", {"artists": [], "q": "", "sort": "name"})
    assert 'id="artist-list"' in html


def test_view_genres_embeddable() -> None:
    html = _render("partials/view_genres.html",
                   {"genres": [{"name": "Jazz", "count": 2}], "untagged_count": 0})
    assert 'id="genre-search"' in html
    assert "Jazz" in html


def test_genre_list_filterable() -> None:
    html = _render("partials/genre_list.html", {
        "genres": [{"name": "Electronic", "count": 12}, {"name": "Jazz", "count": 3}],
        "untagged_count": 5,
    })
    assert "Electronic" in html
    assert 'data-genre-name="electronic"' in html
    assert "genre-card-item" in html
    assert "genre-untagged-item" in html


def _edit_track(**over: Any) -> _Obj:
    base = {
        "id": "test:track:edit",
        "title": "Edit Me",
        "artist_id": "artist:1",
        "track_number": 3,
        "musicbrainz_recording_id": None,
        "tag_quality_score": 0.3,   # low → "Quality OK" control shows
        "quality_suppressed": False,
        "bitrate_suppressed": False,
        "genre": None,
        "artist": {"name": "Edit Artist"},
        "album": {"title": "Edit Album", "year": 2024,
                  "musicbrainz_release_id": None, "mb_release_group_id": None},
        "file": {"codec": "opus", "bitrate_kbps": 96, "has_cover_art": False,
                 "path": "/music/x.ogg", "provider_ref": "yt:abc"},
    }
    base.update(over)
    return _Obj(base)


def _edit_ctx(**over: Any) -> dict:
    ctx: dict[str, Any] = {
        "track": _edit_track(),
        "genre": None,
        "genres": [],
        "artist_names": ["Edit Artist"],
        "album_names": ["Edit Album"],
        "provider_ref": "yt:abc",
        "bitrate_kbps": 96,
        "min_bitrate_kbps": 128,
        "source_album_id": "",
        "open_art": False,
        "saved": False,
    }
    ctx.update(over)
    return ctx


def test_track_edit_card_renders() -> None:
    html = _render("partials/track_edit_card.html", _edit_ctx())
    assert "Edit Me" in html
    # Low quality + low bitrate → both suppression controls present in the card
    assert "Quality OK" in html
    assert "Bitrate OK" in html
    assert "from_edit=true" in html


def test_track_edit_card_suppressed() -> None:
    html = _render("partials/track_edit_card.html",
                   _edit_ctx(track=_edit_track(quality_suppressed=True, bitrate_suppressed=True)))
    assert "Re-flag quality" in html
    assert "Re-flag bitrate" in html


def test_dest_preview_single() -> None:
    html = _render("partials/dest_preview.html", {
        "dest": "/music/Singles/Aphex Twin/Avril 14th.ogg",
        "joins_existing": False,
        "canonical_album": None,
        "normalised_aa": None,
        "is_single": True,
    })
    assert "single" in html.lower()
