"""initial: webhook_events + dead_letter_events

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_EVENT_STATUSES = ("received", "processing", "dispatched", "failed")


def upgrade() -> None:
    event_status = postgresql.ENUM(*_EVENT_STATUSES, name="event_status", create_type=True)
    event_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(*_EVENT_STATUSES, name="event_status", create_type=False),
            nullable=False,
            server_default="received",
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enrichment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "dispatch_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_webhook_events_idempotency_key"),
    )
    op.create_index(
        "ix_webhook_events_idempotency_key",
        "webhook_events",
        ["idempotency_key"],
    )
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])

    op.create_table(
        "dead_letter_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "original_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_events.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("final_error", sa.Text(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "ix_dead_letter_events_original_event_id",
        "dead_letter_events",
        ["original_event_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dead_letter_events_original_event_id", table_name="dead_letter_events")
    op.drop_table("dead_letter_events")
    op.drop_index("ix_webhook_events_status", table_name="webhook_events")
    op.drop_index("ix_webhook_events_idempotency_key", table_name="webhook_events")
    op.drop_table("webhook_events")
    postgresql.ENUM(name="event_status").drop(op.get_bind(), checkfirst=True)
