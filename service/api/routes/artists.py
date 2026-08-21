"""Artist pages, images, merge, acquire-missing."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from service.acquisition.queue import arq_pool, enqueue_album_from_mb
from service.api.shared import (
    _LIST_PAGE,
    _acquisition_batch_receipt,
    _do_scans,
    _error_badge,
    _layout_view,
    _resize_cover,
    templates,
)
from service.config import settings
from service.db.schema import Album, Artist, Track, TrackFile
from service.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/library/artists/merge-candidates", response_class=HTMLResponse)
async def artist_merge_candidates(
    request: Request,
    canonical: str = "",
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Return artist rows as merge-into-canonical candidates with action buttons."""
    if not q.strip():
        return HTMLResponse('<p class="muted" style="font-size:12px">Type to search…</p>')
    pattern = f"%{q.strip()}%"
    stmt = (
        select(Artist)
        .where(Artist.name.ilike(pattern))
        .where(Artist.id != canonical)
        .order_by(Artist.name)
        .limit(20)
    )
    artists = (await session.execute(stmt)).scalars().all()
    if not artists:
        return HTMLResponse('<p class="muted" style="font-size:12px">No matching artists.</p>')

    track_counts: dict[str, int] = {}
    for a in artists:
        cnt = (await session.execute(
            select(func.count()).select_from(Track).where(Track.artist_id == a.id)
        )).scalar_one()
        track_counts[a.id] = cnt

    lines = []
    for a in artists:
        cnt = track_counts[a.id]
        lines.append(
            f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--b1)">'
            f'<div style="flex:1;min-width:0">'
            f'<div style="font-size:13px;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{a.name}</div>'
            f'<div style="font-size:11px;color:var(--t3)">{cnt} track{"s" if cnt != 1 else ""}'
            + (f' · MB: {a.musicbrainz_artist_id[:8]}…' if a.musicbrainz_artist_id else '')
            + '</div>'
            f'</div>'
            f'<button class="btn btn-sm btn-ghost" style="white-space:nowrap"'
            f' hx-post="/library/artists/{canonical}/merge/{a.id}"'
            f' hx-target="#merge-artist-result"'
            f' hx-swap="innerHTML"'
            f' hx-confirm="Merge \'{a.name}\' into this artist? All their tracks and albums will be reassigned. This cannot be undone.">'
            f'Merge in ←</button>'
            f'</div>'
        )
    return HTMLResponse('<div style="margin-top:4px">' + ''.join(lines) + '</div>')


