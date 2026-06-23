"""`build_gap_context` — the pure runtime assembler + the IDENTITY test
(ADR-0011 §1/§5).

Pure and offline: no DB, no network, no model. Two layers:

  1. UNIT — `build_gap_context` joins the engine's structured gap codes to
     their verbatim Arabic prompts (deterministic order), and collapses to ()
     for a no-gap control.

  2. IDENTITY (the retroactive validation) — run the WHOLE runtime construction
     path on each golden case's own inputs:

         source_answers -> build_control_decider -> ControlDecision.unsatisfied_codes
                        -> build_gap_context -> unsatisfied_questions_ar

     and assert it equals the stored golden field VERBATIM. This proves the
     runtime feeds the model EXACTLY the grounding the eval rated — reuse is an
     identity, not an approximation. Crucially it derives the codes from the
     STRUCTURED `ControlDecision` (the C3a source), not a join on pre-stored
     codes; without that step "whole path" would be overclaimed.

NO coupling of eval to runtime: this TEST imports `build_gap_context` (production)
and `load_golden_set` (eval tooling). Contract 5 (`production-no-eval`) forbids
PRODUCTION importing the eval — a test importing both is legal and is how the
two are tied without making the eval a runtime dependency.
"""

from __future__ import annotations

from pdpl.explanations import build_gap_context
from pdpl.eval.golden_set import load_golden_set
from pdpl.services.decision import build_control_decider


def test_build_gap_context_joins_codes_to_arabic_prompts() -> None:
    """The codes are joined to their verbatim Arabic prompts in deterministic
    (control_code, display_order) order — the live catalog join."""
    ctx = build_gap_context(
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        control_title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
        control_description_ar="وصف البند.",
        status="partial",
        rationale="privacy notice: 2 of 4 question(s) satisfied; gap(s): ...",
        severity_weight=7.0,
        # deliberately OUT of display order — the join must reorder
        unsatisfied_codes=("Q-ART12-NOTICE-RIGHTS", "Q-ART12-NOTICE-RECIPIENTS"),
    )
    assert ctx.unsatisfied_questions_ar == (
        "هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟",  # display_order 3
        "هل يبيّن إشعار الخصوصية حقوق صاحب البيانات وكيفية ممارستها؟",  # display_order 4
    )
    assert ctx.control_code == "PDPL-ART12-PRIVACY-NOTICE"
    assert ctx.status == "partial"
    assert ctx.lang == "ar"


def test_build_gap_context_no_codes_yields_empty_questions() -> None:
    """A no-rule / no-gap control carries no codes, so the field collapses to ()
    and the model binds to the control TITLE alone (ADR-0009 §2)."""
    ctx = build_gap_context(
        control_code="PDPL-ART5-LAWFUL-BASIS",
        control_title_ar="الأساس النظامي للمعالجة",
        control_description_ar="وصف البند.",
        status="not_assessed",
        rationale="no deterministic rule registered for this control yet",
        severity_weight=8.0,
    )
    assert ctx.unsatisfied_questions_ar == ()


def test_runtime_construction_is_identity_with_golden_set() -> None:
    """THE IDENTITY TEST (ADR-0011 §5).

    For every golden case, run the FULL runtime path — the real engine to get
    the STRUCTURED `unsatisfied_codes`, then the real `build_gap_context` — and
    require LITERAL equality with the rated golden field. Proves code derivation
    + the catalog join + assembly together match what the eval rated.
    """
    cases = load_golden_set()
    assert cases, "golden set is empty"
    for case in cases:
        decide = build_control_decider(case.source_answers)
        decision = decide(case.gap.control_code)
        ctx = build_gap_context(
            control_code=case.gap.control_code,
            control_title_ar=case.gap.control_title_ar,
            control_description_ar=case.gap.control_description_ar,
            status=decision.status,
            rationale=decision.rationale,
            severity_weight=case.gap.severity_weight,
            unsatisfied_codes=decision.unsatisfied_codes,
            lang="ar",
        )
        assert ctx.unsatisfied_questions_ar == case.gap.unsatisfied_questions_ar, (
            f"{case.id}: runtime construction drifted from the rated golden field"
        )
