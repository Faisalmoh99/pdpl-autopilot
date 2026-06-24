# ADR-0012: Explanation HTTP Surface

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0009 — AI Gap-Explanation Layer](0009-ai-gap-explanation-layer.md), [ADR-0011 — Runtime Explanation Orchestration](0011-runtime-explanation-orchestration.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [ADR-0007 — Readiness Scoring Model](0007-readiness-scoring-model.md), [CLAUDE.md — core decision principle](../../CLAUDE.md)

## Context

ADR-0009 designed the gap-explanation feature and ADR-0011 built the runtime
orchestration core (`explain_gap` — cache + re-gate + fallback, returning
`ExplanationResult`). Both deliberately left the **HTTP surface** open: where the
explanation is triggered, how a request-scoped DB session reaches `explain_gap`,
whether to persist onto `findings.ai_explanation_ar`, and the source of the
control's static text. ADR-0011 §1 also deferred the control-text *sourcing* to
this session.

This ADR resolves those open questions. The control-text faithfulness gap is
already closed in code (C4b commit 1: `pdpl.catalog.SEEDED_CONTROLS`, drift-pinned
to migration 0003, with the golden set's control fields proven faithful to it).
This ADR decides the endpoint that consumes it.

## Decision drivers

- **The readiness report is the product's deterministic headline.** It must stay
  fast and AI-free at runtime; an AI/cache failure must never degrade it.
- **The safety chokepoint (ADR-0011) must be the only path to a user-facing
  string** — the endpoint composes the engine with `explain_gap`, it does not
  open a second path around the gate.
- **Reuse the proven request patterns** — the thin-route + service-owns-session
  shape (`run_check`, `readiness_report`) and the test-only injection seam
  (`run_check(decider=...)`), not a new DI mechanism.
- **`pdpl.explanations` stays free of the decision core** (ADR-0011 §7, contract
  7). The composition that wires the engine to `explain_gap` lives *outside* it.

## Decision

### 1. A dedicated endpoint, lazy / on demand — NOT readiness enrichment

`POST /tenants/{tenant_id}/explanations` with body `{control_code}`, returning
`{control_code, text, source, reason}`. One gap explained per call, on demand
(when the owner drills into a specific gap), **not** every gap inlined into the
readiness response.

Rejected alternative — **enriching the readiness response** (every gap carries
`ai_explanation_ar` inline):

- **Latency:** it turns one fast deterministic read into *N* explanation
  resolutions. Even fully cached, that is *N* cache reads + *N* re-gates on the
  readiness path; on a cold cache it is *N* Gemini calls before the report
  renders.
- **Failure-coupling:** an explainer error or a `cache_regate_failed` anomaly
  would sit inside the product's headline deterministic output. Keeping the
  explanation on its own endpoint isolates that blast radius — the readiness
  report cannot be degraded by the AI path. It also keeps the readiness route
  literally AI-free (it never imports `pdpl.ai` / `pdpl.explanations`),
  preserving the CLAUDE.md line at the transport layer, not just in the core.
- **Cost & product truth:** the owner reads the gap *list* first and wants prose
  only for the gap they click. Precomputing every explanation for every tenant
  is the premature work ADR-0009 §6 already rejected for caching.

**Commits us to:** the client makes a second call per gap on demand, and the
first explanation of each distinct gap pays a cold-start (model call) before it
is cached. Both are acceptable for the drill-down interaction.

### 2. POST, not GET — for cacheability semantics, not the side effect

The operation is `POST` because `GET` carries **safe + cacheable** HTTP
semantics: intermediaries (browsers, proxies, CDNs) may cache a `GET` response
*independently of our own cache and gate logic*. That is wrong for an operation
whose result comes from a **nondeterministic model call** behind a safety gate —
an HTTP-cached copy could outlive a `prompt_version` bump or a gate-policy change
and be served without ever passing `verify_explanation` again. `POST` is
non-cacheable by default, so it structurally prevents that.

(The weaker argument — "it writes a cache row, so it has a side effect" — is
deliberately *not* the basis: a purist rightly calls the cache write an
implementation detail, and `ON CONFLICT DO NOTHING` even makes the write
idempotent. The load-bearing reason is the cacheability semantics above.)

### 3. The trigger: re-derive the structured verdict via the engine

The endpoint takes `(tenant_id, control_code)` and rebuilds the **structured**
verdict at request time — it does **not** read the persisted finding, and it
**never** parses codes out of the formatted `rationale` string (ADR-0011 §1):

```
answers   = load_tenant_answers(session, tenant_id)        # inside the txn
decision  = build_control_decider(answers)(control_code)   # ControlDecision (structured)
control   = catalog.control_by_code(control_code)          # SEEDED_CONTROLS (C4b commit 1)
ctx       = build_gap_context(                              # the C4a pure assembler
                control_code=control_code,
                control_title_ar=control.title_ar,
                control_description_ar=control.description_ar,
                severity_weight=control.severity_weight,
                status=decision.status,
                rationale=decision.rationale,
                unsatisfied_codes=decision.unsatisfied_codes,
            )
result    = explain_gap(session, ctx, explainer, model=...)
```

This is exactly the path the C4a identity test proves faithful to the golden set
(`source_answers → build_control_decider → unsatisfied_codes → build_gap_context`).
Re-deriving — rather than reading the finding — is the **only** way to obtain the
structured `unsatisfied_codes` the explainer needs without re-parsing the
formatted rationale.

**Control-text source (ADR-0011 §1 open item, resolved):** `SEEDED_CONTROLS` via
`catalog.control_by_code`, supplied by the endpoint and passed into the
*unchanged* pass-in `build_gap_context` signature. We do **not** read the DB
`controls` row and do **not** reopen the C4a frozen signature.

### 4. The request-scoped session: the application-service owns `session_scope`

There is no FastAPI request-scoped session dependency in this codebase; the
proven pattern is **thin route → service function that opens its own
`session_scope`** (`run_check`, `readiness_report`). C4b mirrors it: an
application-service coroutine in the endpoint layer opens one `session_scope`,
does the read + re-derive + `explain_gap` inside that one transaction, and the
route is the thin HTTP wrapper over it. `explain_gap` is **unchanged** — it
already takes `session` as a parameter (C4a built it for exactly this).

The explainer is injected the same way the decider is in `run_check`: the service
takes `explainer: Explainer | None = None`, defaulting to
`gemini_explainer_from_settings(get_settings())`; the route does **not** expose
the override (production always uses the default), and tests pass a
`StubExplainer` while still exercising the real session.

### 5. Module layout & the contracts (no new contract)

The composition lives in **`pdpl.api`** (`pdpl/api/explanations.py`: the router +
the application-service), **outside** `pdpl.explanations` — which is precisely
what contract 7 (`explanations-no-decision-core`, ADR-0011 §7) requires. The
endpoint legally imports the decision core (`build_control_decider`), the catalog
(`control_by_code`), `pdpl.explanations` (`build_gap_context`, `explain_gap`), and
`pdpl.ai` (`gemini_explainer_from_settings`).

**`pdpl.api` is permitted to import the decision core under contract 1.**
Contract 1 (`deterministic-core-no-ai`) walls only `pdpl.services.decision`,
`pdpl.services.checks`, `pdpl.services.scoring`, `pdpl.services.alerts` from the
AI layer — `pdpl.api` is **not** a source module of that contract, and contract 2
(`ai-no-decision-core`) forbids `pdpl.ai` importing the core, not `pdpl.api`. So
the composition is legal and the **seven existing contracts stay green** — no new
contract is added.

### 6. No persistence onto `findings.ai_explanation_ar` — served via the cache

The explanation is **not** written back to the finding row. The `ai_explanations`
content-hash cache (C3b) stays the store of record, and the endpoint serves from
it via `explain_gap`. Persisting onto the finding would **recouple** the two
lifecycles C3b deliberately separated:

- a finding is SCD Type-2 — a new row on every status change (ADR-0002) — so a
  finding-attached explanation would need re-attaching on every transition;
- the cache is **content-keyed and tenant-agnostic**: two tenants with the
  identical gap share one entry — a per-finding column cannot;
- re-derive + cache-read is cheap and is already the source of truth.

`findings.ai_explanation_ar` **remains** as the data-model placeholder ADR-0009 §6
described (a possible future finding-attached explanation), but C4b does not
write it.

## Consequences

**Positive**

- The readiness report stays fast, deterministic, and AI-free; the explanation
  path's failures are isolated to its own endpoint.
- The safety chokepoint stays single: the endpoint composes the engine with
  `explain_gap`, so every user-facing string still passes the gate (fresh or
  re-gated on a cache hit).
- The trust boundary is unchanged — composition in `pdpl.api`, `pdpl.explanations`
  provably free of the core, seven contracts green, no new contract.
- The C3b cache/finding decoupling is preserved.

**Negative / accepted (bounded gaps, surfaced not hidden)**

- **Re-derive can disagree with the displayed report (user-visible).** The cache
  key uses the **freshly re-derived** `status` + `rationale`, not the stored
  finding's. If the owner changed their answers *without re-running a check*, the
  explanation reflects a status that may **differ from what the readiness report
  shows them at that moment** — a user-visible inconsistency, not merely an
  internal diff. Accepted for the MVP: the correct flow is to re-run the check
  (which updates the finding), and the window is small. Revisit if it confuses
  real users (e.g. explain strictly from the persisted finding once structured
  codes are persisted alongside it).
- **Cold-start latency on the first explanation of each distinct gap.** One model
  call before the cache is warm; subsequent identical gaps (any tenant) are
  served from cache. Acceptable for a drill-down.
- **A second client round-trip per gap.** The cost of not inlining; deliberate.

## Resolved from ADR-0009 / ADR-0011 (open questions, now closed)

- **HTTP surface** — dedicated `POST /tenants/{id}/explanations` (§1, §2).
- **Trigger + request-scoped session** — re-derive via the engine; the
  application-service owns `session_scope`; `explain_gap` unchanged (§3, §4).
- **Persisting into `findings.ai_explanation_ar`** — no; serve via the cache,
  column stays a placeholder (§6).
- **Control-text source** — `pdpl.catalog.SEEDED_CONTROLS`, passed into the
  unchanged `build_gap_context` (§3; foundation landed in C4b commit 1).
