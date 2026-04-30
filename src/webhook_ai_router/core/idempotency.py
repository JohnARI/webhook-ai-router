"""Stripe-style Idempotency-Key handling backed by Redis.

Two concerns live here:

* :class:`CachedResponse` — a Pydantic model representing a previously-served
  response (status + headers + body bytes), JSON-encoded into a Redis string.
* :class:`IdempotencyStore` — wraps a :class:`redis.asyncio.Redis` client and
  exposes ``get`` / ``set`` for the cache and ``lock`` / ``unlock`` for a
  SETNX-based distributed lock so two concurrent requests with the same key
  cannot both execute the side-effecting work.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from webhook_ai_router.config import Settings, get_settings
from webhook_ai_router.infra.redis import RedisClient, get_redis

_RESPONSE_KEY_PREFIX: Final = "idem:resp:"
_LOCK_KEY_PREFIX: Final = "idem:lock:"


class CachedResponse(BaseModel):
    """Serialised HTTP response stored under an idempotency key."""

    model_config = ConfigDict(frozen=True)

    status_code: int
    headers: dict[str, str]
    body: bytes  # JSON-encoded as base64 by Pydantic


class IdempotencyStore:
    """Cache + distributed lock for idempotent webhook delivery."""

    def __init__(
        self,
        redis: RedisClient,
        *,
        default_ttl_seconds: int = 86_400,
        default_lock_ttl_seconds: int = 60,
    ) -> None:
        self._redis = redis
        self._default_ttl = default_ttl_seconds
        self._default_lock_ttl = default_lock_ttl_seconds

    async def get(self, key: str) -> CachedResponse | None:
        """Return the cached response for ``key`` or ``None`` if absent."""
        raw = await self._redis.get(_RESPONSE_KEY_PREFIX + key)
        if raw is None:
            return None
        return CachedResponse.model_validate_json(raw)

    async def set(
        self,
        key: str,
        response: CachedResponse,
        ttl_seconds: int | None = None,
    ) -> None:
        """Persist ``response`` under ``key`` for ``ttl_seconds`` (default
        :attr:`_default_ttl`).
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        await self._redis.set(
            _RESPONSE_KEY_PREFIX + key,
            response.model_dump_json().encode("utf-8"),
            ex=ttl,
        )

    async def lock(self, key: str, ttl_seconds: int | None = None) -> bool:
        """Try to acquire the per-key distributed lock.

        Returns ``True`` if this caller now holds the lock, ``False`` if it's
        already held by someone else. SETNX semantics: the lock auto-expires
        after ``ttl_seconds`` to bound the impact of a crashed holder.
        """
        ttl = ttl_seconds if ttl_seconds is not None else self._default_lock_ttl
        result = await self._redis.set(_LOCK_KEY_PREFIX + key, b"1", ex=ttl, nx=True)
        return result is True

    async def unlock(self, key: str) -> None:
        """Release a previously-acquired lock. Safe to call if not held."""
        await self._redis.delete(_LOCK_KEY_PREFIX + key)


async def get_idempotency_store(
    redis: Annotated[RedisClient, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> IdempotencyStore:
    """FastAPI dependency wiring the store with app config."""
    return IdempotencyStore(
        redis,
        default_ttl_seconds=settings.idempotency_ttl_seconds,
        default_lock_ttl_seconds=settings.idempotency_lock_ttl_seconds,
    )
