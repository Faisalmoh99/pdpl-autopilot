# ADR-0009: AI Gap-Explanation Layer (Explainer port + deterministic verification gate)

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [ADR-0007 — Readiness Scoring Model](0007-readiness-scoring-model.md), [ADR-0008 — Reliable Alerting / Transactional Outbox](0008-reliable-alerting-transactional-outbox.md), [ADR-0010 — AI Explanation Eval Methodology](0010-ai-explanation-eval-methodology.md), [Data Model](../02-data-model.md), [CLAUDE.md — core decision principle](../../CLAUDE.md)

## Context

Phase 4 introduces AI for the first time. The first feature is the **Arabic gap explanation**: the deterministic engine (ADR-0006/0007) has already decided a control's `status`, `rationale`, and `severity_weight` and assembled them into a `GapItem`; the AI's only job is to turn that machine output into a short, human Arabic explanation for a non-technical Saudi business owner — *"why this is a gap, and one step to fix it."*

This is the exact point where the product's core safety line becomes load-bearing for the first time:

> AI **reads / suggests / explains**. Deterministic logic **decides / scores / classifies**. A compliance decision must **never** reach the user directly from an AI output. AI must **never** say "you are compliant."

Until now that line was cheap to hold: there was no AI. The decision core is already mechanically fenced — `.importlinter` forbids `pdpl.services.decision/scoring/checks/alerts` from importing the reserved `pdpl.ai` namespace (which does not yet exist) or any LLM SDK, and `tests/test_architecture.py` fails the build on a violation. Phase 4 must let `pdpl.ai` come into existence **without** weakening that fence, and must add the missing half of the guarantee: a trusted, deterministic check that stands between any AI output and the user.

This ADR decides the architecture of that layer. The eval that measures the explanation's quality — and that *hardens* the gate decided here — is a separate, standalone artifact in [ADR-0010](0010-ai-explanation-eval-methodology.md). **No code ships this session;** this is an ADR-only design decision. Implementation is sequenced across later sessions (see Consequences).

## Decision drivers

- **The safety line is architectural, not statistical.** The user must be safe on *every* output, regardless of how a model scored on an eval. That can only come from a deterministic check in the request path, not from an aggregate metric.
- **Trust boundary clarity.** `pdpl.ai` is the *untrusted producer*. The thing that guards its output cannot live inside it.
- **Reuse the proven Phase-3 patterns.** The Notifier port (Protocol + stub + real + typed errors) and the reliability machinery (single overall `asyncio.timeout`, transient/permanent classification, full-jitter backoff, `SecretStr` never logged) are tested and correct. The AI external call reuses them rather than inventing new ones.
- **A PDPL product must not leak PII.** The design must keep customer personal data out of a third-party LLM call by construction, not by reminder.

## Decision

### 1. The explainer lives behind a port, in `pdpl.ai`

Mirroring the Notifier port (ADR-0008 §3), the explainer is an abstract contract with swappable implementations, all inside the reserved `pdpl.ai` namespace:

```python
# pdpl.ai — the untrusted producer namespace
@runtime_checkable
class Explainer(Protocol):
    async def explain(self, ctx: GapContext) -> str: ...

class StubExplainer:    # deterministic, no network — used by eval + tests
    ...

class GeminiExplainer:  # the real LLM call (built in a later session)
    ...
```

Eval and tests run against `StubExplainer`, so the measurement harness and the whole boundary work before any real LLM exists. `GeminiExplainer` slots into the identical seam later with no change to callers — the same swap discipline the Notifier port already proved.

### 2. `GapContext` is tenant-agnostic by construction — no PII reaches the LLM

The input the explainer receives carries **only** the deterministic, non-personal facts of a gap:

```
GapContext = { control_code, control_title_ar, control_description_ar,
               status, rationale, severity_weight, lang }
```

It **never** carries the tenant's raw questionnaire answers, customer records, or any personal data. The `rationale` (ADR-0006 §4) is already a non-personal, mechanical statement of *what made the status what it is* (e.g. *"privacy notice: 2 of 4 question(s) satisfied; gap(s): …"*). This single design choice does four things at once:

- protects PII — a PDPL product must not be the thing that leaks personal data to a third party;
- keeps the content-hash cache (§6) **leak-free across tenants** — two tenants with the same gap share one cache entry because the content is identical and impersonal;
- sidesteps cross-border personal-data transfer to Gemini for this feature entirely;
- keeps the explanation a pure function of public control text + a deterministic verdict — which is exactly what makes it cacheable and eval-able.

