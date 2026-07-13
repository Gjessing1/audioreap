"""MusicBrainz lookup with on-disk JSON response cache.

Rate limiting is handled by musicbrainzngs internally (1 req/sec by default).
Disk cache TTL is 24 h. Pass cache_dir=None to bypass cache (tests only).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import musicbrainzngs

from service.search.matcher import DEDUP_THRESHOLD, title_similarity, track_similarity

logger = logging.getLogger(__name__)

musicbrainzngs.set_useragent("audioreap", "0.1", "https://github.com/Gjessing1/audioreap")

# musicbrainzngs' urllib opener has no timeout parameter — a stalled MB (or
# AcoustID, which shares the same gap) connection would otherwise hang a worker
# thread forever. The process-wide default only applies to blocking sockets
# that don't set their own timeout (httpx, yt-dlp, asyncio all do), so in
# practice this covers exactly the libraries that can't be configured directly.
socket.setdefaulttimeout(30)

_CACHE_TTL = 86400  # 24 hours


@dataclass
class MBArtist:
    artist_id: str
    name: str
    disambiguation: str | None
    score: float


@dataclass
class MBReleaseGroup:
    release_group_id: str
    title: str
    year: int | None
    release_type: str  # "Album", "EP", "Single", "Live", "Compilation", etc.


@dataclass
class MBReleaseGroupMatch:
    """One release-group search hit (richer than MBReleaseGroup: carries the
    credited artist and disambiguation for display in link-search results)."""
    release_group_id: str
    title: str
    artist: str
    release_type: str
    year: str  # "" when MB has no first-release-date
    disambiguation: str


@dataclass
class MBRecording:
    recording_id: str
    title: str
    artist: str
    album: str | None
    year: int | None
    track_number: int | None
    score: float
    release_id: str | None = None           # first release's MB release MBID (for CAA artwork)
    artist_id: str | None = None            # MB artist MBID (for MUSICBRAINZ_ARTISTID tag)
    artist_sort: str | None = None          # e.g. "Beatles, The" (for ARTISTSORT tag)
    original_year: int | None = None        # first-ever release year (for ORIGINALDATE tag)
    release_group_id: str | None = None     # MB release group MBID (for genre lookup)
    isrc: str | None = None                 # International Standard Recording Code
    duration_seconds: int | None = None     # recording length from MB (for duration weighting)


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
    artist_id: str | None = None
    artist_sort: str | None = None
    if isinstance(artist_credits, list) and artist_credits:
        first = artist_credits[0]
        if isinstance(first, dict):
            a = first.get("artist") or {}
            if isinstance(a, dict):
                artist = str(a.get("name", ""))
                artist_id = str(a.get("id") or "") or None
                sort_name = str(a.get("sort-name") or "").strip()
                if sort_name and sort_name != artist:
                    artist_sort = sort_name

    album: str | None = None
    year: int | None = None
    original_year: int | None = None
    track_number: int | None = None
    release_id: str | None = None
    release_group_id: str | None = None

    isrc: str | None = None
    isrc_list = rec.get("isrc-list")
    if isinstance(isrc_list, list) and isrc_list:
        isrc = str(isrc_list[0])

    duration_seconds: int | None = None
    length_ms = rec.get("length")
    if length_ms:
        try:
            duration_seconds = int(int(length_ms) / 1000)
        except (ValueError, TypeError):
            pass

    releases = rec.get("release-list") or []
    if isinstance(releases, list) and releases:
        release = releases[0]
        if isinstance(release, dict):
            album = str(release.get("title") or "")
            year = _extract_year(release)
            release_id = str(release.get("id") or "") or None
            rg = release.get("release-group") or {}
            if isinstance(rg, dict):
                release_group_id = str(rg.get("id") or "") or None
                rg_date = str(rg.get("first-release-date") or "")
                if rg_date and len(rg_date) >= 4:
                    try:
                        original_year = int(rg_date[:4])
                    except ValueError:
                        pass
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
        release_id=release_id,
        artist_id=artist_id,
        artist_sort=artist_sort,
        original_year=original_year,
        release_group_id=release_group_id,
        isrc=isrc,
        duration_seconds=duration_seconds,
    )


def lookup_recording(
    title: str,
    artist: str,
    duration_seconds: int | None = None,
    cache_dir: Path | None = None,
    confidence_threshold: float = DEDUP_THRESHOLD,
    preferred_release_group: str | None = None,
) -> MBRecording | None:
    """Search MusicBrainz for a recording. Returns best match or None.

    Results are cached on disk. Pass cache_dir=None to skip disk cache (tests).
    When preferred_release_group is set, recordings in that group get a small
    boost so album-context searches prefer the expected release.
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
            parsed.title, parsed.artist, parsed.duration_seconds,
        )
        # Cohesion boost for recordings in the preferred release group so that
        # album-context text searches stay within the expected release (Phase 5).
        if preferred_release_group and parsed.release_group_id == preferred_release_group:
            sim = min(sim + 0.10, 1.0)
        if sim > best_sim:
            best_sim = sim
            best = parsed

    if best is None or best_sim < confidence_threshold:
        logger.debug("No confident MB match for %r / %r (best=%.2f)", title, artist, best_sim)
        return None

    logger.info("MB match: %r → %s (sim=%.2f)", title, best.recording_id, best_sim)
    best.score = best_sim  # expose local similarity to callers
    return best


