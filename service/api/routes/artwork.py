"""Cover-art search across iTunes/Deezer/CAA + user-supplied art fetching."""
from __future__ import annotations

import json
import logging
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from service.api.shared import _error_badge, templates

logger = logging.getLogger(__name__)
router = APIRouter()


async def _fetch_user_art(art_url: str) -> tuple[bytes | None, HTMLResponse | None]:
    """Download + size-validate user-picked cover art from a URL.

    Returns (art_bytes, None) on success or (None, error_badge_response) on
    failure — the shared front half of the job/track/album apply-art routes.
    """
    from service.metadata.artwork import _MIN_USER_COVER_PX, _image_too_small, fetch_from_url

    art = await fetch_from_url(art_url)
    if not art:
        return None, _error_badge("Could not download image")
    if _image_too_small(art, _MIN_USER_COVER_PX):
        return None, _error_badge("Image too small (< 300×300)")
    return art, None


async def _search_itunes_art(q: str) -> list[dict]:
    """Search iTunes Store for album artwork. Returns list of {url, label} dicts."""
    import urllib.parse
    results: list[dict] = []
    try:
        encoded = urllib.parse.quote(q)
        url = f"https://itunes.apple.com/search?term={encoded}&entity=album&limit=12&media=music"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return results
            data = resp.json()
            for item in data.get("results", []):
                art_url = item.get("artworkUrl100", "")
                if not art_url:
                    continue
                # iTunes returns 100×100; swap to 600×600
                art_url = art_url.replace("100x100bb", "600x600bb")
                thumb_url = art_url.replace("600x600bb", "150x150bb")
                artist = item.get("artistName", "")
                album = item.get("collectionName", "")
                results.append({
                    "thumb": thumb_url,
                    "full": art_url,
                    "label": f"{artist} — {album}" if artist else album,
                    "source": "iTunes",
                })
    except Exception as exc:
        logger.debug("iTunes art search failed: %s", exc)
    return results


async def _search_deezer_art(q: str, offset: int = 0) -> list[dict]:
    """Search Deezer for album artwork. Returns list of {url, label, source} dicts."""
    import urllib.parse
    results: list[dict] = []
    try:
        encoded = urllib.parse.quote(q)
        url = f"https://api.deezer.com/search/album?q={encoded}&limit=12&index={offset}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return results
            for item in resp.json().get("data", []):
                full_url = item.get("cover_xl") or item.get("cover_big", "")
                thumb_url = item.get("cover_medium") or item.get("cover_big", "")
                if not full_url:
                    continue
                artist = (item.get("artist") or {}).get("name", "")
                album = item.get("title", "")
                results.append({
                    "thumb": thumb_url,
                    "full": full_url,
                    "label": f"{artist} — {album}" if artist else album,
                    "source": "Deezer",
                })
    except Exception as exc:
        logger.debug("Deezer art search failed: %s", exc)
    return results


async def _fetch_caa_for_rg(client: "Any", rg_id: str) -> list[dict]:
    """List all releases in an MB release group and probe CAA for covers (inner helper)."""
    releases_url = f"https://musicbrainz.org/ws/2/release?release-group={rg_id}&fmt=json&limit=25"
    rels_resp = await client.get(releases_url)
    if rels_resp.status_code != 200:
        return []
    releases = rels_resp.json().get("releases", [])

    async def _fetch_caa(rel_id: str, rel_label: str) -> "dict | None":
        try:
            caa = await client.get(
                f"https://coverartarchive.org/release/{rel_id}/front-250",
                follow_redirects=True,
            )
            if caa.status_code == 200 and caa.headers.get("content-type", "").startswith("image/"):
                full = await client.get(
                    f"https://coverartarchive.org/release/{rel_id}/front",
                    follow_redirects=False,
                )
                full_url = full.headers.get("location", f"https://coverartarchive.org/release/{rel_id}/front")
                return {"thumb": f"https://coverartarchive.org/release/{rel_id}/front-250",
                        "full": full_url, "label": rel_label, "source": "CAA"}
        except Exception as exc:
            logger.debug("CAA art probe failed: %s", exc)
        return None

    import asyncio as _asyncio
    tasks = [
        _fetch_caa(
            r["id"],
            f"{r.get('title', '')} ({r.get('date', '')[:4] if r.get('date') else '?'})"
            f" [{r.get('country', '') or r.get('status', '')}]"
        )
        for r in releases[:15]
    ]
    return [r for r in await _asyncio.gather(*tasks) if r is not None]


async def _search_caa_editions(release_id: str) -> list[dict]:
    """Fetch all CAA covers for every edition in the same MB release group (given a release ID)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "audioreap/0.1"}) as client:
            rg_url = f"https://musicbrainz.org/ws/2/release/{release_id}?inc=release-groups&fmt=json"
            rg_resp = await client.get(rg_url)
            if rg_resp.status_code != 200:
                return []
            rg_id = (rg_resp.json().get("release-group") or {}).get("id")
            if not rg_id:
                return []
            return await _fetch_caa_for_rg(client, rg_id)
    except Exception as exc:
        logger.debug("CAA editions search failed: %s", exc)
    return []


async def _search_caa_by_rg(rg_id: str) -> list[dict]:
    """Fetch all CAA covers for every edition in an MB release group (given the group ID directly)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "audioreap/0.1"}) as client:
            return await _fetch_caa_for_rg(client, rg_id)
    except Exception as exc:
        logger.debug("CAA by-rg search failed: %s", exc)
    return []


_ART_PAGE_SIZE = 12


@router.get("/art/search", response_class=HTMLResponse)
async def art_search(
    request: Request,
    q: str = "",
    release_id: str = "",
    release_group_id: str = "",
    apply_url: str = "",
    result_target: str = "",
    offset: int = 0,
    page_key: str = "",
) -> HTMLResponse:
    """Return a thumbnail grid from iTunes + Deezer + CAA editions for the given query."""
    results: list[dict] = []
    first_page = offset == 0
    if q.strip():
        import asyncio as _asyncio
        itunes, deezer = await _asyncio.gather(
            _search_itunes_art(q.strip()),
            _search_deezer_art(q.strip(), offset),
        )
        results.extend(itunes)
        results.extend(deezer)
    # CAA results are release-specific — only fetch on first page
    if first_page:
        if release_id.strip():
            caa = await _search_caa_editions(release_id.strip())
            results.extend(caa)
        elif release_group_id.strip():
            caa = await _search_caa_by_rg(release_group_id.strip())
            results.extend(caa)

    if not results and first_page:
        return HTMLResponse('<p class="empty" style="font-size:12px;padding:8px 0">No results found.</p>')
    if not results:
        return HTMLResponse("")

    # Show load-more button if either iTunes or Deezer returned a full page
    has_more = len([r for r in results if r["source"] in ("iTunes", "Deezer")]) >= _ART_PAGE_SIZE
    next_offset = offset + _ART_PAGE_SIZE if has_more else None

    return templates.TemplateResponse(
        request, "partials/art_search_results.html",
        {
            "results": results,
            "apply_url": apply_url,
            "result_target": result_target,
            "next_offset": next_offset,
            "q": q,
            "release_id": release_id,
            "release_group_id": release_group_id,
            "page_key": page_key,
        },
    )
