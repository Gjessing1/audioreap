"""Lyrics fetcher — LRCLIB (https://lrclib.net).

Free, open, no API key. Returns synced (LRC, timestamped) lyrics when available,
falling back to plain text. Lyrics are written as a ``.lrc`` sidecar next to the
audio file (same basename), which Navidrome reads natively and serves over the
Subsonic / OpenSubsonic ``getLyricsBySongId`` API — so every client benefits,
not just one.

Sidecars sit alongside the audio like ``cover.jpg``; audio file tags are never
modified by this module.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_GET_URL = "https://lrclib.net/api/get"
_SEARCH_URL = "https://lrclib.net/api/search"
# LRCLIB asks clients to identify themselves with a descriptive User-Agent.
_USER_AGENT = "audioreap (https://github.com/Gjessing1/audioreap)"

# Backoff for transient failures (rate limits / server errors / network blips).
_MAX_RETRIES = 3            # attempts after the first try
_BACKOFF_BASE = 1.0         # seconds; doubles each retry (1s, 2s, 4s)
_BACKOFF_CAP = 30.0         # never wait longer than this between attempts


class _TransientError(Exception):
    """A retryable LRCLIB failure — must NOT be cached as a definitive miss."""


@dataclass
class LyricsResult:
    synced: str | None       # LRC with [mm:ss.xx] timestamps
    plain: str | None        # plain text fallback
    instrumental: bool = False

    @property
    def best(self) -> str | None:
        """The text to write to a sidecar — prefer synced over plain."""
        return self.synced or self.plain


def lrc_sidecar_path(audio_path: Path) -> Path:
    """Return the ``.lrc`` sidecar path for an audio file (same basename)."""
    return audio_path.with_suffix(".lrc")


def has_lyrics_sidecar(audio_path: Path) -> bool:
    """True if a non-empty .lrc sidecar already exists for this file."""
    p = lrc_sidecar_path(audio_path)
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


# A synced LRC line begins with a timestamp like ``[01:23.45]`` — a digit right
# after the bracket distinguishes it from metadata tags such as ``[ar:...]``.
_TIMESTAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}")


def sidecar_is_synced(audio_path: Path) -> bool:
    """True if the .lrc sidecar exists and contains synced (timestamped) lyrics.

    Plain-text sidecars (and missing ones) return False — used to find tracks
    that could be upgraded to a synced version if LRCLIB now has one.
    """
    p = lrc_sidecar_path(audio_path)
    try:
        if not (p.exists() and p.stat().st_size > 0):
            return False
        for line in p.read_text(encoding="utf-8").splitlines():
            if _TIMESTAMP_RE.match(line.strip()):
                return True
        return False
    except OSError:
        return False


def _retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Honour a ``Retry-After`` header if present, else exponential backoff."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(float(ra), _BACKOFF_CAP)
        except ValueError:
            pass  # HTTP-date form — fall through to exponential backoff
    return min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> object | None:
    """GET with backoff on transient failures.

    Returns the parsed JSON on 200, ``None`` on a definitive negative (404 / other
    4xx), and raises ``_TransientError`` when every retry is exhausted on a
    retryable condition (429, 5xx, timeout, connection error). The caller uses the
    exception to avoid caching a false miss.
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = _TransientError(f"HTTP {resp.status_code}")
                delay = _retry_after_seconds(resp, attempt)
            else:
                return None  # 404 and other 4xx — a real "not found"
        if attempt < _MAX_RETRIES:
            logger.debug("LRCLIB transient (%s); retry %d in %.1fs", last_exc, attempt + 1, delay)
            await asyncio.sleep(delay)
    raise _TransientError(str(last_exc) if last_exc else "exhausted retries")


def _parse(payload: dict) -> LyricsResult | None:
    """Map an LRCLIB record to a LyricsResult, or None if it carries nothing."""
    if not isinstance(payload, dict):
        return None
    if payload.get("instrumental"):
        return LyricsResult(synced=None, plain=None, instrumental=True)
    synced = (payload.get("syncedLyrics") or "").strip() or None
    plain = (payload.get("plainLyrics") or "").strip() or None
    if not synced and not plain:
        return None
    return LyricsResult(synced=synced, plain=plain)


