"""Webhook ingestion service.

Pure parsing/validation logic lives here so the FastAPI route stays a thin
orchestrator (handler reads request, calls service, returns response).
"""

from __future__ import annotations

import json
from typing import Any, assert_never

from pydantic import ValidationError

from webhook_ai_router.core.exceptions import PayloadInvalidError
from webhook_ai_router.schemas.webhooks import (
    HubspotWebhookEvent,
    WebhookEvent,
    WebhookSource,
)


def parsed_to_dict(event: WebhookEvent) -> dict[str, Any]:
    """Return a JSON-serialisable dict view of a parsed webhook event.

    Used by the route to hand a clean payload to the arq worker without
    re-parsing the raw body on the worker side.
    """
    return event.model_dump(mode="json")


def parse_webhook_event(source: WebhookSource, body: bytes) -> WebhookEvent:
    """Parse a raw webhook ``body`` into the source-specific event model.

    Raises :class:`PayloadInvalidError` if the body is not valid JSON or
    fails Pydantic validation.
    """
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise PayloadInvalidError(f"Body is not valid JSON: {exc.msg}") from exc

    match source:
        case WebhookSource.HUBSPOT:
            if not isinstance(raw, list):
                raise PayloadInvalidError(
                    "HubSpot webhooks must be a JSON array of events",
                )
            try:
                return HubspotWebhookEvent.model_validate(
                    {"source": source.value, "events": raw},
                )
            except ValidationError as exc:
                raise PayloadInvalidError(
                    f"Invalid {source.value} webhook payload: {exc.error_count()} error(s)",
                ) from exc
        case _:  # pragma: no cover - exhaustiveness guard
            assert_never(source)
