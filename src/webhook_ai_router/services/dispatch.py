"""Concurrent fan-out to configured downstream URLs with retries.

Per-target shape:

* Tenacity retries on :class:`httpx.TransportError` and :class:`TransientHTTPError`
  (raised internally on 5xx).
* ``wait_random_exponential(multiplier=1, max=30)`` + ``stop_after_delay(120)``
  bounds total wall-clock per target.
* 4xx returns the response (no retry); we map it to ``DispatchResult(success=False)``.
* ``RetryError`` (budget exhausted) → ``DispatchResult(success=False,
  error="timeout_exceeded")``.

All targets fan out concurrently via ``asyncio.gather(..., return_exceptions=True)``
so one bad target can't cancel the others.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_delay,
    wait_random_exponential,
)

from webhook_ai_router.core.metrics import DISPATCH_ATTEMPTS_TOTAL, host_from_url
from webhook_ai_router.schemas.dispatch import DispatchResult, DispatchTarget

log = structlog.get_logger(__name__)


class TransientHTTPError(Exception):
    """Raised internally on 5xx so tenacity retries the call."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"transient HTTP {status_code}")
        self.status_code = status_code


async def dispatch(
    client: httpx.AsyncClient,
    targets: list[DispatchTarget],
    payload: dict[str, Any],
    *,
    total_timeout_seconds: float = 120.0,
) -> list[DispatchResult]:
    """POST ``payload`` to every ``DispatchTarget`` concurrently.

    Returns one :class:`DispatchResult` per target, in the same order. Never
    raises — every failure is captured in a result.
    """
    if not targets:
        return []

    coros = [
        _send_one_with_retry(client, target, payload, total_timeout_seconds) for target in targets
    ]
    raw = await asyncio.gather(*coros, return_exceptions=True)

    results: list[DispatchResult] = []
    for target, item in zip(targets, raw, strict=True):
        if isinstance(item, DispatchResult):
            results.append(item)
            continue
        # Defensive: unhandled exception bubbled out of the per-target coro.
        exc = item if isinstance(item, BaseException) else RuntimeError(str(item))
        log.warning(
            "dispatch.unexpected_error",
            url=str(target.url),
            error=type(exc).__name__,
        )
        results.append(
            DispatchResult(
                url=str(target.url),
                success=False,
                attempts=1,
                error=f"unexpected:{type(exc).__name__}",
            )
        )
    return results


async def _send_one_with_retry(
    client: httpx.AsyncClient,
    target: DispatchTarget,
    payload: dict[str, Any],
    total_timeout_seconds: float,
) -> DispatchResult:
    """Send a single request with tenacity retries; convert outcomes to a
    :class:`DispatchResult`. Never raises.
    """
    url = str(target.url)
    host = host_from_url(url)
    attempts = 0
    last_status: int | None = None

    try:
        # reraise=False so budget exhaustion surfaces as RetryError (we
        # convert it below); reraise=True would resurface the underlying
        # TransientHTTPError and we'd have to switch on type instead.
        async for attempt in AsyncRetrying(
            wait=wait_random_exponential(multiplier=1, max=30),
            stop=stop_after_delay(total_timeout_seconds),
            retry=retry_if_exception_type((httpx.TransportError, TransientHTTPError)),
            reraise=False,
        ):
            with attempt:
                attempts += 1
                try:
                    response = await client.request(
                        target.method,
                        url,
                        json=payload,
                        headers=target.headers or None,
                    )
                except httpx.TransportError:
                    DISPATCH_ATTEMPTS_TOTAL.labels(target=host, outcome="transport_error").inc()
                    raise
                last_status = response.status_code
                if 500 <= response.status_code < 600:
                    DISPATCH_ATTEMPTS_TOTAL.labels(target=host, outcome="5xx").inc()
                    raise TransientHTTPError(response.status_code)
                outcome = "success" if 200 <= response.status_code < 300 else "4xx"
                DISPATCH_ATTEMPTS_TOTAL.labels(target=host, outcome=outcome).inc()
                return DispatchResult(
                    url=url,
                    success=outcome == "success",
                    status_code=response.status_code,
                    attempts=attempts,
                )
    except RetryError:
        log.warning(
            "dispatch.budget_exhausted",
            url=url,
            attempts=attempts,
            last_status=last_status,
        )
        DISPATCH_ATTEMPTS_TOTAL.labels(target=host, outcome="timeout_exceeded").inc()
        return DispatchResult(
            url=url,
            success=False,
            status_code=last_status,
            attempts=attempts,
            error="timeout_exceeded",
        )
    except httpx.TransportError as exc:
        # Reraise cases that escape the retry loop directly (shouldn't
        # happen with retry_if covering, but defensive).
        return DispatchResult(
            url=url,
            success=False,
            attempts=attempts,
            error=f"transport:{type(exc).__name__}",
        )

    # Unreachable — AsyncRetrying always either returns or raises.
    raise RuntimeError("unreachable")  # pragma: no cover
