"""RFC 7807 problem detail response schema."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ProblemDetail(BaseModel):
    """RFC 7807 ``application/problem+json`` payload."""

    model_config = ConfigDict(frozen=True)

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
