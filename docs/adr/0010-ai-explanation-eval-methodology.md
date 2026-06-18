# ADR-0010: AI Explanation Eval Methodology

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0009 — AI Gap-Explanation Layer](0009-ai-gap-explanation-layer.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [Product Definition — success metrics](../product-definition.md), [CLAUDE.md — Tests + eval rule](../../CLAUDE.md)

## Context

`CLAUDE.md` makes eval a first-class build rule, on equal footing with tests:

> Deterministic parts get unit tests; AI parts get an **eval set measuring precision/recall numerically.**

ADR-0009 decides the *architecture* of the gap-explanation feature: an `Explainer` behind a port, and a deterministic verification gate (`pdpl.verification.verify_explanation`) that guarantees safety on every request by falling back to the deterministic `rationale` when an AI output fails. This ADR decides how we **measure**, numerically, whether the AI is doing its job well — and, critically, what role that measurement does and does **not** play.

This is the educational core of Phase 4. It is also where it is easy to fool yourself. The central design question is honest measurement a solo developer can actually trust, and the central risk is conflating two very different things: *"is the user safe?"* (an architecture question, already answered by the gate) and *"is the feature any good?"* (a quality question, which is what the eval answers).

This ADR is a standalone, reusable artifact: the same harness runs against the `StubExplainer` first and against `GeminiExplainer` later, unchanged, so the two are directly comparable.

## Decision drivers

- **Safety is settled by architecture, not by a score.** The eval must not be allowed to imply the opposite.
- **Test the real gate, not a copy of it.** A measurement that re-implements the checks is measuring the wrong thing.
- **Honest about what a solo dev can trust.** Deterministic assertions are trustworthy; a model judging a model is not, without its own validation.
- **Measurement before the feature** — the same principle as observability-from-line-one. The harness must run and produce numbers against a stub before the real LLM exists.

## Decision

### 1. The AI-PM framing: the gate guarantees safety; the eval *hardens* the gate

This distinction is the whole point of the ADR and is stated up front:

- **The deterministic gate (ADR-0009 §3) is the safety guarantee.** Any AI output that fails it is rejected and replaced by the deterministic `rationale`. The user is therefore safe on every request **regardless of any eval score**. The eval does **not** stand between the model and the user; the gate does.
- **The eval's role on safety is to HARDEN the gate, not to gate the release.** If the eval surfaces an unsafe output that the gate *missed* (e.g. a new way of asserting compliance the phrase-list didn't catch), that is a **gate bug**. The fix is to strengthen `verify_explanation` — after which the gate guarantees it forever, deterministically, for that class of input. The fix is never "tighten the prompt and hope."
- Consequently we **do not** define a "safety_pass_rate must be 1.0 or the release fails" metric. Safety is not a release gate because it is an *always-on runtime gate*. Framing it as a pass/fail release threshold would falsely imply the user's safety depends on the score.

### 2. Two layers, one shared verifier

The eval has two layers, and they are deliberately not mixed:

**Layer A — deterministic assertions (trustworthy, the hard floor).** Two kinds, both auto-scored and reproducible:
- the **shared verifier**: the eval calls **the exact same** `pdpl.verification.verify_explanation` function the runtime gate calls (ADR-0009 §2/§3). The checks are **not re-implemented in the eval** — re-implementing them would test a copy, not the gate that actually protects users;
- the golden set's **per-case `must_contain` / `must_not_contain` assertions** (§4): exact-match string checks against each case's hand-written expectations. These are deterministic and belong to Layer A — they are *not* judgement.

This layer yields exact, trustworthy, reproducible numbers.

**Layer B — human quality rating (the only soft signal).**
A single human-rated number per case — `quality_score` — capturing what string assertions cannot: does it explain *this* gap, is the remediation step sensible, is the Arabic genuinely clear. This is judgement, not a theorem, so the set is small and rated by the engineer (§4). It is the **only** soft signal; the deterministic `must_contain`/`must_not_contain` assertions on the same cases are **not** part of it.

### 3. The metric set

All emitted as numbers when the harness runs; Layer-A metrics are exact, Layer-B is a rated sample.

