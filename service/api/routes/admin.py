"""System health page and admin: yt-dlp update, cookies, runtime config."""
from __future__ import annotations

import asyncio
import logging
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from service.config import settings
from service.db.schema import AcquisitionJobRow
from service.db.session import get_session

from service.api.shared import _error_badge, templates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", response_class=HTMLResponse)
async def health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    import shutil as _shutil

    try:
        disk = _shutil.disk_usage(settings.music_dir)
        disk_free_gb = round(disk.free / 1024**3, 1)
    except Exception:
        disk_free_gb = -1

    navidrome_ok = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{settings.navidrome_url}/rest/ping.view",
                params={"u": "x", "p": "x", "v": "1.16.1", "c": "audioreap", "f": "json"},
            )
            navidrome_ok = r.status_code < 500
    except Exception as exc:
        logger.debug("Navidrome ping failed: %s", exc)

    redis_ok = False
    worker_ok = False
    try:
        import redis.asyncio as aioredis
        from datetime import timedelta
        rc = aioredis.from_url(settings.redis_url)
        await rc.ping()
        redis_ok = True
        hb = await rc.get("audioreap:worker:heartbeat")
        if hb:
            from datetime import datetime as _dt
            hb_time = _dt.fromisoformat(hb.decode())
            worker_ok = (_dt.utcnow() - hb_time) < timedelta(minutes=2)
        await rc.aclose()
    except Exception as exc:
        logger.debug("worker heartbeat probe failed: %s", exc)

    active_jobs = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state.notin_(["done", "failed"]))
        )
    ).scalar_one()

    return templates.TemplateResponse(
        request, "health.html",
        {
            "active": "sys-health",
            "health": {
                "navidrome_ok": navidrome_ok,
                "redis_ok": redis_ok,
                "worker_ok": worker_ok,
                "disk_free_gb": disk_free_gb,
                "active_jobs": active_jobs,
                "music_dir": str(settings.music_dir),
                "version": "0.1.0",
            },
        },
    )


@router.post("/admin/update-ytdlp", response_class=HTMLResponse)
async def admin_update_ytdlp(request: Request) -> HTMLResponse:
    """Run pip install -U yt-dlp inside the container and report the result."""
    import subprocess
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            # Extract the new version line from pip output
            for line in (result.stdout + result.stderr).splitlines():
                if "yt-dlp" in line.lower() and ("successfully installed" in line.lower() or "already" in line.lower()):
                    return HTMLResponse(f'<span class="badge-ok">yt-dlp updated: {line.strip()}</span>')
            return HTMLResponse('<span class="badge-ok">yt-dlp updated ✓</span>')
        return _error_badge(f"pip failed (exit {result.returncode}): {result.stderr[:200]}")
    except Exception as exc:
        return _error_badge(f"Update failed: {exc}")


def _admin_config_ctx(*, saved: bool = False) -> dict:
    from service.config import (
        CONFIG_EDITABLE_KEYS,
        config_defaults,
        read_config_overrides,
    )
    from service.providers.ytdlp import active_cookies_file
    try:
        import yt_dlp
        ytdlp_version = yt_dlp.version.__version__
    except Exception:
        ytdlp_version = None

    current = {k: getattr(settings, k) for k in CONFIG_EDITABLE_KEYS}
    defaults = config_defaults()
    return {
        "active": "settings",
        "current": current,
        "defaults": defaults,
        # Per-key "this isn't the shipped default" flag. Settings paints a marker
        # and a restore link from it, so the page always answers "what did I
        # change?" without diffing against the docs.
        "modified": {k: current[k] != defaults[k] for k in CONFIG_EDITABLE_KEYS},
        "modified_count": sum(current[k] != defaults[k] for k in CONFIG_EDITABLE_KEYS),
        # Whether anything has ever been saved from the UI. Not a count: a save
        # writes every editable key, so counting them would always say "all".
        "has_overrides": bool(read_config_overrides()),
        "cookies_active": active_cookies_file(),
        "ytdlp_version": ytdlp_version,
        "saved": saved,
    }


