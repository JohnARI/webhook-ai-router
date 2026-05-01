"""Async SQLAlchemy engine + sessionmaker + FastAPI dependency.

Pattern (mirrors ``infra/redis.py``):

* Application lifespan builds a single :class:`AsyncEngine` and
  :class:`async_sessionmaker` and stows them on
  ``app.state.db_engine`` / ``app.state.db_sessionmaker``.
* :func:`get_db_session` is the FastAPI dependency that opens a per-request
  session against the app-scoped sessionmaker. It rolls back on exception
  and commits implicitly via the ``async with`` block on clean exit.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_db_engine(database_url: str) -> AsyncEngine:
    """Build an :class:`AsyncEngine` for the given DSN.

    ``pool_pre_ping=True`` checks connection liveness before each checkout
    so a Postgres restart doesn't poison the pool.
    """
    return create_async_engine(database_url, pool_pre_ping=True)


def create_db_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the per-request sessionmaker.

    ``expire_on_commit=False`` so attribute access on a returned model after
    commit doesn't trigger a fresh SELECT.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a fresh :class:`AsyncSession`.

    Rolls back on uncaught exception. Commits are the caller's job — the
    repository layer commits explicitly in :mod:`webhook_ai_router.services.events`.
    """
    sessionmaker = cast(async_sessionmaker[AsyncSession], request.app.state.db_sessionmaker)
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
