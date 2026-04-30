"""Pydantic schemas for LLM enrichment output."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnrichmentResult(BaseModel):
    """Structured classification of a webhook payload.

    Returned by :meth:`webhook_ai_router.services.llm.LLMClient.classify_lead`
    and consumed by the dispatch step. ``frozen=True`` so it can be cached
    and shared safely across coroutines.
    """

    model_config = ConfigDict(frozen=True)

    category: Literal["hot", "warm", "cold"]
    reason: str = Field(..., min_length=1, max_length=2000)
    confidence: float = Field(..., ge=0.0, le=1.0)
