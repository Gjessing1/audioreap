"""Contract tests for ResolvedTrackMetadata — the typed Phase 1 → Phase 2 handoff.

These guard the refactor that replaced the untyped resolved_metadata dict:
- producer/consumer JSON shape stays stable (webui dict readers depend on it)
- legacy / enrichment rows with extra keys round-trip without loss
- empty / partial payloads deserialize to sensible defaults (no validation error)
"""
from __future__ import annotations

import json

from service.core.models import ResolvedTrackMetadata


def test_defaults_from_empty_json() -> None:
    m = ResolvedTrackMetadata.model_validate_json("{}")
    assert m.title == "Unknown"
    assert m.artist == "Unknown"
    assert m.album is None
    assert m.ext == "ogg"
    assert m.is_enrichment is False
    assert m.is_replacement is False
    assert m.mb_genres == []


def test_round_trip_preserves_fields() -> None:
    m = ResolvedTrackMetadata(
        title="Paranoid Android",
        artist="Radiohead",
        albumartist="Radiohead",
        album="OK Computer",
        year=1997,
        track_number=2,
        duration_seconds=383,
        mb_recording_id="rec-123",
        mb_match_source="text_search",
        text_search_similarity=0.91,
        mb_genres=["alternative rock"],
        quality_score=0.86,
    )
    dumped = m.model_dump_json()
    again = ResolvedTrackMetadata.model_validate_json(dumped)
    assert again.title == "Paranoid Android"
    assert again.year == 1997
    assert again.text_search_similarity == 0.91
    assert again.mb_genres == ["alternative rock"]


def test_json_shape_has_keys_webui_reads() -> None:
    """webui display paths still json.loads() the blob and read these keys as a dict."""
    blob = json.loads(ResolvedTrackMetadata(title="X", artist="Y").model_dump_json())
    for key in (
        "title", "artist", "albumartist", "album", "year", "track_number",
        "mb_recording_id", "mb_match_source", "text_search_similarity",
        "force_staging_reason", "thumbnail_url", "mb_genres", "is_replacement",
        "prov_title", "prov_recording",
    ):
        assert key in blob, f"missing key {key!r} in dumped JSON"


def test_enrichment_extra_keys_round_trip() -> None:
    """Enrichment rows carry current_* keys and is_enrichment — extras must survive."""
    legacy = json.dumps({
        "title": "New Title",
        "artist": "Artist",
        "is_enrichment": True,
        "current_title": "Old Title",
        "current_artist": "Old Artist",
        "current_mb_recording_id": None,
        "some_future_field": 42,
    })
    m = ResolvedTrackMetadata.model_validate_json(legacy)
    assert m.is_enrichment is True
    assert m.title == "New Title"
    # Extras are preserved on re-dump
    out = json.loads(m.model_dump_json())
    assert out["current_title"] == "Old Title"
    assert out["some_future_field"] == 42


def test_legacy_string_year_coerced() -> None:
    m = ResolvedTrackMetadata.model_validate_json(json.dumps({"year": "1991"}))
    assert m.year == 1991


def test_setattr_override_clears_field() -> None:
    """place_approved_track applies overrides via setattr, including clearing to None."""
    m = ResolvedTrackMetadata(title="A", album="Some Album")
    setattr(m, "album", None)
    assert m.album is None
