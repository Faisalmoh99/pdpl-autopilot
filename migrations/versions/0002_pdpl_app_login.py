"""enable pdpl_app to log in + reaffirm grants

Per ADR-0004 §5: migration 0001 created `pdpl_app NOLOGIN` and applied the
grant pattern, but the role cannot open a connection yet. This migration
raises pdpl_app to LOGIN and sets its password from PDPL_APP_PASSWORD,
then defensively re-issues the grant pattern (audit_log restricted to
SELECT + INSERT; UPDATE/DELETE/TRUNCATE remain revoked) so that any
environment drift since 0001 is brought back into compliance.

0001 is *not* edited. Editing an applied migration is the category of
mistake we do not start making in Phase 2; new state lands in new
migrations.

Revision ID: 0002_pdpl_app_login
Revises: 0001_initial_schema
Create Date: 2026-06-12
"""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op


revision: str = "0002_pdpl_app_login"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Migration 0002 needs it to ALTER ROLE "
            "pdpl_app WITH LOGIN PASSWORD. See ADR-0004 §4 and §5."
        )
    return value


def upgrade() -> None:
    password = _require_env("PDPL_APP_PASSWORD")
    # ALTER ROLE does not accept parameter binds for the password literal,
    # so we interpolate. We control the source (env), but still escape
    # single quotes defensively per standard SQL string-literal rules.
    escaped = password.replace("'", "''")
    op.execute(
        f"ALTER ROLE pdpl_app WITH LOGIN PASSWORD '{escaped}'"
    )

    # Reaffirm the ADR-0003 grant pattern. GRANT is idempotent so re-issuing
    # is safe; this guards against drift from 0001 in any environment.
    op.execute("GRANT USAGE ON SCHEMA public TO pdpl_app;")
    op.execute(
        """
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON tenants, controls, evidence, check_runs, findings, finding_evidence
        TO pdpl_app;
        """
    )
    # audit_log: append-only for the application. Belt-and-suspenders re-issue.
    op.execute("GRANT SELECT, INSERT ON audit_log TO pdpl_app;")
    op.execute(
        "REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM pdpl_app;"
    )


def downgrade() -> None:
    # Reverse: clear the password and put the role back to NOLOGIN so the
    # post-downgrade state matches what 0001 alone produces.
    op.execute("ALTER ROLE pdpl_app WITH NOLOGIN")
    op.execute("ALTER ROLE pdpl_app PASSWORD NULL")
