"""Fuzzy track matching and deduplication.

All comparisons go through normalize() first, so "(feat. X)", "(Official Video)",
diacritics, and case differences don't affect match scores.

Token-based Jaccard similarity is simple, fast, and works well on short
music metadata strings. No external fuzzy-match dependencies needed.
"""
from __future__ import annotations

import dataclasses
import re

from service.core.normalize import extract_modifiers, normalize

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


# ── Same song, different version note ─────────────────────────────────────────
# Qualifiers that name a mastering, mix or edit of the same performance. Live,
# remix, acoustic, demo and take are deliberately absent: those are a different
# recording, and a live file standing in for a studio track is exactly what the
# modifier gates exist to catch.
_VERSION_WORDS = (
    r"(?:re-?master(?:ed)?|mono|stereo|\d(?:\.\d)?\s*(?:mix|surround)|"
    r"(?:mono|stereo|original|album|single|radio|lp|45)\s+(?:mix|edit|version)|"
    r"edit|bonus\s+track|explicit|clean|feat\.?|ft\.|featuring)"
)
_VERSION_BRACKET = re.compile(
    rf"\s*[\[(][^\])[]*\b{_VERSION_WORDS}(?![\w])[^\])[]*[\])]", re.IGNORECASE
)
_VERSION_DASH = re.compile(
    rf"\s+[-–]\s+[^-–]*\b{_VERSION_WORDS}(?![\w])[^-–]*$", re.IGNORECASE
)
_TRAILING_BRACKET = re.compile(r"\s*[\[(]([^\])[]+)[\])]\s*$")
# A trailing parenthetical that numbers or sequences the piece is a different
# track, and one naming a mix or version that _VERSION_WORDS does not cover is a
# different recording — neither is an alternative title: "Song (Part 2)",
# "Song (Reprise)", "Song (Club Mix)".
_NOT_A_SUBTITLE = re.compile(
    r"\b(?:part|pt\.?|vol\.?|chapter|movement|reprise|interlude|intro|outro|"
    r"continued|cont\.?|mix|version|edit|dub|rework|extended|\d+)\b",
    re.IGNORECASE,
)


def _strip_version_qualifiers(title: str) -> str:
    return _VERSION_DASH.sub("", _VERSION_BRACKET.sub("", title)).strip()


def _strip_subtitle(title: str) -> str | None:
    m = _TRAILING_BRACKET.search(title)
    if not m or _NOT_A_SUBTITLE.search(m.group(1)):
        return None
    return title[: m.start()].strip() or None


def same_song_title(a: str | None, b: str | None, threshold: float = 0.85) -> bool:
    """Whether two titles name the same song, allowing version qualifiers and
    an alternative-title subtitle on either side.

    A trailing parenthetical defeats plain token similarity even when the titles
    are otherwise identical: "The Losing End (When You're On)" is MB's long title
    for "The Losing End", and "Words (…) (5.1 mix)" is a mix of "Words (…)".
    Titles whose live/remix/acoustic/… modifiers differ are never the same song.
    """
    if not a or not b:
        return False
    ma = dataclasses.replace(extract_modifiers(a), is_explicit=False)
    mb = dataclasses.replace(extract_modifiers(b), is_explicit=False)
    if ma != mb:
        return False
    if title_similarity(a, b) >= threshold:
        return True
    sa, sb = _strip_version_qualifiers(a) or a, _strip_version_qualifiers(b) or b
    if title_similarity(sa, sb) >= threshold:
        return True
    # One title is the other plus a subtitle — compare against the shorter form.
    for longer, shorter in ((sa, sb), (sb, sa)):
        base = _strip_subtitle(longer)
        if base and title_similarity(base, shorter) >= threshold:
            return True
    return False


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
