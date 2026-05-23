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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from service.core.normalize import normalize, normalize_album_for_grouping

logger = logging.getLogger(__name__)


def _artist_id(name: str) -> str:
    digest = hashlib.sha1(normalize(name).encode()).hexdigest()
    return f"artist:{digest}"


async def find_canonical_album(
    session: AsyncSession,
    album: str | None,
    albumartist: str,
    mb_release_group_id: str | None,
) -> tuple[str, str, int | None] | None:
    """Find an existing local album to anchor this track to.

    Returns (canonical_album_title, canonical_albumartist, canonical_year) or None if no match.
    The year is included so tracks land in the same filesystem directory as existing ones —
    preventing split album directories caused by year metadata drift between editions.

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
            return (existing_album.title, existing_artist.name, existing_album.year)

    # --- Priority 2: normalized title + same AlbumArtist ---
    norm_new = normalize_album_for_grouping(album)
    artist_id = _artist_id(albumartist)

    result = await session.execute(
        select(Album)
        .where(Album.artist_id == artist_id)
        .order_by(Album.created_at)
    )
    for existing in result.scalars().all():
        if normalize_album_for_grouping(existing.title) == norm_new and existing.title != album:
            logger.info(
                "Album cohesion (normalized title): %r → %r (AlbumArtist: %r)",
                album, existing.title, albumartist,
            )
            return (existing.title, albumartist, existing.year)

    return None


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
