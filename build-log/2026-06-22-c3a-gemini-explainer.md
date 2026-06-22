# 2026-06-22 — C3a: real GeminiExplainer + reliability + the eval, ready for real numbers

Phase 4, Session C3a swaps the stub for a **real LLM** behind the existing Explainer port, wraps
it in the Phase-3 reliability patterns, and extends the eval harness so the SAME measurement runs
against Gemini. The actual costed run against real Gemini is a deliberate **manual** step (my key);
everything here is offline and fully tested. C3b (the `ai_explanations` cache + repository) is
deferred — the eval calls the model directly, so a cache gives zero benefit to the numbers.

## The core risk C3 caught, and the fix (input grounding)

The deterministic `rationale` carries cryptic question **codes** (e.g.
`gap(s): Q-ART12-NOTICE-RECIPIENTS`), not readable gap text. Feeding the model only that would force
it to decode the codes and risk explaining the **wrong** gap — and the golden set's `must_contain`
("المستلمة"/"حقوق") would then fail for **input** reasons, not model quality, corrupting the numbers.

Fix: the `GapContext` now carries `unsatisfied_questions_ar` — the **readable Arabic text** of the
unsatisfied/unanswered questions — grounded faithfully, not hand-typed:

- **`pdpl.catalog`** — a new **pure leaf** module: the authoritative, tenant-agnostic seed
  question text + the join `prompts_ar_for(codes) -> tuple[prompt_ar]` (deterministic order). This
  is exactly the join the C4 runtime will feed the model (identity, not approximation).
- **migration 0004** was restructured from inline SQL to a **parameterized** `bulk_insert` from a
  frozen constant — the driver encodes the Arabic + the lone apostrophe (no hand-escaping). It
  stays self-contained (does **not** import the catalogue), so it remains replayable; a drift test
  proves **offline** that it reproduces the originally-seeded values **verbatim** and that the
  catalogue mirrors it.
- the gap codes come from the engine's new **structured** `ControlDecision.unsatisfied_codes`
  (not by re-parsing the formatted rationale string) — the same structured source C4 will use.
  `build_deterministic_decider` is now a thin `(status, rationale)` projection of it, so the
  `run_check` seam (ADR-0006 §5) is untouched.
- the golden set's field is **generated** through that engine+catalogue path and the faithfulness
  test rebuilds it with **literal equality**; the three no-rule controls yield `[]` (the model
  binds to the control TITLE alone).

## What else landed

- **`GeminiExplainer`** (httpx REST `generateContent`, **not** the `google` SDK — `google` stays
  in `forbidden_modules`). Reuses the `WebhookNotifier` shape: one per-attempt `asyncio.timeout`
  wall-clock deadline; typed transient (timeout/connection/5xx/429) vs permanent (4xx);
  malformed/blocked response → permanent; unclassifiable → bounded transient; full-jitter backoff;
  key as `SecretStr` in `x-goog-api-key`, **never logged** (fingerprint only). Minimal usage
  counter (calls + approx tokens). On exhaustion/permanent it raises so C4 can fall back to the
  deterministic `rationale`.
- **`gap-ar-v1` prompt** in its own module (version governance): Arabic, explain-not-decide, never
  assert compliance, one remediation step, bind to the control; renders the unsatisfied questions.
- **Harness expansion**: per-case `CaseResult` records (raw output kept for review) +
  `must_expectations_rate` — a deterministic **content-fidelity diagnostic** (Layer A), explicitly
  **not** a gate/safety metric; a real-model failure on a case is recorded, not raised.
- **Manual costed run** (`python -m pdpl.eval.manual_gemini_run`): runs the harness over the golden
  set at **temperature 0**, prints the Layer-A summary with the **point-estimate** caveat, and
  writes a review artifact (`eval-runs/<run_id>.yaml`) with each case's raw output + a blank
  `rating`. I rate 1–5 there, then copy into `golden_set.yaml` (`quality_score` +
  `quality_score_run` provenance pointer).

## Contracts (all kept)

6 import contracts green: new **contract 6** (catalogue is a pure leaf — no AI/decision-core/
verifier/explanations); catalogue added to contract 5 (it is production, not tooling) and contract
4 (verifier stays hermetic).

## Tests / config

- Mocked unit tests for `GeminiExplainer` via `httpx.MockTransport` (no network/key/cost) — request
  shape, classification + retry counts, the deadline, parsing, fail-fast ctor, usage metrics, and
  the key never reaching the logs. Harness + manual-run helpers tested offline via stubs.
- `GEMINI_*` settings (flash-tier default, 30s / 3 attempts / 0.5s..8s backoff, temperature 0),
  optional at import + fail-fast at construction.
- ADR-0009 §6 documents why `unsatisfied_questions_ar` is excluded from the C3b cache key (static
  function of `(control_code, status, rationale)` + static seed) and the rule that **re-seeding
  question wording requires bumping `prompt_version`**. ADR-0010 §3 documents
  `must_expectations_rate` as a content-fidelity diagnostic and single real runs as point estimates.

