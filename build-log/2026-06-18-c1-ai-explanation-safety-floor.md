# 2026-06-18 — Phase 4 Session C1: the AI explanation safety floor

Phase 4 introduces AI. Before any eval harness (C2) and long before the real
LLM (C3), C1 builds the trusted, deterministic machinery and **proves the safety
floor**: bad AI text never reaches the user. Everything here is deterministic
and in-memory — no database, no network, no Supabase. ADR-0009/0010 are the
locked design; C1 implements the half ADR-0009 deferred to "next session".

## What landed

- **`pdpl.ai`** — the UNTRUSTED producer namespace (ADR-0009 §1-2).
  - `Explainer` Protocol (`async explain(ctx: GapContext) -> str`), mirroring the
    Notifier port (ADR-0008 §3).
  - `GapContext` — the producer's input contract, owned here; tenant-agnostic by
    construction (`control_code`, `control_title_ar`, `control_description_ar`,
    `status`, `rationale`, `severity_weight`, `lang`) — never raw answers / PII.
  - `StubExplainer` — injectable output, no network (mirrors `StubNotifier`),
    with `good(text)` and the keystone `asserting_compliance()` constructors.
- **`pdpl.verification`** — the TRUSTED gate (ADR-0009 §3).
  - `verify_explanation(candidate, *, control_code, control_title_ar)` — a pure
    function taking only what the checks consult (NOT a whole `GapContext`),
    returning a **structured `VerificationVerdict`** (per-check `CheckResult` +
    `passed` conjunction + `checks` dict) named 1:1 with the ADR-0010 §3 metrics.
  - Four checks: (1) no compliance assertion (curated denylist); (2) references
    the control via normalized title / salient token / code; (3) Arabic ratio
    ≥ 0.75 (code stripped first); (4) length 20..600.
  - `denylist.py` (curated assertion phrases) + `normalize.py` (alef/hamza/
    taa-marbuta/alef-maqsura folding, diacritic + tatweel stripping, whitespace
    collapse), applied to both sides of checks 1 and 2.
- **`pdpl.explanations`** — the orchestration (ADR-0009 §4), outside the core.
  `explain_gap(ctx, explainer)`: produce → verify → on a rejected verdict fall
  back to the deterministic `rationale`, via a **single fallback funnel** so
  C3's explainer-failure path drops in additively.
- **Four import-linter contracts** (ADR-0009 §7), all KEPT (verified by
  `lint-imports` + `tests/test_architecture.py`).
- **Tests** — 19 pure unit tests (no DB/network): each check in isolation, the
  two metric-validity tests below, the structured verdict's per-check reporting,
  the keystone, and the verified-text happy path.

## The guarantee proven

The deterministic gate IS the safety floor. The keystone test
(`test_keystone_compliance_assertion_is_rejected_and_falls_back`) drives the
deliberately-unsafe stub returning «أنت ملتزم …»: the gate rejects it and
`explain_gap` returns the deterministic `rationale` — the unsafe AI text never
reaches the caller. The test asserts the stub *was* called (the produce →
verify → fallback path really ran) and that the result is the rationale, not the
stub output. If it ever fails, the safety machinery is broken and the build is
red.

Honest scope (ADR-0010 §5): «أنت ملتزم» is already in the denylist, so this
proves the reject→fallback machinery works end-to-end on a known-bad input — not
that the gate catches *every* phrasing. Unanticipated paraphrases are the
gate-bug loop's job (C2).

## The denylist design (and why it protects the metric)

The denylist bans **assertion PHRASES, not bare compliance words**. Bare tokens
(«ملتزم» / «متوافق» / «ممتثل» / "compliant with") occur in legitimate
remediation guidance — *"to become compliant with the article, do X"* — so
banning them would false-reject good explanations. We ban «أنت ملتزم» / «نظامك
سليم» / «ما عليك ملاحظات» / "you are compliant" / "no gaps", etc. Known
assertions are added now, not withheld for narrative; the gate-bug loop is for
UNANTICIPATED phrasings discovered later.

This is also a *metric-validity* decision: a false-reject of legitimate text
depresses `gate_pass_rate` as a gate-too-strict artefact (not a model problem),
corrupting the headline feature-value number. Two tests pin the behaviour:

- `test_legitimate_remediation_with_bare_compliance_word_passes` — «متوافق» in
  instructive/conditional form PASSES (the assertion-vs-token distinction is
  behavioral, not just a dropped-tokens claim).
- `test_references_control_paraphrase_still_passes_via_salient_token` — a
  paraphrased control reference still satisfies check 2 via normalization +
  keyword, so paraphrase does not false-reject either.

## The verifier's enforced independence (4 contracts)

The three trust regions are fenced mechanically, all four contracts KEPT:

1. (existing) core ✗→ `pdpl.ai` / LLM SDKs — the verdict path stays AI-free.
2. (new) `pdpl.ai` ✗→ decision core — the AI reads outputs as data, never
   recomputes a verdict.
3. (new) `pdpl.verification` ✗→ `pdpl.ai` / LLM SDKs — the guard is independent
   of the thing it guards; that independence is what makes it trustworthy.
4. (new) `pdpl.verification` ✗→ decision core — it verifies, never re-decides.

Keeping `GapContext` in `pdpl.ai` (not in the verifier) and shrinking the
verifier's signature to `(candidate_text, control_code, control_title_ar)` is
what lets `pdpl.verification` import nothing from either side.

## Deferred

- **C2:** the eval harness + the 12–20-case golden set + the numeric metrics,
  calling this exact `verify_explanation`.
- **C3:** `GeminiExplainer` + the reliability wrapper + the `ai_explanations`
  cache table + the usage counter + the error taxonomy (the explainer-raise →
  fallback path; the single funnel is already shaped for it).
- **Later:** the HTTP / findings wiring that calls `explain_gap`, and a
  `correlation_id` in the orchestrator's logs (no request context yet).
- allowlist-groundedness (ADR-0009 §3) until `controls` carries structured
  article data; a deterministic Arabic fallback template (the bare-`rationale`
  fallback is degraded-but-safe for the MVP); LLM-as-judge.

## Definition-of-Done check

- [x] Design/ADR — ADR-0009 + ADR-0010 (implements their deferred half; no new
      decision).
- [x] Logging + observability — `explanations.verified` / `explanations.fallback`
      structured logs + counters (correlation_id deferred with the HTTP wiring).
- [x] Error handling — gate rejection funnels to a safe fallback; explainer-raise
      deferred to C3, shaped to drop in additively.
- [x] Tests / eval — 19 pure unit tests; the numeric eval harness is C2.
- [x] No secrets in code — none introduced (no provider key until C3).
- [x] Build-log entry — this file.

## Lessons (Faisal)

<!-- TODO: Faisal to fill in. -->
