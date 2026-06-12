"""Prove ADR-0002 at the APPLICATION layer.

The schema-side proof (partial unique index, valid_to CHECK) ships in
migration 0001. This file converts those invariants from "defined" to
"running" by exercising the check service:

  1. Baseline run: every active control gets a current finding,
     status='not_assessed'.
  2. Status transition: a follow-up run with a non-default decider
     closes the previous current row and opens a new one — atomically,
     with old.valid_to == new.valid_from per Postgres now() semantics
     inside a transaction.
  3. The partial unique index uniq_findings_current rejects an attempt
     to write a second open row for the same (tenant, control).
  4. A baseline re-run does not create duplicate current rows.
  5. The HTTP route POST /tenants/{id}/checks wires up to run_check and
     returns 404 for an unknown tenant.

Tests run against the same Supabase project the app uses (see
tests/conftest.py). Each test creates its own tenant — by design we
leave behind data; audit history and the findings FK RESTRICT mean
we can't (and shouldn't) clean it up.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest
import uuid6

from pdpl.services.checks import TenantNotFound, run_check


async def _create_test_tenant(app_database_url: str, label: str) -> UUID:
    tenant_id = uuid6.uuid7()
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO tenants (id, name, business_type)
            VALUES ($1::uuid, $2, 'saas')
            """,
            str(tenant_id),
            f"test_check_{label}_{tenant_id}",
        )
    finally:
        await conn.close()
    return tenant_id


async def _fetch_current_findings(app_database_url: str, tenant_id: UUID):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetch(
            """
            SELECT f.id, f.control_id, c.code, f.status,
                   f.valid_from, f.valid_to, f.check_run_id
            FROM findings f
            JOIN controls c ON c.id = f.control_id
            WHERE f.tenant_id = $1::uuid AND f.valid_to IS NULL
            ORDER BY c.code
            """,
            str(tenant_id),
        )
    finally:
        await conn.close()


async def _fetch_history_for_control(
    app_database_url: str, tenant_id: UUID, control_code: str
):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetch(
            """
            SELECT f.id, f.status, f.valid_from, f.valid_to, f.check_run_id
            FROM findings f
            JOIN controls c ON c.id = f.control_id
            WHERE f.tenant_id = $1::uuid AND c.code = $2
            ORDER BY f.valid_from
            """,
            str(tenant_id),
            control_code,
        )
    finally:
        await conn.close()


