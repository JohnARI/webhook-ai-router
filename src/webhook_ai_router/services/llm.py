"""LLM-powered lead classification.

* :class:`LLMClient` is the protocol the worker depends on. Tests provide a
  fake implementation; production wires :class:`AnthropicLLMClient`.
* :class:`AnthropicLLMClient` uses Anthropic forced tool-use for structured
  output (no free-text JSON parsing) with prompt caching on the system
  prompt + tool definition. Per-call timeout default 10 s.
* Transient errors (``APIConnectionError``, ``APITimeoutError``,
  ``RateLimitError``, ``InternalServerError``) are retried via tenacity;
  ``ValidationError`` is **not** retried.
"""

from __future__ import annotations

import json
from typing import Any, Final, Protocol

import anthropic
import structlog
from anthropic import AsyncAnthropic
from anthropic.types import ToolUseBlock
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from webhook_ai_router.schemas.enrichment import EnrichmentResult

log = structlog.get_logger(__name__)

_TOOL_NAME: Final = "record_classification"

_TOOL_DEFINITION: Final[dict[str, Any]] = {
    "name": _TOOL_NAME,
    "description": (
        "Record the classification of an inbound webhook payload as a sales "
        "lead. Always call this exactly once with your best assessment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["hot", "warm", "cold"],
                "description": (
                    "Lead temperature: 'hot' = ready to buy / strong intent, "
                    "'warm' = engaged but not ready, 'cold' = low intent or "
                    "unclear."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Short justification (1-3 sentences) for the category.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confidence in the category, between 0 and 1.",
            },
        },
        "required": ["category", "reason", "confidence"],
    },
}

_SYSTEM_PROMPT: Final = (
    "You are a sales-lead classifier for a B2B SaaS company. You receive "
    "raw webhook payloads from CRMs (HubSpot today; Stripe and Calendly "
    "soon). Classify each payload as 'hot', 'warm', or 'cold' based on "
    "the buying signals it contains: explicit demo requests or pricing "
    "queries -> hot; engagement with materials / repeat visits -> warm; "
    "anything else -> cold. Be conservative — when in doubt, choose a "
    "cooler category with lower confidence. Always classify by calling "
    "the record_classification tool exactly once."
)


class LLMClassificationError(Exception):
    """Raised when the LLM response cannot be parsed into an EnrichmentResult.

    Distinct from network/transient errors so tenacity can target retries
    correctly: this is *not* retried.
    """


class LLMClient(Protocol):
    """Protocol the worker depends on; production = AnthropicLLMClient."""

    async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult: ...

    async def close(self) -> None: ...


class AnthropicLLMClient:
    """Anthropic-backed implementation of :class:`LLMClient`."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        timeout_seconds: float = 10.0,
        max_tokens: int = 1024,
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)
        self._model = model
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens

    @retry(
        retry=retry_if_exception_type(
            (
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            )
        ),
        wait=wait_random_exponential(multiplier=1, max=15),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult:
        # Render order is tools -> system -> messages, so caching the last
        # cacheable block on the system covers BOTH tools + system.
        # The SDK's typed overloads are tighter than the dict-shaped tool /
        # tool_choice / messages we pass — silence with one ignore at the call.
        response = await self._client.messages.create(  # type: ignore[call-overload]
            model=self._model,
            max_tokens=self._max_tokens,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_DEFINITION], # pyright: ignore[reportArgumentType]
            tool_choice={
                "type": "tool",
                "name": _TOOL_NAME,
                "disable_parallel_tool_use": True,
            },
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Classify the following webhook payload as a sales lead. "
                        "Use only the fields present.\n\n"
                        f"<payload>\n{json.dumps(payload, sort_keys=True)}\n</payload>"
                    ),
                }
            ],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
            # Top-level cache_control caches tools + system together.
            extra_body={"cache_control": {"type": "ephemeral"}},
        )

        if response.stop_reason != "tool_use":
            raise LLMClassificationError(
                f"Expected stop_reason='tool_use', got {response.stop_reason!r}"
            )

        tool_block = next(
            (b for b in response.content if isinstance(b, ToolUseBlock) and b.name == _TOOL_NAME),
            None,
        )
        if tool_block is None:
            raise LLMClassificationError(f"No '{_TOOL_NAME}' tool_use block in response")

        try:
            return EnrichmentResult.model_validate(tool_block.input)
        except ValidationError as exc:
            # Validation failures are not retryable — the model returned a
            # well-formed response that doesn't match our schema.
            raise LLMClassificationError(
                f"Tool input failed validation: {exc.error_count()} error(s)"
            ) from exc

    async def close(self) -> None:
        await self._client.close()
