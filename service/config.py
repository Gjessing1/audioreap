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

    # Logging
    log_format: str = "pretty"  # "json" in prod

    # Algorithm versions — increment to trigger migrations
    id_algorithm_version: int = 1
    normalize_version: int = 1
    layout_version: int = 1


settings = Settings()
