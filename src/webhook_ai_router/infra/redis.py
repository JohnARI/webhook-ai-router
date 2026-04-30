"""Async Redis client factory + FastAPI dependency.

Pattern:

* The application lifespan (``main._lifespan``) builds a single
  :class:`redis.asyncio.Redis` connection pool and stows it on
  ``app.state.redis``.
* :func:`get_redis` is the FastAPI dependency that reads it back; injecting
  it into a route is the only sanctioned way to talk to Redis. No module-
  level clients, no globals.
"""

from __future__ import annotations

from fastapi import Request
from redis.asyncio import Redis as AsyncRedis

#: ``Redis`` is generic over the value type returned by ``get`` etc.; we always
#: run with ``decode_responses=False`` so values are bytes.
type RedisClient = AsyncRedis[bytes]


def create_redis_client(url: str) -> RedisClient:
    """Build (lazily-connecting) async Redis client from a URL."""
    return AsyncRedis.from_url(url, decode_responses=False)


async def get_redis(request: Request) -> RedisClient:
    """FastAPI dependency returning the app-scoped Redis client."""
    redis: RedisClient = request.app.state.redis
    return redis
