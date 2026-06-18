# 2026-06-18 — Phase 4 opened: AI explanation layer + eval ADRs

Phase 4 (AI Product + eval) begins. This is an **ADR-only** session — design and the eval
approach, **no code**. Phase 4 finally introduces AI, starting with the **Arabic gap
explanation**, and — just as importantly — the eval discipline to measure its output quality
**numerically**. The core principle (AI explains, deterministic logic decides) becomes
load-bearing for the first time: until now holding the safety line was cheap because there was
no AI.

Two ADRs landed; no other files changed.

## What landed

- **ADR-0009 — AI Gap-Explanation Layer.** The architecture of the explanation feature:
  - an `Explainer` **behind a port** (`StubExplainer` + `GeminiExplainer`, swappable) in the
    reserved `pdpl.ai` namespace — the *untrusted producer*;
  - a **deterministic verification gate** (`pdpl.verification.verify_explanation`) — a pure,
    trusted function **outside** `pdpl.ai` — that **is** the safety guarantee: any AI output
    failing it is rejected and we fall back to the deterministic `rationale`, so the user is
    safe on every request **regardless of any eval score**;
  - `GapContext` **tenant-agnostic by construction** — control text + status + rationale only,
    **never PII** — which also keeps the cache leak-free and sidesteps cross-border transfer;
  - **caching** in a dedicated `ai_explanations` content-hash table, lazy/on-demand, keyed by
    `hash(prompt_version, model_version, control_code, status, rationale, lang)`;
  - the import-linter guard now holds **in both directions**: core ✗→ `pdpl.ai` (existing) and
    `pdpl.ai` ✗→ `decision/scoring/checks` (new).
- **ADR-0010 — AI Explanation Eval Methodology** (the educational core, a standalone artifact):
  - the **AI-PM framing**: the runtime gate guarantees safety; the eval's job is to **harden
    the gate**, not to gate the release. An unsafe output the gate missed = a **gate bug** →
    fix the gate, guaranteed forever after.
  - **two layers, one shared verifier**: Layer A = the *same* `verify_explanation` + the
    golden set's deterministic `must_contain`/`must_not_contain` assertions (auto-scored);
    Layer B = a human `quality_score` (the only soft signal);
  - metrics led by **`gate_pass_rate`** (NOT a safety number — it measures fallback rate /
    feature value), plus per-check rates (`no_compliance_assertion_rate`, … ) for diagnosis;
  - golden set of **12–20 hand-rated cases** reusing the existing synthetic companies' findings
    as the input corpus;
  - the **keystone negative test**: a deliberately-unsafe stub asserting «أنت ملتزم» that the
    gate MUST reject — proving the reject→fallback machinery is wired end-to-end;
  - **LLM-as-judge deferred** — advisory-only, non-load-bearing, with its validity caveats.

## Decisions worth remembering

- **The gate is architecture, not a metric.** Safety comes from a deterministic check in the
  request path, not from an aggregate eval score. The eval *hardens* it.
- **The trusted guard lives outside the untrusted namespace.** `verify_explanation` is in
  `pdpl.verification`, not `pdpl.ai` — putting the guard inside the thing it guards is a
  category error. The same one verifier is reused by runtime AND eval (no second copy to drift).
- **No PII to the LLM, by construction** — protects personal data, keeps the content-hash cache
  shareable across tenants, and avoids a cross-border transfer question for this feature.
- **The compliance-assertion check is a curated denylist** — bounded, not complete; hardened
  through the gate-bug loop. Owned honestly rather than overclaimed.
- **`references_control` keys off `control_title_ar` OR `control_code`** — requiring the raw
  developer token would false-reject good Arabic and corrupt `gate_pass_rate`.

## Build sequencing (confirmed)

Measurement before the feature (the observability-from-line-one principle, applied to AI):

