"""yt-dlp provider.

One class, many sites — YouTube, SoundCloud, Bandcamp, and any other
yt-dlp-supported extractor. Adding Bandcamp is a config flag, not new code.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import threading
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


_COOKIE_COPY_LOCK = threading.Lock()
_COOKIE_COPY_PATH: Path | None = None


def _writable_cookies(src: str) -> str | None:
    """Return a writable copy of the cookies file for yt-dlp to use.

    yt-dlp saves the refreshed cookie jar *back* to ``cookiefile`` after a download
    (rotating session tokens). Our cookies.txt is bind-mounted ``:ro`` for safety, so
    writing there raises ``[Errno 30] Read-only file system``. Copy the source jar to
    a writable temp file once (re-copying only when the source changes) and hand
    yt-dlp that copy — its write-backs land harmlessly on the copy, the real jar stays
    untouched. Returns None (caller falls back to no cookies) if the copy can't be made.
    """
    global _COOKIE_COPY_PATH
    p = Path(src)
    try:
        src_mtime = p.stat().st_mtime
    except OSError:
        return None
    with _COOKIE_COPY_LOCK:
        dst = _COOKIE_COPY_PATH
        if dst is None:
            fd, name = tempfile.mkstemp(prefix="audioreap-cookies-", suffix=".txt")
            os.close(fd)
            dst = Path(name)
            _COOKIE_COPY_PATH = dst
        try:
            # Refresh the copy when the user pastes a newer jar. yt-dlp's own
            # write-backs bump the copy's mtime past the source, so they don't
            # trigger a re-copy (and aren't clobbered).
            if not dst.exists() or dst.stat().st_mtime < src_mtime:
                shutil.copyfile(p, dst)
                os.utime(dst, (src_mtime, src_mtime))
        except OSError:
            return None
        return str(dst)


def _has_login_cookie(p: Path) -> bool:
    """True when the jar carries at least one real (tab-separated, non-comment) cookie.

    The placeholder jar we ship has only ``#`` comment lines, so its mere presence
    isn't enough to count as "logged in".
    """
    try:
        for line in p.read_text(errors="ignore").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "\t" in line:
                return True
    except OSError:
        pass
    return False


def managed_cookies_path() -> Path:
    """Writable jar the UI uploads to — lives in the persistent /data volume."""
    from service.config import settings

    return settings.data_dir / "cookies.txt"


def active_cookies_file() -> str | None:
    """Resolve which cookies jar to actually use, or None for anonymous access.

    A UI-uploaded jar in /data wins over the env-mounted file (which is typically
    bind-mounted ``:ro`` and may be just the header-only placeholder). Either source
    only counts when it carries a real login cookie.
    """
    from service.config import settings

    managed = managed_cookies_path()
    if managed.is_file() and _has_login_cookie(managed):
        return str(managed)
    env = (getattr(settings, "ytdlp_cookies_file", "") or "").strip()
    if env and Path(env).is_file() and _has_login_cookie(Path(env)):
        return env
    return None


def _youtube_auth_opts() -> dict[str, object]:
    """Optional yt-dlp auth so requests look logged-in (cookies / PO-token).

    All blank by default → anonymous access (the adaptive rate gate normally keeps
    us under the limit without credentials). Set the env vars only if logged-out
    429s persist. Returns {} when nothing is configured.
    """
    from service.config import settings

    opts: dict[str, object] = {}
    cookies = active_cookies_file()
    if cookies:
        writable = _writable_cookies(cookies)
        if writable:
            opts["cookiefile"] = writable

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


def cookies_are_configured() -> bool:
    """True when a usable login jar is available (UI-uploaded or env-mounted).

    Used to decide whether an age-gated download can be rescued by substituting a
    non-gated source (no usable cookies) or should be left to the cookies to handle
    (cookies present).
    """
    return active_cookies_file() is not None


_VIDEO_ID_RE = re.compile(r"(?:v=|/watch\?.*v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


def video_id_from_url(url: str) -> str | None:
    """Extract the 11-char YouTube video id from a watch/short URL, else None."""
    if not url:
        return None
    m = _VIDEO_ID_RE.search(url)
    if m:
        return m.group(1)
    # Bare id or trailing-id form (…/v/<id>): take the last 11-char id-shaped token.
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return tail if re.fullmatch(r"[A-Za-z0-9_-]{11}", tail) else None


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
        # A stalled connection must not hang a worker slot indefinitely —
        # yt-dlp has no overall deadline, but a per-socket timeout unsticks
        # both metadata extraction and downloads.
        "socket_timeout": 30,
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
# "Official" artist channels. A match is a strong signal we have the right source —
# but only when the channel belongs to the *wanted* artist (see score_yt_candidate).
_OFFICIAL_CHANNEL_RE = re.compile(r"(-\s*topic$|vevo|\bofficial\b)", re.IGNORECASE)

# Branding suffixes on official channels: "Adele - Topic" → "Adele",
# "AdeleVEVO" → "Adele", "Queen Official" → "Queen". Stripped before artist
# comparison — flat search entries never carry a clean `artist` field, so the
# channel name is the only artist evidence and the suffix would otherwise drag
# similarity below the wrong-artist guard for the artist's own channel.
_CHANNEL_SUFFIX_RES = [
    re.compile(r"\s*-\s*topic\s*$", re.IGNORECASE),
    re.compile(r"\s*vevo\s*$", re.IGNORECASE),
    re.compile(r"\s+official\s*$", re.IGNORECASE),
    re.compile(r"\s+music\s*$", re.IGNORECASE),
]

# Version mutations endemic to YouTube that are never the studio cut we want:
# sped up / slowed+reverb / nightcore / 8D / bass boosted / hour loops / remixes /
# full-album rips. Penalised only when the *wanted* title doesn't ask for them.
_UNWANTED_VERSION_RE = re.compile(
    r"\b(?:sped[\s-]*up|slowed(?:\s*(?:\+|and|&|n)?\s*reverb)?|night\s*core|nightcore|"
    r"8d(?:\s+audio)?|bass\s*boost(?:ed)?|reverb(?:ed)?|remix(?:ed)?|mashup|"
    r"full\s+album|(?:\d+|one)\s+hours?(?:\s+loop)?|loop(?:ed)?|"
    r"extended\s+(?:mix|version|edit)|pitch(?:ed)?\s*(?:up|down)|chipmunks?)\b",
    re.IGNORECASE,
)


# Scoring weight table for score_yt_candidate. The base score is a weighted sum of
# title/artist/duration similarity; everything below it is a bonus/penalty applied on
# top. The artist gate (hard demotion of a clearly-wrong act) shares its threshold and
# penalty with the MB candidate ranker — one tuning pass must move both call sites.
_W_TITLE = 0.50
_W_ARTIST = 0.25
_W_DURATION = 0.20
_OFFICIAL_CHANNEL_BONUS = 0.12
_GROSS_DURATION_PENALTY = 0.35
_EXPLICIT_MATCH_BONUS = 0.15
_EXPLICIT_MISMATCH_PENALTY = 0.18
_LIVE_PENALTY = 0.45
_UNWANTED_VERSION_PENALTY = 0.45


_BRACKETED_RE = re.compile(r"\s*[\(\[\{][^\)\]\}]*[\)\]\}]")


def _debracketed(title: str) -> str:
    """Title with all bracketed segments removed.

    MB recording titles are bare ("Derezzed") while YouTube titles carry
    decorations ("Derezzed (From TRON: Legacy)") that wreck token-set similarity.
    Safe to ignore for *scoring* because the version-mutation signals
    (live/remix/nightcore/…) are detected on the full title separately.
    """
    return _BRACKETED_RE.sub("", title).strip()


def _channel_artist(channel: str) -> str:
    """Channel name with official-channel branding suffixes stripped.

    Best-effort artist evidence for flat search entries, where the channel/uploader
    is all we get ("Adele - Topic", "AdeleVEVO", "Queen Official").
    """
    result = channel
    for pat in _CHANNEL_SUFFIX_RES:
        result = pat.sub("", result)
    result = result.strip()
    return result or channel

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


def source_title_hints(video_title: str) -> list[dict[str, str]]:
    """Version signals in a downloaded YouTube title, for the review UI.

    Returns chips as {"label", "kind"} where kind is "warn" (probably not the
    studio cut the user wanted: live/cover, clean/radio edit, remix/sped-up…)
    or "ok" (explicit — usually the wanted version). Empty list = no signals.
    """
    hints: list[dict[str, str]] = []
    if looks_like_live(video_title):
        hints.append({"label": "live / cover?", "kind": "warn"})
    if _CLEAN_RE.search(video_title):
        hints.append({"label": "clean version?", "kind": "warn"})
    elif _EXPLICIT_RE.search(video_title):
        hints.append({"label": "explicit", "kind": "ok"})
    m = _UNWANTED_VERSION_RE.search(video_title)
    if m:
        hints.append({"label": m.group(0).lower(), "kind": "warn"})
    return hints


def _yt_search_entries(query: str, n_candidates: int) -> list[dict]:
    """Run a flat YouTube search and return the raw entry dicts.

    Plain ``ytsearch`` is the primary pool: its flat entries carry the signals the
    scorer needs (channel, duration, view count). Shared by yt_search_best and
    yt_search_ranked so both see the same pool.
    """
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "socket_timeout": 30, **_youtube_auth_opts()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n_candidates}:{query}", download=False)
        return [e for e in ((info or {}).get("entries") or []) if e]
    except Exception:
        return []


def _yt_music_search_entries(query: str, n_candidates: int) -> list[dict]:
    """Flat YouTube Music search via the music.youtube.com search URL.

    yt-dlp has **no** ``ytmsearch`` prefix — only the search-URL extractor reaches
    the YTM index. Its flat entries are signal-poor (title + id only: no channel,
    duration, or artist), so this pool is a *rescue* supplement when plain search
    scores badly, never a replacement. Non-video results (channels, playlists) are
    filtered out by the 11-char video-id shape.
    """
    import urllib.parse

    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "socket_timeout": 30, **_youtube_auth_opts()}
    url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = [
            e for e in ((info or {}).get("entries") or [])
            if e and e.get("title") and len(str(e.get("id") or "")) == 11
        ]
        return entries[:n_candidates]
    except Exception:
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
    from service.metadata.candidates import ARTIST_GATE_PENALTY, ARTIST_GATE_SIM
    from service.search.matcher import (
        artist_similarity as _asim,
        title_similarity as _tsim,
    )

    vid_title = str(entry.get("track") or entry.get("title") or "")
    # The cleaned "track" field often drops the "(Explicit)"/"(Clean)" suffix, so scan
    # the full raw title for the explicit/clean/live/version signals as well.
    vid_full_title = str(entry.get("title") or vid_title)
    # YouTube Music sets a dedicated "artist" on full extraction; flat search entries
    # (the only thing the pickers see) never do — there the channel/uploader is the
    # only artist evidence, with its branding suffix stripped ("Adele - Topic" → "Adele").
    explicit_artist = str(entry.get("artist") or "")
    vid_channel = str(entry.get("channel") or entry.get("uploader") or "")
    channel_artist = _channel_artist(vid_channel) if vid_channel else ""
    vid_dur = entry.get("duration")

    # Two readings of the candidate: (title-as-is, channel/explicit artist) and — for
    # the pervasive "Artist - Title" upload convention — (after-dash title, dash artist).
    # Score both and keep the stronger pair, so "Adele - Hello" from "Adele - Topic"
    # reads as title="Hello", artist="Adele" instead of a diluted token soup.
    readings = [(vid_title, explicit_artist or channel_artist)]
    if not explicit_artist and " - " in vid_title:
        dash_artist, dash_title = vid_title.split(" - ", 1)
        readings.append((dash_title.strip(), dash_artist.strip()))

    t_sim = a_sim = 0.0
    best_core = -1.0
    has_artist_evidence = False
    for cand_title, cand_artist in readings:
        t = _tsim(title, cand_title)
        stripped = _debracketed(cand_title)
        if stripped and stripped != cand_title:
            t = max(t, _tsim(title, stripped))
        # A blank candidate artist scores neutral, not zero, so we don't punish
        # sources that simply don't expose an artist field.
        a = _asim(artist, cand_artist) if cand_artist else 0.5
        core = t * _W_TITLE + a * _W_ARTIST
        if core > best_core:
            best_core, t_sim, a_sim = core, t, a
            has_artist_evidence = bool(cand_artist)

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
        elif delta <= 75:
            dur_score = 0.4
        else:
            # A source grossly off the locked MB recording's length is almost never
            # the same cut (wrong song, extended/sped edit, full-album upload).
            dur_score = 0.1
            gross_dur_mismatch = True

    score = t_sim * _W_TITLE + a_sim * _W_ARTIST + dur_score * _W_DURATION

    # Prefer official audio: "- Topic" / VEVO / labelled official artist channel — but
    # only when it's the *wanted* artist's channel. Another act's Topic/VEVO channel
    # (covers, karaoke factories) must not inherit the bonus.
    is_official = bool(vid_channel and _OFFICIAL_CHANNEL_RE.search(vid_channel))
    if is_official:
        if channel_artist and _asim(artist, channel_artist) >= 0.5:
            score += _OFFICIAL_CHANNEL_BONUS
        else:
            is_official = False

    if gross_dur_mismatch:
        score -= _GROSS_DURATION_PENALTY

    # Hard guard against a perfect title from the wrong act: when the candidate exposes
    # an artist and it clearly disagrees, push it below any neutral/correct-artist
    # source so it can't win on title alone.
    if has_artist_evidence and a_sim < ARTIST_GATE_SIM:
        score -= ARTIST_GATE_PENALTY

    age_limit = int(entry.get("age_limit") or 0)
    exp = explicit_score(vid_full_title, age_limit if age_limit >= 18 else None)
    if exp != 0:
        # Make a labelled version decisive: the ≈0.30 spread exceeds the title-similarity
        # gap between near-identical "Song" vs "Song (Clean)" uploads.
        if prefer_explicit:
            score += _EXPLICIT_MATCH_BONUS if exp > 0 else -_EXPLICIT_MISMATCH_PENALTY
        else:
            score += _EXPLICIT_MATCH_BONUS if exp < 0 else -_EXPLICIT_MISMATCH_PENALTY

    if looks_like_live(vid_full_title) and not looks_like_live(title):
        # Live/concert uploads surface too often over the studio cut; a heavy penalty
        # pushes them below a near-tied studio version.
        score -= _LIVE_PENALTY

    if _UNWANTED_VERSION_RE.search(vid_full_title) and not _UNWANTED_VERSION_RE.search(title):
        # Sped-up / nightcore / remix / full-album mutations of the right song: same
        # class of wrong-source as live uploads, same decisive penalty.
        score -= _UNWANTED_VERSION_PENALTY

    return score, is_official


def _entry_url(entry: dict) -> str:
    vid_id = entry.get("id") or ""
    return str(entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}")


# Below this best-score, the plain-search pool is considered weak enough to be worth
# a second search against the YouTube Music index (signal-poor entries cap out around
# 0.73, so they can only ever win as a rescue, never override a confident pick).
_YTM_RESCUE_SCORE = 0.60


def yt_search_best(
    artist: str,
    title: str,
    duration_seconds: int | None = None,
    n_candidates: int = 10,
    prefer_ytm: bool = True,
    prefer_explicit: bool = True,
    exclude_ids: set[str] | None = None,
) -> tuple[str, float]:
    """Search for the best-matching studio version on YouTube (Music).

    Scores up to n_candidates results with `score_yt_candidate` and returns
    (url, score). Score < 0.35 = no match. ``exclude_ids`` drops candidates by
    video id — used to pick the next-best source after one age-gates or fails.

    Retrieval breadth matters more than score precision here: a wrong pick is usually a
    *missing* correct candidate, so we pull a wide pool (default 10) and let the
    channel/duration signals surface the official audio. When the plain-search pool
    scores badly (< _YTM_RESCUE_SCORE) and prefer_ytm is set, the YouTube Music index
    is searched as a rescue pool — its top "song" results are usually the canonical
    studio audio even when plain search drowns in reuploads.
    """
    query = f"{artist} {title}"
    entries = _yt_search_entries(query, n_candidates)
    excluded = exclude_ids or set()

    best_url = ""
    best_score = -1.0
    seen_ids: set[str] = set()
    for entry in entries:
        eid = str(entry.get("id") or "")
        seen_ids.add(eid)
        if eid in excluded:
            continue
        score, _ = score_yt_candidate(entry, artist, title, duration_seconds, prefer_explicit)
        if score > best_score:
            best_score = score
            best_url = _entry_url(entry)

    if prefer_ytm and best_score < _YTM_RESCUE_SCORE:
        for entry in _yt_music_search_entries(query, n_candidates):
            eid = str(entry.get("id") or "")
            if eid in seen_ids or eid in excluded:
                continue
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

    Same scoring as `yt_search_best`, but returns every candidate with its score and
    signals so the user sees — and can swap to — exactly what the auto-picker weighed.
    Retrieval is a superset: the YouTube Music pool (which the picker only fetches as
    a rescue) is always merged in here, since a human is choosing. ``query`` overrides
    the search terms (a free-text box) while scoring still happens against the wanted
    (artist, title, duration). Returns a list of dicts keyed for
    ``source_replace_results.html`` (``provider_ref`` = the swap target URL).
    """
    q = (query or f"{artist} {title}").strip()
    entries = _yt_search_entries(q, n_candidates)
    if prefer_ytm:
        seen_ids = {str(e.get("id") or "") for e in entries}
        entries += [
            e for e in _yt_music_search_entries(q, n_candidates)
            if str(e.get("id") or "") not in seen_ids
        ]
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
