"""arq queue infrastructure: pool factory + FastAPI dependency.

The application lifespan builds a single :class:`arq.connections.ArqRedis`
pool and stows it on ``app.state.arq_pool``. Routes get at it via the
:func:`get_arq_pool` dependency. The worker process uses its own
:class:`arq.connections.RedisSettings` derived from the same env vars.
"""

from __future__ import annotations

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Request


def redis_settings_from_url(redis_url: str) -> RedisSettings:
    """Build :class:`RedisSettings` from a standard ``redis://`` URL."""
    return RedisSettings.from_dsn(redis_url)


async def create_arq_pool(redis_url: str) -> ArqRedis:
    """Open an ArqRedis pool against ``redis_url``."""
    return await create_pool(redis_settings_from_url(redis_url))


async def get_arq_pool(request: Request) -> ArqRedis:
    """FastAPI dependency returning the app-scoped arq pool."""
    pool: ArqRedis = request.app.state.arq_pool
    return pool
