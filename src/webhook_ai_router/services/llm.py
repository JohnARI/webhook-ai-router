"""LLM-powered lead classification.

* :class:`LLMClient` is the protocol the worker depends on. Tests provide a
  fake implementation; production wires :class:`AnthropicLLMClient` or
  :class:`GeminiLLMClient` based on ``Settings.llm_provider``.
* :class:`AnthropicLLMClient` uses Anthropic forced tool-use for structured
  output (no free-text JSON parsing) with prompt caching on the system
  prompt + tool definition. Per-call timeout default 10 s.
* :class:`GeminiLLMClient` uses Gemini's ``response_schema`` (the SDK auto-
  converts our Pydantic ``EnrichmentResult`` model). Same per-call timeout.
* Transient errors are retried via tenacity; ``LLMClassificationError`` is
  **not** retried — the model produced something but it failed validation
  or got blocked, retrying won't change that.
* :func:`create_llm_client` is the only place that knows about provider
  selection and the missing-key guard. The worker calls it once at startup
  and stows the result on ``ctx["llm"]``.
"""

from __future__ import annotations

import json
from typing import Any, Final, Protocol, cast

import anthropic
import httpx
import structlog
from anthropic import AsyncAnthropic
from anthropic.types import ToolUseBlock
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from webhook_ai_router.config import LLMProvider, Settings
from webhook_ai_router.schemas.enrichment import EnrichmentResult

log = structlog.get_logger(__name__)

# --- shared prompt + classification tool definition ----------------------

_SYSTEM_PROMPT: Final = (
    "You are a sales-lead classifier for a B2B SaaS company. You receive "
    "raw webhook payloads from CRMs (HubSpot today; Stripe and Calendly "
    "soon). Classify each payload as 'hot', 'warm', or 'cold' based on "
    "the buying signals it contains: explicit demo requests or pricing "
    "queries -> hot; engagement with materials / repeat visits -> warm; "
    "anything else -> cold. Be conservative — when in doubt, choose a "
    "cooler category with lower confidence."
)


def _user_prompt(payload: dict[str, Any]) -> str:
    """Render the per-call user message. Stable across providers so the
    cache prefix (Anthropic) and the prompt text (Gemini) match.
    """
    return (
        "Classify the following webhook payload as a sales lead. "
        "Use only the fields present.\n\n"
        f"<payload>\n{json.dumps(payload, sort_keys=True)}\n</payload>"
    )


_TOOL_NAME: Final = "record_classification"

# Anthropic-only — Gemini uses ``response_schema`` instead of tool-use.
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


# --- Protocol + shared error -------------------------------------------


class LLMClassificationError(Exception):
    """Raised when the LLM response cannot be parsed into an EnrichmentResult.

    Distinct from network/transient errors so tenacity can target retries
    correctly: this is *not* retried.
    """


class LLMClient(Protocol):
    """Protocol the worker depends on; production = AnthropicLLMClient or GeminiLLMClient."""

    async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult: ...

    async def close(self) -> None: ...


# --- Anthropic implementation ------------------------------------------


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
        # The Anthropic SDK's typed overloads expect concrete TypedDicts
        # (``ToolParam``, ``ToolChoiceToolParam``, ``MessageParam``); we pass
        # plain dicts shaped to the same wire format. Replacing the dicts
        # with the SDK's TypedDicts would buy nothing at runtime — both end
        # up as the same JSON — so we keep them readable here and silence
        # the overload check at the call site.
        response = await self._client.messages.create(  # type: ignore[call-overload]
            model=self._model,
            max_tokens=self._max_tokens,
            system=_SYSTEM_PROMPT + " Always classify by calling the "
            f"{_TOOL_NAME} tool exactly once.",
            tools=[_TOOL_DEFINITION],  # pyright: ignore[reportArgumentType]
            tool_choice={
                "type": "tool",
                "name": _TOOL_NAME,
                "disable_parallel_tool_use": True,
            },
            messages=[{"role": "user", "content": _user_prompt(payload)}],
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


# --- Gemini implementation ---------------------------------------------


# `finish_reason` values that mean "the model stopped without giving us a
# usable answer" — retrying won't fix any of these (safety filter, hard
# token cap, recitation block, etc.). Treat all of these as
# `LLMClassificationError` (non-retryable).
_NON_RETRYABLE_FINISH_REASONS: Final = frozenset(
    {
        genai_types.FinishReason.SAFETY,
        genai_types.FinishReason.MAX_TOKENS,
        genai_types.FinishReason.RECITATION,
        genai_types.FinishReason.BLOCKLIST,
        genai_types.FinishReason.PROHIBITED_CONTENT,
        genai_types.FinishReason.SPII,
    }
)


def _is_transient_genai_error(exc: BaseException) -> bool:
    """Tenacity retry predicate for the Gemini SDK.

    Retry on:
      * any 5xx (``ServerError``)
      * 429 specifically (``ClientError`` with ``code == 429``)
      * httpx connect/timeout drops (the SDK uses httpx under the hood
        and surfaces these directly).

    Don't retry on generic 4xx (bad key, bad request, etc.).
    """
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) == 429
    return isinstance(exc, httpx.TimeoutException | httpx.ConnectError)


