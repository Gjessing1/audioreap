from __future__ import annotations

import asyncio
import contextvars
import logging
import subprocess
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import secrets

# ── Per-request context var for log correlation ───────────────────────────
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get("-")
        return True


def _configure_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(request_id)s] %(levelname)s %(name)s: %(message)s")
    filt = _RequestIdFilter()
    root = logging.getLogger()
    for handler in root.handlers or [logging.StreamHandler()]:
        handler.setFormatter(fmt)
        handler.addFilter(filt)
        if handler not in root.handlers:
            root.addHandler(handler)


_configure_logging()

import aiofiles
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.middleware import Middleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from service.config import settings
from service.core.models import (
    AcquisitionJob,
    SearchResult,
    TrackQuality,
    TrackRef,
)
from service.db.schema import AcquisitionJobRow, Artist, Track
from service.db.session import get_session

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).parent.parent / "alembic.ini"


def _run_migrations() -> None:
    """Run alembic upgrade head synchronously (called in a thread on startup)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(_ALEMBIC_INI), "upgrade", "heads"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    if result.returncode != 0:
        logger.error("Migration failed:\n%s", result.stderr)
    else:
        logger.info("Migrations OK: %s", result.stdout.strip() or "up to date")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await asyncio.to_thread(_run_migrations)
    from service.config import load_config_overrides
    load_config_overrides()
    yield


app = FastAPI(title="audioreap", version="0.1.0", lifespan=lifespan)


# ── Basic auth middleware (optional) ──────────────────────────────────────

_UNPROTECTED = frozenset({"/health", "/static"})


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: object) -> Response:
    """Attach a short request ID to each request for log correlation."""
    from collections.abc import Callable
    import uuid
    call_next_fn: Callable = call_next  # type: ignore[assignment]
    rid = secrets.token_hex(4)  # 8 hex chars, compact but unique enough
    token = _request_id_var.set(rid)
    try:
        response: Response = await call_next_fn(request)
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        _request_id_var.reset(token)


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next: object) -> Response:
    from collections.abc import Callable
    call_next_fn: Callable = call_next  # type: ignore[assignment]

    if not settings.ui_password:
        return await call_next_fn(request)

    path = request.url.path
    # /healthz, not /health: the exemption was written for the unauthenticated
    # JSON liveness probe, and /health is now the web UI's System-health page
    # (disk, queue depth, service status) — which should sit behind auth with
    # the rest of the UI. The container healthcheck sends no credentials.
    if (path == "/healthz"
            or path.startswith("/static/")
            or path.endswith(".webmanifest")
            or path.endswith("sw.js")
            # The Android release pair is deliberately unauthenticated: an APK
            # download is handed to Android's download manager, which carries
            # none of the browser's credentials, and a first install has no
            # credentials to carry anyway. The APK holds no secrets — the server
            # address is typed in on first launch — so the only thing exposed is
            # the shell itself.
            or path == "/api/app/version"
            or path == "/api/app/download"
            # The Android app's background check is made by an alarm-woken broadcast
            # receiver, which carries none of the WebView's credentials and cannot
            # follow an SSO redirect. It presents a per-device bearer token instead,
            # which the route checks itself — unconditionally, so this exemption
            # widens nothing (service/api/routes/push.py).
            or path == "/api/push/pending"):
        return await call_next_fn(request)

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            import base64
            decoded = base64.b64decode(auth[6:]).decode()
            username, _, password = decoded.partition(":")
            user_ok = secrets.compare_digest(username, settings.ui_username or "audioreap")
            pass_ok = secrets.compare_digest(password, settings.ui_password)
            if user_ok and pass_ok:
                return await call_next_fn(request)
        except Exception:
            pass

    from fastapi.responses import Response as Resp
    return Resp(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="audioreap"'},
        content="Unauthorized",
    )


# ── Static files ──────────────────────────────────────────────────────────
_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# ── Web UI ────────────────────────────────────────────────────────────────
from service.api.webui import router as webui_router  # noqa: E402

app.include_router(webui_router)


# ── Liveness probe ────────────────────────────────────────────────────────
# Deliberately /healthz, not /health: the web UI's System-health page owns
# /health (service/api/routes/admin.py), and webui_router is included above,
# so anything registered here as /health is shadowed and never reached. That
# shadowing is what silently put the admin page — a full template render that
# round-trips Navidrome, Redis and the DB — behind the container healthcheck,
# once every 10s. Keep this endpoint free of I/O so it stays cheap to poll.

@app.get("/healthz")
async def healthz() -> dict[str, object]:
    return {
        "status": "ok",
        "version": "0.1.0",
        "music_dir": str(settings.music_dir),
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── Audio streaming ───────────────────────────────────────────────────────

_MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
}


def _mime(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")


@app.get("/api/stream/{internal_id}")
async def stream_track(
    internal_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    stmt = (
        select(Track)
        .options(joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or row.file is None:
        raise HTTPException(404, "Track not found")

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    file_size = file_path.stat().st_size
    content_type = _mime(file_path)
    range_header = request.headers.get("range")

    if range_header:
        # Parse "bytes=start-end"
        try:
            byte_range = range_header.strip().removeprefix("bytes=")
            raw_start, raw_end = byte_range.split("-", 1)
            start = int(raw_start) if raw_start else 0
            end = int(raw_end) if raw_end else file_size - 1
        except ValueError:
            raise HTTPException(416, "Invalid Range header")

        end = min(end, file_size - 1)
        if start > end:
            raise HTTPException(416, "Range not satisfiable")
        length = end - start + 1

        async def _gen(s: int, ln: int) -> object:
            async with aiofiles.open(file_path, "rb") as f:
                await f.seek(s)
                remaining = ln
                while remaining > 0:
                    chunk = await f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            _gen(start, length),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Content-Disposition": f'inline; filename="{file_path.name}"',
            },
        )

    # Full file
    async def _full() -> object:
        async with aiofiles.open(file_path, "rb") as f:
            while chunk := await f.read(65536):
                yield chunk

    return StreamingResponse(
        _full(),
        media_type=content_type,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Disposition": f'inline; filename="{file_path.name}"',
        },
    )


# ── Search ────────────────────────────────────────────────────────────────

@app.get("/api/search", response_model=SearchResult)
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> SearchResult:
    pattern = f"%{q}%"
    stmt = (
        select(Track)
        .join(Track.artist)
        .outerjoin(Track.album)
        .outerjoin(Track.file)
        .options(
            joinedload(Track.artist),
            joinedload(Track.album),
            joinedload(Track.file),
        )
        .where(or_(Track.title.ilike(pattern), Artist.name.ilike(pattern)))
        .order_by(Artist.name, Track.title)
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).unique().scalars().all()

    tracks: list[TrackRef] = []
    for row in rows:
        file = row.file
        quality: TrackQuality | None = None
        local_path: Path | None = None
        if file is not None:
            quality = TrackQuality(
                codec=file.codec,
                container=file.container,
                bitrate_kbps=file.bitrate_kbps,
                sample_rate_hz=file.sample_rate_hz,
            )
            local_path = Path(file.path)
        tracks.append(TrackRef(
            internal_id=row.id,
            source="local",
            status="available" if file else "missing",
            title=row.title,
            artist=row.artist.name,
            album=row.album.title if row.album else None,
            duration_seconds=row.duration_seconds,
            local_path=local_path,
            musicbrainz_recording_id=row.musicbrainz_recording_id,
            quality=quality,
        ))

    return SearchResult(tracks=tracks, albums=[], artists=[], query_echo=q)


# ── Scan ─────────────────────────────────────────────────────────────────

@app.post("/api/scan")
async def trigger_scan_endpoint(background_tasks: BackgroundTasks) -> dict[str, str]:
    """Trigger a full library rescan in the background."""
    from service.db.session import AsyncSessionLocal
    from service.index.scanner import scan as run_scan

    async def _do_scan() -> None:
        async with AsyncSessionLocal() as session, session.begin():
            result = await run_scan(session, settings.music_dir)
            logger.info(
                "UI-triggered scan done: added=%d updated=%d removed=%d errors=%d",
                result.added, result.updated, result.removed, result.errors,
            )

    background_tasks.add_task(_do_scan)
    return {"status": "scan started"}


# ── Acquire ───────────────────────────────────────────────────────────────

class AcquireResponse(BaseModel):
    job_id: str


@app.post("/api/acquire")
async def acquire(
    request: Request,
    session: AsyncSession = Depends(get_session),
    provider_name: str = Form(default="ytdlp"),
    provider_ref: str = Form(...),
    candidate_json: str = Form(...),
    query: str = Form(default=""),
) -> Response:
    """Accept form data (from HTMX) and enqueue an acquisition job."""
    from service.acquisition.jobs import create_or_get_active_job, mark_enqueue_failed
    from service.api.shared import _acquisition_receipt
    from service.core.models import TrackCandidate
    from service.db.schema import Track

    candidate = TrackCandidate.model_validate_json(candidate_json)

    # Pre-flight duplicate check: skip if MB recording ID is already owned
    if candidate.mb_recording_id:
        existing = (await session.execute(
            select(Track).where(Track.musicbrainz_recording_id == candidate.mb_recording_id).limit(1)
        )).scalars().first()
        if existing is not None:
            from fastapi.templating import Jinja2Templates as _T
            _tmpl = _T(directory=str(Path(__file__).parent / "templates"))
            return _tmpl.TemplateResponse(
                request, "partials/already_owned.html",
                {"track": existing, "title": candidate.title, "artist": candidate.artist},
            )

    job_id, created = await create_or_get_active_job(
        session,
        provider_name=provider_name,
        provider_ref=provider_ref,
        candidate=candidate,
        query=query or None,
    )
    await session.commit()

    if created:
        try:
            from arq import create_pool
            from arq.connections import RedisSettings
            redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
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
        except Exception as exc:
            await mark_enqueue_failed(session, job_id, exc)
            raise HTTPException(status_code=503, detail=f"Queue unavailable: {exc}") from exc

    row = await session.get(AcquisitionJobRow, job_id)
    if row:
        return _acquisition_receipt(
            request,
            job_id=job_id,
            title=candidate.title,
            artist=candidate.artist,
            state=row.state,
            created=created,
        )
    return AcquireResponse(job_id=job_id)


# ── Jobs ──────────────────────────────────────────────────────────────────

from service.core.job_model import job_row_to_model as _job_row_to_model  # noqa: E402


@app.get("/api/preview")
async def preview_audio(ref: str = Query(...)) -> Response:
    """Return a redirect to the direct audio stream URL for a provider ref."""
    from fastapi.responses import RedirectResponse

    def _extract() -> str:
        import yt_dlp

        opts = {
            "format": "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(ref, download=False)
            return str(info.get("url") or info["webpage_url"])

    try:
        direct_url = await asyncio.to_thread(_extract)
    except Exception as exc:
        raise HTTPException(502, f"Preview extraction failed: {exc}") from exc

    return RedirectResponse(direct_url, status_code=307)


@app.get("/api/jobs", response_model=list[AcquisitionJob])
async def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[AcquisitionJob]:
    rows = (
        await session.execute(
            select(AcquisitionJobRow)
            .order_by(AcquisitionJobRow.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_job_row_to_model(r) for r in rows]


@app.get("/api/jobs/{job_id}", response_model=AcquisitionJob)
async def get_job(
    job_id: str,
    session: AsyncSession = Depends(get_session),
) -> AcquisitionJob:
    row = await session.get(AcquisitionJobRow, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_row_to_model(row)


# ── Metadata enrichment ───────────────────────────────────────────────────

class EnrichResponse(BaseModel):
    queued: int


@app.post("/api/enrich/all", response_model=EnrichResponse)
async def enrich_all(session: AsyncSession = Depends(get_session)) -> EnrichResponse:
    """Queue MusicBrainz enrichment jobs for all tracks lacking a Recording ID."""
    from service.db.schema import Track

    rows = (
        await session.execute(
            select(Track.id).where(Track.musicbrainz_recording_id.is_(None))
        )
    ).scalars().all()

    if not rows:
        return EnrichResponse(queued=0)

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        for track_id in rows:
            await redis.enqueue_job("enrich_track", track_id=track_id)
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Queue unavailable: {exc}") from exc

    logger.info("Queued %d enrichment jobs", len(rows))
    return EnrichResponse(queued=len(rows))
