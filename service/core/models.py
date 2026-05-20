from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class TrackQuality(BaseModel):
    codec: str
    container: str
    bitrate_kbps: int | None = None
    sample_rate_hz: int | None = None


class TrackRef(BaseModel):
    internal_id: str
    source: Literal["local", "cloud"]
    status: Literal["available", "acquiring", "failed", "missing"]

    title: str
    artist: str
    album: str | None = None
    duration_seconds: int | None = None

    provider: str | None = None
    provider_ref: str | None = None
    local_path: Path | None = None

    musicbrainz_recording_id: str | None = None
    quality: TrackQuality | None = None


class AlbumRef(BaseModel):
    internal_id: str
    title: str
    artist: str
    year: int | None = None
    track_count: int | None = None
    musicbrainz_release_id: str | None = None
    provider: str | None = None
    provider_ref: str | None = None
    source: Literal["local", "cloud"]


class ArtistRef(BaseModel):
    internal_id: str
    name: str
    musicbrainz_artist_id: str | None = None
    source: Literal["local", "cloud"]


class SearchQuery(BaseModel):
    q: str
    limit: int = 20
    offset: int = 0


class SearchResult(BaseModel):
    tracks: list[TrackRef]
    albums: list[AlbumRef]
    artists: list[ArtistRef]
    query_echo: str


class TrackCandidate(BaseModel):
    """Returned by Provider.search() — pre-fetch, some fields may be absent."""

    provider: str
    provider_ref: str
    title: str
    artist: str
    album: str | None = None
    year: int | None = None
    track_number: int | None = None
    duration_seconds: int | None = None
    quality_hint: TrackQuality | None = None
    thumbnail_url: str | None = None
    raw_metadata: dict[str, object] = {}
    # Set by album-coordinator jobs — prevents MB text search from overriding
    # placement metadata (album/year/track_number) with a different release.
    mb_release_id: str | None = None
    mb_recording_id: str | None = None


class FetchResult(BaseModel):
    """Returned by Provider.fetch() — post-download, all fields known."""

    file_path: Path
    provider: str
    provider_ref: str
    source_url: str | None = None
    codec: str
    container: str
    bitrate_kbps: int | None = None
    sample_rate_hz: int | None = None
    raw_metadata: dict[str, object] = {}


class ProviderHealth(BaseModel):
    healthy: bool
    message: str | None = None
    checked_at: datetime


class AcquisitionJob(BaseModel):
    id: str
    track_ref: TrackRef
    state: Literal[
        "queued", "downloading", "processing", "tagging", "importing",
        "done", "failed", "cancelled", "staged", "needs_review",
    ]
    progress: float | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class AlbumCandidate(BaseModel):
    """An album resolved to an ordered track list (e.g. from a playlist URL)."""

    provider: str
    provider_ref: str          # playlist URL or other album identifier
    album_title: str
    album_artist: str
    year: int | None = None
    track_count: int | None = None
    tracks: list[TrackCandidate] = []


# Re-export the async generator type alias used by providers
ProviderSearchStream = AsyncGenerator[TrackCandidate, None]
