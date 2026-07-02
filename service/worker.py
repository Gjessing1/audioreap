import logging
import shutil
from datetime import datetime, timedelta

from arq.connections import RedisSettings
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from service.config import settings
from service.db.schema import AcquisitionJobRow

logger = logging.getLogger(__name__)

_STUCK_STATES = frozenset({"queued", "downloading", "processing", "tagging", "importing"})
_STUCK_CUTOFF_MINUTES = 15
# A "waiting" job is parked in the rate gate (_await_rate_slot), which rewrites its
# countdown to the DB every ~3s while a worker is actually sleeping on it. So a
# "waiting" row whose updated_at has gone stale is an orphan: the worker that owned
# it restarted/died mid-park (e.g. a deploy), leaving a frozen "starting in ~5s"
# card that nothing else clears. Recover these on a much shorter clock than the
# generic stuck states — there is no legitimate reason for a live wait to stop
# heartbeating for minutes, even across the 120s back-off cooldown.
_WAITING_CUTOFF_MINUTES = 3


async def _recover_stuck_jobs(
    session_factory: async_sessionmaker,  # type: ignore[type-arg]
    redis: object | None = None,
) -> None:
    """Reset jobs whose worker died mid-flight, and re-queue them automatically.

    A stuck job has no live coroutine left to run acquire_track's tail-of-job
    retry logic (a deploy, worker crash, or arq timeout killed it), so merely
    marking it failed/transient left it sitting until a manual retry click.
    While retry budget remains, put it back on the queue with the usual backoff;
    only budget-exhausted jobs stay failed for manual attention.
    """
    import uuid as _uuid

    from service.acquisition.jobs import _MAX_RETRIES, _RETRY_DELAYS

    now = datetime.utcnow()
    stuck_cutoff = now - timedelta(minutes=_STUCK_CUTOFF_MINUTES)
    waiting_cutoff = now - timedelta(minutes=_WAITING_CUTOFF_MINUTES)
    # (job_id, provider, provider_ref, candidate_json, delay_seconds)
    requeue: list[tuple[str, str, str, str, int]] = []
    async with session_factory() as session, session.begin():
        rows = (await session.execute(
            select(AcquisitionJobRow).where(
                or_(
                    and_(
                        AcquisitionJobRow.state.in_(list(_STUCK_STATES)),
                        AcquisitionJobRow.updated_at < stuck_cutoff,
                    ),
                    and_(
                        AcquisitionJobRow.state == "waiting",
                        AcquisitionJobRow.updated_at < waiting_cutoff,
                    ),
                )
            )
        )).scalars().all()
        for row in rows:
            was_state = row.state
            cutoff_min = _WAITING_CUTOFF_MINUTES if was_state == "waiting" else _STUCK_CUTOFF_MINUTES
            logger.warning("Resetting stuck job %s (was %s, idle since %s)", row.id, was_state, row.updated_at)
            row.failure_class = "transient"
            row.updated_at = now
            if row.candidate_json and row.provider_ref and row.retry_count < _MAX_RETRIES:
                delay = _RETRY_DELAYS[row.retry_count]
                row.retry_count += 1
                row.state = "queued"
                row.error = (
                    f"Stuck in '{was_state}' for >{cutoff_min}m — auto-requeued "
                    f"(retry {row.retry_count}/{_MAX_RETRIES} in {delay}s)"
                )
                requeue.append(
                    (row.id, row.provider, row.provider_ref, row.candidate_json, delay)
                )
            else:
                row.state = "failed"
                row.error = f"Stuck in '{was_state}' for >{cutoff_min}m — reset by worker recovery"

    if not requeue:
        return

    # If the enqueue below fails, the row is left "queued" with no arq job — the
    # next recovery pass sees it stuck in "queued" and tries again.
    own_pool = redis is None
    if own_pool:
        from arq import create_pool
        try:
            redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        except Exception as exc:
            logger.warning("Stuck-job requeue skipped — Redis unavailable: %s", exc)
            return
    try:
        for jid, provider_name, provider_ref, candidate_json, delay in requeue:
            try:
                await redis.enqueue_job(  # type: ignore[attr-defined]
                    "acquire_track",
                    job_id=jid,
                    provider_name=provider_name,
                    provider_ref=provider_ref,
                    candidate_json=candidate_json,
                    music_dir=str(settings.music_dir),
                    tmp_acquire_dir=str(settings.tmp_acquire_dir),
                    # Unique arq ID per requeue so arq's NX dedup doesn't drop it
                    _job_id=f"acquire:{jid}:rec{_uuid.uuid4().hex[:8]}",
                    _defer_by=timedelta(seconds=delay),
                )
                logger.info("Re-queued stuck job %s (retry in %ds)", jid, delay)
            except Exception as exc:
                logger.warning("Could not re-queue stuck job %s: %s", jid, exc)
    finally:
        if own_pool:
            await redis.aclose()  # type: ignore[attr-defined]