1. **This session** — ADR-0009 + ADR-0010 only.
2. **Next** — `pdpl.ai` port + `StubExplainer`, `pdpl.verification`, the eval harness + golden
   set + keystone test, and the second import-linter contract → run the eval against the
   **stub** and produce the first numbers, before any real LLM exists.
3. **Then** — `GeminiExplainer` (real call) + reliability wrapper + `ai_explanations` cache +
   usage counter → re-run the same harness against the real model and compare.
4. **Later** — wire the on-demand explanation into the findings/HTTP layer.

## Definition-of-Done check

- [x] Design/ADR — ADR-0009 (architecture) + ADR-0010 (eval); this whole session is the design.
- [n/a] Logging + correlation ID — no code this session.
- [n/a] Error handling + reliability — designed (reuse Phase-3 patterns); not built.
- [n/a] Tests / eval — eval methodology decided; harness not built yet.
- [x] No secrets in code — provider key specified as `SecretStr`, never logged (design).
- [x] Build-log entry — this file.

## Honest pieces

- **Nothing executable shipped.** No port classes, no verifier, no harness, no migration — by
  design. The first numbers come next session, against the stub.
- **The bare-`rationale` fallback is a degraded UX** (English-ish technical text, not polished
  Arabic). Acceptable for MVP; a deterministic Arabic fallback template is deferred.
- **The gate starts without allowlist-groundedness** — needs structured control→article data
  that doesn't exist yet (article is embedded in `controls.code`). Deferred, not blocking.
- **The golden set will be small and self-rated** — a directional signal, not a robust
  benchmark; grows when real usage gives real cases.

## What's still deferred

The real Gemini call; document reading/upload; scheduling/continuous monitoring; authentication;
billing beyond a minimal usage counter; LLM-as-judge; allowlist-groundedness; a larger/
multi-rater golden set; persisting explanations into `findings.ai_explanation_ar`.

## Lessons (Faisal)

1. Safety is deterministic architecture, not a statistical metric.

The trap is wanting a "safety_pass_rate = 1.0" in the eval — which conflates user safety with model quality. Safety is guaranteed by the architecture and the runtime gate: if the model fails or hallucinates, the output is deterministically rejected and the system falls back to safe deterministic text. The eval's job is not to guard the release; it is to harden the gate numerically — every unsafe output it surfaces that the gate missed becomes a permanent deterministic fix to the gate (the gate is hardened by adding a check, never "trained").

2. Keep the trusted guard outside the untrusted namespace.

Putting the deterministic verifier inside pdpl.ai is an architectural category error: that whole namespace is the untrusted producer, and a guard cannot live inside the thing it guards. The verifier is a pure, trusted function in pdpl.verification, outside pdpl.ai, serving both the runtime gate and the eval harness from one place — the same checks, never duplicated.

3. Eliminate PII by design, not by a procedural reminder.

Instead of telling developers "remember to strip customer data before calling Gemini," cut the risk at the root architecturally. Making GapContext tenant-agnostic — built only from public regulation text and the deterministic rationale — does three things at once for this feature: it protects PII, it makes the cache safely shareable across tenants (identical impersonal content, no cross-tenant data leak), and it sidesteps cross-border data transfer for the gap-explanation feature. (Document reading later would reopen the cross-border question — a logged trigger, not a solved problem.)

4. The negative test proves the failure path fires — not that the safety line is complete.

Positive tests ("does clean text pass?") only show the gate is permissive. The keystone test injects a deliberately unsafe stub output ("أنت ملتزم") and asserts the gate rejects it and the system falls back to the deterministic rationale. What it proves is that the reject-and-fallback machinery actually fires end-to-end on a bad input — a real integration guarantee a positive test can't give. What it does NOT prove is completeness: "أنت ملتزم" is a phrase the denylist already knows, so the test can't show the gate catches novel or paraphrased compliance assertions — that coverage comes from the gate-bug loop (Lesson 1), not this one test. If this test ever fails, the reject→fallback path is broken and the build must go red.
