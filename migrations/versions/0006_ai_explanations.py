"""ai_explanations content-hash cache (ADR-0009 §6, C3b)

A dedicated cache table that decouples a verified AI gap explanation from the
finding's SCD Type-2 lifecycle (a finding gets a NEW row on every change,
ADR-0002): the explanation is a pure function of its content key, not of a
finding's identity, so it is keyed by a content hash and reused across every
tenant with the identical gap (tenant-agnostic, leak-free — ADR-0009 §2/§6).

Rows are IMMUTABLE. The key is `content_hash` (sha256 of the canonical key
payload — see src/pdpl/db/ai_explanations.py), which is the PRIMARY KEY and
therefore the sole uniqueness constraint; the same content can never produce a
second row. `prompt_version`/`model`/`lang` are stored for observability (which
prompt/model produced this row), NOT as part of the key.

Grants MIRROR audit_log (migration 0002 / ADR-0003), NOT outbox: pdpl_app gets
SELECT + INSERT only, and UPDATE/DELETE/TRUNCATE are revoked. The application
computes-once-and-reuses; it never mutates a cached explanation (a refresh is a
prompt_version bump that yields a NEW key, never an in-place edit). The
immutability is enforced at the role, not by application discipline.

Revision ID: 0006_ai_explanations
Revises: 0005_outbox
Create Date: 2026-06-23
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0006_ai_explanations"
down_revision: Union[str, None] = "0005_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # ai_explanations — content-hash cache (ADR-0009 §6).
    # ------------------------------------------------------------------
    op.create_table(
        "ai_explanations",
        # The cache key: sha256 hex of the canonical key payload
        # (prompt_version, model, control_code, status, rationale, lang).
        # PK => the sole uniqueness constraint; the same content is one row.
        sa.Column("content_hash", sa.Text(), primary_key=True),
        # Output language. Kept in the KEY (and here) so a second language later
        # needs no schema change; the row is self-describing via this column.
        sa.Column("lang", sa.Text(), nullable=False),
        # The verified explanation, language-tagged by `lang`. ONLY text the
        # caller has already gate-verified is ever written here (ADR-0009 §6) —
        # the table itself does not and cannot verify.
        sa.Column("text", sa.Text(), nullable=False),
        # Observability: which prompt/model produced this row. NOT part of the
        # key's identity enforcement (that is the content_hash) — a future debug
        # need for more columns is a future migration, not a reason to widen now.
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ------------------------------------------------------------------
    # Grants — MIRROR audit_log (ADR-0003 / migration 0002), NOT outbox.
    # Immutable rows: SELECT + INSERT only; UPDATE/DELETE/TRUNCATE revoked.
    # pdpl_app already exists (migration 0001).
    # ------------------------------------------------------------------
    op.execute("GRANT SELECT, INSERT ON ai_explanations TO pdpl_app;")
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON ai_explanations FROM pdpl_app;"
    )


def downgrade() -> None:
    # Revoke before dropping so the role has no dangling grant.
    op.execute("REVOKE ALL ON ai_explanations FROM pdpl_app;")
    op.drop_table("ai_explanations")
