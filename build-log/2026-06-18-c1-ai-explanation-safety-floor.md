# 2026-06-18 — Phase 4 Session C1: the AI explanation SAFETY scaffolding

Phase 4 introduces AI. Before any eval harness (C2) and long before the real LLM
(C3), C1 builds the trusted, deterministic machinery and **proves the safety
line**. Everything here is deterministic and in-memory — no database, no
network, no Supabase. ADR-0009/0010 are the locked design; this session
implements the half ADR-0009 deferred to "next session".

The product's core principle — *AI reads / suggests / explains; deterministic
logic decides; AI must never tell the user "you are compliant"* — stops being a
convention and becomes a deterministic function in the request path.

## What landed

- **`pdpl.ai`** — the UNTRUSTED producer namespace (ADR-0009 §1-2).
  - **`Explainer` Protocol** (`async explain(ctx: GapContext) -> str`), mirroring
    the Notifier port (ADR-0008 §3).
  - **`GapContext`** — the producer's input contract, owned here. Tenant-agnostic
    by construction: `control_code`, `control_title_ar`, `control_description_ar`,
    `status`, `rationale`, `severity_weight`, `lang`. Never raw answers / PII.
  - **`StubExplainer`** — injectable output, no network (mirrors `StubNotifier`),
    with `good(text)` and the keystone `asserting_compliance()` constructors.
    `GeminiExplainer` deferred to C3 behind the identical seam.
- **`pdpl.verification`** — the TRUSTED gate (ADR-0009 §3). The safety guarantee.
  - **`verify_explanation(candidate, *, control_code, control_title_ar)`** — a
    pure function taking only what the checks consult (NOT a whole `GapContext`),
    returning a **structured `VerificationVerdict`** (per-check `CheckResult` +
    `passed` conjunction + `checks` dict) named 1:1 with the ADR-0010 §3 metrics,
    so C2 computes per-check rates from this exact function the runtime calls.
  - **Four checks:** (1) no compliance assertion — a curated denylist of assertion
    PHRASES; (2) references the control via normalized title / salient token /
    code; (3) Arabic ratio ≥ 0.75 (code stripped first); (4) length 20..600.
  - **Shared Arabic normalization** (`normalize.py`): alef/hamza/taa-marbuta/
    alef-maqsura folding, diacritic + tatweel stripping, whitespace collapse —
    applied to both sides of checks 1 and 2 so paraphrase/orthography do not
    false-reject.
- **`pdpl.explanations`** — the orchestration (ADR-0009 §4), outside the core
  (it imports `pdpl.ai`, banned from the core). **`explain_gap(ctx, explainer)`**:
  produce → verify → on a rejected verdict fall back to the deterministic
  `rationale`. The fallback is a **single funnel** so C3's explainer-failure path
  drops in additively.
- **Four import-linter contracts** (ADR-0009 §7), all KEPT:
  1. (existing) core ✗→ `pdpl.ai` / LLM SDKs.
  2. (new) `pdpl.ai` ✗→ decision core.
  3. (new) `pdpl.verification` ✗→ `pdpl.ai` / LLM SDKs.
  4. (new) `pdpl.verification` ✗→ decision core.
- **Tests** — 18 new, pure unit (no DB/network): each check in isolation
  (assertion caught in AR + EN; references pass + **paraphrase-still-passes** via
  salient token + normalization; Arabic ratio pass/fail + embedded-English
  tolerance; length pass/fail), the structured verdict's per-check reporting, the
  **keystone proof-of-safety test**, and the verified-text happy path.

## The keystone, and what it proves

`test_keystone_compliance_assertion_is_rejected_and_falls_back`: the
deliberately-unsafe stub returns «أنت ملتزم …»; the gate rejects it and
`explain_gap` returns the deterministic `rationale` — the unsafe AI text never
reaches the caller. The test asserts the stub *was* called (the produce →
verify → fallback path really ran), and that the result equals the rationale and
not the stub output. If it ever fails, the safety machinery is broken and the
build is red.

