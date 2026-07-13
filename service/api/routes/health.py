"""Library health: dupes, splits, missing data, artist credits, batch fixes."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from service.config import settings
from service.db.schema import Album, Artist, Track, TrackFile
from service.library.writer import safe_trash
from service.db.session import get_session
from service.library.writer import trash_empty_album_dir as _trash_empty_album_dir

from service.acquisition.queue import arq_pool
from service.api.shared import _error_badge, _get_track_with_file, templates

logger = logging.getLogger(__name__)
router = APIRouter()


async def _album_split_groups(session: AsyncSession) -> list[list[dict]]:
    """Albums split across multiple rows due to artist/title name variants.

    Each group is sorted most-tracks-first (canonical candidate first).
    """
    from collections import defaultdict
    from service.core.normalize import normalize

    rows = (await session.execute(
        select(
            Album.id, Album.title, Album.year, Artist.name,
            func.count(Track.id).label("ntracks"),
        )
        .join(Artist, Artist.id == Album.artist_id)
        .join(Track, Track.album_id == Album.id)
        .group_by(Album.id, Album.title, Album.year, Artist.name)
    )).all()

    key_to_albums: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for album_id, title, year, artist_name, ntracks in rows:
        key = (normalize(title), normalize(artist_name))
        key_to_albums[key].append({
            "id": album_id,
            "title": title,
            "year": year,
            "artist": artist_name,
            "ntracks": ntracks,
        })

    split_groups = [albums for albums in key_to_albums.values() if len(albums) > 1]
    for g in split_groups:
        g.sort(key=lambda a: a["ntracks"], reverse=True)
    return split_groups


async def _library_attention_counts(session: AsyncSession) -> dict[str, int]:
    """Per-category counts of library items needing attention.

    Shared by the Library Health page and the nav attention badge.
    """
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

    no_cover_count = (await session.execute(
        select(func.count(Album.id)).where(
            ~Album.id.in_(
                select(Track.album_id)
                .join(Track.file)
                .where(TrackFile.has_cover_art == 1)
                .where(Track.album_id.is_not(None))
            )
        ).where(
            Album.id.in_(
                select(Track.album_id).where(Track.album_id.is_not(None))
            )
        )
    )).scalar_one()

    no_mbid_count = (await session.execute(
        select(func.count(Track.id))
        .join(Track.file)
        .where(Track.musicbrainz_recording_id.is_(None))
    )).scalar_one()

    low_bitrate_count = (await session.execute(
        select(func.count(Track.id))
        .join(Track.file)
        .where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < settings.min_bitrate_kbps,
            (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0),
        )
    )).scalar_one()

    return {
        "dupes": dupe_count,
        "no_cover": no_cover_count,
        "no_mbid": no_mbid_count,
        "low_bitrate": low_bitrate_count,
        "splits": len(await _album_split_groups(session)),
        "artist_credits": len(await _artist_credit_mismatches(session)),
    }


# (monotonic timestamp, total) — the attention rollup runs several aggregate
# queries (splits/dupes group-bys scan every album), too heavy to recompute on
# every nav poll from every open tab.
_attention_cache: tuple[float, int] | None = None


_ATTENTION_TTL_S = 300.0


@router.get("/nav/attention-count", response_class=HTMLResponse)
async def nav_attention_count(
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Badge span rolling up every library item needing attention.

    Sum of the Library Health categories (low bitrate, missing covers, no MB
    ID, duplicates, split albums, artist-credit mismatches). needs_review jobs
    are deliberately NOT included — the Jobs badge next to it already shows
    them, and one item counted in two adjacent badges reads as two problems.
    """
    global _attention_cache
    now = time.monotonic()
    if _attention_cache is not None and now - _attention_cache[0] < _ATTENTION_TTL_S:
        total = _attention_cache[1]
    else:
        counts = await _library_attention_counts(session)
        total = sum(counts.values())
        _attention_cache = (now, total)

    poll = 'hx-get="/nav/attention-count" hx-trigger="every 120s" hx-swap="outerHTML"'
    if total:
        return HTMLResponse(
            f'<span class="nav-badge nav-badge-attention" title="{total} library item(s) need attention — see Library Health" {poll}>{total}</span>'
        )
    return HTMLResponse(f"<span {poll}></span>")


