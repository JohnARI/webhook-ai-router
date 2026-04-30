"""Liveness and readiness probes.

* ``GET /healthz`` — always 200; the process is up.
* ``GET /readyz`` — 200 only when Redis and Postgres respond. Otherwise 503.

The Postgres dependency is still a placeholder (always returns ``True``);
the database client is wired up in a later session. Redis is real.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError

from webhook_ai_router.infra.redis import RedisClient, get_redis

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    redis: bool
    database: bool


async def check_redis(redis: Annotated[RedisClient, Depends(get_redis)]) -> bool:
    """Ping Redis and return whether it answered."""
    try:
        return bool(await redis.ping())
    except RedisError, OSError:
        return False


async def check_database() -> bool:
    """Placeholder Postgres health check.

    Replaced in a later session by an actual ``SELECT 1`` against the
    async engine.
    """
    return True


@router.get(
    "/healthz",
    status_code=status.HTTP_200_OK,
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def healthz() -> HealthResponse:
    return HealthResponse()


@router.get(
    "/readyz",
    summary="Readiness probe",
    responses={
        status.HTTP_200_OK: {"model": ReadinessResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse},
    },
)
async def readyz(
    redis_ok: Annotated[bool, Depends(check_redis)],
    database_ok: Annotated[bool, Depends(check_database)],
) -> JSONResponse:
    ready = redis_ok and database_ok
    body = ReadinessResponse(
        status="ready" if ready else "not_ready",
        redis=redis_ok,
        database=database_ok,
    )
    return JSONResponse(
        status_code=(status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE),
        content=body.model_dump(mode="json"),
    )
