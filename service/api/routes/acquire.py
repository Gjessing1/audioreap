"""Acquire pages: root redirect, provider search, cloud search, URL acquire."""
from __future__ import annotations

import logging
from pathlib import Path
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from service.config import settings
from service.core.models import TrackCandidate, TrackQuality, TrackRef
from service.db.schema import Artist, Track
from service.db.session import get_session
from service.providers.ytdlp import explicit_score as _explicit_score

from service.acquisition.queue import arq_pool, enqueue_acquire_track
from service.api.shared import _acquire_ctx, templates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/", response_class=RedirectResponse)
async def root() -> RedirectResponse:
    return RedirectResponse("/acquire")


@router.get("/acquire", response_class=HTMLResponse)
async def acquire_page(
    request: Request, q: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, q, "search", session)
    return templates.TemplateResponse(request, "acquire.html", ctx)


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request, q: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    ctx = await _acquire_ctx(request, q, "search", session)
    return templates.TemplateResponse(request, "acquire.html", ctx)


def _track_to_ref(row: Track) -> TrackRef:
    file = row.file
    quality: TrackQuality | None = None
    local_path: Path | None = None
    if file:
        quality = TrackQuality(
            codec=file.codec, container=file.container,
            bitrate_kbps=file.bitrate_kbps, sample_rate_hz=file.sample_rate_hz,
        )
        local_path = Path(file.path)
    return TrackRef(
        internal_id=row.id,
        source="local",
        status="available" if file else "missing",
        title=row.title,
        artist=row.artist.name,
        album=row.album.title if row.album else None,
        duration_seconds=row.duration_seconds,
        local_path=local_path,
        quality=quality,
        musicbrainz_recording_id=row.musicbrainz_recording_id,
    )


