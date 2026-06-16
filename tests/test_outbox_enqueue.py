"""Session A — durability of the alert pipeline (ADR-0008).

These tests prove the half that says *the alert can never be lost*:

  1. A worsening transition writes exactly ONE outbox row, with the right
     payload / idempotency key, in the same transaction as the finding.
  2. Atomicity: a crash inside run_check's transaction (after a worsening
     transition + enqueue, before commit) leaves NO orphan finding AND NO
     orphan outbox row — both roll back together.
  3. Trigger policy: a baseline run, an improving transition, and a
     knowledge-loss transition enqueue nothing; only worsening does.
  4. Idempotency at the DB layer: the UNIQUE idempotency_key rejects a
     duplicate enqueue.

The Session B half (worker, webhook send, backoff, dead-letter, secret
hygiene) is not exercised here.

Tests run against the same Supabase project the app uses (tests/conftest.py).
Each test creates its own tenant; by design we leave data behind.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest
import uuid6

from pdpl.db.outbox import idempotency_key_for_finding
from pdpl.services.alerts import is_worsening_transition
from pdpl.services.checks import run_check


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------


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
            f"test_outbox_{label}_{tenant_id}",
        )
    finally:
        await conn.close()
    return tenant_id


async def _fetch_outbox(app_database_url: str, tenant_id: UUID):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetch(
            """
            SELECT id, tenant_id, topic, payload, idempotency_key, status,
                   attempts, next_attempt_at, last_error, sent_at
            FROM outbox
            WHERE tenant_id = $1::uuid
            ORDER BY created_at
            """,
            str(tenant_id),
        )
    finally:
        await conn.close()


async def _current_status_for_code(
    app_database_url: str, tenant_id: UUID, control_code: str
) -> str | None:
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetchval(
            """
            SELECT f.status
            FROM findings f
            JOIN controls c ON c.id = f.control_id
            WHERE f.tenant_id = $1::uuid AND c.code = $2 AND f.valid_to IS NULL
            """,
            str(tenant_id),
            control_code,
        )
    finally:
        await conn.close()


async def _current_codes(app_database_url: str, tenant_id: UUID) -> list[str]:
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            """
            SELECT c.code
            FROM findings f
            JOIN controls c ON c.id = f.control_id
            WHERE f.tenant_id = $1::uuid AND f.valid_to IS NULL
            ORDER BY c.code
            """,
            str(tenant_id),
        )
        return [r["code"] for r in rows]
    finally:
        await conn.close()


def _all_to(status: str):
    def decider(_code: str) -> tuple[str, str]:
        return (status, f"test: forced {status}")

    return decider


def _flip_one(target_code: str, to_status: str, rest: str):
    def decider(code: str) -> tuple[str, str]:
        if code == target_code:
            return (to_status, f"test: forced {to_status}")
        return (rest, "test: unchanged")

    return decider


# ---------------------------------------------------------------------
# Part 1 — the trigger policy, as a pure function (no DB).
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "frm,to,expected",
    [
        ("compliant", "non_compliant", True),
        ("compliant", "partial", True),
        ("partial", "non_compliant", True),
        # Improving — never alerts.
        ("non_compliant", "compliant", False),
        ("partial", "compliant", False),
        ("non_compliant", "partial", False),
        # First assessment — not_assessed is never a source.
        ("not_assessed", "non_compliant", False),
        ("not_assessed", "compliant", False),
        # Knowledge loss — not a verdict worsening.
        ("compliant", "not_assessed", False),
        ("partial", "not_assessed", False),
        # Unranked states never alert.
        ("unknown", "non_compliant", False),
        ("compliant", "not_applicable", False),
        # No-op (run_check never calls us here, but the policy is total).
        ("compliant", "compliant", False),
    ],
)
def test_worsening_policy(frm: str, to: str, expected: bool):
    assert is_worsening_transition(frm, to) is expected


# ---------------------------------------------------------------------
# Part 2 — a worsening transition enqueues exactly one outbox row.
# ---------------------------------------------------------------------


async def test_worsening_transition_enqueues_exactly_one_row(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "worsen")
    codes = []

    # Run #1 — establish a known-good state on every control.
    await run_check(tenant_id, kind="manual", decider=_all_to("compliant"))
    codes = await _current_codes(app_database_url, tenant_id)
    assert codes, "expected seeded controls"
    target_code = codes[0]

    # No alerts from establishing state.
    assert await _fetch_outbox(app_database_url, tenant_id) == []

    # Run #2 — flip exactly one control compliant -> non_compliant (worsening),
    # leave the rest compliant (unchanged, no transition).
    await run_check(
        tenant_id,
        kind="manual",
        decider=_flip_one(target_code, "non_compliant", "compliant"),
    )

    rows = await _fetch_outbox(app_database_url, tenant_id)
    assert len(rows) == 1, "exactly one worsening transition -> one outbox row"
    row = rows[0]

    # Status / scheduling defaults: pending, immediately due, untried.
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["sent_at"] is None
    assert row["last_error"] is None
    assert row["next_attempt_at"] is not None
    assert row["topic"] == "finding.worsened"

    # Payload describes the transition.
    import json

    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["control_code"] == target_code
    assert payload["from_status"] == "compliant"
    assert payload["to_status"] == "non_compliant"
    assert payload["tenant_id"] == str(tenant_id)

    # Idempotency key is derived 1:1 from the new finding row.
    new_finding_id = UUID(payload["finding_id"])
    assert row["idempotency_key"] == idempotency_key_for_finding(new_finding_id)

    # The new finding the alert points at is the current row for the control.
    assert (
        await _current_status_for_code(app_database_url, tenant_id, target_code)
        == "non_compliant"
    )

    # And an alert.enqueued audit row landed atomically with it.
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        n_audit = await conn.fetchval(
            """
            SELECT count(*)::int FROM audit_log
            WHERE tenant_id = $1::uuid
              AND event_type = 'alert.enqueued'
              AND entity_id = $2::uuid
            """,
            str(tenant_id),
            str(row["id"]),
        )
        assert n_audit == 1
    finally:
        await conn.close()


# ---------------------------------------------------------------------
# Part 3 — baseline / improving / knowledge-loss enqueue nothing.
# ---------------------------------------------------------------------


async def test_baseline_run_enqueues_no_alert(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "baseline")
    # Default decider -> every control first-seen as not_assessed (created,
    # not transitioned). Establishing state must not alert.
    await run_check(tenant_id, kind="manual")
    assert await _fetch_outbox(app_database_url, tenant_id) == []


async def test_improving_and_knowledge_loss_enqueue_no_alert(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "improve")
    codes_decider = _all_to("non_compliant")

    # Run #1 — establish non_compliant everywhere.
    await run_check(tenant_id, kind="manual", decider=codes_decider)
    assert await _fetch_outbox(app_database_url, tenant_id) == []

    # Run #2 — improve everything to compliant. No alert.
    await run_check(tenant_id, kind="manual", decider=_all_to("compliant"))
    assert await _fetch_outbox(app_database_url, tenant_id) == []

    # Run #3 — knowledge loss: compliant -> not_assessed. Still no alert.
    await run_check(tenant_id, kind="manual", decider=_all_to("not_assessed"))
    assert await _fetch_outbox(app_database_url, tenant_id) == []


# ---------------------------------------------------------------------
# Part 4 — atomicity: a crash before commit leaves no orphan of either kind.
# ---------------------------------------------------------------------


async def test_crash_before_commit_leaves_no_orphan_finding_or_alert(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "atomic")

    # Run #1 — establish compliant everywhere.
    await run_check(tenant_id, kind="manual", decider=_all_to("compliant"))
    codes = await _current_codes(app_database_url, tenant_id)
    assert len(codes) >= 2, "need at least two controls to fail on the second"
    target_code = codes[0]

    # Run #2 — worsen the FIRST control (compliant -> non_compliant, which
    # would enqueue), then blow up on a LATER control so the whole
    # transaction rolls back. Controls are processed in code order, so the
    # target's transition + enqueue happen in-session BEFORE the explosion.
    def exploding_decider(code: str) -> tuple[str, str]:
        if code == target_code:
            return ("non_compliant", "test: worsening, would enqueue")
        raise RuntimeError("boom: forced failure on a later control")

    with pytest.raises(RuntimeError, match="boom"):
        await run_check(tenant_id, kind="manual", decider=exploding_decider)

    # The finding transition rolled back: target is still compliant.
    assert (
        await _current_status_for_code(app_database_url, tenant_id, target_code)
        == "compliant"
    )
    # And no orphan alert was left behind.
    assert await _fetch_outbox(app_database_url, tenant_id) == []


# ---------------------------------------------------------------------
# Part 5 — DB-layer idempotency: the unique key rejects a duplicate.
# ---------------------------------------------------------------------


async def test_unique_idempotency_key_rejects_duplicate(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "idemp")
    await run_check(tenant_id, kind="manual", decider=_all_to("compliant"))
    codes = await _current_codes(app_database_url, tenant_id)
    target_code = codes[0]
    await run_check(
        tenant_id,
        kind="manual",
        decider=_flip_one(target_code, "non_compliant", "compliant"),
    )

    rows = await _fetch_outbox(app_database_url, tenant_id)
    assert len(rows) == 1
    existing_key = rows[0]["idempotency_key"]

    # A second row with the same idempotency key must be rejected by
    # uniq_outbox_idempotency_key, regardless of application discipline.
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                """
                INSERT INTO outbox (id, tenant_id, topic, payload, idempotency_key)
                VALUES ($1::uuid, $2::uuid, 'finding.worsened', '{}'::jsonb, $3)
                """,
                str(uuid6.uuid7()),
                str(tenant_id),
                existing_key,
            )
    finally:
        await conn.close()
