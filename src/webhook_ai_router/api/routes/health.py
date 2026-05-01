"""Liveness and readiness probes.

* ``GET /healthz`` — always 200; the process is up.
* ``GET /readyz`` — 200 only when Redis and Postgres respond. Otherwise 503.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from webhook_ai_router.db.session import get_db_session
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
    except (RedisError, OSError):
        return False


async def check_database(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> bool:
    """Run ``SELECT 1`` against Postgres and return whether it answered."""
    try:
        await session.execute(text("SELECT 1"))
        return True
    except (SQLAlchemyError, OSError):
        return False


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
