from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUDIOREAP_", env_file=".env", extra="ignore")

    # Paths
    music_dir: Path = Path("/music")
    data_dir: Path = Path("/data")
    cache_dir: Path = Path("/cache")
    tmp_acquire_dir: Path = Path("/tmp-acquire")

    # Database
    db_url: str = "sqlite+aiosqlite:////data/audioreap.db"

    # Redis / arq
    redis_url: str = "redis://redis:6379"

    # Navidrome
    navidrome_url: str = "http://navidrome:4533"
    navidrome_user: str = "admin"
    navidrome_password: str = ""

    # Worker
    worker_concurrency: int = 2
    # arq queue poll interval (seconds). arq busy-polls Redis for due jobs every
    # poll_delay; the default 0.5s is ~2 zrangebyscore/sec forever and dominates
    # idle Redis CPU. Downloads are not latency-sensitive to sub-second pickup, so
    # a longer delay trades a little job-start latency for much lower idle load.
    worker_poll_delay_seconds: float = 2.0

    # Auth (optional — leave empty to rely on a reverse proxy for auth)
    ui_username: str = ""
    ui_password: str = ""

    # Spotify (optional — enables Spotify playlist resolution)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # Logging
    log_format: str = "pretty"  # "json" in prod

    # AcoustID (optional — enables fingerprint-based MB matching)
    acoustid_api_key: str = ""

    # Staging (tracks below threshold land here for review before Navidrome sees them)
    staging_dir: Path = Path("/music-staging")
    # Unused since the review gate became universal (every track stops at
    # needs_review regardless of score). Kept so an existing env var stays
    # harmless; not exposed in Settings.
    staging_quality_threshold: float = 0.40

    # Quality review
    min_bitrate_kbps: int = 128  # Tracks below this are flagged as low quality

    # Acquisition preferences
    prefer_explicit: bool = True  # Rank explicit versions above clean when searching

    # What the per-track ARTIST tag says on a various-artists compilation.
    # ALBUMARTIST is always "Various Artists"; this decides whether every
    # PERFORMER also becomes its own artist entry in Navidrome — a 20-track
    # "Now That's What I Call Music" otherwise adds 20 one-track artists.
    #   "append_to_title" — ARTIST = "Various Artists" and the performer moves
    #                       into the title: "Silent Night (Mahalia Jackson)".
    #                       One artist entry, performer still visible.
    #   "album_artist"    — ARTIST = "Various Artists", title untouched. Cleanest
    #                       artist list, but no client can show who performs.
    #   "keep"            — every performer stays in ARTIST, and becomes its own
    #                       artist entry.
    # The replaced credit always survives in ORIGINALARTIST, so any mode is
    # reversible from the file alone.
    compilation_artist_mode: str = "append_to_title"

    # Lyrics — fetch synced/plain lyrics from LRCLIB (free, no key) and write a
    # .lrc sidecar next to each track on approval. Navidrome reads these natively.
    lyrics_enabled: bool = True

    # yt-dlp rate limiting — pace downloads to a slow, steady stream that stays
    # *under* YouTube's rate limit rather than bursting into a 429. Adaptive: the
    # interval grows after a 429 and relaxes back toward the minimum on success.
    ytdlp_rate_limit_enabled: bool = True
    ytdlp_min_download_interval_seconds: float = 5.0   # steady spacing between download starts
    ytdlp_max_download_interval_seconds: float = 45.0  # cap the interval may back off to
    ytdlp_rate_cooldown_seconds: float = 120.0         # hard pause after a 429 is seen

    # Optional YouTube auth — only needed if logged-out 429s persist (the adaptive
    # gate normally keeps us under the limit without it). These make yt-dlp's
    # requests look authenticated. All blank by default = anonymous access.
    ytdlp_cookies_file: str = ""     # path to a Netscape cookies.txt mounted into the container
    ytdlp_player_client: str = ""    # yt-dlp youtube player_client, comma-sep (e.g. "web_safari,web")
    ytdlp_po_token: str = ""         # yt-dlp youtube po_token(s), comma-sep (e.g. "web.gvs+XXXX")

    # Auto-rescan interval (0 = disabled)
    rescan_interval_minutes: int = 0

    # Daily "fix file tags" sweep: rewrite album/albumartist/year + canonical
    # MUSICBRAINZ_ALBUMID across every album so Navidrome doesn't fragment them.
    # Opt-in; runs once a day (see worker cron). Off by default.
    auto_fix_tags_enabled: bool = False

    # Algorithm versions — increment to trigger migrations
    id_algorithm_version: int = 1
    normalize_version: int = 1
    layout_version: int = 2


