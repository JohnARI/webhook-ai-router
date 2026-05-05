"""Tests for ``services.llm.create_llm_client``.

Covers the four cases that matter:
* ``LLM_PROVIDER=anthropic`` with the key set → an Anthropic-backed client.
* ``LLM_PROVIDER=gemini`` with the key set → a Gemini-backed client.
* The selected provider's key is missing → ``RuntimeError`` with a clear
  message naming the missing field and the chosen provider.
* When *both* keys are present, only the configured provider is built —
  no accidental key reuse.
"""

from __future__ import annotations

import pytest

from webhook_ai_router.config import LLMProvider, Settings
from webhook_ai_router.services.llm import (
    AnthropicLLMClient,
    GeminiLLMClient,
    create_llm_client,
)

# Env vars pydantic-settings would otherwise pick up (from os.environ or
# .env) and accidentally satisfy the missing-key tests.
_LLM_ENV_VARS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER")


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip LLM-related env vars and disable .env loading so each test
    builds Settings purely from its kwargs.
    """
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _settings(**overrides: object) -> Settings:
    """Build a Settings without reading .env (the file holds a stub
    ``ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxx`` for the docker-compose flow,
    which would otherwise poison the missing-key tests).
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_anthropic_provider_with_key_returns_anthropic_client() -> None:
    settings = _settings(
        llm_provider=LLMProvider.ANTHROPIC,
        anthropic_api_key="sk-ant-test",
    )

    client = create_llm_client(settings)

    assert isinstance(client, AnthropicLLMClient)


def test_gemini_provider_with_key_returns_gemini_client() -> None:
    settings = _settings(
        llm_provider=LLMProvider.GEMINI,
        gemini_api_key="gemini-test-key",
    )

    client = create_llm_client(settings)

    assert isinstance(client, GeminiLLMClient)


def test_anthropic_provider_missing_key_raises_with_clear_message() -> None:
    settings = _settings(llm_provider=LLMProvider.ANTHROPIC)

    with pytest.raises(RuntimeError) as exc_info:
        create_llm_client(settings)

    msg = str(exc_info.value)
    assert "anthropic_api_key" in msg
    assert "anthropic" in msg


def test_gemini_provider_missing_key_raises_with_clear_message() -> None:
    settings = _settings(llm_provider=LLMProvider.GEMINI)

    with pytest.raises(RuntimeError) as exc_info:
        create_llm_client(settings)

    msg = str(exc_info.value)
    assert "gemini_api_key" in msg
    assert "gemini" in msg


def test_both_keys_present_anthropic_wins_when_configured() -> None:
    settings = _settings(
        llm_provider=LLMProvider.ANTHROPIC,
        anthropic_api_key="sk-ant-test",
        gemini_api_key="gemini-test-key",
    )

    client = create_llm_client(settings)

    assert isinstance(client, AnthropicLLMClient)
    assert not isinstance(client, GeminiLLMClient)


def test_both_keys_present_gemini_wins_when_configured() -> None:
    settings = _settings(
        llm_provider=LLMProvider.GEMINI,
        anthropic_api_key="sk-ant-test",
        gemini_api_key="gemini-test-key",
    )

    client = create_llm_client(settings)

    assert isinstance(client, GeminiLLMClient)
    assert not isinstance(client, AnthropicLLMClient)


def test_default_provider_is_anthropic() -> None:
    """If LLM_PROVIDER isn't overridden, anthropic is the default."""
    settings = _settings(anthropic_api_key="sk-ant-test")

    client = create_llm_client(settings)

    assert isinstance(client, AnthropicLLMClient)
