"""End-to-end test: route → enqueue → worker task → llm + dispatch.

We do NOT spin up a real arq Worker. Two reasons:

1. arq's Worker requires a real (or fakeredis) Redis to deserialise jobs.
2. The contracts worth testing here are (a) the route enqueues the right job
   with the right args, and (b) ``process_webhook`` orchestrates LLM and
   dispatch correctly. Neither requires arq's loop. A real Worker would also
   verify msgpack-serialisability of args; if that ever breaks, add a smoke
   test against fakeredis in a follow-up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tests.conftest import HUBSPOT_TEST_SECRET, FakeArqPool
from webhook_ai_router.config import Settings as AppSettings
from webhook_ai_router.schemas.dispatch import DispatchTarget
from webhook_ai_router.schemas.enrichment import EnrichmentResult
from webhook_ai_router.services.llm import LLMClient
from webhook_ai_router.workers.tasks import process_webhook


def _sign(body: bytes, ts: int, secret: str = HUBSPOT_TEST_SECRET) -> str:
    msg = str(ts).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def _signed_headers(body: bytes, key: str) -> dict[str, str]:
    ts = int(time.time())
    return {
        "X-Signature": _sign(body, ts),
        "X-Timestamp": str(ts),
        "Content-Type": "application/json",
        "Idempotency-Key": key,
    }


class FakeLLMClient:
    """Records calls; returns a canned :class:`EnrichmentResult`."""

    def __init__(self, result: EnrichmentResult | None = None) -> None:
        self.result = result or EnrichmentResult(
            category="warm", reason="signal of interest", confidence=0.7
        )
        self.calls: list[dict[str, Any]] = []

    async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult:
        self.calls.append(payload)
        return self.result

    async def close(self) -> None:
        return None


# --- route side -----------------------------------------------------------


def test_route_enqueues_canonical_job(client: TestClient, fake_arq_pool: FakeArqPool) -> None:
    body = json.dumps([{"eventId": 1, "objectId": 42}]).encode("utf-8")
    key = str(uuid4())

    resp = client.post("/webhooks/hubspot", content=body, headers=_signed_headers(body, key))

    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert len(fake_arq_pool.enqueued) == 1
    job = fake_arq_pool.enqueued[0]
    assert job.function == "process_webhook"
    assert job.args[0] == resp.json()["event_id"]
    assert job.args[1] == "hubspot"
    # payload arg is a dict of the parsed event, not the raw body
    assert isinstance(job.args[2], dict)
    assert job.args[2]["source"] == "hubspot"
    assert isinstance(job.args[2]["events"], list)
    assert job.args[3] == key
    # job_id is event_id for queue-level dedup
    assert job.job_id == resp.json()["event_id"]


# --- worker side ----------------------------------------------------------


async def test_process_webhook_classifies_and_dispatches(caplog: Any) -> None:
    """Drive ``process_webhook`` with fakes and assert orchestration."""
    fake_llm = FakeLLMClient()

    downstream_received: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        downstream_received.append(json.loads(request.content))
        return httpx.Response(200)

    targets = [
        DispatchTarget(url="https://hook.example.com/a"),  # type: ignore[arg-type]
        DispatchTarget(url="https://hook.example.com/b"),  # type: ignore[arg-type]
    ]

    settings = AppSettings(  # type: ignore[call-arg]
        dispatch_targets=targets,
        dispatch_total_timeout_seconds=10,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = {"llm": fake_llm, "http": http, "settings": settings}
        with capture_logs() as captured:
            result = await process_webhook(
                ctx,
                event_id="evt-1",
                source="hubspot",
                payload={"contactId": 99, "email": "lead@example.com"},
                idempotency_key="key-1",
            )

    # LLM was called exactly once with the payload
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0]["contactId"] == 99

    # Each downstream target received the enriched payload (not the raw one)
    assert len(downstream_received) == 2
    for body in downstream_received:
        assert body["event_id"] == "evt-1"
        assert body["category"] == "warm"
        assert body["confidence"] == 0.7
        assert body["data"] == {"contactId": 99, "email": "lead@example.com"}

    # Task summary
    assert result["event_id"] == "evt-1"
    assert result["category"] == "warm"
    assert result["dispatch_count"] == 2
    assert result["dispatch_succeeded"] == 2

    # Critical: task.processed log line must NOT contain raw payload contents.
    processed = [e for e in captured if e.get("event") == "task.processed"]
    assert len(processed) == 1
    record = processed[0]
    # Allowed correlation fields
    assert record["event_id"] == "evt-1"
    assert record["source"] == "hubspot"
    assert record["category"] == "warm"
    # Forbidden raw-payload leakage
    assert "payload" not in record
    assert "contactId" not in str(record)
    assert "email" not in str(record).lower() or "lead@example.com" not in str(record)


async def test_process_webhook_with_no_targets_still_succeeds() -> None:
    fake_llm = FakeLLMClient()
    settings = AppSettings(dispatch_targets=[])  # type: ignore[call-arg]

    async with httpx.AsyncClient() as http:
        ctx = {"llm": fake_llm, "http": http, "settings": settings}
        result = await process_webhook(
            ctx,
            event_id="evt-2",
            source="hubspot",
            payload={"contactId": 1},
            idempotency_key=None,
        )

    assert result["dispatch_count"] == 0
    assert result["dispatch_succeeded"] == 0


async def test_process_webhook_propagates_classification_error() -> None:
    """LLM classification failures must not be swallowed silently."""
    from webhook_ai_router.services.llm import LLMClassificationError

    class BadLLM:
        async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult:
            raise LLMClassificationError("schema mismatch")

        async def close(self) -> None:
            return None

    settings = AppSettings()  # type: ignore[call-arg]
    bad_llm: LLMClient = BadLLM()  # type: ignore[assignment]

    async with httpx.AsyncClient() as http:
        ctx = {"llm": bad_llm, "http": http, "settings": settings}
        with pytest.raises(LLMClassificationError):
            await process_webhook(
                ctx,
                event_id="evt-3",
                source="hubspot",
                payload={"x": 1},
                idempotency_key="k",
            )
