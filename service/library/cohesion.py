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
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from service.core.normalize import normalize, normalize_album_for_grouping

logger = logging.getLogger(__name__)


def _artist_id(name: str) -> str:
    digest = hashlib.sha1(normalize(name).encode()).hexdigest()
    return f"artist:{digest}"


async def resolve_canonical_release_id(album: object) -> str | None:
    """Canonical MUSICBRAINZ_ALBUMID for an album: the DB value if set, else the
    majority value across the album's files (an inconsistent album ID is itself a
    cause of Navidrome splits). Requires ``tracks[].file`` loaded.
    """
    import asyncio
    from collections import Counter

    from service.library.tagger import read_mb_release_id

    canonical: str | None = album.musicbrainz_release_id  # type: ignore[attr-defined]
    if canonical:
        return canonical

    mb_ids: list[str] = []
    for track in album.tracks:  # type: ignore[attr-defined]
        if track.file:
            fp = Path(track.file.path)
            if fp.exists():
                rid = await asyncio.to_thread(read_mb_release_id, fp)
                if rid:
                    mb_ids.append(rid)
    if mb_ids:
        return Counter(mb_ids).most_common(1)[0][0]
    return None


async def apply_album_tags(album: object) -> int:
    """Rewrite album / albumartist / year / canonical MUSICBRAINZ_ALBUMID on every
    track file of ``album`` so Navidrome groups them into one album (fixes splits).

    This is the "Fix file tags" operation — shared by the per-album button and the
    optional daily sweep. Reads ``album.title/year/artist`` and the joined
    ``tracks[].file`` (load those before calling). Returns the number of files
    retagged. Does **not** commit — the caller owns the session/transaction.
    """
    import asyncio

    from service.library.tagger import write_tags as _write_tags

    title_val = album.title  # type: ignore[attr-defined]
    year_val = album.year  # type: ignore[attr-defined]
    albumartist_val = album.artist.name if album.artist else "Unknown"  # type: ignore[attr-defined]

    canonical_mb_release_id = await resolve_canonical_release_id(album)

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
    Flushes but does **not** commit — the caller owns the session/transaction
    (same convention as :func:`apply_album_tags`) and must commit promptly, since
    the filesystem moves have already happened by the time this returns.
    Returns counts: moved / already_there / collisions.

    Deliberately does NOT call ``index_file()`` — that would re-read file tags and
    re-group on the source's tags. Instead we update TrackFile.path and
    Track.album_id directly, then re-tag in place.
    """
    import asyncio

    from sqlalchemy import delete as _sa_delete, select
    from sqlalchemy.orm import joinedload as _jl

    from service.db.schema import Album, Track
    from service.library.layout import track_path
    from service.library.tagger import strip_album_version_tags, write_tags
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

    # Canonical release ID: DB value, else majority vote across canonical's files
    # (same heuristic as apply_album_tags).
    canonical_release_id = await resolve_canonical_release_id(canonical)

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

    logger.info(
        "Merged album %s → %s (moved=%d already=%d collisions=%d)",
        source_id, canonical_id, moved, already_there, collisions,
    )
    return {"moved": moved, "already_there": already_there, "collisions": collisions}


async def merge_artists(
    session: AsyncSession,
    canonical_id: str,
    source_id: str,
    music_dir: "Path",
) -> dict[str, int] | None:
    """Merge ``source`` artist into ``canonical``: reassign all albums + tracks,
    rewrite artist/albumartist tags, and move files to the canonical layout path.

    Counterpart of :func:`merge_albums` (the manual artist-merge route used to
    inline all of this in the web layer). Returns ``None`` when either artist
    doesn't exist, else counts: retagged / moved. Flushes but does **not**
    commit — the caller owns the session/transaction and must commit promptly,
    since the filesystem moves have already happened by the time this returns.
    """
    import asyncio
    import shutil

    from sqlalchemy import delete as _sa_delete, select
    from sqlalchemy.orm import joinedload as _jl

    from service.db.schema import Artist, Track, TrackFile
    from service.library.layout import track_path
    from service.library.tagger import write_tags

    canonical = (await session.execute(
        select(Artist)
        .options(_jl(Artist.albums), _jl(Artist.tracks).joinedload(Track.file))
        .where(Artist.id == canonical_id)
    )).unique().scalar_one_or_none()
    source = (await session.execute(
        select(Artist)
        .options(_jl(Artist.albums), _jl(Artist.tracks).joinedload(Track.file))
        .where(Artist.id == source_id)
    )).unique().scalar_one_or_none()

    if canonical is None or source is None:
        return None

    canonical_name = canonical.name

    # Carry over MB artist ID / sort name if canonical lacks them.
    if not canonical.musicbrainz_artist_id and source.musicbrainz_artist_id:
        canonical.musicbrainz_artist_id = source.musicbrainz_artist_id
    if not canonical.sort_name and source.sort_name:
        canonical.sort_name = source.sort_name

    # Reassign albums
    for album in source.albums:
        album.artist_id = canonical_id

    # Reassign tracks and collect files to retag; plan path moves but don't update
    # DB yet. Compute each destination with the canonical library layout
    # (track_path) so files filed under the album-artist dir, /music/Singles/<artist>/,
    # AND /music/Compilations/ all relocate to the merged artist. A plain prefix
    # check on /music/<source_name>/ would strand singles & comps (e.g. a duet
    # single left in /music/Singles/<collab>/ surfacing as "[Unknown Album]").
    albums_by_id = {alb.id: alb for alb in source.albums}
    files_to_retag: list[Path] = []
    planned_moves: list[tuple[Path, Path, "TrackFile"]] = []  # (old, new, orm_row)
    for track in source.tracks:
        track.artist_id = canonical_id
        if not track.file:
            continue
        fp = Path(track.file.path)
        if not fp.exists():
            continue
        files_to_retag.append(fp)
        alb = albums_by_id.get(track.album_id) if track.album_id else None
        new_fp = track_path(
            music_dir,
            artist=canonical_name,
            album=(alb.title if alb else None),
            year=(alb.year if alb else None),
            track_number=track.track_number,
            disc_number=track.disc_number,
            title=track.title,
            ext=fp.suffix,
            albumartist=canonical_name,
        )
        if new_fp != fp:
            planned_moves.append((fp, new_fp, track.file))

    # Rewrite albumartist (and artist) tags so Navidrome groups under canonical name.
    # The MB artist IDs go with them: Navidrome keys artist identity on the MBID
    # as much as on the name, so files left holding the source artist's ID would
    # re-split into the artist this merge just dissolved.
    retagged = 0
    canonical_mbid = canonical.musicbrainz_artist_id
    for fp in files_to_retag:
        try:
            await asyncio.to_thread(
                write_tags, fp,
                artist=canonical_name, albumartist=canonical_name,
                mb_artist_id=canonical_mbid, mb_albumartist_id=canonical_mbid,
            )
            retagged += 1
        except Exception as exc:
            logger.warning("merge_artists: tag write failed for %s: %s", fp, exc)

    # Move files to their canonical location; update DB paths only for files that
    # actually moved so the DB stays consistent with the filesystem. Remember each
    # source dir → dest dir so we can carry sidecars and prune emptied source dirs.
    moved = 0
    dir_moves: dict[Path, Path] = {}
    for old_fp, new_fp, tf_row in planned_moves:
        try:
            new_fp.parent.mkdir(parents=True, exist_ok=True)
            if not new_fp.exists():
                shutil.move(str(old_fp), str(new_fp))
            tf_row.path = str(new_fp)
            moved += 1
            if old_fp.parent != new_fp.parent:
                dir_moves.setdefault(old_fp.parent, new_fp.parent)
        except Exception as exc:
            logger.warning("merge_artists: could not move %s → %s: %s", old_fp, new_fp, exc)

    # Carry over sidecars (cover.jpg, artist.jpg, …) from each emptied source dir,
    # then prune now-empty source directories upward (stops at /music).
    for old_dir, new_dir in dir_moves.items():
        try:
            if old_dir.exists():
                for item in sorted(old_dir.iterdir()):
                    if item.is_file():
                        dest_item = new_dir / item.name
                        dest_item.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_item.exists():
                            shutil.move(str(item), str(dest_item))
            d = old_dir
            while d != music_dir and d.exists() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        except Exception as exc:
            logger.warning("merge_artists: source dir cleanup failed for %s: %s", old_dir, exc)

    # Delete the source Artist row. Flush the reassignments first, then expunge
    # so SQLAlchemy doesn't auto-NULL the reassigned rows' FK on delete.
    await session.flush()
    session.expunge(source)
    await session.execute(_sa_delete(Artist).where(Artist.id == source_id))

    logger.info(
        "Merged artist %s → %s (retagged=%d moved=%d)",
        source_id, canonical_id, retagged, moved,
    )
    return {"retagged": retagged, "moved": moved}


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
    it. Returns the number of source albums merged. Does **not** commit — the
    caller owns the session/transaction and must commit promptly (files have
    already moved on disk).
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


class AlbumArtistDecision(NamedTuple):
    """What ALBUMARTIST a file already in the library should carry.

    ``locked`` says the name came from an existing album grouping rather than
    from the recording's performer, so nothing downstream (a review-form artist
    edit, a manual MB recording pick) may re-derive it — see
    ``albumartist_for_existing_file``.
    """

    name: str
    mb_artist_id: str | None
    is_compilation: bool
    locked: bool


async def albumartist_for_existing_file(
    session: AsyncSession,
    file_path: Path,
    *,
    performer: str,
    performer_mb_id: str | None = None,
) -> AlbumArtistDecision:
    """Pick ALBUMARTIST for a file that is ALREADY in /music (enrichment).

    Acquisition chooses the album artist for a file it is about to place, and
    moves the file to match. Enrichment can do neither: the file stays in the
    folder it is in, so which album it belongs to is a fact, not a choice — and
    ALBUMARTIST is what Navidrome groups that album by. Writing the recording's
    performer there, which is all a per-track MB match knows, splits a
    compilation into one album per track.

    Rules, in order:

    1. No ALBUM tag — a single, nothing to group. The performer wins and stays
       re-derivable (``locked=False``), because for a single ALBUMARTIST is just
       the artist under which the file is filed.
    2. The file already carries an ALBUMARTIST — keep it verbatim, spelling and
       all. It is what the album's other files say, and Navidrome compares the
       literal string.
    3. ALBUMARTIST is implicit (Navidrome falls back to ARTIST) — read it off the
       other indexed tracks in the same folder: Various Artists when at least
       half of those name a different performer (``is_various_artists_release``'s
       threshold, over at least three of them — too few and a guest spot decides
       it), the dominant name otherwise.

    The returned MBID always describes the returned name — never the performer's
    when the album artist is someone else.
    """
    import asyncio
    from collections import Counter

    from sqlalchemy import select

    from service.db.schema import Artist, Track, TrackFile
    from service.library.tagger import read_tags
    from service.metadata.musicbrainz import VARIOUS_ARTISTS_NAME

    free = AlbumArtistDecision(performer, performer_mb_id, False, False)

    if not file_path.exists():
        return free
    tags = await asyncio.to_thread(read_tags, file_path)
    if tags is None or not tags.album:
        return free

    existing = (tags.albumartist or "").strip()
    if existing:
        return await _anchored_albumartist(session, existing, performer, performer_mb_id)

    rows = (await session.execute(
        select(Track.artist_credit, Artist.name, TrackFile.path)
        .join(TrackFile, TrackFile.track_id == Track.id)
        .join(Artist, Artist.id == Track.artist_id)
        .where(TrackFile.path.startswith(f"{file_path.parent}/", autoescape=True))
    )).all()
    siblings = [
        (credit or name or "").strip()
        for credit, name, path in rows
        if Path(path) != file_path and (credit or name)
    ]
    if not siblings:
        return free

    # The verdict is about the folder, so it is read off the OTHER files: this
    # file's own performer is the very thing being second-guessed, and MB may
    # well spell it differently than the album it sits in does.
    counts = Counter(normalize(p) for p in siblings)
    dominant_key = max(
        counts, key=lambda k: (sum(n for k2, n in counts.items() if _credit_covers(k, k2)), -len(k))
    )
    shared = sum(n for k, n in counts.items() if _credit_covers(dominant_key, k))
    if len(siblings) >= 3 and (len(siblings) - shared) / len(siblings) >= 0.5:
        return await _anchored_albumartist(
            session, VARIOUS_ARTISTS_NAME, performer, performer_mb_id
        )

    # The folder's own spelling, not MusicBrainz' — the unenriched siblings still
    # group under theirs, and one file renaming the album artist is a split.
    dominant = Counter(
        p for p in siblings if _credit_covers(dominant_key, normalize(p))
    ).most_common(1)[0][0]
    return await _anchored_albumartist(session, dominant, performer, performer_mb_id)


def _credit_covers(head: str, credit: str) -> bool:
    """Is `credit` the act `head`, possibly with a guest? (both normalized)

    "bjørn eidsvåg med lisa nilsson" is still Bjørn Eidsvåg's track, the same
    reading `is_various_artists_release` takes when MB gives it no IDs to compare.
    The character after the prefix must be non-alphanumeric so "the be" doesn't
    swallow "the beatles".
    """
    if not head or not credit.startswith(head):
        return False
    return len(credit) == len(head) or not credit[len(head)].isalnum()


async def _anchored_albumartist(
    session: AsyncSession,
    name: str,
    performer: str,
    performer_mb_id: str | None,
) -> AlbumArtistDecision:
    """Wrap an album-artist name settled from existing grouping in a decision.

    The MBID has to describe `name`: the performer's own only when they are the
    same artist, otherwise whatever the local Artist row for that name carries
    (often nothing — an absent ID is always safer than a wrong one).
    """
    from service.db.schema import Artist

    if normalize(name) == normalize(performer):
        mb_id = performer_mb_id
    else:
        row = await session.get(Artist, _artist_id(name))
        mb_id = row.musicbrainz_artist_id if row is not None else None
    return AlbumArtistDecision(
        name=name,
        mb_artist_id=mb_id,
        is_compilation=normalize(name) in ("various artists", "various"),
        locked=True,
    )
