"""Tests for ``services.llm.GeminiLLMClient``.

We monkeypatch ``client.aio.models.generate_content`` so the SDK's
async surface is exercised but no network is involved. The four cases:

* Happy path: SDK populates ``response.parsed`` with a hydrated
  :class:`EnrichmentResult`. Returned as-is.
* Fallback path: ``parsed is None`` but ``response.text`` is valid JSON
  matching the schema → manually validated and returned.
* Non-retryable finish: ``finish_reason = SAFETY`` →
  :class:`LLMClassificationError`. Tenacity must not retry.
* Transient retry: first call raises ``ServerError`` (5xx), second call
  succeeds. Tenacity retries; final result is the second call's value.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from webhook_ai_router.schemas.enrichment import EnrichmentResult
from webhook_ai_router.services.llm import (
    GeminiLLMClient,
    LLMClassificationError,
    _is_transient_genai_error,
)

# --- response builders --------------------------------------------------


def _make_response(
    *,
    parsed: EnrichmentResult | None = None,
    text: str | None = None,
    finish_reason: genai_types.FinishReason | None = genai_types.FinishReason.STOP,
) -> Any:
    """Build a lightweight stand-in for a Gemini ``GenerateContentResponse``.

    The real type is a Pydantic model with ~30 fields; we only need the
    handful our client actually reads.
    """
    candidate = MagicMock()
    candidate.finish_reason = finish_reason

    response = MagicMock()
    response.parsed = parsed
    response.text = text
    response.candidates = [candidate]
    return response


def _client_with_response(monkeypatch: pytest.MonkeyPatch, response: Any) -> GeminiLLMClient:
    """Build a real GeminiLLMClient; patch its async generate_content."""
    client = GeminiLLMClient(api_key="dummy", model="gemini-2.5-flash", timeout_seconds=5.0)
    monkeypatch.setattr(
        client._client.aio.models, "generate_content", AsyncMock(return_value=response)
    )
    return client


# --- happy path ---------------------------------------------------------


async def test_classify_lead_returns_parsed_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = EnrichmentResult(category="hot", reason="explicit demo request", confidence=0.9)
    client = _client_with_response(monkeypatch, _make_response(parsed=expected))

    result = await client.classify_lead({"contactId": 1})

    assert result == expected


# --- fallback to response.text -----------------------------------------


async def test_classify_lead_falls_back_to_text_when_parsed_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({"category": "warm", "reason": "engaged", "confidence": 0.5})
    client = _client_with_response(monkeypatch, _make_response(parsed=None, text=body))

    result = await client.classify_lead({"x": 1})

    assert result.category == "warm"
    assert result.confidence == 0.5


async def test_classify_lead_raises_when_parsed_none_and_text_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_with_response(monkeypatch, _make_response(parsed=None, text=None))

    with pytest.raises(LLMClassificationError, match="no parsed object and no text"):
        await client.classify_lead({"x": 1})


async def test_classify_lead_raises_when_text_fails_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Invalid: confidence > 1 violates the Pydantic bound.
    body = json.dumps({"category": "hot", "reason": "x", "confidence": 99})
    client = _client_with_response(monkeypatch, _make_response(parsed=None, text=body))

    with pytest.raises(LLMClassificationError, match="failed schema validation"):
        await client.classify_lead({"x": 1})


# --- non-retryable finish reasons --------------------------------------


@pytest.mark.parametrize(
    "finish_reason",
    [
        genai_types.FinishReason.SAFETY,
        genai_types.FinishReason.MAX_TOKENS,
        genai_types.FinishReason.RECITATION,
        genai_types.FinishReason.BLOCKLIST,
    ],
)
async def test_non_retryable_finish_reason_raises_classification_error(
    monkeypatch: pytest.MonkeyPatch, finish_reason: genai_types.FinishReason
) -> None:
    client = _client_with_response(
        monkeypatch, _make_response(parsed=None, text=None, finish_reason=finish_reason)
    )
    # Spy on the call count so we can assert no retries happened.
    call_count = 0

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return _make_response(parsed=None, text=None, finish_reason=finish_reason)

    monkeypatch.setattr(client._client.aio.models, "generate_content", _spy)

    with pytest.raises(LLMClassificationError):
        await client.classify_lead({"x": 1})

    assert call_count == 1, "non-retryable finish reasons must not trigger retries"


# --- transient retry ---------------------------------------------------


async def test_transient_server_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = EnrichmentResult(category="cold", reason="no signal", confidence=0.2)
    call_count = 0

    async def _flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Construct a real ServerError; the SDK signature is
            # ``ServerError(code, response_json, response=None)``.
            raise genai_errors.ServerError(503, {"error": {"message": "transient"}})
        return _make_response(parsed=expected)

    client = GeminiLLMClient(api_key="dummy", timeout_seconds=5.0)
    monkeypatch.setattr(client._client.aio.models, "generate_content", _flaky)

    result = await client.classify_lead({"x": 1})

    assert result == expected
    assert call_count == 2, "tenacity should retry exactly once on transient ServerError"


async def test_non_transient_client_error_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400 (e.g. bad API key, malformed request) is permanent."""
    call_count = 0

    async def _always_400(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        raise genai_errors.ClientError(400, {"error": {"message": "bad request"}})

    client = GeminiLLMClient(api_key="dummy", timeout_seconds=5.0)
    monkeypatch.setattr(client._client.aio.models, "generate_content", _always_400)

    with pytest.raises(genai_errors.ClientError):
        await client.classify_lead({"x": 1})

    assert call_count == 1, "non-429 4xx must not retry"


# --- predicate ---------------------------------------------------------


def test_is_transient_predicate_classifies_known_errors_correctly() -> None:
    server = genai_errors.ServerError(500, {"error": {"message": "boom"}})
    assert _is_transient_genai_error(server) is True

    rate_limit = genai_errors.ClientError(429, {"error": {"message": "slow down"}})
    assert _is_transient_genai_error(rate_limit) is True

    bad_request = genai_errors.ClientError(400, {"error": {"message": "bad request"}})
    assert _is_transient_genai_error(bad_request) is False

    timeout = httpx.TimeoutException("read timed out")
    assert _is_transient_genai_error(timeout) is True

    connect = httpx.ConnectError("dns failed")
    assert _is_transient_genai_error(connect) is True

    unrelated = ValueError("anything else")
    assert _is_transient_genai_error(unrelated) is False
