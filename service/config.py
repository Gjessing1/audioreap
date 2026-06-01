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
    staging_quality_threshold: float = 0.40  # ~3/7 factors; set 0.0 to disable staging

    # Quality review
    min_bitrate_kbps: int = 128  # Tracks below this are flagged as low quality

    # Acquisition preferences
    prefer_explicit: bool = True  # Rank explicit versions above clean when searching

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

# Keys that the config UI may override (must match Settings field names)
CONFIG_EDITABLE_KEYS = (
    "staging_quality_threshold",
    "min_bitrate_kbps",
    "prefer_explicit",
    "worker_concurrency",
    "rescan_interval_minutes",
    "auto_fix_tags_enabled",
)


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
