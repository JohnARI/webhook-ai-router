"""Event persistence service.

Wraps :class:`AsyncSession` so neither the FastAPI route nor the arq task
touches the ORM directly. This is the **only** layer that imports from
``webhook_ai_router.db``; everything else gets a typed repository.

Schemas (Pydantic) and models (SQLAlchemy) NEVER share classes — see the
``_dispatch_summary`` helper for the explicit schema → JSON conversion
that crosses the boundary.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_ai_router.db.models import DeadLetterEvent, EventStatus, WebhookEvent
from webhook_ai_router.db.session import get_db_session
from webhook_ai_router.schemas.dispatch import DispatchResult
from webhook_ai_router.schemas.enrichment import EnrichmentResult


class DuplicateIdempotencyKeyError(Exception):
    """Raised when the unique idempotency_key constraint fires.

    The Redis idempotency cache should have caught this earlier; this is a
    defense-in-depth signal that two webhook submissions raced past Redis.
    """

    def __init__(self, idempotency_key: str) -> None:
        super().__init__(f"Idempotency-Key already persisted: {idempotency_key!r}")
        self.idempotency_key = idempotency_key


# Backwards-compatible alias — preferred name is the Error-suffixed one.
DuplicateIdempotencyKey = DuplicateIdempotencyKeyError


def _dispatch_summary(results: list[DispatchResult]) -> dict[str, Any]:
    """Convert dispatch result schemas to a JSON-serialisable dict.

    Lives here (not on the schema) because it's a persistence-side
    representation — schemas stay pure.
    """
    return {
        "results": [
            {
                "url": r.url,
                "success": r.success,
                "status_code": r.status_code,
                "attempts": r.attempts,
                "error": r.error,
            }
            for r in results
        ],
        "total": len(results),
        "succeeded": sum(1 for r in results if r.success),
    }


class EventRepository:
    """Async persistence operations for webhook events + DLQ.

    A fresh instance wraps a fresh session per request / per worker job.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_received(
        self,
        *,
        source: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> uuid.UUID:
        """Insert a new event in ``received`` status. Commits immediately so
        the row is visible before we enqueue the arq job.
        """
        event = WebhookEvent(
            source=source,
            idempotency_key=idempotency_key,
            payload=payload,
            status=EventStatus.RECEIVED,
        )
        self._session.add(event)
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicateIdempotencyKeyError(idempotency_key) from exc
        await self._session.refresh(event)
        return event.id

    async def get(self, event_id: uuid.UUID) -> WebhookEvent | None:
        result = await self._session.execute(
            select(WebhookEvent).where(WebhookEvent.id == event_id)
        )
        return result.scalar_one_or_none()

    async def mark_processing(self, event_id: uuid.UUID) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(status=EventStatus.PROCESSING)
        )
        await self._session.commit()

    async def mark_dispatched(
        self,
        event_id: uuid.UUID,
        *,
        enrichment: EnrichmentResult,
        dispatch_results: list[DispatchResult],
        attempts: int,
    ) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(
                status=EventStatus.DISPATCHED,
                enrichment={
                    "category": enrichment.category,
                    "reason": enrichment.reason,
                    "confidence": enrichment.confidence,
                    "dispatch": _dispatch_summary(dispatch_results),
                },
                dispatch_attempts=attempts,
                last_error=None,
            )
        )
        await self._session.commit()

    async def mark_failed(
        self,
        event_id: uuid.UUID,
        *,
        error: str,
        attempts: int,
    ) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(WebhookEvent.id == event_id)
            .values(
                status=EventStatus.FAILED,
                last_error=error,
                dispatch_attempts=attempts,
            )
        )
        await self._session.commit()

    async def insert_dead_letter(
        self,
        *,
        original_event_id: uuid.UUID,
        final_error: str,
        retry_count: int,
    ) -> uuid.UUID:
        dlq = DeadLetterEvent(
            original_event_id=original_event_id,
            final_error=final_error,
            retry_count=retry_count,
        )
        self._session.add(dlq)
        await self._session.commit()
        await self._session.refresh(dlq)
        return dlq.id


async def get_event_repository(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> EventRepository:
    """FastAPI dependency wiring an :class:`EventRepository`."""
    return EventRepository(session)