def _cleanup_tmp(tmp_dir: str) -> None:
    import os
    from pathlib import Path
    cutoff = datetime.utcnow() - timedelta(hours=1)
    tmp = Path(tmp_dir)
    if not tmp.exists():
        return
    for entry in tmp.iterdir():
        try:
            mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
            if mtime < cutoff:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    os.unlink(entry)
                logger.info("Cleaned up stale tmp entry: %s", entry)
        except Exception as exc:
            logger.debug("tmp cleanup error for %s: %s", entry, exc)


async def startup(ctx: dict[str, object]) -> None:
    import yt_dlp

    import service.providers.ytdlp  # noqa: F401 — registers YtdlpProvider
    from service.config import load_config_overrides
    from service.providers import all_providers

    # UI-saved settings (/admin/config → /data/config_overrides.json). The API
    # process loads these at startup; without this the worker would run on env
    # defaults forever and Runtime Config changes would never reach downloads.
    load_config_overrides()

    logger.info("yt-dlp version: %s", yt_dlp.version.__version__)

    # Reuse the engine configured in service.db.session — it installs WAL +
    # PRAGMA busy_timeout=30000 via a connect listener and connect_args timeout.
    # A bare create_async_engine(settings.db_url) here would fall back to SQLite's
    # ~5s default busy timeout, so the every-3s rate-gate countdown write in
    # _await_rate_slot loses the write lock under contention and raises
    # "database is locked", failing the whole download job before it ever runs.
    from service.db.session import AsyncSessionLocal, engine

    ctx["engine"] = engine
    session_factory = AsyncSessionLocal
    ctx["session_factory"] = session_factory
    ctx["providers"] = {cls.name: cls() for cls in all_providers()}
    ctx["music_dir"] = str(settings.music_dir)
    ctx["tmp_acquire_dir"] = str(settings.tmp_acquire_dir)

    await _recover_stuck_jobs(session_factory, ctx.get("redis"))
    _cleanup_tmp(str(settings.tmp_acquire_dir))

    providers: dict[str, object] = ctx["providers"]  # type: ignore[assignment]
    logger.info("audioreap worker ready — providers: %s", list(providers))


async def shutdown(ctx: dict[str, object]) -> None:
    import asyncio

    from service.acquisition.jobs import _bg_tasks
    from service.acquisition.pipeline import _bg_tasks as _pipeline_bg_tasks
    pending = list(_bg_tasks) + list(_pipeline_bg_tasks)
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("Cancelled %d background task(s) on shutdown", len(pending))

    engine = ctx.get("engine")
    if isinstance(engine, AsyncEngine):
        await engine.dispose()
    logger.info("audioreap worker stopped")


