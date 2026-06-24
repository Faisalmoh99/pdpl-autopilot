"""Prove ADR-0003 at the APP layer.

Connecting directly as `pdpl_app` (the role the FastAPI runtime uses),
INSERT must succeed, but UPDATE / DELETE / TRUNCATE on `audit_log` must
all fail at the database. This is the test that converts ADR-0003 from a
schema-level invariant into an application-level guarantee.
"""

from __future__ import annotations

import json
from uuid import UUID

import asyncpg
import pytest
import uuid6

# Fail fast on a network hang. asyncpg.connect does NOT read PGCONNECT_TIMEOUT
# (that is a libpq/psycopg2 variable), so the timeout must be passed explicitly.
# Without this, an unreachable host (e.g. an IPv6-only endpoint on an IPv4
# network) hangs ~60s per connection on asyncpg's default instead of 10s.
_CONNECT_TIMEOUT_S = 10


async def _connect(dsn: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        dsn, statement_cache_size=0, timeout=_CONNECT_TIMEOUT_S
    )


async def _insert_audit_row(conn: asyncpg.Connection) -> UUID:
    row_id = uuid6.uuid7()
    await conn.execute(
        """
        INSERT INTO audit_log (
            id, actor_type, actor_id, event_type, payload
        )
        VALUES ($1::uuid, 'system', 'test:immutability', 'test.probe', $2::jsonb)
        """,
        str(row_id),
        json.dumps({"reason": "test_audit_immutability"}),
    )
    return row_id


async def test_connection_role_is_pdpl_app(app_database_url):
    """Guard the whole file: prove the rejections below are pdpl_app's OWN
    grants, not an artifact of pooler-level role rewriting.

    Supavisor (the Supabase pooler) uses the role embedded in the username
    (``pdpl_app.<project_ref>``) as the real Postgres role, so current_user
    and session_user must both resolve to ``pdpl_app``. If a pooler ever
    rewrote the effective role to something more privileged, the privilege
    tests in this file would be silently testing the wrong role — this
    catches that before the privilege assertions run.
    """
    conn = await _connect(app_database_url)
    try:
        row = await conn.fetchrow("SELECT current_user, session_user")
        assert row["current_user"] == "pdpl_app", row["current_user"]
        assert row["session_user"] == "pdpl_app", row["session_user"]
    finally:
        await conn.close()


async def test_pdpl_app_can_insert_audit_row(app_database_url):
    conn = await _connect(app_database_url)
    try:
        row_id = await _insert_audit_row(conn)
        landed = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE id = $1::uuid", str(row_id)
        )
        assert landed is not None
    finally:
        await conn.close()


async def test_pdpl_app_cannot_update_audit_row(app_database_url):
    conn = await _connect(app_database_url)
    try:
        row_id = await _insert_audit_row(conn)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "UPDATE audit_log SET event_type = 'tampered' WHERE id = $1::uuid",
                str(row_id),
            )
    finally:
        await conn.close()


async def test_pdpl_app_cannot_delete_audit_row(app_database_url):
    conn = await _connect(app_database_url)
    try:
        row_id = await _insert_audit_row(conn)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "DELETE FROM audit_log WHERE id = $1::uuid", str(row_id)
            )
    finally:
        await conn.close()


async def test_pdpl_app_cannot_truncate_audit_log(app_database_url):
    """Even if a future migration re-grants TRUNCATE, the BEFORE TRUNCATE
    trigger from ADR-0003 fails the statement. Today both guards apply:
    pdpl_app has REVOKE TRUNCATE, *and* the trigger would error anyway.
    """
    conn = await _connect(app_database_url)
    try:
        with pytest.raises(
            (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError)
        ):
            await conn.execute("TRUNCATE audit_log")
    finally:
        await conn.close()


async def test_pdpl_app_can_select_audit_log(app_database_url):
    conn = await _connect(app_database_url)
    try:
        rows = await conn.fetch(
            "SELECT id FROM audit_log ORDER BY created_at DESC LIMIT 1"
        )
        # Either there's a row from a prior test or there isn't — the point
        # is SELECT is permitted (no exception raised).
        assert rows is not None
    finally:
        await conn.close()
