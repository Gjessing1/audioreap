"""Per-track operations: tags, art, lyrics, replacement, suppression, streaming."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from service.config import settings
from service.core.models import TrackCandidate
from service.db.schema import Album, Artist, DeletedTrack, Track
from service.library.writer import safe_trash
from service.db.session import get_session
from service.library.writer import trash_empty_album_dir as _trash_empty_album_dir
from service.providers.ytdlp import explicit_score as _explicit_score

from service.api.routes.artwork import _fetch_user_art
from service.api.shared import _do_scans, _error_badge, _get_track_with_file, _mb_recording_search, _resize_cover, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/library/tracks")


@router.delete("/{internal_id}", response_class=HTMLResponse)
async def delete_track(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _joinedload
    stmt = (
        select(Track)
        .options(_joinedload(Track.file), _joinedload(Track.artist))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        return HTMLResponse("")

    if row.file:
        file_path = Path(row.file.path)
        album_dir = file_path.parent
        if file_path.exists():
            try:
                safe_trash(file_path, settings.music_dir / ".trash")
            except Exception as exc:
                logger.warning("Trash move failed for %s: %s", file_path, exc)
        _trash_empty_album_dir(album_dir, settings.music_dir / ".trash")
        await session.delete(row.file)

    from datetime import UTC as _UTC, datetime as _dt
    tombstone = DeletedTrack(
        mb_recording_id=row.musicbrainz_recording_id,
        track_title=row.title,
        track_artist=row.artist.name if row.artist else None,
        deleted_at=_dt.now(_UTC).replace(tzinfo=None),
    )
    album_id_was = row.album_id
    artist_id_was = row.artist_id
    session.add(tombstone)
    await session.delete(row)
    await session.flush()

    # Inline orphan cleanup so the browse UI reflects changes immediately
    if album_id_was:
        remaining = (await session.execute(
            select(func.count(Track.id)).where(Track.album_id == album_id_was)
        )).scalar_one()
        if remaining == 0:
            await session.execute(sa_delete(Album).where(Album.id == album_id_was))
    remaining_artist = (await session.execute(
        select(func.count(Track.id)).where(Track.artist_id == artist_id_was)
    )).scalar_one()
    if remaining_artist == 0:
        await session.execute(sa_delete(Artist).where(Artist.id == artist_id_was))

    await session.commit()
    await _do_scans()
    return HTMLResponse("")


@router.post("/{track_id}/move-to-album/{album_id}", response_class=HTMLResponse)
async def move_track_to_album(
    request: Request,
    track_id: str,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Move a misplaced track's file into this album's folder and fix its tags."""
    from service.library.tagger import write_tags as _wt
    from service.library.writer import atomic_place as _ap
    from service.library.layout import track_path as _tp

    track = (await session.execute(
        select(Track).options(joinedload(Track.file), joinedload(Track.album))
        .where(Track.id == track_id)
    )).unique().scalar_one_or_none()

    target_album = (await session.execute(
        select(Album).options(joinedload(Album.artist), joinedload(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()

    if track is None or target_album is None or track.file is None:
        raise HTTPException(404)

    src = Path(track.file.path)
    if not src.exists():
        return _error_badge("File not found on disk", level="fail")

    artist_name = target_album.artist.name if target_album.artist else "Unknown"
    ext = src.suffix.lstrip(".")
    dst = _tp(
        settings.music_dir,
        artist=artist_name,
        album=target_album.title,
        year=target_album.year,
        track_number=track.track_number,
        disc_number=track.disc_number,
        title=track.title,
        ext=ext,
        albumartist=artist_name,
    )

    if dst == src:
        return HTMLResponse('<span style="color:var(--success);font-size:12px">Already in place</span>')

    if dst.exists():
        return _error_badge(f"Collision: {dst.name} already exists in target", level="fail")

    # Fix tags on the file
    try:
        canonical_release_id: str | None = target_album.musicbrainz_release_id
        await asyncio.to_thread(
            _wt, src,
            album=target_album.title,
            year=target_album.year,
            albumartist=artist_name,
            track_number=track.track_number,
            mb_release_id=canonical_release_id,
        )
    except Exception as exc:
        logger.warning("move_track_to_album: tag write failed for %s: %s", src, exc)

    old_dir = src.parent
    await asyncio.to_thread(_ap, src, dst)

    # Update DB
    track.file.path = str(dst)
    track.album_id = album_id
    await session.commit()

    # Clean up old dir if empty
    _trash_empty_album_dir(old_dir, settings.music_dir / ".trash")

    await _do_scans()

    return HTMLResponse(f'<span style="color:var(--success);font-size:12px">✓ Moved to {target_album.title}</span>')


@router.get("/{internal_id}/cover-art")
async def track_cover_art(
    internal_id: str,
    size: int | None = Query(None, ge=32, le=512),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return cover art for a track: embedded first, then sidecar cover.jpg.

    ?size=N serves a disk-cached thumbnail (max-width N px) instead of the
    full embedded art — list rows and any future grid view must use it so a
    screenful of cells doesn't re-download full-size art per track. The cache
    entry is regenerated whenever the audio file or sidecar is newer than it;
    browsers revalidate after 10 min so replaced art propagates same-session.
    """
    from fastapi.responses import Response as Resp
    from service.library.tagger import read_cover_art_bytes

    stmt = (
        select(Track)
        .options(joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)
    path = Path(row.file.path)
    if not path.exists():
        raise HTTPException(404)

    thumb_headers = {"Cache-Control": "public, max-age=600"}
    thumb_path: Path | None = None
    if size is not None:
        thumb_path = settings.cache_dir / "thumbs" / f"{internal_id}_{size}.jpg"
        cover_jpg = path.parent / "cover.jpg"
        src_mtime = path.stat().st_mtime
        if cover_jpg.exists():
            src_mtime = max(src_mtime, cover_jpg.stat().st_mtime)
        if thumb_path.exists() and thumb_path.stat().st_mtime >= src_mtime:
            data = await asyncio.to_thread(thumb_path.read_bytes)
            return Resp(content=data, media_type="image/jpeg", headers=thumb_headers)

    art = await asyncio.to_thread(read_cover_art_bytes, path)

    if not art:
        # Fall back to sidecar cover.jpg in the same directory
        cover_jpg = path.parent / "cover.jpg"
        if cover_jpg.exists():
            art = await asyncio.to_thread(cover_jpg.read_bytes)

    if not art:
        raise HTTPException(404)

    if thumb_path is not None:
        data = await asyncio.to_thread(_resize_cover, art, size, thumb_path)
        if data:
            return Resp(content=data, media_type="image/jpeg", headers=thumb_headers)
        # resize failed — fall through to full-size art

    return Resp(content=art, media_type="image/jpeg",
                headers={"Cache-Control": "no-cache"})


async def _suppression_response(
    request: Request,
    session: AsyncSession,
    row: Track,
    *,
    from_health: bool,
    from_edit: bool,
) -> HTMLResponse:
    """Render the appropriate partial after a (un)suppress toggle.

    from_health → empty (row drops out of the health list); from_edit → re-render
    the edit card so the user stays in it; otherwise the browse row.
    """
    if from_health:
        return HTMLResponse("")
    if from_edit:
        ctx = await _edit_card_ctx(session, row)
        return templates.TemplateResponse(request, "partials/track_edit_card.html", ctx)
    return templates.TemplateResponse(request, "partials/browse_row.html", {"t": row})


@router.post("/{internal_id}/suppress-quality", response_class=HTMLResponse)
async def track_suppress_quality(
    request: Request,
    internal_id: str,
    from_health: bool = Query(False),
    from_edit: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Mark a track's quality warning as suppressed so it no longer appears in the low-quality filter."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.quality_suppressed = True
    await session.commit()
    return await _suppression_response(request, session, row, from_health=from_health, from_edit=from_edit)


@router.post("/{internal_id}/suppress-bitrate", response_class=HTMLResponse)
async def track_suppress_bitrate(
    request: Request,
    internal_id: str,
    from_health: bool = Query(False),
    from_edit: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Mark a track's bitrate warning as suppressed so it no longer appears in the low-bitrate filter."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.bitrate_suppressed = True
    await session.commit()
    return await _suppression_response(request, session, row, from_health=from_health, from_edit=from_edit)


@router.post("/{internal_id}/unsuppress-bitrate", response_class=HTMLResponse)
async def track_unsuppress_bitrate(
    request: Request,
    internal_id: str,
    from_edit: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Remove bitrate suppression for a track."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.bitrate_suppressed = False
    await session.commit()
    return await _suppression_response(request, session, row, from_health=False, from_edit=from_edit)


@router.post("/{internal_id}/unsuppress-quality", response_class=HTMLResponse)
async def track_unsuppress_quality(
    request: Request,
    internal_id: str,
    from_edit: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Remove quality suppression for a track."""
    row = (await session.execute(
        select(Track).options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    row.quality_suppressed = False
    await session.commit()
    return await _suppression_response(request, session, row, from_health=False, from_edit=from_edit)


@router.get("/{internal_id}/browse-row", response_class=HTMLResponse)
async def track_browse_row(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return a single browse-list row for a track (used by edit-card Cancel button)."""
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": row},
    )


async def _edit_card_ctx(
    session: AsyncSession,
    row: Track,
    *,
    source_album_id: str = "",
    open_art: bool = False,
) -> dict:
    """Build the template context for track_edit_card.html.

    Shared by the edit-card route and the suppression handlers (which re-render
    the edit card so quality/bitrate-OK toggles keep the user in the card).
    """
    from sqlalchemy import distinct as _distinct

    # Use genre stored in DB (populated by scanner and save-tags); fall back to file.
    genre: str | None = row.genre
    if not genre and row.file:
        from service.library.tagger import read_tags as _read_tags
        fp = Path(row.file.path)
        if fp.exists():
            tagged = await asyncio.to_thread(_read_tags, fp)
            if tagged:
                genre = tagged.genre
    # Autocomplete datalists
    genre_rows = (await session.execute(
        select(_distinct(Track.genre)).where(Track.genre.isnot(None)).order_by(Track.genre)
    )).scalars().all()
    genres = [g for g in genre_rows if g]
    artist_names = (await session.execute(
        select(Artist.name).order_by(Artist.name)
    )).scalars().all()
    album_names = (await session.execute(
        select(Album.title)
        .where(Album.artist_id == row.artist_id)
        .order_by(Album.title)
    )).scalars().all()
    # Lyrics sidecar status for the badge (cheap: two stats + a small read)
    lyrics_status: str | None = None
    if row.file:
        from service.metadata.lyrics import has_lyrics_sidecar, sidecar_is_synced
        fp = Path(row.file.path)
        if has_lyrics_sidecar(fp):
            lyrics_status = "synced" if sidecar_is_synced(fp) else "plain"

    return {
        "track": row,
        "genre": genre,
        "genres": list(genres),
        "artist_names": list(artist_names),
        "album_names": list(album_names),
        "provider_ref": row.file.provider_ref if row.file else None,
        "bitrate_kbps": row.file.bitrate_kbps if row.file else None,
        "min_bitrate_kbps": settings.min_bitrate_kbps,
        "source_album_id": source_album_id,
        "open_art": open_art,
        "lyrics_status": lyrics_status,
    }


@router.get("/{internal_id}/edit-card", response_class=HTMLResponse)
async def track_edit_card(
    request: Request,
    internal_id: str,
    album_id: str = Query(""),
    open_art: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    ctx = await _edit_card_ctx(session, row, source_album_id=album_id, open_art=open_art)
    return templates.TemplateResponse(request, "partials/track_edit_card.html", ctx)


@router.post("/{internal_id}/save-tags", response_class=HTMLResponse)
async def save_track_tags(
    request: Request,
    internal_id: str,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    year: str = Form(""),
    track_number: str = Form(""),
    mb_recording_id: str = Form(""),
    genre: str = Form(""),
    source_album_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.library.tagger import write_tags as _write_tags, has_cover_art as _has_cover_art
    from service.metadata.quality import compute_quality_score
    from service.index.scanner import _upsert_artist, _upsert_album

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")

    # Parse inputs
    year_val: int | None = int(year) if year.strip().isdigit() else None
    track_num_val: int | None = int(track_number) if track_number.strip().isdigit() else None
    title_val = title.strip() or row.title
    artist_val = artist.strip() or row.artist.name
    album_val = album.strip() or (row.album.title if row.album else None)
    mbid_val = mb_recording_id.strip() or None
    genre_val = genre.strip() or None

    # Write tags to file
    try:
        await asyncio.to_thread(
            _write_tags,
            file_path,
            title=title_val,
            artist=artist_val,
            albumartist=artist_val,
            album=album_val,
            year=year_val,
            track_number=track_num_val,
            mb_recording_id=mbid_val,
            genre=genre_val,
        )
    except Exception as exc:
        # Do NOT update the DB when the file write failed — a partial "save" would
        # leave DB and on-disk tags silently disagreeing from then on.
        logger.warning("save-tags write failed for %s: %s", file_path, exc)
        import html as _html
        return HTMLResponse(
            f'<div style="color:var(--danger);font-size:12px;padding:6px 0">'
            f'✗ Tag write failed — nothing was saved: {_html.escape(str(exc))}</div>'
        )

    # Update DB — update existing rows in-place to avoid hash ID churn
    row.title = title_val
    row.track_number = track_num_val
    row.musicbrainz_recording_id = mbid_val
    row.genre = genre_val

    old_artist_id: str | None = None
    old_album_id: str | None = row.album_id
    if artist_val != row.artist.name:
        old_artist_id = row.artist_id
        new_artist_id = await _upsert_artist(session, artist_val)
        row.artist_id = new_artist_id

    if album_val:
        # Re-upsert album whenever artist OR album title changed so the album
        # stays associated with the correct artist.
        artist_changed = old_artist_id is not None
        album_changed = not row.album or album_val != row.album.title
        if artist_changed or album_changed:
            artist_id_for_album = row.artist_id
            new_album_id = await _upsert_album(session, artist_id_for_album, album_val, year_val, artist_val)
            row.album_id = new_album_id
    elif not album_val:
        row.album_id = None

    hca = await asyncio.to_thread(_has_cover_art, file_path)
    if row.file:
        row.file.has_cover_art = hca
    row.tag_quality_score = compute_quality_score(
        title=title_val, artist=artist_val, album=album_val, year=year_val,
        track_number=track_num_val, musicbrainz_recording_id=mbid_val, has_cover_art=hca,
    )
    await session.commit()

    # Prune the old album if the edit moved this track's last occupant out of it
    # (album-only change, where the old-artist block below wouldn't run). Leaving
    # the empty Album row behind is what inflated the library album count until a
    # manual Rescan.
    if old_album_id and old_album_id != row.album_id:
        remaining_album = (await session.execute(
            select(func.count(Track.id)).where(Track.album_id == old_album_id)
        )).scalar_one()
        if remaining_album == 0:
            await session.execute(sa_delete(Album).where(Album.id == old_album_id))
            await session.commit()

    # Prune old artist: delete its empty albums first, then delete artist if it
    # now has 0 tracks.  Empty albums must be removed first or the FK prevents
    # the artist delete.
    if old_artist_id:
        remaining = (await session.execute(
            select(func.count(Track.id)).where(Track.artist_id == old_artist_id)
        )).scalar_one()
        if remaining == 0:
            from sqlalchemy import delete as _sa_del_artist
            # Remove albums that now have no tracks
            old_albums = (await session.execute(
                select(Album).where(Album.artist_id == old_artist_id)
            )).scalars().all()
            for alb in old_albums:
                alb_tracks = (await session.execute(
                    select(func.count(Track.id)).where(Track.album_id == alb.id)
                )).scalar_one()
                if alb_tracks == 0:
                    await session.execute(_sa_del_artist(Album).where(Album.id == alb.id))
            old_artist = await session.get(Artist, old_artist_id)
            if old_artist:
                await session.delete(old_artist)
            await session.commit()

    await _do_scans()

    # When called from album detail view, reload the whole album card so the
    # track list reflects the updated metadata immediately.
    if source_album_id:
        from sqlalchemy.orm import joinedload as _jl2
        album_row = (await session.execute(
            select(Album)
            .options(_jl2(Album.artist), _jl2(Album.tracks).joinedload(Track.file))
            .where(Album.id == source_album_id)
        )).unique().scalar_one_or_none()
        if album_row:
            safe_aid = source_album_id.replace(":", "_")
            sorted_tracks = sorted(
                album_row.tracks, key=lambda t: (t.track_number is None, t.track_number or 0)
            )
            cover_track = next(
                (t for t in album_row.tracks if t.file and Path(t.file.path).exists()), None
            )
            resp = templates.TemplateResponse(
                request, "partials/album_detail.html",
                {"album": album_row, "sorted_tracks": sorted_tracks,
                 "cover_track": cover_track, "saved": True},
            )
            resp.headers["HX-Retarget"] = f"#album-{safe_aid}"
            resp.headers["HX-Reswap"] = "outerHTML"
            return resp

    # Reload fresh row, collapse back to browse-row in library context
    stmt2 = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    updated = (await session.execute(stmt2)).unique().scalar_one_or_none()
    return templates.TemplateResponse(
        request, "partials/browse_row.html",
        {"t": updated},
    )


@router.post("/{internal_id}/save-tags/preview", response_class=HTMLResponse)
async def preview_track_tags(
    request: Request,
    internal_id: str,
    title: str = Form(""),
    artist: str = Form(""),
    album: str = Form(""),
    year: str = Form(""),
    track_number: str = Form(""),
    mb_recording_id: str = Form(""),
    genre: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Read-only before/after preview for a library metadata edit (dry-run).

    Mirrors save-tags' input parsing and DB regrouping logic but writes nothing.
    Classifies each changed field as safe (retag only) or structural (regroups
    artist/album in Navidrome) and surfaces the album-split consequence — only
    this track moves, siblings stay put. save-tags re-tags the file in place; it
    does not relocate it, so we say so when structure changes.
    """
    from service.library.tagger import read_tags as _read_tags

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)

    file_path = Path(row.file.path)

    # Current ("before") state
    cur_title = row.title
    cur_artist = row.artist.name
    cur_album = row.album.title if row.album else None
    cur_year = row.album.year if row.album and row.album.year else None
    cur_track = row.track_number
    cur_genre = row.genre
    if not cur_genre and file_path.exists():
        tagged = await asyncio.to_thread(_read_tags, file_path)
        if tagged:
            cur_genre = tagged.genre
    cur_mbid = row.musicbrainz_recording_id

    # Proposed ("after") state — same parsing as save-tags
    year_val: int | None = int(year) if year.strip().isdigit() else None
    track_num_val: int | None = int(track_number) if track_number.strip().isdigit() else None
    new_title = title.strip() or cur_title
    new_artist = artist.strip() or cur_artist
    new_album = album.strip() or None
    new_mbid = mb_recording_id.strip() or None
    new_genre = genre.strip() or None

    changes: list[dict] = []

    def _add(field: str, before: object, after: object, structural: bool) -> None:
        if str(before or "") != str(after or ""):
            changes.append({
                "field": field,
                "before": before if (before is not None and before != "") else "—",
                "after": after if (after is not None and after != "") else "—",
                "structural": structural,
            })

    _add("Title", cur_title, new_title, False)
    _add("Artist", cur_artist, new_artist, True)
    _add("Album", cur_album, new_album, True)
    _add("Year", cur_year, year_val, False)
    _add("Track #", cur_track, track_num_val, False)
    _add("Genre", cur_genre, new_genre, False)
    _add("MB Recording ID", cur_mbid, new_mbid, True)

    structural = any(c["structural"] for c in changes)
    warnings: list[str] = []
    notes: list[str] = []

    artist_changed = new_artist != cur_artist
    album_changed = (new_album or None) != (cur_album or None)

    if artist_changed:
        existing_artist = (await session.execute(
            select(Artist).where(Artist.name == new_artist)
        )).scalars().first()
        if existing_artist:
            notes.append(f"Artist “{new_artist}” already exists — track merges into it.")
        else:
            notes.append(f"New artist “{new_artist}” will be created.")

    if album_changed and row.album_id:
        siblings = (await session.execute(
            select(func.count(Track.id)).where(
                Track.album_id == row.album_id, Track.id != row.id
            )
        )).scalar_one()
        if siblings > 0:
            warnings.append(
                f"Only this track moves. {siblings} other track"
                f"{'s' if siblings != 1 else ''} stay in “{cur_album}”."
            )
    if album_changed:
        if new_album:
            notes.append(f"Track regroups under album “{new_album}”.")
        else:
            notes.append("Album cleared — track becomes a Single.")

    if structural:
        notes.append("The audio file is re-tagged in place — it is not moved to a new folder.")

    return templates.TemplateResponse(
        request, "partials/edit_preview.html",
        {
            "changes": changes,
            "structural": structural,
            "warnings": warnings,
            "notes": notes,
        },
    )


@router.get("/{internal_id}/mb-search", response_class=HTMLResponse)
async def library_track_mb_search(
    request: Request,
    internal_id: str,
    q: str = "",
    limit: int = 10,
    duration: int | None = None,
) -> HTMLResponse:
    return await _mb_recording_search(
        request, q, limit, duration, job_id=None, track_id=internal_id
    )


@router.post("/{internal_id}/retag", response_class=HTMLResponse)
async def retag_track(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.library.tagger import has_cover_art as _has_cover_art, write_tags as _write_tags
    from service.metadata.musicbrainz import get_recording_by_id
    from service.metadata.quality import compute_quality_score

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.musicbrainz_recording_id or not row.file:
        raise HTTPException(400, "Track not found or missing MB Recording ID")

    match = await asyncio.to_thread(
        get_recording_by_id, row.musicbrainz_recording_id, settings.cache_dir
    )
    if match is None:
        raise HTTPException(502, "MusicBrainz lookup failed")

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    await asyncio.to_thread(
        _write_tags,
        file_path,
        title=match.title or None,
        artist=match.artist or None,
        album=match.album,
        year=match.year,
        track_number=match.track_number,
    )
    hca = await asyncio.to_thread(_has_cover_art, file_path)

    row.file.has_cover_art = hca
    row.tag_quality_score = compute_quality_score(
        title=match.title or row.title,
        artist=match.artist or row.artist.name,
        album=match.album or (row.album.title if row.album else None),
        year=match.year,
        track_number=match.track_number,
        musicbrainz_recording_id=row.musicbrainz_recording_id,
        has_cover_art=hca,
    )
    await session.commit()

    pct = int((row.tag_quality_score or 0) * 100)
    return HTMLResponse(
        f'<div class="card" style="opacity:0.6">'
        f'<div class="card-info">'
        f'<div class="card-title">{row.title}</div>'
        f'<div class="card-sub">{row.artist.name} · Re-tagged · Quality {pct}%</div>'
        f"</div></div>"
    )


@router.post("/{internal_id}/reacquire", response_class=HTMLResponse)
async def reacquire_track(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a re-acquisition for a track using its original provider_ref."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)
    if not row.file or not row.file.provider_ref:
        raise HTTPException(400, "Track has no provider reference — search and re-acquire manually")

    candidate = TrackCandidate(
        provider=row.file.provider or "ytdlp",
        provider_ref=row.file.provider_ref,
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        duration_seconds=row.duration_seconds,
    )

    job_id = await create_job(
        session,
        provider_name=candidate.provider,
        provider_ref=candidate.provider_ref,
        candidate=candidate,
        query=f"{candidate.artist} - {candidate.title} [re-acquire]",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=candidate.provider,
            provider_ref=candidate.provider_ref,
            candidate_json=candidate.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    return HTMLResponse(
        f'<div class="card" style="opacity:0.5">'
        f'<div class="card-info">'
        f'<div class="card-title">{row.title}</div>'
        f'<div class="card-sub">{row.artist.name} · Re-acquisition queued → <a href="/jobs">Jobs</a></div>'
        f"</div></div>"
    )


@router.get("/{internal_id}/search-replacement", response_class=HTMLResponse)
async def search_replacement_sources(
    request: Request,
    internal_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search for a replacement audio source for an existing library track."""
    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)

    search_q = q.strip() or f"{row.artist.name} - {row.title}"
    candidates: list[dict[str, object]] = []
    try:
        import service.providers.ytdlp  # noqa: F401
        from service.core.models import SearchQuery
        from service.providers import get

        provider = get("ytdlp")()
        raw: list[dict[str, object]] = []
        async for c in provider.search(SearchQuery(q=search_q, limit=10)):
            raw.append({
                "title": c.title,
                "artist": c.artist,
                "duration_seconds": c.duration_seconds,
                "provider_ref": c.provider_ref,
                "thumbnail_url": c.thumbnail_url,
                "candidate_json": c.model_dump_json(),
                "_score": _explicit_score(c.title),
            })
        if settings.prefer_explicit:
            raw.sort(key=lambda x: -int(x["_score"]))  # type: ignore[arg-type]
        for item in raw:
            del item["_score"]
        candidates = raw[:8]
    except Exception as exc:
        logger.warning("Replacement search failed for %s: %s", internal_id, exc)

    return templates.TemplateResponse(
        request, "partials/replacement_results.html",
        {"candidates": candidates, "track": row, "q": search_q},
    )


@router.post("/{internal_id}/queue-replacement", response_class=HTMLResponse)
async def queue_replacement_track(
    request: Request,
    internal_id: str,
    provider_ref: str = Form(...),
    candidate_json: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a new acquisition to replace a library track's audio source.

    Locks the existing track's album/artist/MB ID into the candidate so album
    grouping is preserved. The old track remains until the user deletes it after
    approving the replacement in the review queue.
    """
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    stmt = (
        select(Track)
        .options(
            joinedload(Track.artist),
            joinedload(Track.album),
            joinedload(Track.file),
        )
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)

    try:
        base = TrackCandidate.model_validate_json(candidate_json)
    except Exception:
        raise HTTPException(400, "Invalid candidate JSON")

    # Lock existing track's metadata so album grouping is preserved.
    # skip_dedup=True: the existing track IS the local match — we want to replace it,
    # not have the dedup check mark the job done immediately.
    locked = base.model_copy(update={
        "title": row.title,
        "artist": row.artist.name,
        "album": row.album.title if row.album else None,
        "year": row.album.year if row.album else None,
        "track_number": row.track_number,
        "mb_recording_id": row.musicbrainz_recording_id,
        "mb_release_id": row.album.musicbrainz_release_id if row.album else None,
        "skip_dedup": True,
        "replace_path": row.file.path if row.file else None,
    })

    job_id = await create_job(
        session,
        provider_name=locked.provider,
        provider_ref=provider_ref,
        candidate=locked,
        query=f"{locked.artist} - {locked.title} [replacement]",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=locked.provider,
            provider_ref=provider_ref,
            candidate_json=locked.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    safe_id = internal_id.replace(":", "_")
    return HTMLResponse(
        f'<span class="badge badge-done" id="replace-status-{safe_id}">'
        f'Queued → <a href="/jobs">Jobs ↗</a></span>'
    )


@router.post("/{internal_id}/queue-url-replacement", response_class=HTMLResponse)
async def queue_url_replacement(
    request: Request,
    internal_id: str,
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue a replacement download from a user-supplied URL."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    stmt = (
        select(Track)
        .options(
            joinedload(Track.artist),
            joinedload(Track.album),
            joinedload(Track.file),
        )
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(404)

    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref=url.strip(),
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        year=row.album.year if row.album else None,
        track_number=row.track_number,
        mb_recording_id=row.musicbrainz_recording_id,
        mb_release_id=row.album.musicbrainz_release_id if row.album else None,
        skip_dedup=True,
        replace_path=row.file.path if row.file else None,
    )

    job_id = await create_job(
        session,
        provider_name=candidate.provider,
        provider_ref=candidate.provider_ref,
        candidate=candidate,
        query=f"{candidate.artist} - {candidate.title} [url-replacement]",
    )
    await session.commit()

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job(
            "acquire_track",
            job_id=job_id,
            provider_name=candidate.provider,
            provider_ref=candidate.provider_ref,
            candidate_json=candidate.model_dump_json(),
            music_dir=str(settings.music_dir),
            tmp_acquire_dir=str(settings.tmp_acquire_dir),
            _job_id=f"acquire:{job_id}",
        )
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    safe_id = internal_id.replace(":", "_")
    return HTMLResponse(
        f'<span class="badge badge-done" id="replace-status-{safe_id}">'
        f'Queued → <a href="/jobs">Jobs ↗</a></span>'
    )


@router.post("/{internal_id}/fetch-art", response_class=HTMLResponse)
async def fetch_track_art(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Fetch cover art from Cover Art Archive and embed it in the track file."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_tags as _write_tags
    from service.metadata.artwork import fetch_artwork
    from service.metadata.musicbrainz import get_recording_by_id
    from service.metadata.quality import compute_quality_score

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.musicbrainz_recording_id or not row.file:
        raise HTTPException(400, "Track not found or missing MB Recording ID")

    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    # Get release ID for CAA via MB recording lookup
    mb_rec = await asyncio.to_thread(
        get_recording_by_id, row.musicbrainz_recording_id, settings.cache_dir
    )
    release_id = mb_rec.release_id if mb_rec else None

    art = await fetch_artwork(
        release_mbid=release_id,
        cache_dir=settings.cache_dir,
    )
    if not art:
        return _error_badge("No artwork found on Cover Art Archive")

    await asyncio.to_thread(_write_tags, file_path, artwork_bytes=art)
    hca = await asyncio.to_thread(_has_cover_art, file_path)

    row.file.has_cover_art = hca
    row.tag_quality_score = compute_quality_score(
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        year=None,
        track_number=row.track_number,
        musicbrainz_recording_id=row.musicbrainz_recording_id,
        has_cover_art=hca,
    )
    await session.commit()
    await _do_scans()

    return HTMLResponse('<span class="badge badge-done">Art embedded ✓</span>')


@router.post("/{internal_id}/apply-art", response_class=HTMLResponse)
async def apply_art_to_track(
    request: Request,
    internal_id: str,
    art_url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download art from a URL and embed it in a track file."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)
    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    art, err = await _fetch_user_art(art_url)
    if err is not None:
        return err

    await asyncio.to_thread(_write_tags, file_path, artwork_bytes=art)
    # Only write sidecar cover.jpg for album tracks — singles share their parent
    # directory with other singles from the same artist, so a sidecar would
    # overwrite every sibling's cover.
    if row.album_id is not None:
        write_cover_jpg(file_path.parent, art)
    hca = await asyncio.to_thread(_has_cover_art, file_path)
    row.file.has_cover_art = hca
    await session.commit()
    await _do_scans()

    # Refresh the cover art preview in the card header via OOB swap.
    # The card uses id="edit-cover-{safe_id}" where safe_id = track.id.replace(':', '_').
    import time as _time
    safe_id = internal_id.replace(":", "_")
    cache_bust = int(_time.time())
    oob_img = (
        f'<img src="/library/tracks/{internal_id}/cover-art?t={cache_bust}" '
        f'id="edit-cover-{safe_id}" hx-swap-oob="true" '
        f'onerror="this.style.display=\'none\';'
        f'var ph=document.getElementById(\'edit-cover-placeholder-{safe_id}\');'
        f'if(ph)ph.style.display=\'flex\'" '
        f'style="width:100%;height:100%;object-fit:cover;border-radius:inherit" alt="">'
    )
    return HTMLResponse(f'<span class="badge badge-done">Art applied ✓</span>{oob_img}')


@router.post("/{internal_id}/upload-art", response_class=HTMLResponse)
async def upload_track_art(
    request: Request,
    internal_id: str,
    cover: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Embed a user-supplied image as cover art in a track file."""
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small

    stmt = (
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id == internal_id)
    )
    row = (await session.execute(stmt)).unique().scalar_one_or_none()
    if row is None or not row.file:
        raise HTTPException(404)
    file_path = Path(row.file.path)
    if not file_path.exists():
        raise HTTPException(404, "File not on disk")

    if not cover.content_type or not cover.content_type.startswith("image/"):
        return _error_badge("Not an image file")

    art = await cover.read()
    if not art:
        return _error_badge("Empty file")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return _error_badge("Image too small — must be at least 300×300 px")

    await asyncio.to_thread(_write_tags, file_path, artwork_bytes=art)
    write_cover_jpg(file_path.parent, art)
    hca = await asyncio.to_thread(_has_cover_art, file_path)
    row.file.has_cover_art = hca
    await session.commit()

    return HTMLResponse(
        f'<img src="/library/tracks/{internal_id}/cover-art?t={int(asyncio.get_event_loop().time())}"'
        f' style="width:100%;height:100%;object-fit:cover;border-radius:inherit" alt="">'
        f'<span class="badge badge-done" style="position:absolute;bottom:4px;left:4px;font-size:10px">Saved ✓</span>'
    )


@router.post("/{track_id}/enrich", response_class=HTMLResponse)
async def enrich_track_now(
    track_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Enqueue a MusicBrainz enrichment job for a specific library track."""
    row = await session.get(Track, track_id)
    if row is None:
        raise HTTPException(404)

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("enrich_track", track_id=track_id)
        await redis.aclose()
    except Exception as exc:
        return _error_badge(f"Error: {exc}", level="fail")

    return HTMLResponse('<span style="color:var(--success);font-size:12px">✓ Enrichment queued — check the Jobs page</span>')


@router.get("/{internal_id}/stream")
async def stream_library_track(
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream a library track's audio — used by the lyrics sync preview player."""
    from fastapi.responses import FileResponse
    track = await _get_track_with_file(session, internal_id)
    if not track.file:
        raise HTTPException(404)
    path = Path(track.file.path)
    if not path.exists():
        raise HTTPException(404)
    ext = path.suffix.lower()
    media_map = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".flac": "audio/flac",
                 ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac"}
    return FileResponse(path, media_type=media_map.get(ext, "audio/ogg"))


@router.get("/{internal_id}/lyrics-panel", response_class=HTMLResponse)
async def track_lyrics_panel(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    track = await _get_track_with_file(session, internal_id)
    return templates.TemplateResponse(
        request, "partials/lyrics_panel.html", _lyrics_panel_ctx(track)
    )


@router.post("/{internal_id}/lyrics", response_class=HTMLResponse)
async def track_lyrics_action(
    request: Request,
    internal_id: str,
    action: str = Form("save"),
    lyrics: str = Form(""),
    offset: float = Form(0.0),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save, delete, (re)fetch, or time-shift the .lrc sidecar for one track."""
    from service.metadata.lyrics import fetch_lyrics, lrc_sidecar_path, shift_lrc, write_lrc_sidecar

    track = await _get_track_with_file(session, internal_id)
    if not track.file:
        raise HTTPException(400, "Track has no file")
    audio = Path(track.file.path)
    lrc = lrc_sidecar_path(audio)

    message, kind = "", "ok"
    if action == "delete":
        try:
            lrc.unlink(missing_ok=True)
            message = "Lyrics sidecar deleted."
        except OSError as exc:
            message, kind = f"Delete failed: {exc}", "warn"
    elif action == "fetch":
        # Bypass the disk cache (incl. miss markers) — this is an explicit
        # user request, so always ask LRCLIB fresh.
        result = await fetch_lyrics(
            artist=track.artist.name if track.artist else None,
            title=track.title,
            album=track.album.title if track.album else None,
            duration_seconds=track.duration_seconds,
            cache_dir=None,
        )
        if result is not None and result.instrumental:
            message, kind = "LRCLIB marks this track as instrumental — no lyrics to write.", "warn"
        elif result is not None and result.best:
            if write_lrc_sidecar(audio, result.best):
                message = "Fetched from LRCLIB" + (" (synced)." if result.synced else " (plain text).")
            else:
                message, kind = "Fetched, but writing the sidecar failed.", "warn"
        else:
            message, kind = "No lyrics found on LRCLIB for this track.", "warn"
    elif action == "offset":
        # Shift every timestamp in the submitted text (keeps unsaved edits) and save.
        text = shift_lrc(lyrics.replace("\r\n", "\n"), offset).strip()
        if not text:
            message, kind = "Nothing to shift — lyrics are empty.", "warn"
        elif abs(offset) < 0.001:
            message, kind = "Offset is 0 — nothing changed.", "warn"
        elif write_lrc_sidecar(audio, text + "\n"):
            direction = "later" if offset > 0 else "earlier"
            message = f"Timestamps shifted {abs(offset):.2f}s {direction} and saved."
        else:
            message, kind = "Shifted, but saving the sidecar failed.", "warn"
    else:  # save
        text = lyrics.replace("\r\n", "\n").strip()
        if not text:
            try:
                lrc.unlink(missing_ok=True)
                message = "Empty — lyrics sidecar removed."
            except OSError as exc:
                message, kind = f"Delete failed: {exc}", "warn"
        elif write_lrc_sidecar(audio, text + "\n"):
            message = "Lyrics saved."
        else:
            message, kind = "Saving the sidecar failed.", "warn"

    if kind == "ok":
        # Navidrome serves .lrc sidecars — nudge it so clients see the change.
        try:
            from service.navidrome.client import trigger_scan
            await trigger_scan()
        except Exception as exc:
            logger.debug("best-effort Navidrome scan trigger failed: %s", exc)

    return templates.TemplateResponse(
        request, "partials/lyrics_panel.html",
        _lyrics_panel_ctx(track, message=message, message_kind=kind,
                          sync_open=(action == "offset")),
    )


def _lyrics_panel_ctx(
    track: Track, *, message: str = "", message_kind: str = "ok", sync_open: bool = False,
) -> dict:
    from service.metadata.lyrics import lrc_sidecar_path, sidecar_is_synced

    status: str | None = None
    text = ""
    if track.file:
        audio = Path(track.file.path)
        lrc = lrc_sidecar_path(audio)
        try:
            if lrc.exists() and lrc.stat().st_size > 0:
                text = lrc.read_text(encoding="utf-8")
                status = "synced" if sidecar_is_synced(audio) else "plain"
        except OSError:
            pass
    return {
        "track": track,
        "safe_id": track.id.replace(":", "_"),
        "lyrics_status": status,
        "lyrics_text": text,
        "message": message,
        "message_kind": message_kind,
        "sync_open": sync_open,
    }
