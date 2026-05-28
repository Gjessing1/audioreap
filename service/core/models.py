from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    mb_release_group_id: str | None = None  # for release-group preference in text search
    # Explicit lock: when True the pipeline never lets MB re-route this track to
    # a different album, regardless of whether track_number is populated.
    album_locked: bool = False
    # When True, the dedup check in run_acquisition is skipped.  Used for
    # "replace source" jobs where the existing track IS the local match.
    skip_dedup: bool = False


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
        "done", "failed", "cancelled", "needs_review",
    ]
    progress: float | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    is_replacement: bool = False


class AlbumCandidate(BaseModel):
    """An album resolved to an ordered track list (e.g. from a playlist URL)."""

    provider: str
    provider_ref: str          # playlist URL or other album identifier
    album_title: str
    album_artist: str
    year: int | None = None
    track_count: int | None = None
    tracks: list[TrackCandidate] = []


class ResolvedTrackMetadata(BaseModel):
    """Typed metadata handoff between identification (Phase 1) and approval (Phase 2).

    Built in ``pipeline.run_acquisition``, persisted as JSON on
    ``AcquisitionJobRow.resolved_metadata_json``, then consumed in
    ``pipeline.place_approved_track`` when the user approves.

    ``extra="allow"`` keeps this forward- and backward-compatible: enrichment rows
    carry ``current_*`` fields, legacy/synthesised rows may omit fields, and future
    scoring breakdowns can be added without breaking deserialisation — unknown keys
    survive a load→dump round-trip rather than being dropped or rejected.
    """

    model_config = ConfigDict(extra="allow")

    # Core tags
    title: str = "Unknown"
    artist: str = "Unknown"
    albumartist: str = ""
    album: str | None = None
    year: int | None = None
    original_year: int | None = None
    track_number: int | None = None
    disc_number: int | None = None
    duration_seconds: int | None = None
    genre: str | None = None
    ext: str = "ogg"

    # Source audio
    source_codec: str | None = None
    source_bitrate_kbps: int | None = None

    # MusicBrainz / AcoustID identification
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_release_group_id: str | None = None
    mb_artist_id: str | None = None
    mb_artist_sort: str | None = None
    isrc: str | None = None
    acoustid_confidence: float | None = None
    text_search_similarity: float | None = None
    mb_match_source: str | None = None
    mb_genres: list[str] = []

    # Review / placement state
    is_compilation: bool = False
    force_staging_reason: str | None = None
    quality_score: float = 0.0
    thumbnail_url: str | None = None
    is_replacement: bool = False
    is_enrichment: bool = False

    # Metadata provenance (which source contributed each field)
    prov_title: str | None = None
    prov_artist: str | None = None
    prov_album: str | None = None
    prov_year: str | None = None
    prov_recording: str | None = None


# Re-export the async generator type alias used by providers
ProviderSearchStream = AsyncGenerator[TrackCandidate, None]
