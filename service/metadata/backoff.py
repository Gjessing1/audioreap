"""Shared retry/backoff for external metadata services.

Pattern extracted from lyrics.py (the module that got it right first):
exponential backoff with a cap, ``Retry-After`` honoured when a server sends
one, and a hard transient/permanent distinction — a :class:`TransientError`
means "the service was unreachable, not that the answer is no", so callers
must never cache the outcome as a definitive miss.

Used by lyrics.py and artwork.py (httpx, async) and musicbrainz.py
(musicbrainzngs, sync — always called via ``asyncio.to_thread``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TypeVar

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3      # attempts after the first try
BACKOFF_BASE = 1.0   # seconds; doubles each retry (1s, 2s, 4s)
BACKOFF_CAP = 30.0   # never wait longer than this between attempts


class TransientError(Exception):
    """A retryable failure — must NOT be cached as a definitive miss."""


def backoff_delay(attempt: int) -> float:
    return min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)


def retry_after_seconds(resp: httpx.Response, attempt: int) -> float:
    """Honour a ``Retry-After`` header if present, else exponential backoff."""
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return min(float(ra), BACKOFF_CAP)
        except ValueError:
            pass  # HTTP-date form — fall through to exponential backoff
    return backoff_delay(attempt)


async def get_with_backoff(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    retries: int = MAX_RETRIES,
    label: str = "",
) -> httpx.Response | None:
    """GET with backoff on transient failures.

    Returns the response on 200, ``None`` on a definitive negative (404 / other
    4xx), and raises :class:`TransientError` when every retry is exhausted on a
    retryable condition (429, 5xx, timeout, connection error). The caller uses
    the exception to avoid caching a false miss.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            delay = backoff_delay(attempt)
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = TransientError(f"HTTP {resp.status_code}")
                delay = retry_after_seconds(resp, attempt)
            else:
                return None  # 404 and other 4xx — a real "not found"
        if attempt < retries:
            logger.debug(
                "%s transient (%s); retry %d in %.1fs",
                label or url, last_exc, attempt + 1, delay,
            )
            await asyncio.sleep(delay)
    raise TransientError(str(last_exc) if last_exc else "exhausted retries")


T = TypeVar("T")


def call_with_backoff(
    fn: Callable[[], T],
    *,
    is_transient: Callable[[Exception], bool],
    retries: int = MAX_RETRIES,
    label: str = "",
) -> T:
    """Sync variant for non-httpx clients (musicbrainzngs).

    Retries ``fn`` while ``is_transient(exc)`` is true; any other exception —
    and the last transient one once retries are exhausted — propagates to the
    caller unchanged. Blocking (``time.sleep``) — call from a worker thread
    (``asyncio.to_thread``), never on the event loop.
    """
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt >= retries or not is_transient(exc):
                raise
            delay = backoff_delay(attempt)
            logger.debug(
                "%s transient (%s); retry %d in %.1fs", label, exc, attempt + 1, delay
            )
            time.sleep(delay)
    raise AssertionError("unreachable")
