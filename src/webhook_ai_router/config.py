"""Application configuration loaded from environment / .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    DEV = "dev"
    PROD = "prod"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Runtime configuration.

    All values come from environment variables (or a ``.env`` file). Defaults
    are tuned for local development; production must override at minimum
    ``APP_ENV`` and the connection URLs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_env: AppEnv = AppEnv.DEV
    log_level: LogLevel = LogLevel.INFO
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/webhook_ai_router"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency returning a cached :class:`Settings` instance."""
    return Settings()