| Metric | Layer | Meaning | Trust |
|---|---|---|---|
| `gate_pass_rate` | A | Fraction of **raw model** outputs that pass `verify_explanation` **as a whole** (the conjunction of all checks; i.e. would NOT need fallback). **Low = high fallback rate = degraded feature value, NOT danger.** | exact |
| `no_compliance_assertion_rate` | A | Per-check: fraction passing the compliance-assertion denylist (ADR-0009 §3 check 1). The safety-critical check's own number. | exact |
| `references_control_rate` | A | Per-check: fraction satisfying the reference rule — contains `control_title_ar` (or a salient keyword from it) **OR** `control_code` (ADR-0009 §3 check 2). | exact |
| `arabic_rate` | A | Per-check: fraction meeting the Arabic-character ratio threshold. | exact |
| `within_length_bounds_rate` | A | Per-check: fraction within the non-empty / max-length bounds. | exact |
| `grounded_rate` | A | Per-check (deferred with the allowlist check, ADR-0009 §3): fraction citing no PDPL article outside the control's known set. | exact, when data exists |
| `must_expectations_rate` | A | Golden-set per-case `must_contain` / `must_not_contain` assertions passing. | exact |
| `quality_score` | B | Mean hand-rating over the golden set (explains the right gap, sensible fix, clear Arabic). | human-rated sample |
| `judge_correctness` | B | (Deferred, advisory only — §6) An LLM-as-judge correctness signal, non-load-bearing. | low / advisory |

