"""Unit tests for Phase 4 staged Lucene query construction (musicbrainz.py)."""
from __future__ import annotations

from service.metadata.musicbrainz import (
    _and_field,
    _lucene_escape,
    _lucene_phrase,
    _staged_recording_queries,
)


# ── escaping ──────────────────────────────────────────────────────────────────

def test_lucene_escape_specials() -> None:
    assert _lucene_escape("AC/DC") == r"AC\/DC"
    assert _lucene_escape("Song: Reprise") == r"Song\: Reprise"
    assert _lucene_escape("Hey (You)") == r"Hey \(You\)"
    assert _lucene_escape("100%") == "100%"  # % is not special


def test_lucene_escape_boolean_operators_neutralised() -> None:
    # && / || would be parsed as Lucene operators; we blank them out.
    assert "&&" not in _lucene_escape("rock && roll")
    assert "||" not in _lucene_escape("this || that")


def test_lucene_phrase_quotes_and_escapes() -> None:
    assert _lucene_phrase("Paranoid Android") == '"Paranoid Android"'
    assert _lucene_phrase('Say "Hi"') == '"Say \\"Hi\\""'


def test_and_field_builds_token_conjunction() -> None:
    assert _and_field("recording", "paranoid android") == "recording:(paranoid AND android)"
    assert _and_field("artist", "") is None


# ── staged query plan ─────────────────────────────────────────────────────────

def test_staged_basic_plan() -> None:
    qs = _staged_recording_queries("Paranoid Android", "Radiohead", 383, None)
    assert len(qs) <= 4
    # Stage 1 is the strict phrase with artist + duration window.
    assert qs[0].startswith('recording:"Paranoid Android" AND artist:"Radiohead"')
    assert "dur:[" in qs[0]
    # A relaxed structured stage with AND'd tokens follows.
    assert any("recording:(paranoid AND android)" in q for q in qs)


def test_staged_includes_rgid_when_preferred() -> None:
    rgid = "b1392450-e666-3926-a536-22c65f834433"
    qs = _staged_recording_queries("Creep", "Radiohead", None, rgid)
    assert f"rgid:{rgid}" in qs[0]  # MBID passes through unescaped


def test_staged_modifier_stripped_stage() -> None:
    qs = _staged_recording_queries("Creep (Live at Reading)", "Radiohead", None, None)
    # A stage with the live/at modifiers stripped from the recording tokens.
    assert any(
        q.startswith("recording:(creep") and "live" not in q.lower() for q in qs
    )


def test_staged_dedup_and_budget() -> None:
    # Empty artist collapses the relaxed + title-only stages onto the same query.
    qs = _staged_recording_queries("Creep", "", 240, None)
    assert len(qs) == len(set(qs))      # no duplicates
    assert len(qs) <= 4                 # budget respected


def test_staged_title_only_fallback_present() -> None:
    qs = _staged_recording_queries("Creep", "Radiohead", None, None)
    # Last-resort title-only query (no artist constraint) is in the plan.
    assert any(q == "recording:(creep)" for q in qs)
