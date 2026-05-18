"""MusicBrainz lookup with on-disk JSON response cache.

Rate limiting is handled by musicbrainzngs internally (1 req/sec by default).
Disk cache TTL is 24 h. Pass cache_dir=None to bypass cache (tests only).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import musicbrainzngs

from service.search.matcher import DEDUP_THRESHOLD, track_similarity

logger = logging.getLogger(__name__)

musicbrainzngs.set_useragent("audioreap", "0.1", "https://github.com/Gjessing1/audioreap")

_CACHE_TTL = 86400  # 24 hours


@dataclass
class MBRecording:
    recording_id: str
    title: str
    artist: str
    album: str | None
    year: int | None
    track_number: int | None
    score: float


def _cache_key(title: str, artist: str) -> str:
    payload = f"{title.lower()}|{artist.lower()}"
    return hashlib.sha1(payload.encode()).hexdigest()


def _cache_path(cache_dir: Path, key: str) -> Path:
    return cache_dir / "musicbrainz" / f"{key}.json"


def _load_cache(cache_dir: Path, key: str) -> dict[str, object] | None:
    path = _cache_path(cache_dir, key)
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > _CACHE_TTL:
        return None
    try:
        return json.loads(path.read_text())  # type: ignore[return-value]
    except Exception:
        return None


def _save_cache(cache_dir: Path, key: str, data: dict[str, object]) -> None:
    path = _cache_path(cache_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _extract_year(release: dict[str, object]) -> int | None:
    date = str(release.get("date") or "")
    if date and len(date) >= 4:
        try:
            return int(date[:4])
        except ValueError:
            pass
    return None


def _parse_recording(rec: dict[str, object]) -> MBRecording:
    rid = str(rec.get("id", ""))
    title = str(rec.get("title", ""))
    score = int(rec.get("ext:score", 0)) / 100.0

    artist_credits = rec.get("artist-credit") or []
    artist = ""
    if isinstance(artist_credits, list) and artist_credits:
        first = artist_credits[0]
        if isinstance(first, dict):
            a = first.get("artist") or {}
            if isinstance(a, dict):
                artist = str(a.get("name", ""))

    album: str | None = None
    year: int | None = None
    track_number: int | None = None

    releases = rec.get("release-list") or []
    if isinstance(releases, list) and releases:
        release = releases[0]
        if isinstance(release, dict):
            album = str(release.get("title") or "")
            year = _extract_year(release)
            medium_list = release.get("medium-list") or []
            if isinstance(medium_list, list) and medium_list:
                medium = medium_list[0]
                if isinstance(medium, dict):
                    track_list = medium.get("track-list") or []
                    if isinstance(track_list, list) and track_list:
                        track = track_list[0]
                        if isinstance(track, dict):
                            pos = track.get("position") or track.get("number")
                            if pos:
                                try:
                                    track_number = int(pos)
                                except (ValueError, TypeError):
                                    pass

    return MBRecording(
        recording_id=rid,
        title=title,
        artist=artist,
        album=album or None,
        year=year,
        track_number=track_number,
        score=score,
    )


def lookup_recording(
    title: str,
    artist: str,
    duration_seconds: int | None = None,
    cache_dir: Path | None = None,
    confidence_threshold: float = DEDUP_THRESHOLD,
) -> MBRecording | None:
    """Search MusicBrainz for a recording. Returns best match or None.

    Results are cached on disk. Pass cache_dir=None to skip disk cache (tests).
    """
    key = _cache_key(title, artist)

    raw: dict[str, object] | None = None
    if cache_dir is not None:
        raw = _load_cache(cache_dir, key)

    if raw is None:
        try:
            result = musicbrainzngs.search_recordings(
                recording=title,
                artistname=artist,
                limit=5,
            )
            raw = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key, raw)
        except Exception as exc:
            logger.warning("MusicBrainz lookup failed for %r / %r: %s", title, artist, exc)
            return None
    else:
        logger.debug("MB cache hit: %s", key)

    recordings = raw.get("recording-list") or []
    if not isinstance(recordings, list) or not recordings:
        return None

    best: MBRecording | None = None
    best_sim = 0.0
    for rec in recordings:
        if not isinstance(rec, dict):
            continue
        parsed = _parse_recording(rec)
        sim = track_similarity(
            title, artist, duration_seconds,
            parsed.title, parsed.artist, None,
        )
        if sim > best_sim:
            best_sim = sim
            best = parsed

    if best is None or best_sim < confidence_threshold:
        logger.debug("No confident MB match for %r / %r (best=%.2f)", title, artist, best_sim)
        return None

    logger.info("MB match: %r → %s (sim=%.2f)", title, best.recording_id, best_sim)
    return best
