"""Integration tests for the runtime orchestration `explain_gap`
(ADR-0011 §2) — against the same Supabase project as the C3b cache tests.

`explain_gap` is now cache-aware, so it needs a real DB session (the cache is
integral to the runtime path). Each test uses a UNIQUE GapContext (a nonce in
the rationale) so cache rows never collide across runs.

THE KEYSTONE (ADR-0010 §5) is surfaced here on BOTH paths the gate now guards:
  - FRESH: a deliberately-unsafe explainer asserts compliance -> the gate
    rejects -> fallback. The unsafe AI text never reaches the caller.
  - RE-GATED HIT: a poisoned row injected directly into the cache (bypassing the
    gate, as `put` does not verify) -> the read re-gates -> rejects ->
    fallback, reason=cache_regate_failed. A poisoned cache row is never served.
If either ever fails, the safety machinery is broken and the build is red.
"""

from __future__ import annotations

import uuid

from pdpl.ai.explainer import (
    ExplainerError,
    GapContext,
    StubExplainer,
    TransientExplainerError,
)
from pdpl.ai.prompt import PROMPT_VERSION
from pdpl.db.ai_explanations import compute_cache_key, put
from pdpl.db.session import session_scope
from pdpl.explanations import explain_gap
from pdpl.explanations.fallback import deterministic_fallback

_MODEL = "gemini-2.5-flash"

# A known-good Arabic explanation that passes the gate (references the control,
# Arabic, within bounds, no compliance assertion).
_GOOD_AR = (
    "لا يتوفر لديك إشعار خصوصية يوضّح للعميل أغراض المعالجة وحقوقه. "
    "أضف الإشعار بالخصوصية إلى موقعك كخطوة أولى لمعالجة هذه الثغرة."
)

# A bald compliance assertion — the gate MUST reject it on either path.
_UNSAFE = "أنت ملتزم بالنظام بشكل كامل ولا توجد أي ثغرات لديك."


def _ctx() -> GapContext:
    """A unique, real-shaped, tenant-agnostic GapContext (the nonce in the
    rationale guarantees an uncached cache key per test)."""
    nonce = uuid.uuid4().hex
    return GapContext(
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        control_title_ar="الإشعار بالخصوصية",
        control_description_ar="إشعار يوضّح للعميل أغراض معالجة بياناته وحقوقه.",
        status="partial",
        rationale=f"privacy notice: 2 of 4 question(s) satisfied; gap(s): [{nonce}]",
        severity_weight=3.0,
        unsatisfied_questions_ar=(
            "هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟",
        ),
        lang="ar",
    )


def _key_for(ctx: GapContext) -> str:
    return compute_cache_key(
        prompt_version=PROMPT_VERSION,
        model=_MODEL,
        control_code=ctx.control_code,
        status=ctx.status,
        rationale=ctx.rationale,
        lang=ctx.lang,
    )


class _RaisingExplainer:
    """An Explainer (structurally) that always raises — models the real Gemini
    call failing (timeout / 5xx / truncation) after exhausted retries."""

    def __init__(self) -> None:
        self.calls: list[GapContext] = []

    async def explain(self, ctx: GapContext) -> str:
        self.calls.append(ctx)
        raise TransientExplainerError("simulated exhausted retries")


# ---------------------------------------------------------------------
# KEYSTONE — fresh path.
# ---------------------------------------------------------------------
async def test_keystone_fresh_compliance_assertion_is_rejected(app) -> None:
    """KEYSTONE (fresh): an unsafe compliance assertion from the model is
    rejected by the gate and replaced by the deterministic floor."""
    ctx = _ctx()
    explainer = StubExplainer.asserting_compliance()

    async with session_scope() as session:
        result = await explain_gap(session, ctx, explainer, model=_MODEL)

    assert explainer.calls == [ctx]  # we really ran produce -> verify -> fallback
    assert result.source == "fallback"
    assert result.reason == "gate_rejected"
    assert result.text == deterministic_fallback(ctx)
    assert "أنت ملتزم" not in result.text


# ---------------------------------------------------------------------
# KEYSTONE — re-gated hit path (the C4a re-gate-on-read proof).
# ---------------------------------------------------------------------
async def test_keystone_poisoned_cache_row_is_regated_and_rejected(app) -> None:
    """KEYSTONE (re-gated hit): a poisoned row injected directly into the cache
    is RE-GATED on read, rejected, and replaced by the fallback —
    reason=cache_regate_failed. The poisoned text is never served, and the
    explainer is never called (it was a hit)."""
    ctx = _ctx()
    key = _key_for(ctx)
    # Inject the unsafe text directly — put does not verify (verified-only is the
    # orchestrator's contract, not the cache's).
    async with session_scope() as session:
        await put(
            session, key, text=_UNSAFE, lang="ar",
            prompt_version=PROMPT_VERSION, model=_MODEL,
        )

    explainer = StubExplainer.good(_GOOD_AR)
    async with session_scope() as session:
        result = await explain_gap(session, ctx, explainer, model=_MODEL)

    assert explainer.calls == []  # a hit — the model was not called
    assert result.source == "fallback"
    assert result.reason == "cache_regate_failed"
    assert result.text == deterministic_fallback(ctx)
    assert "أنت ملتزم" not in result.text


# ---------------------------------------------------------------------
# Happy path + the cache miss -> put -> hit round trip.
# ---------------------------------------------------------------------
async def test_verified_output_is_returned_and_cached_then_hit(app) -> None:
    """A miss: the model's good output passes the gate, is cached, and returned
    as ai_verified. A second call for the SAME gap is a cache_hit served from
    the row just written — the explainer is not called again."""
    ctx = _ctx()

    explainer = StubExplainer.good(_GOOD_AR)
    async with session_scope() as session:
        first = await explain_gap(session, ctx, explainer, model=_MODEL)

    assert first.source == "ai_verified"
    assert first.text == _GOOD_AR
    assert first.reason is None
    assert first.model_version is None  # populated in C4b (modelVersion capture)
    assert explainer.calls == [ctx]

    # Second call: a different explainer that must NOT be called (cache hit).
    second_explainer = StubExplainer.good("نص مختلف يجب ألا يُستخدم لأن الكاش يخدم.")
    async with session_scope() as session:
        second = await explain_gap(session, ctx, second_explainer, model=_MODEL)

    assert second.source == "cache_hit"
    assert second.text == _GOOD_AR  # served the first verified text, not the new one
    assert second_explainer.calls == []  # the model was not called on a hit


# ---------------------------------------------------------------------
# Explainer failure -> fallback (a failed call is never a failed request).
# ---------------------------------------------------------------------
async def test_explainer_error_falls_back(app) -> None:
    """When the explainer raises (exhausted retries / truncation / block), the
    orchestration falls back to the deterministic floor — never propagates the
    error, never caches anything."""
    ctx = _ctx()
    explainer = _RaisingExplainer()

    async with session_scope() as session:
        result = await explain_gap(session, ctx, explainer, model=_MODEL)

    assert explainer.calls == [ctx]
    assert result.source == "fallback"
    assert result.reason == "explainer_error"
    assert result.text == deterministic_fallback(ctx)


def test_explainer_error_is_an_explainer_error_subclass() -> None:
    """Guard: the raising stub raises the real taxonomy the orchestrator catches
    (so the except clause genuinely covers the real failure mode)."""
    assert issubclass(TransientExplainerError, ExplainerError)
