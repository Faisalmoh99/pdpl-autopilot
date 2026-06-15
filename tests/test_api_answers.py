"""POST /tenants/{id}/answers — the HTTP surface over record_answers (ADR-0005).

Thin route, so these tests prove the transport contract, not the engine
(that is tests/test_decision_engine.py):

  * a valid submission writes the expected evidence rows and 201s;
  * re-submitting a changed answer and re-running a check reflects the new
    answer (append + latest-wins, end to end through HTTP);
  * service semantics map to HTTP — bad answer / unknown question -> 422 with
    the project error shape, rolled back (nothing written);
  * unknown tenant -> 404;
  * SHAPE validation lives in the route — empty list and duplicate
    question_code -> 422 before the service is ever called;
  * the request correlation_id threads to the evidence audit rows and back out
    on the response header.

Runs against the same Supabase project as the app (see tests/conftest.py).
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest
import uuid6


# ---------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------
async def _create_tenant_via_api(client, label: str) -> UUID:
    resp = await client.post(
        "/tenants",
        json={"name": f"test_api_answers_{label}", "business_type": "saas"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _count_answer_evidence(app_database_url: str, tenant_id: UUID) -> int:
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetchval(
            """
            SELECT count(*)::int FROM evidence
            WHERE tenant_id = $1::uuid AND type = 'questionnaire_answer'
            """,
            str(tenant_id),
        )
    finally:
        await conn.close()


async def _current_status(app_database_url: str, tenant_id: UUID, code: str):
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        return await conn.fetchval(
            """
            SELECT f.status FROM findings f
            JOIN controls c ON c.id = f.control_id
            WHERE f.tenant_id = $1::uuid AND c.code = $2 AND f.valid_to IS NULL
            """,
            str(tenant_id),
            code,
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------
# Happy path + the round trip through a re-run.
# ---------------------------------------------------------------------
async def test_post_answers_writes_evidence_and_returns_201(client, app_database_url):
    tenant_id = await _create_tenant_via_api(client, "ok")

    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={
            "answers": [
                {"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"},
                {"question_code": "Q-ART4-ACCESS-PROCESS", "answer": "no"},
            ]
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tenant_id"] == str(tenant_id)
    assert body["count"] == 2
    assert len(body["recorded"]) == 2

    assert await _count_answer_evidence(app_database_url, tenant_id) == 2


async def test_post_answers_then_check_reflects_latest_answer(client, app_database_url):
    tenant_id = await _create_tenant_via_api(client, "roundtrip")

    # Answer 'no' -> check -> non_compliant.
    await client.post(
        f"/tenants/{tenant_id}/answers",
        json={"answers": [{"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "no"}]},
    )
    await client.post(f"/tenants/{tenant_id}/checks")
    assert await _current_status(
        app_database_url, tenant_id, "PDPL-ART31-ROPA"
    ) == "non_compliant"

    # Re-submit 'yes' (append, no dedup) -> re-run -> compliant.
    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={"answers": [{"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"}]},
    )
    assert resp.status_code == 201, resp.text
    await client.post(f"/tenants/{tenant_id}/checks")
    assert await _current_status(
        app_database_url, tenant_id, "PDPL-ART31-ROPA"
    ) == "compliant"

    # Both answer rows persist — append-only, no overwrite.
    assert await _count_answer_evidence(app_database_url, tenant_id) == 2


# ---------------------------------------------------------------------
# Service semantics -> HTTP, rolled back.
# ---------------------------------------------------------------------
async def test_post_answers_invalid_answer_value_is_422_and_rolls_back(
    client, app_database_url
):
    tenant_id = await _create_tenant_via_api(client, "badval")

    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={
            "answers": [
                {"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "maybe"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["correlation_id"]  # project error shape
    # Validation is up front -> nothing written.
    assert await _count_answer_evidence(app_database_url, tenant_id) == 0


async def test_post_answers_unknown_question_is_422_and_rolls_back(
    client, app_database_url
):
    tenant_id = await _create_tenant_via_api(client, "badq")

    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={
            "answers": [
                # one valid, one unknown — the whole submission must roll back.
                {"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"},
                {"question_code": "Q-DOES-NOT-EXIST", "answer": "yes"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert await _count_answer_evidence(app_database_url, tenant_id) == 0


async def test_post_answers_unknown_tenant_is_404(client):
    bogus = uuid6.uuid7()
    resp = await client.post(
        f"/tenants/{bogus}/answers",
        json={"answers": [{"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"}]},
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------
# SHAPE validation in the route (before the service is touched).
# ---------------------------------------------------------------------
async def test_post_answers_empty_list_is_422(client):
    tenant_id = await _create_tenant_via_api(client, "empty")
    resp = await client.post(f"/tenants/{tenant_id}/answers", json={"answers": []})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"


async def test_post_answers_duplicate_question_code_is_422(client, app_database_url):
    tenant_id = await _create_tenant_via_api(client, "dupe")
    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={
            "answers": [
                {"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"},
                {"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "no"},
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "validation_error"
    # Rejected before the service -> nothing written.
    assert await _count_answer_evidence(app_database_url, tenant_id) == 0


# ---------------------------------------------------------------------
# Correlation ID threads request -> audit rows -> response header.
# ---------------------------------------------------------------------
async def test_post_answers_threads_correlation_id(client, app_database_url):
    tenant_id = await _create_tenant_via_api(client, "corr")
    incoming = str(uuid6.uuid7())

    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={"answers": [{"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"}]},
        headers={"X-Request-ID": incoming},
    )
    assert resp.status_code == 201, resp.text
    assert resp.headers["x-request-id"] == incoming

    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            """
            SELECT event_type FROM audit_log
            WHERE correlation_id = $1::uuid AND tenant_id = $2::uuid
            """,
            incoming,
            str(tenant_id),
        )
    finally:
        await conn.close()
    assert "evidence.recorded" in [r["event_type"] for r in rows]
