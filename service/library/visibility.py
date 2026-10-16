"""Shared vs private library: which of the two roots a file belongs under.

Navidrome has no per-user artist filter, only per-user *library* access, so an
artist one household member shouldn't see lives in ``/music/<private_subdir>/``.
A ``.ndignore`` keeps Navidrome's main library from listing that folder, and
Navidrome mounts it a second time as its own library, granted only to its owner.
audioreap doesn't automate any of that — Navidrome stays vanilla — it only
decides which root a file lands under, and moves an artist between them.

Rules (see docs/ROADMAP.md, "Multi-user visibility"):

* **The filesystem is the rule.** An album artist is private when
  ``<private_root>/<AlbumArtist>/`` (or ``<private_root>/Singles/<Artist>/``)
  exists. There is no artist list in Settings to drift from what Navidrome
  actually shows the other users.
* **Shared is the default**; private is opt-in, chosen on the review card.
* **Compilations stay shared** — a folder can be in only one library.
* **An album never straddles both roots.** A track joining an album already
  on disk goes wherever that album is.

``layout.track_path`` stays a pure template: callers resolve the root here and
pass it in place of ``music_dir``.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from service.config import settings
from service.library.layout import _VARIOUS_ARTISTS, _safe

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Top-level names the layout already owns. A private root by one of these names
# would mix the two libraries in a single folder.
_RESERVED = frozenset({"singles", "compilations"})

PRIVATE = "private"
SHARED = "shared"


def private_subdir() -> str | None:
    """The validated private folder name, or None when the feature is off.

    Must be one plain path component that does not start with ``.`` — the
    scanner skips dot-directories, so the files would drop out of the index.
    """
    raw = str(getattr(settings, "private_subdir", "") or "").strip().strip("/")
    if not raw or raw.startswith(".") or "/" in raw or "\\" in raw:
        return None
    if raw.lower() in _RESERVED or _safe(raw) != raw:
        return None
    return raw


def private_root(music_dir: Path | None = None) -> Path | None:
    sub = private_subdir()
    if sub is None:
        return None
    return (music_dir or settings.music_dir) / sub


def enabled() -> bool:
    return private_subdir() is not None


def library_root(private: bool, music_dir: Path | None = None) -> Path:
    """The root to hand ``track_path``: the private one only when asked for and on."""
    base = music_dir or settings.music_dir
    root = private_root(base) if private else None
    return root or base


def is_private_path(path: Path, music_dir: Path | None = None) -> bool:
    root = private_root(music_dir)
    if root is None:
        return False
    try:
        Path(path).relative_to(root)
    except ValueError:
        return False
    return True


def root_of(path: Path, music_dir: Path | None = None) -> Path:
    """The library root an existing file lives under — moves keep it there."""
    return library_root(is_private_path(path, music_dir), music_dir)


def is_compilation_artist(albumartist: str | None) -> bool:
    return (albumartist or "").strip().lower() in _VARIOUS_ARTISTS


def artist_is_private(name: str | None, music_dir: Path | None = None) -> bool:
    """The filesystem rule: does this album artist already have a private folder?"""
    root = private_root(music_dir)
    if root is None or not name or is_compilation_artist(name):
        return False
    safe = _safe(name)
    return (root / safe).is_dir() or (root / "Singles" / safe).is_dir()


def artist_dir(name: str, music_dir: Path | None = None) -> Path:
    """The folder that holds this artist's albums (and artist.jpg)."""
    return library_root(artist_is_private(name, music_dir), music_dir) / _safe(name)


async def existing_album_is_private(
    session: AsyncSession, album: str | None, albumartist: str | None,
    music_dir: Path | None = None,
) -> bool | None:
    """Where an album already on disk lives: True/False, or None when it has no
    file on disk (a new album, or one whose files are all gone).

    Matches the canonical names that album cohesion settled on, so call it
    after cohesion rather than with the staged names.
    """
    if not album or not albumartist or not enabled():
        return None
    from sqlalchemy import select

    from service.db.schema import Album, Track, TrackFile
    from service.library.cohesion import _artist_id

    paths = (await session.execute(
        select(TrackFile.path)
        .join(Track, TrackFile.track_id == Track.id)
        .join(Album, Track.album_id == Album.id)
        .where(Album.artist_id == _artist_id(albumartist), Album.title == album)
    )).scalars().all()
    for p in paths:
        if Path(p).exists():
            return is_private_path(Path(p), music_dir)
    return None


