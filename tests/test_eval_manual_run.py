"""Manual costed-run HELPERS (C3a) — offline, no network/key/cost.

Only the pure pieces of pdpl.eval.manual_gemini_run are tested here (run-id
formatting, the review-artifact YAML, the on-screen summary). `_amain` itself —
which calls the real Gemini API and writes to disk — is the deliberate manual
step and is never executed in the suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import yaml

from pdpl.ai.explainer import StubExplainer
from pdpl.eval.golden_set import load_golden_set
from pdpl.eval.harness import run
from pdpl.eval.manual_gemini_run import (
    build_review_artifact,
    format_manual_summary,
    make_run_id,
)

_GOOD_AR = (
    "لا يتوفر لديك إشعار خصوصية واضح يوضّح للعميل أغراض المعالجة وحقوقه. "
    "أضف إشعار الخصوصية إلى موقعك كخطوة أولى لمعالجة هذه الثغرة."
)


def test_make_run_id_is_sortable_and_self_describing() -> None:
    now = datetime(2026, 6, 22, 9, 30, 0, tzinfo=timezone.utc)
    rid = make_run_id("gemini-2.5-flash", "gap-ar-v1", now)
    assert rid == "gemini-2.5-flash_gap-ar-v1_20260622T093000Z"


async def test_review_artifact_is_valid_yaml_with_blank_ratings() -> None:
    cases = load_golden_set()
    metrics = await run(StubExplainer.good(_GOOD_AR), cases)
    rid = "gemini-2.5-flash_gap-ar-v1_20260622T093000Z"

    text = build_review_artifact(
        run_id=rid,
        model="gemini-2.5-flash",
        prompt_version="gap-ar-v1",
        temperature=0.0,
        metrics=metrics,
        cases=cases,
    )
    doc = yaml.safe_load(text)

    assert doc["run_id"] == rid
    assert doc["temperature"] == 0.0
    assert doc["metrics"]["n_cases"] == len(cases)
    assert len(doc["cases"]) == len(cases)
    # Every case carries the raw output and a blank rating slot for the engineer,
    # plus the provenance pointer instruction.
    for block in doc["cases"]:
        assert "output" in block
        assert block["rating"] is None
    assert rid in doc["rating_instructions"]
    # The raw output the rating is made against is present verbatim.
    assert any(block["output"] == _GOOD_AR for block in doc["cases"])


async def test_summary_labels_the_diagnostic_and_caveats() -> None:
    cases = load_golden_set()
    metrics = await run(StubExplainer.good(_GOOD_AR), cases)
    out = format_manual_summary(metrics, model="gemini-2.5-flash", run_id="rid-1")

    assert "gate_pass_rate" in out
    assert "must_expectations_rate" in out
    assert "NOT safety" in out
    assert "POINT ESTIMATE" in out
