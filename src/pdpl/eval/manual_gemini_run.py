"""`python -m pdpl.eval.manual_gemini_run` — the MANUAL, costed eval (C3a).

This is the ONE step that calls the real Gemini API and spends money. It is a
deliberate manual action run by the engineer with their own key — never in CI,
never in the test suite (the mocked unit tests in test_gemini_explainer.py
cover the explainer with no network/key/cost).

It runs the same harness (`harness.run`) over the golden set against the real
model at temperature 0, prints the Layer-A summary, and writes a human-review
ARTIFACT to `eval-runs/<run_id>.yaml`: each case's raw model output, the gate
verdict, the per-case must-expectations result, and a blank `rating` slot. The
engineer rates each 1–5 against THAT artifact, then copies the scores into
`golden_set.yaml` as `quality_score` with `quality_score_run: <run_id>` so the
Layer-B rating is pinned to the exact output it was rated against (ADR-0010 §3).

The helpers (`make_run_id`, `build_review_artifact`, `format_manual_summary`)
are pure and unit-tested offline; only `_amain` touches the network and disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pdpl.ai.gemini import gemini_explainer_from_settings
from pdpl.ai.prompt import PROMPT_VERSION
from pdpl.config import get_settings
from pdpl.eval.golden_set import GoldenCase, load_golden_set
from pdpl.eval.harness import EvalMetrics, run

_ARTIFACT_DIR = Path(__file__).resolve().parents[3] / "eval-runs"

_POINT_ESTIMATE_NOTE = (
    "Single non-deterministic sample — a POINT ESTIMATE, not a stable baseline. "
    "Re-running may shift the numbers even at temperature 0."
)


def make_run_id(model: str, prompt_version: str, now: datetime | None = None) -> str:
    """A sortable, self-describing run id used to name the artifact and to pin
    each quality_score to its provenance."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{model}_{prompt_version}_{ts}"


def build_review_artifact(
    *,
    run_id: str,
    model: str,
    prompt_version: str,
    temperature: float,
    metrics: EvalMetrics,
    cases: Sequence[GoldenCase],
) -> str:
    """Render the YAML review artifact: the run header, the Layer-A metrics, and
    one block per case with the raw output and a blank `rating` for the engineer.
    """
    by_id = {c.id: c for c in cases}
    case_blocks = []
    for r in metrics.case_results:
        gc = by_id.get(r.id)
        case_blocks.append(
            {
                "id": r.id,
                "control_code": gc.gap.control_code if gc else None,
                "status": gc.gap.status if gc else None,
                "quality_criteria": gc.quality_criteria if gc else "",
                "output": r.candidate,
                "error": r.error,
                "gate_passed": r.gate_passed,
                "checks": r.checks,
                "must_contain_missing": list(r.must_contain_missing),
                "must_not_contain_present": list(r.must_not_contain_present),
                "must_expectations_passed": r.must_expectations_passed,
                "rating": None,  # <- engineer fills 1-5 here
            }
        )

    doc = {
        "run_id": run_id,
        "model": model,
        "prompt_version": prompt_version,
        "temperature": temperature,
        "note": _POINT_ESTIMATE_NOTE,
        "rating_instructions": (
            "Rate each case 1-5 in `rating` (right gap, sensible fix, clear "
            "Arabic), then copy into golden_set.yaml as `quality_score` with "
            f"`quality_score_run: {run_id}`."
        ),
        "metrics": {
            "n_cases": metrics.n_cases,
            "gate_pass_rate": round(metrics.gate_pass_rate, 4),
            "must_expectations_rate": round(metrics.must_expectations_rate, 4),
            "per_check_rates": {
                k: round(v, 4) for k, v in metrics.per_check_rates.items()
            },
        },
        "cases": case_blocks,
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)


def format_manual_summary(metrics: EvalMetrics, *, model: str, run_id: str) -> str:
    """The on-screen Layer-A summary of a real run, with the honest caveats."""
    lines = [
        f"AI explanation eval — REAL model (ADR-0010), run_id={run_id}",
        f"model={model}, n_cases={metrics.n_cases}",
        "",
        f"  gate_pass_rate         {metrics.gate_pass_rate:.2f}",
        f"  must_expectations_rate {metrics.must_expectations_rate:.2f}  (content-fidelity diagnostic, NOT safety)",
    ]
    for name, rate in metrics.per_check_rates.items():
        lines.append(f"  {name:<24} {rate:.2f}")
    errored = [r.id for r in metrics.case_results if r.error]
    if errored:
        lines.append("")
        lines.append(f"  ERRORED cases ({len(errored)}): {', '.join(errored)}")
    lines += [
        "",
        "Notes (ADR-0010):",
        "  - gate_pass_rate is a FEATURE-VALUE number, not a safety number: the",
        "    runtime gate guarantees safety regardless of any score.",
        f"  - {_POINT_ESTIMATE_NOTE}",
    ]
    return "\n".join(lines)


async def _amain() -> None:
    settings = get_settings()
    explainer = gemini_explainer_from_settings(settings)  # fails fast if unset
    cases = load_golden_set()

    metrics = await run(explainer, cases)
    run_id = make_run_id(settings.gemini_model, PROMPT_VERSION)

    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = _ARTIFACT_DIR / f"{run_id}.yaml"
    artifact_path.write_text(
        build_review_artifact(
            run_id=run_id,
            model=settings.gemini_model,
            prompt_version=PROMPT_VERSION,
            temperature=settings.gemini_temperature,
            metrics=metrics,
            cases=cases,
        ),
        encoding="utf-8",
    )

    print(format_manual_summary(metrics, model=settings.gemini_model, run_id=run_id))
    print(f"\nReview artifact written to: {artifact_path}")
    print("Rate each case 1-5 there, then copy into golden_set.yaml "
          "(quality_score + quality_score_run).")


if __name__ == "__main__":
    asyncio.run(_amain())
