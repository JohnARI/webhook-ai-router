"""Shared test fixtures.

* ``FakeAsyncRedis`` — a tiny in-memory stand-in implementing the subset of
  :class:`redis.asyncio.Redis` that ``IdempotencyStore`` and the readiness
  probe actually call.
* ``fake_redis`` / ``client`` fixtures wire it (plus a stub ``Settings``
  carrying the test HubSpot secret) into the FastAPI app.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import pytest
from fastapi.testclient import TestClient

from webhook_ai_router.core.settings import Settings, get_settings
from webhook_ai_router.infra.redis import get_redis
from webhook_ai_router.main import create_app

HUBSPOT_TEST_SECRET: Final = "test-hubspot-secret"


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


@pytest.fixture
def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis()


@pytest.fixture
def client(fake_redis: FakeAsyncRedis) -> Iterator[TestClient]:
    """A TestClient with FakeAsyncRedis and a stub HubSpot secret wired in."""
    app = create_app()

    async def _redis_override() -> FakeAsyncRedis:
        return fake_redis

    def _settings_override() -> Settings:
        return Settings(hubspot_webhook_secret=HUBSPOT_TEST_SECRET)  # type: ignore[arg-type]

    app.dependency_overrides[get_redis] = _redis_override
    app.dependency_overrides[get_settings] = _settings_override

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
