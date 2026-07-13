"""Library overview, browse, genres, quality review, enrichment, trash."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from service.config import settings
from service.db.schema import AcquisitionJobRow, Album, Artist, Track, TrackFile
from service.db.session import get_session

from service.api.shared import _BROWSE_PAGE, _do_scans, _error_badge, templates

logger = logging.getLogger(__name__)
router = APIRouter()


async def _library_stats_context(session: AsyncSession) -> dict:
    """Compute stats and quality counts for the library overview."""
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    track_count = (await session.execute(
        select(func.count(Track.id)).join(Track.artist).join(Track.file)
    )).scalar_one()
    # Count albums/artists/genres by the tracks that actually exist (with a file),
    # not by raw Album/Artist row counts. Empty rows left behind by edits that move
    # the last track out of an album/artist would otherwise inflate these until a
    # manual Rescan ran the scanner's cascade cleanup — the recurring "counts don't
    # match reality" complaint. Counting through tracks-with-files keeps the overview
    # correct immediately, regardless of which mutation path forgot to prune.
    album_count = (await session.execute(
        select(func.count(func.distinct(Track.album_id)))
        .join(Track.file).where(Track.album_id.isnot(None))
    )).scalar_one()
    artist_count = (await session.execute(
        select(func.count(func.distinct(Track.artist_id))).join(Track.file)
    )).scalar_one()
    genre_count = (await session.execute(
        select(func.count(func.distinct(Track.genre)))
        .join(Track.file).where(Track.genre.isnot(None))
    )).scalar_one()
    no_mbid_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.musicbrainz_recording_id.is_(None))
    )).scalar_one()
    no_art_count = (await session.execute(
        select(func.count(Track.id)).join(Track.artist).join(Track.file).where(
            (TrackFile.has_cover_art.is_(None)) | (TrackFile.has_cover_art == 0)
        )
    )).scalar_one()
    _not_suppressed = (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0)
    low_quality_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(
            (Track.tag_quality_score.isnot(None))
            & (Track.tag_quality_score < LOW_QUALITY_THRESHOLD)
            & _not_suppressed
        )
    )).scalar_one()
    _bitrate_not_suppressed = (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0)
    low_bitrate_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).join(Track.artist).where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < settings.min_bitrate_kbps,
            _bitrate_not_suppressed,
        )
    )).scalar_one()
    dupe_count = (await session.execute(
        select(func.count()).select_from(
            select(Track.musicbrainz_recording_id)
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.is_not(None))
            .group_by(Track.musicbrainz_recording_id)
            .having(func.count(Track.id) > 1)
            .subquery()
        )
    )).scalar_one()
    quality_suppressed_count = (await session.execute(
        select(func.count(Track.id)).where(Track.quality_suppressed == 1)
    )).scalar_one()
    bitrate_suppressed_count = (await session.execute(
        select(func.count(Track.id)).where(Track.bitrate_suppressed == 1)
    )).scalar_one()
    return {
        "stats": {"tracks": track_count, "albums": album_count, "artists": artist_count, "genres": genre_count},
        "quality": {
            "no_mbid": no_mbid_count, "no_art": no_art_count,
            "low_quality": low_quality_count, "low_bitrate": low_bitrate_count, "dupes": dupe_count,
            "quality_suppressed": quality_suppressed_count, "bitrate_suppressed": bitrate_suppressed_count,
        },
        "min_bitrate_kbps": settings.min_bitrate_kbps,
    }


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    stats_ctx = await _library_stats_context(session)
    _not_suppressed = (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0)

    recent_rows = (
        await session.execute(
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .join(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .order_by(TrackFile.created_at.desc())
            .limit(60)
        )
    ).unique().scalars().all()

    # Recently-added rail: one card per album (newest first), single tracks as
    # their own cards — a batch of 15 album tracks shouldn't fill the rail with
    # 15 copies of the same cover.
    recent_rail: list[dict] = []
    _seen_albums: set[str] = set()
    for t in recent_rows:
        if t.album_id:
            if t.album_id in _seen_albums:
                continue
            _seen_albums.add(t.album_id)
            recent_rail.append({
                "kind": "album", "album_id": t.album_id,
                "title": t.album.title if t.album else "?",
                "sub": t.artist.name, "cover_track_id": t.id,
            })
        else:
            recent_rail.append({
                "kind": "track", "track_id": t.id,
                "title": t.title, "sub": t.artist.name, "cover_track_id": t.id,
            })
        if len(recent_rail) >= 20:
            break

    needs_review_count = (
        await session.execute(
            select(func.count(AcquisitionJobRow.id))
            .where(AcquisitionJobRow.state == "needs_review")
        )
    ).scalar_one()

    artist_names = (await session.execute(select(Artist.name).order_by(Artist.name))).scalars().all()
    album_names = (await session.execute(select(Album.title).order_by(Album.title))).scalars().all()

    return templates.TemplateResponse(
        request, "library.html",
        {
            "active": "library",
            **stats_ctx,
            "recent_rail": recent_rail,
            "settings_music_dir": str(settings.music_dir),
            "needs_review_count": needs_review_count,
            "artist_names": artist_names,
            "album_names": album_names,
        },
    )


@router.get("/library/stats-fragment", response_class=HTMLResponse)
async def library_stats_fragment(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return the stats tiles fragment for OOB update after rescan."""
    stats_ctx = await _library_stats_context(session)
    inner = templates.get_template("partials/library_stats.html").render(stats_ctx)
    return HTMLResponse(f'<div id="library-stats" hx-swap-oob="true">{inner}</div>')


