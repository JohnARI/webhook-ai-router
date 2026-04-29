"""Tests for the liveness/readiness probes."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from webhook_ai_router.api.routes.health import check_database, check_redis
from webhook_ai_router.main import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_healthz_returns_200(client: TestClient) -> None:
    resp = client.get("healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_returns_200_when_dependencies_healthy(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ready", "redis": True, "database": True}


def test_readyz_returns_503_when_redis_down() -> None:
    app = create_app()

    async def _redis_down() -> bool:
        return False

    app.dependency_overrides[check_redis] = _redis_down
    with TestClient(app) as client:
        resp = client.get("/readyz")
    app.dependency_overrides.clear()

    assert resp.status_code == 503
    body = resp.json()
    assert body == {"status": "not_ready", "redis": False, "database": True}


def test_readyz_returns_503_when_database_down() -> None:
    app = create_app()

    async def _db_down() -> bool:
        return False

    app.dependency_overrides[check_database] = _db_down
    with TestClient(app) as client:
        resp = client.get("/readyz")
    app.dependency_overrides.clear()

    assert resp.status_code == 503
    body = resp.json()
    assert body == {"status": "not_ready", "redis": True, "database": False}
