"""initial schema

Creates the MVP relational schema described in docs/02-data-model.md:
seven tables (tenants, controls, evidence, check_runs, findings,
finding_evidence, audit_log) with their constraints and indexes; the
`pdpl_app` application role with the grant pattern from ADR-0003; and the
BEFORE TRUNCATE trigger that hard-fails any attempt to truncate audit_log.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # tenants
    # ------------------------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("business_type", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
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
        sa.CheckConstraint("length(name) > 0", name="ck_tenants_name_nonempty"),
        sa.CheckConstraint(
            "business_type IN ('ecommerce','clinic','saas','other')",
            name="ck_tenants_business_type",
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended','cancelled')",
            name="ck_tenants_status",
        ),
    )

    # ------------------------------------------------------------------
    # controls
    # ------------------------------------------------------------------
    op.create_table(
        "controls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("severity_weight", sa.Numeric(4, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
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
        sa.CheckConstraint("code LIKE 'PDPL-%'", name="ck_controls_code_prefix"),
        sa.CheckConstraint(
            "severity_weight > 0 AND severity_weight <= 10",
            name="ck_controls_severity_weight_range",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_controls_effective_range",
        ),
    )

    # ------------------------------------------------------------------
    # evidence
    # ------------------------------------------------------------------
    op.create_table(
        "evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("file_uri", sa.Text(), nullable=True),
        sa.Column(
            "collected_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "type IN ('questionnaire_answer','document_upload',"
            "'scheduled_check_result','manual_attestation')",
            name="ck_evidence_type",
        ),
        sa.CheckConstraint(
            "type <> 'document_upload' OR file_uri IS NOT NULL",
            name="ck_evidence_document_requires_uri",
        ),
    )
    op.create_index(
        "idx_evidence_tenant_collected",
        "evidence",
        ["tenant_id", sa.text("collected_at DESC")],
    )

    # ------------------------------------------------------------------
    # check_runs
    # ------------------------------------------------------------------
    op.create_table(
        "check_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
        ),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "started_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "completed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('initial_questionnaire','scheduled','manual')",
            name="ck_check_runs_kind",
        ),
        sa.CheckConstraint(
            "status IN ('running','completed','failed')",
            name="ck_check_runs_status",
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR completed_at >= started_at",
            name="ck_check_runs_completed_after_started",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR completed_at IS NOT NULL",
            name="ck_check_runs_completed_has_timestamp",
        ),
    )
    op.create_index(
        "idx_check_runs_tenant_started",
        "check_runs",
        ["tenant_id", sa.text("started_at DESC")],
    )

    # ------------------------------------------------------------------
    # findings  (SCD Type 2 — see ADR-0002)
    # ------------------------------------------------------------------
    op.create_table(
        "findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "control_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("controls.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "check_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("check_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("ai_explanation_ar", sa.Text(), nullable=True),
        sa.Column("due_date", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "valid_from",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("valid_to", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "detected_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('compliant','partial','non_compliant',"
            "'not_applicable','unknown')",
            name="ck_findings_status",
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_findings_valid_range",
        ),
    )
    # One *current* finding per (tenant, control). Enforced at the DB layer
    # so an application bug cannot duplicate a current row. See ADR-0002.
    op.create_index(
        "uniq_findings_current",
        "findings",
        ["tenant_id", "control_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        "idx_findings_deadline",
        "findings",
        ["tenant_id", "due_date"],
        postgresql_where=sa.text(
            "valid_to IS NULL AND status <> 'compliant' AND due_date IS NOT NULL"
        ),
    )
    op.create_index(
        "idx_findings_control_status_current",
        "findings",
        ["control_id", "status"],
        postgresql_where=sa.text("valid_to IS NULL"),
    )
    op.create_index(
        "idx_findings_tenant_valid_from",
        "findings",
        ["tenant_id", sa.text("valid_from DESC")],
    )

    # ------------------------------------------------------------------
    # finding_evidence  (M2M join)
    # ------------------------------------------------------------------
    op.create_table(
        "finding_evidence",
        sa.Column(
            "finding_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("findings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "linked_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("finding_id", "evidence_id"),
    )
    op.create_index(
        "idx_finding_evidence_evidence",
        "finding_evidence",
        ["evidence_id"],
    )

    # ------------------------------------------------------------------
    # audit_log  (append-only — see ADR-0003)
    # ------------------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "actor_type IN ('user','system','ai_subsystem','migration')",
            name="ck_audit_log_actor_type",
        ),
    )
    op.create_index(
        "idx_audit_log_tenant_created",
        "audit_log",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_audit_log_correlation",
        "audit_log",
        ["correlation_id"],
        postgresql_where=sa.text("correlation_id IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # Application role and grants  (ADR-0003)
    # ------------------------------------------------------------------
    # Create pdpl_app idempotently — Supabase environments may already have
    # the role from a prior run on the same project.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE pdpl_app NOLOGIN;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO pdpl_app;")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON tenants, controls, evidence, check_runs, findings, finding_evidence
        TO pdpl_app;
        """
    )
    # audit_log: append-only for the application.
    op.execute("GRANT SELECT, INSERT ON audit_log TO pdpl_app;")
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM pdpl_app;"
    )

    # ------------------------------------------------------------------
    # TRUNCATE protection on audit_log  (independent gate — ADR-0003)
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_block_truncate()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION
                'TRUNCATE on audit_log is not permitted: append-only invariant (ADR-0003)';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT
        EXECUTE FUNCTION audit_log_block_truncate();
        """
    )


def downgrade() -> None:
    # Reverse order: trigger -> function -> grants -> tables -> role.
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;")
    op.execute("DROP FUNCTION IF EXISTS audit_log_block_truncate();")

    # Revoke before dropping tables so the role has no dangling grants.
    op.execute(
        """
        REVOKE ALL ON tenants, controls, evidence, check_runs,
                     findings, finding_evidence, audit_log
        FROM pdpl_app;
        """
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM pdpl_app;")

    op.drop_index("idx_audit_log_correlation", table_name="audit_log")
    op.drop_index("idx_audit_log_tenant_created", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("idx_finding_evidence_evidence", table_name="finding_evidence")
    op.drop_table("finding_evidence")

    op.drop_index("idx_findings_tenant_valid_from", table_name="findings")
    op.drop_index("idx_findings_control_status_current", table_name="findings")
    op.drop_index("idx_findings_deadline", table_name="findings")
    op.drop_index("uniq_findings_current", table_name="findings")
    op.drop_table("findings")

    op.drop_index("idx_check_runs_tenant_started", table_name="check_runs")
    op.drop_table("check_runs")

    op.drop_index("idx_evidence_tenant_collected", table_name="evidence")
    op.drop_table("evidence")

    op.drop_table("controls")
    op.drop_table("tenants")

    op.execute("DROP ROLE IF EXISTS pdpl_app;")