@router.get("/library/artists/{artist_id}", response_class=HTMLResponse)
async def artist_page(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Artist page: owned tracks grouped by album + MB discography if MBID known."""
    from sqlalchemy.orm import joinedload as _jl

    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)

    # Owned tracks, grouped into albums
    tracks = (await session.execute(
        select(Track)
        .options(_jl(Track.album), _jl(Track.file))
        .where(Track.artist_id == artist_id)
        .order_by(Album.year.nullslast(), Album.title.nullslast(), Track.disc_number.nullsfirst(), Track.track_number.nullslast(), Track.title)  # type: ignore[union-attr]
        .outerjoin(Track.album)
        .join(Track.file)
    )).unique().scalars().all()

    # Group tracks by album
    from collections import OrderedDict
    albums_map: dict[str | None, list[Track]] = OrderedDict()
    for t in tracks:
        key = t.album_id
        if key not in albums_map:
            albums_map[key] = []
        albums_map[key].append(t)

    albums_list = []
    for album_id_key, atracks in albums_map.items():
        album_obj = atracks[0].album if atracks else None
        albums_list.append({
            "album": album_obj,
            "tracks": atracks,
        })

    # MB discography (if MBID known)
    mb_release_groups: list[dict] = []
    if artist.musicbrainz_artist_id:
        try:
            from service.core.normalize import normalize as _norm
            from service.metadata.musicbrainz import get_artist_release_groups
            _, rgs = await asyncio.to_thread(
                get_artist_release_groups, artist.musicbrainz_artist_id, settings.cache_dir
            )
            owned_album_titles = {_norm(a["album"].title) for a in albums_list if a["album"]}
            # Map normalised album title → owned track count for the completion indicator
            owned_title_counts: dict[str, int] = {
                _norm(a["album"].title): len(a["tracks"])
                for a in albums_list if a["album"]
            }
            # → a local track id per owned album so its cover thumb is local art
            owned_title_covers: dict[str, str] = {
                _norm(a["album"].title): a["tracks"][0].id
                for a in albums_list if a["album"] and a["tracks"]
            }
            for rg in rgs:
                owned = _norm(rg.title) in owned_album_titles
                mb_release_groups.append({
                    "release_group_id": rg.release_group_id,
                    "title": rg.title,
                    "year": rg.year,
                    "release_type": rg.release_type,
                    "owned": owned,
                    "owned_track_count": owned_title_counts.get(_norm(rg.title), 0),
                    "cover_track_id": owned_title_covers.get(_norm(rg.title)),
                })
        except Exception as exc:
            logger.debug("Artist page MB lookup failed: %s", exc)

    return templates.TemplateResponse(
        request, "artist_page.html",
        {
            "active": "library",
            "artist": artist,
            "albums_list": albums_list,
            "mb_release_groups": mb_release_groups,
            "total_tracks": len(tracks),
        },
    )


@router.post("/library/artists/{artist_id}/delete", response_class=HTMLResponse)
async def delete_artist(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Delete an artist that has no tracks.  Removes empty albums first."""
    from sqlalchemy import delete as _sa_del

    # Purge orphaned Track rows (no TrackFile) — these ghost rows can block
    # deletion even though the artist page shows 0 tracks (it inner-joins files).
    orphan_ids = (await session.execute(
        select(Track.id)
        .outerjoin(TrackFile, TrackFile.track_id == Track.id)
        .where(Track.artist_id == artist_id)
        .where(TrackFile.id.is_(None))
    )).scalars().all()
    if orphan_ids:
        await session.execute(_sa_del(Track).where(Track.id.in_(orphan_ids)))

    track_count = (await session.execute(
        select(func.count(Track.id)).where(Track.artist_id == artist_id)
    )).scalar_one()
    if track_count > 0:
        raise HTTPException(400, "Artist still has tracks")

    # Delete empty albums for this artist
    old_albums = (await session.execute(
        select(Album).where(Album.artist_id == artist_id)
    )).scalars().all()
    for alb in old_albums:
        alb_tracks = (await session.execute(
            select(func.count(Track.id)).where(Track.album_id == alb.id)
        )).scalar_one()
        if alb_tracks == 0:
            await session.execute(_sa_del(Album).where(Album.id == alb.id))

    await session.execute(_sa_del(Artist).where(Artist.id == artist_id))
    await session.commit()

    return HTMLResponse("", status_code=200, headers={"HX-Redirect": "/library/artists"})


@router.post("/library/artists/{artist_id}/update", response_class=HTMLResponse)
async def update_artist(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Update artist name, sort name, and/or MB artist ID.  Writes tags to all track files."""
    from sqlalchemy.orm import joinedload as _jl

    from service.library.tagger import write_tags as _write_tags

    form = await request.form()
    name_val = (form.get("name") or "").strip()
    sort_name_val = (form.get("sort_name") or "").strip() or None
    mb_artist_id_val = (form.get("musicbrainz_artist_id") or "").strip() or None

    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    if not name_val:
        raise HTTPException(400, "Artist name required")

    name_changed = name_val != artist.name
    sort_changed = sort_name_val != artist.sort_name

    artist.name = name_val
    artist.sort_name = sort_name_val
    artist.musicbrainz_artist_id = mb_artist_id_val
    artist.updated_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()

    # Write tags to all track files if name or sort_name changed
    if name_changed or sort_changed:
        tracks = (await session.execute(
            select(Track).options(_jl(Track.file)).where(Track.artist_id == artist_id)
        )).unique().scalars().all()
        for t in tracks:
            if t.file:
                fp = Path(t.file.path)
                if fp.exists():
                    kwargs: dict = {}
                    if name_changed:
                        kwargs["artist"] = name_val
                        kwargs["albumartist"] = name_val
                    if sort_changed:
                        kwargs["artist_sort"] = sort_name_val or ""
                    try:
                        await asyncio.to_thread(_write_tags, fp, **kwargs)
                    except Exception as exc:
                        logger.warning("update_artist tag write failed for %s: %s", fp, exc)

    await _do_scans()

    # Re-render the artist page header section
    return HTMLResponse("", status_code=200, headers={"HX-Redirect": f"/library/artists/{artist_id}"})


@router.post("/library/artists/{canonical_id}/merge/{source_id}", response_class=HTMLResponse)
async def merge_artist(
    request: Request,
    canonical_id: str,
    source_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Merge source artist into canonical.

    Thin route over :func:`service.library.cohesion.merge_artists`, which does
    the DB reassignment + tag rewrite + filesystem moves (mirrors merge_album →
    cohesion.merge_albums).
    """
    from service.library.cohesion import merge_artists as _merge_artists

    result = await _merge_artists(session, canonical_id, source_id, settings.music_dir)
    if result is None:
        raise HTTPException(404)
    await session.commit()

    await _do_scans()

    return HTMLResponse(
        "",
        status_code=200,
        headers={"HX-Redirect": f"/library/artists/{canonical_id}"},
    )


# Auto-fetched artist portraits (Navidrome-style external agent behaviour):
# throttle concurrent Deezer lookups and remember misses so a library page full
# of artists doesn't hammer the API on every render.
_artist_img_fetch_sem = asyncio.Semaphore(3)


_ARTIST_IMG_MISS_TTL_SECONDS = 7 * 24 * 3600


def _artist_img_cache_paths(name: str) -> tuple[Path, Path]:
    import hashlib
    key = hashlib.sha1(name.strip().lower().encode()).hexdigest()
    base = settings.cache_dir / "artist_images"
    return base / f"{key}.jpg", base / f"{key}.miss"


async def _auto_artist_image(name: str) -> Path | None:
    """Best-effort cached artist portrait from Deezer for artists with no artist.jpg.

    Navidrome shows artist images via its external agents even when no file
    exists on disk; this mirrors that so the audioreap library doesn't look
    emptier than Navidrome. Cached in /cache/artist_images (never in /music —
    the user's explicit "Change image" flow is what writes artist.jpg). Misses
    are cached with a TTL so absent artists are retried only weekly.
    """

    import httpx

    from service.search.matcher import artist_similarity

    jpg, miss = _artist_img_cache_paths(name)
    if jpg.exists():
        return jpg
    try:
        if miss.exists() and (time.time() - miss.stat().st_mtime) < _ARTIST_IMG_MISS_TTL_SECONDS:
            return None
    except OSError:
        pass

    async with _artist_img_fetch_sem:
        if jpg.exists():  # another request fetched it while we waited
            return jpg
        url: str | None = None
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    "https://api.deezer.com/search/artist",
                    params={"q": name, "limit": 5},
                )
                resp.raise_for_status()
                for item in resp.json().get("data", []):
                    pic = item.get("picture_xl") or item.get("picture_big") or ""
                    if not pic or "default_artist" in pic:
                        continue
                    if artist_similarity(name, item.get("name") or "") >= 0.85:
                        url = pic
                        break
                if url:
                    img = await client.get(url)
                    img.raise_for_status()
                    jpg.parent.mkdir(parents=True, exist_ok=True)
                    tmp = jpg.with_suffix(".tmp")
                    tmp.write_bytes(img.content)
                    tmp.replace(jpg)
                    return jpg
        except Exception as exc:
            logger.debug("Auto artist image fetch failed for %r: %s", name, exc)
        try:
            jpg.parent.mkdir(parents=True, exist_ok=True)
            miss.touch()
        except OSError:
            pass
        return None


@router.get("/library/artists/{artist_id}/image", response_class=HTMLResponse)
async def artist_image(
    artist_id: str,
    size: int | None = Query(None, ge=32, le=512),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the artist's portrait: artist.jpg from /music when the user saved
    one, else an auto-fetched cached image (same sources Navidrome's agents use),
    else 204.

    204 (not 404) when nothing exists: the <img> tags that request this fall
    back via onerror either way, but a 2xx keeps the browser console clean.

    ?size=N serves a disk-cached thumbnail like the track cover-art route —
    the artist grid must use it so a screenful of cells doesn't re-download
    full portraits. Regenerated when the source image is newer than the thumb.
    """
    from fastapi.responses import FileResponse, Response
    artist = await session.get(Artist, artist_id)
    if artist is None:
        return Response(status_code=204)
    img_path = settings.music_dir / artist.name / "artist.jpg"
    src = img_path if img_path.exists() else await _auto_artist_image(artist.name)
    if src is None:
        return Response(status_code=204)
    if size is not None:
        thumb_headers = {"Cache-Control": "public, max-age=600"}
        thumb_path = settings.cache_dir / "thumbs" / f"artist_{artist_id}_{size}.jpg"
        if thumb_path.exists() and thumb_path.stat().st_mtime >= src.stat().st_mtime:
            data = await asyncio.to_thread(thumb_path.read_bytes)
            return Response(content=data, media_type="image/jpeg", headers=thumb_headers)
        art = await asyncio.to_thread(src.read_bytes)
        data = await asyncio.to_thread(_resize_cover, art, size, thumb_path)
        if data:
            return Response(content=data, media_type="image/jpeg", headers=thumb_headers)
        # resize failed — fall through to full-size art
    return FileResponse(str(src), media_type="image/jpeg")


@router.get("/library/artists/{artist_id}/mb-search", response_class=HTMLResponse)
async def artist_mb_search(
    request: Request,
    artist_id: str,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search MusicBrainz for artists by name; returns clickable candidates."""
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    search_name = q.strip() or artist.name
    safe_id = artist_id.replace(":", "_")
    try:
        import musicbrainzngs as _mb
        _mb.set_useragent("audioreap", "1.0")
        result = await asyncio.to_thread(
            lambda: _mb.search_artists(artist=search_name, limit=6)
        )
        candidates = []
        for a in result.get("artist-list", []):
            candidates.append({
                "mbid": a.get("id", ""),
                "name": a.get("name", ""),
                "sort_name": a.get("sort-name", ""),
                "type": a.get("type", ""),
                "score": a.get("ext:score", ""),
                "disambiguation": a.get("disambiguation", ""),
            })
    except Exception as exc:
        return _error_badge(f"MB search failed: {exc}")

    if not candidates:
        return HTMLResponse('<p class="muted" style="font-size:12px">No results.</p>')

    return templates.TemplateResponse(
        request, "partials/artist_mb_candidates.html",
        {"candidates": candidates, "input_id": f"artist-mb-id-{safe_id}"},
    )


_ARTIST_IMG_PAGE_SIZE = 10


@router.get("/library/artists/{artist_id}/image-search", response_class=HTMLResponse)
async def artist_image_search(
    request: Request,
    artist_id: str,
    q: str = "",
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Search Deezer for artist images (no API key required)."""
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)
    search_name = q.strip() or artist.name
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.deezer.com/search/artist",
                params={"q": search_name, "limit": _ARTIST_IMG_PAGE_SIZE, "index": offset},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return _error_badge(f"Image search failed: {exc}")

    results = [
        {"name": item["name"], "image_url": item.get("picture_xl") or item.get("picture_medium", ""), "deezer_id": item["id"]}
        for item in data.get("data", [])
        if item.get("picture_medium") and "default_artist" not in item.get("picture_medium", "")
    ]
    has_more = len(results) >= _ARTIST_IMG_PAGE_SIZE
    return templates.TemplateResponse(
        request, "partials/artist_image_candidates.html",
        {
            "artist_id": artist_id,
            "safe_id": artist_id.replace(":", "_"),
            "results": results,
            "q": search_name,
            "offset": offset,
            "next_offset": offset + _ARTIST_IMG_PAGE_SIZE if has_more else None,
        },
    )


@router.post("/library/artists/{artist_id}/save-artist-image", response_class=HTMLResponse)
async def save_artist_image(
    request: Request,
    artist_id: str,
    image_url: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Download an artist image and save as artist.jpg in the artist's music folder."""
    if not image_url:
        raise HTTPException(400, "image_url required")
    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)

    artist_dir = settings.music_dir / artist.name
    artist_dir.mkdir(parents=True, exist_ok=True)
    img_path = artist_dir / "artist.jpg"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            img_path.write_bytes(resp.content)
    except Exception as exc:
        return _error_badge(f"Download failed: {exc}", level="fail")

    # Trigger Navidrome rescan so the new image is picked up
    await _do_scans()

    cache_bust = int(datetime.now(UTC).timestamp())
    return HTMLResponse(
        f'<img src="/library/artists/{artist_id}/image?v={cache_bust}" '
        f'style="width:80px;height:80px;object-fit:cover;border-radius:8px;display:block;margin-bottom:6px" '
        f'alt="{artist.name}">'
        f'<p style="font-size:12px;color:var(--success)">✓ Artist image saved — Navidrome rescan triggered.</p>'
    )


@router.post("/library/artists/{artist_id}/image/upload", response_class=HTMLResponse)
async def upload_artist_image(
    request: Request,
    artist_id: str,
    image: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Save a user-uploaded portrait as artist.jpg in the artist's music folder."""
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small

    if not image.content_type or not image.content_type.startswith("image/"):
        return _error_badge("Not an image file", level="fail")
    art = await image.read()
    if not art:
        return _error_badge("Empty file", level="fail")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return _error_badge("Image too small — must be at least 300×300 px", level="fail")

    artist = await session.get(Artist, artist_id)
    if artist is None:
        raise HTTPException(404)

    artist_dir = settings.music_dir / artist.name
    artist_dir.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread((artist_dir / "artist.jpg").write_bytes, art)
    except OSError as exc:
        return _error_badge(f"Save failed: {exc}", level="fail")

    # Trigger Navidrome rescan so the new image is picked up
    await _do_scans()

    cache_bust = int(datetime.now(UTC).timestamp())
    return HTMLResponse(
        f'<img src="/library/artists/{artist_id}/image?v={cache_bust}" '
        f'style="width:80px;height:80px;object-fit:cover;border-radius:8px;display:block;margin-bottom:6px" '
        f'alt="{artist.name}">'
        f'<p style="font-size:12px;color:var(--success)">✓ Artist image saved — Navidrome rescan triggered.</p>'
    )


@router.post("/artist/{artist_id}/acquire-missing", response_class=HTMLResponse)
async def artist_acquire_missing(
    request: Request,
    artist_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue acquire_album_from_mb for every un-owned release group for this artist."""
    from service.db.schema import Artist as _Artist
    artist = await session.get(_Artist, artist_id)
    if artist is None or not artist.musicbrainz_artist_id:
        raise HTTPException(404)

    try:
        from service.core.normalize import normalize as _norm
        from service.metadata.musicbrainz import get_artist_release_groups
        _, rgs = await asyncio.to_thread(
            get_artist_release_groups, artist.musicbrainz_artist_id, settings.cache_dir
        )
    except Exception as exc:
        return _error_badge(f"MB lookup failed: {exc}")

    # Find which release groups are already owned
    owned_albums = (await session.execute(
        select(Album).join(Album.tracks).join(Track.artist).where(Artist.id == artist_id)
    )).unique().scalars().all()
    owned_titles = {_norm(a.title) for a in owned_albums}
    unowned = [rg for rg in rgs if _norm(rg.title) not in owned_titles]

    if not unowned:
        return HTMLResponse('<span class="badge-done">All release groups already owned ✓</span>')

    from service.acquisition.album_pipeline import create_or_get_active_album_job
    from service.core.models import AlbumCandidate
    from service.db.schema import AlbumAcquisitionJob

    batches: list[tuple[str, str, bool]] = []
    for rg in unowned:
        candidate = AlbumCandidate(
            provider="ytdlp",
            provider_ref=f"mbid:{rg.release_group_id}",
            album_title=rg.title,
            album_artist=artist.name,
            tracks=[],
        )
        album_job_id, created = await create_or_get_active_album_job(
            session,
            provider_name="ytdlp",
            album_ref=candidate.provider_ref,
            album_candidate=candidate,
            query=f"{artist.name} — {rg.title}",
        )
        batches.append((album_job_id, rg.release_group_id, created))
    await session.commit()

    queued_ids = {
        album_job_id for album_job_id, _release_group_id, created in batches
        if not created
    }
    failed_ids: list[str] = []
    try:
        async with arq_pool() as redis:
            for album_job_id, release_group_id, created in batches:
                if not created:
                    continue
                try:
                    await enqueue_album_from_mb(
                        redis,
                        album_job_id,
                        release_group_id=release_group_id,
                        artist_name=artist.name,
                    )
                    queued_ids.add(album_job_id)
                except Exception:
                    failed_ids.append(album_job_id)
    except Exception:
        failed_ids.extend(
            album_job_id for album_job_id, _release_group_id, created in batches
            if created
            and album_job_id not in queued_ids and album_job_id not in failed_ids
        )

    if failed_ids:
        now = datetime.now(UTC).replace(tzinfo=None)
        failed_rows = (await session.execute(
            select(AlbumAcquisitionJob).where(AlbumAcquisitionJob.id.in_(failed_ids))
        )).scalars().all()
        for row in failed_rows:
            row.state = "failed"
            row.updated_at = now
        await session.commit()

    first_id = batches[0][0]
    return _acquisition_batch_receipt(
        request,
        batch_id=first_id,
        title=f"{artist.name} discography",
        queued_count=len(queued_ids),
        owned_count=len(rgs) - len(unowned),
        failed_count=len(failed_ids),
        jobs_anchor=f"album-{first_id}",
        unit="release",
        retry_url="/discography/retry-albums" if failed_ids else None,
        retry_ids=failed_ids,
        failed_items=[
            {
                "id": album_job_id,
                "title": next(
                    (rg.title for rg in unowned if rg.release_group_id == release_group_id),
                    "Album",
                ),
                "artist": artist.name,
            }
            for album_job_id, release_group_id, _created in batches
            if album_job_id in failed_ids
        ],
    )


@router.get("/library/artists", response_class=HTMLResponse)
async def library_artists_page(
    request: Request,
    q: str = "",
    sort: str = "name",
    view: str = "",
    offset: int = Query(0, ge=0),
    embed: bool = Query(False),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from sqlalchemy import func as _func

    _artist_sort_map = {
        "name":   (Artist.sort_name, Artist.name),
        "tracks": (_func.count(Track.id.distinct()).desc(),),
        "albums": (_func.count(Album.id.distinct()).desc(), Artist.sort_name),
    }
    sort_cols = _artist_sort_map.get(sort) or _artist_sort_map["name"]

    stmt = (
        select(
            Artist,
            _func.count(Track.id.distinct()).label("track_count"),
            _func.count(Album.id.distinct()).label("album_count"),
        )
        .outerjoin(Artist.tracks)
        .outerjoin(Track.album)
        .group_by(Artist.id)
        .order_by(*sort_cols, Artist.id)  # id tiebreak keeps pages disjoint
        .offset(offset)
        .limit(_LIST_PAGE + 1)
    )
    if q.strip():
        stmt = stmt.where(Artist.name.ilike(f"%{q.strip()}%"))
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > _LIST_PAGE
    artists = [
        {"artist": r.Artist, "track_count": r.track_count, "album_count": r.album_count}
        for r in rows[:_LIST_PAGE]
    ]
    view = _layout_view(request, view, "artist_view")
    ctx = {"active": "library", "artists": artists, "q": q, "sort": sort, "view": view,
           "offset": offset, "has_more": has_more, "next_offset": offset + _LIST_PAGE}
    # embed=1: full view content for in-place loading on the /library page.
    if embed:
        resp = templates.TemplateResponse(request, "partials/view_artists.html", ctx)
    # HTMX partial reload (search form / view toggle): only the list block.
    elif request.headers.get("HX-Request"):
        tmpl = "partials/artist_grid.html" if view == "grid" else "partials/artist_list.html"
        resp = templates.TemplateResponse(request, tmpl, ctx)
    else:
        resp = templates.TemplateResponse(request, "library_artists.html", ctx)
    resp.set_cookie("artist_view", view, max_age=365 * 24 * 3600, samesite="lax")
    return resp