@router.get("/library/stats-poll", response_class=HTMLResponse)
async def library_stats_poll(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return inner stats content for periodic polling (no OOB wrapper)."""
    stats_ctx = await _library_stats_context(session)
    return templates.TemplateResponse(request, "partials/library_stats.html", stats_ctx)


@router.post("/library/rescan", response_class=HTMLResponse)
async def library_rescan(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Full rescan of /music: adds new files, removes missing ones from DB."""
    from service.index.scanner import scan

    try:
        result = await scan(session, settings.music_dir, incremental=False)
        await session.commit()
    except Exception as exc:
        logger.error("Library rescan failed: %s", exc)
        return _error_badge(f"Rescan failed: {exc}", level="fail")

    await _do_scans()

    # OOB-update the stats tiles so the user sees fresh counts without a page reload
    stats_ctx = await _library_stats_context(session)
    inner = templates.get_template("partials/library_stats.html").render(stats_ctx)
    badge = (
        f'<span class="badge badge-done">'
        f'Rescan done — {result.added} added, {result.removed} removed, {result.updated} updated'
        f'</span>'
    )
    oob = f'<div id="library-stats" hx-swap-oob="true">{inner}</div>'
    return HTMLResponse(badge + oob)


@router.get("/library/genres", response_class=HTMLResponse)
async def library_genres_page(
    request: Request,
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (await session.execute(
        select(Track.genre, func.count(Track.id).label("track_count"))
        .join(Track.file)
        .where(Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc(), Track.genre)
    )).all()
    untagged_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.genre.is_(None))
    )).scalar_one()
    genres = [{"name": r.genre, "count": r.track_count} for r in rows]
    ctx = {"active": "library", "genres": genres, "untagged_count": untagged_count}
    tmpl = "partials/view_genres.html" if embed else "library_genres.html"
    return templates.TemplateResponse(request, tmpl, ctx)


