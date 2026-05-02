"""Smoke tests for the Prometheus surface.

Asserts:
* ``GET /metrics`` returns 200 and the prometheus exposition content type.
* The four custom metrics are registered (their names appear in the output).
* The route increments ``webhook_received_total{status="accepted"}`` on the
  happy path and ``status="missing_key"`` on the missing-header path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from tests.conftest import HUBSPOT_TEST_SECRET
from webhook_ai_router.core.metrics import (
    DISPATCH_ATTEMPTS_TOTAL,
    DLQ_EVENTS_TOTAL,
    WEBHOOK_PROCESSING_SECONDS,
    WEBHOOK_RECEIVED_TOTAL,
    host_from_url,
)


def _signed_headers(body: bytes, key: str) -> dict[str, str]:
    ts = int(time.time())
    msg = str(ts).encode("ascii") + b"." + body
    sig = hmac.new(HUBSPOT_TEST_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return {
        "X-Signature": sig,
        "X-Timestamp": str(ts),
        "Content-Type": "application/json",
        "Idempotency-Key": key,
    }


def test_metrics_endpoint_returns_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    # prometheus_client exposes the OpenMetrics-friendly text/plain content type.
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # All four custom metrics should be HELP-described in the output.
    assert "webhook_received_total" in body
    assert "webhook_processing_seconds" in body
    assert "dispatch_attempts_total" in body
    assert "dlq_events_total" in body


def test_route_increments_webhook_received_accepted(client: TestClient) -> None:
    before = WEBHOOK_RECEIVED_TOTAL.labels(source="hubspot", status="accepted")._value.get()
    body = json.dumps([{"eventId": 1}]).encode("utf-8")
    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers=_signed_headers(body, str(uuid4())),
    )
    assert resp.status_code == 202
    after = WEBHOOK_RECEIVED_TOTAL.labels(source="hubspot", status="accepted")._value.get()
    assert after - before == 1


def test_missing_key_increments_received_missing_key(client: TestClient) -> None:
    before = WEBHOOK_RECEIVED_TOTAL.labels(source="hubspot", status="missing_key")._value.get()
    body = b"[]"
    ts = int(time.time())
    msg = str(ts).encode("ascii") + b"." + body
    sig = hmac.new(HUBSPOT_TEST_SECRET.encode(), msg, hashlib.sha256).hexdigest()

    resp = client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-Signature": sig,
            "X-Timestamp": str(ts),
            "Content-Type": "application/json",
            # No Idempotency-Key header — should hit the missing_key branch.
        },
    )
    assert resp.status_code == 400
    after = WEBHOOK_RECEIVED_TOTAL.labels(source="hubspot", status="missing_key")._value.get()
    assert after - before == 1


def test_host_from_url_extracts_netloc() -> None:
    assert host_from_url("https://hooks.example.com/path?x=1") == "hooks.example.com"
    assert host_from_url("https://hooks.example.com:8443/path") == "hooks.example.com:8443"
    assert host_from_url("not-a-url") == "unknown"


def test_processing_seconds_and_dlq_metrics_are_registered() -> None:
    """Constructing the labels must not raise — proves the metrics are
    well-formed even before any traffic hits them.
    """
    WEBHOOK_PROCESSING_SECONDS.labels(source="hubspot")
    DLQ_EVENTS_TOTAL.labels(source="hubspot")
    DISPATCH_ATTEMPTS_TOTAL.labels(target="x.example.com", outcome="success")
