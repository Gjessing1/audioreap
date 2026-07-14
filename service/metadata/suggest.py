"""'For you' acquisition suggestions — proactive discovery from the owned library.

Two MusicBrainz signals, cross-referenced against what is NOT yet owned:

1. **Relationships** — band members, side projects, collaborations of owned
   artists (via get_related_artists). Strong signal: weight 2.0 per link.
2. **Genre/tag overlap** — MB artists tagged with the library's most common
   genres (via search_artists_by_tag). Weaker, broader signal: weight scales
   with MB's own relevance score.

All underlying MB calls go through the 24 h disk cache, so recomputation after
the first build is cheap. The caller (webui route) samples owned artists and
top genres from the DB and runs this sync function in a thread.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from service.core.normalize import normalize
from service.metadata.musicbrainz import get_related_artists, search_artists_by_tag

logger = logging.getLogger(__name__)

# Bound MB traffic on a cold cache: at 1 req/s a full build costs at most
# MAX_SEED_ARTISTS + MAX_SEED_GENRES requests (~11 s worst case, then cached).
MAX_SEED_ARTISTS = 8
MAX_SEED_GENRES = 3

_RELATION_WEIGHT = 2.0
_TAG_BASE_WEIGHT = 0.4
_TAG_SCORE_WEIGHT = 0.6


@dataclass
class Suggestion:
    artist_id: str
    name: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)  # human "why": shown as tooltip


def build_for_you(
    seed_artists: list[tuple[str, str]],
    seed_genres: list[str],
    owned_names: set[str],
    cache_dir: Path | None,
    limit: int = 16,
) -> list[Suggestion]:
    """Rank un-owned MB artists connected to the library.

    seed_artists: (mb_artist_id, name) of the owned artists to expand from
    (caller picks the most-listened few). seed_genres: the library's top genre
    strings. owned_names: normalized names of ALL owned artists — exclusion set,
    so suggestions never include something already in the library.
    """
    seed_ids = {mbid for mbid, _ in seed_artists}
    pool: dict[str, Suggestion] = {}

    def _entry(artist_id: str, name: str) -> Suggestion:
        return pool.setdefault(artist_id, Suggestion(artist_id=artist_id, name=name))

    for mbid, seed_name in seed_artists[:MAX_SEED_ARTISTS]:
        for rel in get_related_artists(mbid, cache_dir):
            if rel.artist_id in seed_ids or normalize(rel.name) in owned_names:
                continue
            s = _entry(rel.artist_id, rel.name)
            s.score += _RELATION_WEIGHT
            s.reasons.append(f"{rel.relation} · {seed_name}")

    for genre in seed_genres[:MAX_SEED_GENRES]:
        tag = genre.strip().lower()
        if not tag:
            continue
        for a in search_artists_by_tag(tag, limit=15, cache_dir=cache_dir):
            if a.artist_id in seed_ids or normalize(a.name) in owned_names:
                continue
            s = _entry(a.artist_id, a.name)
            s.score += _TAG_BASE_WEIGHT + _TAG_SCORE_WEIGHT * a.score
            s.reasons.append(f"tagged {tag}")

    ranked = sorted(pool.values(), key=lambda s: s.score, reverse=True)
    return ranked[:limit]
