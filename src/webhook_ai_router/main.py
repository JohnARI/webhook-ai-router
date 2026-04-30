"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webhook_ai_router.api.middleware import RequestIDMiddleware
from webhook_ai_router.api.routes import health, webhooks
from webhook_ai_router.config import get_settings
from webhook_ai_router.core.exceptions import WebhookError
from webhook_ai_router.core.logging import configure_logging
from webhook_ai_router.infra.redis import create_redis_client
from webhook_ai_router.schemas.errors import ProblemDetail

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"
ERROR_TYPE_BASE: Final = "https://errors.webhook-ai-router/"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.app_env, settings.log_level)
    redis = create_redis_client(settings.redis_url)
    app.state.redis = redis
    try:
        yield
    finally:
        await redis.aclose()  # type: ignore[attr-defined]


def _problem_response(request: Request, exc: WebhookError) -> JSONResponse:
    problem = ProblemDetail(
        type=f"{ERROR_TYPE_BASE}{type(exc).__name__}",
        title=exc.title,
        status=exc.status_code,
        detail=exc.detail,
        instance=str(request.url),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=problem.model_dump(mode="json"),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="webhook-ai-router", lifespan=_lifespan)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(webhooks.router)
    app.include_router(health.router)

    @app.exception_handler(WebhookError)
    async def _webhook_error_handler(request: Request, exc: WebhookError) -> JSONResponse:
        return _problem_response(request, exc)

    return app


app = create_app()
