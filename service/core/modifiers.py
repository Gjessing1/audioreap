"""Shared recording-modifier helpers (Phase 3).

Two related concerns live here so identification, YouTube source scoring, and the
web UI all share one definition of "what kind of recording is this":

* :func:`looks_like_live` — bracket-aware heuristic over a raw YouTube title,
  used to penalise live/cover/tribute videos when a studio take is wanted.
* :func:`modifier_mismatch_reason` — the Phase 3 incompatibility gate: compares
  the *source* intent flags against the *MB winner* flags and returns a
  force-staging reason when they are incompatible (e.g. a live source silently
  matched to a studio recording).

The structured flag model itself (:class:`ModifierFlags` / :func:`extract_modifiers`)
lives in :mod:`service.core.normalize`; this module builds on it.
"""
from __future__ import annotations

import re

from service.core.normalize import ModifierFlags

_LIVE_KEYWORDS = frozenset({
    "live", "concert", "in concert", "at the", "at madison", "tour",
    "tribute", "cover", "karaoke", "instrumental", "acoustic",
    "session", "radio edit", "bbc", "unplugged", "bootleg",
})


def looks_like_live(title: str) -> bool:
    """Return True if a YouTube title suggests a live/cover/tribute version."""
    lower = title.lower()
    bracketed = re.findall(r'[\(\[\{]([^\)\]\}]+)[\)\]\}]', lower)
    for chunk in bracketed:
        words = set(chunk.split())
        if words & _LIVE_KEYWORDS:
            return True
    if re.search(r'\blive\b', lower) or re.search(r'\btribute\b', lower):
        return True
    return False


def modifier_mismatch_reason(source: ModifierFlags, mb: ModifierFlags) -> str | None:
    """Return a force-staging reason when source intent is incompatible with the MB winner.

    ``source`` is :func:`extract_modifiers` over the download's title + user query;
    ``mb`` is the same over the winning MB recording's title + album. We only gate
    the *catastrophic* directions — a specifically-modified source matched to an
    unmarked (studio / original) recording — never the reverse (a plain query can
    legitimately resolve to a recording MB happens to disambiguate).

    AcoustID may rescue a weak text match but must not override this gate; callers
    apply it regardless of match source.
    """
    if source.is_live and not mb.is_live:
        return "source looks like a live recording but matched a studio recording"
    if (source.is_cover or source.is_karaoke) and not (mb.is_cover or mb.is_karaoke):
        return "source looks like a cover/karaoke but matched the original recording"
    return None