class GeminiLLMClient:
    """Gemini-backed implementation of :class:`LLMClient`.

    Uses ``response_schema=EnrichmentResult`` for structured output. The
    SDK auto-converts our Pydantic v2 model to its JSON-schema dialect and
    populates ``response.parsed`` with a hydrated instance on success.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        timeout_seconds: float = 10.0,
        max_output_tokens: int = 1024,
    ) -> None:
        # ``HttpOptions.timeout`` is in **milliseconds** and bounds the
        # whole HTTP call; tenacity layers on top for transient retries.
        # TODO: explicit context caching via client.caches.create(...) is
        # cheap to add later if the system prompt grows. The current
        # response_schema is small enough not to warrant it.
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=int(timeout_seconds * 1000)),
        )
        self._model = model
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens

    @retry(
        retry=retry_if_exception(_is_transient_genai_error),
        wait=wait_random_exponential(multiplier=1, max=15),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult:
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=_user_prompt(payload),
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=EnrichmentResult,
                temperature=0.0,
                candidate_count=1,
                max_output_tokens=self._max_output_tokens,
            ),
        )

        # Guard: the model stopped for a reason that won't be fixed by
        # retrying — safety filter, max-tokens, recitation, etc.
        candidates = response.candidates or []
        if candidates:
            finish = candidates[0].finish_reason
            if finish is not None and finish in _NON_RETRYABLE_FINISH_REASONS:
                raise LLMClassificationError(
                    f"Gemini stopped with finish_reason={finish.name!r}; not retrying"
                )

        # Happy path: SDK already hydrated the Pydantic model for us.
        parsed = response.parsed
        if isinstance(parsed, EnrichmentResult):
            return parsed

        # Fallback: SDK left ``parsed`` as None (rare on schema responses
        # but possible if the model added extraneous text). Try to recover
        # from ``response.text``; otherwise raise.
        text = response.text
        if not text:
            raise LLMClassificationError("Gemini returned no parsed object and no text body")
        try:
            return EnrichmentResult.model_validate_json(text)
        except ValidationError as exc:
            raise LLMClassificationError(
                f"Gemini response failed schema validation: {exc.error_count()} error(s)"
            ) from exc

    async def close(self) -> None:
        # ``aio.aclose`` shuts down the SDK's async httpx pool.
        await self._client.aio.aclose()


# --- factory -----------------------------------------------------------


def create_llm_client(settings: Settings) -> LLMClient:
    """Build the configured :class:`LLMClient`.

    This is the **only** place provider selection happens and the only
    place the missing-key guard lives. Worker startup calls it once and
    stows the result on ``ctx["llm"]``.
    """
    provider = settings.llm_provider
    if provider is LLMProvider.ANTHROPIC:
        if settings.anthropic_api_key is None:
            raise RuntimeError(
                "anthropic_api_key required to run the worker with provider=anthropic"
            )
        return cast(
            LLMClient,
            AnthropicLLMClient(
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=settings.anthropic_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ),
        )
    if provider is LLMProvider.GEMINI:
        if settings.gemini_api_key is None:
            raise RuntimeError("gemini_api_key required to run the worker with provider=gemini")
        return cast(
            LLMClient,
            GeminiLLMClient(
                api_key=settings.gemini_api_key.get_secret_value(),
                model=settings.gemini_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ),
        )
    # Exhaustiveness — `LLMProvider` is a closed StrEnum so this is
    # unreachable today. Kept so adding a new provider is a one-line
    # extension upstairs and a one-line raise here.
    raise RuntimeError(f"Unknown LLM provider: {provider!r}")  # pragma: no cover
