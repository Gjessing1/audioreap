"""Tag quality scoring.

A score from 0.0 to 1.0 reflecting how complete a track's metadata is.
Seven factors, each worth 1/7: title, artist, album, year, track_number,
MusicBrainz Recording ID, and cover art.
"""
from __future__ import annotations

import re

_GENERIC_TITLE = re.compile(r"^(unknown|untitled|track\s*\d+)$", re.IGNORECASE)
_GENERIC_ARTIST = re.compile(r"^(unknown|unknown artist|various artists?)$", re.IGNORECASE)

LOW_QUALITY_THRESHOLD = 4 / 7  # fewer than 4 of 7 fields = low quality


def compute_quality_score(
    title: str,
    artist: str,
    album: str | None,
    year: int | None,
    track_number: int | None,
    musicbrainz_recording_id: str | None,
    has_cover_art: bool,
) -> float:
    score = 0.0
    if title and not _GENERIC_TITLE.match(title.strip()):
        score += 1
    if artist and not _GENERIC_ARTIST.match(artist.strip()):
        score += 1
    if album:
        score += 1
    if year:
        score += 1
    if track_number:
        score += 1
    if musicbrainz_recording_id:
        score += 1
    if has_cover_art:
        score += 1
    return round(score / 7, 3)