# ── Staged Lucene retrieval (Phase 4) ─────────────────────────────────────────
# Instead of one fuzzy structured query, build a sequence of explicit-field Lucene
# queries from most to least specific and run them SEQUENTIALLY, short-circuiting
# as soon as the accumulated pool contains a strong match. This widens retrieval
# breadth for noisy titles without query explosion (budget: ≤4 queries/track,
# never parallelised — MB throttles to 1 req/s).

_LUCENE_SPECIALS = re.compile(r'([+\-!(){}\[\]^"~*?:\\/])')

# Title tokens dropped in the modifier-stripped fallback stage so a noisy YouTube
# title ("… (Official Live Video) [Remastered]") can still reach the canonical
# MB recording. We REMOVE these here (vs. extract_modifiers which only flags them).
_MODIFIER_STOPWORDS = frozenset({
    "live", "remix", "remixed", "acoustic", "cover", "karaoke", "instrumental",
    "version", "remaster", "remastered", "official", "video", "audio", "lyrics",
    "lyric", "explicit", "clean", "hd", "hq", "feat", "ft", "featuring",
    "unplugged", "session", "edit", "radio",
})


def _lucene_escape(term: str) -> str:
    """Backslash-escape Lucene syntax so a user term is a safe query fragment."""
    term = term.replace("&&", " ").replace("||", " ")
    return _LUCENE_SPECIALS.sub(r"\\\1", term).strip()


def _lucene_phrase(term: str) -> str:
    """Quote a term as an exact Lucene phrase (escape embedded quote/backslash)."""
    inner = term.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{inner}"'


def _lucene_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _and_field(field: str, text: str) -> str | None:
    """Build ``field:(t1 AND t2 …)`` from escaped, lower-cased tokens, or None.

    Tokens are lower-cased for stable output; MB Lucene text matching is
    case-insensitive so this changes nothing functionally.
    """
    toks = [e for e in (_lucene_escape(t.lower()) for t in _lucene_tokens(text)) if e]
    if not toks:
        return None
    return f"{field}:(" + " AND ".join(toks) + ")"


def _safe_mbid(value: str) -> str:
    """Keep only UUID-legal characters — MBID fields must not be Lucene-escaped."""
    return re.sub(r"[^0-9a-fA-F-]", "", value)


