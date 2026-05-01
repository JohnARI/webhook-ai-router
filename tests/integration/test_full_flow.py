"""End-to-end test: route → enqueue → worker task → llm + dispatch + persistence.

We do NOT spin up a real arq Worker. Two reasons:

1. arq's Worker requires a real (or fakeredis) Redis to deserialise jobs.
2. The contracts worth testing here are (a) the route enqueues the right job
   with the right args, and (b) ``process_webhook`` orchestrates LLM,
   dispatch, persistence, and the DLQ correctly. Neither requires arq's loop.
   A real Worker would also verify msgpack-serialisability of args; if that
   ever breaks, add a smoke test against fakeredis in a follow-up.

For the persistence side we use :class:`FakeEventRepository` so this file
stays self-contained — see ``tests/integration/test_persistence.py`` for
a full trip against a real Postgres.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from tests.conftest import (
    HUBSPOT_TEST_SECRET,
    FakeArqPool,
    FakeEventRepository,
)
from webhook_ai_router.config import Settings as AppSettings
from webhook_ai_router.schemas.dispatch import DispatchTarget
from webhook_ai_router.schemas.enrichment import EnrichmentResult
from webhook_ai_router.services.llm import LLMClient
from webhook_ai_router.workers.tasks import MAX_TRIES, process_webhook


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


def _events_factory(repo: FakeEventRepository) -> Any:
    @asynccontextmanager
    async def _factory() -> AsyncIterator[FakeEventRepository]:
        yield repo

    return _factory


def _ctx(
    *,
    llm: Any,
    http: httpx.AsyncClient,
    settings: AppSettings,
    repo: FakeEventRepository,
    job_try: int = 1,
) -> dict[str, Any]:
    return {
        "llm": llm,
        "http": http,
        "settings": settings,
        "events_factory": _events_factory(repo),
        "job_try": job_try,
    }


# --- route side -----------------------------------------------------------


def test_route_enqueues_canonical_job(
    client: TestClient,
    fake_arq_pool: FakeArqPool,
    fake_event_repo: FakeEventRepository,
) -> None:
    body = json.dumps([{"eventId": 1, "objectId": 42}]).encode("utf-8")
    key = str(uuid4())

    resp = client.post("/webhooks/hubspot", content=body, headers=_signed_headers(body, key))

    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert len(fake_arq_pool.enqueued) == 1
    job = fake_arq_pool.enqueued[0]
    assert job.function == "process_webhook"
    event_id = resp.json()["event_id"]
    assert job.args[0] == event_id
    assert job.args[1] == "hubspot"
    # payload arg is a dict of the parsed event, not the raw body
    assert isinstance(job.args[2], dict)
    assert job.args[2]["source"] == "hubspot"
    assert isinstance(job.args[2]["events"], list)
    assert job.args[3] == key
    # job_id is event_id for queue-level dedup
    assert job.job_id == event_id

    # And the row was persisted in `received` status before enqueue.
    assert len(fake_event_repo.events) == 1
    stored = next(iter(fake_event_repo.events.values()))
    assert str(stored.event_id) == event_id
    assert stored.status == "received"
    assert stored.idempotency_key == key


# --- worker side ----------------------------------------------------------


async def test_process_webhook_classifies_dispatches_and_persists() -> None:
    fake_llm = FakeLLMClient()
    repo = FakeEventRepository()

    # Pre-seed the event in `received` status, mirroring what the route does.
    event_id = await repo.create_received(
        source="hubspot",
        idempotency_key="key-1",
        payload={"contactId": 99, "email": "lead@example.com"},
    )

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
        ctx = _ctx(llm=fake_llm, http=http, settings=settings, repo=repo)
        with capture_logs() as captured:
            result = await process_webhook(
                ctx,
                event_id=str(event_id),
                source="hubspot",
                payload={"contactId": 99, "email": "lead@example.com"},
                idempotency_key="key-1",
            )

    # LLM called once with the payload
    assert len(fake_llm.calls) == 1
    assert fake_llm.calls[0]["contactId"] == 99

    # Each downstream target received the enriched payload (not the raw one)
    assert len(downstream_received) == 2
    for body in downstream_received:
        assert body["event_id"] == str(event_id)
        assert body["category"] == "warm"
        assert body["confidence"] == 0.7
        assert body["data"] == {"contactId": 99, "email": "lead@example.com"}

    # Task summary
    assert result["category"] == "warm"
    assert result["dispatch_count"] == 2
    assert result["dispatch_succeeded"] == 2

    # Persistence transitions: received → processing → dispatched.
    stored = repo.events[event_id]
    assert stored.status == "dispatched"
    assert stored.enrichment is not None
    assert stored.enrichment["category"] == "warm"
    assert stored.dispatch_attempts == 1
    assert stored.last_error is None
    # No DLQ row on success.
    assert repo.dead_letters == []

    # task.processed log must not leak the raw payload.
    processed = [e for e in captured if e.get("event") == "task.processed"]
    assert len(processed) == 1
    record = processed[0]
    assert record["event_id"] == str(event_id)
    assert record["source"] == "hubspot"
    assert record["category"] == "warm"
    assert "payload" not in record
    assert "contactId" not in str(record)


async def test_process_webhook_with_no_targets_still_marks_dispatched() -> None:
    fake_llm = FakeLLMClient()
    repo = FakeEventRepository()
    event_id = await repo.create_received(
        source="hubspot", idempotency_key="k-noop", payload={"contactId": 1}
    )
    settings = AppSettings(dispatch_targets=[])  # type: ignore[call-arg]

    async with httpx.AsyncClient() as http:
        ctx = _ctx(llm=fake_llm, http=http, settings=settings, repo=repo)
        result = await process_webhook(
            ctx,
            event_id=str(event_id),
            source="hubspot",
            payload={"contactId": 1},
            idempotency_key="k-noop",
        )

    assert result["dispatch_count"] == 0
    assert repo.events[event_id].status == "dispatched"


async def test_classification_failure_below_max_tries_re_raises() -> None:
    """A retryable classification failure marks the event failed and re-raises."""
    from webhook_ai_router.services.llm import LLMClassificationError

    class BadLLM:
        async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult:
            raise LLMClassificationError("schema mismatch")

        async def close(self) -> None:
            return None

    repo = FakeEventRepository()
    event_id = await repo.create_received(
        source="hubspot", idempotency_key="k-fail", payload={"x": 1}
    )
    bad_llm: LLMClient = BadLLM()  # type: ignore[assignment]
    settings = AppSettings()  # type: ignore[call-arg]

    async with httpx.AsyncClient() as http:
        ctx = _ctx(llm=bad_llm, http=http, settings=settings, repo=repo, job_try=1)
        with pytest.raises(LLMClassificationError):
            await process_webhook(
                ctx,
                event_id=str(event_id),
                source="hubspot",
                payload={"x": 1},
                idempotency_key="k-fail",
            )

    assert repo.events[event_id].status == "failed"
    assert "schema mismatch" in (repo.events[event_id].last_error or "")
    assert repo.dead_letters == []


async def test_classification_failure_at_max_tries_dlqs_and_swallows() -> None:
    """Final retry: write DLQ row, swallow the exception so arq stops retrying."""
    from webhook_ai_router.services.llm import LLMClassificationError

    class BadLLM:
        async def classify_lead(self, payload: dict[str, Any]) -> EnrichmentResult:
            raise LLMClassificationError("permanent error")

        async def close(self) -> None:
            return None

    repo = FakeEventRepository()
    event_id = await repo.create_received(
        source="hubspot", idempotency_key="k-dlq", payload={"x": 1}
    )
    bad_llm: LLMClient = BadLLM()  # type: ignore[assignment]
    settings = AppSettings()  # type: ignore[call-arg]

    async with httpx.AsyncClient() as http:
        ctx = _ctx(llm=bad_llm, http=http, settings=settings, repo=repo, job_try=MAX_TRIES)
        # Should NOT raise — DLQ wrote the durable failure record.
        result = await process_webhook(
            ctx,
            event_id=str(event_id),
            source="hubspot",
            payload={"x": 1},
            idempotency_key="k-dlq",
        )

    assert result["category"] is None
    assert repo.events[event_id].status == "failed"
    assert len(repo.dead_letters) == 1
    dlq = repo.dead_letters[0]
    assert dlq.original_event_id == event_id
    assert "permanent error" in dlq.final_error
    assert dlq.retry_count == MAX_TRIES


async def test_all_dispatch_targets_failing_at_max_tries_dlqs() -> None:
    """If every target 5xxs and we're on the final retry, DLQ + swallow."""
    fake_llm = FakeLLMClient()
    repo = FakeEventRepository()
    event_id = await repo.create_received(
        source="hubspot", idempotency_key="k-disp", payload={"x": 1}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    targets = [DispatchTarget(url="https://broken.example.com/")]  # type: ignore[arg-type]
    settings = AppSettings(  # type: ignore[call-arg]
        dispatch_targets=targets,
        dispatch_total_timeout_seconds=1,  # make the test fast
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ctx = _ctx(
            llm=fake_llm,
            http=http,
            settings=settings,
            repo=repo,
            job_try=MAX_TRIES,
        )
        result = await process_webhook(
            ctx,
            event_id=str(event_id),
            source="hubspot",
            payload={"x": 1},
            idempotency_key="k-disp",
        )

    assert result["dispatch_succeeded"] == 0
    assert repo.events[event_id].status == "failed"
    assert len(repo.dead_letters) == 1
    assert repo.dead_letters[0].original_event_id == event_id