## First real run — what it taught us (the honest pieces)

The first manual Gemini run was **invalid** (all 14 outputs truncated mid-word): `gemini-2.5-flash`
runs *thinking* by default and thinking tokens count toward `maxOutputTokens`, so at 512 they ate
the budget. Fixed with `thinkingBudget=0` + a 1024 ceiling + a `finishReason != STOP` guard that
turns any truncated/blocked completion into a typed permanent error (never a silent partial string).

The second run was clean (14 complete, zero errors, all `STOP`) and gave the first honest data:

- **Length bound recalibrated 600 → 800 (evidence-based, not scope creep).** 7 of 8 overflows were
  605–713 chars of complete, useful Arabic with no padding — the 600 bound was an unvalidated C1
  guess made before any real output existed. Natural PDPL Arabic gap explanations (2–4 sentence
  reason + one remediation step) run ~600–750. Same spirit as the 0.75 Arabic-ratio calibration.
- **`gap-ar-v1` baseline caveat — DEFERRED to a v2 prompt, deliberately not tuned this session.**
  The prompt makes the model **quote the control title/description verbatim** in the remediation
  tail (e.g. «…وذلك التزاماً بمتطلبات إفصاح إشعار الخصوصية…»; `ropa-non_compliant` copies the full
  description in parentheses → a ~907-char outlier). Two honest consequences for the v1 baseline:
  (1) it inflates length, and (2) it **partly drives `references_control_rate = 1.00`** — the
  control token is present because it was *copied*, not necessarily because the gap was understood.
  Tuning the prompt now would corrupt the v1 baseline before `quality_score` is rated against it, so
  a new `prompt_version` is a discuss-first decision needing a **comparative run** (v1 vs v2). This
  note is the baseline-v1 record; the bound widening above is the only behavioural change.

## Lessons (Faisal)

1. Deterministic content metrics are blind to Arabic lexical flexibility.

must_expectations_rate = 0.43 is not a model failure and not a misleading number — it is an honest
diagnostic measuring exactly what it was built to measure: literal agreement with the golden set's
keywords. The discipline is in reading it correctly. In this sample, the model expressed the right
gaps using natural commercial synonyms ("إجراء موثق" for "آلية", "سجل لعمليات المعالجة" for
"أنشطة المعالجة"), which substring matching scores as misses. The lesson: for Arabic regulatory
explanation, rigid substring matching penalizes high-quality output, so a deterministic content
metric undercounts quality. The human quality_score is the load-bearing signal precisely because
the deterministic metric is synonym-blind. (Semantic-similarity scoring is a possible future
improvement, not a present claim.)

2. Strict negative keyword rules carry a context blind spot.

The security-measures case tripped the must_not_contain "تشفير" rule — but the model did not
fabricate an encryption gap; it named encryption as an EXAMPLE of what to review manually. The
matcher punished the bare word regardless of context. The lesson: a literal denylist cannot tell
"asserts a specific gap" from "offers a review example the user needs." The cheaper v2 fix is to
narrow the negative expectation to forbid FABRICATING a gap, not mentioning an example;
context-aware inference (NLI) is a heavier option, deferred.

3. Structured input reduces hallucination; the gate — not the model — is the guarantee.

On this sample, constraining the model to the unsatisfied_questions_ar array correlated with
honest behavior on the not_assessed cases: it stayed neutral ("لم يُقيَّم، يحتاج مراجعة") and did
not fabricate violations, including the three no-rule controls (the highest hallucination risk).
But this is measured model BEHAVIOR on one non-deterministic sample, not a safety guarantee. The
guarantee remains architectural: the runtime gate rejects unsafe output regardless of what the
model produces (proven by the C1 keystone). Structured grounding makes good behavior more likely;
the gate makes safety certain. Conflating "the model behaved safely here" with "safety is
guaranteed" is the exact gate-vs-metric error ADR-0010 §1 corrects.

This run reframed baseline v1: not a numeric failure in the metrics' eyes, but evidence that the
deterministic content metric undercounts a capable model — and a clear list of what to fix in the
harness for v2 (synonym-aware content checks; context-aware negative rules).

## Next

- **Manual step (me):** re-run `python -m pdpl.eval.manual_gemini_run` (temp 0, thinking off, bound
  800) → the v1 Layer-A numbers + the review artifact → rate `quality_score` (Layer B) against THIS
  bound-corrected, prompt-unchanged v1 output.
- **C3b:** the `ai_explanations` content-hash cache table + repository (INSERT+SELECT grants only,
  verified text only).
- **C4:** wire the on-demand explanation into the findings/HTTP layer (the orchestration that calls
  explainer → `verify_explanation` → fallback, and fills `unsatisfied_questions_ar` live).
