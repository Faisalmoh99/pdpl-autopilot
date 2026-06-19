"""`python -m pdpl.eval` — the thin CLI around the harness (ADR-0010 §2-3).

It only loads the golden set, runs `harness.run` against both stubs, and prints
`harness.format_report`. All logic and every number live in the testable
`run()`; this shell does no measurement of its own — same split as the outbox
worker's `run_once` vs `__main__`.

Running BOTH stubs is the C2 deliverable made visible: the good stub vs the
deliberately-unsafe `asserting_compliance` stub, side by side, so the
measurement is seen to DISCRIMINATE — `no_compliance_assertion_rate` collapses
1.00 -> 0.00 and drags `gate_pass_rate` with it — before any real LLM exists
(ADR-0010 §1/§5).
"""

from __future__ import annotations

import asyncio

from pdpl.ai.explainer import StubExplainer
from pdpl.eval.golden_set import load_golden_set
from pdpl.eval.harness import format_report, run

# A known-good Arabic explanation grounded to the privacy-notice control. It
# deliberately avoids «البيانات» (a token shared by most control titles): a
# fixed-output stub can only genuinely ground to the controls naming «إشعار»/
# «الخصوصية», so `references_control` passes on those and fails elsewhere — the
# stub artifact the report calls out, which makes the per-check breakdown
# visibly DIAGNOSE why the good stub's gate_pass is below 1.0 (grounding, NOT
# safety). It asserts nothing about compliance.
_GOOD_DEMO_AR = (
    "لا يتوفر لديك إشعار خصوصية واضح يوضّح للعميل أغراض المعالجة وحقوقه. "
    "أضف إشعار الخصوصية إلى موقعك كخطوة أولى لمعالجة هذه الثغرة."
)


async def _amain() -> None:
    cases = load_golden_set()
    results = {
        "good": await run(StubExplainer.good(_GOOD_DEMO_AR), cases),
        "asserting_compliance": await run(StubExplainer.asserting_compliance(), cases),
    }
    print(format_report(results))


if __name__ == "__main__":
    asyncio.run(_amain())