def _staged_recording_queries(
    title: str,
    artist: str,
    duration_seconds: int | None,
    preferred_release_group: str | None,
) -> list[str]:
    """Return Lucene queries from most to least specific (deduped, ≤4)."""
    title = title.strip()
    artist = artist.strip()
    ra = _and_field("artist", artist)
    queries: list[str] = []

    # Stage 1 — strict phrase (+ duration window + release-group when known)
    s1 = f"recording:{_lucene_phrase(title)}"
    if artist:
        s1 += f" AND artist:{_lucene_phrase(artist)}"
    if duration_seconds:
        lo = max(0, duration_seconds - 10) * 1000
        hi = (duration_seconds + 10) * 1000
        s1 += f" AND dur:[{lo} TO {hi}]"
    if preferred_release_group:
        s1 += f" AND rgid:{_safe_mbid(preferred_release_group)}"
    queries.append(s1)

    # Stage 2 — relaxed structured: all tokens present, any order, no duration gate
    rt = _and_field("recording", title)
    if rt:
        queries.append(rt + (f" AND {ra}" if ra else ""))

    # Stage 3 — modifier-stripped title + artist. Strip punctuation to alnum words
    # so bracketed modifiers ("(Live)") don't survive as attached tokens.
    words = re.findall(r"[0-9a-zA-Z']+", title)
    stripped = " ".join(w for w in words if w.lower() not in _MODIFIER_STOPWORDS)
    rt3 = _and_field("recording", stripped)
    if rt3 and stripped.lower() != title.lower():
        queries.append(rt3 + (f" AND {ra}" if ra else ""))

    # Stage 4 — title-only (drop artist constraint entirely)
    rt4 = _and_field("recording", stripped or title)
    if rt4:
        queries.append(rt4)

    # Dedup preserving order, cap at the 4-query budget.
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:4]


def _staged_recording_search(
    title: str,
    artist: str,
    duration_seconds: int | None,
    limit: int,
    preferred_release_group: str | None,
) -> list[dict]:
    """Run staged queries sequentially, stopping once the pool is strong enough.

    Returns merged raw recording dicts (deduped by MBID, first-seen order).
    """
    merged: dict[str, dict] = {}
    for q in _staged_recording_queries(title, artist, duration_seconds, preferred_release_group):
        try:
            result = musicbrainzngs.search_recordings(query=q, limit=limit)
        except Exception as exc:
            logger.warning("MB staged query failed (%s): %s", q, exc)
            continue
        for rec in result.get("recording-list") or []:
            if isinstance(rec, dict):
                rid = str(rec.get("id", ""))
                if rid and rid not in merged:
                    merged[rid] = rec
        # Sufficiency: stop as soon as any candidate clears the confidence bar.
        best = 0.0
        for rec in merged.values():
            parsed = _parse_recording(rec)
            best = max(best, track_similarity(
                title, artist, duration_seconds,
                parsed.title, parsed.artist, parsed.duration_seconds,
            ))
            if best >= DEDUP_THRESHOLD:
                break
        logger.debug("MB staged: %d candidates after %r (best_sim=%.2f)", len(merged), q, best)
        if best >= DEDUP_THRESHOLD:
            break
    return list(merged.values())


def get_recording_candidates(
    title: str,
    artist: str,
    duration_seconds: int | None = None,
    cache_dir: Path | None = None,
    preferred_release_group: str | None = None,
    min_sim: float = 0.40,
    limit: int = 10,
) -> list[tuple["MBRecording", float]]:
    """Return all plausible (recording, text_sim) pairs for multi-signal ranking.

    Retrieval uses staged explicit-field Lucene queries (see
    :func:`_staged_recording_search`). The low min_sim floor lets the caller apply
    additional signals (user query intent, AcoustID boost) before choosing the
    winner and applying the final confidence threshold.
    """
    # Key includes duration + release-group because they shape the staged pool.
    key = f"cand:{_cache_key(title, artist)}:{duration_seconds or 0}:{preferred_release_group or ''}"
    raw: dict[str, object] | None = None
    if cache_dir is not None:
        raw = _load_cache(cache_dir, key)

    if raw is None:
        records = _staged_recording_search(
            title, artist, duration_seconds, limit, preferred_release_group
        )
        raw = {"recording-list": records}
        if cache_dir is not None:
            _save_cache(cache_dir, key, raw)
    else:
        logger.debug("MB cache hit (candidates): %s", key)

    recordings = raw.get("recording-list") or []
    if not isinstance(recordings, list):
        return []

    out: list[tuple[MBRecording, float]] = []
    for rec in recordings:
        if not isinstance(rec, dict):
            continue
        parsed = _parse_recording(rec)
        sim = track_similarity(
            title, artist, duration_seconds,
            parsed.title, parsed.artist, parsed.duration_seconds,
        )
        # Phase 5: strong cohesion bias — keep tracks anchored to a release group
        # the user already owns (was +0.05; too weak to beat alternate editions).
        if preferred_release_group and parsed.release_group_id == preferred_release_group:
            sim = min(sim + 0.10, 1.0)
        if sim >= min_sim:
            out.append((parsed, sim))

    out.sort(key=lambda x: x[1], reverse=True)
    return out


