"""Canonical album cohesion: anchor new tracks to existing local album grouping.

MusicBrainz release metadata enriches provenance but does NOT dictate filesystem
structure. When a track lands in /music, its album grouping follows whatever local
album already exists — by release-group ID first, then by normalized title match.
This prevents Navidrome album fragmentation caused by remaster editions or minor
MB formatting differences.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from service.core.normalize import normalize, normalize_album_for_grouping

logger = logging.getLogger(__name__)


def _artist_id(name: str) -> str:
    digest = hashlib.sha1(normalize(name).encode()).hexdigest()
    return f"artist:{digest}"


async def apply_album_tags(album: object) -> int:
    """Rewrite album / albumartist / year / canonical MUSICBRAINZ_ALBUMID on every
    track file of ``album`` so Navidrome groups them into one album (fixes splits).

    This is the "Fix file tags" operation — shared by the per-album button and the
    optional daily sweep. Reads ``album.title/year/artist`` and the joined
    ``tracks[].file`` (load those before calling). Returns the number of files
    retagged. Does **not** commit — the caller owns the session/transaction.
    """
    import asyncio
    from collections import Counter

    import mutagen

    from service.library.tagger import write_tags as _write_tags

    title_val = album.title  # type: ignore[attr-defined]
    year_val = album.year  # type: ignore[attr-defined]
    albumartist_val = album.artist.name if album.artist else "Unknown"  # type: ignore[attr-defined]

    # Canonical MUSICBRAINZ_ALBUMID: prefer the DB value, else the majority across the
    # files — an inconsistent album ID is itself a cause of Navidrome splits.
    canonical_mb_release_id = album.musicbrainz_release_id  # type: ignore[attr-defined]
    if not canonical_mb_release_id:
        mb_ids: list[str] = []
        for track in album.tracks:  # type: ignore[attr-defined]
            if track.file:
                fp = Path(track.file.path)
                if fp.exists():
                    try:
                        raw = await asyncio.to_thread(mutagen.File, fp)
                        if raw and raw.tags:
                            for key in ("musicbrainz_albumid", "TXXX:MusicBrainz Album Id",
                                        "----:com.apple.iTunes:MusicBrainz Album Id"):
                                v = raw.tags.get(key)
                                if v:
                                    val_str = v[0] if isinstance(v, list) else str(v)
                                    if hasattr(val_str, "text"):
                                        val_str = str(val_str.text[0]) if val_str.text else ""
                                    if val_str:
                                        mb_ids.append(str(val_str).strip())
                                    break
                    except Exception:
                        pass
        if mb_ids:
            canonical_mb_release_id = Counter(mb_ids).most_common(1)[0][0]

    count = 0
    for track in album.tracks:  # type: ignore[attr-defined]
        if track.file:
            fp = Path(track.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(
                        _write_tags, fp,
                        album=title_val,
                        albumartist=albumartist_val,
                        year=year_val,
                        mb_release_id=canonical_mb_release_id,
                    )
                    count += 1
                except Exception as exc:
                    logger.warning("apply_album_tags: write failed for %s: %s", fp, exc)
    return count


async def find_canonical_album(
    session: AsyncSession,
    album: str | None,
    albumartist: str,
    mb_release_group_id: str | None,
) -> tuple[str, str, int | None, str | None] | None:
    """Find an existing local album to anchor this track to.

    Returns (canonical_album_title, canonical_albumartist, canonical_year, canonical_release_id)
    or None if no match. The year prevents split directories from year metadata drift.
    The release_id is the album's musicbrainz_release_id — callers should use it for the
    new track's tag so all tracks in the album share the same MUSICBRAINZ_ALBUMID (Navidrome
    splits the album into two entries if any track differs).

    Priority:
    1. MB release-group ID — strongest; same release group = same album regardless
       of minor title differences between editions.
    2. Normalized album title within same AlbumArtist — catches cases where
       release group isn't set but the normalized title matches (remaster / deluxe).
    """
    from sqlalchemy import select

    from service.db.schema import Album, Artist

    if not album:
        return None

    # --- Priority 1: MB release group ID ---
    if mb_release_group_id:
        result = await session.execute(
            select(Album, Artist)
            .join(Artist, Album.artist_id == Artist.id)
            .where(Album.mb_release_group_id == mb_release_group_id)
            .order_by(Album.created_at)
            .limit(1)
        )
        row = result.first()
        if row is not None:
            existing_album, existing_artist = row
            if existing_album.title != album or existing_artist.name != albumartist:
                logger.info(
                    "Album cohesion (release group %s): %r / %r → %r / %r",
                    mb_release_group_id[:8],
                    album, albumartist,
                    existing_album.title, existing_artist.name,
                )
            return (existing_album.title, existing_artist.name, existing_album.year,
                    existing_album.musicbrainz_release_id)

    # --- Priority 2: normalized title + same AlbumArtist ---
    # Matches "Metallica" (year=None) to existing "Metallica" (year=1991), and
    # "Abbey Road (Remaster)" to existing "Abbey Road".  Albums WITH a year sort
    # first so we anchor to the better-tagged entry when multiple candidates exist.
    norm_new = normalize_album_for_grouping(album)
    artist_id = _artist_id(albumartist)

    from sqlalchemy import case as _case
    result = await session.execute(
        select(Album)
        .where(Album.artist_id == artist_id)
        .order_by(_case((Album.year.is_not(None), 0), else_=1), Album.created_at)
    )
    for existing in result.scalars().all():
        if normalize_album_for_grouping(existing.title) == norm_new:
            if existing.title != album or existing.year is not None:
                logger.info(
                    "Album cohesion (normalized title): %r → %r (year %s, AlbumArtist: %r)",
                    album, existing.title, existing.year, albumartist,
                )
            return (existing.title, albumartist, existing.year,
                    existing.musicbrainz_release_id)

    return None


async def find_local_release_group(
    session: AsyncSession,
    album: str | None,
    albumartist: str,
) -> str | None:
    """Return the MB release-group ID of an existing local album matching
    ``album`` + ``albumartist`` (normalized title within the same artist), or None.

    Phase 5 retrieval-time cohesion hint: bias MB candidate ranking toward the
    release group the user already owns so remasters / alternate editions don't
    fragment a locally stable album. Unlike :func:`find_canonical_album` this
    takes no MB release-group input — it *discovers* one from the local library.
    """
    if not album:
        return None
    from sqlalchemy import select

    from service.db.schema import Album

    norm_new = normalize_album_for_grouping(album)
    artist_id = _artist_id(albumartist)
    result = await session.execute(
        select(Album)
        .where(Album.artist_id == artist_id, Album.mb_release_group_id.is_not(None))
        .order_by(Album.created_at)
    )
    for existing in result.scalars().all():
        if normalize_album_for_grouping(existing.title) == norm_new:
            return existing.mb_release_group_id
    return None


async def get_owned_recording_ids(
    session: "AsyncSession",
    recording_ids: list[str],
) -> set[str]:
    """Return the subset of recording_ids already present in the local library."""
    if not recording_ids:
        return set()
    from sqlalchemy import select
    from service.db.schema import Track
    rows = (await session.execute(
        select(Track).where(Track.musicbrainz_recording_id.in_(recording_ids))
    )).scalars().all()
    return {r.musicbrainz_recording_id for r in rows if r.musicbrainz_recording_id}


async def merge_albums(
    session: AsyncSession,
    canonical_id: str,
    source_id: str,
    trash_dir: "Path",
    music_dir: "Path",
) -> dict[str, int]:
    """Move ``source`` album's files into the ``canonical`` album and merge DB rows.

    Reusable core shared by the manual merge route and :func:`auto_heal_album_splits`.
    Normalises album / albumartist / year / MUSICBRAINZ_ALBUMID across the moved
    tracks and strips any stray VERSION tag so Navidrome groups them as one album.
    Commits the transaction. Returns counts: moved / already_there / collisions.

    Deliberately does NOT call ``index_file()`` — that would re-read file tags and
    re-group on the source's tags. Instead we update TrackFile.path and
    Track.album_id directly, then re-tag in place.
    """
    import asyncio

    from sqlalchemy import delete as _sa_delete, select
    from sqlalchemy.orm import joinedload as _jl

    from service.db.schema import Album, Track
    from service.library.layout import track_path
    from service.library.tagger import (
        read_mb_release_id,
        strip_album_version_tags,
        write_tags,
    )
    from service.library.writer import atomic_place, trash_empty_album_dir

    canonical = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file), _jl(Album.artist))
        .where(Album.id == canonical_id)
    )).unique().scalar_one_or_none()
    source = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file), _jl(Album.artist))
        .where(Album.id == source_id)
    )).unique().scalar_one_or_none()

    if canonical is None or source is None or canonical_id == source_id:
        return {"moved": 0, "already_there": 0, "collisions": 0}

    # Resolve the canonical album directory from any file that exists on disk.
    canonical_dir: "Path | None" = None
    for t in canonical.tracks:
        if t.file and Path(t.file.path).exists():
            canonical_dir = Path(t.file.path).parent
            break
    if canonical_dir is None:
        canonical_dir = track_path(
            music_dir,
            artist=canonical.artist.name if canonical.artist else "Unknown",
            album=canonical.title,
            year=canonical.year,
            track_number=None, disc_number=None,
            title="placeholder", ext="flac",
            albumartist=canonical.artist.name if canonical.artist else None,
        ).parent
        canonical_dir.mkdir(parents=True, exist_ok=True)

    moved = already_there = collisions = 0

    # Collect source dirs BEFORE mutating track.file.path (the loop rewrites them).
    src_dirs: set[Path] = set()
    for track in source.tracks:
        if track.file:
            d = Path(track.file.path).parent
            if d != canonical_dir:
                src_dirs.add(d)

    for track in source.tracks:
        if not track.file:
            track.album_id = canonical_id
            continue
        src = Path(track.file.path)
        dst = canonical_dir / src.name
        if src == dst:
            already_there += 1
            track.album_id = canonical_id
            continue
        if not src.exists():
            track.file.path = str(dst)
            track.album_id = canonical_id
            continue
        if dst.exists():
            collisions += 1
            logger.info("Merge: name collision at %s — keeping source %s, reassigning album", dst, src)
            track.album_id = canonical_id
            continue
        try:
            atomic_place(src, dst)
            track.file.path = str(dst)
            track.album_id = canonical_id
            moved += 1
        except Exception as exc:
            logger.warning("Merge: failed to move %s → %s: %s", src, dst, exc)

    for src_dir in src_dirs:
        try:
            trash_empty_album_dir(src_dir, trash_dir)
        except Exception:
            pass

    # Determine the canonical release ID (DB first, then read from a canonical file).
    canonical_release_id: str | None = canonical.musicbrainz_release_id
    if not canonical_release_id:
        for t in canonical.tracks:
            if t.file:
                fp = Path(t.file.path)
                if fp.exists():
                    canonical_release_id = read_mb_release_id(fp)
                    if canonical_release_id:
                        break

    # Normalise grouping tags on every moved track + strip any stray VERSION tag.
    canonical_artist_name = canonical.artist.name if canonical.artist else "Unknown"
    for track in source.tracks:
        if track.file:
            fpath = Path(track.file.path)
            if fpath.exists():
                try:
                    await asyncio.to_thread(
                        write_tags, fpath,
                        album=canonical.title,
                        albumartist=canonical_artist_name,
                        year=canonical.year,
                        mb_release_id=canonical_release_id,
                    )
                    await asyncio.to_thread(strip_album_version_tags, fpath)
                except Exception as exc:
                    logger.warning("Merge: tag update failed for %s: %s", fpath.name, exc)

    if canonical_release_id and not canonical.musicbrainz_release_id:
        canonical.musicbrainz_release_id = canonical_release_id

    # Delete the source Album row. Flush the album_id reassignments first, then
    # expunge so SQLAlchemy doesn't auto-NULL the reassigned tracks' FK on delete.
    await session.flush()
    session.expunge(source)
    await session.execute(_sa_delete(Album).where(Album.id == source_id))
    await session.commit()

    logger.info(
        "Merged album %s → %s (moved=%d already=%d collisions=%d)",
        source_id, canonical_id, moved, already_there, collisions,
    )
    return {"moved": moved, "already_there": already_there, "collisions": collisions}


async def auto_heal_album_splits(
    session: AsyncSession,
    album: str,
    albumartist: str,
    trash_dir: "Path",
    music_dir: "Path",
) -> int:
    """Detect and merge a split of one album (same normalized title + artist).

    Run after a discography batch's tracks land in /music. Groups local albums by
    normalized (title, artist); when more than one album row matches, picks the
    canonical (one with a MB release ID, else most tracks) and merges the rest into
    it. Returns the number of source albums merged.
    """
    from sqlalchemy import func, select

    from service.db.schema import Album, Artist, Track

    if not album or not albumartist:
        return 0

    norm_t = normalize_album_for_grouping(album)
    norm_a = normalize(albumartist)

    rows = (await session.execute(
        select(Album.id, Album.title, Album.musicbrainz_release_id,
               Artist.name, func.count(Track.id).label("ntracks"))
        .join(Artist, Artist.id == Album.artist_id)
        .outerjoin(Track, Track.album_id == Album.id)
        .group_by(Album.id, Album.title, Album.musicbrainz_release_id, Artist.name)
    )).all()

    matches = [
        {"id": aid, "has_mbid": bool(mbid), "ntracks": ntracks}
        for aid, title, mbid, artist_name, ntracks in rows
        if normalize_album_for_grouping(title) == norm_t and normalize(artist_name) == norm_a
    ]
    if len(matches) < 2:
        return 0

    # Canonical: prefer one with a MB release ID, then the one with most tracks.
    matches.sort(key=lambda m: (m["has_mbid"], m["ntracks"]), reverse=True)
    canonical_id = matches[0]["id"]

    merged = 0
    for m in matches[1:]:
        result = await merge_albums(
            session, canonical_id, m["id"], trash_dir, music_dir
        )
        if result["moved"] or result["already_there"] or result["collisions"]:
            merged += 1
    if merged:
        logger.info(
            "Auto-heal: merged %d split(s) of %r / %r into %s",
            merged, album, albumartist, canonical_id,
        )
    return merged


async def stable_albumartist(
    session: AsyncSession,
    albumartist: str,
    mb_artist_id: str | None,
) -> str:
    """Return the locally-established AlbumArtist name if the artist is already in the DB.

    Prevents "The Beatles" vs "Beatles, The" fragmentation when MB returns
    a differently-formatted artist name than what's already in the library.
    Matches by MB artist ID (exact), then falls back to the provided name.
    """
    if not mb_artist_id:
        return albumartist

    from sqlalchemy import select

    from service.db.schema import Artist

    result = await session.execute(
        select(Artist).where(Artist.musicbrainz_artist_id == mb_artist_id).limit(1)
    )
    existing = result.scalar_one_or_none()
    if existing is not None and existing.name != albumartist:
        logger.info(
            "AlbumArtist stability (MB artist %s): %r → %r",
            mb_artist_id[:8], albumartist, existing.name,
        )
        return existing.name
    return albumartist