async def resolve_placement(
    session: AsyncSession,
    *,
    album: str | None,
    albumartist: str | None,
    override: str | None,
    music_dir: Path | None = None,
) -> tuple[bool, str]:
    """Decide which library a track lands in. Returns (private, reason).

    ``reason`` is what the review card says about the decision: "off",
    "compilation", "existing_album" (fixed — the album is already on disk),
    "chosen" (the review card's override), or "artist_folder" / "default".
    Only "chosen", "artist_folder" and "default" can be changed by the user.
    """
    if not enabled():
        return False, "off"
    if is_compilation_artist(albumartist):
        return False, "compilation"
    if album:
        existing = await existing_album_is_private(session, album, albumartist, music_dir)
        if existing is not None:
            return existing, "existing_album"
    if override in (PRIVATE, SHARED):
        return override == PRIVATE, "chosen"
    if artist_is_private(albumartist, music_dir):
        return True, "artist_folder"
    return False, "default"


# ── Moving an artist between the two libraries ─────────────────────────────


def artist_location(name: str, music_dir: Path | None = None) -> str:
    """"shared", "private", "both", or "none" — by which roots hold its folders."""
    base = music_dir or settings.music_dir
    root = private_root(base)
    safe = _safe(name)
    shared = (base / safe).is_dir() or (base / "Singles" / safe).is_dir()
    private = root is not None and (
        (root / safe).is_dir() or (root / "Singles" / safe).is_dir()
    )
    if shared and private:
        return "both"
    if private:
        return PRIVATE
    if shared:
        return SHARED
    return "none"


def _merge_tree(src: Path, dst: Path) -> tuple[int, int]:
    """Move everything under ``src`` into ``dst``, never overwriting. Returns
    (moved, collisions); a colliding file stays where it is, in ``src``."""
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)  # same filesystem: /music/<sub> is inside /music
        return sum(len(files) for _, _, files in os.walk(dst)), 0
    moved = collisions = 0
    for item in sorted(src.iterdir()):
        target = dst / item.name
        if item.is_dir():
            m, c = _merge_tree(item, target)
            moved += m
            collisions += c
        elif target.exists():
            collisions += 1
            logger.info("Visibility move: %s already exists — leaving %s", target, item)
        else:
            shutil.move(str(item), str(target))
            moved += 1
    try:
        src.rmdir()
    except OSError:
        pass  # collisions left behind
    return moved, collisions


async def move_artist(
    session: AsyncSession,
    name: str,
    *,
    private: bool,
    music_dir: Path | None = None,
) -> dict[str, int]:
    """Move an artist's folders to the private or shared library.

    Moves ``<root>/<Artist>/`` and ``<root>/Singles/<Artist>/`` whole — sidecars
    (cover.jpg, artist.jpg, .lrc) travel with them — then rewrites the index
    paths under them. Track identity is content-based, so nothing else in the
    index changes; Navidrome recognises the files as moved and keeps their
    plays, stars and playlist entries. Compilations are not touched: they stay
    in the shared library. Flushes but does **not** commit — the caller owns
    the transaction and must commit promptly, the files have already moved.
    """
    from sqlalchemy import select

    from service.db.schema import TrackFile

    base = music_dir or settings.music_dir
    root = private_root(base)
    if root is None:
        raise ValueError("No private library is configured")
    src_root, dst_root = (base, root) if private else (root, base)
    safe = _safe(name)

    moved = collisions = reindexed = 0
    for rel in (Path(safe), Path("Singles") / safe):
        src, dst = src_root / rel, dst_root / rel
        if not src.is_dir():
            continue
        m, c = _merge_tree(src, dst)
        moved += m
        collisions += c

        rows = (await session.execute(
            select(TrackFile).where(TrackFile.path.startswith(f"{src}/", autoescape=True))
        )).scalars().all()
        for tf in rows:
            old = Path(tf.path)
            new = dst / old.relative_to(src)
            if new.exists() and not old.exists():
                tf.path = str(new)
                reindexed += 1

        parent = src.parent
        while parent not in (src_root, base) and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent

    await session.flush()
    logger.info(
        "Moved artist %r to the %s library (files=%d collisions=%d reindexed=%d)",
        name, PRIVATE if private else SHARED, moved, collisions, reindexed,
    )
    return {"moved": moved, "collisions": collisions, "reindexed": reindexed}