@router.get("/library/health", response_class=HTMLResponse)
async def library_health_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Library health overview — duplicates, split albums, missing covers."""
    global _attention_cache
    counts = await _library_attention_counts(session)
    # Fresh numbers were just computed — keep the nav badge consistent with
    # what this page shows instead of waiting out the TTL.
    _attention_cache = (time.monotonic(), sum(counts.values()))

    return templates.TemplateResponse(
        request, "library_health.html",
        {
            "active": "lib-health",
            "dupe_count": counts["dupes"],
            "no_cover_count": counts["no_cover"],
            "no_mbid_count": counts["no_mbid"],
            "low_bitrate_count": counts["low_bitrate"],
            "artist_credit_count": counts["artist_credits"],
            "min_bitrate_kbps": settings.min_bitrate_kbps,
        },
    )


@router.get("/library/health/dupes", response_class=HTMLResponse)
async def library_health_dupes(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: duplicate tracks (same MB recording_id, multiple files)."""
    from collections import defaultdict
    from sqlalchemy.orm import joinedload as _jl

    dupe_rids = (await session.execute(
        select(Track.musicbrainz_recording_id)
        .join(Track.file)
        .where(Track.musicbrainz_recording_id.is_not(None))
        .group_by(Track.musicbrainz_recording_id)
        .having(func.count(Track.id) > 1)
    )).scalars().all()

    groups: list[dict] = []
    if dupe_rids:
        rows = (await session.execute(
            select(Track)
            .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
            .join(Track.file)
            .where(Track.musicbrainz_recording_id.in_(dupe_rids))
            .order_by(Track.musicbrainz_recording_id, TrackFile.bitrate_kbps.desc().nulls_last())
        )).unique().scalars().all()

        by_rid: dict[str, list[Track]] = defaultdict(list)
        for t in rows:
            by_rid[t.musicbrainz_recording_id].append(t)  # type: ignore[index]

        for rid, tracks in by_rid.items():
            groups.append({
                "recording_id": rid,
                "title": tracks[0].title,
                "artist": tracks[0].artist.name,
                "tracks": [
                    {
                        "id": t.id,
                        "path": t.file.path if t.file else "",
                        "codec": t.file.codec if t.file else "",
                        "bitrate_kbps": t.file.bitrate_kbps if t.file else None,
                        "has_cover_art": bool(t.file.has_cover_art) if t.file else False,
                        "quality_score": t.tag_quality_score,
                    }
                    for t in tracks
                ],
            })

    return templates.TemplateResponse(
        request, "partials/health_dupes.html", {"groups": groups}
    )


