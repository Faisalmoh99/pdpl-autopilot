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


async def test_pdpl_app_can_insert_audit_row(app_database_url):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        row_id = await _insert_audit_row(conn)
        landed = await conn.fetchrow(
            "SELECT id FROM audit_log WHERE id = $1::uuid", str(row_id)
        )
        assert landed is not None
    finally:
        await conn.close()


async def test_pdpl_app_cannot_update_audit_row(app_database_url):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
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
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
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
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        with pytest.raises(
            (asyncpg.InsufficientPrivilegeError, asyncpg.RaiseError)
        ):
            await conn.execute("TRUNCATE audit_log")
    finally:
        await conn.close()


async def test_pdpl_app_can_select_audit_log(app_database_url):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            "SELECT id FROM audit_log ORDER BY created_at DESC LIMIT 1"
        )
        # Either there's a row from a prior test or there isn't — the point
        # is SELECT is permitted (no exception raised).
        assert rows is not None
    finally:
        await conn.close()
