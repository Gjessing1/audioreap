"""Shared web-UI infrastructure: templates, scan scheduling, cross-section helpers."""
from __future__ import annotations

import asyncio
import html
import json
import logging
from pathlib import Path
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from service.config import settings
from service.db.schema import AcquisitionJobRow, PlaylistImport, Track


logger = logging.getLogger(__name__)


templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


# Expose settings as Jinja globals so partials (browse_row, etc.) can use them without
# requiring every calling endpoint to pass them in context explicitly.
templates.env.globals["settings"] = settings


def _local_album_rows(album) -> list[dict]:
    """Owned-track rows for album_tracklist.html's local (non-MB) mode.

    Sorted disc-major so multi-disc albums don't interleave (two "track 1" rows
    from different discs). Done here rather than in Jinja because disc_number is
    None on single-disc tracks and Jinja's sort can't compare None with int.
    """
    def _key(t):
        return (t.disc_number or 1, t.track_number is None, t.track_number or 0, t.title or "")
    return [
        {"status": "plain", "number": t.track_number, "title": t.title,
         "track": t, "disc": t.disc_number}
        for t in sorted(album.tracks, key=_key)
    ]


templates.env.globals["local_album_rows"] = _local_album_rows


_JOBS_COMPLETED_PAGE = 50


_BROWSE_PAGE = 75


_LIST_PAGE = 100  # Albums / Artists load-more page size


_COMPLETED_STATES = ("done", "failed", "cancelled")


def _error_badge(message: str, *, level: str = "warn") -> HTMLResponse:
    """Shared inline error partial for HTMX swap targets.

    The one error-presentation pattern for routes whose hx-target is a small
    inline element (spans/badges) — full-page routes should keep raising
    HTTPException instead. level="warn" for user-fixable conditions,
    "fail" for operations that errored. Always 200: htmx does not swap
    non-2xx responses into the target, so the message would never be seen.
    Escapes the message — exception text can embed file/MB-derived strings.
    """
    cls = "badge badge-fail" if level == "fail" else "badge badge-warn"
    feedback_level = "error" if level == "fail" else "warning"
    return HTMLResponse(
        f'<span class="{cls}">{html.escape(str(message))}</span>',
        headers={"X-Feedback-Level": feedback_level},
    )


def _acquisition_receipt(
    request: Request,
    *,
    job_id: str,
    title: str,
    artist: str | None = None,
    state: str = "queued",
    created: bool = True,
) -> HTMLResponse:
    """Render the shared acknowledgement returned by individual Get actions."""
    response = templates.TemplateResponse(
        request,
        "partials/acquisition_receipt.html",
        {
            "job_id": job_id,
            "title": title,
            "artist": artist,
            "state": state,
            "created": created,
        },
    )
    response.headers["HX-Trigger"] = json.dumps({
        "jobsChanged": {
            "jobId": job_id,
            "state": state,
            "created": created,
        }
    })
    return response


def _acquisition_batch_receipt(
    request: Request,
    *,
    batch_id: str,
    title: str,
    queued_count: int,
    owned_count: int = 0,
    failed_count: int = 0,
    jobs_anchor: str = "",
    unit: str = "track",
    retry_url: str | None = None,
    retry_ids: list[str] | None = None,
    retry_field: str = "batch_ids",
    failed_items: list[dict[str, str]] | None = None,
) -> HTMLResponse:
    """Render one acknowledgement for playlist and album mutations.

    Batch routes intentionally return HTTP 200 even when only part of the work
    reached Redis: HTMX can then keep the actionable failed summary in place
    and let the user retry just those coordinator/job IDs.
    """
    response = templates.TemplateResponse(
        request,
        "partials/acquisition_batch_receipt.html",
        {
            "batch_id": batch_id,
            "title": title,
            "queued_count": queued_count,
            "owned_count": owned_count,
            "failed_count": failed_count,
            "jobs_anchor": jobs_anchor,
            "unit": unit,
            "retry_url": retry_url,
            "retry_ids": retry_ids or [],
            "retry_field": retry_field,
            "failed_items": failed_items or [],
        },
    )
    response.headers["HX-Trigger"] = json.dumps({
        "jobsChanged": {
            "batchId": batch_id,
            "queued": queued_count,
            "owned": owned_count,
            "failed": failed_count,
        }
    })
    return response


_ACTIVE_STATES_EXCLUDE = _COMPLETED_STATES  # states NOT in active list


_scan_task: asyncio.Task | None = None  # deduplicate concurrent auto-scans


