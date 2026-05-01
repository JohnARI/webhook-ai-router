"""``POST /webhooks/{source}`` — verify, parse, persist, enqueue.

Request flow:

1. Reject if ``Idempotency-Key`` header is absent (400).
2. Cache hit → return the previously-served response immediately.
3. Try to acquire a Redis lock on the key. If held, 409.
4. Re-check the cache under the lock to absorb the race where another
   request finished between our miss and our lock acquisition.
5. Verify HMAC, parse payload, **insert WebhookEvent (status=received)**,
   **enqueue arq job**, build the 202 response.
6. Cache the success response, then release the lock and return.

The DB unique constraint on ``idempotency_key`` is defense-in-depth — if
two requests squeeze past Redis, the second one's INSERT fails with
:class:`DuplicateIdempotencyKey` and we surface it as a 409 conflict.
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from webhook_ai_router.config import Settings, get_settings
from webhook_ai_router.core.exceptions import (
    IdempotencyConflictError,
    IdempotencyKeyMissingError,
)
from webhook_ai_router.core.idempotency import (
    CachedResponse,
    IdempotencyStore,
    get_idempotency_store,
)
from webhook_ai_router.core.security import verify_hmac
from webhook_ai_router.infra.arq import get_arq_pool
from webhook_ai_router.schemas.webhooks import WebhookSource
from webhook_ai_router.services.events import (
    DuplicateIdempotencyKey,
    EventRepository,
    get_event_repository,
)
from webhook_ai_router.services.ingest import parse_webhook_event, parsed_to_dict

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger(__name__)


class WebhookQueued(BaseModel):
    """Response body for a queued webhook delivery."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    status: Literal["queued"] = "queued"


def _response_from_cache(cached: CachedResponse) -> Response:
    return Response(
        content=cached.body,
        status_code=cached.status_code,
        headers=cached.headers,
    )


@router.post(
    "/{source}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_202_ACCEPTED: {"model": WebhookQueued},
    },
)
async def receive_webhook(
    source: WebhookSource,
    request: Request,
    x_signature: Annotated[str, Header(alias="X-Signature")],
    x_timestamp: Annotated[str, Header(alias="X-Timestamp")],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency: Annotated[IdempotencyStore, Depends(get_idempotency_store)],
    arq_pool: Annotated[ArqRedis, Depends(get_arq_pool)],
    events: Annotated[EventRepository, Depends(get_event_repository)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    if not idempotency_key:
        raise IdempotencyKeyMissingError()

    cached = await idempotency.get(idempotency_key)
    if cached is not None:
        return _response_from_cache(cached)

    if not await idempotency.lock(idempotency_key):
        raise IdempotencyConflictError()

    try:
        # Re-check after acquiring the lock — another worker may have just
        # finished while we were racing for it.
        cached = await idempotency.get(idempotency_key)
        if cached is not None:
            return _response_from_cache(cached)

        body = await request.body()
        secret = settings.secret_for(source)
        verify_hmac(secret, body, x_signature, x_timestamp)

        event = parse_webhook_event(source, body)
        payload_dict = parsed_to_dict(event)

        # Persist BEFORE enqueue so the worker can find the row, and the DB
        # unique constraint catches anything that beat Redis.
        try:
            event_uuid = await events.create_received(
                source=source.value,
                idempotency_key=idempotency_key,
                payload=payload_dict,
            )
        except DuplicateIdempotencyKey as exc:
            raise IdempotencyConflictError(str(exc)) from exc

        event_id = str(event_uuid)

        # Enqueue OUTSIDE the cache write — if this raises, don't cache, so
        # a retry can re-enqueue. _job_id=event_id gives queue-level dedup
        # alongside the idempotency cache and DB unique constraint.
        await arq_pool.enqueue_job(
            "process_webhook",
            event_id,
            source.value,
            payload_dict,
            idempotency_key,
            _job_id=event_id,
            _queue_name="arq:queue",
        )

        log.info(
            "webhook.queued",
            event_id=event_id,
            source=source.value,
            event_count=len(event.events),
            idempotency_key=idempotency_key,
        )

        accepted_body = WebhookQueued(event_id=event_id).model_dump_json().encode("utf-8")
        cached_resp = CachedResponse(
            status_code=status.HTTP_202_ACCEPTED,
            headers={"content-type": "application/json"},
            body=accepted_body,
        )
        await idempotency.set(idempotency_key, cached_resp)

        return Response(
            content=accepted_body,
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )
    finally:
        await idempotency.unlock(idempotency_key)
