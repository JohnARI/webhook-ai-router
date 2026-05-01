"""Shared test fixtures.

* ``FakeAsyncRedis`` — in-memory stand-in for :class:`redis.asyncio.Redis`.
* ``FakeArqPool`` — records ``enqueue_job`` calls so tests can assert what
  the route handed to the worker without booting a real arq Worker.
* ``FakeEventRepository`` — in-memory stand-in for
  :class:`webhook_ai_router.services.events.EventRepository`.
* ``client`` fixture wires everything into a lifespan-skipping TestClient,
  along with a stub ``Settings`` carrying the test HubSpot secret.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from webhook_ai_router.api.routes.health import check_database
from webhook_ai_router.config import AppEnv, LogLevel, Settings, get_settings
from webhook_ai_router.core.logging import configure_logging
from webhook_ai_router.infra.arq import get_arq_pool
from webhook_ai_router.infra.redis import get_redis
from webhook_ai_router.main import create_app
from webhook_ai_router.schemas.dispatch import DispatchResult
from webhook_ai_router.schemas.enrichment import EnrichmentResult
from webhook_ai_router.services.events import (
    DuplicateIdempotencyKey,
    get_event_repository,
)

HUBSPOT_TEST_SECRET: Final = "test-hubspot-secret"

# Tests skip the FastAPI lifespan (see ``client`` fixture), so
# ``configure_logging`` never fires from there. Call it once at conftest
# import time so structlog handlers accept kwargs in every test.
configure_logging(AppEnv.DEV, LogLevel.INFO)


class FakeAsyncRedis:
    """In-memory Redis fake.

    Honours SETNX (returns ``None`` on conflict, ``True`` on success) but
    intentionally ignores TTLs — tests that care about expiry drive it
    explicitly via ``delete``.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def set(
        self,
        key: str,
        value: bytes | str,
        *,
        ex: int | None = None,
        nx: bool = False,
        **_: Any,
    ) -> bool | None:
        if nx and key in self._store:
            return None
        if isinstance(value, bytes):
            self._store[key] = value
        elif isinstance(value, str):
            self._store[key] = value.encode("utf-8")
        else:
            self._store[key] = bytes(value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    async def aclose(self) -> None:
        return None


@dataclass
class EnqueuedJob:
    """One captured enqueue call."""

    function: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    job_id: str | None


@dataclass
class FakeArqPool:
    """Records ``enqueue_job`` calls; honours ``_job_id`` dedup."""

    enqueued: list[EnqueuedJob] = field(default_factory=list)
    _job_ids: set[str] = field(default_factory=set)

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if _job_id is not None and _job_id in self._job_ids:
            # arq returns None when the job_id collides — keep that contract.
            return None
        if _job_id is not None:
            self._job_ids.add(_job_id)
        # Filter out leading underscore kwargs (`_queue_name`, etc.) for
        # cleaner test assertions; keep them in EnqueuedJob.kwargs.
        self.enqueued.append(
            EnqueuedJob(function=function, args=args, kwargs=kwargs, job_id=_job_id)
        )
        return object()  # sentinel — Job-like, never inspected by our code

    async def aclose(self) -> None:
        return None


@dataclass
class StoredEvent:
    """One row recorded by :class:`FakeEventRepository`."""

    event_id: uuid.UUID
    source: str
    idempotency_key: str
    payload: dict[str, Any]
    status: str = "received"
    enrichment: dict[str, Any] | None = None
    dispatch_attempts: int = 0
    last_error: str | None = None


@dataclass
class StoredDeadLetter:
    """One DLQ row recorded by :class:`FakeEventRepository`."""

    original_event_id: uuid.UUID
    final_error: str
    retry_count: int


@dataclass
class FakeEventRepository:
    """In-memory stand-in for :class:`EventRepository`.

    Honours the unique-idempotency-key contract by raising
    :class:`DuplicateIdempotencyKey` on the second insert with the same key.
    Mutation methods are no-ops apart from updating the recorded state.
    """

    events: dict[uuid.UUID, StoredEvent] = field(default_factory=dict)
    by_key: dict[str, uuid.UUID] = field(default_factory=dict)
    dead_letters: list[StoredDeadLetter] = field(default_factory=list)

    async def create_received(
        self,
        *,
        source: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> uuid.UUID:
        if idempotency_key in self.by_key:
            raise DuplicateIdempotencyKey(idempotency_key)
        event_id = uuid.uuid4()
        self.events[event_id] = StoredEvent(
            event_id=event_id,
            source=source,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        self.by_key[idempotency_key] = event_id
        return event_id

    async def get(self, event_id: uuid.UUID) -> StoredEvent | None:
        return self.events.get(event_id)

    async def mark_processing(self, event_id: uuid.UUID) -> None:
        self.events[event_id].status = "processing"

    async def mark_dispatched(
        self,
        event_id: uuid.UUID,
        *,
        enrichment: EnrichmentResult,
        dispatch_results: list[DispatchResult],
        attempts: int,
    ) -> None:
        ev = self.events[event_id]
        ev.status = "dispatched"
        ev.enrichment = {
            "category": enrichment.category,
            "reason": enrichment.reason,
            "confidence": enrichment.confidence,
            "dispatch": {
                "results": [r.model_dump() for r in dispatch_results],
                "total": len(dispatch_results),
                "succeeded": sum(1 for r in dispatch_results if r.success),
            },
        }
        ev.dispatch_attempts = attempts
        ev.last_error = None

    async def mark_failed(self, event_id: uuid.UUID, *, error: str, attempts: int) -> None:
        ev = self.events[event_id]
        ev.status = "failed"
        ev.last_error = error
        ev.dispatch_attempts = attempts

    async def insert_dead_letter(
        self,
        *,
        original_event_id: uuid.UUID,
        final_error: str,
        retry_count: int,
    ) -> uuid.UUID:
        self.dead_letters.append(
            StoredDeadLetter(
                original_event_id=original_event_id,
                final_error=final_error,
                retry_count=retry_count,
            )
        )
        return uuid.uuid4()


@pytest.fixture
def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
def fake_arq_pool() -> FakeArqPool:
    return FakeArqPool()


@pytest.fixture
def fake_event_repo() -> FakeEventRepository:
    return FakeEventRepository()


@pytest.fixture
def client(
    fake_redis: FakeAsyncRedis,
    fake_arq_pool: FakeArqPool,
    fake_event_repo: FakeEventRepository,
) -> Iterator[TestClient]:
    """A TestClient with all infra fakes wired in.

    Skips the FastAPI lifespan (would open real Redis / arq / Postgres
    connections); dependency overrides handle every route call.
    """
    app = create_app()

    async def _redis_override() -> FakeAsyncRedis:
        return fake_redis

    async def _arq_override() -> FakeArqPool:
        return fake_arq_pool

    async def _events_override() -> FakeEventRepository:
        return fake_event_repo

    async def _check_db_override() -> bool:
        return True

    def _settings_override() -> Settings:
        return Settings(hubspot_webhook_secret=HUBSPOT_TEST_SECRET)  # type: ignore[arg-type]

    app.dependency_overrides[get_redis] = _redis_override
    app.dependency_overrides[get_arq_pool] = _arq_override
    app.dependency_overrides[get_event_repository] = _events_override
    app.dependency_overrides[check_database] = _check_db_override
    app.dependency_overrides[get_settings] = _settings_override

    yield TestClient(app)
    app.dependency_overrides.clear()