async def _run_auto_scan() -> None:
    """Incremental scan + orphan cleanup in a throw-away session."""
    from service.db.session import AsyncSessionLocal
    from service.index.scanner import scan as _scan
    from service.db.schema import Album as _Album, Artist as _Artist, Track as _Track, TrackFile as _TrackFile

    try:
        async with AsyncSessionLocal() as session:
            await _scan(session, settings.music_dir, incremental=True)
            # Orphan cleanup: remove Album/Artist rows that lost all their tracks
            # (e.g. after a delete_track that leaves an empty album/artist)
            await session.execute(
                sa_delete(_Album).where(
                    ~_Album.id.in_(select(_Track.album_id).where(_Track.album_id.is_not(None)))
                )
            )
            await session.execute(
                sa_delete(_Artist).where(
                    ~_Artist.id.in_(select(_Track.artist_id))
                )
            )
            await session.commit()
    except Exception as exc:
        logger.warning("Auto-scan failed: %s", exc)


def _schedule_auto_scan() -> None:
    """Fire-and-forget incremental scan after a mutation.  Skips if one is already running."""
    global _scan_task
    if _scan_task is not None and not _scan_task.done():
        return  # already in progress
    try:
        loop = asyncio.get_running_loop()
        _scan_task = loop.create_task(_run_auto_scan())
    except RuntimeError:
        pass  # no running loop (tests, startup)


async def _do_scans() -> None:
    """Trigger Navidrome rescan and schedule audioreap incremental scan."""
    from service.navidrome.client import trigger_scan as _nv
    try:
        await _nv()
    except Exception as exc:
        logger.debug("best-effort Navidrome scan trigger failed: %s", exc)
    _schedule_auto_scan()


async def _acquire_ctx(request: Request, q: str, active_section: str, session: AsyncSession) -> dict:
    """Build context dict shared by the /acquire page."""
    from sqlalchemy import func as sa_func
    rows = (
        await session.execute(
            select(PlaylistImport).order_by(PlaylistImport.created_at.desc()).limit(10)
        )
    ).scalars().all()
    _ACTIVE = ("queued", "waiting", "downloading", "processing", "enriching", "tagging", "importing", "placing")
    import_states: dict[str, str] = {}
    if rows:
        active_counts = (await session.execute(
            select(AcquisitionJobRow.playlist_import_id, sa_func.count())
            .where(
                AcquisitionJobRow.playlist_import_id.in_([r.id for r in rows]),
                AcquisitionJobRow.state.in_(list(_ACTIVE)),
            )
            .group_by(AcquisitionJobRow.playlist_import_id)
        )).all()
        active_by_id = {pid: cnt for pid, cnt in active_counts}
        for r in rows:
            import_states[r.id] = "active" if active_by_id.get(r.id, 0) > 0 else "done"
    return {
        "active": "acquire",
        "q": q,
        "active_section": active_section,
        "imports": rows,
        "import_states": import_states,
        "spotify_enabled": bool(settings.spotify_client_id),
    }


async def _mb_recording_search(
    request: Request, q: str, limit: int, duration: int | None, **ctx: object
) -> HTMLResponse:
    """Shared MB recording search → mb_candidates.html partial.

    Used by both the review-card (job) and library-track edit-card search
    boxes; ctx carries the route-specific keys (job_id / track_id).
    """
    if not q.strip():
        return HTMLResponse("")
    from service.metadata.musicbrainz import search_recordings_free
    results = await asyncio.to_thread(
        search_recordings_free, q.strip(), limit, settings.cache_dir, duration
    )
    return templates.TemplateResponse(
        request, "partials/mb_candidates.html",
        {"results": results, "q": q.strip(), "limit": limit, "duration": duration, **ctx},
    )


def _layout_view(request: Request, view: str, cookie: str) -> str:
    """Resolve a list/grid layout preference: explicit ?view= wins, else the
    cookie set on every list render. Grid is the default for fresh visitors."""
    if view in ("list", "grid"):
        return view
    return "list" if request.cookies.get(cookie) == "list" else "grid"


def _resize_cover(art: bytes, size: int, dest: Path) -> bytes | None:
    """Downscale cover art to `size` px wide via ffmpeg and cache it at dest.

    ffmpeg is used instead of Pillow so no new dependency enters the image.
    Never upscales (min(size, iw)). Returns None on any failure — callers
    fall back to serving the full-size art.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-i", "pipe:0",
                "-vf", f"scale='min({size},iw)':-1",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1",
            ],
            input=art, capture_output=True, timeout=20,
        )
        if proc.returncode != 0 or not proc.stdout:
            logger.debug(
                "cover thumbnail resize failed (rc=%s): %s",
                proc.returncode, (proc.stderr or b"")[-200:],
            )
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(proc.stdout)
        tmp.replace(dest)
        return proc.stdout
    except Exception as exc:
        logger.debug("cover thumbnail resize failed: %s", exc)
        return None


async def _get_track_with_file(session: AsyncSession, internal_id: str) -> Track:
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    return row