def search_recordings_free(
    query: str,
    limit: int = 10,
    cache_dir: Path | None = None,
    duration_seconds: int | None = None,
) -> list[MBRecording]:
    """Free-form MB recording search for the manual review lookup.

    Splits on ' - ' to extract 'Artist - Title' prefix; otherwise treats whole
    string as the recording title with no artist filter.

    Re-ranks results using a blend of MB's text relevance score and local
    title/artist/duration similarity so that exact matches surface above
    live versions, tribute covers, or same-title tracks by other artists.
    """
    if " - " in query:
        parts = query.split(" - ", 1)
        artist_q, title_q = parts[0].strip(), parts[1].strip()
    else:
        artist_q, title_q = "", query.strip()

    # Use limit in the cache key so "load more" fetches fresh results
    key = f"free:{limit}:{_cache_key(title_q or query, artist_q)}"
    raw: dict[str, object] | None = None
    if cache_dir is not None:
        raw = _load_cache(cache_dir, key)

    if raw is None:
        try:
            result = musicbrainzngs.search_recordings(
                recording=title_q or query,
                artistname=artist_q or None,
                limit=limit,
            )
            raw = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key, raw)
        except Exception as exc:
            logger.warning("MB free search failed for %r: %s", query, exc)
            return []

    records = [
        _parse_recording(rec)
        for rec in (raw.get("recording-list") or [])
        if isinstance(rec, dict)
    ]

    # Re-rank: blend MB relevance with local title/artist/duration similarity.
    # This surfaces exact artist+title matches above live versions, tributes,
    # and same-title tracks by other artists that MB may score similarly.
    from service.search.matcher import artist_similarity, title_similarity
    ref_title = title_q or query
    for rec in records:
        t_sim = title_similarity(ref_title, rec.title)
        a_sim = artist_similarity(artist_q, rec.artist) if artist_q else 0.5
        local_sim = t_sim * 0.6 + a_sim * 0.4
        if duration_seconds and rec.duration_seconds:
            diff = abs(duration_seconds - rec.duration_seconds)
            if diff <= 10:
                local_sim = min(local_sim + 0.1, 1.0)
            elif diff > 60:
                local_sim = min(local_sim, 0.75)
        rec.score = 0.5 * rec.score + 0.5 * local_sim

    records.sort(key=lambda r: r.score, reverse=True)
    return records[:limit]


def search_artists(
    name: str,
    limit: int = 10,
    cache_dir: Path | None = None,
) -> list[MBArtist]:
    """Search MB for artists by name. Returns ranked candidates."""
    key = f"artist_search:{_cache_key(name, '')}"

    raw: dict[str, object] | None = None
    if cache_dir is not None:
        raw = _load_cache(cache_dir, key)

    if raw is None:
        try:
            result = musicbrainzngs.search_artists(artist=name, limit=limit)
            raw = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key, raw)
        except Exception as exc:
            logger.warning("MB artist search failed for %r: %s", name, exc)
            return []

    artists: list[MBArtist] = []
    for a in raw.get("artist-list") or []:
        if not isinstance(a, dict):
            continue
        artists.append(MBArtist(
            artist_id=str(a.get("id", "")),
            name=str(a.get("name", "")),
            disambiguation=str(a.get("disambiguation") or "") or None,
            score=int(a.get("ext:score", 0)) / 100.0,
        ))
    return artists


