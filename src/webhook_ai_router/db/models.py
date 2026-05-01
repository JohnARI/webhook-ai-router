"""SQLAlchemy 2.0 ORM models for persisted webhook events.

These are the persistence layer. **Do not import them outside**
``src/webhook_ai_router/db/`` and ``src/webhook_ai_router/services/`` —
schemas live in ``src/webhook_ai_router/schemas/`` and never share classes
with these. Conversion between the two happens explicitly in services.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class EventStatus(enum.StrEnum):
    """Lifecycle status of a webhook event."""

    RECEIVED = "received"
    PROCESSING = "processing"
    DISPATCHED = "dispatched"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WebhookEvent(Base):
    """One inbound webhook delivery, persisted at receive time."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus, name="event_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=EventStatus.RECEIVED,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enrichment: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    dead_letter: Mapped[DeadLetterEvent | None] = relationship(
        back_populates="original_event", cascade="all, delete-orphan", uselist=False
    )


class DeadLetterEvent(Base):
    """A WebhookEvent that exhausted its retry budget without dispatch."""

    __tablename__ = "dead_letter_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    final_error: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    original_event: Mapped[WebhookEvent] = relationship(back_populates="dead_letter")
