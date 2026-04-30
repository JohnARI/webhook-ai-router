"""Tests for the Idempotency-Key handling on ``POST /webhooks/{source}``.

Covers:

* same key replayed → second call returns the *exact* cached response and
  does not generate a new ``event_id`` (no double processing).
* missing ``Idempotency-Key`` header → 400.
* concurrent in-flight request with the same key → second gets 409.
* the underlying :class:`IdempotencyStore` round-trips correctly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import HUBSPOT_TEST_SECRET, FakeAsyncRedis
from webhook_ai_router.core.idempotency import CachedResponse, IdempotencyStore


def _sign(body: bytes, ts: int, secret: str = HUBSPOT_TEST_SECRET) -> str:
    msg = str(ts).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _signed_headers(body: bytes, idempotency_key: str | None) -> dict[str, str]:
    ts = int(time.time())
    headers = {
        "X-Signature": _sign(body, ts),
        "X-Timestamp": str(ts),
        "Content-Type": "application/json",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _hubspot_body() -> bytes:
    return json.dumps([{"eventId": 1, "objectId": 42}]).encode("utf-8")


# --- endpoint behaviour --------------------------------------------------


def test_missing_idempotency_key_returns_400(client: TestClient) -> None:
    body = _hubspot_body()

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_signed_headers(body, idempotency_key=None),
    )

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"] == "Idempotency-Key header is required"


def test_same_key_replay_returns_cached_response_without_reprocessing(
    client: TestClient,
) -> None:
    body = _hubspot_body()
    key = str(uuid4())

    first = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_signed_headers(body, idempotency_key=key),
    )
    assert first.status_code == 202
    first_event_id = first.json()["event_id"]

    second = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_signed_headers(body, idempotency_key=key),
    )

    assert second.status_code == 202
    # Same event_id proves the second request returned the *cached* body
    # rather than executing the route logic and minting a new uuid.
    assert second.json()["event_id"] == first_event_id


def test_concurrent_requests_with_same_key_return_409_for_loser(
    client: TestClient, fake_redis: FakeAsyncRedis
) -> None:
    """Simulate concurrency by pre-acquiring the lock, then making a request.

    The route should see the lock held and return 409 without processing.
    """
    import asyncio

    body = _hubspot_body()
    key = str(uuid4())

    store = IdempotencyStore(fake_redis)  # type: ignore[arg-type]
    assert asyncio.run(store.lock(key)) is True

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_signed_headers(body, idempotency_key=key),
    )

    assert resp.status_code == 409
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"].startswith("Concurrent request")


# --- store-level round-trip ---------------------------------------------


@pytest.fixture
def store(fake_redis: FakeAsyncRedis) -> IdempotencyStore:
    return IdempotencyStore(fake_redis)  # type: ignore[arg-type]


async def test_store_get_returns_none_for_missing_key(store: IdempotencyStore) -> None:
    assert await store.get("nope") is None


async def test_store_set_then_get_round_trips(store: IdempotencyStore) -> None:
    response = CachedResponse(
        status_code=202,
        headers={"content-type": "application/json"},
        body=b'{"event_id":"abc","status":"accepted"}',
    )
    await store.set("key-1", response)

    fetched = await store.get("key-1")

    assert fetched is not None
    assert fetched.status_code == 202
    assert fetched.headers == {"content-type": "application/json"}
    assert fetched.body == b'{"event_id":"abc","status":"accepted"}'


async def test_store_lock_is_exclusive(store: IdempotencyStore) -> None:
    assert await store.lock("k") is True
    assert await store.lock("k") is False  # second caller is locked out


async def test_store_unlock_releases_lock(store: IdempotencyStore) -> None:
    assert await store.lock("k") is True
    await store.unlock("k")
    assert await store.lock("k") is True