async def fetch_lyrics(
    *,
    artist: str | None,
    title: str | None,
    album: str | None = None,
    duration_seconds: int | None = None,
    cache_dir: Path | None = None,
) -> LyricsResult | None:
    """Fetch lyrics from LRCLIB. Returns None when nothing usable is found.

    Tries the precise ``/api/get`` endpoint first (artist+title+album+duration,
    which LRCLIB matches against its database with a small duration tolerance),
    then falls back to ``/api/search`` and takes the first hit with lyrics.
    """
    if not title or not artist:
        return None

    # Disk cache (keyed on the query, not a recording ID — lyrics have no MBID)
    cache_path: Path | None = None
    if cache_dir is not None:
        import hashlib
        key = hashlib.sha1(
            f"{artist}\x00{title}\x00{album or ''}\x00{duration_seconds or ''}".encode()
        ).hexdigest()
        cache_path = cache_dir / "lyrics" / f"{key}.lrc"
        if cache_path.exists():
            try:
                text = cache_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text == "\x00MISS":
                return None
            if text:
                # Heuristic: synced lyrics contain a leading timestamp bracket.
                synced = text if "[" in text.split("\n", 1)[0] else None
                return LyricsResult(synced=synced, plain=None if synced else text)

    headers = {"User-Agent": _USER_AGENT}
    result: LyricsResult | None = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
            params: dict[str, str] = {"artist_name": artist, "track_name": title}
            if album:
                params["album_name"] = album
            if duration_seconds:
                params["duration"] = str(int(duration_seconds))
            payload = await _get_json(client, _GET_URL, params)
            if isinstance(payload, dict):
                result = _parse(payload)

            if result is None:
                # Looser search — first result that actually carries lyrics.
                sp = {"track_name": title, "artist_name": artist}
                hits = await _get_json(client, _SEARCH_URL, sp)
                if isinstance(hits, list):
                    for rec in hits:
                        result = _parse(rec)
                        if result is not None:
                            break
    except _TransientError as exc:
        # Rate-limited / server-side / network failure after retries — return None
        # WITHOUT caching, so this track is retried on the next backfill run rather
        # than being poisoned as a permanent miss.
        logger.debug("LRCLIB transient failure for %r — %r: %s", artist, title, exc)
        return None
    except Exception as exc:
        logger.debug("LRCLIB fetch failed for %r — %r: %s", artist, title, exc)
        return None

    # Cache the outcome (including a miss marker so we don't re-hit LRCLIB).
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if result is not None and result.best:
                cache_path.write_text(result.best, encoding="utf-8")
            else:
                cache_path.write_text("\x00MISS", encoding="utf-8")
        except OSError:
            pass

    if result is not None and result.instrumental:
        return result  # caller decides; nothing to write but it's a real answer
    return result


# Any [mm:ss] / [mm:ss.xx] / [mm:ss.xxx] timestamp, incl. mid-line (enhanced LRC).
# The \d after '[' keeps metadata tags like [ar:...] / [offset:...] untouched.
_SHIFT_TS_RE = re.compile(r"\[(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\]")


def shift_lrc(text: str, offset_seconds: float) -> str:
    """Shift every LRC timestamp by offset_seconds (positive = lyrics later).

    Preserves each timestamp's fractional precision ([01:23.45] stays 2-digit,
    [01:23] stays whole-second) and clamps at 00:00 — a shift can never produce
    a negative time. Metadata tags ([ar:], [ti:], [offset:], …) are untouched.
    """
    def _repl(m: re.Match[str]) -> str:
        sec_str = m.group(2)
        digits = len(sec_str.split(".")[1]) if "." in sec_str else 0
        total = max(0.0, int(m.group(1)) * 60 + float(sec_str) + offset_seconds)
        total = round(total, digits)
        mm, ss = int(total // 60), total - int(total // 60) * 60
        if digits:
            return f"[{mm:02d}:{ss:0{3 + digits}.{digits}f}]"
        return f"[{mm:02d}:{int(ss):02d}]"

    return _SHIFT_TS_RE.sub(_repl, text)


def write_lrc_sidecar(audio_path: Path, text: str) -> bool:
    """Write LRC/plain lyrics to the .lrc sidecar next to audio_path.

    Returns True on success. Best-effort: never raises.
    """
    try:
        dest = lrc_sidecar_path(audio_path)
        dest.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        logger.debug("Lyrics sidecar write failed for %s: %s", audio_path, exc)
        return False
