"""Application settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import assert_never

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from webhook_ai_router.schemas.webhooks import WebhookSource


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    hubspot_webhook_secret: SecretStr | None = None

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
    """FastAPI dependency that returns a cached :class:`Settings` instance."""
    return Settings()
