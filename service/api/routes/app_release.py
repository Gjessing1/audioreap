"""Android release metadata and APK download.

The APK is a thin Capacitor shell around this server's web UI, so almost every
change ships as a page reload — no new APK. The shell itself changes rarely, and
when it does the phone has to be told: `/api/app/version` is what an installed
app polls, `/api/app/download` is where both it and a first install get the file.

The bytes are published out-of-band by `scripts/publish-android.sh`, which writes
the APK plus a `version.json` describing it into ``settings.android_app_dir``.
This module only reads that pair, and treats anything malformed as "nothing
published" — a half-written release must never be offered as an update.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from service.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

_VERSION_NAME = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]*$")
_APK_FILE = re.compile(r"^audioreap-[0-9A-Za-z._-]+\.apk$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class PublishedApp:
    version_code: int
    version_name: str
    file: str
    sha256: str
    bytes: int
    apk_path: Path


def read_published_app(app_dir: Path) -> PublishedApp:
    """The currently published release, or raise.

    Every field is re-validated rather than trusted: the filename becomes a path
    join and a Content-Disposition, so it must be a plain basename in the shape
    the publish script writes, never a traversal or a header injection.
    """
    metadata = json.loads((app_dir / "version.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("version.json is not an object")

    version_code = metadata.get("versionCode")
    version_name = metadata.get("versionName")
    file = metadata.get("file")
    sha256 = metadata.get("sha256")

    if not isinstance(version_code, int) or isinstance(version_code, bool) or version_code < 1:
        raise ValueError("version.json has an invalid versionCode")
    if not isinstance(version_name, str) or not _VERSION_NAME.match(version_name):
        raise ValueError("version.json has an invalid versionName")
    if not isinstance(file, str) or file != Path(file).name or not _APK_FILE.match(file):
        raise ValueError("version.json has an invalid APK filename")
    if not isinstance(sha256, str) or not _SHA256.match(sha256):
        raise ValueError("version.json has an invalid sha256")

    apk_path = app_dir / file
    if not apk_path.is_file():
        raise FileNotFoundError(f"published APK is missing: {apk_path}")

    return PublishedApp(
        version_code=version_code,
        version_name=version_name,
        file=file,
        sha256=sha256,
        bytes=apk_path.stat().st_size,
        apk_path=apk_path,
    )


def _published_or_404() -> PublishedApp:
    try:
        return read_published_app(settings.android_app_dir)
    except FileNotFoundError:
        raise HTTPException(404, "No Android app has been published")
    except (OSError, ValueError) as exc:
        logger.error("published Android app is unreadable: %s", exc)
        raise HTTPException(404, "No Android app has been published")


@router.get("/api/app/version")
async def app_version() -> JSONResponse:
    """What the installed APK compares itself against."""
    published = _published_or_404()
    return JSONResponse(
        {
            "versionCode": published.version_code,
            "versionName": published.version_name,
            "sha256": published.sha256,
            "bytes": published.bytes,
            "apkUrl": "/api/app/download",
        },
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/app/download")
async def app_download() -> FileResponse:
    """The signed APK itself, at a URL that stays the same across releases."""
    published = _published_or_404()
    return FileResponse(
        published.apk_path,
        media_type="application/vnd.android.package-archive",
        filename=published.file,
        headers={"Cache-Control": "no-cache"},
    )