`gate_pass_rate` stays the **headline** *feature-value* number — it is the **conjunction** of the individual checks. The **per-check rates explain *which* check is dragging it down**: a low `gate_pass_rate` with a high `no_compliance_assertion_rate` but a low `references_control_rate`, for instance, says the model writes safe-but-ungrounded prose — a prompt problem, not a safety one. Before this split the safety-critical check had no dedicated number; `no_compliance_assertion_rate` gives it one (for diagnosis — *not* as a release gate; safety is the runtime gate's job, §1).

`gate_pass_rate` is explicitly **not** a safety number: a value of 0.6 means 40% of requests fall back to deterministic text (a worse experience) and tells us the model/prompt needs work — it does **not** mean 40% of users saw something unsafe, because nothing unsafe ever passes the gate. And because `references_control_rate` measures the title-OR-code rule (ADR-0009 §3 check 2), a low value means *the model failed to ground its prose to the control*, **not** that it merely omitted a developer code.

### 4. The golden set: reuse the synthetic companies as the input corpus

The eval cases reuse the **existing synthetic companies' findings** (the same fixtures behind the deterministic engine and the product success metric of "≥ 90% of real gaps on 10 synthetic companies") as the input corpus. No new fictional data to maintain. Each case is:

```
input:   { control_code, control_title_ar, control_description_ar,
           status, rationale, severity_weight }   # a real GapContext (ADR-0009 §2)

# Part 1 — deterministic per-case assertions (Layer A, auto-scored):
expect:  { must_contain:    [ ... ],   # e.g. reference to the control / the specific gap
           must_not_contain:[ ... ] }  # e.g. any compliance assertion

# Part 2 — human judgement (Layer B, the only soft signal):
quality_score: <1–5, rated once by the engineer>   # right gap, sensible fix, clear Arabic
```

Each case carries **two distinct parts**, kept apart on purpose (§2): the `expect` block is deterministic string-matching (auto-scored, Layer-A trust), and `quality_score` is the human judgement (Layer B). The deterministic assertions are **not** folded into the quality rating.

**12–20 cases, hand-rated once by the engineer.** That is enough to cover the seeded controls across `non_compliant` / `partial` / `not_assessed` statuses and to make the Layer-A numbers meaningful, and small enough that one person can rate them honestly. The cases live as fixtures, version-controlled, so the set is a durable artifact.

### 5. The keystone negative test: the gate MUST reject "أنت ملتزم"

The single most important test of the phase is a **deliberately-unsafe `StubExplainer`** that asserts compliance — it returns text containing «أنت ملتزم» (or an equivalent compliance assertion). The eval asserts the gate **rejects** it and the system falls back to the deterministic `rationale`.

**Be precise about what this proves.** «أنت ملتزم» is already *in* the denylist (ADR-0009 §3 check 1). So the keystone proves the **reject→fallback machinery works end-to-end on a known-bad input** — the gate detects, refuses, and substitutes safe deterministic text, with no AI assertion reaching the user. It does **not** prove the gate catches *every* compliance phrasing: coverage of unseen, paraphrased assertions (e.g. «نظامك سليم») is explicitly the **gate-bug loop's** job (§1, ADR-0009 §3), not this one test's. Claiming otherwise would overclaim what a single known-input test can show.

It is still the keystone because it proves the safety line is **wired and real, not assumed**: a positive test ("the gate passes good text") only shows the gate is permissive; only a negative test that *forces a known worst case* proves the rejection path is actually connected. If this test ever fails, the safety machinery is broken and the build is red. It is named and called out as the proof-of-machinery test, not buried among happy-path cases.

### 6. LLM-as-judge: deferred, advisory-only, non-load-bearing

An LLM grading the explainer's output is **not** part of the trusted measurement and is **deferred**. When/if added, it is advisory only and never load-bearing on a release, for honest reasons:

- **same-family bias** — a judge from the same model family as the producer tends to rate it favourably;
- **non-determinism** — the judge's own output varies run to run, so its numbers are not reproducible;
- **it needs its own validation** — a judge you have not validated against human ratings is an opinion with a number attached, not a measurement.

It can serve later as a *cheap, noisy* second opinion on Layer-B semantic correctness — strictly alongside the human-rated golden set, never replacing it, and never gating anything.

## Consequences

**Positive**

- The eval reuses the *real* verifier, so its Layer-A numbers describe the gate that actually protects users — not a divergent copy that could drift and lie.
- The gate-bug framing turns every unsafe output the eval finds into a permanent deterministic fix, monotonically hardening the safety line over time instead of chasing prompts.
- Reusing the synthetic companies means no parallel fixture corpus to maintain, and ties the AI eval to the same ground truth as the deterministic success metric.
- Running against the stub first proves the measurement works before the LLM exists; the identical harness then quantifies the real model and the stub-vs-Gemini delta.

**Negative / accepted**

- **The golden set is small and self-rated.** 12–20 cases rated by one engineer is a thin, biased sample for Layer-B quality — honest about it: it is a directional signal, not a statistically robust benchmark. It is the right size for a solo MVP and grows when real usage gives real cases.
- **`quality_score` is subjective.** Without a second rater there is no inter-rater agreement to report. Accepted for now.
- **`gate_pass_rate` can be misread as a safety number** by someone who skips §1/§3. The naming (`gate_pass_rate`, not `safety_rate`) and this ADR's framing are the mitigation.

**NOT built this session (ADR-only), and triggers**

- This session: **ADR-0010 only. No harness, no fixtures, no judge.**
- Next session (with ADR-0009): the harness + the 12–20-case golden set + the keystone negative test, run against `StubExplainer` to produce the first numbers — calling the real shared `pdpl.verification.verify_explanation`.
- **LLM-as-judge** is deferred; add only as an advisory, validated-against-humans, non-gating signal — trigger: when Layer-B human rating becomes the bottleneck and a noisy second opinion is worth its caveats.
- **`grounded_rate` / allowlist-groundedness** is deferred with its runtime counterpart (ADR-0009 §3) until structured control→article data exists.
- **A larger / multi-rater golden set** is deferred until real user gaps and a second rater are available.
- **No scheduler / continuous monitoring** of eval scores in this phase — the harness is run on demand by the developer, not on a schedule.

## Open questions (deferred)

- **A pass/fail threshold for `gate_pass_rate` as a *feature-value* (not safety) gate** — e.g. "ship the real model only if it clears the stub by X" — left until we have stub and real numbers to calibrate against.
- **Whether `quality_score` ratings live in code fixtures or a small data file** — a harness-shape detail for the build session.
