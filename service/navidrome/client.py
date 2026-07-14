"""Navidrome HTTP client.

Interactions are limited to: triggering a library scan, and reading album
play-activity lists (most played / recently played). Strictly read-only apart
from the scan trigger — playback state is never written.
Uses the Subsonic-compatible API that Navidrome exposes.
"""
import logging

import httpx

from service.config import settings

logger = logging.getLogger(__name__)

_SUBSONIC_VERSION = "1.16.1"
_CLIENT_NAME = "audioreap"


def _auth_params() -> dict[str, str]:
    return {
        "u": settings.navidrome_user,
        "p": settings.navidrome_password,
        "v": _SUBSONIC_VERSION,
        "c": _CLIENT_NAME,
        "f": "json",
    }


async def get_album_list(list_type: str, size: int = 20) -> list[dict]:
    """Read an album activity list from Navidrome (Subsonic getAlbumList2).

    ``list_type`` is a Subsonic list type — "frequent" (most played) or
    "recent" (recently played) are the ones audioreap uses. Returns the raw
    album dicts (name, artist, playCount, …); an unreachable or unconfigured
    Navidrome yields an empty list so callers never break the page over it.
    """
    url = f"{settings.navidrome_url}/rest/getAlbumList2.view"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url, params={**_auth_params(), "type": list_type, "size": size}
            )
            resp.raise_for_status()
            body = resp.json().get("subsonic-response", {})
            if body.get("status") != "ok":
                logger.warning("Navidrome getAlbumList2(%s) error: %s", list_type, body.get("error"))
                return []
            return body.get("albumList2", {}).get("album", []) or []
    except Exception as exc:
        logger.warning("Navidrome getAlbumList2(%s) failed: %s", list_type, exc)
        return []


async def trigger_scan() -> None:
    """Ask Navidrome to start a library rescan (non-blocking on Navidrome's side)."""
    url = f"{settings.navidrome_url}/rest/startScan.view"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=_auth_params())
            resp.raise_for_status()
        logger.info("Navidrome scan triggered")
    except Exception as exc:
        logger.warning("Could not trigger Navidrome scan: %s", exc)
        raise