@router.post("/library/health/dupes/keep-best", response_class=HTMLResponse)
async def dupes_keep_best(
    request: Request,
    recording_id: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Keep the highest-bitrate copy; trash all lower-quality duplicates."""
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.file))
        .join(Track.file)
        .where(Track.musicbrainz_recording_id == recording_id)
        .order_by(TrackFile.bitrate_kbps.desc().nulls_last(), Track.tag_quality_score.desc().nulls_last())
    )).unique().scalars().all()

    if len(rows) <= 1:
        return HTMLResponse("")  # nothing to do, remove the card

    for track in rows[1:]:  # keep rows[0], trash the rest
        if track.file:
            file_path = Path(track.file.path)
            album_dir = file_path.parent
            if file_path.exists():
                try:
                    safe_trash(file_path, settings.music_dir / ".trash")
                except Exception as exc:
                    logger.warning("Trash failed for %s: %s", file_path, exc)
            _trash_empty_album_dir(album_dir, settings.music_dir / ".trash")
            await session.delete(track.file)
        await session.delete(track)

    await session.commit()
    return HTMLResponse("")  # remove the group card on success


@router.get("/library/health/splits", response_class=HTMLResponse)
async def library_health_splits(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: albums split across multiple folders due to artist name variants."""
    return templates.TemplateResponse(
        request, "partials/health_splits.html",
        {"groups": await _album_split_groups(session)},
    )


@router.get("/library/health/no-mbid", response_class=HTMLResponse)
async def library_health_no_mbid(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks without a MusicBrainz recording ID."""
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.file)
        .where(Track.musicbrainz_recording_id.is_(None))
        .order_by(Track.tag_quality_score.asc().nulls_first())
        .limit(50)
    )).unique().scalars().all()

    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name,
            "album": t.album.title if t.album else None,
            "quality_score": t.tag_quality_score,
        }
        for t in rows
    ]
    return templates.TemplateResponse(
        request, "partials/health_no_mbid.html",
        {"tracks": tracks, "total": len(tracks)},
    )


@router.get("/library/health/low-bitrate", response_class=HTMLResponse)
async def library_health_low_bitrate(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks with bitrate below the configured threshold."""
    from sqlalchemy.orm import joinedload as _jl

    min_br = settings.min_bitrate_kbps
    _not_suppressed = (Track.bitrate_suppressed.is_(None)) | (Track.bitrate_suppressed == 0)
    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.file)
        .where(
            TrackFile.bitrate_kbps.isnot(None),
            TrackFile.bitrate_kbps < min_br,
            _not_suppressed,
        )
        .order_by(TrackFile.bitrate_kbps.asc())
        .limit(100)
    )).unique().scalars().all()

    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name,
            "album": t.album.title if t.album else None,
            "bitrate_kbps": t.file.bitrate_kbps if t.file else None,
            "codec": t.file.codec if t.file else None,
            "bitrate_suppressed": bool(t.bitrate_suppressed),
        }
        for t in rows
    ]
    return templates.TemplateResponse(
        request, "partials/health_low_bitrate.html",
        {"tracks": tracks, "min_bitrate_kbps": min_br},
    )


@router.get("/library/health/low-quality", response_class=HTMLResponse)
async def library_health_low_quality(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks with low metadata quality score."""
    from service.metadata.quality import LOW_QUALITY_THRESHOLD
    from sqlalchemy.orm import joinedload as _jl

    _not_suppressed = (Track.quality_suppressed.is_(None)) | (Track.quality_suppressed == 0)
    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album))
        .where(
            Track.tag_quality_score.isnot(None),
            Track.tag_quality_score < LOW_QUALITY_THRESHOLD,
            _not_suppressed,
        )
        .order_by(Track.tag_quality_score.asc().nullslast())
        .limit(100)
    )).unique().scalars().all()

    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else None,
            "quality_score": t.tag_quality_score,
        }
        for t in rows
    ]
    return templates.TemplateResponse(
        request, "partials/health_low_quality.html",
        {"tracks": tracks},
    )


@router.get("/library/health/missing-files", response_class=HTMLResponse)
async def library_health_missing_files(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks indexed in the DB whose file is gone from disk."""
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.file)
        .order_by(Track.title)
    )).unique().scalars().all()

    def _find_missing() -> list[Track]:
        return [t for t in rows if t.file and not Path(t.file.path).exists()][:100]

    missing = await asyncio.to_thread(_find_missing)
    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else None,
            "provider_ref": t.file.provider_ref if t.file else None,
        }
        for t in missing
    ]
    return templates.TemplateResponse(
        request, "partials/health_missing_files.html",
        {"tracks": tracks},
    )


