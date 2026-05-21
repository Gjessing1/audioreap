"""Artwork fetcher.

Priority: Cover Art Archive (MB release art) → provider thumbnail URL → nothing.
Returns raw JPEG/PNG bytes or None.
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
_CAA_FRONT = "https://coverartarchive.org/release/{release_mbid}/front-500"
_MIN_COVER_PX = 300       # used for automatic CAA fetches
_MIN_USER_COVER_PX = 75  # minimum for user-supplied art (upload / search pick)


def _image_too_small(data: bytes, min_px: int = _MIN_COVER_PX) -> bool:
    """Return True if the image dimensions are below min_px."""
    try:
        if data[:8] == b'\x89PNG\r\n\x1a\n':  # PNG
            w = struct.unpack('>I', data[16:20])[0]
            h = struct.unpack('>I', data[20:24])[0]
            return w < min_px or h < min_px
        if data[:2] == b'\xff\xd8':  # JPEG — scan for SOF marker
            i = 2
            while i + 4 < len(data):
                if data[i] != 0xff:
                    break
                marker = data[i + 1]
                seg_len = struct.unpack('>H', data[i + 2:i + 4])[0]
                if marker in (0xc0, 0xc1, 0xc2, 0xc3):
                    h = struct.unpack('>H', data[i + 5:i + 7])[0]
                    w = struct.unpack('>H', data[i + 7:i + 9])[0]
                    return w < min_px or h < min_px
                i += 2 + seg_len
    except Exception:
        pass
    return False  # Unknown format or parse error — don't reject


async def fetch_from_caa(release_mbid: str) -> bytes | None:
    """Fetch front cover from MusicBrainz Cover Art Archive."""
    url = _CAA_FRONT.format(release_mbid=release_mbid)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return resp.content
            logger.debug("CAA returned %d for %s", resp.status_code, release_mbid)
    except Exception as exc:
        logger.debug("CAA fetch failed for %s: %s", release_mbid, exc)
    return None


async def fetch_from_url(url: str) -> bytes | None:
    """Fetch artwork from an arbitrary URL (provider thumbnail etc.)."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                if "image" in content_type:
                    return resp.content
    except Exception as exc:
        logger.debug("Artwork URL fetch failed for %s: %s", url, exc)
    return None


async def fetch_artwork(
    release_mbid: str | None = None,
    thumbnail_url: str | None = None,
    cache_dir: Path | None = None,
) -> bytes | None:
    """Fetch artwork with fallback chain. Returns bytes or None."""
    # Check disk cache first
    if cache_dir is not None:
        cache_key = release_mbid or (
            __import__("hashlib").sha1((thumbnail_url or "").encode()).hexdigest()
        )
        cache_path = cache_dir / "artwork" / f"{cache_key}.jpg"
        if cache_path.exists():
            return cache_path.read_bytes()

    art: bytes | None = None

    if release_mbid:
        art = await fetch_from_caa(release_mbid)
        if art and _image_too_small(art):
            logger.debug("CAA cover for %s is below %dpx — discarding", release_mbid, _MIN_COVER_PX)
            art = None

    if art is None and thumbnail_url:
        art = await fetch_from_url(thumbnail_url)

    # Write to cache
    if art and cache_dir is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(art)

    return art
