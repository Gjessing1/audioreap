"""Atomic filesystem placement.

Prefers os.rename (same-filesystem atomic) with shutil.move fallback for
cross-device links (different Docker volume mounts on same host).
"""
import os
import shutil
from datetime import UTC
from pathlib import Path


def atomic_place(src: Path, dest: Path) -> Path:
    """Move src → dest, atomically when possible.

    Falls back to shutil.move (copy + delete) when src and dest are on
    different filesystems — handles Docker setups where /tmp-acquire and
    /music are separate bind mounts.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError as exc:
        if exc.errno != 18:  # EXDEV: cross-device link
            raise
        shutil.move(str(src), dest)
    return dest


def safe_trash(path: Path, trash_dir: Path) -> Path:
    """Move path to trash_dir instead of deleting — preserves data on removal."""
    from datetime import datetime

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    dest = trash_dir / ts / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(path, dest)
    except OSError as exc:
        if exc.errno != 18:
            raise
        shutil.move(str(path), dest)
    return dest