@router.get("/search/results", response_class=HTMLResponse)
async def search_results(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from service.core.normalize import normalize as _norm

    tracks: list[TrackRef] = []
    if q:
        # Split query into meaningful tokens (skip single-char stopwords).
        # Fetch candidates matching ANY token across title or artist, then rank
        # by how many tokens match the combined "artist title" string.
        tokens = [t for t in q.lower().split() if len(t) > 1]
        if not tokens:
            tokens = [q.lower()]

        from sqlalchemy import or_
        token_filters = or_(
            *[Track.title.ilike(f"%{tok}%") | Artist.name.ilike(f"%{tok}%") for tok in tokens]
        )
        stmt = (
            select(Track)
            .join(Track.artist)
            .outerjoin(Track.album)
            .outerjoin(Track.file)
            .options(joinedload(Track.artist), joinedload(Track.album), joinedload(Track.file))
            .where(token_filters)
            .limit(200)
        )
        rows = (await session.execute(stmt)).unique().scalars().all()

        # Score by how many tokens appear in the combined "artist title" string
        def _score(row: Track) -> int:
            haystack = _norm(f"{row.artist.name} {row.title}").lower()
            return sum(1 for tok in tokens if tok in haystack)

        rows_sorted = sorted(rows, key=_score, reverse=True)[:30]
        tracks = [_track_to_ref(r) for r in rows_sorted]

    return templates.TemplateResponse(
        request, "partials/local_results.html", {"tracks": tracks, "q": q}
    )


async def _find_owned_match(
    session: AsyncSession,
    title: str,
    artist: str,
    duration_seconds: int | None,
) -> Track | None:
    """Best-effort local-library match for a cloud search candidate.

    Cloud candidates rarely carry MB recording IDs, so this is a fuzzy
    title/artist/duration check (matcher.is_confident_match) over a small
    ILIKE-prefiltered pool — cheap enough to run per result card.
    """
    from service.core.normalize import normalize
    from service.search.matcher import track_similarity, DEDUP_THRESHOLD

    tokens = [t for t in normalize(title).split() if len(t) >= 3]
    if not tokens:
        return None
    # Longest token is the most selective LIKE prefilter
    anchor = max(tokens, key=len)
    stmt = (
        select(Track)
        .join(Track.artist)
        .options(joinedload(Track.artist))
        .where(Track.title.ilike(f"%{anchor}%"))
        .limit(50)
    )
    rows = (await session.execute(stmt)).unique().scalars().all()
    best: Track | None = None
    best_score = 0.0
    for row in rows:
        score = track_similarity(
            title, artist, duration_seconds,
            row.title, row.artist.name if row.artist else "", row.duration_seconds,
        )
        if score > best_score:
            best, best_score = row, score
    return best if best is not None and best_score >= DEDUP_THRESHOLD else None


@router.get("/search/cloud", response_class=HTMLResponse)
async def cloud_search_page(
    request: Request,
    q: str = "",
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    candidates: list[dict[str, object]] = []
    PAGE = 5
    if q:
        try:
            import service.providers.ytdlp  # noqa: F401
            from service.core.models import SearchQuery
            from service.providers import get

            provider = get("ytdlp")()
            # Fetch enough for this page + ranking headroom
            fetch_limit = offset + PAGE * 2
            raw: list[dict[str, object]] = []
            async for c in provider.search(SearchQuery(q=q, limit=fetch_limit)):
                raw.append({
                    "title": c.title,
                    "artist": c.artist,
                    "duration_seconds": c.duration_seconds,
                    "provider_ref": c.provider_ref,
                    "thumbnail_url": c.thumbnail_url,
                    "candidate_json": c.model_dump_json(),
                    "_score": _explicit_score(c.title),
                })

            # Sort: explicit first (when prefer_explicit is on), clean last; stable
            if settings.prefer_explicit:
                raw.sort(key=lambda x: -int(x["_score"]))  # type: ignore[arg-type]
            for item in raw:
                del item["_score"]

            candidates = raw[offset: offset + PAGE]

            for item in candidates:
                try:
                    owned = await _find_owned_match(
                        session,
                        str(item["title"]),
                        str(item["artist"]),
                        item["duration_seconds"],  # type: ignore[arg-type]
                    )
                except Exception as exc:
                    logger.debug("Owned check failed for %r: %s", item["title"], exc)
                    owned = None
                if owned is not None:
                    item["owned_title"] = owned.title
                    item["owned_artist"] = owned.artist.name if owned.artist else ""
        except Exception as exc:
            logger.warning("Cloud search failed: %s", exc)

    return templates.TemplateResponse(
        request, "partials/cloud_results.html",
        {"candidates": candidates, "q": q, "offset": offset, "limit": PAGE},
    )


@router.get("/search/cloud/quality", response_class=HTMLResponse)
async def cloud_quality_probe(url: str = "") -> HTMLResponse:
    """On-demand audio-quality probe for one search result.

    Flat search results carry no format data, so this runs a single full
    yt-dlp extraction (disk-cached by video id) and reports the audio stream
    a download would actually get — the deciding signal between an official
    audio upload and a low-bitrate rip of the same track.
    """
    import asyncio
    import html as _html

    from service.providers.ytdlp import probe_source_quality

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return HTMLResponse('<span class="badge badge-warn" title="Quality probe needs a direct video URL">n/a</span>')

    try:
        info = await asyncio.to_thread(probe_source_quality, url, settings.cache_dir)
    except Exception as exc:
        logger.debug("Quality probe failed for %s: %s", url, exc)
        info = None
    if not info or not info.get("abr_kbps"):
        return HTMLResponse('<span class="badge badge-warn" title="Could not read format data for this video">?</span>')

    codec = _html.escape(str(info.get("codec") or "audio"))
    abr = int(info["abr_kbps"])
    asr = info.get("sample_rate")
    detail = f"{codec} · {abr} kbps" + (f" · {int(asr) / 1000:g} kHz" if asr else "")
    # YouTube's best audio tops out around 130 kbps opus — treat that band as
    # good; meaningfully below it usually means a re-encoded rip.
    color = "var(--success)" if abr >= 110 else ("var(--warn)" if abr >= 70 else "var(--danger)")
    return HTMLResponse(
        f'<span class="badge" style="color:{color};font-family:var(--font-mono)"'
        f' title="Best audio stream a download would get: {detail}">{codec} {abr}k</span>'
    )


@router.post("/search/acquire-url", response_class=HTMLResponse)
async def acquire_from_url(
    request: Request,
    url: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Queue an acquisition from a manually entered URL."""
    from service.acquisition.jobs import create_job
    from service.core.models import TrackCandidate

    url = url.strip()
    candidate = TrackCandidate(
        provider="ytdlp",
        provider_ref=url,
        title=url,
        artist="Unknown",
    )
    job_id = await create_job(
        session,
        provider_name="ytdlp",
        provider_ref=url,
        candidate=candidate,
        query=url,
    )
    await session.commit()

    try:
        async with arq_pool() as redis:
            await enqueue_acquire_track(
                redis, job_id,
                provider_name="ytdlp",
                provider_ref=url,
                candidate_json=candidate.model_dump_json(),
            )
    except Exception as exc:
        raise HTTPException(503, f"Queue unavailable: {exc}") from exc

    return HTMLResponse(
        '<div id="cloud-url-row" style="padding:8px 0">'
        f'<span class="badge badge-done">Queued → <a href="/jobs">Jobs ↗</a></span>'
        '</div>'
    )
