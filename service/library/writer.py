"""Atomic filesystem placement.

Prefers os.rename (same-filesystem atomic) with shutil.move fallback for
cross-device links (different Docker volume mounts on same host).
"""
import os
import shutil
from datetime import UTC
from pathlib import Path

_AUDIO_SUFFIXES = frozenset({".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wav"})


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
    """Move path to trash_dir instead of deleting — preserves data on removal.

    Writes a .restore_path sidecar containing the original absolute path so
    the trash recovery UI can offer one-click restore.
    """
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
    # Record original path for restore
    try:
        (dest.parent / f"{path.name}.restore_path").write_text(str(path), encoding="utf-8")
    except Exception:
        pass
    return dest


def trash_empty_album_dir(album_dir: Path, trash_dir: Path) -> None:
    """If album_dir has no audio files left, trash remaining sidecars and rmdir it.

    Called after tracks move out of a directory so ghost directories (with only
    cover.jpg) don't cause Navidrome to show phantom albums.
    """
    if not album_dir.is_dir():
        return
    entries = list(album_dir.iterdir())
    if any(e.suffix.lower() in _AUDIO_SUFFIXES for e in entries):
        return
    for e in entries:
        try:
            safe_trash(e, trash_dir)
        except Exception:
            pass
    try:
        album_dir.rmdir()
    except OSError:
        pass
