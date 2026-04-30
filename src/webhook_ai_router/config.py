"""Application configuration loaded from environment / .env."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import assert_never

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from webhook_ai_router.schemas.dispatch import DispatchTarget
from webhook_ai_router.schemas.webhooks import WebhookSource


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
    ``APP_ENV``, the connection URLs, ``ANTHROPIC_API_KEY``, and
    ``DISPATCH_TARGETS``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # Runtime
    app_env: AppEnv = AppEnv.DEV
    log_level: LogLevel = LogLevel.INFO
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/webhook_ai_router"

    # Idempotency
    idempotency_ttl_seconds: int = 86_400  # 24h, Stripe-compatible default
    idempotency_lock_ttl_seconds: int = 60

    # Webhook secrets
    hubspot_webhook_secret: SecretStr | None = None

    # LLM enrichment
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-4-6"
    llm_timeout_seconds: float = 10.0

    # Downstream dispatch (parsed as JSON when read from env vars)
    dispatch_targets: list[DispatchTarget] = []
    dispatch_total_timeout_seconds: int = 120

    def secret_for(self, source: WebhookSource) -> str:
        """Return the shared HMAC secret for a given webhook source."""
        match source:
            case WebhookSource.HUBSPOT:
                if not self.hubspot_webhook_secret:
                    raise RuntimeError("hubspot_webhook_secret is not set")
                return self.hubspot_webhook_secret.get_secret_value()
            case _:  # pragma: no cover - exhaustiveness guard
                assert_never(source)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """FastAPI dependency returning a cached :class:`Settings` instance."""
    return Settings()
