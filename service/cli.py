"""Admin CLI — thin wrapper around the service internals."""
from __future__ import annotations

import asyncio
import sys


def _usage() -> None:
    print(
        "Usage: service <command>\n"
        "\n"
        "Commands:\n"
        "  scan                     Full library rescan\n"
        "  scan --incremental       Only process changed files\n"
        "  acquire <query>          Search and acquire a track\n"
        "  jobs list                List recent acquisition jobs\n"
        "  jobs retry <job-id>      Re-enqueue a failed job\n",
        file=sys.stderr,
    )


# ── scan ──────────────────────────────────────────────────────────────────

async def _run_scan(incremental: bool) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from service.config import settings
    from service.db.schema import Base
    from service.index.scanner import scan

    engine = create_async_engine(settings.db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session, session.begin():
        result = await scan(session, settings.music_dir, incremental=incremental)
    await engine.dispose()
    print(
        f"Scan done — added={result.added} updated={result.updated} "
        f"skipped={result.skipped} removed={result.removed} errors={result.errors}"
    )


# ── acquire ───────────────────────────────────────────────────────────────

async def _run_acquire(query: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import service.providers.ytdlp  # noqa: F401
    from service.acquisition.jobs import create_job
    from service.config import settings
    from service.core.models import SearchQuery
    from service.db.schema import Base
    from service.providers import get

    engine = create_async_engine(settings.db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    provider = get("ytdlp")()

    print(f"Searching: {query!r} …")
    candidates = []
    async for c in provider.search(SearchQuery(q=query, limit=5)):
        candidates.append(c)
        print(f"  [{len(candidates)}] {c.artist} — {c.title} ({c.duration_seconds}s)")
        if len(candidates) >= 5:
            break

    if not candidates:
        print("No results found.", file=sys.stderr)
        await engine.dispose()
        sys.exit(1)

    choice_str = input("Pick a result [1]: ").strip() or "1"
    try:
        chosen = candidates[int(choice_str) - 1]
    except (ValueError, IndexError):
        print("Invalid choice.", file=sys.stderr)
        await engine.dispose()
        sys.exit(1)

    async with session_factory() as session, session.begin():
        job_id = await create_job(
            session,
            provider_name="ytdlp",
            provider_ref=chosen.provider_ref,
            candidate=chosen,
            query=query,
        )

    from arq import create_pool

    redis = await create_pool(settings.redis_url)  # type: ignore[arg-type]
    await redis.enqueue_job(
        "acquire_track",
        job_id=job_id,
        provider_name="ytdlp",
        provider_ref=chosen.provider_ref,
        candidate_json=chosen.model_dump_json(),
        music_dir=str(settings.music_dir),
        tmp_acquire_dir=str(settings.tmp_acquire_dir),
        _job_id=f"acquire:{job_id}",
    )
    await redis.aclose()
    await engine.dispose()
    print(f"Enqueued job {job_id}")


# ── jobs ──────────────────────────────────────────────────────────────────

async def _jobs_list() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from service.config import settings
    from service.db.schema import AcquisitionJobRow

    engine = create_async_engine(settings.db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AcquisitionJobRow).order_by(AcquisitionJobRow.created_at.desc()).limit(20)
            )
        ).scalars().all()
    await engine.dispose()

    if not rows:
        print("No jobs.")
        return
    for row in rows:
        label = row.query or f"{row.provider}:{row.provider_ref[:40]}"
        print(f"{row.id[:8]}  {row.state:<12}  {label}")


async def _jobs_retry(job_id: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from service.config import settings
    from service.db.schema import AcquisitionJobRow

    engine = create_async_engine(settings.db_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        row = await session.get(AcquisitionJobRow, job_id)
        if row is None:
            print(f"Job {job_id!r} not found.", file=sys.stderr)
            await engine.dispose()
            sys.exit(1)
        candidate_json = row.candidate_json
        provider_name = row.provider
        provider_ref = row.provider_ref

    await engine.dispose()

    if not candidate_json:
        print("No candidate data stored — cannot retry.", file=sys.stderr)
        sys.exit(1)

    from arq import create_pool

    redis = await create_pool(settings.redis_url)  # type: ignore[arg-type]
    await redis.enqueue_job(
        "acquire_track",
        job_id=job_id,
        provider_name=provider_name,
        provider_ref=provider_ref,
        candidate_json=candidate_json,
        music_dir=str(settings.music_dir),
        tmp_acquire_dir=str(settings.tmp_acquire_dir),
        _job_id=f"acquire:{job_id}",
    )
    await redis.aclose()
    print(f"Re-enqueued job {job_id}")


# ── entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]
    if not args:
        _usage()
        sys.exit(1)

    cmd = args[0]
    if cmd == "scan":
        asyncio.run(_run_scan("--incremental" in args))
    elif cmd == "acquire":
        if len(args) < 2:
            print("Usage: service acquire <query>", file=sys.stderr)
            sys.exit(1)
        asyncio.run(_run_acquire(args[1]))
    elif cmd == "jobs":
        sub = args[1] if len(args) > 1 else ""
        if sub == "list":
            asyncio.run(_jobs_list())
        elif sub == "retry" and len(args) > 2:
            asyncio.run(_jobs_retry(args[2]))
        else:
            _usage()
            sys.exit(1)
    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        _usage()
        sys.exit(1)
