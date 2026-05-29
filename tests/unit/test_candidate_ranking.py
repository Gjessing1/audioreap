"""Unit tests for metadata.candidates.rank_candidates (pipeline Path B scoring)."""
from __future__ import annotations

from dataclasses import dataclass

from service.metadata.candidates import ACOUSTID_WEIGHT, QUERY_WEIGHT, rank_candidates


@dataclass
class _Rec:
    """Duck-typed stand-in for MBRecording (rank_candidates only needs these)."""
    recording_id: str
    title: str
    artist: str


def test_ranks_by_text_sim_when_no_other_signals() -> None:
    cands = [(_Rec("a", "Song A", "Artist"), 0.6), (_Rec("b", "Song B", "Artist"), 0.9)]
    ranked = rank_candidates(cands, clean_query=None, acoustid_mbid=None)
    assert [c.recording.recording_id for c in ranked] == ["b", "a"]
    assert ranked[0].combined == 0.9
    assert ranked[0].query_sim == 0.0
    assert ranked[0].acoustid_match is False


def test_acoustid_match_adds_weight() -> None:
    cands = [(_Rec("a", "X", "Y"), 0.70), (_Rec("b", "X", "Y"), 0.72)]
    # AcoustID points at the lower text_sim candidate; +0.15 should flip the order.
    ranked = rank_candidates(cands, clean_query=None, acoustid_mbid="a")
    assert ranked[0].recording.recording_id == "a"
    assert ranked[0].acoustid_match is True
    assert abs(ranked[0].combined - (0.70 + ACOUSTID_WEIGHT)) < 1e-9


def test_query_signal_contributes_weighted() -> None:
    rec = _Rec("a", "Paranoid Android", "Radiohead")
    ranked = rank_candidates([(rec, 0.5)], clean_query="paranoid android radiohead", acoustid_mbid=None)
    c = ranked[0]
    assert c.query_sim > 0.9  # near-exact query match
    assert abs(c.combined - (0.5 + QUERY_WEIGHT * c.query_sim)) < 1e-9


def test_empty_pool() -> None:
    assert rank_candidates([], clean_query=None, acoustid_mbid=None) == []
