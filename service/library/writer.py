"""Atomic filesystem placement.

All writes go through /tmp-acquire (same volume as /music) then os.rename,
so no partial files are ever visible in the library.
"""
import os
from datetime import UTC
from pathlib import Path


def atomic_place(src: Path, dest: Path) -> Path:
    """Move src → dest atomically via os.rename.

    Requires src and dest to be on the same filesystem — guaranteed when
    /tmp-acquire is a bind mount on the same physical volume as /music.
    Creates dest parent directories as needed.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)
    return dest


def safe_trash(path: Path, trash_dir: Path) -> Path:
    """Move path to trash_dir instead of deleting — preserves data on removal."""
    from datetime import datetime

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    dest = trash_dir / ts / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(path, dest)
    return dest