@router.get("/admin/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "admin_config.html", _admin_config_ctx())


@router.post("/admin/cookies", response_class=HTMLResponse)
async def admin_cookies_save(request: Request) -> HTMLResponse:
    """Save a UI-uploaded Netscape cookies.txt to the writable /data jar (or clear it).

    Lets the user paste/upload cookies exported from a logged-in browser window so
    age-gated downloads work without editing the :ro bind-mount. Read live by the
    worker at download time — no restart needed.
    """
    from service.providers.ytdlp import managed_cookies_path

    form = await request.form()
    managed = managed_cookies_path()

    if (form.get("action") or "save") == "clear":
        try:
            managed.unlink(missing_ok=True)
        except OSError as exc:
            return _error_badge(f"Clear failed: {exc}")
        return HTMLResponse('<span class="badge-ok">Cookies cleared — downloads run anonymously.</span>')

    # Content can come from a file input or a pasted textarea.
    content = ""
    upload = form.get("file")
    if upload is not None and hasattr(upload, "read"):
        raw = await upload.read()
        if raw:
            content = raw.decode("utf-8", errors="ignore")
    if not content.strip():
        content = str(form.get("cookies") or "")
    content = content.strip()
    if not content:
        return _error_badge("Nothing to save — paste a cookies.txt or choose a file.")

    def _is_cookie(ln: str) -> bool:
        return bool(ln.strip()) and not ln.strip().startswith("#") and "\t" in ln

    lines = content.splitlines()
    n = sum(1 for ln in lines if _is_cookie(ln))
    if n == 0:
        return _error_badge(
            "That doesn’t look like a Netscape cookies.txt (no tab-separated cookie "
            "lines). Export it with a “Get cookies.txt” browser extension on "
            "youtube.com and paste the whole file."
        )
    if not lines[0].startswith(("# Netscape", "# HTTP Cookie")):
        content = "# Netscape HTTP Cookie File\n" + content
    try:
        managed.parent.mkdir(parents=True, exist_ok=True)
        tmp = managed.with_name(managed.name + ".tmp")
        tmp.write_text(content + "\n", encoding="utf-8")
        tmp.replace(managed)
    except OSError as exc:
        return _error_badge(f"Save failed: {exc}")
    return HTMLResponse(
        f'<span class="badge-ok">Saved {n} cookie{"" if n == 1 else "s"} to /data/cookies.txt — '
        f'age-gated downloads will use them. No restart needed.</span>'
    )


@router.post("/admin/config", response_class=HTMLResponse)
async def admin_config_save(request: Request) -> HTMLResponse:
    """Persist the Settings form.

    Toggles post as a hidden "false" immediately followed by the checkbox's
    "true", so an unchecked switch still submits a value instead of vanishing
    from the form. Reading the *last* value for a key is what makes that work —
    don't swap this back to ``form.get()``.
    """
    from service.config import CONFIG_EDITABLE_KEYS, save_config_overrides
    form = await request.form()
    overrides: dict = {}
    for key in CONFIG_EDITABLE_KEYS:
        submitted = form.getlist(key)
        if not submitted:
            continue
        val = submitted[-1]
        field_type = type(getattr(settings, key))
        if field_type is bool:
            overrides[key] = val == "true"
        else:
            try:
                overrides[key] = field_type(val)
            except Exception as exc:
                logger.debug("config override value not coercible, skipped: %s", exc)
    save_config_overrides(overrides)

    ctx = _admin_config_ctx(saved=True)
    if request.headers.get("HX-Request"):
        # Swap only the summary strip; the form keeps the values the user just
        # typed. The shared toast layer announces the save from this header.
        return templates.TemplateResponse(
            request, "partials/settings_summary.html", ctx,
            headers={"X-Feedback-Message": "Settings saved."},
        )
    return templates.TemplateResponse(request, "admin_config.html", ctx)
