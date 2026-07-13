"""Web-layer helpers for enqueuing arq jobs on a throw-away Redis pool.

Every acquire route used to open its own pool with ten lines of boilerplate;
these helpers keep the pool lifecycle and the acquire_track kwarg envelope in
one place. Error handling stays at the call site — some routes answer a dead
queue with HTTP 503, others with an inline error badge.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from service.config import settings


@asynccontextmanager
async def arq_pool() -> AsyncIterator:
    """Yield a fresh arq Redis pool and close it afterwards."""
    from arq import create_pool
    from arq.connections import RedisSettings

    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    try:
        yield redis
    finally:
        await redis.aclose()


async def enqueue_acquire_track(
    redis,
    job_id: str,
    *,
    provider_name: str,
    provider_ref: str,
    candidate_json: str,
    unique_retry: bool = False,
) -> None:
    """Enqueue one acquire_track job.

    `unique_retry=True` appends a random suffix to the arq job ID so arq's NX
    dedup doesn't block a retry while the prior attempt's key lingers in Redis.
    """
    arq_job_id = f"acquire:{job_id}"
    if unique_retry:
        arq_job_id += f":{uuid.uuid4().hex[:8]}"
    await redis.enqueue_job(
        "acquire_track",
        job_id=job_id,
        provider_name=provider_name,
        provider_ref=provider_ref,
        candidate_json=candidate_json,
        music_dir=str(settings.music_dir),
        tmp_acquire_dir=str(settings.tmp_acquire_dir),
        _job_id=arq_job_id,
    )


async def enqueue_album_from_mb(
    redis,
    album_job_id: str,
    *,
    release_group_id: str,
    artist_name: str,
    job_key_prefix: str = "album",
) -> None:
    """Enqueue one acquire_album_from_mb job (album batch coordinator)."""
    await redis.enqueue_job(
        "acquire_album_from_mb",
        album_job_id=album_job_id,
        release_group_id=release_group_id,
        artist_name=artist_name,
        music_dir=str(settings.music_dir),
        tmp_acquire_dir=str(settings.tmp_acquire_dir),
        _job_id=f"{job_key_prefix}:{album_job_id}",
    )
