"""outbox table for reliable alerting (ADR-0008)

Creates the transactional `outbox` table: a worsening finding transition
writes a row here in the SAME transaction as the finding (atomic, see
src/pdpl/db/outbox.py), and a separate worker (Session B) claims pending
rows with FOR UPDATE SKIP LOCKED, sends them through the Notifier port,
and marks them sent / reschedules with backoff / dead-letters.

The full table is created now — every column the Session B worker needs
(attempts, next_attempt_at, last_error, sent_at) ships here so the worker
adds no further migration.

Grants DIFFER from audit_log: pdpl_app gets SELECT + INSERT + UPDATE (no
DELETE). The enqueue path inserts; the worker selects and updates status.
This is deliberately NOT audit_log's INSERT+SELECT-only pattern — the
worker must UPDATE rows to mark them sent/failed/dead_letter.

Revision ID: 0005_outbox
Revises: 0004_seed_questions
Create Date: 2026-06-16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005_outbox"
down_revision: Union[str, None] = "0004_seed_questions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # outbox
    # ------------------------------------------------------------------
    op.create_table(
        "outbox",
        # App-generated UUID v7 — no server default (identifier strategy,
        # docs/02-data-model.md).
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The outbox event kind, e.g. 'finding.worsened'. Free-text now so
        # future event types can share the table.
        sa.Column("topic", sa.Text(), nullable=False),
        # Alert content. No secrets land here.
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        # 'alert:finding-transition:{finding_id}' — 1:1 with a transition.
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # Drives the claim query; pending rows are immediately due.
        sa.Column(
            "next_attempt_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "sent_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','dead_letter')",
            name="ck_outbox_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_nonneg"),
        sa.CheckConstraint(
            "status != 'sent' OR sent_at IS NOT NULL",
            name="ck_outbox_sent_has_sent_at",
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uniq_outbox_idempotency_key"
        ),
    )

    # Claim-query index — partial, excludes terminal rows so it stays small.
    op.execute(
        """
        CREATE INDEX idx_outbox_due
        ON outbox (next_attempt_at)
        WHERE status IN ('pending','failed')
        """
    )
    # Per-tenant operational reads.
    op.create_index(
        "idx_outbox_tenant_created",
        "outbox",
        ["tenant_id", sa.text("created_at DESC")],
    )

    # ------------------------------------------------------------------
    # Grants (ADR-0008 — DIFFER from audit_log: INSERT + SELECT + UPDATE,
    # NO DELETE). pdpl_app already exists (migration 0001).
    # ------------------------------------------------------------------
    op.execute("GRANT SELECT, INSERT, UPDATE ON outbox TO pdpl_app;")


def downgrade() -> None:
    # Revoke before dropping so the role has no dangling grant.
    op.execute("REVOKE ALL ON outbox FROM pdpl_app;")
    op.drop_index("idx_outbox_tenant_created", table_name="outbox")
    op.execute("DROP INDEX IF EXISTS idx_outbox_due;")
    op.drop_table("outbox")
