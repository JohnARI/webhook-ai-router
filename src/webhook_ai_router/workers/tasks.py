"""arq task definitions and worker settings.

The worker process boots from :class:`WorkerSettings`. ``on_startup`` builds
the long-lived collaborators (``AsyncAnthropic`` LLM client, ``httpx.AsyncClient``,
:class:`webhook_ai_router.config.Settings`) and stows them on the ctx; the
task pulls them off, never imports them at module scope.

We never log raw payloads — only ``event_id``, ``source``, ``idempotency_key``,
``category``, ``confidence``, and a dispatch summary. Adding a payload field
to a log call is a review-blocker.
"""

from __future__ import annotations

from typing import Any, Final

import httpx
import structlog

from webhook_ai_router.config import Settings, get_settings
from webhook_ai_router.core.logging import configure_logging
from webhook_ai_router.infra.arq import redis_settings_from_url
from webhook_ai_router.services.dispatch import dispatch
from webhook_ai_router.services.llm import (
    AnthropicLLMClient,
    LLMClassificationError,
    LLMClient,
)

log = structlog.get_logger(__name__)

QUEUE_NAME: Final = "arq:queue"


async def process_webhook(
    ctx: dict[str, Any],
    event_id: str,
    source: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Classify the payload via the LLM, then fan out to dispatch targets."""
    llm: LLMClient = ctx["llm"]
    http: httpx.AsyncClient = ctx["http"]
    settings: Settings = ctx["settings"]

    try:
        enrichment = await llm.classify_lead(payload)
    except LLMClassificationError as exc:
        log.error(
            "task.classification_failed",
            event_id=event_id,
            source=source,
            idempotency_key=idempotency_key,
            error=str(exc),
        )
        # TODO(session-5): persist to DLQ instead of re-raising.
        raise

    enriched_payload = {
        "event_id": event_id,
        "source": source,
        "category": enrichment.category,
        "confidence": enrichment.confidence,
        "reason": enrichment.reason,
        "data": payload,
    }

    results = await dispatch(
        http,
        settings.dispatch_targets,
        enriched_payload,
        total_timeout_seconds=float(settings.dispatch_total_timeout_seconds),
    )

    log.info(
        "task.processed",
        event_id=event_id,
        source=source,
        idempotency_key=idempotency_key,
        category=enrichment.category,
        confidence=enrichment.confidence,
        dispatch=[
            {
                "url": r.url,
                "success": r.success,
                "status": r.status_code,
                "attempts": r.attempts,
            }
            for r in results
        ],
    )

    return {
        "event_id": event_id,
        "category": enrichment.category,
        "dispatch_count": len(results),
        "dispatch_succeeded": sum(1 for r in results if r.success),
    }


async def _on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.app_env, settings.log_level)

    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the worker — set it in .env")

    ctx["settings"] = settings
    ctx["llm"] = AnthropicLLMClient(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.anthropic_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    ctx["http"] = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
    )
    log.info("worker.startup", model=settings.anthropic_model)


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    http: httpx.AsyncClient | None = ctx.get("http")
    llm: LLMClient | None = ctx.get("llm")
    if http is not None:
        await http.aclose()
    if llm is not None:
        await llm.close()
    log.info("worker.shutdown")


class WorkerSettings:
    """arq WorkerSettings — discovered by ``arq.run_worker``.

    ``redis_settings`` is evaluated at class-body-execution time. We rely on
    pydantic-settings reading ``.env``; all keys have safe defaults.
    """

    functions = [process_webhook]
    queue_name = QUEUE_NAME
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 10
    job_timeout = 180  # > dispatch budget (120s) + LLM (~10s) + slack
    keep_result = 3600
    redis_settings = redis_settings_from_url(get_settings().redis_url)
