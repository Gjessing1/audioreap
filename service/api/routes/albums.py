"""Album list/detail, metadata, MB linkage, disc fixes, cover art, merge."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from service.config import settings
from service.db.schema import Album, Artist, Track, TrackFile
from service.library.writer import safe_trash
from service.db.session import get_session
from service.library.writer import trash_empty_album_dir as _trash_empty_album_dir
from service.library.tagger import read_mb_release_id as _read_mb_release_id

from service.api.routes.artwork import _fetch_user_art
from service.api.shared import _LIST_PAGE, _do_scans, _error_badge, _layout_view, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/library/albums")


@router.get("", response_class=HTMLResponse)
async def library_albums_page(
    request: Request,
    q: str = "",
    sort: str = "",
    view: str = "",
    open_id: str = Query("", alias="open"),
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    # `open` makes the drill-down bookmarkable: album rows hx-push-url this
    # page with ?open=<album id>, so refresh/back restores the open album.
    ctx = {"active": "library", "q": q, "sort": sort, "open_id": open_id,
           "view": _layout_view(request, view, "album_view")}
    tmpl = "partials/view_albums.html" if embed else "library_albums.html"
    return templates.TemplateResponse(request, tmpl, ctx)


@router.get("/merge-candidates", response_class=HTMLResponse)
async def library_albums_merge_candidates(
    request: Request,
    q: str = "",
    canonical: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return album rows as merge-into-canonical candidates with action buttons."""
    from sqlalchemy.orm import joinedload as _jl
    if not q.strip():
        return HTMLResponse('<p class="muted" style="font-size:12px">Type to search…</p>')
    pattern = f"%{q.strip()}%"
    stmt = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .where(Album.title.ilike(pattern) | Artist.name.ilike(pattern))
        .where(Album.id != canonical)
        .order_by(Artist.name, Album.year, Album.title)
        .limit(20)
    )
    albums = (await session.execute(stmt)).unique().scalars().all()
    if not albums:
        return HTMLResponse('<p class="muted" style="font-size:12px">No matching albums.</p>')
    lines = []
    for album in albums:
        ntracks = len(album.tracks)
        lines.append(
            f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--b1)">'
            f'<div style="flex:1;min-width:0">'
            f'<div style="font-size:13px;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{album.title}</div>'
            f'<div style="font-size:11px;color:var(--t3)">{album.artist.name}'
            + (f' · {album.year}' if album.year else '')
            + f' · {ntracks} track{"s" if ntracks != 1 else ""}</div>'
            f'</div>'
            f'<button class="btn btn-sm btn-ghost" style="white-space:nowrap"'
            f' hx-post="/library/albums/{canonical}/merge/{album.id}"'
            f' hx-target="#album-list"'
            f' hx-swap="innerHTML"'
            f' hx-confirm="Merge \'{album.title}\' into the current album? This moves all its tracks and cannot be undone.">'
            f'Merge in ←</button>'
            f'</div>'
        )
    return HTMLResponse('<div style="margin-top:4px">' + ''.join(lines) + '</div>')


