# ADR-0011: Runtime Explanation Orchestration

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0009 — AI Gap-Explanation Layer](0009-ai-gap-explanation-layer.md), [ADR-0010 — AI Explanation Eval Methodology](0010-ai-explanation-eval-methodology.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [CLAUDE.md — core decision principle](../../CLAUDE.md)

## Context

ADR-0009 designed the gap-explanation feature as separate, individually-tested pieces and built them across three sessions: the `Explainer` port + `StubExplainer` and the deterministic gate `verify_explanation` (C1), the real `GeminiExplainer` with the Phase-3 reliability wrapper (C3a), and the `ai_explanations` content-hash cache repository (C3b). The eval (ADR-0010) rated the real model against the human golden set — `gate_pass_rate = 1.0`, `quality_score = 4.79`.

What did **not** exist is the runtime path that wires those pieces into one live call: *look up the cache → on a miss, build the `GapContext`, call the explainer, gate the output, persist only the verified text, return.* C3b's repository docstring sketched this sequence but deliberately deferred it ("WIRING IS DEFERRED to C4"). This ADR decides that orchestration.

It is the session where two things that were *constructs* become *runtime*:

- The **safety chokepoint** stops being a property of one pure function tested in isolation and becomes the single point every user-facing string actually passes through — including strings read back from the cache.
- The **`unsatisfied_questions_ar` join** (C3a) stops being an eval-time reconstruction and becomes the live grounding the model receives. This **retroactively validates the golden-set construction**: the runtime now feeds the model exactly what the eval rated, so "reuse" is an **identity**, not an approximation — and a test proves it.

This ADR covers the orchestration **core** only. The HTTP surface — a dedicated endpoint vs. enriching the readiness response, the request-scoped DB session, and the trigger point — carries an unresolved product decision and is **explicitly out of scope** (it stays open in ADR-0009; see Open questions). Keeping the product decision out of the mechanics is the reason for the split.

## Decision drivers

- **The safety line must hold on _every_ user-facing string, by mechanism — not by trusting each write path.** A guarantee that depends on "the only writer always gated correctly" is weaker than one that re-checks on read.
- **Reuse must be provable, not asserted.** The claim "the runtime feeds the model what the eval rated" is only worth stating if a test forces it to be true verbatim, across the whole derivation path.
- **A failed or rejected AI call is never a failed request.** There must be a deterministic floor that is safe by construction and served directly.
- **Keep `pdpl.explanations` free of the decision core.** The orchestrator consumes a `ControlDecision` as data (dependency injection); it must not be able to import the engine and recompute a verdict.

## Decision

### 1. The orchestration core: `pdpl.explanations`, three modules

```
pdpl/explanations/
├── context.py       # build_gap_context(...)  — PURE assembler + the live catalog join
├── fallback.py      # deterministic_fallback(ctx) — the safe floor (per-status Arabic)
└── orchestrator.py  # explain_gap(...)  — cache + gate + fallback, returns ExplanationResult
```

- **`build_gap_context` is pure** (no DB, no network, tenant-agnostic). It takes the engine's structured `ControlDecision` plus the control's static text and assembles a `GapContext`, performing the live `catalog.prompts_ar_for(decision.unsatisfied_codes)` join. The **source** of the control's static text (DB `controls` table vs. a future `pdpl.catalog.SEEDED_CONTROLS` leaf) is a wiring concern deferred to C4b — `build_gap_context` receives it as arguments.
- **`deterministic_fallback` is the safe floor** (§4).
- **`explain_gap` is the orchestrator** (§2), and the **only** async/stateful piece (it touches the cache).

### 2. The sequence — the gate is the single chokepoint

```
key = compute_cache_key(prompt_version, model, control_code, status, rationale, lang)
hit = await ai_explanations.get(session, key)
if hit is not None:
    verdict = verify_explanation(hit, ...)          # RE-GATE ON READ
    if verdict.passed:  return ExplanationResult(hit, source="cache_hit")
    else:               log cache_regate_failed; return fallback(reason="cache_regate_failed")
# miss:
try:
    candidate = await explainer.explain(ctx)
except ExplainerError:
    return fallback(reason="explainer_error")        # timeout / 5xx / truncation / block
verdict = verify_explanation(candidate, ...)          # THE GATE
if not verdict.passed:
    return fallback(reason="gate_rejected")           # never cached
await ai_explanations.put(session, key, text=candidate, ...)  # ONLY verified text
return ExplanationResult(candidate, source="ai_verified")
```

Two invariants, both load-bearing:

- **Gate before put, always; put verified text only.** The orchestrator is the writer, and verified-only is *its* contract — the cache enforces no safety (ADR-0009 §6 / the C3b repo docstring).
- **The gate is the single chokepoint every user-facing string passes through — fresh _or_ cached.** This is the refinement of ADR-0009 §6's read semantic: that section said "served from cache" after a verified write; this ADR adds that the cache read is **re-gated**, so the safety property is **independent of trusting every write path**. `verify_explanation` is deterministic and costs microseconds, so re-gating is free relative to the call it guards. It removes a chain of trust assumptions ("the row was written by the orchestrator, which gated before put, and the row is immutable, and no other writer exists, and no migration injected a row…") and replaces them with one mechanical check on read.

**A re-gate failure is an anomaly, never served.** A cached row that fails the re-gate is logged as `cache_regate_failed`, counted, and replaced by the fallback for that read. Because the row is **immutable at the DB role** (the `pdpl_app` grant is INSERT + SELECT only — no DELETE/UPDATE, ADR-0003 / C3b), a poisoned row is served as fallback on **every** read until a `prompt_version` bump produces a new key. The counter is the standing signal that this is happening; the user is never exposed.

### 3. `ExplanationResult` — the return contract

```python
Source = Literal["cache_hit", "ai_verified", "fallback"]

@dataclass(frozen=True)
class ExplanationResult:
    text: str                       # the user-facing Arabic string (always safe)
    source: Source
    reason: str | None = None       # set ONLY when source == "fallback":
                                    #   "gate_rejected" | "explainer_error" | "cache_regate_failed"
    model_version: str | None = None  # provenance: the model that PRODUCED a fresh ai_verified
                                       # output (§6). None for cache_hit / fallback.
```

A structured result (not a bare `str`) because three consumers need more than the text: the metrics need `source`/`reason` to compute the AI-success-vs-fallback ratio, the C4b run artifact needs `model_version` for per-call provenance, and the eventual endpoint needs to know whether it is serving a degraded floor. `model_version` is in the **result** (provenance) but **not** in the cache key — it is unknown before the call, so it cannot key the lookup (§6).

### 4. The deterministic fallback floor — safe by construction, served directly

The floor is the deterministic outcome when the AI is unavailable, errors, truncates, or the gate rejects its output. It is **not** runtime-gated: if the floor itself could fail the gate, there would be no floor. Its safety is therefore guaranteed **by construction**, and a test proves the construction holds.

Per-status Arabic templates:

- **`non_compliant` / `partial`** name the shortfall, because the **deterministic engine** decided it (it is not an AI claim). The intro is **factual, not evaluative** — it describes *what the engine decided* ("this control is incomplete; the following requirements were not met"), never a judgment word like «قصور». It is followed by the unsatisfied questions' Arabic text (the live `unsatisfied_questions_ar`) and a "review this manually" line. When a rule-bearing control has **no** unsatisfied questions (the rare `()` case), the requirements header and list are **dropped entirely** — the intro is a complete standalone sentence — so no dangling empty list is rendered.
- **`not_assessed` asserts no gap.** This is the explicit fix for the C3a #6 failure mode (the model wrongly asserted a deficiency on a `not_assessed` case; a hand-written template falls into the same trap just as easily). The template reads "this control was not assessed automatically and needs manual review — this does not mean a deficiency exists, only that it was not checked." It references the control (by title) and asserts nothing about compliance.

**What is guaranteed by construction vs. what is not (stated honestly):**

- **SAFETY holds by construction, always:** no compliance assertion (every template is checked against the denylist verbatim at authoring time — there is no excuse for a static string to trip the gate), Arabic, references the control (every template opens with `control_title_ar`), and tenant-agnostic (only the control title + the static question text; never a tenant answer or PII).
- **LENGTH is the only check that can fail.** A control with many unsatisfied questions yields a long enumeration that can exceed the 800-char bound. This is **acceptable**: length is a **content** check, not a safety one (the C3a lesson — `finishReason` detects truncation, not the length bound). A long-but-safe fallback is served directly. The enumeration is **never truncated** to force the bound — truncation is exactly the C3a failure we refuse to reintroduce.

The test asserts the fallback passes the **full** gate on all golden cases (construction-quality evidence) **and** that the four safety properties hold — it does **not** claim "the fallback always passes the gate for every possible input." The guaranteed property is **safety**, not gate-pass.

### 5. The identity test — the retroactive validation, made concrete

A test exercises the **whole runtime construction path** on the golden-set cases' own inputs:

```
case.source_answers
  -> build_control_decider(...)(control_code)   # the real engine -> ControlDecision.unsatisfied_codes
  -> build_gap_context(decision, control text)  # the real C4 runtime assembler -> GapContext
  -> .unsatisfied_questions_ar  ==  case.gap.unsatisfied_questions_ar   # VERBATIM, all cases
```

It must run **code derivation from the structured `ControlDecision`** (the same source C3a used), then the runtime `build_gap_context`, then compare — **not** a join on pre-stored codes. Without the code-derivation step, "whole path" is overclaimed. This proves the runtime feeds the model exactly the grounding the eval rated: **reuse = identity, not approximation.**

**No coupling of eval to runtime.** The test lives in `tests/`, imports `build_gap_context` from `pdpl.explanations` (production) and `load_golden_set` from `pdpl.eval` (tooling). Contract 5 (`production-no-eval`) forbids *production* importing the eval; a *test* importing both is legal and is how the two are tied without making the eval a runtime dependency. The eval harness itself never imports the runtime.

### 6. Model-version observability — DETECT, not PREVENT

The cache key uses the **requested** model id (the configured `gemini_model`, default the GA alias `gemini-2.5-flash`). The key is computed **before** the call to look up a hit, so the API's resolved `modelVersion` — unknown until the response arrives — **cannot** be in the key. Pinning the requested id is "as pinned as available"; the residual risk is the provider silently re-pointing the GA alias to a newer underlying snapshot, which the key cannot see.

The mitigation is **detection, not prevention**: `GeminiExplainer` captures the `modelVersion` the API returns, the orchestrator surfaces it on `ExplanationResult.model_version`, it is logged and recorded in the C4b run artifact, and a **mismatch between requested and returned** is logged as a warning. Prevention is then a human act — noticing the warning and bumping `prompt_version` (which busts the cache and re-rates). The seam: the `Explainer` port return is refined from `str` to `ExplainerOutput(text, model_version)` (a **refinement of ADR-0009 §1**) — the model id must travel **with** the text it produced, because a `GeminiExplainer` instance may serve concurrent calls and a stored "last model version" attribute would race.

The existing rated run (`gemini-2.5-flash_gap-ar-v1_20260622T195344Z`) is **not** backfilled: it is frozen, alias-pinned provenance, recorded as a known limitation of the v1 rating (the rating predates modelVersion capture).

### 7. The seventh `.importlinter` contract: `explanations-no-decision-core`

`pdpl.explanations` may not import `pdpl.services.decision`, `pdpl.services.checks`, or `pdpl.services.scoring`. The orchestrator receives the `ControlDecision` via dependency injection, so this is a **free lock of a real boundary**: the explanation layer reads a deterministic verdict as data and can never reach back to recompute one. The composition that wires the engine to `explain_gap` lives **outside** `pdpl.explanations` — in the C4b endpoint layer. The six existing contracts (ADR-0009 §7 / ADR-0010) stay green; this is the seventh.

## Consequences

**Positive**

- Safety becomes a **request-path property independent of model quality _and_ independent of trusting every write path** — the gate re-checks on read, so the worst case for any user-facing string (fresh or cached) is a fallback to safe deterministic text.
- The identity test makes "the runtime feeds the model what the eval rated" a **proven, verbatim** fact across the full derivation path, not a hopeful assertion — retroactively validating the golden-set construction.
- The trust boundary is locked one notch tighter: a seventh contract makes `pdpl.explanations` provably free of the decision core, and the engine-to-explainer composition is pushed out to the wiring layer.
- `model_version` provenance makes silent alias drift **detectable** at the point it would otherwise corrupt the cache.

**Negative / accepted (bounded gaps, surfaced not hidden)**

- **Control-text faithfulness gap (logged explicitly).** Deferring the control-text *sourcing* to C4b is fine, but the golden set's `control_title_ar` / `control_description_ar` are **hand-typed and NOT drift-protected** — the same mirage rejected in C3a, one layer up (on `controls` instead of `questions`). The C4a identity test covers `unsatisfied_questions_ar` **only** (which is independent of the control text), so the rated `GapContext`'s control-text portion is **not yet proven faithful to runtime**. The proper fix mirrors the catalog exactly: a `SEEDED_CONTROLS` leaf in `pdpl.catalog`, a drift test against migration 0003, and rebuilding the golden set's control fields from it. This is **C4b work and is not built in C4a** — it is recorded here as a known bounded gap.
- **Alias drift is detected, not prevented.** The guarantee rests on a human noticing the `modelVersion` mismatch warning and acting. Accepted: prevention would require a stable dated GA id we do not have, and the cache key cannot see a post-call value.
- **A long fallback can exceed the length bound.** A `non_compliant` control with many unsatisfied questions produces an enumeration over 800 chars. Served directly as the safe floor; length is a content concern, not safety, and truncating it would reintroduce the C3a failure.
- **A poisoned cache row is served as fallback on every read until a `prompt_version` bump.** Immutability at the DB role means it cannot be deleted; the `cache_regate_failed` counter is the standing signal. Accepted — the alternative (a delete path) would weaken the audit/immutability guarantee for a case that should never occur.

## Open questions (deferred to C4b — RESOLVED in [ADR-0012](0012-explanation-http-surface.md))

These were left open here; all four are now resolved in [ADR-0012](0012-explanation-http-surface.md):

- **The HTTP surface** — a dedicated explanation endpoint vs. enriching the readiness/gap-report response. → ADR-0012 §1: dedicated `POST /tenants/{id}/explanations` (§2: POST, for cacheability semantics).
- **The request-scoped DB session** and the **trigger point** for an on-demand explanation. → ADR-0012 §3–§4: re-derive the structured verdict via the engine; the application-service owns `session_scope`; `explain_gap` unchanged.
- **Persisting into `findings.ai_explanation_ar`** vs. serving purely from the content-hash cache. → ADR-0012 §6: no persistence; serve via the cache.
- **The control-text source** — DB `controls` read vs. a `pdpl.catalog.SEEDED_CONTROLS` leaf (with the drift test that closes the faithfulness gap above). → ADR-0012 §3 + C4b commit 1: `SEEDED_CONTROLS`, drift-pinned to migration 0003, passed into the unchanged `build_gap_context`.
