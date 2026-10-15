"""Application settings, loaded from environment variables (and a local .env file)."""

from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr
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
