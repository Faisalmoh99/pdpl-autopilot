"""Prove the FIRST real compliance decision — ADR-0005 + ADR-0006.

Until now the check service proved its SCD Type 2 mechanics with a stub
decider that returned 'not_assessed' for everything. This file proves the
product's core job for the first time: real input about a tenant (yes/no
questionnaire answers, recorded as evidence) turns into a REAL finding
(compliant / non_compliant / partial) via a 100% deterministic engine —
no AI in the path, no injected stub.

  1. Answers satisfying a control       -> 'compliant'   (+ deterministic rationale)
  2. Answers failing a control          -> 'non_compliant'
  3. Mixed answers (multi-question)     -> 'partial'
  4. Missing answers (rule exists)      -> 'not_assessed'
  5. The payoff: change an answer, re-run with the REAL default engine ->
     the control transitions, the old finding closes and a new one opens
     (SCD Type 2), driven by a real cause, not an injected decider.
  6. audit_log + correlation_id thread through record_answers AND run_check.
  7. record_answers validates input (unknown question, bad answer) and
     writes nothing on failure.

Runs against the same Supabase project as the app (see tests/conftest.py).
Each test creates its own tenant; we leave data behind by design.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest
import uuid6

from pdpl.services.answers import (
    InvalidAnswer,
    UnknownQuestion,
    record_answers,
)
from pdpl.services.checks import run_check


# ---------------------------------------------------------------------
# Helpers (mirrors tests/test_check_runs.py — each opens its own conn).
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
            f"test_decision_{label}_{tenant_id}",
        )
    finally:
        await conn.close()
    return tenant_id


async def _current_finding_for_control(
    app_database_url: str, tenant_id: UUID, control_code: str
):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetchrow(
            """
            SELECT f.id, f.status, f.rationale, f.valid_from, f.valid_to,
                   f.check_run_id
            FROM findings f
            JOIN controls c ON c.id = f.control_id
            WHERE f.tenant_id = $1::uuid AND c.code = $2 AND f.valid_to IS NULL
            """,
            str(tenant_id),
            control_code,
        )
    finally:
        await conn.close()


async def _history_for_control(
    app_database_url: str, tenant_id: UUID, control_code: str
):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetch(
            """
            SELECT f.id, f.status, f.rationale, f.valid_from, f.valid_to,
                   f.check_run_id
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