def search_release_groups(
    artist: str,
    album: str,
    limit: int = 8,
    cache_dir: Path | None = None,
) -> list[MBReleaseGroupMatch]:
    """Search MB for release groups matching artist + album title."""
    key = f"rg_search:{_cache_key(artist + '|' + album, '')}"
    raw = _load_cache(cache_dir, key) if cache_dir else None
    if raw is None:
        try:
            result = musicbrainzngs.search_release_groups(
                releasegroup=album, artist=artist, limit=limit
            )
            raw = dict(result)
            if cache_dir:
                _save_cache(cache_dir, key, raw)
        except Exception as exc:
            logger.warning("MB release group search failed: %s", exc)
            return []

    out: list[MBReleaseGroupMatch] = []
    for rg in (raw.get("release-group-list") or []):
        if not isinstance(rg, dict):
            continue
        artist_name = ""
        for ac in (rg.get("artist-credit") or []):
            if isinstance(ac, dict) and ac.get("artist"):
                artist_name = str(ac["artist"].get("name", ""))
                break
        # year from first-release-date
        frd = str(rg.get("first-release-date") or "")
        out.append(MBReleaseGroupMatch(
            release_group_id=str(rg.get("id", "")),
            title=str(rg.get("title", "")),
            artist=artist_name,
            release_type=str(rg.get("type") or rg.get("primary-type") or ""),
            year=frd[:4] if frd else "",
            disambiguation=str(rg.get("disambiguation") or ""),
        ))
    return out


def get_artist_release_groups(
    artist_mbid: str,
    cache_dir: Path | None = None,
) -> tuple[str, list[MBReleaseGroup]]:
    """Get artist name and all release groups from MusicBrainz.

    Returns (artist_name, release_groups). Fetches all pages so prolific
    artists (>25 release groups) are fully represented.
    """
    key = f"artist_rg_v2:{artist_mbid}"

    cached: dict[str, object] | None = None
    if cache_dir is not None:
        cached = _load_cache(cache_dir, key)

    if cached is not None:
        artist_name = str(cached.get("artist_name") or "Unknown Artist")
        raw_rgs: list[dict] = cached.get("release_groups") or []  # type: ignore[assignment]
    else:
        # Fetch artist name (fast call, no includes).
        try:
            artist_result = musicbrainzngs.get_artist_by_id(artist_mbid)
            artist_name = str(artist_result.get("artist", {}).get("name") or "Unknown Artist")
        except Exception as exc:
            logger.warning("MB artist fetch failed for %s: %s", artist_mbid, exc)
            return "Unknown Artist", []

        # Paginate browse_release_groups (limit 100) to get every release group.
        raw_rgs = []
        limit = 100
        offset = 0
        while True:
            try:
                page = musicbrainzngs.browse_release_groups(
                    artist=artist_mbid, limit=limit, offset=offset
                )
            except Exception as exc:
                logger.warning("MB browse_release_groups failed for %s offset %d: %s", artist_mbid, offset, exc)
                break
            page_list: list[dict] = page.get("release-group-list") or []
            raw_rgs.extend(page_list)
            total = int(page.get("release-group-count") or 0)
            offset += len(page_list)
            if offset >= total or not page_list:
                break

        if cache_dir is not None:
            _save_cache(cache_dir, key, {"artist_name": artist_name, "release_groups": raw_rgs})

    groups: list[MBReleaseGroup] = []
    for rg in raw_rgs:
        if not isinstance(rg, dict):
            continue
        rg_type = str(rg.get("type") or rg.get("primary-type") or "Other")
        date = str(rg.get("first-release-date") or "")
        year: int | None = None
        if date and len(date) >= 4:
            try:
                year = int(date[:4])
            except ValueError:
                pass
        groups.append(MBReleaseGroup(
            release_group_id=str(rg.get("id", "")),
            title=str(rg.get("title", "")),
            year=year,
            release_type=rg_type,
        ))

    groups.sort(key=lambda g: (g.year or 0, g.title))
    return artist_name, groups


