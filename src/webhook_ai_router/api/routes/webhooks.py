"""``POST /webhooks/{source}`` — verify, parse, accept."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict

from webhook_ai_router.core.security import verify_hmac
from webhook_ai_router.core.settings import Settings, get_settings
from webhook_ai_router.schemas.webhooks import WebhookSource
from webhook_ai_router.services.ingest import parse_webhook_event

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = structlog.get_logger(__name__)


class WebhookAccepted(BaseModel):
    """Response body for an accepted webhook delivery."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    status: Literal["accepted"] = "accepted"


@router.post(
    "/{source}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
)
async def receive_webhook(
    source: WebhookSource,
    request: Request,
    x_signature: Annotated[str, Header(alias="X-Signature")],
    x_timestamp: Annotated[str, Header(alias="X-Timestamp")],
    settings: Annotated[Settings, Depends(get_settings)],
) -> WebhookAccepted:
    """Authenticate the webhook (HMAC + replay window), parse it, ack 202.

    Persistence and downstream dispatch are wired up in a later session; for
    now we just generate an event id and log it.
    """
    body = await request.body()
    secret = settings.secret_for(source)

    verify_hmac(secret, body, x_signature, x_timestamp)

    event = parse_webhook_event(source, body)
    event_id = str(uuid4())

    log.info(
        "webhook.accepted",
        event_id=event_id,
        source=source.value,
        event_count=len(event.events),
    )

    return WebhookAccepted(event_id=event_id)
