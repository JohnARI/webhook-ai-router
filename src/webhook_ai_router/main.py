"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from webhook_ai_router.api.middleware import RequestIDMiddleware
from webhook_ai_router.api.routes import health, webhooks
from webhook_ai_router.config import get_settings
from webhook_ai_router.core.exceptions import (
    IdempotencyConflictError,
    IdempotencyKeyMissingError,
    PayloadInvalidError,
    SignatureInvalidError,
    TimestampExpiredError,
    WebhookError,
)
from webhook_ai_router.core.logging import configure_logging
from webhook_ai_router.core.metrics import WEBHOOK_RECEIVED_TOTAL
from webhook_ai_router.db.session import create_db_engine, create_db_sessionmaker
from webhook_ai_router.infra.arq import create_arq_pool
from webhook_ai_router.infra.redis import create_redis_client
from webhook_ai_router.schemas.errors import ProblemDetail

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"
ERROR_TYPE_BASE: Final = "https://errors.webhook-ai-router/"

# Map a WebhookError subclass to the metric label we want to record.
# Anything missing falls back to "error" so cardinality stays bounded.
_RECEIVED_STATUS_BY_EXC: Final[dict[type[WebhookError], str]] = {
    IdempotencyKeyMissingError: "missing_key",
    IdempotencyConflictError: "conflict",
    SignatureInvalidError: "unauthorized",
    TimestampExpiredError: "unauthorized",
    PayloadInvalidError: "invalid",
}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(settings.app_env, settings.log_level)

    redis = create_redis_client(settings.redis_url)
    arq_pool = await create_arq_pool(settings.redis_url)
    db_engine = create_db_engine(settings.database_url)
    db_sessionmaker = create_db_sessionmaker(db_engine)
    app.state.redis = redis
    app.state.arq_pool = arq_pool
    app.state.db_engine = db_engine
    app.state.db_sessionmaker = db_sessionmaker

    log = structlog.get_logger(__name__)
    try:
        yield
    finally:
        # Close each independently so a failure on one doesn't leak the others.
        try:
            # arq's ArqRedis stubs don't expose ``aclose`` even though the
            # runtime API has it (added alongside redis-py's deprecation of
            # the sync ``close``). Drop the ignore once stubs catch up.
            await arq_pool.aclose()  # type: ignore[attr-defined]
        except Exception:  # log + continue, must close others too
            log.exception("lifespan.arq_close_failed")
        try:
            # types-redis lags the actual ``redis.asyncio.Redis.aclose``
            # method; same story as the arq pool above.
            await redis.aclose()  # type: ignore[attr-defined]
        except Exception:
            log.exception("lifespan.redis_close_failed")
        try:
            await db_engine.dispose()
        except Exception:
            log.exception("lifespan.db_close_failed")


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


def _record_received_failure(request: Request, exc: WebhookError) -> None:
    """Increment ``webhook_received_total`` from the exception path.

    The route increments ``status="accepted"`` / ``"cached"`` itself for the
    success and replay paths; this handler covers every other terminal
    status that goes through :class:`WebhookError`.
    """
    source = request.path_params.get("source", "unknown")
    status_label = _RECEIVED_STATUS_BY_EXC.get(type(exc), "error")
    WEBHOOK_RECEIVED_TOTAL.labels(source=source, status=status_label).inc()


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="webhook-ai-router", lifespan=_lifespan)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(webhooks.router)
    app.include_router(health.router)

    # Default HTTP request/response/latency metrics + ``GET /metrics``.
    Instrumentator(
        excluded_handlers=["/metrics", "/healthz", "/readyz"],
    ).instrument(app).expose(app, include_in_schema=False)

    @app.exception_handler(WebhookError)
    async def _webhook_error_handler(request: Request, exc: WebhookError) -> JSONResponse:
        _record_received_failure(request, exc)
        return _problem_response(request, exc)

    return app


app = create_app()