def get_recording_by_id(
    recording_id: str,
    cache_dir: Path | None = None,
) -> MBRecording | None:
    """Fetch a specific MB recording by its ID (for re-tagging)."""
    key = f"mbid:{recording_id}"

    raw: dict[str, object] | None = None
    if cache_dir is not None:
        raw = _load_cache(cache_dir, key)

    if raw is None:
        try:
            result = musicbrainzngs.get_recording_by_id(
                recording_id,
                includes=["artists", "releases", "media", "isrcs"],
            )
            raw = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key, raw)
        except Exception as exc:
            logger.warning("MB get_recording_by_id failed for %s: %s", recording_id, exc)
            return None

    rec = raw.get("recording")
    if not rec or not isinstance(rec, dict):
        return None
    return _parse_recording(rec)


def get_release_group_genres(
    release_group_id: str,
    cache_dir: Path | None = None,
    max_genres: int = 5,
) -> list[str]:
    """Return the top folksonomy genre tags for a MB release group, sorted by vote count.

    Returns an empty list on any error.
    """
    key = f"rg_tags:{release_group_id}"

    raw: dict[str, object] | None = None
    if cache_dir is not None:
        raw = _load_cache(cache_dir, key)

    if raw is None:
        try:
            result = musicbrainzngs.get_release_group_by_id(
                release_group_id,
                includes=["tags"],
            )
            raw = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key, raw)
        except Exception as exc:
            logger.warning("MB release group tags fetch failed for %s: %s", release_group_id, exc)
            return []

    rg = raw.get("release-group") or {}
    if not isinstance(rg, dict):
        return []

    tags = rg.get("tag-list") or []
    if not isinstance(tags, list):
        return []

    scored: list[tuple[int, str]] = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or "").strip()
        count = int(tag.get("count") or 0)
        if name and count > 0:
            scored.append((count, name))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in scored[:max_genres]]


# MB medium `format` substrings that denote a video side rather than audio.
_VIDEO_MEDIUM_HINTS = ("dvd", "blu-ray", "bluray", "vhs", "vcd", "umd", "video")


@dataclass
class MBTrack:
    number: int
    title: str
    duration_seconds: int | None
    recording_id: str | None
    # Disc (audio medium) this track sits on. None for single-disc releases so
    # everything downstream keeps today's behaviour; 1..N on multi-disc releases.
    disc: int | None = None


def get_release_group_tracks(
    release_group_id: str,
    cache_dir: Path | None = None,
) -> tuple[str, str | None, int | None, list[MBTrack]]:
    """Fetch the track list for the primary release of a release group.

    Returns (album_title, release_id, year, tracks). Makes 2 MB API calls:
    one to find the first official release, one to fetch its tracks.
    Both responses are cached. year is the original release year from the
    release group's first-release-date.
    """
    key_rg = f"rg_tracks:{release_group_id}"
    raw_rg: dict[str, object] | None = None
    if cache_dir is not None:
        raw_rg = _load_cache(cache_dir, key_rg)

    if raw_rg is None:
        try:
            result = musicbrainzngs.get_release_group_by_id(
                release_group_id,
                includes=["releases"],
            )
            raw_rg = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key_rg, raw_rg)
        except Exception as exc:
            logger.warning("MB release group fetch failed for %s: %s", release_group_id, exc)
            return "Unknown Album", None, None, []

    rg_data = raw_rg.get("release-group") or {}
    if not isinstance(rg_data, dict):
        return "Unknown Album", None, None, []

    album_title = str(rg_data.get("title") or "Unknown Album")

    rg_year: int | None = None
    rg_date = str(rg_data.get("first-release-date") or "")
    if rg_date and len(rg_date) >= 4:
        try:
            rg_year = int(rg_date[:4])
        except ValueError:
            pass

    releases = rg_data.get("release-list") or []
    if not isinstance(releases, list) or not releases:
        return album_title, None, rg_year, []

    # Prefer an official release; fall back to first
    release_id: str | None = None
    for rel in releases:
        if isinstance(rel, dict):
            release_id = str(rel.get("id") or "")
            if rel.get("status", "").lower() == "official":
                break

    if not release_id:
        return album_title, None, rg_year, []

    return _fetch_release_tracks(release_id, album_title, rg_year, cache_dir)


