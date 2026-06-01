"""yt-dlp provider.

One class, many sites — YouTube, SoundCloud, Bandcamp, and any other
yt-dlp-supported extractor. Adding Bandcamp is a config flag, not new code.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from service.core.modifiers import looks_like_live
from service.core.models import (
    AlbumCandidate,
    FetchResult,
    ProviderHealth,
    SearchQuery,
    TrackCandidate,
)
from service.providers import register
from service.providers.base import Provider, ProviderCapabilities

logger = logging.getLogger(__name__)

_DEFAULT_SEARCH_LIMIT = 5
_SEARCH_PREFIX = "ytsearch"


def _youtube_auth_opts() -> dict[str, object]:
    """Optional yt-dlp auth so requests look logged-in (cookies / PO-token).

    All blank by default → anonymous access (the adaptive rate gate normally keeps
    us under the limit without credentials). Set the env vars only if logged-out
    429s persist. Returns {} when nothing is configured.
    """
    from service.config import settings

    opts: dict[str, object] = {}
    cookies = (getattr(settings, "ytdlp_cookies_file", "") or "").strip()
    if cookies and Path(cookies).is_file():
        opts["cookiefile"] = cookies

    yt_args: dict[str, list[str]] = {}
    clients = [c.strip() for c in (getattr(settings, "ytdlp_player_client", "") or "").split(",") if c.strip()]
    if clients:
        yt_args["player_client"] = clients
    tokens = [t.strip() for t in (getattr(settings, "ytdlp_po_token", "") or "").split(",") if t.strip()]
    if tokens:
        yt_args["po_token"] = tokens
    if yt_args:
        opts["extractor_args"] = {"youtube": yt_args}
    return opts


def _canonical_source_url(info: object, provider_ref: str) -> str | None:
    """Best canonical media URL for the thing yt-dlp actually downloaded.

    ``info`` is a single-video dict for a direct-URL ref, or a search/playlist
    wrapper with ``entries`` for a ``ytsearch1:`` ref (album batches). Dig into the
    first real entry so album tracks get a clickable watch URL instead of the bare
    search expression. Returns None if nothing usable is found.
    """
    node = info if isinstance(info, dict) else {}
    entries = node.get("entries") if isinstance(node, dict) else None
    if entries:
        node = next((e for e in entries if e), node)
    if isinstance(node, dict):
        url = node.get("webpage_url") or node.get("original_url") or node.get("url")
        if url:
            return str(url)
        vid = node.get("id")
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    # Fall back to the ref only when it's already a real URL, never a search query.
    if provider_ref and not provider_ref.startswith(("ytsearch", "ytmsearch")):
        return provider_ref
    return None


def _ydl_opts_base() -> dict[str, object]:
    return {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        **_youtube_auth_opts(),
    }


@register
class YtdlpProvider(Provider):
    """yt-dlp-backed provider. Handles any site yt-dlp supports."""

    name = "ytdlp"
    capabilities = ProviderCapabilities(
        supports_search=True,
        supports_album_search=True,
        supports_quality_selection=False,
        search_is_async=False,
        requires_credentials=False,
    )

    async def search(self, query: SearchQuery) -> AsyncIterator[TrackCandidate]:
        import yt_dlp

        opts = {
            **_ydl_opts_base(),
            "extract_flat": True,
        }
        url = f"{_SEARCH_PREFIX}{query.limit}:{query.q}"

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            logger.warning("yt-dlp search failed for %r: %s", query.q, exc)
            return

        if not info or "entries" not in info:
            return

        for entry in info["entries"]:
            if not entry:
                continue
            video_id = entry.get("id") or entry.get("url", "")
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"

            artist_from_meta = entry.get("artist")  # only set for YouTube Music
            title_raw = str(entry.get("track") or entry.get("title") or "Unknown")
            uploader = entry.get("uploader") or entry.get("channel") or "Unknown"

            if artist_from_meta:
                title = title_raw
                artist = str(artist_from_meta)
            elif " - " in title_raw:
                # Split "Artist - Title" convention common in regular YouTube uploads
                parts = title_raw.split(" - ", 1)
                artist = parts[0].strip()
                title = parts[1].strip()
            else:
                title = title_raw
                artist = str(uploader)

            album = entry.get("album")
            duration = entry.get("duration")

            yield TrackCandidate(
                provider=self.name,
                provider_ref=video_url,
                title=title,
                artist=artist,
                album=album,
                duration_seconds=int(duration) if duration else None,
                thumbnail_url=entry.get("thumbnail"),
                raw_metadata={k: v for k, v in entry.items() if isinstance(v, (str, int, float, bool))},
            )

    async def fetch(
        self,
        provider_ref: str,
        dest_dir: Path,
        on_progress=None,
    ) -> FetchResult:
        import yt_dlp

        downloaded_path: list[Path] = []
        loop = asyncio.get_running_loop()
        _last_reported = [0.0]  # throttle: only report every 5% change

        def _progress_hook(d: dict[str, object]) -> None:
            if d.get("status") == "finished":
                fp = d.get("filename")
                if fp:
                    downloaded_path.append(Path(str(fp)))
            if on_progress is not None and d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                dl = d.get("downloaded_bytes")
                if total and dl:
                    fraction = float(dl) / float(total)
                    if fraction - _last_reported[0] >= 0.05:
                        _last_reported[0] = fraction
                        loop.call_soon_threadsafe(on_progress, fraction)

        opts: dict[str, object] = {
            **_ydl_opts_base(),
            "format": "bestaudio/best",
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "progress_hooks": [_progress_hook],
            # Space out the HTTP requests within a single extraction a touch so a
            # fragmented download doesn't hammer YouTube and trip a 429. The
            # cross-job pacing lives in the worker's rate gate; this is local.
            "sleep_interval_requests": 1,
        }

        def _run() -> object:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(provider_ref, download=True)

        info = await asyncio.to_thread(_run)

        if not info:
            raise RuntimeError(f"yt-dlp returned no info for {provider_ref!r}")

        # Prefer the hook path; fall back to reconstructing from info
        if downloaded_path:
            file_path = downloaded_path[-1]
        else:
            video_id = info.get("id", "unknown")
            ext = info.get("ext", "webm")
            file_path = dest_dir / f"{video_id}.{ext}"

        if not file_path.exists():
            raise FileNotFoundError(f"Downloaded file not found: {file_path}")

        ext = file_path.suffix.lstrip(".")
        codec = str(info.get("acodec") or info.get("vcodec") or ext)
        container = ext
        bitrate = info.get("abr") or info.get("tbr")
        sample_rate = info.get("asr")

        return FetchResult(
            file_path=file_path,
            provider=self.name,
            provider_ref=provider_ref,
            source_url=_canonical_source_url(info, provider_ref),
            codec=codec,
            container=container,
            bitrate_kbps=int(bitrate) if bitrate else None,
            sample_rate_hz=int(sample_rate) if sample_rate else None,
            raw_metadata={k: v for k, v in info.items() if isinstance(v, (str, int, float, bool))},
        )

    async def fetch_album(self, album_ref: str) -> AlbumCandidate:
        """Extract ordered track list from a playlist URL."""
        import yt_dlp

        opts = {
            **_ydl_opts_base(),
            "extract_flat": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(album_ref, download=False)

        if not info:
            raise ValueError(f"yt-dlp returned no info for {album_ref!r}")

        album_title = info.get("title") or "Unknown Album"
        album_artist = info.get("uploader") or info.get("channel") or "Unknown Artist"
        entries = info.get("entries") or []

        tracks: list[TrackCandidate] = []
        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id") or ""
            video_url = entry.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            # Use the dedicated track/artist fields (set for YouTube Music), fall back to
            # splitting "Artist - Title" from the video title, then fall back to album_artist.
            track_title_raw = str(entry.get("track") or entry.get("title") or "Unknown")
            entry_artist = entry.get("artist")
            if entry_artist:
                track_title = track_title_raw
                track_artist = str(entry_artist)
            elif " - " in track_title_raw:
                parts = track_title_raw.split(" - ", 1)
                track_artist = parts[0].strip()
                track_title = parts[1].strip()
            else:
                track_title = track_title_raw
                track_artist = album_artist
            duration = entry.get("duration")
            tracks.append(TrackCandidate(
                provider=self.name,
                provider_ref=video_url,
                title=track_title,
                artist=track_artist,
                album=album_title,
                duration_seconds=int(duration) if duration else None,
                thumbnail_url=entry.get("thumbnail"),
                raw_metadata={k: v for k, v in entry.items()
                              if isinstance(v, (str, int, float, bool))},
            ))

        return AlbumCandidate(
            provider=self.name,
            provider_ref=album_ref,
            album_title=str(album_title),
            album_artist=str(album_artist),
            track_count=len(tracks),
            tracks=tracks,
        )

    async def resolve_playlist(self, url: str) -> tuple[str, str, list[TrackCandidate]]:
        """Extract a flat track list from a playlist URL.

        Returns (title, source, candidates) where source is one of
        'youtube', 'youtube_music', 'spotify', 'unknown'.
        """
        import yt_dlp

        opts = {**_ydl_opts_base(), "extract_flat": True}

        def _sync() -> tuple[str, str, list[TrackCandidate]]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not info:
                raise ValueError(f"yt-dlp returned no info for {url!r}")

            if "entries" not in info:
                raise ValueError("URL does not appear to be a playlist")

            title = str(info.get("title") or "Unknown Playlist")
            webpage_url = str(info.get("webpage_url") or url)

            if "music.youtube.com" in webpage_url:
                source = "youtube_music"
            elif "spotify.com" in url:
                source = "spotify"
            else:
                source = "youtube"

            candidates: list[TrackCandidate] = []
            for entry in info.get("entries") or []:
                if not entry:
                    continue
                video_id = entry.get("id") or ""
                video_url = (
                    entry.get("url")
                    or entry.get("webpage_url")
                    or f"https://www.youtube.com/watch?v={video_id}"
                )
                track_title = str(entry.get("track") or entry.get("title") or "Unknown")
                artist = str(
                    entry.get("artist")
                    or entry.get("uploader")
                    or entry.get("channel")
                    or "Unknown"
                )
                album = entry.get("album")
                duration = entry.get("duration")
                candidates.append(TrackCandidate(
                    provider=self.name,
                    provider_ref=video_url,
                    title=track_title,
                    artist=artist,
                    album=str(album) if album else None,
                    duration_seconds=int(duration) if duration else None,
                    thumbnail_url=entry.get("thumbnail"),
                    raw_metadata={
                        k: v for k, v in entry.items()
                        if isinstance(v, (str, int, float, bool))
                    },
                ))

            return title, source, candidates

        import asyncio
        return await asyncio.to_thread(_sync)

    async def health_check(self) -> ProviderHealth:
        import yt_dlp

        try:
            with yt_dlp.YoutubeDL({**_ydl_opts_base(), "extract_flat": True}) as ydl:
                ydl.extract_info("ytsearch1:test", download=False)
            return ProviderHealth(healthy=True, checked_at=datetime.now(UTC))
        except Exception as exc:
            return ProviderHealth(
                healthy=False,
                message=str(exc),
                checked_at=datetime.now(UTC),
            )


# ── Shared YouTube source-selection helpers ──────────────────────────────────

_EXPLICIT_RE = re.compile(r"\b(explicit|dirty|uncensored|uncut)\b", re.IGNORECASE)
_CLEAN_RE = re.compile(
    r"\b(clean|radio edit|radio version|censored|edited|family friendly|no swearing)\b",
    re.IGNORECASE,
)
# Channels that reliably host the official studio audio: the auto-generated
# "<Artist> - Topic" channels (YouTube Music's official audio), VEVO, and labelled
# "Official" artist channels. A match is a strong signal we have the right source.
_OFFICIAL_CHANNEL_RE = re.compile(r"(-\s*topic$|vevo|\bofficial\b)", re.IGNORECASE)

def explicit_score(title: str, age_limit: int | None = None) -> int:
    """Return +1 for explicit, -1 for clean/radio-edit, 0 otherwise.

    age_limit: yt-dlp's age_limit field (18 = explicit) takes precedence over title
    keywords. Note: under extract_flat search (how yt_search_best lists candidates)
    age_limit is usually absent, so the title-keyword signal does the real work —
    keep these patterns broad.
    """
    if age_limit is not None and age_limit >= 18:
        return 1
    if _EXPLICIT_RE.search(title):
        return 1
    if _CLEAN_RE.search(title):
        return -1
    return 0


def _yt_search_entries(query: str, n_candidates: int, prefer_ytm: bool) -> list[dict]:
    """Run a flat YouTube (Music) search and return the raw entry dicts.

    Tries YouTube Music first when preferred (official audio, clean artist/track
    fields) and falls back to plain YouTube if it yields nothing. Shared by
    yt_search_best and yt_search_ranked so both see the same pool.
    """
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, **_youtube_auth_opts()}
    prefixes = [f"ytmsearch{n_candidates}"] if prefer_ytm else []
    prefixes.append(f"ytsearch{n_candidates}")
    for prefix in prefixes:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"{prefix}:{query}", download=False)
            entries = [e for e in ((info or {}).get("entries") or []) if e]
            if entries:
                return entries
        except Exception:
            continue
    return []


def score_yt_candidate(
    entry: dict,
    artist: str,
    title: str,
    duration_seconds: int | None = None,
    prefer_explicit: bool = True,
) -> tuple[float, bool]:
    """Score one YouTube search result against the wanted (artist, title, duration).

    Returns ``(score, is_official_channel)``. This is the single source of truth for
    YouTube source ranking — both the auto-picker (`yt_search_best`) and the review
    card's "Different source" panel (`yt_search_ranked`) use it, so the panel shows
    exactly what the picker weighed. That parity is what makes weight-tuning tractable.
    """
    from service.search.matcher import (
        artist_similarity as _asim,
        title_similarity as _tsim,
    )

    vid_title = str(entry.get("track") or entry.get("title") or "")
    # The cleaned "track" field often drops the "(Explicit)"/"(Clean)" suffix, so scan
    # the full raw title for the explicit/clean signal as well.
    vid_full_title = str(entry.get("title") or vid_title)
    # YouTube Music sets a dedicated "artist"; regular uploads only have the
    # channel/uploader. Comparing it to the wanted artist guards against the common
    # failure of a perfect title from the wrong artist (covers, karaoke, a different
    # act's "- Topic" channel).
    vid_artist = str(entry.get("artist") or entry.get("uploader") or entry.get("channel") or "")
    # Keep the raw channel/uploader separate from vid_artist: when YouTube Music sets a
    # clean "artist" the "- Topic"/VEVO suffix is lost, but that suffix is exactly what
    # flags official-audio channels.
    vid_channel = str(entry.get("channel") or entry.get("uploader") or "")
    vid_dur = entry.get("duration")

    t_sim = _tsim(title, vid_title)
    # A blank candidate artist scores neutral, not zero, so we don't punish sources
    # that simply don't expose an artist field.
    a_sim = _asim(artist, vid_artist) if vid_artist else 0.5

    dur_score = 0.5
    gross_dur_mismatch = False
    if duration_seconds and vid_dur:
        delta = abs(duration_seconds - int(vid_dur))
        if delta <= 5:
            dur_score = 1.0
        elif delta <= 15:
            dur_score = 0.8
        elif delta <= 30:
            dur_score = 0.6
        elif delta > 90:
            dur_score = 0.1
        # A source grossly off the locked MB recording's length is almost never the
        # same cut (wrong song, extended/sped edit, full-album upload, mislabel).
        if delta > 75:
            gross_dur_mismatch = True

    score = t_sim * 0.50 + a_sim * 0.25 + dur_score * 0.20

    # Prefer official audio: "- Topic" / VEVO / labelled official artist channel. Modest
    # bonus so it tips near-ties toward the canonical source without overriding a clear
    # title/artist win.
    is_official = bool(vid_channel and _OFFICIAL_CHANNEL_RE.search(vid_channel))
    if is_official:
        score += 0.12

    if gross_dur_mismatch:
        score -= 0.35

    # Hard guard against a perfect title from the wrong act: when the candidate exposes
    # an artist and it clearly disagrees, push it below any neutral/correct-artist
    # source so it can't win on title alone.
    if vid_artist and a_sim < 0.34:
        score -= 0.25

    age_limit = int(entry.get("age_limit") or 0)
    exp = explicit_score(vid_full_title, age_limit if age_limit >= 18 else None)
    if exp != 0:
        # Make a labelled version decisive: the ≈0.30 spread exceeds the title-similarity
        # gap between near-identical "Song" vs "Song (Clean)" uploads.
        if prefer_explicit:
            score += 0.15 if exp > 0 else -0.18
        else:
            score += 0.15 if exp < 0 else -0.18

    if looks_like_live(vid_title) and not looks_like_live(title):
        # Live/concert uploads surface too often over the studio cut; a heavy penalty
        # pushes them below a near-tied studio version.
        score -= 0.45

    return score, is_official


def _entry_url(entry: dict) -> str:
    vid_id = entry.get("id") or ""
    return str(entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}")


def yt_search_best(
    artist: str,
    title: str,
    duration_seconds: int | None = None,
    n_candidates: int = 10,
    prefer_ytm: bool = True,
    prefer_explicit: bool = True,
) -> tuple[str, float]:
    """Search for the best-matching studio version on YouTube (Music).

    Scores up to n_candidates results with `score_yt_candidate` and returns
    (url, score). Score < 0.35 = no match.

    Retrieval breadth matters more than score precision here: a wrong pick is usually a
    *missing* correct candidate, so we pull a wide pool (default 10) and let the
    channel/duration signals surface the official audio.
    """
    query = f"{artist} {title}"
    entries = _yt_search_entries(query, n_candidates, prefer_ytm)
    if not entries:
        return f"ytsearch1:{query}", 0.0

    best_url = ""
    best_score = -1.0
    for entry in entries:
        score, _ = score_yt_candidate(entry, artist, title, duration_seconds, prefer_explicit)
        if score > best_score:
            best_score = score
            best_url = _entry_url(entry)

    return best_url or f"ytsearch1:{query}", max(best_score, 0.0)


def yt_search_ranked(
    artist: str,
    title: str,
    duration_seconds: int | None = None,
    *,
    query: str | None = None,
    n_candidates: int = 10,
    prefer_ytm: bool = True,
    prefer_explicit: bool = True,
) -> list[dict]:
    """Ranked YouTube candidate pool (best-first) for the review "Different source" panel.

    Same retrieval + scoring as `yt_search_best`, but returns every candidate with its
    score and signals so the user sees — and can swap to — exactly what the auto-picker
    weighed. ``query`` overrides the search terms (a free-text box) while scoring still
    happens against the wanted (artist, title, duration). Returns a list of dicts keyed
    for ``source_replace_results.html`` (``provider_ref`` = the swap target URL).
    """
    q = (query or f"{artist} {title}").strip()
    entries = _yt_search_entries(q, n_candidates, prefer_ytm)
    results: list[dict] = []
    for entry in entries:
        score, is_official = score_yt_candidate(
            entry, artist, title, duration_seconds, prefer_explicit
        )
        dur = entry.get("duration")
        abr = entry.get("abr") or entry.get("tbr")
        results.append({
            "provider_ref": _entry_url(entry),
            "title": str(entry.get("track") or entry.get("title") or "Unknown"),
            "artist": str(entry.get("artist") or entry.get("uploader") or entry.get("channel") or ""),
            "duration_seconds": int(dur) if dur else None,
            "thumbnail_url": entry.get("thumbnail"),
            "abr": int(abr) if abr else None,
            "score": round(score, 2),
            "is_official": is_official,
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results
