"""Pydantic schemas for downstream dispatch."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class DispatchTarget(BaseModel):
    """A configured downstream URL we forward enriched events to.

    The full ``DISPATCH_TARGETS`` env var is a JSON array of these.
    """

    model_config = ConfigDict(frozen=True)

    url: HttpUrl
    method: Literal["POST", "PUT", "PATCH"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)


class DispatchResult(BaseModel):
    """Per-target outcome reported back from
    :func:`webhook_ai_router.services.dispatch.dispatch`.
    """

    model_config = ConfigDict(frozen=True)

    url: str
    success: bool
    status_code: int | None = None
    attempts: int = 1
    error: str | None = None
