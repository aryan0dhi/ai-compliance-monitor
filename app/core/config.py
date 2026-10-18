"""Application settings, loaded from environment variables (and a local .env file)."""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"

    # /v1/test/* routes (pod contract, Section 5). Must stay off in production.
    enable_test_routes: bool = False

    # PostgreSQL. Either set DATABASE_URL directly, or the individual POSTGRES_* parts
    # (Lightsail managed databases expose host/port/user/password separately).
    database_url: SecretStr | None = None
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "compliance"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "compliance"
    postgres_sslmode: str | None = None

    db_pool_size: int = 5
    db_max_overflow: int = 5

    # REQ-04 / REQ-02: maximum delay between detection and reviewer notification (15 minutes).
    notify_max_delay_s: int = Field(default=900, gt=0)
    # Background worker: how often it sweeps for gaps and delivers notifications. Must be well
    # under notify_max_delay_s.
    worker_poll_interval_s: float = Field(default=5.0, gt=0)
    # /healthz reports the worker as stale when its last heartbeat is older than this.
    worker_stale_after_s: float = Field(default=60.0, gt=0)

    # Webhook delivery (POST /v1/webhooks).
    webhook_timeout_s: float = Field(default=5.0, gt=0, le=30)
    webhook_max_attempts: int = Field(default=10, ge=1)
    # Allow webhook URLs on private, loopback or link-local addresses (local development only).
    # Ignored when APP_ENV=production, which also requires https.
    webhook_allow_private_networks: bool = False

    @model_validator(mode="after")
    def _poll_faster_than_notification_deadline(self) -> "Settings":
        if self.worker_poll_interval_s >= self.notify_max_delay_s:
            raise ValueError("WORKER_POLL_INTERVAL_S must be shorter than NOTIFY_MAX_DELAY_S")
        return self

    @property
    def webhook_private_networks_allowed(self) -> bool:
        return self.webhook_allow_private_networks and self.app_env != AppEnv.PRODUCTION

    @property
    def webhook_https_required(self) -> bool:
        return self.app_env == AppEnv.PRODUCTION

    @property
    def sqlalchemy_url(self) -> URL | str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        query = {"sslmode": self.postgres_sslmode} if self.postgres_sslmode else {}
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value() or None,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
            query=query,
        )

    @property
    def test_routes_enabled(self) -> bool:
        return self.enable_test_routes and self.app_env != AppEnv.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    return Settings()