The deferred trigger (revisit cross-border transfer / DPA *if* we ever need to feed PII to the AI) is recorded in Consequences.

### 3. The deterministic verification gate IS the safety guarantee

A **pure function**, living in a **trusted deterministic module outside `pdpl.ai`** — `pdpl.verification`:

```python
# pdpl.verification — TRUSTED, deterministic, no model, no network
def verify_explanation(candidate_text: str, ctx: GapContext) -> VerificationVerdict
```

Putting this guard inside `pdpl.ai` would be a category error: that namespace is the untrusted producer, and a guard cannot live inside the thing it guards. It lives outside, is 100% deterministic, and imports no model and no LLM SDK.

**This function is the safety guarantee for the whole feature.** Any candidate AI text that fails it is **rejected**, and the system falls back to showing the deterministic `rationale` alone. Therefore the user is safe on *every* request, **independent of any eval score** — even a catastrophically bad model can only ever cause a fallback to safe deterministic text, never an unsafe message to the user.

**Starting check set** (deliberately the cheap, high-confidence ones first):

1. **No compliance assertion.** Reject if the text contains compliance-asserting phrasing in Arabic or English (e.g. «أنت ملتزم», «متوافق», «مطابق للنظام», "you are compliant", "fully compliant"). This is the single most important check — it is the literal product safety line. **Be honest about its mechanism:** it is a **curated denylist** — best-effort and fragile to paraphrase. A novel wording the list has not seen (e.g. «نظامك سليم», «ما عليك ملاحظات») can evade «أنت ملتزم»/«متوافق». The list is extended through the gate-bug loop (ADR-0010 §1), so its coverage is **bounded and growing, not complete** — the safety value comes from the reject→fallback machinery on every *caught* phrase, plus the discipline of widening the list whenever the eval finds a miss.
2. **References the control.** The text must reference the gap's control. **Chosen rule:** the text must contain the `control_title_ar` (or a salient keyword from it) **OR** the `control_code` — either satisfies the check. We deliberately do **not** require the raw developer token (`control_code` / `ART12`) on its own: injecting a code into layperson Arabic prose is unnatural, and demanding it would cause **false rejects** of perfectly good Arabic that names the obligation in words — and a false reject artificially depresses `gate_pass_rate`, corrupting the headline metric of the phase. Keying off the Arabic title lets natural prose pass while still forbidding generic, detached text.
3. **Arabic.** The text must be Arabic above an Arabic-character ratio threshold.
4. **Length bounds.** Non-empty and within a sane maximum — no empty output, no runaway generation.

**Deferred (MINOR):** an *allowlist-groundedness* check — "mentions no PDPL article outside this control's known set" — needs structured control→article data that does not exist yet (`controls` has no `article` column; the article is embedded in `code`, ADR-0006 / data model). We do **not** block the gate on building that allowlist. The gate ships with the four checks above; allowlist-groundedness is added when the structured data exists.

### 4. Orchestration lives in the app/API layer — never in the core

The sequence *call explainer → run `verify_explanation` → on failure fall back to the deterministic `rationale`* is **application orchestration**, and it lives in the app/API layer, **not** in `pdpl.services.decision/scoring/checks`. The decision core neither produces nor consumes AI output; it only ever emits the deterministic verdict the explainer later reads. The fallback path is modelled on the worker's discipline (ADR-0008): a failure is caught and turned into a safe outcome, never propagated to the user.

### 5. The real LLM call reuses the Phase-3 reliability patterns

`GeminiExplainer` (later session) is an external API call and is wrapped in the patterns already proven by `WebhookNotifier` / the outbox worker:

- a **single overall wall-clock deadline** via `asyncio.timeout` (connect/write/read do not stack into an unbounded total);
- **typed failure classification** — timeout / connection / 5xx / 429 → transient; 4xx → permanent — reusing the `TransientNotifierError` / `PermanentNotifierError` taxonomy shape;
- **retry with full-jitter exponential backoff** for transient failures;
- the provider key is a **`SecretStr`** added to `config.py`, **never logged** (at most a short fingerprint, as the webhook does);
- a **minimal usage counter** (call count + approximate tokens emitted as a structured log line + a metric). Real billing/quota accounting is deferred — the cache (§6) is the primary cost control.

