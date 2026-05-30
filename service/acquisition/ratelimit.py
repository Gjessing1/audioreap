"""Adaptive, cluster-wide rate gate for yt-dlp / YouTube access.

The goal is a *slow, steady stream* of downloads that stays under YouTube's rate
limit rather than bursting all queued jobs at once and tripping HTTP 429. State
lives in Redis so it is shared across the worker's concurrent slots (and any
future extra workers):

  ar:rl:next      — epoch seconds; the earliest the next download may start
  ar:rl:interval  — current spacing between download starts (adaptive)
  ar:rl:cooldown  — epoch seconds; a hard "everyone pause until" set after a 429

Reservation is a single atomic Lua step so two slots can't claim the same slot:
each caller reads ``next``, clamps it to ``max(now, cooldown)``, and writes back
``start + interval`` for the next caller. The caller then waits until ``start``.

Adaptation (AIMD-style, converges near the acceptable rate without manual tuning):
  • penalize() — on a 429: multiply the interval (capped) and set a cooldown.
  • reward()   — on a clean success: ease the interval back toward the minimum.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_K_NEXT = "ar:rl:next"
_K_INTERVAL = "ar:rl:interval"
_K_COOLDOWN = "ar:rl:cooldown"

# Multiplicative back-off on a 429, multiplicative ease-off on success.
_BACKOFF_FACTOR = 1.5
_RECOVER_FACTOR = 0.9
# Keys self-expire so a long idle period resets the gate to defaults.
_TTL = 3600

# Atomically reserve the next download slot. Returns {start_epoch, cooldown_flag}.
_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local base = tonumber(ARGV[2])
local interval = tonumber(redis.call('GET', KEYS[2]))
if not interval then interval = base end
local cooldown = tonumber(redis.call('GET', KEYS[3]))
if not cooldown then cooldown = 0 end
local nxt = tonumber(redis.call('GET', KEYS[1]))
if not nxt then nxt = 0 end
local start = now
if nxt > start then start = nxt end
if cooldown > start then start = cooldown end
-- Flag as cooldown whenever we're still inside the 429 penalty window, so the
-- UI shows "paused after a rate-limit" rather than ordinary pacing.
local cd_flag = 0
if cooldown > now then cd_flag = 1 end
redis.call('SET', KEYS[1], start + interval, 'EX', ARGV[3])
return {tostring(start), tostring(cd_flag)}
"""

# Grow the interval and arm a cooldown after a 429.
_PENALIZE_LUA = """
local now = tonumber(ARGV[1])
local factor = tonumber(ARGV[2])
local base = tonumber(ARGV[3])
local maxi = tonumber(ARGV[4])
local cd = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])
local interval = tonumber(redis.call('GET', KEYS[2]))
if not interval then interval = base end
interval = interval * factor
if interval > maxi then interval = maxi end
redis.call('SET', KEYS[2], interval, 'EX', ttl)
local until_ = now + cd
redis.call('SET', KEYS[3], until_, 'EX', math.ceil(cd) + 10)
local nxt = tonumber(redis.call('GET', KEYS[1]))
if not nxt then nxt = 0 end
if until_ > nxt then redis.call('SET', KEYS[1], until_, 'EX', ttl) end
return tostring(interval)
"""

# Ease the interval back toward the floor after a clean success.
_RECOVER_LUA = """
local base = tonumber(ARGV[1])
local factor = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local interval = tonumber(redis.call('GET', KEYS[1]))
if not interval then return tostring(base) end
interval = interval * factor
if interval < base then interval = base end
redis.call('SET', KEYS[1], interval, 'EX', ttl)
return tostring(interval)
"""


class YtdlpRateGate:
    """Cluster-wide pacing gate backed by Redis.

    ``redis`` is any redis.asyncio-compatible client (arq's ``ArqRedis`` works).
    """

    def __init__(self, redis: object, settings: object) -> None:
        self._redis = redis
        self._base = float(getattr(settings, "ytdlp_min_download_interval_seconds", 5.0))
        self._max = float(getattr(settings, "ytdlp_max_download_interval_seconds", 45.0))
        self._cooldown = float(getattr(settings, "ytdlp_rate_cooldown_seconds", 120.0))

    async def reserve(self) -> tuple[float, bool]:
        """Claim the next slot. Returns (start_epoch, is_cooldown).

        The caller should wait until ``start_epoch`` before downloading. Falls back
        to "start now" if Redis is unavailable so a broker hiccup never wedges jobs.
        """
        try:
            res = await self._redis.eval(
                _RESERVE_LUA, 3, _K_NEXT, _K_INTERVAL, _K_COOLDOWN,
                str(time.time()), str(self._base), str(_TTL),
            )
            start = float(_decode(res[0]))
            cd_flag = _decode(res[1]) == "1"
            return start, cd_flag
        except Exception as exc:
            logger.debug("Rate gate reserve failed (proceeding now): %s", exc)
            return time.time(), False

    async def penalize(self) -> None:
        """Back off after a 429: grow the interval and arm a cooldown for all jobs."""
        try:
            new_interval = await self._redis.eval(
                _PENALIZE_LUA, 3, _K_NEXT, _K_INTERVAL, _K_COOLDOWN,
                str(time.time()), str(_BACKOFF_FACTOR), str(self._base),
                str(self._max), str(self._cooldown), str(_TTL),
            )
            logger.warning(
                "yt-dlp 429 — backing off: interval now %ss, %ss cooldown",
                _decode(new_interval), int(self._cooldown),
            )
        except Exception as exc:
            logger.debug("Rate gate penalize failed: %s", exc)

    async def reward(self) -> None:
        """Ease the interval back toward the minimum after a clean download."""
        try:
            await self._redis.eval(
                _RECOVER_LUA, 1, _K_INTERVAL,
                str(self._base), str(_RECOVER_FACTOR), str(_TTL),
            )
        except Exception as exc:
            logger.debug("Rate gate reward failed: %s", exc)


def _decode(v: object) -> str:
    """Redis Lua returns bytes (or str under decode_responses); normalise to str."""
    if isinstance(v, bytes):
        return v.decode()
    return str(v)
