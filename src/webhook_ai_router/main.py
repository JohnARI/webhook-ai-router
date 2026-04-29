"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from webhook_ai_router.api.routes import health
from webhook_ai_router.config import get_settings
from webhook_ai_router.core.logging import configure_logging


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.app_env, settings.log_level)
    yield


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="webhook-ai-router", lifespan=_lifespan)
    app.include_router(health.router)
    return app


app = create_app()