Honest about scope (per ADR-0010 §5): «أنت ملتزم» is already in the denylist, so
this proves the **reject→fallback machinery works end-to-end on a known-bad
input** — not that the gate catches *every* phrasing. Unanticipated paraphrases
are the gate-bug loop's job (C2).

## Decisions worth remembering

- **GapContext lives in `pdpl.ai`, not in the verifier** (implementation-level
  tightening of the ADR sketch). The verifier takes only `candidate_text`,
  `control_code`, `control_title_ar` — so `pdpl.verification` imports NOTHING
  from `pdpl.ai` or the core and is genuinely independent. Independence is what
  makes a guard trustworthy; contracts 3+4 enforce it as mechanism, not
  convention.
- **The denylist bans assertion PHRASES, not bare compliance words.** «ملتزم» /
  «متوافق» / «ممتثل» / "compliant with" occur in legitimate remediation guidance
  (*"to become compliant with the article, do X"*); banning them would
  false-reject good text and depress `gate_pass_rate` (gate-too-strict, not
  model-bad — corrupting the metric). We ban «أنت ملتزم» / «نظامك سليم» /
  «ما عليك ملاحظات» / "you are compliant", etc.
- **Known assertions are added now, not withheld for narrative.** The gate-bug
  loop is for UNANTICIPATED phrasings found later; withholding a known assertion
  would weaken the safety floor. The keystone narrative holds regardless of list
  completeness.
- **References-control is keyword + normalization, not full-title substring** —
  forcing the raw `control_code` token into layperson Arabic causes false
  rejects that corrupt `gate_pass_rate`. Title / salient token / code all
  satisfy it.
- **Arabic ratio 0.75** (calibrated up from a too-lenient 0.6 that passed
  ~40%-English text), tunable once C2 produces numbers.

## Definition-of-Done check

- [x] Design/ADR — ADR-0009 + ADR-0010 (this implements their deferred half; no
      new decision).
- [x] Logging + observability — `explanations.verified` / `explanations.fallback`
      structured logs + counters in the orchestrator (no correlation ID yet — no
      request context; added when the HTTP wiring lands).
- [x] Error handling — gate rejection funnels to a safe fallback; explainer-raise
      path deferred to C3 (the stub does not throw), shaped to drop in additively.
- [x] Tests / eval — 18 pure unit tests; the numeric eval harness is C2.
- [x] No secrets in code — none introduced (no provider key until C3).
- [x] Build-log entry — this file.

## Honest pieces / still deferred

- **The bare-`rationale` fallback is degraded UX** — English-ish technical text,
  not polished Arabic. Safe and honest for the MVP; a deterministic Arabic
  fallback template is deferred (ADR-0009).
- **The denylist's coverage is bounded, not complete** — best-effort, grown by
  the gate-bug loop. Surfaced, not hidden.
- **allowlist-groundedness** deferred until `controls` carries structured
  article data (ADR-0009 §3).
- **Out of scope (per plan):** the eval harness / golden set / metrics (C2);
  `GeminiExplainer` + reliability wrapper + `ai_explanations` cache table + usage
  counter (C3); the HTTP/findings wiring; LLM-as-judge.

## Lessons (Faisal)

Lesson (Faisal): A safety guarantee is a *trust boundary*, not a clever check.
The check (a phrase denylist) is admittedly fragile and incomplete — and that's
fine, because the guarantee doesn't come from the check being perfect. It comes
from three structural facts the check sits inside: (1) the guard is a pure
deterministic function in the request path, so it runs on *every* output
regardless of model quality; (2) the guard is import-fenced away from both the
thing it guards (`pdpl.ai`) and the thing it must not become (the decision
core), so it can't drift into trusting or re-deciding; (3) the worst case is a
fallback to safe deterministic text, never an unsafe message. The eval doesn't
make users safe — the boundary does; the eval only *hardens* the imperfect
check over time. Getting this ordering right is the whole point: don't try to
make the model safe, make the *system* safe and let the model be as bad as it
likes.
