"""End-to-end tests for ``POST /webhooks/{source}`` via FastAPI TestClient."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.conftest import HUBSPOT_TEST_SECRET, FakeArqPool


def _sign(body: bytes, ts: int, secret: str = HUBSPOT_TEST_SECRET) -> str:
    msg = str(ts).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, idempotency_key: str | None = None) -> dict[str, str]:
    ts = int(time.time())
    headers = {
        "X-Signature": _sign(body, ts),
        "X-Timestamp": str(ts),
        "Content-Type": "application/json",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_signed_hubspot_request_returns_202_queued(
    client: TestClient, fake_arq_pool: FakeArqPool
) -> None:
    body = json.dumps(
        [
            {
                "eventId": 1,
                "subscriptionType": "contact.creation",
                "portalId": 12345,
                "objectId": 999,
                "occurredAt": 1_700_000_000_000,
            }
        ],
    ).encode("utf-8")

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_headers(body, idempotency_key=str(uuid4())),
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["status"] == "queued"
    assert payload["event_id"]
    assert resp.headers.get("X-Request-ID")
    # The route should have enqueued exactly one job, named correctly,
    # using event_id as the queue-level dedup key.
    assert len(fake_arq_pool.enqueued) == 1
    job = fake_arq_pool.enqueued[0]
    assert job.function == "process_webhook"
    assert job.job_id == payload["event_id"]
    assert job.args[0] == payload["event_id"]
    assert job.args[1] == "hubspot"


def test_replay_with_same_idempotency_key_does_not_re_enqueue(
    client: TestClient, fake_arq_pool: FakeArqPool
) -> None:
    body = json.dumps([{"eventId": 1, "objectId": 42}]).encode("utf-8")
    key = str(uuid4())

    first = client.post(
        "/webhooks/hubspot", content=body, headers=_headers(body, idempotency_key=key)
    )
    second = client.post(
        "/webhooks/hubspot", content=body, headers=_headers(body, idempotency_key=key)
    )

    assert first.status_code == second.status_code == 202
    # Same event_id (replay returns cached body) and exactly one enqueue.
    assert first.json()["event_id"] == second.json()["event_id"]
    assert len(fake_arq_pool.enqueued) == 1


def test_invalid_signature_returns_401_problem_json(client: TestClient) -> None:
    body = b"[]"
    ts = int(time.time())

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-Signature": "0" * 64,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid4()),
        },
    )

    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")
    body_json = resp.json()
    assert body_json["title"] == "Invalid webhook signature"
    assert body_json["status"] == 401


def test_expired_timestamp_returns_401(client: TestClient) -> None:
    body = b"[]"
    ts = int(time.time()) - 3600
    sig = _sign(body, ts)

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid4()),
        },
    )

    assert resp.status_code == 401
    assert resp.json()["title"] == "Webhook timestamp expired"


def test_invalid_payload_returns_422(client: TestClient) -> None:
    body = b"not-valid-json"

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_headers(body, idempotency_key=str(uuid4())),
    )

    assert resp.status_code == 422
    assert resp.json()["title"] == "Invalid webhook payload"


def test_unknown_source_returns_422(client: TestClient) -> None:
    body = b"[]"

    resp = client.post(
        "/webhooks/stripe",
        content=body,
        headers=_headers(body, idempotency_key=str(uuid4())),
    )

    assert resp.status_code == 422


def test_propagates_supplied_request_id(client: TestClient) -> None:
    body = b"[]"
    headers = _headers(body, idempotency_key=str(uuid4()))
    headers["X-Request-ID"] = "abc-123"

    resp = client.post("/webhooks/hubspot", content=body, headers=headers)

    assert resp.headers.get("X-Request-ID") == "abc-123"
