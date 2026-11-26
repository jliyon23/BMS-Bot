from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """12-factor configuration. All values can be set with BMS_* env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="BMS_",
        extra="ignore",
    )

    app_name: str = "BMS Bot"
    env: str = "dev"
    debug: bool = False

    # Relative SQLite paths are resolved against the project root, never the
    # process working directory. This prevents a second empty DB being
    # created when a command is started from a different folder.
    data_dir: Path = PROJECT_ROOT / "data"
    database_url: str = ""

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    admin_secret_key: str = "change-this-in-production"
    admin_session_ttl_sec: int = 1800

    # BMS endpoints
    bms_base_url: str = "https://in.bookmyshow.com"
    bms_canary_event: str = "ET00489093"
    bms_canary_region: str = "HYD"
    bms_impersonations: str = "chrome,chrome131,safari_ios"

    # Direct mode is available for local testing through the admin toggle.
    # In production it is refused unless this explicit escape hatch is set.
    allow_direct: bool = False

    # Proxy hunter
    proxy_country: str = "IN"
    proxy_free_enabled: bool = True
    proxy_validation_iterations: int = 5
    proxy_validation_required: int = 4
    proxy_validation_required_rotating: int = 2
    proxy_max_p95_ms: int = 8000
    proxy_max_p95_ms_rotating: int = 15000
    proxy_refresh_sec: int = 900
    proxy_recheck_sec: int = 600
    proxy_candidate_limit: int = 150
    proxy_validation_batch: int = 15
    proxy_validation_concurrency: int = 4
    proxy_encryption_key: str = ""

    # Rotating proxy retry/failure policy
    rotating_retries: int = 3
    rotating_failure_threshold: int = 3
    rotating_cooldown_sec: int = 60

    # Scraper cadence
    poll_interval_sec: int = 10
    seat_poll_interval_sec: int = 30
    max_concurrent_scrapes: int = 4
    max_requests_per_sec: float = 2.0
    max_seat_checks_per_scan: int = 10
    seat_alert_threshold: int = 5
    seat_alert_cooldown_sec: int = 60

    # Worker identity
    worker_id: str = "worker"

    @field_validator("data_dir", mode="before")
    @classmethod
    def _resolve_data_dir(cls, value: object) -> Path:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        return path

    @model_validator(mode="after")
    def _resolve_database_url(self) -> Settings:
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{self.data_dir}/bmsbot.db"
            return self

        if self.database_url.startswith("sqlite"):
            try:
                url = make_url(self.database_url)
            except Exception:
                return self
            database = url.database
            if database and database != ":memory:" and not Path(database).is_absolute():
                absolute = (PROJECT_ROOT / database).resolve()
                self.database_url = str(url.set(database=str(absolute)))
        return self

    @field_validator("poll_interval_sec", "seat_poll_interval_sec")
    @classmethod
    def _minimum_interval(cls, value: int) -> int:
        if value < 10:
            raise ValueError("polling interval cannot be below 10 seconds")
        return value

    @property
    def impersonations(self) -> tuple[str, ...]:
        items = tuple(part.strip() for part in self.bms_impersonations.split(",") if part.strip())
        return items or ("chrome",)

    @property
    def proxy_sources(self) -> tuple[str, ...]:
        return ("geonode", "proxifly", "proxyscrape")

    @property
    def direct_allowed(self) -> bool:
        return self.allow_direct or self.env != "prod"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