async def _artist_credit_mismatches(session: AsyncSession) -> list[Track]:
    """Tracks whose per-file ARTIST tag differs from the album artist.

    The scanner keys Artist rows on ALBUMARTIST, so these credits are invisible
    as artists in audioreap — but Subsonic clients read the ARTIST tag directly
    and surface them as separate artists (e.g. "Vitamin String Quartet" on a
    Ramin Djawadi album). Featuring credits ("Main feat. Guest") and
    compilations (Various Artists) are intentional and excluded.
    """
    from service.core.normalize import normalize as _norm
    from service.library.tagger import primary_artist as _primary_artist
    from sqlalchemy.orm import joinedload as _jl

    rows = (await session.execute(
        select(Track)
        .options(_jl(Track.artist), _jl(Track.album), _jl(Track.file))
        .join(Track.artist)
        .where(
            Track.artist_credit.is_not(None),
            Track.album_id.is_not(None),
            Track.artist_credit != Artist.name,
        )
        .order_by(Artist.name, Track.title)
    )).unique().scalars().all()

    out: list[Track] = []
    for t in rows:
        credit = (t.artist_credit or "").strip()
        albumartist = (t.artist.name or "").strip() if t.artist else ""
        if not credit or not albumartist:
            continue
        if _norm(albumartist) == "various artists":
            continue  # compilation — per-track credits are the point
        if _norm(credit) == _norm(albumartist):
            continue  # case/punctuation-only difference
        if _norm(_primary_artist(credit)) == _norm(albumartist):
            continue  # "Main feat. Guest" under Main — by design
        out.append(t)
    return out