@router.get("/list", response_class=HTMLResponse)
async def library_albums_list(
    request: Request,
    q: str = "",
    sort: str = "artist",
    view: str = "",
    offset: int = Query(0, ge=0),
    open_id: str = Query("", alias="open"),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _jl

    # Quality sort happens in SQL (scalar subquery) so offset pagination stays
    # correct — a Python re-sort would only order each page internally.
    _quality_sq = (
        select(func.avg(Track.tag_quality_score))
        .where(Track.album_id == Album.id)
        .scalar_subquery()
    )
    _album_sort_map = {
        "artist":  (Artist.sort_name, Artist.name, Album.year, Album.title),
        "title":   (Album.title, Artist.name),
        "year":    (Album.year.desc().nulls_last(), Album.title, Artist.name),
        "quality": (func.coalesce(_quality_sq, 0.0), Album.title),
    }
    sort_cols = _album_sort_map.get(sort) or _album_sort_map["artist"]

    stmt = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .order_by(*sort_cols, Album.id)  # id tiebreak keeps pages disjoint
        .offset(offset)
        .limit(_LIST_PAGE + 1)
    )

    if q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(Album.title.ilike(pattern) | Artist.name.ilike(pattern))
    albums = (await session.execute(stmt)).unique().scalars().all()
    has_more = len(albums) > _LIST_PAGE
    albums = albums[:_LIST_PAGE]

    # A bookmarked ?open= album may live on a later page — fetch and append it
    # so refresh still restores the expanded detail.
    if open_id and offset == 0 and not any(a.id == open_id for a in albums):
        open_album = (await session.execute(
            select(Album)
            .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
            .where(Album.id == open_id)
        )).unique().scalar_one_or_none()
        if open_album is not None:
            albums.append(open_album)

    # Compute per-album quality from owned tracks (no extra query needed — tracks already loaded)
    album_quality: dict[str, float | None] = {}
    for alb in albums:
        scores = [t.tag_quality_score for t in alb.tracks if t.tag_quality_score is not None]
        album_quality[alb.id] = round(sum(scores) / len(scores), 3) if scores else None

    singles_count = 0
    singles_cover_id: str | None = None
    if not q.strip() and offset == 0:
        singles_count = (await session.execute(
            select(func.count(Track.id)).join(Track.file).where(Track.album_id.is_(None))
        )).scalar_one()
        if singles_count:
            cover_row = (await session.execute(
                select(Track.id)
                .join(Track.file)
                .where(Track.album_id.is_(None), TrackFile.has_cover_art == 1)
                .limit(1)
            )).scalar_one_or_none()
            singles_cover_id = cover_row
    view = _layout_view(request, view, "album_view")
    tmpl = "partials/album_grid.html" if view == "grid" else "partials/album_list.html"
    resp = templates.TemplateResponse(
        request, tmpl,
        {"albums": albums, "q": q, "sort": sort, "album_quality": album_quality,
         "singles_count": singles_count, "singles_cover_id": singles_cover_id,
         "open_id": open_id, "view": view,
         "offset": offset, "has_more": has_more,
         "next_offset": offset + _LIST_PAGE},
    )
    resp.set_cookie("album_view", view, max_age=365 * 24 * 3600, samesite="lax")
    return resp


