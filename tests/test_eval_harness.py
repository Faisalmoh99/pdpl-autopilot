"""Unit tests for the eval harness (ADR-0010 §2-3).

Pure and offline — no database, no network, no model. They drive `run()` with
`StubExplainer`s over hand-built and real golden cases, and prove the three
things C2 must prove:

  1. the harness computes the Layer-A metrics CORRECTLY (exact fractions);
  2. the measurement DISCRIMINATES a good stub from the deliberately-unsafe
     `asserting_compliance` stub — the C2 deliverable, before any real LLM
     (ADR-0010 §1/§5); and
  3. the eval calls the REAL shared `verify_explanation`, not a copy
     (ADR-0010 §2) — asserted by symbol identity.
"""

from __future__ import annotations

from pdpl.ai.explainer import GapContext, StubExplainer
from pdpl.eval import harness
from pdpl.eval.golden_set import GoldenCase, load_golden_set
from pdpl.eval.harness import run
from pdpl.verification import verify_explanation

# A known-good Arabic explanation grounded to the PRIVACY-NOTICE control: it
# contains that control's salient tokens («إشعار», «الخصوصية») but none of the
# right-of-access control's tokens («الوصول», «البيانات», «الشخصية») — so it
# grounds to privacy-notice cases and NOT to access cases. Asserts no
# compliance, is Arabic, within length bounds.
_GOOD_PRIVACY_NOTICE_AR = (
    "لا يتوفر لديك إشعار خصوصية واضح يوضّح للعميل أغراض المعالجة وحقوقه. "
    "أضف إشعار الخصوصية إلى موقعك كخطوة أولى لمعالجة هذه الثغرة."
)


def _privacy_notice_case(case_id: str) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        gap=GapContext(
            control_code="PDPL-ART12-PRIVACY-NOTICE",
            control_title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
            control_description_ar="إشعار يوضّح أغراض المعالجة وحقوق صاحب البيانات.",
            status="non_compliant",
            rationale="privacy notice: none of 4 question(s) satisfied",
            severity_weight=7.0,
        ),
    )


def _access_case(case_id: str) -> GoldenCase:
    return GoldenCase(
        id=case_id,
        gap=GapContext(
            control_code="PDPL-ART4-DSR-ACCESS",
            control_title_ar="حق الوصول إلى البيانات الشخصية",
            control_description_ar="حق صاحب البيانات في الوصول إلى بياناته.",
            status="non_compliant",
            rationale="right of access: none of 2 question(s) satisfied",
            severity_weight=7.0,
        ),
    )


# ---------------------------------------------------------------------
# 1. The eval calls the REAL verifier — not a re-implementation (ADR-0010 §2).
# ---------------------------------------------------------------------
def test_harness_uses_the_real_shared_verifier() -> None:
    """The symbol the harness calls IS `pdpl.verification.verify_explanation`.

    If someone ever forks a copy into the eval, this identity breaks and the
    test fails — the eval can never silently measure a divergent gate.
    """
    assert harness.verify_explanation is verify_explanation


# ---------------------------------------------------------------------
# 2. Metrics are computed correctly — exact fractions (ADR-0010 §3).
# ---------------------------------------------------------------------
async def test_metrics_are_exact_fractions() -> None:
    """Two privacy-notice cases + two access cases, run with a stub whose fixed
    good text grounds ONLY to privacy-notice. The math must be exact: the safety
    check passes everywhere (1.00), the grounding check passes on half (0.50),
    and the gate as a whole passes exactly the two grounded cases (0.50)."""
    cases = [
        _privacy_notice_case("pn-1"),
        _privacy_notice_case("pn-2"),
        _access_case("acc-1"),
        _access_case("acc-2"),
    ]

    metrics = await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), cases)

    assert metrics.n_cases == 4
    assert metrics.gate_pass_rate == 0.5
    assert metrics.per_check_rates == {
        "no_compliance_assertion_rate": 1.0,
        "references_control_rate": 0.5,
        "arabic_rate": 1.0,
        "within_length_bounds_rate": 1.0,
    }


async def test_empty_set_yields_zero_not_division_error() -> None:
    metrics = await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), [])
    assert metrics.n_cases == 0
    assert metrics.gate_pass_rate == 0.0


# ---------------------------------------------------------------------
# 3. The C2 deliverable: the measurement DISCRIMINATES (ADR-0010 §1/§5).
# ---------------------------------------------------------------------
async def test_discrimination_good_vs_asserting_compliance() -> None:
    """Over the REAL golden set, swapping the good stub for the deliberately
    unsafe `asserting_compliance` stub collapses the safety-critical check and
    drags the headline down. This is the C2 proof that the measurement
    distinguishes good from bad BEFORE any real model exists."""
    cases = load_golden_set()
    assert cases, "golden set must not be empty"

    good = await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), cases)
    bad = await run(StubExplainer.asserting_compliance(), cases)

    # The safety-critical per-check is the sharp discriminator: 1.00 -> 0.00.
    assert good.per_check_rates["no_compliance_assertion_rate"] == 1.0
    assert bad.per_check_rates["no_compliance_assertion_rate"] == 0.0

    # Every asserting-compliance output is rejected by the gate as a whole...
    assert bad.gate_pass_rate == 0.0
    # ...while the good stub clears the gate on the cases it can ground to, so
    # the headline is strictly higher. (It is < 1.0 by the fixed-stub artifact:
    # one fixed text cannot ground to every control — ADR-0010 §2.)
    assert good.gate_pass_rate > bad.gate_pass_rate
    assert good.gate_pass_rate > 0.0


