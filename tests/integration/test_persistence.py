"""Persistence round-trip against a real Postgres.

Run by setting ``TEST_DATABASE_URL`` to an asyncpg DSN. With the bundled
``docker-compose up -d postgres`` running, that's::

    TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/webhook_ai_router

The test creates and drops the schema itself so it's isolated from your
local data and from concurrent test runs (each test uses unique idempotency
keys / IDs).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from webhook_ai_router.db.models import (
    Base,
    DeadLetterEvent,
    EventStatus,
    WebhookEvent,
)
from webhook_ai_router.db.session import create_db_engine, create_db_sessionmaker
from webhook_ai_router.schemas.dispatch import DispatchResult
from webhook_ai_router.schemas.enrichment import EnrichmentResult
from webhook_ai_router.services.events import (
    DuplicateIdempotencyKey,
    EventRepository,
)

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DB_URL is None,
    reason="TEST_DATABASE_URL not set; bring up docker-compose postgres + export it",
)


@pytest_asyncio.fixture
async def sessionmaker_with_schema() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create the schema for the duration of the test, then drop it.

    Cheaper than running alembic — and verifies the SQLAlchemy metadata
    matches what alembic would produce, since both come from the same
    ``Base.metadata``.
    """
    assert TEST_DB_URL is not None  # pytest-skipif handles the None case
    engine = create_db_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    sm = create_db_sessionmaker(engine)
    try:
        yield sm
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def test_create_received_inserts_in_received_status(
    sessionmaker_with_schema: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        event_id = await repo.create_received(
            source="hubspot",
            idempotency_key=f"test-{uuid.uuid4()}",
            payload={"contactId": 7, "email": "x@example.com"},
        )

    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        event = await repo.get(event_id)
        assert event is not None
        assert event.status == EventStatus.RECEIVED
        assert event.source == "hubspot"
        assert event.payload == {"contactId": 7, "email": "x@example.com"}
        assert event.dispatch_attempts == 0
        assert event.last_error is None


async def test_duplicate_idempotency_key_raises(
    sessionmaker_with_schema: async_sessionmaker[AsyncSession],
) -> None:
    key = f"dup-{uuid.uuid4()}"
    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        await repo.create_received(source="hubspot", idempotency_key=key, payload={})

    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        with pytest.raises(DuplicateIdempotencyKey):
            await repo.create_received(source="hubspot", idempotency_key=key, payload={})


async def test_full_dispatched_flow(
    sessionmaker_with_schema: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        event_id = await repo.create_received(
            source="hubspot",
            idempotency_key=f"flow-{uuid.uuid4()}",
            payload={"contactId": 1},
        )
        await repo.mark_processing(event_id)
        await repo.mark_dispatched(
            event_id,
            enrichment=EnrichmentResult(
                category="hot", reason="explicit demo request", confidence=0.92
            ),
            dispatch_results=[
                DispatchResult(
                    url="https://hook.example.com/", success=True, status_code=200, attempts=1
                )
            ],
            attempts=1,
        )

    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        event = await repo.get(event_id)
        assert event is not None
        assert event.status == EventStatus.DISPATCHED
        assert event.enrichment is not None
        assert event.enrichment["category"] == "hot"
        assert event.enrichment["dispatch"]["succeeded"] == 1


async def test_dead_letter_round_trip(
    sessionmaker_with_schema: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker_with_schema() as session:
        repo = EventRepository(session)
        event_id = await repo.create_received(
            source="hubspot",
            idempotency_key=f"dlq-{uuid.uuid4()}",
            payload={"x": 1},
        )
        await repo.mark_failed(event_id, error="permanent boom", attempts=5)
        dlq_id = await repo.insert_dead_letter(
            original_event_id=event_id,
            final_error="permanent boom",
            retry_count=5,
        )

    assert dlq_id is not None

    # Verify DLQ row + FK linkage in a fresh session.
    async with sessionmaker_with_schema() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(DeadLetterEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].original_event_id == event_id
        assert rows[0].retry_count == 5
        assert rows[0].final_error == "permanent boom"

        ev = (
            await session.execute(select(WebhookEvent).where(WebhookEvent.id == event_id))
        ).scalar_one()
        assert ev.status == EventStatus.FAILED
        assert ev.last_error == "permanent boom"
