"""Pydantic v2 models for incoming webhook payloads.

The router supports a single source today (HubSpot) but the schema is shaped
as a discriminated union on ``source`` so adding ``stripe`` and ``calendly``
later is purely additive: define a new ``BaseModel`` with
``source: Literal[WebhookSource.STRIPE]`` and add it to the ``WebhookEvent``
alias below.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class WebhookSource(StrEnum):
    """Supported webhook sources."""

    HUBSPOT = "hubspot"


class HubspotEventPayload(BaseModel):
    """A single event inside a HubSpot webhook batch.

    HubSpot delivers a JSON array of these per request. The fields below cover
    the common subscription-event shape; ``extra="allow"`` keeps any unknown
    fields so the downstream consumer never silently loses upstream data.
    """

    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)

    event_id: int | None = Field(default=None, alias="eventId")
    subscription_id: int | None = Field(default=None, alias="subscriptionId")
    subscription_type: str | None = Field(default=None, alias="subscriptionType")
    portal_id: int | None = Field(default=None, alias="portalId")
    app_id: int | None = Field(default=None, alias="appId")
    occurred_at: int | None = Field(default=None, alias="occurredAt")
    object_id: int | None = Field(default=None, alias="objectId")
    change_source: str | None = Field(default=None, alias="changeSource")
    change_flag: str | None = Field(default=None, alias="changeFlag")


class HubspotWebhookEvent(BaseModel):
    """A HubSpot webhook delivery (a batch of events)."""

    model_config = ConfigDict(frozen=True)

    source: Literal[WebhookSource.HUBSPOT] = WebhookSource.HUBSPOT
    events: list[HubspotEventPayload]


# Discriminated union over all supported sources. With one source today this
# is a degenerate union; when a second source is added the line becomes:
#   WebhookEvent = Annotated[
#       HubspotWebhookEvent | StripeWebhookEvent,
#       Field(discriminator="source"),
#   ]
WebhookEvent = Annotated[HubspotWebhookEvent, Field(discriminator="source")]
