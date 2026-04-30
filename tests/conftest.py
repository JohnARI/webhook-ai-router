"""Shared test fixtures.

* ``FakeAsyncRedis`` — in-memory stand-in for :class:`redis.asyncio.Redis`.
* ``FakeArqPool`` — records ``enqueue_job`` calls so tests can assert what
  the route handed to the worker without booting a real arq Worker.
* ``fake_redis`` / ``fake_arq_pool`` / ``client`` fixtures wire them into
  the FastAPI app along with a stub ``Settings`` carrying the test HubSpot
  secret.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from webhook_ai_router.config import AppEnv, LogLevel, Settings, get_settings
from webhook_ai_router.core.logging import configure_logging
from webhook_ai_router.infra.arq import get_arq_pool
from webhook_ai_router.infra.redis import get_redis
from webhook_ai_router.main import create_app

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


@pytest.fixture
def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
def fake_arq_pool() -> FakeArqPool:
    return FakeArqPool()


@pytest.fixture
def client(
    fake_redis: FakeAsyncRedis,
    fake_arq_pool: FakeArqPool,
) -> Iterator[TestClient]:
    """A TestClient with FakeAsyncRedis, FakeArqPool, and stub HubSpot secret."""
    app = create_app()

    async def _redis_override() -> FakeAsyncRedis:
        return fake_redis

    async def _arq_override() -> FakeArqPool:
        return fake_arq_pool

    def _settings_override() -> Settings:
        return Settings(hubspot_webhook_secret=HUBSPOT_TEST_SECRET)  # type: ignore[arg-type]

    app.dependency_overrides[get_redis] = _redis_override
    app.dependency_overrides[get_arq_pool] = _arq_override
    app.dependency_overrides[get_settings] = _settings_override

    # NB: not using `with TestClient(...)` — that would run the lifespan,
    # which tries to open real Redis + arq pool connections. Dependency
    # overrides handle everything routes touch.
    yield TestClient(app)
    app.dependency_overrides.clear()