def _fetch_release_tracks(
    release_id: str,
    album_title: str = "Unknown Album",
    rg_year: int | None = None,
    cache_dir: Path | None = None,
) -> tuple[str, str, int | None, list[MBTrack]]:
    """Fetch track list for a known MB release ID. Returns (album_title, release_id, year, tracks)."""
    key_rel = f"release_tracks:{release_id}"
    raw_rel: dict[str, object] | None = None
    if cache_dir is not None:
        raw_rel = _load_cache(cache_dir, key_rel)

    if raw_rel is None:
        try:
            result = musicbrainzngs.get_release_by_id(
                release_id,
                includes=["recordings", "media"],
            )
            raw_rel = dict(result)
            if cache_dir is not None:
                _save_cache(cache_dir, key_rel, raw_rel)
        except Exception as exc:
            logger.warning("MB release fetch failed for %s: %s", release_id, exc)
            return album_title, release_id, rg_year, []

    tracks: list[MBTrack] = []
    rel_data = raw_rel.get("release") or {}
    if not isinstance(rel_data, dict):
        return album_title, release_id, rg_year, []

    album_title = str(rel_data.get("title") or album_title)

    if rg_year is None:
        rel_date = str(rel_data.get("date") or "")
        if rel_date and len(rel_date) >= 4:
            try:
                rg_year = int(rel_date[:4])
            except ValueError:
                pass

    seen_rids: set[str] = set()
    disc_idx = 0
    for medium in rel_data.get("medium-list") or []:
        if not isinstance(medium, dict):
            continue
        # Skip video sides (DualDisc DVD-Video, bonus DVDs/Blu-rays, …). They list
        # the same songs as the audio disc, which otherwise doubles the tracklist
        # (e.g. The Offspring "Greatest Hits" DualDisc), and they aren't audio
        # tracks the user acquires anyway.
        fmt = str(medium.get("format") or "").lower()
        if any(h in fmt for h in _VIDEO_MEDIUM_HINTS):
            continue
        disc_idx += 1
        for t in medium.get("track-list") or []:
            if not isinstance(t, dict):
                continue
            rec = t.get("recording") or {}
            title = str(t.get("title") or (rec.get("title") if isinstance(rec, dict) else None) or "Unknown")
            pos = t.get("position") or t.get("number")
            try:
                number = int(pos) if pos else 0
            except (ValueError, TypeError):
                number = 0
            duration_ms = t.get("length") or (rec.get("length") if isinstance(rec, dict) else None)
            duration_s: int | None = int(int(duration_ms) / 1000) if duration_ms else None
            rid = str(rec.get("id") or "") or None if isinstance(rec, dict) else None
            # Dedupe by recording ID — guards against the same recording appearing
            # on more than one medium of the release.
            if rid:
                if rid in seen_rids:
                    continue
                seen_rids.add(rid)
            tracks.append(MBTrack(number=number, title=title, duration_seconds=duration_s, recording_id=rid, disc=disc_idx))

    # Positions restart at 1 on every medium, so a flat sort by number interleaves
    # discs (two "track 1"s, two "track 2"s…). Sort disc-major, and only keep disc
    # numbers when the release actually has more than one audio disc — single-disc
    # albums stay exactly as before (no DISCNUMBER tag, no "NN" prefix change).
    if disc_idx <= 1:
        for t in tracks:
            t.disc = None
    tracks.sort(key=lambda t: (t.disc or 1, t.number))
    return album_title, release_id, rg_year, tracks


def get_release_tracks_by_id(
    release_id: str,
    cache_dir: Path | None = None,
) -> tuple[str, str, int | None, list[MBTrack]]:
    """Fetch tracks for a known MB release ID (skips the release-group lookup).

    Use when you have musicbrainz_release_id (a specific pressing) rather than
    mb_release_group_id (the abstract release group). Cached.
    """
    return _fetch_release_tracks(release_id, cache_dir=cache_dir)