async def _count_evidence(app_database_url: str, tenant_id: UUID) -> int:
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetchval(
            "SELECT count(*)::int FROM evidence WHERE tenant_id = $1::uuid",
            str(tenant_id),
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------
# 1–4: the four verdicts, each from real answers via the REAL default engine.
# ---------------------------------------------------------------------
async def test_satisfying_answers_yield_compliant(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "compliant")

    # ROPA is a single-question control: 'yes' => fully satisfied.
    await record_answers(tenant_id, {"Q-ART31-ROPA-MAINTAINED": "yes"})
    await run_check(tenant_id, kind="manual")  # no decider -> real engine

    row = await _current_finding_for_control(
        app_database_url, tenant_id, "PDPL-ART31-ROPA"
    )
    assert row is not None
    assert row["status"] == "compliant"
    # Deterministic rationale — NOT an AI explanation.
    assert "satisfied" in row["rationale"]


async def test_failing_answers_yield_non_compliant(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "noncompliant")

    await record_answers(tenant_id, {"Q-ART31-ROPA-MAINTAINED": "no"})
    await run_check(tenant_id, kind="manual")

    row = await _current_finding_for_control(
        app_database_url, tenant_id, "PDPL-ART31-ROPA"
    )
    assert row is not None
    assert row["status"] == "non_compliant"


async def test_mixed_answers_yield_partial(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "partial")

    # Privacy notice has 4 questions; 2 yes / 2 no must produce 'partial'.
    await record_answers(
        tenant_id,
        {
            "Q-ART12-NOTICE-EXISTS": "yes",
            "Q-ART12-NOTICE-PURPOSES": "yes",
            "Q-ART12-NOTICE-RECIPIENTS": "no",
            "Q-ART12-NOTICE-RIGHTS": "no",
        },
    )
    await run_check(tenant_id, kind="manual")

    row = await _current_finding_for_control(
        app_database_url, tenant_id, "PDPL-ART12-PRIVACY-NOTICE"
    )
    assert row is not None
    assert row["status"] == "partial"
    assert "2 of 4" in row["rationale"]


async def test_missing_answers_yield_not_assessed(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "notassessed")

    # DSR-ACCESS needs TWO answers; answer only one -> required question
    # missing -> not_assessed (distinct from non_compliant).
    await record_answers(tenant_id, {"Q-ART4-ACCESS-PROCESS": "yes"})
    await run_check(tenant_id, kind="manual")

    row = await _current_finding_for_control(
        app_database_url, tenant_id, "PDPL-ART4-DSR-ACCESS"
    )
    assert row is not None
    assert row["status"] == "not_assessed"
    assert "unanswered" in row["rationale"]


# ---------------------------------------------------------------------
# 5: the payoff — change an answer, re-run, watch SCD Type 2 fire from a
#    REAL cause (the changed answer), with the default engine, no stub.
# ---------------------------------------------------------------------
async def test_changing_an_answer_transitions_the_finding(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "payoff")

    # Answer 'no' -> first check -> non_compliant.
    await record_answers(tenant_id, {"Q-ART31-ROPA-MAINTAINED": "no"})
    await run_check(tenant_id, kind="manual")

    first = await _current_finding_for_control(
        app_database_url, tenant_id, "PDPL-ART31-ROPA"
    )
    assert first["status"] == "non_compliant"

    # The tenant fixes it and changes the answer to 'yes'. Append-only:
    # this writes a NEW evidence row; nothing is overwritten.
    await record_answers(tenant_id, {"Q-ART31-ROPA-MAINTAINED": "yes"})

    # Re-run with the REAL default engine — the transition is driven by the
    # changed answer, not an injected decider.
    await run_check(tenant_id, kind="manual")

    history = await _history_for_control(
        app_database_url, tenant_id, "PDPL-ART31-ROPA"
    )
    assert len(history) == 2, "expected one closed row + one current row"
    old, new = history

    # Old row: the non_compliant verdict, now closed.
    assert old["status"] == "non_compliant"
    assert old["valid_to"] is not None

    # New row: the compliant verdict, currently open, real rationale.
    assert new["status"] == "compliant"
    assert new["valid_to"] is None
    assert "satisfied" in new["rationale"]

    # Different runs produced the two rows.
    assert old["check_run_id"] != new["check_run_id"]

    # Atomic handoff: old.valid_to == new.valid_from (one transaction).
    assert old["valid_to"] == new["valid_from"]

    # Two answer rows now exist for this tenant (append-only, not overwrite).
    assert await _count_evidence(app_database_url, tenant_id) == 2


# ---------------------------------------------------------------------
# 6: observability — one correlation_id threads answers + check + findings.
# ---------------------------------------------------------------------
async def test_correlation_id_threads_answers_and_check(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "corr")
    cid = uuid6.uuid7()

    await record_answers(
        tenant_id, {"Q-ART31-ROPA-MAINTAINED": "yes"}, correlation_id=cid
    )
    await run_check(tenant_id, kind="manual", correlation_id=cid)

    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            """
            SELECT event_type FROM audit_log
            WHERE correlation_id = $1::uuid
            ORDER BY created_at
            """,
            str(cid),
        )
    finally:
        await conn.close()

    event_types = [r["event_type"] for r in rows]
    assert "evidence.recorded" in event_types
    assert "check_run.started" in event_types
    assert "check_run.completed" in event_types
    assert "finding.created" in event_types


# ---------------------------------------------------------------------
# 7: record_answers validation — fails loud, writes nothing.
# ---------------------------------------------------------------------
async def test_record_answers_rejects_unknown_question(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "badq")

    with pytest.raises(UnknownQuestion):
        await record_answers(tenant_id, {"Q-DOES-NOT-EXIST": "yes"})

    # Nothing written — the transaction rolled back.
    assert await _count_evidence(app_database_url, tenant_id) == 0


async def test_record_answers_rejects_bad_answer_value(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "badans")

    with pytest.raises(InvalidAnswer):
        await record_answers(tenant_id, {"Q-ART31-ROPA-MAINTAINED": "maybe"})

    assert await _count_evidence(app_database_url, tenant_id) == 0
