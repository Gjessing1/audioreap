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

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0
_GET_URL = "https://lrclib.net/api/get"
_SEARCH_URL = "https://lrclib.net/api/search"
# LRCLIB asks clients to identify themselves with a descriptive User-Agent.
_USER_AGENT = "audioreap (https://github.com/Gjessing1/audioreap)"


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
            resp = await client.get(_GET_URL, params=params)
            if resp.status_code == 200:
                result = _parse(resp.json())

            if result is None:
                # Looser search — first result that actually carries lyrics.
                sp = {"track_name": title, "artist_name": artist}
                sresp = await client.get(_SEARCH_URL, params=sp)
                if sresp.status_code == 200:
                    for rec in sresp.json() or []:
                        result = _parse(rec)
                        if result is not None:
                            break
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
