"""POST /tenants/{id}/explanations — the on-demand explanation surface (ADR-0012).

Two layers, mirroring the project's pattern (transport over HTTP; AI behaviour
via the injected-explainer service seam, like `run_check(decider=...)`):

  * INTEGRATION (against the real session, C4a-style) — exercises
    `explain_tenant_gap` directly with a `StubExplainer`, so the whole runtime
    path runs (re-derive -> build_gap_context -> explain_gap -> cache) against
    the real DB without needing a GEMINI_API_KEY:
      - miss -> verified AI text is returned AND written to the cache;
      - the second call is a CACHE HIT, served from the cache and RE-GATED
        (the explainer is never called again);
      - the KEYSTONE end-to-end: a poisoned cache row (unsafe text inserted via
        the low-level repo, bypassing the orchestrator's gate-before-put) is
        RE-GATED on read and replaced by the fallback — the poison is NEVER
        served — proving the endpoint layer does not bypass the chokepoint;
      - a compliance-asserting explainer is rejected to the fallback and is
        NEVER cached (a following good call produces fresh ai_verified).

  * TRANSPORT (over HTTP) — unknown tenant / unknown control -> 404, missing
    body -> 422, and the correlation id is echoed. These short-circuit before
    the explainer is constructed, so they need no GEMINI_API_KEY either.

Runs against the same Supabase project as the app (see tests/conftest.py).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import uuid6

from pdpl.ai.explainer import StubExplainer
from pdpl.api.explanations import explain_tenant_gap
from pdpl.catalog import control_by_code
from pdpl.config import get_settings
from pdpl.db.ai_explanations import compute_cache_key, put
from pdpl.db.session import session_scope
from pdpl.services.decision import build_control_decider


async def _create_tenant_via_api(client, label: str) -> UUID:
    resp = await client.post(
        "/tenants",
        json={"name": f"test_api_explanations_{label}", "business_type": "saas"},
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _post_answers(client, tenant_id: UUID, answers: dict[str, str]) -> None:
    resp = await client.post(
        f"/tenants/{tenant_id}/answers",
        json={
            "answers": [
                {"question_code": qc, "answer": a} for qc, a in answers.items()
            ]
        },
    )
    assert resp.status_code == 201, resp.text


def _good_explanation_for(control_code: str) -> str:
    """A known-good Arabic explanation that passes the full gate: it references
    the control (by its Arabic title), is Arabic, within length bounds, and
    asserts no compliance."""
    title = control_by_code(control_code).title_ar
    return (
        f"بخصوص {title}: يوجد نقص في الالتزام يحتاج إلى مراجعة ومعالجة من قبلكم "
        "لاستيفاء المتطلبات النظامية."
    )


# ---------------------------------------------------------------------
# INTEGRATION — miss -> ai_verified + cache write, then a re-gated cache hit.
# ---------------------------------------------------------------------
async def test_miss_returns_ai_verified_then_hit_is_served_and_regated(client):
    tenant_id = await _create_tenant_via_api(client, "hit")
    code = "PDPL-ART12-PRIVACY-NOTICE"
    # All four notice questions 'no' -> non_compliant (a real gap to explain).
    await _post_answers(
        client,
        tenant_id,
        {
            "Q-ART12-NOTICE-EXISTS": "no",
            "Q-ART12-NOTICE-PURPOSES": "no",
            "Q-ART12-NOTICE-RECIPIENTS": "no",
            "Q-ART12-NOTICE-RIGHTS": "no",
        },
    )

    # A unique prompt_version yields a fresh cache key -> a deterministic MISS,
    # regardless of what prior runs left in the tenant-agnostic persistent cache.
    pv = f"test-{uuid4()}"

    good = _good_explanation_for(code)
    stub = StubExplainer.good(good)
    first = await explain_tenant_gap(tenant_id, code, explainer=stub, prompt_version=pv)

    assert first.source == "ai_verified"
    assert first.text == good
    assert len(stub.calls) == 1  # the model was called on the miss

    # Second call (same prompt_version): a DIFFERENT stub proves the result is
    # served from the cache, not re-generated — and the explainer is never
    # invoked on a hit.
    stub2 = StubExplainer.good("نص مختلف تماماً لإثبات أنه يُخدم من الكاش.")
    second = await explain_tenant_gap(
        tenant_id, code, explainer=stub2, prompt_version=pv
    )

    assert second.source == "cache_hit"
    assert second.text == good  # the cached (re-gated) text, not stub2's
    assert len(stub2.calls) == 0  # cache hit -> explainer not called


# ---------------------------------------------------------------------
# INTEGRATION — the KEYSTONE: a poisoned cache row is re-gated on read.
# ---------------------------------------------------------------------
async def test_poisoned_cache_row_is_regated_and_never_served(client):
    tenant_id = await _create_tenant_via_api(client, "poison")
    code = "PDPL-ART4-DSR-ACCESS"
    answers = {
        "Q-ART4-ACCESS-PROCESS": "no",
        "Q-ART4-ACCESS-TIMEFRAME": "no",
    }
    await _post_answers(client, tenant_id, answers)

    # A unique prompt_version isolates this row from prior runs' cache.
    pv = f"test-{uuid4()}"

    # Reproduce the exact cache key the endpoint will compute: the re-derived
    # status + rationale for these answers (same engine path the endpoint runs).
    settings = get_settings()
    decision = build_control_decider(answers)(code)
    key = compute_cache_key(
        prompt_version=pv,
        model=settings.gemini_model,
        control_code=code,
        status=decision.status,
        rationale=decision.rationale,
        lang="ar",
    )

    # Poison the cache: write UNSAFE text (a compliance assertion) directly via
    # the low-level repo, which performs no verification — simulating a row that
    # reached the cache by some path OTHER than the gate-before-put orchestrator.
    poison = StubExplainer._COMPLIANCE_ASSERTION
    async with session_scope() as session:
        await put(
            session,
            key,
            text=poison,
            lang="ar",
            prompt_version=pv,
            model=settings.gemini_model,
        )

    # The endpoint finds the poison (a hit), RE-GATES it, the gate fails, and the
    # fallback replaces it. The poison is never shown; the explainer is never
    # reached (it was a hit, not a miss).
    stub = StubExplainer.good(_good_explanation_for(code))
    result = await explain_tenant_gap(
        tenant_id, code, explainer=stub, prompt_version=pv
    )

    assert result.source == "fallback"
    assert result.reason == "cache_regate_failed"
    assert result.text != poison
    assert len(stub.calls) == 0


# ---------------------------------------------------------------------
# INTEGRATION — a compliance assertion is rejected and NOT cached.
# ---------------------------------------------------------------------
async def test_compliance_assertion_falls_back_and_is_not_cached(client):
    tenant_id = await _create_tenant_via_api(client, "reject")
    code = "PDPL-ART20-BREACH-NOTIFY-72H"
    await _post_answers(
        client,
        tenant_id,
        {
            "Q-ART20-BREACH-PROCEDURE": "no",
            "Q-ART20-BREACH-72H": "no",
        },
    )

    pv = f"test-{uuid4()}"

    bad = StubExplainer.asserting_compliance()
    first = await explain_tenant_gap(tenant_id, code, explainer=bad, prompt_version=pv)
    assert first.source == "fallback"
    assert first.reason == "gate_rejected"

    # The rejected text was never cached: a following good call generates fresh
    # ai_verified output (it would be a cache_hit if the bad text had been put).
    good = _good_explanation_for(code)
    stub_good = StubExplainer.good(good)
    second = await explain_tenant_gap(
        tenant_id, code, explainer=stub_good, prompt_version=pv
    )
    assert second.source == "ai_verified"
    assert second.text == good
    assert len(stub_good.calls) == 1


# ---------------------------------------------------------------------
# TRANSPORT (HTTP) — these short-circuit before the explainer is built.
# ---------------------------------------------------------------------
async def test_unknown_tenant_returns_404(client):
    resp = await client.post(
        f"/tenants/{uuid6.uuid7()}/explanations",
        json={"control_code": "PDPL-ART12-PRIVACY-NOTICE"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == "tenant not found"


async def test_unknown_control_returns_404(client):
    tenant_id = await _create_tenant_via_api(client, "badcontrol")
    resp = await client.post(
        f"/tenants/{tenant_id}/explanations",
        json={"control_code": "PDPL-ART999-DOES-NOT-EXIST"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["message"] == "control not found"


async def test_missing_control_code_returns_422(client):
    tenant_id = await _create_tenant_via_api(client, "badbody")
    resp = await client.post(
        f"/tenants/{tenant_id}/explanations",
        json={},
    )
    assert resp.status_code == 422, resp.text


async def test_correlation_id_is_echoed(client):
    # A bad control short-circuits to 404 without needing the model, but the
    # request still flows through the correlation middleware.
    resp = await client.post(
        f"/tenants/{uuid4()}/explanations",
        json={"control_code": "PDPL-ART999-DOES-NOT-EXIST"},
    )
    assert resp.headers.get("X-Request-ID")
