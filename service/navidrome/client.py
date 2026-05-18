"""Navidrome HTTP client.

Interactions are limited to: triggering a library scan.
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
