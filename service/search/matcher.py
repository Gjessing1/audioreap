"""Fuzzy track matching and deduplication.

All comparisons go through normalize() first, so "(feat. X)", "(Official Video)",
diacritics, and case differences don't affect match scores.

Token-based Jaccard similarity is simple, fast, and works well on short
music metadata strings. No external fuzzy-match dependencies needed.
"""
from __future__ import annotations

from service.core.normalize import normalize

# Similarity threshold above which a local track is considered a confident match
# and acquisition is skipped.
DEDUP_THRESHOLD = 0.85

# Duration must agree within this many seconds for a full match score.
DURATION_TOLERANCE_SECONDS = 10


def _jaccard(a: str, b: str) -> float:
    """Token-based Jaccard similarity of two already-normalized strings."""
    ta = set(a.split())
    tb = set(b.split())
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def title_similarity(a: str, b: str) -> float:
    return _jaccard(normalize(a), normalize(b))


def artist_similarity(a: str, b: str) -> float:
    return _jaccard(normalize(a), normalize(b))


def track_similarity(
    title_a: str,
    artist_a: str,
    duration_a: int | None,
    title_b: str,
    artist_b: str,
    duration_b: int | None,
) -> float:
    """Combined similarity score for two tracks. Returns 0.0–1.0.

    Weights: title 50%, artist 40%, duration agreement 10%.
    Duration disagreement beyond DURATION_TOLERANCE_SECONDS caps the score at 0.9.
    """
    t_sim = title_similarity(title_a, title_b)
    a_sim = artist_similarity(artist_a, artist_b)
    score = t_sim * 0.5 + a_sim * 0.4

    if duration_a and duration_b:
        if abs(duration_a - duration_b) <= DURATION_TOLERANCE_SECONDS:
            score += 0.1
        # else: no bonus, and cap so duration disagreement prevents confident match
        else:
            score = min(score, DEDUP_THRESHOLD - 0.01)  # duration mismatch blocks confident match
    else:
        score += 0.05  # partial duration bonus when unknown

    return min(score, 1.0)


def is_confident_match(
    title_a: str,
    artist_a: str,
    duration_a: int | None,
    title_b: str,
    artist_b: str,
    duration_b: int | None,
    threshold: float = DEDUP_THRESHOLD,
) -> bool:
    return track_similarity(title_a, artist_a, duration_a, title_b, artist_b, duration_b) >= threshold