On exhausted retries or a permanent failure, the orchestration (§4) falls back to the deterministic `rationale`, exactly as a gate rejection does. A failed LLM call is never a failed request.

### 6. Caching: a dedicated content-hash table, lazy on demand

A dedicated table decouples the cached explanation from the finding's SCD Type-2 lifecycle (a finding gets a new row on every change, ADR-0002):

```
ai_explanations(
    content_hash  text PRIMARY KEY,   -- the cache key (below)
    lang          text NOT NULL,
    text          text NOT NULL,      -- the verified explanation (language-tagged by `lang`)
    prompt_version text NOT NULL,
    model         text NOT NULL,
    created_at    timestamptz NOT NULL
)
```

- **Cache key** = `hash(prompt_version, model_version, control_code, status, rationale, lang)`. The explanation is a pure function of these, so the same gap never re-calls the LLM, and a prompt or model change correctly busts the cache.
- **Cache-key invariant:** *every `GapContext` field that can influence the output must be in the key.* `control_title_ar`, `control_description_ar`, and `severity_weight` are **omitted** because they are **static functions of `control_code`** (a control's Arabic text and weight do not vary per request — ADR-0006/0007), which **is** in the key. If any of them ever stops being static per control, it **must** be added to the key.
- **`unsatisfied_questions_ar` (added C3a) is also omitted from the key** for the same reason: it is a **static function of `(control_code, status, rationale)`** — the gap's question codes live in the `rationale` (already in the key, since the `rationale` is hashed) and each code's Arabic prompt is static seed text (`pdpl.catalog`, mirroring migration 0004). So the same gap always yields the same field, and a change to status/rationale already busts the cache. The one thing the key does **not** see is a change to the **seeded question wording itself**: if a question's `prompt_ar` is ever re-seeded, the cached explanation would go stale silently. That is closed by discipline, not by the key — **re-seeding question wording requires bumping `prompt_version`** (which *is* in the key), enforced by the catalogue↔migration drift test and documented in `pdpl.catalog`.
- **`lang` vs the column (resolved):** the MVP is Arabic-only, but to avoid mixing we keep `lang` *in the key* and name the column generically `text` (not `text_ar`). The row is self-describing via its `lang` column, the key already varies by `lang`, and a second language later needs no schema change. (The alternative — drop `lang` from the key and keep `text_ar` — was rejected only to keep `GapContext.lang` meaningful end-to-end.)
- **Tenant-agnostic content** (per §2): the key contains no tenant identifier and no PII, so the entry is safely shared across tenants with an identical gap.
- **Normalize before hashing** (trim/collapse whitespace, Unicode-normalize) so trivially-different-but-equal inputs hit the same entry.
- **Lazy / on-demand**: generated the first time a gap explanation is requested, then served from cache. Precomputing every gap for every tenant is premature.
- **Only verified text is cached.** A candidate that fails the gate (§3) is never written to `ai_explanations`.

This table is distinct from `findings.ai_explanation_ar` (data model): that column remains the place a *finding-attached* explanation could later be persisted after verification, but the **cache** is keyed by content, not by finding identity.

### 7. The guard holds in BOTH directions

`pdpl.ai` now exists, so the architectural fitness function (`.importlinter`, ADR-0006 §2 / `tests/test_architecture.py`) gains a second contract. Both must hold:

1. **(existing) Core cannot import the AI layer.** `pdpl.services.decision/scoring/checks/alerts` may not import `pdpl.ai` or any LLM SDK — the verdict path stays provably AI-free.
2. **(new) The AI layer cannot import the decision core.** `pdpl.ai` may not import `pdpl.services.decision`, `pdpl.services.scoring`, or `pdpl.services.checks`. The AI *reads deterministic outputs as data* (a `GapContext` handed to it by the orchestration layer); it must not be able to import the decision machinery and thereby recompute or feed back into a verdict. This closes the back door the one-directional fence left open.

`pdpl.verification` is the **trusted** module and is intentionally importable by both the runtime orchestration layer and the eval (ADR-0010) — it is the *one* shared verifier. It imports neither `pdpl.ai` nor the decision core.

## Consequences

**Positive**

- The product's safety line stops being a convention and becomes a deterministic function in the request path: the user is safe on every output regardless of model quality, because the worst case is a fallback to deterministic text.
- The trust boundary is explicit and mechanically enforced in both directions — the untrusted producer (`pdpl.ai`), the trusted guard (`pdpl.verification`), and the AI-free decision core are three separate, import-linted regions.
- PII never reaches the LLM by construction, which simultaneously protects personal data, keeps the cache leak-free across tenants, and avoids a cross-border transfer question for this feature.
- The explainer-behind-a-port + content-hash cache let the eval (ADR-0010) run and produce numbers against a stub *before* the real LLM exists, and let the same harness re-run against Gemini later for a direct comparison.

**Negative / accepted**

- **The bare-`rationale` fallback is a degraded UX.** When the gate rejects or the LLM fails, the user sees the deterministic `rationale` — English-ish technical text (e.g. *"privacy notice: 2 of 4 question(s) satisfied; gap(s): …"*), not polished Arabic prose. Acceptable for the MVP: degraded but safe and honest. A deterministic Arabic fallback **template** is deferred (trigger below).
- **The compliance-assertion check is a curated denylist — coverage is bounded, not complete.** A novel or paraphrased compliance assertion the list has not yet seen (e.g. «نظامك سليم», «ما عليك ملاحظات») can slip the gate until it is added. The exposure is bounded — not eliminated — by two things: the reject→fallback machinery fires deterministically on every phrase the list *does* hold (proven by the keystone test, ADR-0010 §5), and the gate-bug loop (ADR-0010 §1) turns every miss the eval finds into a permanent addition. This is the same honesty applied to allowlist-groundedness: a known, bounded gap, surfaced not hidden.
- **The gate starts without allowlist-groundedness.** Until structured control→article data exists, the gate cannot reject a *plausibly-worded but fabricated* article reference; it only catches the cheap, high-confidence failures (§3). This is a known, bounded gap, surfaced not hidden.
- **A second `.importlinter` contract is a second thing to keep green.** Accepted — it is the cost of letting `pdpl.ai` exist while the safety guarantee holds.

**NOT built this session (ADR-only), and the build sequencing**

- This session: **ADR-0009 + ADR-0010 only. No code, no port classes, no migration.**
- Next session: `pdpl.ai` namespace + `Explainer` port + `StubExplainer`, `pdpl.verification` with `verify_explanation`, the eval harness + golden set (ADR-0010), and the second `.importlinter` contract — run the eval against the stub and **produce numbers before the real LLM exists**.
- Following session: `GeminiExplainer` (the real call) + the reliability wrapper + the `ai_explanations` cache table + the usage counter — re-run the same eval against the real model and compare.
- Later: wiring the on-demand explanation into the findings/HTTP layer.

**Deferred triggers (stated honestly)**

- **PII / cross-border:** *if* we ever need to feed PII (e.g. a raw uploaded document or customer answers) to the AI, revisit cross-border personal-data transfer and a DPA with the provider, alongside the existing data-residency posture. The current design deliberately avoids this for gap explanation.
- **Degraded fallback:** when the bare-`rationale` UX proves too rough in front of real users, build a deterministic Arabic fallback template (still no AI — a static, per-status Arabic sentence).
- **Allowlist-groundedness:** add the article-allowlist check to the gate once `controls` carries structured article data.
- **LLM-as-judge:** deferred and advisory-only / non-load-bearing (ADR-0010); never a release gate.
- **No scheduler / continuous monitoring** in this phase — explanation is generated on demand for an already-computed gap, not on a schedule.

## Open questions (deferred)

- **Where the on-demand explanation is triggered** (a dedicated endpoint vs. enriching the existing readiness/gap-report response) — an API-shape decision deferred to the wiring session. **RESOLVED in [ADR-0012](0012-explanation-http-surface.md) §1: a dedicated `POST /tenants/{id}/explanations`.**
- **Prompt-version governance** — how `prompt_version` is bumped and reviewed when the prompt changes. For now it is a constant in `pdpl.ai`; a lightweight process can come when prompts change often. (Still open.)
- **Persisting into `findings.ai_explanation_ar`** vs. serving purely from the content-hash cache — decided when the wiring lands; the cache is the source of truth for now. **RESOLVED in [ADR-0012](0012-explanation-http-surface.md) §6: no persistence; serve via the cache, the column stays a placeholder.**
