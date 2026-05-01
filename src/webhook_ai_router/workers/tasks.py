"""arq task definitions and worker settings.

The worker process boots from :class:`WorkerSettings`. ``on_startup`` builds
the long-lived collaborators (``AsyncAnthropic`` LLM client, ``httpx.AsyncClient``,
async DB engine + sessionmaker, :class:`webhook_ai_router.config.Settings`)
and stows them on the ctx; the task pulls them off, never imports them at
module scope.

Status flow (persisted via :class:`EventRepository`):

* Route writes ``received``.
* Task entry → ``processing``.
* Successful dispatch → ``dispatched`` with enrichment + summary stored.
* Any classification or dispatch failure → ``failed`` with ``last_error``.
* On the final retry (``ctx['job_try'] >= MAX_TRIES``), insert a
  :class:`DeadLetterEvent` and **do not re-raise** so arq treats the job as
  done; the DLQ row is the durable record of the failure.

We never log raw payloads — only ``event_id``, ``source``,
``idempotency_key``, ``category``, ``confidence``, and a dispatch summary.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Final

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from webhook_ai_router.config import Settings, get_settings
from webhook_ai_router.core.logging import configure_logging
from webhook_ai_router.db.session import create_db_engine, create_db_sessionmaker
from webhook_ai_router.infra.arq import redis_settings_from_url
from webhook_ai_router.services.dispatch import dispatch
from webhook_ai_router.services.events import EventRepository
from webhook_ai_router.services.llm import (
    AnthropicLLMClient,
    LLMClassificationError,
    LLMClient,
)

# A factory that yields a per-job EventRepository wrapping a fresh session.
# Production wires it from the sessionmaker; tests inject a stub yielding
# a FakeEventRepository so they don't need a real Postgres.
EventsFactory = Callable[[], AbstractAsyncContextManager[EventRepository]]

log = structlog.get_logger(__name__)

QUEUE_NAME: Final = "arq:queue"
MAX_TRIES: Final = 5  # mirrors WorkerSettings.max_tries below


async def process_webhook(
    ctx: dict[str, Any],
    event_id: str,
    source: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict[str, Any]:
    """Classify the payload via the LLM, fan out to dispatch targets, persist
    status transitions, and shunt to the DLQ on the final retry.
    """
    llm: LLMClient = ctx["llm"]
    http: httpx.AsyncClient = ctx["http"]
    settings: Settings = ctx["settings"]
    events_factory: EventsFactory = ctx["events_factory"]
    job_try: int = int(ctx.get("job_try", 1))

    event_uuid = uuid.UUID(event_id)

    async with events_factory() as events:
        await events.mark_processing(event_uuid)

        try:
            enrichment = await llm.classify_lead(payload)
        except LLMClassificationError as exc:
            await _handle_failure(
                events,
                event_uuid=event_uuid,
                source=source,
                idempotency_key=idempotency_key,
                error=f"classification: {exc}",
                job_try=job_try,
            )
            if job_try >= MAX_TRIES:
                # Already DLQ'd — don't re-raise so arq stops retrying.
                return _summary(event_id, category=None, dispatch_count=0, succeeded=0)
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
        succeeded = sum(1 for r in results if r.success)

        if results and succeeded == 0:
            # All targets failed — treat the whole task as a retryable failure
            # so arq can have another go (and we DLQ on the final attempt).
            errors = "; ".join(f"{r.url}={r.error or r.status_code}" for r in results)
            await _handle_failure(
                events,
                event_uuid=event_uuid,
                source=source,
                idempotency_key=idempotency_key,
                error=f"dispatch: {errors}",
                job_try=job_try,
            )
            if job_try >= MAX_TRIES:
                return _summary(
                    event_id,
                    category=enrichment.category,
                    dispatch_count=len(results),
                    succeeded=succeeded,
                )
            raise RuntimeError(f"dispatch failed for all {len(results)} target(s)")

        await events.mark_dispatched(
            event_uuid,
            enrichment=enrichment,
            dispatch_results=results,
            attempts=job_try,
        )

        log.info(
            "task.processed",
            event_id=event_id,
            source=source,
            idempotency_key=idempotency_key,
            category=enrichment.category,
            confidence=enrichment.confidence,
            attempts=job_try,
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

        return _summary(
            event_id,
            category=enrichment.category,
            dispatch_count=len(results),
            succeeded=succeeded,
        )


async def _handle_failure(
    events: EventRepository,
    *,
    event_uuid: uuid.UUID,
    source: str,
    idempotency_key: str | None,
    error: str,
    job_try: int,
) -> None:
    """Mark the event failed and, on the final attempt, write a DLQ row."""
    await events.mark_failed(event_uuid, error=error, attempts=job_try)
    log.warning(
        "task.attempt_failed",
        event_id=str(event_uuid),
        source=source,
        idempotency_key=idempotency_key,
        attempt=job_try,
        max_tries=MAX_TRIES,
        error=error,
    )
    if job_try >= MAX_TRIES:
        await events.insert_dead_letter(
            original_event_id=event_uuid,
            final_error=error,
            retry_count=job_try,
        )
        log.error(
            "task.dlq",
            event_id=str(event_uuid),
            source=source,
            idempotency_key=idempotency_key,
            retry_count=job_try,
            error=error,
        )


def _summary(
    event_id: str, *, category: str | None, dispatch_count: int, succeeded: int
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "category": category,
        "dispatch_count": dispatch_count,
        "dispatch_succeeded": succeeded,
    }


def make_sessionmaker_events_factory(
    sessionmaker: async_sessionmaker[Any],
) -> EventsFactory:
    """Build the production ``events_factory``: a fresh session per task."""

    @asynccontextmanager
    async def _factory() -> AsyncIterator[EventRepository]:
        async with sessionmaker() as session:
            yield EventRepository(session)

    return _factory


async def _on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.app_env, settings.log_level)

    if settings.anthropic_api_key is None:
        raise RuntimeError("ANTHROPIC_API_KEY is required to run the worker — set it in .env")

    db_engine: AsyncEngine = create_db_engine(settings.database_url)
    sessionmaker = create_db_sessionmaker(db_engine)

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
    ctx["db_engine"] = db_engine
    ctx["events_factory"] = make_sessionmaker_events_factory(sessionmaker)
    log.info("worker.startup", model=settings.anthropic_model)


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    http: httpx.AsyncClient | None = ctx.get("http")
    llm: LLMClient | None = ctx.get("llm")
    db_engine: AsyncEngine | None = ctx.get("db_engine")
    if http is not None:
        await http.aclose()
    if llm is not None:
        await llm.close()
    if db_engine is not None:
        await db_engine.dispose()
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
    max_tries = MAX_TRIES
    redis_settings = redis_settings_from_url(get_settings().redis_url)
