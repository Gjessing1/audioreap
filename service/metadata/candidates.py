"""Multi-signal candidate ranking for standalone acquisition (pipeline Path B).

Pure scoring over an already-fetched candidate pool — no I/O — so it is unit
testable and observable. The pipeline fetches the MB candidate pool and the
AcoustID fingerprint (with their own timeouts), optionally augments the pool with
an AcoustID-only recording, then calls :func:`rank_candidates` to score and order.

Score formula (unchanged from the original inline logic):

    combined = text_sim + 0.10 × query_sim + 0.15 × acoustid_match

where ``query_sim`` is the user's raw search string vs ``"{title} {artist}"`` and
``acoustid_match`` is 1.0 when the candidate's recording is the fingerprint hit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from service.search.matcher import title_similarity

if TYPE_CHECKING:
    from service.metadata.musicbrainz import MBRecording

QUERY_WEIGHT = 0.10
ACOUSTID_WEIGHT = 0.15


@dataclass
class ScoredCandidate:
    """One ranked candidate with its component score breakdown (for observability)."""

    recording: "MBRecording"
    text_sim: float
    query_sim: float          # user-query intent contribution (pre-weight)
    acoustid_match: bool      # True when this candidate is the AcoustID fingerprint hit
    combined: float           # final score actually used for ranking


def rank_candidates(
    candidates: list[tuple["MBRecording", float]],
    *,
    clean_query: str | None,
    acoustid_mbid: str | None,
) -> list[ScoredCandidate]:
    """Score every (recording, text_sim) pair and return them ranked best-first.

    ``candidates`` is the pool from ``get_recording_candidates`` (already including
    any AcoustID-only bonus recording the caller appended). ``clean_query`` is the
    cleaned user search string, or None to skip the query signal.
    """
    scored: list[ScoredCandidate] = []
    for rec, text_sim in candidates:
        query_sim = 0.0
        if clean_query:
            query_sim = title_similarity(clean_query, f"{rec.title} {rec.artist}")
        acoustid_match = bool(acoustid_mbid and rec.recording_id == acoustid_mbid)
        combined = text_sim + QUERY_WEIGHT * query_sim + (ACOUSTID_WEIGHT if acoustid_match else 0.0)
        scored.append(ScoredCandidate(
            recording=rec,
            text_sim=text_sim,
            query_sim=query_sim,
            acoustid_match=acoustid_match,
            combined=combined,
        ))
    scored.sort(key=lambda c: c.combined, reverse=True)
    return scored
