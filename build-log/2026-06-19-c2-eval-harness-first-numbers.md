# 2026-06-19 — C2: eval harness + golden set + first numbers (against the stub)

Phase 4, Session C2 builds the **measurement** ADR-0010 designed and produces the **first
numbers** — before any real LLM (that is C3). The harness runs against the `StubExplainer`, and
the deliverable is proving the measurement **discriminates** a good output from a deliberately
unsafe one. Nothing here calls a model.

## What landed

- **The harness (`pdpl.eval`)** — a pure, testable `run(explainer, cases) -> EvalMetrics`,
  wrapped by a thin CLI (`python -m pdpl.eval`) that only prints. Same testability split as the
  outbox worker's `run_once` vs `__main__`.
  - It measures the **raw** model output: `explainer.explain` → the **same**
    `pdpl.verification.verify_explanation` the runtime gate calls (asserted by symbol identity,
    never a copy) — **not** `explain_gap`. `gate_pass_rate` is a property of the raw candidate
    (would-NOT-need-fallback); routing through the orchestrator's fallback would mask it.
  - **Layer A only**: `gate_pass_rate` (the conjunction headline) + the per-check breakdown
    (`no_compliance_assertion_rate`, `references_control_rate`, `arabic_rate`,
    `within_length_bounds_rate`), keyed to ADR-0010 §3 and derived by iterating
    `VerificationVerdict.checks` so a future check appears automatically.
- **The golden set** — a version-controlled YAML, **14 cases** (weighted hybrid): heavy on the
  four governed controls (privacy-notice / dsr-access / breach-72h / ropa) across
  `non_compliant` / `partial` / `not_assessed`, plus high-severity `not_assessed` cases
  (security ART19, lawful-basis ART5, cross-border ART29). Every case's `status` + `rationale`
  is **provably faithful** engine output — generated via the real decision engine and carrying
  `source_answers`, with a test that regenerates and asserts an exact match (drift protection).
- **5th import contract** — `production ✗→ pdpl.eval`: the eval is tooling, never part of the
  feature, so it cannot become a runtime dependency (nor drag `pyyaml` / the golden set into the
  serving path). It freely imports `pdpl.ai` + `pdpl.verification`; only the reverse is barred.
- **Tests** (pure, offline): metrics are exact fractions; the measurement discriminates; the
  eval reuses the real verifier; the corpus is faithful and the agreed size.

## The first numbers (stub)

```
metric                         good   asserting_compliance
gate_pass_rate                 0.64   0.00
no_compliance_assertion_rate   1.00   0.00
references_control_rate        0.64   1.00
arabic_rate                    1.00   1.00
within_length_bounds_rate      1.00   1.00
```

Read it the way ADR-0010 §3 intends: the headline alone is muddy; the per-check breakdown
**diagnoses**.
- The **bad** stub is rejected entirely (`gate_pass=0.00`) **purely** on
  `no_compliance_assertion_rate=0.00` — even though it grounds to the control (`1.00`), is
  Arabic, and is within length. The safety check is isolated, doing its job.
- The **good** stub's `gate_pass=0.64` tracks `references_control_rate=0.64` exactly, with
  `no_compliance=1.00` — so the shortfall is **grounding (a prompt problem), not safety**.

That `references_control_rate=0.64` is a **stub artifact**: one fixed text cannot genuinely
ground to 14 different controls. It is *not* a feature defect — on the control-matched subset
(privacy-notice) the good stub clears the **whole** gate (`1.00`), proving the gate is fully
passable. This is exactly why a fixed stub cannot meaningfully exercise Layer B.

## Honest constraint (stated, not faked)

Against a stub, **only Layer A discriminates**. The human `quality_score` and the per-case
`must_contain` / `must_not_contain` are **not** meaningfully exercisable against a fixed
template, so they are version-controlled **empty** now and scored in **C3** against the real
model. No `quality_score` is printed off the stub.

## Division of labor / handoff

- **Done (me):** the harness, the faithfully-extracted `GapContext` cases, and the empty
  expectation fields.
- **Faisal's (AI-PM judgement, not delegated):** fill `must_contain` / `must_not_contain` /
  `quality_criteria` per case in `golden_set.yaml`, then rate `quality_score` in C3 against the
  real `GeminiExplainer`.

## Lessons (Faisal)

1. The 0.64 trap, and the coincidental passes (a PDPL-vocabulary collision).

The good stub returns one fixed text — a privacy-notice explanation — for every case, ignoring
the GapContext, so it naturally fails references_control on the 5 cases whose control isn't
privacy-notice. The telling part is the passes: the 3 privacy-notice cases passed legitimately,
but several others passed by COINCIDENCE — the fixed text contains common PDPL words («إشعار»,
«معالجة») that the gate's salient-token match accepted as references to breach-notification or
ROPA controls. So against a fixed-text stub, references_control is noise in both directions. More
usefully, those coincidental passes are behavioral evidence of a real precision limit in
references_control: a salient-token substring can bind to the WRONG control when controls share
vocabulary (token collision). This confirms — with a measured failure mode, not a hunch — exactly
why the stricter allowlist-groundedness check (already deferred in ADR-0009/0010) is needed. The
eval did its job: it gave concrete proof of an imprecision in our own gate before we paid a cent
to any LLM.

2. The measurement platform has teeth (safety measured, not assumed).

The clean, certain win this session: the safety metric (no_compliance_assertion_rate) collapsed
from 1.00 to 0.00 the moment we ran the malicious stub. That proves behaviorally that the gate is
awake and deterministically separates malicious from benign — before we ever touch the API.

3. Drift detection, not magic growth (a regression firewall).

Generating the corpus from the real engine and freezing it in YAML does NOT mean the system grows
or adds new controls by itself. It is a regression firewall: a drift test keeps the EXISTING cases
from silently diverging — change the core's mechanics and the test goes red, forcing the developer
to regenerate the YAML by hand. The absence of a coverage test for newly-added controls is logged
as a conscious deferred item (to close when the control set grows in a later session), without
expanding scope now.

## Deferred / honest pieces (later sessions)

- **`references_control` token-collision imprecision** — behaviorally confirmed this session (a
  salient-token substring can bind to the WRONG control when controls share PDPL vocabulary, e.g.
  «إشعار» / «معالجة»). Addressed later by the stricter **allowlist-groundedness** check already
  deferred in ADR-0009 §3 / ADR-0010 §3 — not widened here.
- **No coverage test ensuring every seeded control has a golden case** — the drift test guards
  against EXISTING cases diverging, but nothing asserts that each seeded control is represented.
  Adding a new control is therefore a silent manual step today. A later-session item, to close
  when the control set grows.

## Out of scope (deferred to C3)

The real Gemini call, the cache, the usage counter, HTTP wiring, `quality_score` as a release
gate, and LLM-as-judge — all unbuilt by design.
