# 2026-06-24 — C4a: runtime explanation orchestration (the live wiring)

Phase 4, Session C4a is the **live wiring** — the largest seam in the phase. The pieces built and
rated in isolation (the `Explainer` port + the deterministic gate in C1, `GeminiExplainer` in C3a, the
`ai_explanations` cache in C3b) become **one runtime path**: `explain_gap` does cache get → re-gate on
hit / miss → build `GapContext` + live catalog join → gate → put → return. Two constructs become
runtime in this session:

- the **safety chokepoint** stops being a property of one pure function and becomes the single point
  every user-facing string passes through — including strings read back from the cache (re-gated);
- the **`unsatisfied_questions_ar` join** (C3a) stops being an eval-time reconstruction and becomes the
  live grounding, which **retroactively validates the golden-set construction**: a test proves the
  runtime feeds the model exactly what the eval rated, verbatim — reuse is an **identity**, not an
  approximation.

The HTTP surface (endpoint vs. readiness enrichment, request-scoped session, the trigger point) is
**out of scope** — deferred to C4b with the product decision it carries. ADR-0009's endpoint open
questions stay open.

## What landed (6 separated commits)

- **ADR-0011** — runtime explanation orchestration: the sequence, the gate as single chokepoint incl.
  re-gate-on-read (refining ADR-0009 §6), the fallback floor + its safety-by-construction invariant,
  `ExplanationResult`, and modelVersion observability. Logs the control-text faithfulness gap as a
  bounded, known limitation. Open questions left OPEN for C4b.
- **7th `.importlinter` contract** `explanations-no-decision-core` — `pdpl.explanations` may not import
  `services.decision/checks/scoring`. The orchestrator takes the `ControlDecision` via DI; the
  engine→`explain_gap` composition lives in the C4b endpoint, outside this layer. 7 kept, 0 broken.
- **`build_gap_context`** (pure assembler) + the **identity test** — assembles a tenant-agnostic
  `GapContext` and performs the live `catalog.prompts_ar_for` join. Takes the verdict's **fields**
  (not a `ControlDecision` object) to honour the 7th contract. The identity test runs the full path
  `source_answers → build_control_decider → unsatisfied_codes → build_gap_context →
  unsatisfied_questions_ar` and asserts verbatim equality with the rated golden field across all 14
  cases.
- **`deterministic_fallback`** (the safe floor) — per-status Arabic templates, safe by construction
  (no compliance assertion, Arabic, references the control, tenant-agnostic), served directly.
- **`explain_gap`** orchestrator + `ExplanationResult(text, source, reason, model_version)` — cache +
  re-gate + fallback. Integration-tested against real Supabase.
- **modelVersion capture** — the `Explainer` port return is refined from `str` to
  `ExplainerOutput(text, model_version)`; `GeminiExplainer` captures the API's `modelVersion`, warns on
  a requested-vs-returned mismatch, and the orchestrator surfaces it on the result.

## Decisions made this session (and why)

- **Re-gate the cache on read** (not "trust the verified-only write invariant"). It makes the gate the
  single chokepoint every user-facing string passes through — fresh **or** cached — so the safety
  property is **independent of trusting every write path** (removes a chain of trust assumptions for
  microseconds; the gate is deterministic). A cached row that fails the re-gate is an anomaly
  (`cache_regate_failed`): logged at error, counted as the standing signal, replaced by the fallback.
  The row is immutable at the DB role, so a poisoned row falls back on every read until a
  `prompt_version` bump.
- **`not_assessed` asserts NO gap** — the explicit fix for the C3a #6 failure (the model wrongly
  asserted a deficiency on a `not_assessed` case; a hand-written template falls into the same trap). It
  states the control was not checked and that this does **not** mean a deficiency. Only
  `non_compliant`/`partial` name the shortfall — and **factually** (what the engine decided), with no
  evaluative judgment word («قصور» removed from the `non_compliant` intro).
- **The fallback's guarantee is SAFETY, not "passes the gate."** Safety holds by construction; **length**
  is the only check that can fail (a long enumeration > 800), which is acceptable because length is a
  content check, not safety (the C3a lesson). The enumeration is **never truncated** to force the bound
  (truncation is the C3a failure we refuse to reintroduce). The test asserts the fallback passes the
  **full** gate on all 14 golden cases (construction-quality evidence) **and** that the safety
  properties hold — it does **not** claim gate-pass for every possible input.
- **`build_gap_context` takes the verdict's fields, not a `ControlDecision`** — so `pdpl.explanations`
  never imports the decision core (the 7th contract). "The verdict crosses as data" is precisely that
  boundary; the C4b endpoint destructures the `ControlDecision` it computed.
- **modelVersion is DETECT, not PREVENT.** The cache key uses the **requested** id (modelVersion is
  unknown before the call, so it cannot key the lookup). We capture the returned `modelVersion`, log it,
  and warn on a mismatch so a human can notice a silently re-pointed alias and bump `prompt_version`.
  The port return is refined to `ExplainerOutput` because the model id must travel **with** the text it
  produced — a shared explainer instance serving concurrent calls makes a "last model version" attribute
  race.

## Bounded gap logged (not hidden)

- **Control-text faithfulness gap.** The golden set's `control_title_ar` / `control_description_ar` are
  hand-typed and **not** drift-protected — the same mirage rejected in C3a, one layer up (on `controls`
  instead of `questions`). The C4a identity test covers `unsatisfied_questions_ar` only, so the rated
  `GapContext`'s control-text portion is **not yet proven faithful to runtime**. The proper fix (C4b)
  mirrors the catalog: a `SEEDED_CONTROLS` leaf in `pdpl.catalog`, a drift test against migration 0003,
  and rebuilding the golden set's control fields from it. **Not built in C4a** — recorded in ADR-0011.

## Tests

- New offline: `test_explanation_context.py` (build + identity), `test_explanation_fallback.py`
  (denylist-clean, full-gate on 14, safety props, not_assessed asserts no gap, () drops the line),
  modelVersion provenance tests in `test_gemini_explainer.py`.
- Rewritten integration (real Supabase): `test_explanation_orchestration.py` surfaces the KEYSTONE on
  **both** gated paths — fresh compliance assertion → `gate_rejected`; poisoned cache row → re-gate →
  `cache_regate_failed` — plus miss→put→hit round trip, explainer-error fallback, and model_version
  propagation.
- Full suite green; `.importlinter` 7 kept, 0 broken.

## Out of scope (C4b and later)

The HTTP endpoint / session / trigger; the `SEEDED_CONTROLS` leaf that closes the control-text gap;
persisting into `findings.ai_explanation_ar`; v2 prompt; scheduling / continuous monitoring; auth; real
channels; real billing; LLM-as-judge; allowlist-groundedness.
