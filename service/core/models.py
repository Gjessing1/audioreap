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
    disc_number: int | None = None
    duration_seconds: int | None = None
    quality_hint: TrackQuality | None = None
    thumbnail_url: str | None = None
    raw_metadata: dict[str, object] = {}
    # Full artist credit ("Bjørn Eidsvåg med Lisa Nilsson") when `artist` holds
    # only the collapsed primary. Preserved into the ORIGINALARTIST tag so the
    # substitution is visible and reversible from the file itself.
    artist_credit: str | None = None
    # Set by the album coordinator. `artist` is the per-track PERFORMER, which on
    # a compilation is nothing like the album artist, so the album artist can no
    # longer be inferred from it and must be carried explicitly.
    albumartist: str | None = None
    mb_albumartist_id: str | None = None
    mb_artist_id: str | None = None
    is_compilation: bool = False
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
    # Absolute path of the existing library file this job replaces. Carried so
    # Phase 2 can trash the original even when the replacement remuxes to a
    # different extension (e.g. .mp3 → .ogg), where the recomputed destination
    # path no longer matches the original and the old file would otherwise survive.
    replace_path: str | None = None


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
        "queued", "waiting", "downloading", "processing", "tagging", "importing",
        "placing", "done", "failed", "cancelled", "needs_review",
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


class CandidateScore(BaseModel):
    """One ranked MB candidate's component scores — stored for review-card observability.

    Mirrors ``metadata.candidates.ScoredCandidate`` but holds only the display fields
    (no MBRecording object), so it serialises cleanly into resolved_metadata_json.
    """

    recording_id: str
    title: str
    artist: str
    text_sim: float
    query_sim: float = 0.0
    acoustid_match: bool = False
    combined: float
    artist_sim: float = 1.0
    artist_penalty: float = 0.0


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
    # The album artist was settled from an album that already exists on disk
    # (enrichment: the file cannot move, so its grouping is a fact). Nothing may
    # re-derive it from the track artist — see `_apply_review_overrides`.
    albumartist_locked: bool = False
    # The credit `artist` was collapsed out of, when a guest was dropped so the
    # library wouldn't gain an artist that doesn't exist. Written to
    # ORIGINALARTIST; None when nothing was replaced.
    original_artist: str | None = None
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
    # Canonical URL of the actual media the audio was fetched from (e.g. the
    # resolved YouTube watch URL, not the `ytsearch1:` query). Surfaced in the
    # review card so the user can open the source and validate the pick fast.
    source_url: str | None = None
    # The provider and the reference it actually fetched from — post-download, so
    # this is the substituted video on an age-gate retry, not the `ytsearch1:`
    # expression the job was created with. Persisted onto the TrackFile row at
    # approval so "where did this file come from?" survives the job's deletion and
    # one-click re-acquire has something to re-fetch.
    source_provider: str | None = None
    source_provider_ref: str | None = None
    # Human-readable description of that media: the raw video title (with all
    # its "(Live)" / "(Clean)" decorations intact), the uploading channel, and
    # the media length. Lets the review UI show WHAT was downloaded without a
    # network call, so wrong-version picks are visible before approval.
    source_title: str | None = None
    source_channel: str | None = None
    source_duration_seconds: int | None = None

    # MusicBrainz / AcoustID identification
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_release_group_id: str | None = None
    mb_artist_id: str | None = None
    # The album artist's own MBID, which Navidrome keys albumartist identity on.
    # Never the performer's on a compilation — see `place_approved_track`.
    mb_albumartist_id: str | None = None
    mb_artist_sort: str | None = None
    isrc: str | None = None
    acoustid_confidence: float | None = None
    text_search_similarity: float | None = None
    mb_match_source: str | None = None
    mb_genres: list[str] = []
    # Phase 1 observability: the ranked candidate pool with per-candidate component
    # scores (text_sim / query_sim / acoustid_match / combined), best-first. Empty
    # for Path A (locked recording) and when no candidates were found.
    candidates: list[CandidateScore] = []

    # Review / placement state
    is_compilation: bool = False
    force_staging_reason: str | None = None
    quality_score: float = 0.0
    thumbnail_url: str | None = None
    is_replacement: bool = False
    is_enrichment: bool = False
    # Original library file path being replaced (see TrackCandidate.replace_path).
    replace_path: str | None = None

    # Metadata provenance (which source contributed each field)
    prov_title: str | None = None
    prov_artist: str | None = None
    prov_album: str | None = None
    prov_year: str | None = None
    prov_recording: str | None = None


# Re-export the async generator type alias used by providers
ProviderSearchStream = AsyncGenerator[TrackCandidate, None]