@router.post("/library/genres/rename", response_class=HTMLResponse)
async def genre_rename(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Rename a genre across all tracks (DB + file tags)."""
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    old_genre = (form.get("old_genre") or "").strip()
    new_genre = (form.get("new_genre") or "").strip()
    if not old_genre:
        return _error_badge("Missing genre name")

    target_genre = new_genre if new_genre else None  # empty new = remove genre

    rows = (await session.execute(
        select(Track).options(joinedload(Track.file)).where(Track.genre == old_genre)
    )).unique().scalars().all()
    for row in rows:
        row.genre = target_genre
        if row.file:
            fp = Path(row.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(_write_tags, fp, genre=target_genre or "")
                except Exception as exc:
                    logger.warning("genre_rename tag write failed for %s: %s", fp, exc)
    await session.commit()
    await _do_scans()

    rows2 = (await session.execute(
        select(Track.genre, func.count(Track.id).label("track_count"))
        .join(Track.file)
        .where(Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc(), Track.genre)
    )).all()
    untagged_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.genre.is_(None))
    )).scalar_one()
    genres = [{"name": r.genre, "count": r.track_count} for r in rows2]
    return templates.TemplateResponse(
        request, "partials/genre_list.html",
        {"genres": genres, "untagged_count": untagged_count},
    )


@router.post("/library/genres/remove", response_class=HTMLResponse)
async def genre_remove(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Remove a genre from all tracks that have it."""
    form = await request.form()
    genre = (form.get("genre") or "").strip()
    if not genre:
        return _error_badge("Missing genre name")
    # Delegate to rename with empty new_genre
    # Reconstruct form-like data and call rename logic inline
    from service.library.tagger import write_tags as _write_tags

    rows = (await session.execute(
        select(Track).options(joinedload(Track.file)).where(Track.genre == genre)
    )).unique().scalars().all()
    for row in rows:
        row.genre = None
        if row.file:
            fp = Path(row.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(_write_tags, fp, genre="")
                except Exception as exc:
                    logger.warning("genre_remove tag write failed for %s: %s", fp, exc)
    await session.commit()
    await _do_scans()

    rows2 = (await session.execute(
        select(Track.genre, func.count(Track.id).label("track_count"))
        .join(Track.file)
        .where(Track.genre.isnot(None))
        .group_by(Track.genre)
        .order_by(func.count(Track.id).desc(), Track.genre)
    )).all()
    untagged_count = (await session.execute(
        select(func.count(Track.id)).join(Track.file).where(Track.genre.is_(None))
    )).scalar_one()
    genres = [{"name": r.genre, "count": r.track_count} for r in rows2]
    return templates.TemplateResponse(
        request, "partials/genre_list.html",
        {"genres": genres, "untagged_count": untagged_count},
    )


@router.get("/library/browse", response_class=HTMLResponse)
async def library_browse(
    request: Request,
    q: str = "",
    f: str = "",
    sort: str = "artist",
    genre: str = "",
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unified library browser: search + quality review + metadata edit.

    embed=1 returns just the view content for in-place loading into the /library
    page; otherwise the full standalone page.
    """
    genre_list = (await session.execute(
        select(Track.genre).where(Track.genre.isnot(None)).distinct().order_by(Track.genre)
    )).scalars().all()
    ctx = {"active": "library", "q": q, "f": f, "sort": sort, "genre": genre, "genre_list": genre_list}
    tmpl = "partials/view_browse.html" if embed else "library_browse.html"
    return templates.TemplateResponse(request, tmpl, ctx)


@router.get("/library/browse/results", response_class=HTMLResponse)
async def library_browse_results(
    request: Request,
    q: str = "",
    f: str = "",
    sort: str = "artist",
    offset: int = 0,
    genre: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.metadata.quality import LOW_QUALITY_THRESHOLD

    order = {
        "title":   Track.title,
        "quality": Track.tag_quality_score.asc().nullslast(),  # type: ignore[union-attr]
        "recent":  TrackFile.created_at.desc(),
        "album":   (Album.title.nullslast(), Track.track_number),  # type: ignore[union-attr]
    }.get(sort, (Artist.name, Track.title))

    stmt = (
        select(Track)
        .join(Track.artist)
        .outerjoin(Track.album)
        .join(Track.file)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .offset(offset)
        .limit(_BROWSE_PAGE + 1)
    )
    if isinstance(order, tuple):
        stmt = stmt.order_by(*order)
    else:
        stmt = stmt.order_by(order)
    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            Track.title.ilike(pattern) | Artist.name.ilike(pattern) | Album.title.ilike(pattern)
        )

    # Filter tabs
    if f == "no_mb":
        stmt = stmt.where(Track.musicbrainz_recording_id.is_(None))
    elif f == "no_art":
        stmt = stmt.where(
            (TrackFile.has_cover_art.is_(None)) | (TrackFile.has_cover_art == 0)
        )
    elif f == "low_quality":
        stmt = stmt.where(
            Track.tag_quality_score.isnot(None),
            Track.tag_quality_score < LOW_QUALITY_THRESHOLD,
            (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0),
        )
    elif f == "low_bitrate":
        min_br = settings.min_bitrate_kbps
        stmt = stmt.where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < min_br,
            (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0),
        )
    elif f == "low_bitrate_suppressed":
        min_br = settings.min_bitrate_kbps
        stmt = stmt.where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < min_br,
            Track.bitrate_suppressed == 1,
        )
    elif f == "quality_suppressed":
        stmt = stmt.where(Track.quality_suppressed == 1)
    elif f == "dupes":
        dupe_rids_sub = (
            select(Track.musicbrainz_recording_id)
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.is_not(None))
            .group_by(Track.musicbrainz_recording_id)
            .having(func.count(Track.id) > 1)
            .scalar_subquery()
        )
        stmt = stmt.where(Track.musicbrainz_recording_id.in_(dupe_rids_sub))
    elif f == "singles":
        stmt = stmt.where(Track.album_id.is_(None))
    elif f == "no_genre":
        stmt = stmt.where(Track.genre.is_(None))

    # Genre is an orthogonal filter — stack it on top of any f tab.
    if genre:
        stmt = stmt.where(Track.genre == genre)

    all_rows = (await session.execute(stmt)).unique().scalars().all()
    has_more = len(all_rows) > _BROWSE_PAGE
    rows = all_rows[:_BROWSE_PAGE]
    return templates.TemplateResponse(
        request, "partials/browse_results.html",
        {"tracks": rows, "q": q, "f": f, "sort": sort,
         "offset": offset, "has_more": has_more, "next_offset": offset + _BROWSE_PAGE},
    )


@router.post("/library/browse/bulk-edit", response_class=HTMLResponse)
async def library_bulk_edit(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Apply genre and/or year to a batch of selected tracks."""
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    track_ids: list[str] = list(form.getlist("track_id"))  # type: ignore[arg-type]
    genre_val = (form.get("genre") or "").strip() or None  # type: ignore[union-attr]
    year_str = (form.get("year") or "").strip()  # type: ignore[union-attr]
    year_val: int | None = int(year_str) if year_str.isdigit() else None  # type: ignore[arg-type]

    if not track_ids:
        return _error_badge("No tracks selected")
    if genre_val is None and year_val is None:
        return _error_badge("Enter at least one field to update")

    updated = 0
    # Batch-fetch all selected tracks in one query
    all_rows = (await session.execute(
        select(Track)
        .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
        .where(Track.id.in_(track_ids))
    )).unique().scalars().all()
    row_by_id = {r.id: r for r in all_rows}

    failed = 0
    for tid in track_ids:
        row = row_by_id.get(tid)
        if row is None or not row.file:
            continue
        file_path = Path(row.file.path)
        if not file_path.exists():
            continue
        try:
            kwargs: dict[str, object] = {}
            if genre_val is not None:
                kwargs["genre"] = genre_val
                row.genre = genre_val
            if year_val is not None:
                kwargs["year"] = year_val
            if kwargs:
                await asyncio.to_thread(_write_tags, file_path, **kwargs)
            updated += 1
        except Exception as exc:
            logger.warning("bulk-edit: write_tags failed for %s: %s", file_path, exc)
            failed += 1

    await session.commit()
    await _do_scans()

    msg = f"Updated {updated} track{'s' if updated != 1 else ''}"
    if failed:
        msg += f", {failed} failed"
    return HTMLResponse(f'<span class="badge-ok">{msg} ✓</span>')


@router.get("/library/quality")
async def quality_review_page() -> RedirectResponse:
    """Legacy quality-review page — superseded by Library Health, which covers the
    same data (low bitrate / missing art / missing files) with richer remediation."""
    return RedirectResponse("/library/health", status_code=301)


@router.post("/library/enrich", response_class=HTMLResponse)
async def library_enrich_filtered(
    request: Request,
    session: AsyncSession = Depends(get_session),
    artist: str = Form(""),
    album: str = Form(""),
) -> HTMLResponse:
    """Queue MusicBrainz enrichment for tracks without a Recording ID.

    Optional artist/album name filters narrow the scope — useful for re-enriching
    a specific artist or album rather than the entire library.
    """
    from sqlalchemy.orm import joinedload as _jl
    from service.core.normalize import normalize as _norm

    stmt = (
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album))
        .where(Track.musicbrainz_recording_id.is_(None))
    )
    rows = (await session.execute(stmt)).unique().scalars().all()

    artist_filter = _norm(artist.strip())
    album_filter = _norm(album.strip())
    if artist_filter:
        rows = [r for r in rows if artist_filter in _norm(r.artist.name)]
    if album_filter:
        rows = [r for r in rows if r.album and album_filter in _norm(r.album.title)]

    if not rows:
        return HTMLResponse('<p class="empty" style="font-size:12px;padding:4px 0">No matching tracks without a MB Recording ID.</p>')

    try:
        from arq import create_pool
        from arq.connections import RedisSettings
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        for track in rows:
            await redis.enqueue_job("enrich_track", track_id=track.id)
        await redis.aclose()
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    label = f"{len(rows)} track{'s' if len(rows) != 1 else ''}"
    if artist_filter or album_filter:
        parts = []
        if artist_filter:
            parts.append(f'artist “{artist.strip()}”')
        if album_filter:
            parts.append(f'album “{album.strip()}”')
        label += f" matching {' + '.join(parts)}"
    return HTMLResponse(f'<p style="font-size:12px;padding:4px 0;color:var(--success)">✓ Queued enrichment for {label} — results appear in Jobs.</p>')


def _list_trash(trash_dir: Path) -> list[dict]:
    """Walk a .trash directory and return metadata for each file."""
    items: list[dict] = []
    if not trash_dir.exists():
        return items
    for ts_dir in sorted(trash_dir.iterdir(), reverse=True):
        if not ts_dir.is_dir() or ts_dir.name.startswith("."):
            continue
        for f in ts_dir.iterdir():
            if f.name.endswith(".restore_path") or not f.is_file():
                continue
            restore_path_file = ts_dir / f"{f.name}.restore_path"
            original_path: str | None = None
            if restore_path_file.exists():
                try:
                    original_path = restore_path_file.read_text(encoding="utf-8").strip()
                except Exception as exc:
                    logger.warning("reading restore_path sidecar failed: %s", exc)
            try:
                size_bytes = f.stat().st_size
            except OSError:
                size_bytes = 0
            items.append({
                "ts": ts_dir.name,
                "filename": f.name,
                "original_path": original_path,
                "size_mb": round(size_bytes / 1_048_576, 1),
            })
    return items


@router.get("/library/trash", response_class=HTMLResponse)
async def library_trash(request: Request) -> HTMLResponse:
    music_trash = _list_trash(settings.music_dir / ".trash")
    staging_trash = _list_trash(settings.staging_dir / ".trash")
    return templates.TemplateResponse(
        request, "partials/trash_list.html",
        {"music_trash": music_trash, "staging_trash": staging_trash},
    )


@router.post("/library/trash/{ts}/{filename}/restore", response_class=HTMLResponse)
async def trash_restore(
    request: Request,
    ts: str,
    filename: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Restore a trashed file to its original path (or music root if unknown)."""
    import urllib.parse
    filename = urllib.parse.unquote(filename)
    # Check both music and staging trash directories
    trash_file = settings.music_dir / ".trash" / ts / filename
    if not trash_file.exists():
        trash_file = settings.staging_dir / ".trash" / ts / filename
    if not trash_file.exists():
        raise HTTPException(404, "File not found in trash")

    restore_path_file = trash_file.parent / f"{filename}.restore_path"
    if restore_path_file.exists():
        try:
            dest = Path(restore_path_file.read_text(encoding="utf-8").strip())
        except Exception:
            dest = settings.music_dir / filename
    else:
        dest = settings.music_dir / filename

    try:
        from service.library.writer import atomic_place
        atomic_place(trash_file, dest)
        # Clean up sidecar
        if restore_path_file.exists():
            restore_path_file.unlink(missing_ok=True)
    except Exception as exc:
        raise HTTPException(500, f"Restore failed: {exc}") from exc

    try:
        from service.index.scanner import index_file
        await index_file(session, dest)
        await session.commit()
    except Exception as exc:
        logger.warning("post-restore indexing failed (file restored, will appear on next scan): %s", exc)

    await _do_scans()

    return HTMLResponse(f'<span class="badge-ok">Restored → {dest.name}</span>')


@router.delete("/library/trash/{ts}/{filename}", response_class=HTMLResponse)
async def trash_delete(ts: str, filename: str) -> HTMLResponse:
    """Permanently delete a file from trash."""
    import urllib.parse
    filename = urllib.parse.unquote(filename)
    trash_file = settings.music_dir / ".trash" / ts / filename
    if not trash_file.exists():
        trash_file = settings.staging_dir / ".trash" / ts / filename
    restore_sidecar = trash_file.parent / f"{filename}.restore_path"
    try:
        if trash_file.exists():
            trash_file.unlink()
        restore_sidecar.unlink(missing_ok=True)
        # Remove empty timestamp dir
        try:
            trash_file.parent.rmdir()
        except OSError:
            pass
    except Exception as exc:
        raise HTTPException(500, f"Delete failed: {exc}") from exc
    return HTMLResponse("")