@router.get("/library/health/artist-credits", response_class=HTMLResponse)
async def library_health_artist_credits(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """HTMX partial: tracks whose ARTIST tag credit differs from the album artist."""
    mismatches = await _artist_credit_mismatches(session)
    credits_populated = (await session.execute(
        select(func.count(Track.id)).where(Track.artist_credit.is_not(None))
    )).scalar_one()
    tracks = [
        {
            "id": t.id,
            "title": t.title,
            "credit": t.artist_credit,
            "albumartist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else None,
        }
        for t in mismatches
    ]
    return templates.TemplateResponse(
        request, "partials/health_artist_credits.html",
        {"tracks": tracks, "credits_populated": credits_populated},
    )


async def _fix_artist_credit(session: AsyncSession, track: Track) -> str | None:
    """Set the file's ARTIST tag to the album artist. Returns an error or None.

    Writes ONLY the ARTIST tag (never ALBUMARTIST — album grouping is already
    correct for these tracks) and mirrors the change into Track.artist_credit.
    """
    from service.library.tagger import write_tags as _write_tags

    if not track.file:
        return "no file"
    albumartist = track.artist.name if track.artist else None
    if not albumartist:
        return "no album artist"
    fp = Path(track.file.path)
    if not fp.exists():
        return "file missing on disk"
    try:
        await asyncio.to_thread(_write_tags, fp, artist=albumartist)
    except Exception as exc:  # mutagen failures are per-file, keep going
        return str(exc)
    track.artist_credit = albumartist
    track.updated_at = datetime.now(UTC).replace(tzinfo=None)
    try:
        track.file.file_mtime = fp.stat().st_mtime
    except OSError:
        pass
    return None


@router.post("/library/health/artist-credits/{internal_id}/fix", response_class=HTMLResponse)
async def fix_artist_credit(
    request: Request,
    internal_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """One-click: rewrite this track's ARTIST tag to the album artist."""
    track = await _get_track_with_file(session, internal_id)
    err = await _fix_artist_credit(session, track)
    if err:
        return HTMLResponse(
            f'<div class="card" style="padding:8px 14px"><span style="font-size:12px;color:var(--warn)">Failed: {err}</span></div>'
        )
    await session.commit()
    try:
        from service.navidrome.client import trigger_scan
        await trigger_scan()
    except Exception as exc:
        logger.debug("best-effort Navidrome scan trigger failed: %s", exc)
    return HTMLResponse("")  # row disappears from the list


@router.post("/library/health/artist-credits/fix-all", response_class=HTMLResponse)
async def fix_all_artist_credits(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Rewrite the ARTIST tag to the album artist for every mismatched track."""
    mismatches = await _artist_credit_mismatches(session)
    fixed, failed = 0, 0
    for t in mismatches:
        if await _fix_artist_credit(session, t) is None:
            fixed += 1
        else:
            failed += 1
    await session.commit()
    if fixed:
        try:
            from service.navidrome.client import trigger_scan
            await trigger_scan()
        except Exception as exc:
            logger.debug("best-effort Navidrome scan trigger failed: %s", exc)
    # Re-render the list (anything that failed stays visible)
    return await library_health_artist_credits(request, session)


@router.post("/library/health/fetch-missing-covers", response_class=HTMLResponse)
async def fetch_missing_covers(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Enqueue a background arq job to fetch cover art for all albums missing it."""
    try:
        async with arq_pool() as redis:
            await redis.enqueue_job("fetch_missing_covers")
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    return HTMLResponse('<span class="badge-ok">Cover art fetch queued — check back in a few minutes</span>')


@router.post("/library/health/backfill-replaygain", response_class=HTMLResponse)
async def backfill_replaygain_route(request: Request, full: bool = False, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Enqueue a background arq job to write ReplayGain tags across the whole library.

    full=True forces every file to be re-analyzed and retagged, even ones that
    already carry ReplayGain info — use after changing the target loudness.
    """
    try:
        async with arq_pool() as redis:
            await redis.enqueue_job("backfill_replaygain", full=full)
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    label = "Full ReplayGain retag" if full else "ReplayGain backfill"
    return HTMLResponse(f'<span class="badge-ok">{label} queued — check back in a few minutes</span>')


@router.post("/library/health/fetch-missing-lyrics", response_class=HTMLResponse)
async def fetch_missing_lyrics(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """Enqueue a background arq job to fetch LRCLIB lyrics for tracks missing them."""
    try:
        async with arq_pool() as redis:
            await redis.enqueue_job("fetch_missing_lyrics")
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    return HTMLResponse('<span class="badge-ok">Lyrics fetch queued — runs in the background (large libraries take a while)</span>')


@router.post("/library/health/upgrade-plain-lyrics", response_class=HTMLResponse)
async def upgrade_plain_lyrics(request: Request) -> HTMLResponse:
    """Enqueue a job that upgrades plain-text .lrc sidecars to synced when LRCLIB has one."""
    try:
        async with arq_pool() as redis:
            await redis.enqueue_job("fetch_missing_lyrics", upgrade_plain=True)
    except Exception as exc:
        return _error_badge(f"Queue unavailable: {exc}")
    return HTMLResponse('<span class="badge-ok">Synced-lyrics upgrade queued — re-checks plain tracks in the background</span>')


@router.post("/library/health/reset-lyrics-misses", response_class=HTMLResponse)
async def reset_lyrics_misses(request: Request) -> HTMLResponse:
    """Delete cached LRCLIB miss markers so previously-missed tracks are retried.

    A miss marker (``\\x00MISS``) is written when LRCLIB has no lyrics for a track,
    so the next backfill skips re-hitting the API. Clearing them forces a fresh
    lookup — useful after LRCLIB gains new lyrics, or to recover from any markers
    written before transient errors were excluded from caching. Real lyric files
    are left untouched.
    """
    from pathlib import Path

    lyrics_cache = settings.cache_dir / "lyrics"
    cleared = 0
    try:
        def _purge() -> int:
            n = 0
            if not lyrics_cache.is_dir():
                return 0
            for p in lyrics_cache.glob("*.lrc"):
                try:
                    if p.read_text(encoding="utf-8") == "\x00MISS":
                        p.unlink()
                        n += 1
                except OSError:
                    continue
            return n
        cleared = await asyncio.to_thread(_purge)
    except Exception as exc:
        return _error_badge(f"Reset failed: {exc}")
    return HTMLResponse(
        f'<span class="badge-ok">Cleared {cleared} cached miss marker'
        f'{"" if cleared == 1 else "s"} — run “Fetch all” to retry those tracks</span>'
    )