settings = Settings()

# Path for runtime config overrides (written by /admin/config UI)
_OVERRIDES_FILE = settings.data_dir / "config_overrides.json"

# Keys that the config UI may override (must match Settings field names).
# staging_quality_threshold is deliberately absent: the universal review gate
# replaced score-based staging, so the knob no longer influences anything and
# showing it in Settings only promised behaviour that never happens.
CONFIG_EDITABLE_KEYS = (
    "min_bitrate_kbps",
    "prefer_explicit",
    "lyrics_enabled",
    "worker_concurrency",
    "rescan_interval_minutes",
    "auto_fix_tags_enabled",
    "compilation_artist_mode",
)

# Accepted values for `compilation_artist_mode`, best-first. Anything else is a
# typo in an env var or a hand-edited overrides file, and must not silently
# choose a different tagging policy — see `compilation_artist_mode()`.
COMPILATION_ARTIST_MODES = ("append_to_title", "album_artist", "keep")


def compilation_artist_mode() -> str:
    """The validated compilation ARTIST policy (see the Settings field above).

    `load_config_overrides` writes straight onto the settings object without
    pydantic validation, and the env var is free text, so an unrecognised value
    falls back to the shipped default rather than quietly reverting to "keep".
    """
    mode = str(getattr(settings, "compilation_artist_mode", "") or "").strip().lower()
    if mode in COMPILATION_ARTIST_MODES:
        return mode
    return str(Settings.model_fields["compilation_artist_mode"].default)


def config_defaults() -> dict:
    """The value each editable key ships with, straight off the model fields.

    Settings renders a "differs from default" marker and a one-click restore from
    this, so it stays correct automatically when a default is ever retuned.
    """
    return {key: Settings.model_fields[key].default for key in CONFIG_EDITABLE_KEYS}


def read_config_overrides() -> dict:
    """Overrides currently persisted by the Settings UI ({} when none/unreadable).

    Unlike the live ``settings`` object this shows what the *UI* set, which is
    what "stored in config_overrides.json" in Settings reports.
    """
    import json
    try:
        stored = json.loads(_OVERRIDES_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {k: v for k, v in stored.items() if k in CONFIG_EDITABLE_KEYS}


def load_config_overrides() -> None:
    """Apply /data/config_overrides.json on top of the singleton settings object.

    Called once at startup after migrations. Changes take effect without
    editing the .env file; a process restart is still required.
    """
    import json
    if not _OVERRIDES_FILE.exists():
        return
    try:
        overrides = json.loads(_OVERRIDES_FILE.read_text())
        for key, value in overrides.items():
            if key in CONFIG_EDITABLE_KEYS and hasattr(settings, key):
                field_type = type(getattr(settings, key))
                try:
                    setattr(settings, key, field_type(value))
                except Exception:
                    pass
    except Exception:
        pass


def save_config_overrides(overrides: dict) -> None:
    """Persist runtime overrides to /data/config_overrides.json and apply them."""
    import json
    _OVERRIDES_FILE.parent.mkdir(parents=True, exist_ok=True)
    clean: dict = {}
    for key in CONFIG_EDITABLE_KEYS:
        if key in overrides:
            clean[key] = overrides[key]
    _OVERRIDES_FILE.write_text(json.dumps(clean, indent=2))
    load_config_overrides()