async def worker_heartbeat(ctx: dict[str, object]) -> None:
    """Write a heartbeat timestamp to Redis so /health can detect a dead worker.

    Also periodically recovers stuck jobs so they can be retried without requiring
    a worker restart.
    """
    import redis.asyncio as aioredis

    from service.config import load_config_overrides

    # Re-apply UI config overrides every minute so Runtime Config changes reach
    # the worker without a restart (worker_concurrency is the one exception —
    # arq reads max_jobs once at startup).
    try:
        load_config_overrides()
    except Exception as exc:
        logger.debug("config override reload failed: %s", exc)

    ts = datetime.utcnow().isoformat()
    try:
        rc = aioredis.from_url(settings.redis_url)
        await rc.set("audioreap:worker:heartbeat", ts, ex=300)
        await rc.aclose()
    except Exception as exc:
        logger.warning("heartbeat write failed: %s", exc)

    # Recover stuck jobs every heartbeat (every minute) — catches hangs that
    # don't raise, so the retry logic can re-queue them with backoff.
    session_factory: async_sessionmaker = ctx.get("session_factory")  # type: ignore[assignment]
    if session_factory:
        try:
            await _recover_stuck_jobs(session_factory, ctx.get("redis"))
        except Exception as exc:
            logger.warning("periodic stuck-job recovery failed: %s", exc)

        # Advance album jobs whose child tracks have all finished to a terminal
        # state (done/partial/failed) so they stop showing as "running" forever.
        try:
            from service.acquisition.jobs import reconcile_album_jobs
            await reconcile_album_jobs(ctx)
        except Exception as exc:
            logger.warning("periodic album reconcile failed: %s", exc)


async def auto_rescan(ctx: dict[str, object]) -> None:
    """Periodic job: trigger a library rescan at the configured interval.

    Skipped when rescan_interval_minutes is 0 (disabled) or when the last rescan
    was more recent than the configured interval.
    """
    import redis.asyncio as aioredis
    from service.index.scanner import scan
    from service.navidrome.client import trigger_scan

    interval = settings.rescan_interval_minutes
    if interval <= 0:
        return

    redis_key = "audioreap:last_auto_rescan"
    try:
        rc = aioredis.from_url(settings.redis_url)
        last_ts = await rc.get(redis_key)
        if last_ts:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last_ts.decode())).total_seconds() / 60
            if elapsed < interval:
                await rc.aclose()
                return
        await rc.set(redis_key, datetime.utcnow().isoformat())
        await rc.aclose()
    except Exception as exc:
        logger.warning("auto_rescan: redis check failed: %s", exc)
        return

    session_factory: async_sessionmaker = ctx.get("session_factory")  # type: ignore[assignment]
    if session_factory is None:
        return

    try:
        async with session_factory() as session:
            # Incremental: skip files whose path+mtime are unchanged so we don't
            # re-read tags (mutagen open+parse) for the entire library on every
            # tick — the point of the periodic rescan is to pick up newly-added
            # or changed files cheaply. External deletions are reconciled by a
            # full scan (manual, or the daily fix-tags sweep), not here.
            await scan(session, settings.music_dir, incremental=True)
        logger.info("auto_rescan: library scan complete")
    except Exception as exc:
        logger.warning("auto_rescan: scan failed: %s", exc)

    try:
        await trigger_scan()
    except Exception as exc:
        logger.warning("auto_rescan: Navidrome trigger failed: %s", exc)


from arq.cron import cron  # noqa: E402

from service.acquisition.jobs import acquire_album, acquire_album_from_mb, acquire_track, enrich_track, fetch_missing_covers, fetch_missing_lyrics, fix_all_album_tags, gc_staging  # noqa: E402


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [acquire_track, acquire_album, acquire_album_from_mb, enrich_track, gc_staging, fetch_missing_covers, fetch_missing_lyrics, fix_all_album_tags, worker_heartbeat, auto_rescan]
    cron_jobs = [
        cron(gc_staging, hour=3, minute=0),
        cron(fix_all_album_tags, hour=4, minute=0),     # daily; no-op unless auto_fix_tags_enabled
        cron(worker_heartbeat, minute=None, second=0),  # every minute
        cron(auto_rescan, minute=None, second=30),      # every minute (offset from heartbeat)
    ]
    max_jobs = settings.worker_concurrency
    # Slow the queue poller down from arq's 0.5s default to cut constant idle
    # Redis load (the zrangebyscore busy-poll). Job pickup latency rises to at
    # most this many seconds, which is irrelevant for downloads.
    poll_delay = settings.worker_poll_delay_seconds
    # A download job may now park in the rate gate's "waiting" state (up to a
    # ~120s 429 cooldown) before the download itself runs, so the arq default of
    # 300s would prematurely abort a paced job. Give jobs ample headroom.
    job_timeout = 1800
    on_startup = startup
    on_shutdown = shutdown
