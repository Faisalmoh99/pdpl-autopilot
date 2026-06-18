"""Unit tests for the gap-explanation orchestration (ADR-0009 §4).

Pure and offline — they drive `explain_gap` with a `StubExplainer`, no
database / network / model. This file contains the KEYSTONE proof-of-safety
test (ADR-0010 §5): a deliberately-unsafe stub that asserts compliance must be
rejected by the gate, and the orchestration must fall back to the
deterministic `rationale` — proving the safety line is wired and real, not
assumed.
"""

from __future__ import annotations

from pdpl.ai.explainer import GapContext, StubExplainer
from pdpl.explanations import explain_gap

# A real-shaped GapContext (tenant-agnostic — no PII). The rationale is the
# deterministic engine's output, and is exactly what the fallback returns.
_RATIONALE = "privacy notice: 2 of 4 question(s) satisfied; gap(s): purposes, rights"
_CTX = GapContext(
    control_code="PDPL-ART12-PRIVACY-NOTICE",
    control_title_ar="الإشعار بالخصوصية",
    control_description_ar="إشعار يوضّح للعميل أغراض معالجة بياناته وحقوقه.",
    status="partial",
    rationale=_RATIONALE,
    severity_weight=3.0,
    lang="ar",
)

_GOOD_AR = (
    "لا يتوفر لديك إشعار خصوصية يوضّح للعميل أغراض المعالجة وحقوقه. "
    "أضف الإشعار بالخصوصية إلى موقعك كخطوة أولى لمعالجة هذه الثغرة."
)


async def test_keystone_compliance_assertion_is_rejected_and_falls_back() -> None:
    """KEYSTONE / proof-of-safety (ADR-0010 §5).

    The deliberately-unsafe stub asserts compliance («أنت ملتزم …»). The gate
    MUST reject it and the orchestration MUST return the deterministic
    rationale — the unsafe AI text never reaches the caller. If this test ever
    fails, the safety machinery is broken and the build is red.
    """
    explainer = StubExplainer.asserting_compliance()

    result = await explain_gap(_CTX, explainer)

    # The stub WAS called (we really exercised the produce->verify->fallback
    # path, not a short-circuit)...
    assert explainer.calls == [_CTX]
    # ...and yet the caller got the safe deterministic rationale, NOT the
    # unsafe compliance assertion.
    assert result == _RATIONALE
    assert result != explainer.output
    assert "أنت ملتزم" not in result


async def test_verified_explanation_is_returned_unchanged() -> None:
    """The happy path: a good explanation passes the gate and is returned as
    is — proving the gate is permissive to safe, grounded Arabic."""
    explainer = StubExplainer.good(_GOOD_AR)

    result = await explain_gap(_CTX, explainer)

    assert result == _GOOD_AR
    assert explainer.calls == [_CTX]
