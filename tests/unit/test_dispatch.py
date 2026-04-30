"""Tests for ``services.dispatch`` — retry behaviour against ``MockTransport``.

We use ``httpx.MockTransport`` instead of monkeypatching so the real httpx
client (timeouts, headers, methods, retries) sits in the path.
"""

from __future__ import annotations

import httpx
import pytest

from webhook_ai_router.schemas.dispatch import DispatchTarget
from webhook_ai_router.services.dispatch import dispatch


def _target(url: str = "https://example.com/hook") -> DispatchTarget:
    return DispatchTarget(url=url)  # type: ignore[arg-type]


# --- happy paths ----------------------------------------------------------


async def test_2xx_returns_success_with_one_attempt() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        results = await dispatch(c, [_target()], {"hello": "world"})

    assert len(results) == 1
    r = results[0]
    assert r.success is True
    assert r.status_code == 200
    assert r.attempts == 1
    assert r.error is None
    assert len(calls) == 1


async def test_5xx_then_2xx_retries_to_success() -> None:
    """Transient 5xx triggers retry; eventual 2xx succeeds."""
    statuses = [502, 503, 200]
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = statuses[len(attempts)]
        attempts.append(status)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # Tight wall-clock so the test doesn't sleep too long; tenacity's
        # exponential backoff starts low, so 3 attempts fit comfortably.
        results = await dispatch(c, [_target()], {"x": 1}, total_timeout_seconds=10.0)

    assert results[0].success is True
    assert results[0].status_code == 200
    assert results[0].attempts == 3
    assert attempts == [502, 503, 200]


async def test_transport_error_then_success_retries() -> None:
    """``httpx.TransportError`` is treated as transient and retried."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("simulated", request=request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        results = await dispatch(c, [_target()], {"x": 1}, total_timeout_seconds=10.0)

    assert results[0].success is True
    assert results[0].attempts == 2
    assert call_count == 2


# --- failure modes --------------------------------------------------------


async def test_4xx_is_not_retried_and_returns_failure() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        results = await dispatch(c, [_target()], {"x": 1})

    assert call_count == 1, "4xx must not retry"
    r = results[0]
    assert r.success is False
    assert r.status_code == 404
    assert r.attempts == 1
    assert r.error is None


async def test_persistent_5xx_exhausts_budget() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        # Very tight budget so the test exits fast. tenacity's first sleep
        # is at most ~2s on `multiplier=1`, so 1.5s budget triggers
        # exhaustion after ~1-2 attempts.
        results = await dispatch(c, [_target()], {"x": 1}, total_timeout_seconds=1.5)

    r = results[0]
    assert r.success is False
    assert r.error == "timeout_exceeded"
    assert r.status_code == 503  # last seen
    assert call_count >= 1


# --- fan-out --------------------------------------------------------------


async def test_concurrent_fan_out_returns_one_result_per_target() -> None:
    """Three targets hit different paths; one bad one doesn't cancel the others."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(200)
        if request.url.path == "/bad":
            return httpx.Response(404)
        return httpx.Response(500)

    targets = [
        DispatchTarget(url="https://a.example.com/ok"),  # type: ignore[arg-type]
        DispatchTarget(url="https://b.example.com/bad"),  # type: ignore[arg-type]
        DispatchTarget(url="https://c.example.com/ok"),  # type: ignore[arg-type]
    ]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        results = await dispatch(c, targets, {"x": 1})

    assert len(results) == 3
    assert [r.success for r in results] == [True, False, True]
    # Order is preserved
    assert results[0].url.startswith("https://a.")
    assert results[1].url.startswith("https://b.")
    assert results[2].url.startswith("https://c.")


async def test_empty_targets_returns_empty_list() -> None:
    async with httpx.AsyncClient() as c:
        results = await dispatch(c, [], {"x": 1})
    assert results == []


# --- request shape --------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH"])
async def test_method_is_passed_through(method: str) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content.decode()
        return httpx.Response(200)

    target = DispatchTarget(url="https://example.com/", method=method)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await dispatch(c, [target], {"k": "v"})

    assert seen["method"] == method
    assert '"k"' in seen["body"]


async def test_custom_headers_are_forwarded() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["x-auth"] = request.headers.get("x-auth", "")
        return httpx.Response(200)

    target = DispatchTarget(
        url="https://example.com/",  # type: ignore[arg-type]
        headers={"X-Auth": "secret-123"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await dispatch(c, [target], {"x": 1})

    assert seen["x-auth"] == "secret-123"