async def _count_active_controls(app_database_url: str) -> int:
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetchval(
            """
            SELECT count(*)::int FROM controls
            WHERE effective_from <= now()
              AND (effective_to IS NULL OR effective_to > now())
            """
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------
# Part 1 — baseline run.
# ---------------------------------------------------------------------


async def test_baseline_run_writes_not_assessed_for_every_active_control(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "baseline")
    n_controls = await _count_active_controls(app_database_url)
    assert n_controls > 0, "controls seed missing — migration 0003 not applied?"

    await run_check(tenant_id, kind="manual")

    rows = await _fetch_current_findings(app_database_url, tenant_id)
    assert len(rows) == n_controls
    assert {r["status"] for r in rows} == {"not_assessed"}
    assert all(r["valid_to"] is None for r in rows)


# ---------------------------------------------------------------------
# Part 2 — SCD Type 2 transition.
# ---------------------------------------------------------------------


async def test_transition_closes_old_and_opens_new_atomically(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "transition")

    # Run #1 — baseline.
    await run_check(tenant_id, kind="manual")
    baseline = await _fetch_current_findings(app_database_url, tenant_id)
    assert len(baseline) > 0
    target_code = baseline[0]["code"]

    # Run #2 — flip exactly one control to 'compliant', keep the rest unchanged.
    def decider(code: str) -> tuple[str, str]:
        if code == target_code:
            return ("compliant", "test: forced compliant to prove SCD Type 2 transition")
        return ("not_assessed", "baseline: unchanged in this run")

    await run_check(tenant_id, kind="manual", decider=decider)

    history = await _fetch_history_for_control(
        app_database_url, tenant_id, target_code
    )
    assert len(history) == 2, "expected exactly one closed row + one current row"
    old, new = history

    # Old row: was the baseline 'not_assessed', now closed.
    assert old["status"] == "not_assessed"
    assert old["valid_to"] is not None

    # New row: the 'compliant' verdict, currently open.
    assert new["status"] == "compliant"
    assert new["valid_to"] is None

    # Different runs produced the two rows.
    assert old["check_run_id"] != new["check_run_id"]

    # Atomic handoff: old.valid_to == new.valid_from (Postgres now() is
    # constant within a transaction). This is the inspectable proof that
    # the close-old + insert-new happened in ONE transaction.
    assert old["valid_to"] == new["valid_from"]

    # Exactly one current row for the changed control across the whole table.
    current = await _fetch_current_findings(app_database_url, tenant_id)
    current_for_target = [r for r in current if r["code"] == target_code]
    assert len(current_for_target) == 1
    assert current_for_target[0]["status"] == "compliant"

    # And exactly one current row per control overall — invariant #1 holds.
    by_control: dict[UUID, int] = {}
    for r in current:
        by_control[r["control_id"]] = by_control.get(r["control_id"], 0) + 1
    assert all(n == 1 for n in by_control.values())


# ---------------------------------------------------------------------
# Part 3 — DB-side invariant: partial unique index rejects duplicates.
# ---------------------------------------------------------------------


async def test_partial_unique_index_rejects_second_current_row(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "uniqindex")
    await run_check(tenant_id, kind="manual")

    rows = await _fetch_current_findings(app_database_url, tenant_id)
    target = rows[0]

    # Try to write a second open row for the same (tenant, control). The
    # partial unique index uniq_findings_current must reject it at the
    # DB layer, regardless of application discipline.
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        check_run_id = await conn.fetchval(
            "SELECT check_run_id FROM findings WHERE id = $1::uuid",
            str(target["id"]),
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO findings (
                    id, tenant_id, control_id, check_run_id,
                    status, rationale
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                    'compliant', 'rogue: should be rejected by uniq_findings_current'
                )
                """,
                str(uuid6.uuid7()),
                str(tenant_id),
                str(target["control_id"]),
                str(check_run_id),
            )
    finally:
        await conn.close()


# ---------------------------------------------------------------------
# Part 4 — baseline re-run is idempotent (dedup ADR-0002 §82).
# ---------------------------------------------------------------------


async def test_baseline_rerun_does_not_create_duplicate_current_rows(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "idempotent")

    await run_check(tenant_id, kind="manual")
    after_first = await _fetch_current_findings(app_database_url, tenant_id)
    n_first = len(after_first)
    ids_first = {r["id"] for r in after_first}

    # Two more runs, same decider, same statuses — no row should change.
    await run_check(tenant_id, kind="manual")
    await run_check(tenant_id, kind="manual")

    after_third = await _fetch_current_findings(app_database_url, tenant_id)
    assert len(after_third) == n_first
    # Identical row identities — proves dedup kicked in, not "delete + reinsert".
    assert {r["id"] for r in after_third} == ids_first


# ---------------------------------------------------------------------
# Part 5 — HTTP route wiring + 404 behaviour.
# ---------------------------------------------------------------------


async def test_post_tenants_id_checks_creates_run_with_correlation_id(
    client, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "http")
    incoming = str(uuid6.uuid7())

    response = await client.post(
        f"/tenants/{tenant_id}/checks",
        headers={"X-Request-ID": incoming},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tenant_id"] == str(tenant_id)
    assert body["kind"] == "manual"
    check_run_id = UUID(body["check_run_id"])
    assert response.headers["x-request-id"] == incoming

    # The check_run row and every audit row emitted by run_check must carry
    # the same correlation_id as the incoming request — the full trace.
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        run_row = await conn.fetchrow(
            "SELECT status, correlation_id FROM check_runs WHERE id = $1::uuid",
            str(check_run_id),
        )
        assert run_row is not None
        assert run_row["status"] == "completed"
        assert run_row["correlation_id"] == UUID(incoming)

        audit_rows = await conn.fetch(
            """
            SELECT event_type, correlation_id
            FROM audit_log
            WHERE correlation_id = $1::uuid
            ORDER BY created_at
            """,
            incoming,
        )
        event_types = [r["event_type"] for r in audit_rows]
        assert "check_run.started" in event_types
        assert "check_run.completed" in event_types
        # Baseline run on a fresh tenant produces 1 finding.created per active control.
        assert event_types.count("finding.created") >= 1
    finally:
        await conn.close()


async def test_post_tenants_id_checks_returns_404_for_unknown_tenant(client):
    bogus = uuid6.uuid7()
    response = await client.post(f"/tenants/{bogus}/checks")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "http_404"


async def test_run_check_raises_tenant_not_found_for_unknown_tenant(
    app, app_database_url
):
    bogus = uuid6.uuid7()
    with pytest.raises(TenantNotFound):
        await run_check(bogus, kind="manual")
