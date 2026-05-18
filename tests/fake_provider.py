"""FakeProvider — test double for the Provider interface.

Pre-seeded with known metadata backed by WAV fixtures checked into
tests/fixtures/audio/. Every integration test uses this; no network calls.
"""
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from service.core.models import (
    AlbumCandidate,
    FetchResult,
    ProviderHealth,
    SearchQuery,
    TrackCandidate,
)
from service.providers.base import Provider, ProviderCapabilities

_CATALOGUE: list[dict[str, object]] = [
    {
        "provider_ref": "fake-001",
        "title": "Test Track One",
        "artist": "Fake Artist",
        "album": "Fake Album",
        "duration_seconds": 1,
        "fixture": "tone_1s.wav",
    },
    {
        "provider_ref": "fake-002",
        "title": "Test Track Two",
        "artist": "Another Artist",
        "album": None,
        "duration_seconds": 1,
        "fixture": "tone_1s.wav",
    },
]

# Album catalogue: 4 tracks on one album (used by album tests)
_ALBUM_TRACKS: list[dict[str, object]] = [
    {"provider_ref": "fake-album-01", "title": "Album Track One",   "track_index": 1},
    {"provider_ref": "fake-album-02", "title": "Album Track Two",   "track_index": 2},
    {"provider_ref": "fake-album-03", "title": "Album Track Three", "track_index": 3},
    {"provider_ref": "fake-album-04", "title": "Album Track Four",  "track_index": 4},
]

FAKE_ALBUM_REF = "fake://album/discovery"
FAKE_ALBUM_TITLE = "Fake Discovery"
FAKE_ALBUM_ARTIST = "Fake Daft Punk"


class FakeProvider(Provider):
    name = "fake"
    capabilities = ProviderCapabilities(
        supports_search=True,
        supports_album_search=True,
        supports_quality_selection=True,
        search_is_async=False,
        requires_credentials=False,
    )

    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir

    async def search(self, query: SearchQuery) -> AsyncIterator[TrackCandidate]:  # type: ignore[override]
        q = query.q.lower()
        for entry in _CATALOGUE:
            title = str(entry["title"])
            artist = str(entry["artist"])
            if q in title.lower() or q in artist.lower() or q == "":
                yield TrackCandidate(
                    provider=self.name,
                    provider_ref=str(entry["provider_ref"]),
                    title=title,
                    artist=artist,
                    album=entry.get("album") and str(entry["album"]) or None,
                    duration_seconds=int(entry["duration_seconds"]),  # type: ignore[arg-type]
                    raw_metadata={"source": "fake"},
                )

    async def fetch_album(self, album_ref: str) -> AlbumCandidate:
        tracks = [
            TrackCandidate(
                provider=self.name,
                provider_ref=str(t["provider_ref"]),
                title=str(t["title"]),
                artist=FAKE_ALBUM_ARTIST,
                album=FAKE_ALBUM_TITLE,
                duration_seconds=1,
                raw_metadata={"track_index": t["track_index"]},
            )
            for t in _ALBUM_TRACKS
        ]
        return AlbumCandidate(
            provider=self.name,
            provider_ref=album_ref,
            album_title=FAKE_ALBUM_TITLE,
            album_artist=FAKE_ALBUM_ARTIST,
            track_count=len(tracks),
            tracks=tracks,
        )

    async def fetch(self, provider_ref: str, dest_dir: Path) -> FetchResult:
        all_entries = _CATALOGUE + [
            {**t, "album": FAKE_ALBUM_TITLE, "duration_seconds": 1, "fixture": "tone_1s.wav"}
            for t in _ALBUM_TRACKS
        ]
        entry = next((e for e in all_entries if e["provider_ref"] == provider_ref), None)
        if entry is None:
            raise ValueError(f"Unknown provider_ref: {provider_ref!r}")

        fixture_path = self._fixture_dir / str(entry["fixture"])
        # Disambiguate filenames within the same dest_dir by using provider_ref
        safe_ref = str(provider_ref).replace("/", "_").replace(":", "_")
        dest_path = dest_dir / f"{safe_ref}_{fixture_path.name}"
        shutil.copy2(fixture_path, dest_path)

        return FetchResult(
            file_path=dest_path,
            provider=self.name,
            provider_ref=provider_ref,
            source_url=None,
            codec="pcm_s16le",
            container="wav",
            bitrate_kbps=None,
            sample_rate_hz=44100,
            raw_metadata={"source": "fake", "ref": provider_ref},
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            healthy=True,
            message="fake provider always healthy",
            checked_at=datetime.now(UTC),
        )
