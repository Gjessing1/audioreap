"""yt-dlp provider.

One class, many sites — YouTube, SoundCloud, Bandcamp, and any other
yt-dlp-supported extractor. Adding Bandcamp is a config flag, not new code.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from service.core.models import FetchResult, ProviderHealth, SearchQuery, TrackCandidate
from service.providers import register
from service.providers.base import Provider, ProviderCapabilities

logger = logging.getLogger(__name__)

_DEFAULT_SEARCH_LIMIT = 5
_SEARCH_PREFIX = "ytsearch"


def _ydl_opts_base() -> dict[str, object]:
    return {
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
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
            title = entry.get("title") or "Unknown"
            uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
            duration = entry.get("duration")

            yield TrackCandidate(
                provider=self.name,
                provider_ref=video_url,
                title=title,
                artist=uploader,
                duration_seconds=int(duration) if duration else None,
                thumbnail_url=entry.get("thumbnail"),
                raw_metadata={k: v for k, v in entry.items() if isinstance(v, (str, int, float, bool))},
            )

    async def fetch(self, provider_ref: str, dest_dir: Path) -> FetchResult:
        import yt_dlp

        downloaded_path: list[Path] = []

        def _progress_hook(d: dict[str, object]) -> None:
            if d.get("status") == "finished":
                fp = d.get("filename")
                if fp:
                    downloaded_path.append(Path(str(fp)))

        opts: dict[str, object] = {
            **_ydl_opts_base(),
            "format": "bestaudio/best",
            "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
            "progress_hooks": [_progress_hook],
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(provider_ref, download=True)

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
            source_url=provider_ref,
            codec=codec,
            container=container,
            bitrate_kbps=int(bitrate) if bitrate else None,
            sample_rate_hz=int(sample_rate) if sample_rate else None,
            raw_metadata={k: v for k, v in info.items() if isinstance(v, (str, int, float, bool))},
        )

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
