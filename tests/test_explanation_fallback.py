"""The deterministic fallback floor — safe BY CONSTRUCTION (ADR-0011 §4).

Pure and offline. The tests prove the construction holds; they do NOT claim the
fallback passes the gate for every possible input — the guaranteed property is
SAFETY, not gate-pass.

  - AUTHORING-TIME denylist check: every template constant is clean against the
    real `COMPLIANCE_ASSERTIONS` denylist (a static string has no excuse to trip
    the safety-critical check).
  - CONSTRUCTION-QUALITY evidence: the fallback passes the FULL gate on all
    golden cases.
  - SAFETY properties hold on all golden cases (no compliance assertion, Arabic,
    references the control).
  - `not_assessed` asserts NO gap (the C3a #6 fix).
  - The rare no-question gap drops the requirements line (no dangling list).
  - `non_compliant`/`partial` reference the gap (the questions' Arabic text).
"""

from __future__ import annotations

from pdpl.ai.explainer import GapContext
from pdpl.explanations.fallback import (
    _GAP_INTRO,
    _MANUAL_REVIEW,
    _NOT_ASSESSED,
    _REQUIREMENTS_HEADER,
    deterministic_fallback,
)
from pdpl.eval.golden_set import load_golden_set
from pdpl.verification import verify_explanation

# Every user-visible template string the fallback can emit. The three the C4a
# decision turns on — _GAP_INTRO["non_compliant"], _GAP_INTRO["partial"],
# _NOT_ASSESSED — are included via _GAP_INTRO.values() and _NOT_ASSESSED.
_ALL_TEMPLATES = (
    *_GAP_INTRO.values(),
    _REQUIREMENTS_HEADER,
    _NOT_ASSESSED,
    _MANUAL_REVIEW,
)


def test_templates_pass_no_compliance_assertion_check() -> None:
    """AUTHORING-TIME safety on the template STRINGS themselves, via the REAL
    gate (ADR-0010 §2 — test the gate, not a re-implemented copy).

    Each template constant must PASS the gate's own `no_compliance_assertion`
    check. This is the load-bearing static guarantee: a template that tripped the
    denylist would make EVERY fallback for that status SILENTLY fail the gate —
    and the length bound would never catch it (it is a content check, ADR-0011
    §4). The control args are irrelevant to this check; dummies suffice."""
    for template in _ALL_TEMPLATES:
        verdict = verify_explanation(
            template, control_code="PDPL-X", control_title_ar="عنوان"
        )
        assert verdict.no_compliance_assertion.passed, (
            f"template {template!r} trips the no_compliance_assertion check: "
            f"{verdict.no_compliance_assertion.reason}"
        )


def test_fallback_passes_full_gate_on_all_golden_cases() -> None:
    """CONSTRUCTION-QUALITY evidence: the fallback passes the FULL gate on every
    golden case. (Evidence the construction is sound — NOT a claim that every
    possible input passes; length is a content check that can fail, ADR-0011 §4.)"""
    for case in load_golden_set():
        text = deterministic_fallback(case.gap)
        verdict = verify_explanation(
            text,
            control_code=case.gap.control_code,
            control_title_ar=case.gap.control_title_ar,
        )
        assert verdict.passed, f"{case.id}: fallback failed the gate: {verdict.checks}"


def test_fallback_safety_properties_hold_on_all_golden_cases() -> None:
    """The three checkable safety properties hold on every golden case
    (tenant-agnostic is structural — the fallback only reads control text)."""
    for case in load_golden_set():
        text = deterministic_fallback(case.gap)
        verdict = verify_explanation(
            text,
            control_code=case.gap.control_code,
            control_title_ar=case.gap.control_title_ar,
        )
        assert verdict.no_compliance_assertion.passed, f"{case.id}: asserts compliance"
        assert verdict.arabic.passed, f"{case.id}: not Arabic"
        assert verdict.references_control.passed, f"{case.id}: does not reference control"


def test_not_assessed_asserts_no_gap() -> None:
    """The C3a #6 fix: a not_assessed fallback states the control was not checked
    and explicitly does NOT assert a deficiency — it references the control but
    names no gap, no requirements, no shortfall."""
    ctx = GapContext(
        control_code="PDPL-ART5-LAWFUL-BASIS",
        control_title_ar="الأساس النظامي للمعالجة",
        control_description_ar="وصف البند.",
        status="not_assessed",
        rationale="no deterministic rule registered for this control yet",
        severity_weight=8.0,
    )
    text = deterministic_fallback(ctx)
    assert ctx.control_title_ar in text  # references the control
    assert "لم يُقيَّم" in text and "مراجعة" in text  # says: not assessed, needs review
    # asserts NO gap: none of the gap-naming language appears
    assert _REQUIREMENTS_HEADER not in text
    for intro in _GAP_INTRO.values():
        assert intro not in text


def test_no_question_gap_drops_the_requirements_line() -> None:
    """The rare rule-bearing gap with no unsatisfied questions: the requirements
    header and list are dropped entirely — no dangling empty list."""
    ctx = GapContext(
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        control_title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
        control_description_ar="وصف البند.",
        status="non_compliant",
        rationale="privacy notice: none of 4 question(s) satisfied",
        severity_weight=7.0,
        unsatisfied_questions_ar=(),  # the rare () case
    )
    text = deterministic_fallback(ctx)
    assert _REQUIREMENTS_HEADER not in text
    assert "\n-" not in text  # no dangling bullet
    assert _GAP_INTRO["non_compliant"] in text  # intro stands alone, complete
    assert _MANUAL_REVIEW in text


def test_gap_fallback_references_the_unsatisfied_questions() -> None:
    """non_compliant/partial render the unsatisfied questions' Arabic text so the
    floor honestly points at the gap that genuinely exists."""
    ctx = GapContext(
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        control_title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
        control_description_ar="وصف البند.",
        status="partial",
        rationale="privacy notice: 2 of 4 question(s) satisfied; gap(s): ...",
        severity_weight=7.0,
        unsatisfied_questions_ar=(
            "هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟",
        ),
    )
    text = deterministic_fallback(ctx)
    assert _REQUIREMENTS_HEADER in text
    assert "هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟" in text
    assert _GAP_INTRO["partial"] in text
