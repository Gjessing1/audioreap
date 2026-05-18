import logging

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from service.config import settings

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, object]) -> None:
    import service.providers.ytdlp  # noqa: F401 — registers YtdlpProvider
    from service.providers import all_providers

    engine = create_async_engine(settings.db_url)
    ctx["engine"] = engine
    ctx["session_factory"] = async_sessionmaker(engine, expire_on_commit=False)
    ctx["providers"] = {cls.name: cls() for cls in all_providers()}
    ctx["music_dir"] = str(settings.music_dir)
    ctx["tmp_acquire_dir"] = str(settings.tmp_acquire_dir)
    providers: dict[str, object] = ctx["providers"]  # type: ignore[assignment]
    logger.info("audioreap worker starting — providers: %s", list(providers))


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
