import logging

from arq.connections import RedisSettings

from service.config import settings

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, object]) -> None:
    logger.info("audioreap worker starting")


async def shutdown(ctx: dict[str, object]) -> None:
    logger.info("audioreap worker stopping")


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions: list[object] = []
    cron_jobs: list[object] = []
    max_jobs = settings.worker_concurrency
    on_startup = startup
    on_shutdown = shutdown
