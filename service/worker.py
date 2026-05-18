import logging
import shutil
from datetime import datetime, timedelta

from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from service.config import settings
from service.db.schema import AcquisitionJobRow

logger = logging.getLogger(__name__)

_STUCK_STATES = frozenset({"downloading", "processing", "tagging", "importing"})
_STUCK_CUTOFF_MINUTES = 15


async def _recover_stuck_jobs(session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
    cutoff = datetime.utcnow() - timedelta(minutes=_STUCK_CUTOFF_MINUTES)
    async with session_factory() as session, session.begin():
        rows = (await session.execute(
            select(AcquisitionJobRow).where(
                AcquisitionJobRow.state.in_(list(_STUCK_STATES)),
                AcquisitionJobRow.updated_at < cutoff,
            )
        )).scalars().all()
        for row in rows:
            logger.warning("Resetting stuck job %s (was %s, idle since %s)", row.id, row.state, row.updated_at)
            row.state = "failed"
            row.failure_class = "transient"
            row.error = f"Stuck in '{row.state}' for >{_STUCK_CUTOFF_MINUTES}m — reset at worker startup"
            row.updated_at = datetime.utcnow()


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
    from service.providers import all_providers

    logger.info("yt-dlp version: %s", yt_dlp.version.__version__)

    engine = create_async_engine(settings.db_url)
    ctx["engine"] = engine
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    ctx["session_factory"] = session_factory
    ctx["providers"] = {cls.name: cls() for cls in all_providers()}
    ctx["music_dir"] = str(settings.music_dir)
    ctx["tmp_acquire_dir"] = str(settings.tmp_acquire_dir)

    await _recover_stuck_jobs(session_factory)
    _cleanup_tmp(str(settings.tmp_acquire_dir))

    providers: dict[str, object] = ctx["providers"]  # type: ignore[assignment]
    logger.info("audioreap worker ready — providers: %s", list(providers))


async def shutdown(ctx: dict[str, object]) -> None:
    engine = ctx.get("engine")
    if isinstance(engine, AsyncEngine):
        await engine.dispose()
    logger.info("audioreap worker stopped")


from service.acquisition.jobs import acquire_album, acquire_track  # noqa: E402


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [acquire_track, acquire_album]
    max_jobs = settings.worker_concurrency
    on_startup = startup
    on_shutdown = shutdown
