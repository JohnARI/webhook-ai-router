"""End-to-end tests for ``POST /webhooks/{source}`` via FastAPI TestClient."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from webhook_ai_router.core.settings import Settings, get_settings
from webhook_ai_router.main import create_app

SECRET = "test-hubspot-secret"


def _settings_override() -> Settings:
    return Settings(hubspot_webhook_secret=SECRET)  # type: ignore[arg-type]


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_settings] = _settings_override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _sign(body: bytes, ts: int, secret: str = SECRET) -> str:
    msg = str(ts).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def test_signed_hubspot_request_returns_202(client: TestClient) -> None:
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
    ts = int(time.time())
    sig = _sign(body, ts)

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 202
    payload = resp.json()
    assert payload["status"] == "accepted"
    assert payload["event_id"]
    assert resp.headers.get("X-Request-ID")


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
        },
    )

    assert resp.status_code == 401
    assert resp.json()["title"] == "Webhook timestamp expired"


def test_invalid_payload_returns_422(client: TestClient) -> None:
    body = b"not-valid-json"
    ts = int(time.time())
    sig = _sign(body, ts)

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 422
    assert resp.json()["title"] == "Invalid webhook payload"


def test_unknown_source_returns_422(client: TestClient) -> None:
    body = b"[]"
    ts = int(time.time())
    sig = _sign(body, ts)

    resp = client.post(
        "/webhooks/stripe",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
        },
    )

    assert resp.status_code == 422


def test_propagates_supplied_request_id(client: TestClient) -> None:
    body = b"[]"
    ts = int(time.time())
    sig = _sign(body, ts)
    rid = "abc-123"

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
            "X-Request-ID": rid,
        },
    )

    assert resp.headers.get("X-Request-ID") == rid
