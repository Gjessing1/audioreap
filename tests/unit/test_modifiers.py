"""Unit tests for core.modifiers (Phase 3 incompatibility gates + live heuristic)."""
from __future__ import annotations

import pytest

from service.core.modifiers import looks_like_live, modifier_mismatch_reason
from service.core.normalize import extract_modifiers


def _flags(text: str):
    return extract_modifiers(text)


# ── modifier_mismatch_reason ──────────────────────────────────────────────────

def test_live_source_vs_studio_winner_is_gated() -> None:
    src = _flags("Creep (Live at Reading)")
    mb = _flags("Creep Pablo Honey")  # studio: no live marker
    assert modifier_mismatch_reason(src, mb) is not None


def test_live_source_vs_live_winner_passes() -> None:
    src = _flags("Creep (Live at Reading)")
    mb = _flags("Creep (Live) I Might Be Wrong: Live Recordings")
    assert modifier_mismatch_reason(src, mb) is None


def test_cover_source_vs_original_is_gated() -> None:
    src = _flags("Hurt (Johnny Cash cover)")
    mb = _flags("Hurt The Downward Spiral")
    reason = modifier_mismatch_reason(src, mb)
    assert reason is not None and "cover" in reason


def test_karaoke_source_vs_original_is_gated() -> None:
    src = _flags("Bohemian Rhapsody (Karaoke Version)")
    mb = _flags("Bohemian Rhapsody A Night at the Opera")
    assert modifier_mismatch_reason(src, mb) is not None


def test_plain_source_is_never_gated() -> None:
    # A clean query that MB happens to disambiguate must not be force-staged.
    src = _flags("Creep")
    assert modifier_mismatch_reason(src, _flags("Creep Pablo Honey")) is None
    assert modifier_mismatch_reason(src, _flags("Creep (Live)")) is None


def test_remix_is_not_gated() -> None:
    # Only live/cover/karaoke are gated; remix/acoustic are advisory, not catastrophic.
    src = _flags("Get Lucky (Remix)")
    assert modifier_mismatch_reason(src, _flags("Get Lucky Random Access Memories")) is None


# ── looks_like_live (relocated from providers.ytdlp) ──────────────────────────

@pytest.mark.parametrize("title", [
    "Song (Live)", "Song [Live at Wembley]", "Artist - Song (Acoustic Session)",
    "Song (Karaoke)", "Imagine - Tribute", "Song (Unplugged)",
])
def test_looks_like_live_true(title: str) -> None:
    assert looks_like_live(title) is True


@pytest.mark.parametrize("title", [
    "Stayin' Alive", "Paranoid Android", "Song (Official Video)",
])
def test_looks_like_live_false(title: str) -> None:
    assert looks_like_live(title) is False
