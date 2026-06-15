"""GET /tenants/{id}/readiness — the read-only readiness report (ADR-0007).

Thin route over scoring.readiness_report. These tests prove the HTTP contract
and that the response honours ADR-0007:

  * a tenant with mixed statuses gets the right weighted readiness_score,
    coverage_pct, counts, and a gap list ordered by severity with both titles;
  * the GET does NOT run a check — before any check the report is the honest
    zero-case: readiness_score null (never 0/100), coverage 0%, every
    applicable control listed as a not_assessed gap (falls out of the LEFT
    JOIN);
  * no field is named compliance_score; readiness_score is explicitly nullable;
  * unknown tenant -> 404;
  * the request correlation_id comes back on the response header.

Runs against the same Supabase project as the app (see tests/conftest.py).
"""

from __future__ import annotations

from uuid import UUID

import uuid6


async def _create_tenant_via_api(client, label: str) -> UUID:
    resp = await client.post(
        "/tenants",
        json={"name": f"test_api_readiness_{label}", "business_type": "saas"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


# ---------------------------------------------------------------------
# Mixed statuses -> the worked example, over HTTP.
# ---------------------------------------------------------------------
async def test_readiness_reports_score_coverage_and_ordered_gaps(client):
    tenant_id = await _create_tenant_via_api(client, "mixed")

    # ROPA (w=5) compliant, DSR-ACCESS (w=7) non_compliant,
    # PRIVACY-NOTICE (w=7) partial; the rest unanswered -> not_assessed.
    await client.post(
        f"/tenants/{tenant_id}/answers",
        json={
            "answers": [
                {"question_code": "Q-ART31-ROPA-MAINTAINED", "answer": "yes"},
                {"question_code": "Q-ART4-ACCESS-PROCESS", "answer": "no"},
                {"question_code": "Q-ART4-ACCESS-TIMEFRAME", "answer": "no"},
                {"question_code": "Q-ART12-NOTICE-EXISTS", "answer": "yes"},
                {"question_code": "Q-ART12-NOTICE-PURPOSES", "answer": "yes"},
                {"question_code": "Q-ART12-NOTICE-RECIPIENTS", "answer": "no"},
                {"question_code": "Q-ART12-NOTICE-RIGHTS", "answer": "no"},
            ]
        },
    )
    await client.post(f"/tenants/{tenant_id}/checks")

    resp = await client.get(f"/tenants/{tenant_id}/readiness")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # (5*1.0 + 7*0.0 + 7*0.5) / 19 * 100 = 44.74 ; coverage 3/10.
    assert body["readiness_score"] == 44.74
    assert body["coverage_pct"] == 30.0
    assert body["assessed_controls"] == 3
    assert body["applicable_controls"] == 10
    assert body["counts"]["compliant"] == 1
    assert body["counts"]["partial"] == 1
    assert body["counts"]["non_compliant"] == 1
    assert body["counts"]["not_assessed"] == 7

    # No compliance-framed field; the number is a readiness indicator.
    assert "compliance_score" not in body

    gaps = body["gaps"]
    # compliant ROPA is NOT a gap; the 7 not_assessed + 2 answered gaps are.
    codes = [g["control_code"] for g in gaps]
    assert "PDPL-ART31-ROPA" not in codes
    assert "PDPL-ART4-DSR-ACCESS" in codes
    assert "PDPL-ART12-PRIVACY-NOTICE" in codes

    # Ordered by severity DESC; each gap carries both titles + status + rationale.
    weights = [g["severity_weight"] for g in gaps]
    assert weights == sorted(weights, reverse=True)
    assert all(g["title_en"] and g["title_ar"] and g["rationale"] for g in gaps)


# ---------------------------------------------------------------------
# Zero-case: no check has run -> null score, 0% coverage, all not_assessed.
# ---------------------------------------------------------------------
async def test_readiness_zero_case_is_null_score_not_zero_or_hundred(client):
    tenant_id = await _create_tenant_via_api(client, "fresh")

    resp = await client.get(f"/tenants/{tenant_id}/readiness")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Honest absence — NOT 0 (looks failing) and NOT 100 (looks perfect).
    assert body["readiness_score"] is None
    assert body["coverage_pct"] == 0.0
    assert body["assessed_controls"] == 0
    assert body["applicable_controls"] == 10

    # Every applicable control surfaces as a not_assessed gap (LEFT JOIN).
    gaps = body["gaps"]
    assert len(gaps) == 10
    assert all(g["status"] == "not_assessed" for g in gaps)


async def test_readiness_does_not_mutate_state(client):
    # A GET must not create a check_run; the zero-case stays the zero-case
    # across repeated reads.
    tenant_id = await _create_tenant_via_api(client, "nomutate")
    first = (await client.get(f"/tenants/{tenant_id}/readiness")).json()
    second = (await client.get(f"/tenants/{tenant_id}/readiness")).json()
    assert first == second
    assert first["readiness_score"] is None


async def test_readiness_unknown_tenant_is_404(client):
    bogus = uuid6.uuid7()
    resp = await client.get(f"/tenants/{bogus}/readiness")
    assert resp.status_code == 404, resp.text


async def test_readiness_threads_correlation_id_on_response_header(client):
    tenant_id = await _create_tenant_via_api(client, "corr")
    incoming = str(uuid6.uuid7())
    resp = await client.get(
        f"/tenants/{tenant_id}/readiness",
        headers={"X-Request-ID": incoming},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-request-id"] == incoming
