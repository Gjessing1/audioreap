import asyncio
import sys

from service.config import settings


def _usage() -> None:
    print(
        "Usage: service <command>\n"
        "\n"
        "Commands:\n"
        "  scan              Full library rescan\n"
        "  scan --incremental  Only process changed files\n",
        file=sys.stderr,
    )


async def _run_scan(incremental: bool) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from service.db.schema import Base
    from service.index.scanner import scan

    engine = create_async_engine(settings.db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            result = await scan(session, settings.music_dir, incremental=incremental)

    print(
        f"Scan done — added={result.added} updated={result.updated} "
        f"skipped={result.skipped} removed={result.removed} errors={result.errors}"
    )
    await engine.dispose()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        _usage()
        sys.exit(1)

    if args[0] == "scan":
        incremental = "--incremental" in args
        asyncio.run(_run_scan(incremental))
    else:
        print(f"Unknown command: {args[0]!r}", file=sys.stderr)
        _usage()
        sys.exit(1)