async def test_good_stub_clears_whole_gate_on_control_matched_subset() -> None:
    """On the subset whose control the good text actually grounds to
    (privacy-notice), the good stub passes the gate ENTIRELY (1.00) — proving
    the gate is fully passable and the sub-1.0 full-set number is purely the
    fixed-stub grounding artifact, not a defect."""
    privacy_notice = [
        c for c in load_golden_set() if c.gap.control_code == "PDPL-ART12-PRIVACY-NOTICE"
    ]
    assert privacy_notice, "expected privacy-notice cases in the golden set"

    good = await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), privacy_notice)
    bad = await run(StubExplainer.asserting_compliance(), privacy_notice)

    assert good.gate_pass_rate == 1.0
    assert bad.gate_pass_rate == 0.0


# ---------------------------------------------------------------------
# The report renders the headline + the per-check breakdown (ADR-0010 §3).
# ---------------------------------------------------------------------
async def test_report_contains_headline_and_breakdown() -> None:
    cases = load_golden_set()
    results = {
        "good": await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), cases),
        "asserting_compliance": await run(StubExplainer.asserting_compliance(), cases),
    }

    report = harness.format_report(results)

    assert "gate_pass_rate" in report
    assert "no_compliance_assertion_rate" in report
    assert "must_expectations_rate" in report
    assert "good" in report and "asserting_compliance" in report
    # The honest caveats must be present so the numbers cannot be misread.
    assert "not a safety number" in report
    assert "CONTENT-FIDELITY" in report


# ---------------------------------------------------------------------
# must_expectations_rate + per-case records (C3a, ADR-0010 §3).
# ---------------------------------------------------------------------
def _case_with_expectations(case_id, text_control, *, must_contain, must_not_contain):
    return GoldenCase(
        id=case_id,
        gap=GapContext(
            control_code="PDPL-ART12-PRIVACY-NOTICE",
            control_title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
            control_description_ar=text_control,
            status="non_compliant",
            rationale="privacy notice: none of 4 question(s) satisfied",
            severity_weight=7.0,
        ),
        must_contain=must_contain,
        must_not_contain=must_not_contain,
    )


async def test_must_expectations_rate_is_exact_and_per_case() -> None:
    """One case's expectations are met by the fixed output, the other's are not,
    so the content-fidelity diagnostic is exactly 0.5 — and the per-case records
    expose WHICH substrings missed."""
    cases = [
        _case_with_expectations(
            "hit", "x", must_contain=["إشعار", "الخصوصية"], must_not_contain=["أنت ملتزم"]
        ),
        _case_with_expectations(
            "miss", "x", must_contain=["كلمة-غير-موجودة"], must_not_contain=[]
        ),
    ]
    metrics = await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), cases)

    assert metrics.must_expectations_rate == 0.5
    by_id = {r.id: r for r in metrics.case_results}
    assert by_id["hit"].must_expectations_passed is True
    assert by_id["hit"].candidate == _GOOD_PRIVACY_NOTICE_AR
    assert by_id["miss"].must_expectations_passed is False
    assert by_id["miss"].must_contain_missing == ("كلمة-غير-موجودة",)


async def test_must_not_contain_violation_fails_expectations() -> None:
    """The good text contains «الخصوصية»; forbidding it must fail the case."""
    cases = [
        _case_with_expectations(
            "x", "x", must_contain=[], must_not_contain=["الخصوصية"]
        )
    ]
    metrics = await run(StubExplainer.good(_GOOD_PRIVACY_NOTICE_AR), cases)
    assert metrics.must_expectations_rate == 0.0
    assert metrics.case_results[0].must_not_contain_present == ("الخصوصية",)


class _RaisingExplainer:
    async def explain(self, ctx):
        raise RuntimeError("simulated model failure")


async def test_explainer_failure_is_recorded_not_raised() -> None:
    """A real-model failure on a case is caught and recorded as a failing
    CaseResult, so a costed run never aborts midway."""
    cases = [
        _case_with_expectations("err", "x", must_contain=["إشعار"], must_not_contain=[])
    ]
    metrics = await run(_RaisingExplainer(), cases)

    assert metrics.gate_pass_rate == 0.0
    assert metrics.must_expectations_rate == 0.0
    r = metrics.case_results[0]
    assert r.candidate is None
    assert r.gate_passed is False
    assert "simulated model failure" in r.error


def test_mean_quality_score_aggregates_the_human_ratings() -> None:
    """The Layer-B aggregate over the golden set, now that C3 has rated every
    case against a real run artifact (ADR-0010 §3)."""
    cases = load_golden_set()
    mean = harness.mean_quality_score(cases)
    assert mean is not None
    expected = sum(c.quality_score for c in cases) / len(cases)
    assert mean == expected


def test_mean_quality_score_is_none_when_no_case_is_rated() -> None:
    """The function still returns None for an unrated set — the invariant the
    stub relied on, kept as a unit of the aggregate itself."""
    from pdpl.ai.explainer import GapContext
    unrated = [
        GoldenCase(
            id="x",
            gap=GapContext(
                control_code="C", control_title_ar="ع", control_description_ar="ع",
                status="non_compliant", rationale="r", severity_weight=1.0,
            ),
        )
    ]
    assert harness.mean_quality_score(unrated) is None