async def render_album_detail(
    request: Request,
    session: AsyncSession,
    album_id: str,
    *,
    saved: bool = False,
) -> HTMLResponse | None:
    """Load an album fresh and render its detail card; None if the album is gone.

    Shared by the detail route and by mutations that re-render the open card
    afterwards (update-meta, MB link, per-track tag save).
    """
    album = (await session.execute(
        select(Album)
        .options(joinedload(Album.artist),
                 joinedload(Album.tracks).joinedload(Track.file).joinedload(TrackFile.track))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        return None
    # Cover art comes from the first track whose file still exists (sidecar or embedded)
    cover_track = next((t for t in album.tracks if t.file and Path(t.file.path).exists()), None)
    sorted_tracks = sorted(album.tracks, key=lambda t: (t.track_number is None, t.track_number or 0))
    ctx = {"album": album, "sorted_tracks": sorted_tracks, "cover_track": cover_track}
    if saved:
        ctx["saved"] = True
    return templates.TemplateResponse(request, "partials/album_detail.html", ctx)


@router.get("/{album_id}/detail", response_class=HTMLResponse)
async def album_detail(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    resp = await render_album_detail(request, session, album_id)
    if resp is None:
        raise HTTPException(404)
    return resp


@router.post("/{album_id}/update-meta", response_class=HTMLResponse)
async def album_update_meta(
    request: Request,
    album_id: str,
    title: str = Form(""),
    year: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy.orm import joinedload as _jl
    from service.library.cohesion import apply_album_tags

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    # Update DB, then rewrite album/albumartist/year + canonical MB album ID on every
    # track file so Navidrome groups them as one album.
    album.title = title.strip() or album.title
    album.year = int(year) if year.strip().isdigit() else album.year
    album.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await apply_album_tags(album)

    await session.commit()
    await _do_scans()

    resp = await render_album_detail(request, session, album_id, saved=True)
    if resp is None:
        raise HTTPException(404)
    return resp


@router.post("/{album_id}/set-genre", response_class=HTMLResponse)
async def album_set_genre(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Set (or clear) genre on all tracks in an album — DB + file tags."""
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    genre_val = (form.get("genre") or "").strip() or None

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    updated = 0
    for track in album.tracks:
        track.genre = genre_val
        if track.file:
            fp = Path(track.file.path)
            if fp.exists():
                try:
                    await asyncio.to_thread(_write_tags, fp, genre=genre_val or "")
                    updated += 1
                except Exception as exc:
                    logger.warning("album set-genre tag write failed for %s: %s", fp, exc)
    await session.commit()
    await _do_scans()

    label = f'"{genre_val}"' if genre_val else "removed"
    return HTMLResponse(
        f'<span class="badge badge-done">Genre {label} set on {updated} track(s) ✓</span>'
    )


@router.post("/bulk-edit", response_class=HTMLResponse)
async def albums_bulk_edit(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Apply genre and/or year to every track of the selected albums.

    Album-level counterpart of the Browse bulk-edit: same genre/year fields,
    but the selection unit is a whole album. Writes file tags per track and
    keeps the Album/Track DB rows in sync.
    """
    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    album_ids: list[str] = list(form.getlist("album_id"))  # type: ignore[arg-type]
    genre_val = (form.get("genre") or "").strip() or None  # type: ignore[union-attr]
    year_str = (form.get("year") or "").strip()  # type: ignore[union-attr]
    year_val: int | None = int(year_str) if year_str.isdigit() else None

    if not album_ids:
        return _error_badge("No albums selected")
    if genre_val is None and year_val is None:
        return _error_badge("Enter at least one field to update")

    albums = (await session.execute(
        select(Album)
        .options(joinedload(Album.tracks).joinedload(Track.file))
        .where(Album.id.in_(album_ids))
    )).unique().scalars().all()

    tracks_updated = 0
    failed = 0
    for album in albums:
        if year_val is not None:
            album.year = year_val
        album.updated_at = datetime.now(UTC).replace(tzinfo=None)
        for track in album.tracks:
            if genre_val is not None:
                track.genre = genre_val
            if not track.file:
                continue
            fp = Path(track.file.path)
            if not fp.exists():
                continue
            kwargs: dict[str, object] = {}
            if genre_val is not None:
                kwargs["genre"] = genre_val
            if year_val is not None:
                kwargs["year"] = year_val
            try:
                await asyncio.to_thread(_write_tags, fp, **kwargs)
                tracks_updated += 1
            except Exception as exc:
                logger.warning("albums bulk-edit: write_tags failed for %s: %s", fp, exc)
                failed += 1

    await session.commit()
    await _do_scans()

    msg = (f"Updated {len(albums)} album{'s' if len(albums) != 1 else ''} "
           f"({tracks_updated} track{'s' if tracks_updated != 1 else ''})")
    if failed:
        msg += f", {failed} failed"
    return HTMLResponse(f'<span class="badge-ok">{msg} ✓</span>')


async def _fetch_mb_tracklist(album: Album) -> list:
    """Fetch the MB tracklist for a linked album (release group first, release fallback).

    Raises on MB failure — callers decide how to degrade.
    """
    if album.mb_release_group_id:
        from service.metadata.musicbrainz import get_release_group_tracks as _get
        _, _, _, mb_tracks = await asyncio.to_thread(
            _get, album.mb_release_group_id, settings.cache_dir
        )
    else:
        from service.metadata.musicbrainz import get_release_tracks_by_id as _get
        _, _, _, mb_tracks = await asyncio.to_thread(
            _get, album.musicbrainz_release_id, settings.cache_dir
        )
    return mb_tracks


async def _reconcile_mb_tracklist(
    session: AsyncSession,
    local_tracks: list[Track],
    mb_tracks: list,
) -> tuple[list[dict], dict[str, int]]:
    """Annotate the MB tracklist with ownership status against the local album.

    Returns (rows, counts): one row per MB track tagged ``here`` (owned in this
    album), ``elsewhere`` (owned on another album), or ``missing``, plus any
    local track absent from the MB list appended as ``extra``.
    """
    from service.library.cohesion import get_owned_recording_ids as _owned_rids
    from service.search.matcher import title_similarity as _tsim

    # Map recording ID → local track, preferring a file-bearing row. Replacements
    # can leave a fileless ghost Track sharing the same recording ID; without this
    # the ghost could win the slot and bump the real (playable) file to "extra".
    local_by_rid: dict[str, Track] = {}
    for t in local_tracks:
        rid = t.musicbrainz_recording_id
        if not rid:
            continue
        cur = local_by_rid.get(rid)
        if cur is None or (t.file and not cur.file):
            local_by_rid[rid] = t
    local_rids = set(local_by_rid)

    # Recording IDs owned ANYWHERE in the library — those not in this album = "elsewhere"
    mb_recording_ids = [t.recording_id for t in mb_tracks if t.recording_id]
    all_owned_rids = await _owned_rids(session, mb_recording_ids) if mb_recording_ids else set()
    elsewhere_rids = all_owned_rids - local_rids
    mb_rid_set = set(mb_recording_ids)
    mb_titles = [t.title for t in mb_tracks]

    rows: list[dict] = []
    matched_ids: set[str] = set()
    here = elsewhere = missing = 0

    for mt in mb_tracks:
        track = None
        status = None
        if mt.recording_id and mt.recording_id in local_by_rid:
            track = local_by_rid[mt.recording_id]
            status = "here"
        else:
            # Title match against not-yet-matched local tracks (recording ID absent).
            # Prefer a file-bearing candidate so a ghost doesn't claim the slot.
            unmatched = [lt for lt in local_tracks if lt.id not in matched_ids]
            unmatched.sort(key=lambda lt: lt.file is None)  # file-bearing first
            for lt in unmatched:
                if _tsim(mt.title, lt.title) >= 0.80:
                    track, status = lt, "here"
                    break
            if status is None:
                status = "elsewhere" if (mt.recording_id and mt.recording_id in elsewhere_rids) else "missing"

        owner_track_id = None
        if status == "here" and track is not None:
            matched_ids.add(track.id)
            here += 1
        elif status == "elsewhere":
            elsewhere += 1
            owner = (await session.execute(
                select(Track).where(Track.musicbrainz_recording_id == mt.recording_id).limit(1)
            )).scalar_one_or_none()
            owner_track_id = owner.id if owner else None
        else:
            missing += 1

        rows.append({
            "status": status,
            "number": mt.number,
            "disc": mt.disc,
            "title": (track.title if track is not None else mt.title),
            "track": track,
            "recording_id": mt.recording_id,
            "duration_seconds": mt.duration_seconds,
            "owner_track_id": owner_track_id,
        })

    # Genuinely-extra local tracks: unmatched AND not corresponding to any MB
    # track by recording ID or title. A duplicate/ghost of an MB track (same rid
    # or matching title) is NOT "not in MB" — skip it so it isn't mislabeled and
    # doesn't dump a stray track number at the bottom of the list.
    extra = 0
    for lt in local_tracks:
        if lt.id in matched_ids:
            continue
        rid_in_mb = bool(lt.musicbrainz_recording_id and lt.musicbrainz_recording_id in mb_rid_set)
        title_in_mb = any(_tsim(lt.title, mt) >= 0.80 for mt in mb_titles)
        if rid_in_mb or title_in_mb:
            continue  # duplicate of an MB track already shown above
        extra += 1
        rows.append({"status": "extra", "number": None, "disc": None, "title": lt.title, "track": lt})

    return rows, {"here": here, "elsewhere": elsewhere, "missing": missing, "extra": extra}


@router.get("/{album_id}/tracklist", response_class=HTMLResponse)
async def album_tracklist(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Unified album tracklist.

    When the album is linked to MusicBrainz, the MB release tracklist is the
    backbone: each row is tagged ``here`` (owned in this album), ``elsewhere``
    (owned on another album), or ``missing``, and any local track absent from
    the MB list is appended as ``extra``. Unlinked albums degrade to a plain
    owned-track list. Replaces the old three-section layout (local list + full
    MB list + local list again) with one status-annotated list.
    """
    from sqlalchemy.orm import joinedload as _jl

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file), _jl(Album.artist))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        return HTMLResponse('<p class="muted" style="font-size:12px">Album not found.</p>')

    local_tracks = sorted(
        album.tracks, key=lambda t: (t.track_number is None, t.track_number or 0)
    )

    def _local_only(note: str | None = None) -> HTMLResponse:
        # Owned tracks only — no MB status. Used for unlinked albums and as the
        # fallback when MusicBrainz is unavailable, so the user's tracks always show.
        return templates.TemplateResponse(
            request, "partials/album_tracklist.html",
            {"album": album, "local_only": True, "note": note},
        )

    # ── Unlinked: plain owned list, no MB backbone ────────────────────────────
    if not (album.mb_release_group_id or album.musicbrainz_release_id):
        return _local_only()

    # ── Linked: reconcile against the MB tracklist ────────────────────────────
    try:
        mb_tracks = await _fetch_mb_tracklist(album)
    except Exception as exc:
        # MB down/unreachable — keep showing the owned tracks rather than an error.
        logger.warning("album_tracklist: MB fetch failed for %s: %s", album_id, exc)
        return _local_only("MusicBrainz unavailable — showing your tracks only.")
    if not mb_tracks:
        return _local_only("No MusicBrainz tracklist available — showing your tracks only.")

    rows, counts = await _reconcile_mb_tracklist(session, local_tracks, mb_tracks)
    if not rows:
        return HTMLResponse('<p class="muted" style="font-size:12px">No tracks found.</p>')

    total = len(mb_tracks)
    # Persist the MB track count so the album list can show "N/total" without re-fetching
    if album.track_count != total:
        album.track_count = total
        await session.commit()

    return templates.TemplateResponse(
        request, "partials/album_tracklist.html",
        {"album": album, "rows": rows, "linked": True,
         **counts, "total": total,
         "artist_mbid": (album.artist.musicbrainz_artist_id
                         if album.artist and album.artist.musicbrainz_artist_id else "unknown"),
         "release_ref": album.mb_release_group_id or album.musicbrainz_release_id or album.id},
    )


@router.get("/{album_id}/mb-link-search", response_class=HTMLResponse)
async def album_mb_link_search(
    request: Request,
    album_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search MusicBrainz for release groups to link to this album."""
    from sqlalchemy.orm import joinedload as _jl
    album = (await session.execute(
        select(Album).options(_jl(Album.artist)).where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    if not q:
        q = f"{album.artist.name} {album.title}" if album.artist else album.title

    from service.metadata.musicbrainz import search_release_groups as _search_rgs
    results = await asyncio.to_thread(
        _search_rgs,
        album.artist.name if album.artist else "",
        album.title,
        8,
        settings.cache_dir,
    )

    # MB fields are external free text — the Jinja partial autoescapes them.
    return templates.TemplateResponse(
        request, "partials/mb_rg_search_results.html",
        {"results": results, "album_id": album_id},
    )


@router.post("/{album_id}/link-mb-rg", response_class=HTMLResponse)
async def album_link_mb_rg(
    request: Request,
    album_id: str,
    release_group_id: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save a MusicBrainz release group ID to this album and return the refreshed detail card."""
    album = (await session.execute(
        select(Album).where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)
    album.mb_release_group_id = release_group_id
    await session.commit()

    resp = await render_album_detail(request, session, album_id, saved=True)
    if resp is None:
        raise HTTPException(404)
    return resp


def _match_local_to_mb_slots(album: Album, mb_tracks: list) -> list[tuple[Track, object]]:
    """Match owned tracks to MB tracklist slots: recording ID first, then title
    similarity among unclaimed slots (same fallback the tracklist reconciliation uses)."""
    from service.search.matcher import title_similarity as _tsim

    by_rid = {t.recording_id: t for t in mb_tracks if t.recording_id}
    used: set[int] = set()
    matches: list[tuple[Track, object]] = []
    for track in album.tracks:
        rid = track.musicbrainz_recording_id
        mt = by_rid.get(rid) if rid else None
        if mt is not None and id(mt) not in used:
            used.add(id(mt))
            matches.append((track, mt))
    matched_ids = {t.id for t, _ in matches}
    for track in album.tracks:
        if track.id in matched_ids:
            continue
        best, best_s = None, 0.0
        for mt in mb_tracks:
            if id(mt) in used:
                continue
            s = _tsim(track.title, mt.title)
            if s > best_s:
                best_s, best = s, mt
        if best is not None and best_s >= 0.80:
            used.add(id(best))
            matches.append((track, best))
    return matches


async def _apply_disc_numbers(
    album: Album, matches: list[tuple[Track, object]]
) -> tuple[int, int]:
    """Write disc/track numbers from matched MB slots to tags + DB rows and
    rename files into the disc-aware layout. Returns (fixed, moved). Mutates
    rows but does not commit — the caller owns the session."""
    from service.library.layout import track_path as _tp
    from service.library.tagger import write_tags as _wt
    from service.library.writer import atomic_place as _ap

    albumartist = album.artist.name if album.artist else "Unknown"
    fixed = moved = 0
    for track, mt in matches:
        new_disc = mt.disc
        new_num = mt.number or track.track_number
        fp = Path(track.file.path) if track.file else None
        if fp is None or not fp.exists():
            track.disc_number = new_disc
            track.track_number = new_num
            continue
        try:
            await asyncio.to_thread(_wt, fp, track_number=new_num, disc_number=new_disc)
        except Exception as exc:
            logger.warning("fix-discs: tag write failed for %s: %s", fp, exc)
            continue
        if track.disc_number != new_disc or track.track_number != new_num:
            fixed += 1
        track.disc_number = new_disc
        track.track_number = new_num
        dst = _tp(
            settings.music_dir,
            artist=(track.artist.name if track.artist else albumartist),
            album=album.title,
            year=album.year,
            track_number=new_num,
            disc_number=new_disc,
            title=track.title,
            ext=fp.suffix.lstrip("."),
            albumartist=albumartist,
        )
        if dst != fp and not dst.exists():
            try:
                await asyncio.to_thread(_ap, fp, dst)
                # Keep the .lrc lyrics sidecar next to its audio file
                lrc = fp.with_suffix(".lrc")
                if lrc.exists():
                    try:
                        lrc.rename(dst.with_suffix(".lrc"))
                    except OSError:
                        pass
                track.file.path = str(dst)
                moved += 1
            except Exception as exc:
                logger.warning("fix-discs: rename failed %s → %s: %s", fp, dst, exc)
    return fixed, moved


@router.post("/{album_id}/fix-discs", response_class=HTMLResponse)
async def album_fix_discs(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Repair disc/track numbers on a multi-disc album from its MB tracklist.

    Albums acquired before disc awareness existed had every disc flattened with
    per-disc positions and no DISCNUMBER tag — two "track 1" rows, two "track 2"
    rows, and so on. Matches owned tracks to the MB tracklist (recording ID
    first, title fallback), writes disc + track number tags, and renames files
    into the disc-aware layout (disc 2 track 1 → "201 - Title.ext").
    """
    from sqlalchemy.orm import joinedload as _jl

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file),
                 _jl(Album.tracks).joinedload(Track.artist))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)
    if not (album.mb_release_group_id or album.musicbrainz_release_id):
        return _error_badge("Not linked to MusicBrainz — link the album first")

    try:
        mb_tracks = await _fetch_mb_tracklist(album)
    except Exception as exc:
        return _error_badge(f"MusicBrainz unavailable: {exc}")

    if not any(t.disc for t in mb_tracks):
        return HTMLResponse('<span class="badge badge-done">MusicBrainz lists a single disc — nothing to fix ✓</span>')

    matches = _match_local_to_mb_slots(album, mb_tracks)
    if not matches:
        return _error_badge("No owned track matched the MB tracklist")

    fixed, moved = await _apply_disc_numbers(album, matches)
    await session.commit()
    await _do_scans()
    return HTMLResponse(
        f'<span class="badge badge-done">Disc numbers written to {fixed} track(s), '
        f'{moved} file(s) renamed ✓ — reopen the album to see discs</span>'
    )


async def _embed_album_art(session: AsyncSession, album_id: str, art: bytes) -> int:
    """Embed art into every track file of an album + write the cover.jpg sidecar.

    Returns the number of files embedded; raises HTTPException(404) for an
    unknown album. Commits the session and triggers scans — shared back half
    of the album apply-art and album cover-upload routes.
    """
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags

    album = (await session.execute(
        select(Album)
        .options(joinedload(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    album_dir: Path | None = None
    embedded = 0
    for track in album.tracks:
        if not track.file:
            continue
        fp = Path(track.file.path)
        if not fp.exists():
            continue
        album_dir = album_dir or fp.parent
        try:
            await asyncio.to_thread(_write_tags, fp, artwork_bytes=art)
            hca = await asyncio.to_thread(_has_cover_art, fp)
            track.file.has_cover_art = hca
            embedded += 1
        except Exception as exc:
            logger.debug("album art embed failed for %s: %s", fp, exc)

    if album_dir:
        write_cover_jpg(album_dir, art)

    await session.commit()
    await _do_scans()
    return embedded


@router.post("/{album_id}/apply-art", response_class=HTMLResponse)
async def apply_art_to_album(
    request: Request,
    album_id: str,
    art_url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download art from a URL and embed it in all tracks of an album."""
    art, err = await _fetch_user_art(art_url)
    if err is not None:
        return err

    embedded = await _embed_album_art(session, album_id, art)
    return HTMLResponse(f'<span class="badge badge-done">Art applied to {embedded} track(s) ✓</span>')


@router.post("/{album_id}/cover/upload", response_class=HTMLResponse)
async def upload_album_cover(
    request: Request,
    album_id: str,
    cover: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Embed a user-supplied image as cover art for all tracks in an album + sidecar."""
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small

    if not cover.content_type or not cover.content_type.startswith("image/"):
        return _error_badge("Not an image file")

    art = await cover.read()
    if not art:
        return _error_badge("Empty file")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return _error_badge("Image too small — must be at least 300×300 px")

    embedded = await _embed_album_art(session, album_id, art)
    return HTMLResponse(f'<span class="badge badge-done">Cover saved to {embedded} track(s) ✓</span>')


@router.delete("/{album_id}", response_class=HTMLResponse)
async def delete_album(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Trash all files in an album and remove its DB records."""
    from sqlalchemy.orm import joinedload as _jl

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    album_dirs: set[Path] = set()
    for track in album.tracks:
        if track.file:
            fp = Path(track.file.path)
            album_dirs.add(fp.parent)
            if fp.exists():
                try:
                    safe_trash(fp, settings.music_dir / ".trash")
                except Exception as exc:
                    logger.warning("Trash failed for %s: %s", fp, exc)
            await session.delete(track.file)
        await session.delete(track)

    await session.delete(album)
    await session.commit()

    for d in album_dirs:
        _trash_empty_album_dir(d, settings.music_dir / ".trash")

    await _do_scans()

    return HTMLResponse("")


@router.post("/{album_id}/cover/fetch", response_class=HTMLResponse)
async def fetch_album_cover(
    request: Request,
    album_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Fetch cover art from Cover Art Archive and embed in all tracks + write cover.jpg."""
    from sqlalchemy.orm import joinedload as _jl
    from service.library.tagger import has_cover_art as _has_cover_art, write_cover_jpg, write_tags as _write_tags
    from service.metadata.artwork import fetch_from_caa

    album = (await session.execute(
        select(Album)
        .options(_jl(Album.tracks).joinedload(Track.file))
        .where(Album.id == album_id)
    )).unique().scalar_one_or_none()
    if album is None:
        raise HTTPException(404)

    # Find the first track with a real file to locate the album dir and release ID
    release_id: str | None = album.musicbrainz_release_id
    album_dir: Path | None = None
    for track in album.tracks:
        if track.file and Path(track.file.path).exists():
            album_dir = Path(track.file.path).parent
            if not release_id:
                release_id = _read_mb_release_id(Path(track.file.path))
            break

    # If still no release ID, resolve one from the release group (cached)
    if not release_id and album.mb_release_group_id:
        try:
            from service.metadata.musicbrainz import get_release_group_tracks
            _, release_id, _, _ = await asyncio.to_thread(
                get_release_group_tracks, album.mb_release_group_id, settings.cache_dir
            )
        except Exception as exc:
            logger.debug("release-group tracklist lookup for release id failed: %s", exc)

    if not release_id:
        return _error_badge("No MusicBrainz release ID — cannot fetch cover")
    if album_dir is None:
        return _error_badge("No files found for this album")

    art = await fetch_from_caa(release_id)
    if art is None:
        return _error_badge("Cover not found on Cover Art Archive")

    try:
        write_cover_jpg(album_dir, art)
    except Exception as exc:
        return _error_badge(f"Write failed: {exc}")

    # Embed art in every track file and update DB
    embedded = 0
    for track in album.tracks:
        if not track.file:
            continue
        fp = Path(track.file.path)
        if not fp.exists():
            continue
        try:
            await asyncio.to_thread(_write_tags, fp, artwork_bytes=art)
            track.file.has_cover_art = await asyncio.to_thread(_has_cover_art, fp)
            embedded += 1
        except Exception as exc:
            logger.debug("fetch_album_cover: embed failed for %s: %s", fp, exc)

    await session.commit()
    await _do_scans()

    return HTMLResponse(f'<span class="badge-ok">Cover saved to {embedded} track(s) ✓</span>')


@router.post("/{canonical_id}/merge/{source_id}", response_class=HTMLResponse)
async def merge_album(
    request: Request,
    canonical_id: str,
    source_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Move source album files into canonical album folder and reassign all DB records.

    Thin route over :func:`service.library.cohesion.merge_albums`, which does the
    filesystem move + tag normalization + DB merge. Returns the refreshed album list.
    """
    from sqlalchemy.orm import joinedload as _jl

    from service.library.cohesion import merge_albums as _merge_albums

    await _merge_albums(
        session, canonical_id, source_id,
        settings.music_dir / ".trash", settings.music_dir,
    )
    await session.commit()

    await _do_scans()

    # Return the refreshed album list so the UI updates immediately
    stmt2 = (
        select(Album)
        .join(Album.artist)
        .options(_jl(Album.artist), _jl(Album.tracks).joinedload(Track.file))
        .order_by(Artist.name, Album.year, Album.title)
        .limit(300)
    )
    albums = (await session.execute(stmt2)).unique().scalars().all()
    album_quality: dict[str, float | None] = {}
    for alb in albums:
        scores = [t.tag_quality_score for t in alb.tracks if t.tag_quality_score is not None]
        album_quality[alb.id] = round(sum(scores) / len(scores), 3) if scores else None
    view = _layout_view(request, "", "album_view")
    tmpl = "partials/album_grid.html" if view == "grid" else "partials/album_list.html"
    return templates.TemplateResponse(
        request, tmpl,
        {"albums": albums, "q": "", "album_quality": album_quality, "view": view},
    )
