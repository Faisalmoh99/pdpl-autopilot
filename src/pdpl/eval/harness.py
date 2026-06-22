"""`run` — the eval harness core, and its Layer-A metrics (ADR-0010 §2-3).

`run(explainer, cases)` is a pure, testable function (it returns numbers, it
does not print) — the CLI in `__main__` is the only thing that prints. For
each golden case it asks the explainer for a candidate and runs that **raw**
candidate through the SAME `pdpl.verification.verify_explanation` the runtime
gate calls. It deliberately does NOT go through `pdpl.explanations.explain_gap`:
the orchestration returns the deterministic *fallback* text on rejection, which
would mask whether the raw model output passed — and `gate_pass_rate` is by
definition a property of the raw output (the fraction that would NOT need
fallback, ADR-0010 §3).

The metrics are Layer A only (ADR-0010 §2): exact, reproducible, trustworthy.
`gate_pass_rate` is the conjunction headline; `per_check_rates` is the
diagnosable breakdown — computed by iterating `VerificationVerdict.checks`, so a
future check (e.g. groundedness, ADR-0009 §3) appears automatically with no
change here. The names are keyed to ADR-0010 §3 (`no_compliance_assertion_rate`
etc.).

Layer-B signals (`must_contain`/`must_not_contain` per-case scoring and the
human `quality_score`) are deliberately NOT computed here: against a
fixed-output stub they carry no signal (ADR-0010 §2, honest constraint). The
fields are version-controlled in the golden set now and scored in C3 against
the real model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pdpl.ai.explainer import Explainer
from pdpl.eval.golden_set import GoldenCase

# The ONE shared verifier — imported, never re-implemented. The identity of
# this symbol with `pdpl.verification.verify_explanation` is asserted by the
# tests, so the eval can never silently drift into measuring a copy.
from pdpl.verification import verify_explanation


@dataclass(frozen=True)
class CaseResult:
    """One case's full result — the raw output and every signal scored on it.

    Carries the raw model `candidate` so the manual run can dump it into the
    human-review artifact (the Layer-B rating, ADR-0010 §2, is done against THIS
    exact text). `error` is set instead of `candidate` when the explainer raised
    (a real-model failure), so one failing case never aborts the whole run.
    """

    id: str
    candidate: str | None
    gate_passed: bool
    checks: dict[str, bool]
    must_contain_missing: tuple[str, ...]
    must_not_contain_present: tuple[str, ...]
    must_expectations_passed: bool
    error: str | None = None


@dataclass(frozen=True)
class EvalMetrics:
    """The Layer-A result of one harness run over a set of cases (ADR-0010 §3).

    `gate_pass_rate` is the conjunction headline (fraction of raw candidates
    passing the whole gate). `per_check_rates` is the per-check breakdown that
    explains WHICH check drags the headline down, keyed by the ADR-0010 §3
    metric names (`no_compliance_assertion_rate`, `references_control_rate`,
    `arabic_rate`, `within_length_bounds_rate`).

    `must_expectations_rate` is a DETERMINISTIC content-fidelity DIAGNOSTIC
    (ADR-0010 §3): the fraction of cases whose raw output contains every
    `must_contain` substring and none of the `must_not_contain` substrings.
    It is auto-scored (Layer-A trust) but it is NOT a gate or safety metric and
    NOT a release gate — the runtime gate guarantees safety; this measures how
    faithfully the output matches the golden set's per-case expectations.

    All numbers are exact for a given set of `case_results`; against the REAL
    model a single run is one non-deterministic sample (a point estimate).
    """

    n_cases: int
    gate_pass_rate: float
    per_check_rates: dict[str, float]
    must_expectations_rate: float = 0.0
    case_results: tuple[CaseResult, ...] = ()


def _rate(count: int, total: int) -> float:
    """Fraction in [0, 1]; an empty set yields 0.0 rather than dividing by zero."""
    return count / total if total else 0.0


def _score_case(case: GoldenCase, candidate: str) -> tuple[dict[str, bool], bool, tuple[str, ...], tuple[str, ...], bool]:
    """Run the shared gate + the per-case must-expectations on one raw output."""
    ctx = case.gap
    verdict = verify_explanation(
        candidate,
        control_code=ctx.control_code,
        control_title_ar=ctx.control_title_ar,
    )
    checks = {name: result.passed for name, result in verdict.checks.items()}
    missing = tuple(s for s in case.must_contain if s not in candidate)
    present = tuple(s for s in case.must_not_contain if s in candidate)
    must_passed = not missing and not present
    return checks, verdict.passed, missing, present, must_passed


async def run(explainer: Explainer, cases: Sequence[GoldenCase]) -> EvalMetrics:
    """Measure `explainer` over `cases` and return the Layer-A metrics + the
    per-case records.

    Pure of I/O beyond the explainer call: no DB, no network of its own. For a
    `StubExplainer` it is fully deterministic and offline (ADR-0010 §1). Against
    the real model, an explainer failure on a case is caught and recorded as a
    failing `CaseResult` rather than aborting the run, so a costed run always
    yields a complete picture.
    """
    results: list[CaseResult] = []
    passed = 0
    must_passed_count = 0
    check_pass_counts: dict[str, int] = {}

    for case in cases:
        try:
            candidate = await explainer.explain(case.gap)
        except Exception as exc:  # real-model failure — record, do not abort
            results.append(
                CaseResult(
                    id=case.id,
                    candidate=None,
                    gate_passed=False,
                    checks={},
                    must_contain_missing=tuple(case.must_contain),
                    must_not_contain_present=(),
                    must_expectations_passed=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        checks, gate_passed, missing, present, must_passed = _score_case(case, candidate)
        if gate_passed:
            passed += 1
        if must_passed:
            must_passed_count += 1
        for name, ok in checks.items():
            check_pass_counts[name] = check_pass_counts.get(name, 0) + int(ok)
        results.append(
            CaseResult(
                id=case.id,
                candidate=candidate,
                gate_passed=gate_passed,
                checks=checks,
                must_contain_missing=missing,
                must_not_contain_present=present,
                must_expectations_passed=must_passed,
            )
        )

    total = len(cases)
    return EvalMetrics(
        n_cases=total,
        gate_pass_rate=_rate(passed, total),
        per_check_rates={
            f"{name}_rate": _rate(count, total)
            for name, count in check_pass_counts.items()
        },
        must_expectations_rate=_rate(must_passed_count, total),
        case_results=tuple(results),
    )


def mean_quality_score(cases: Sequence[GoldenCase]) -> float | None:
    """The Layer-B aggregate: the mean human `quality_score` over rated cases
    (ADR-0010 §3). Returns None until any case is rated — quality_score is filled
    by the engineer against a real run artifact, never off a stub (ADR-0010 §2)."""
    rated = [c.quality_score for c in cases if c.quality_score is not None]
    return sum(rated) / len(rated) if rated else None


def format_report(results: dict[str, EvalMetrics]) -> str:
    """Render the metric table comparing one or more named runs (ADR-0010 §3).

    Pure string-building so the CLI stays a thin shell. Columns are the named
    runs (e.g. the good stub vs the asserting-compliance stub); rows are
    `gate_pass_rate` followed by the per-check breakdown. The honest caveats
    (the fixed-stub artifact, the C3-deferred Layer-B signals) are printed so a
    reader cannot misread the numbers (ADR-0010 §2).
    """
    if not results:
        return "no runs to report"

    names = list(results)
    first = results[names[0]]
    metric_rows = [
        "gate_pass_rate",
        *first.per_check_rates.keys(),
        "must_expectations_rate",
    ]

    label_w = max(len(m) for m in metric_rows)
    col_w = max(12, *(len(n) for n in names))

    def _value(metrics: EvalMetrics, metric: str) -> float:
        if metric == "gate_pass_rate":
            return metrics.gate_pass_rate
        if metric == "must_expectations_rate":
            return metrics.must_expectations_rate
        return metrics.per_check_rates[metric]

    header = "metric".ljust(label_w) + "  " + "".join(n.rjust(col_w + 2) for n in names)
    lines = [
        f"AI explanation eval — Layer A (ADR-0010), n_cases={first.n_cases}",
        "",
        header,
        "-" * len(header),
    ]
    for metric in metric_rows:
        row = metric.ljust(label_w) + "  "
        row += "".join(f"{_value(results[n], metric):.2f}".rjust(col_w + 2) for n in names)
        lines.append(row)

    lines += [
        "",
        "Notes (ADR-0010):",
        "  - gate_pass_rate is a FEATURE-VALUE number, not a safety number: the",
        "    runtime gate guarantees safety regardless of any score.",
        "  - references_control_rate is low here as a STUB ARTIFACT — a",
        "    fixed-output stub cannot ground itself to many different controls;",
        "    a real model is measured in C3.",
        "  - must_expectations_rate is a deterministic CONTENT-FIDELITY",
        "    diagnostic (Layer A), NOT a gate or safety metric: it measures how",
        "    faithfully the output matches the golden set's per-case expectations.",
        "    Against a fixed stub it is a stub artifact; it is meaningful against",
        "    the real model (C3).",
        "  - quality_score (Layer B) is human-rated against a real run artifact,",
        "    never off a stub (ADR-0010 §2).",
    ]
    return "\n".join(lines)
