"""Golden-set integrity tests (ADR-0010 §4).

Pure and offline — no database, no network. They guard the corpus itself:

  - FAITHFULNESS (the key one): every case's `status` and `rationale` are
    REAL deterministic-engine output, not hand-typed lookalikes. The test
    regenerates them by running the case's `source_answers` through the actual
    `pdpl.services.decision.build_deterministic_decider` — the same code path
    `run_check` uses — and asserts an exact match. If the engine's rationale
    wording ever changes, this fails and the corpus must be regenerated, so the
    eval can never drift onto stale fixtures.
  - STRUCTURE: the set is the agreed size and shape, covers the gap statuses,
    and the human expectation fields exist (empty now, filled in C3).

`build_deterministic_decider` is a pure function (no I/O), so importing the
decision core here needs no database — and the import is legal: the
`.importlinter` fence forbids PRODUCTION from importing the eval, not a test
from importing the decision core.
"""

from __future__ import annotations

from pdpl.catalog import prompts_ar_for
from pdpl.eval.golden_set import load_golden_set
from pdpl.services.decision import build_control_decider, build_deterministic_decider


def test_every_case_rationale_is_faithful_engine_output() -> None:
    """The drift guard: regenerate (status, rationale) from each case's
    `source_answers` via the real engine and require an exact match."""
    for case in load_golden_set():
        decide = build_deterministic_decider(case.source_answers)
        status, rationale = decide(case.gap.control_code)
        assert status == case.gap.status, f"{case.id}: status drifted"
        assert rationale == case.gap.rationale, f"{case.id}: rationale drifted"


def test_unsatisfied_questions_ar_is_faithful_to_engine_and_catalog() -> None:
    """The C3a grounding guard: rebuild each case's `unsatisfied_questions_ar`
    from the engine's STRUCTURED `unsatisfied_codes` joined through the catalogue
    — the exact path the C4 runtime feeds the model — and require LITERAL
    equality with the stored field. No string-parsing, no hand-typed text."""
    for case in load_golden_set():
        decide = build_control_decider(case.source_answers)
        decision = decide(case.gap.control_code)
        rebuilt = prompts_ar_for(decision.unsatisfied_codes)
        assert rebuilt == case.gap.unsatisfied_questions_ar, (
            f"{case.id}: unsatisfied_questions_ar drifted from engine+catalog"
        )


def test_no_rule_controls_have_no_unsatisfied_questions() -> None:
    """The three high-severity controls with no engine rule carry no questions,
    so the field is empty and the model must bind to the control TITLE alone
    (ADR-0010 §4)."""
    no_rule = {
        c.id for c in load_golden_set()
        if c.gap.rationale == "no deterministic rule registered for this control yet"
    }
    assert no_rule, "expected at least one no-rule case in the corpus"
    for case in load_golden_set():
        if case.id in no_rule:
            assert case.gap.unsatisfied_questions_ar == ()


def test_golden_set_is_the_agreed_size() -> None:
    """12-20 cases (ADR-0010 §4). The current weighted-hybrid set is 14."""
    n = len(load_golden_set())
    assert 12 <= n <= 20, f"golden set has {n} cases, expected 12-20"


def test_golden_set_covers_the_gap_statuses() -> None:
    """The corpus must exercise the non-compliant / partial / not_assessed
    statuses (ADR-0010 §4) — compliant is not a gap and is intentionally absent."""
    statuses = {c.gap.status for c in load_golden_set()}
    assert {"non_compliant", "partial", "not_assessed"} <= statuses
    assert "compliant" not in statuses


def test_case_ids_are_unique() -> None:
    cases = load_golden_set()
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids)), "golden-set case ids must be unique"


def test_human_expectation_fields_are_present_and_unrated_in_c2() -> None:
    """The Layer-B / per-case structure is version-controlled now; the values
    are filled and rated in C3, never faked off the stub (ADR-0010 §2). Here we
    only assert the fields EXIST and carry no fabricated rating."""
    for case in load_golden_set():
        assert isinstance(case.must_contain, list)
        assert isinstance(case.must_not_contain, list)
        # No quality_score may be invented against a stub.
        assert case.quality_score is None, f"{case.id}: quality_score must be unrated in C2"
