# 2026-06-24 — C4b: explanation HTTP surface (the last component of the runtime path)

Phase 4, Session C4b wires `explain_gap` (built in C4a) to an actual HTTP request, and closes the
control-text faithfulness gap C4a logged. This is the session where the open product decision deferred
since ADR-0009 — a dedicated endpoint vs. enriching the readiness response — finally gets resolved, and
where the rated `GapContext`'s control text becomes provably faithful to runtime.

## What landed (3 separated commits)

1. **`feat(catalog): SEEDED_CONTROLS faithfulness foundation`** — the faithfulness foundation, first.
   - `SeededControl` + `SEEDED_CONTROLS` in `pdpl.catalog` (verbatim mirror of migration 0003's seeded
     control columns) + `control_by_code()` for the endpoint.
   - Migration 0003 refactored from an inline SQL INSERT to a parameterized `bulk_insert` built from a
     frozen `_SEED_CONTROLS` constant (mirroring the C3a 0004 restructure). Replayable, self-contained,
     does not import the catalogue.
   - Drift test (offline, no DB): `_ORIGINAL_SEED_CONTROLS` is the **genuine pre-refactor original**
     captured from `git show main:0003` (NOT re-derived from the new constant — non-circular, the exact
     C3a discipline), asserting (a) the migration constant reproduces it verbatim, (b) the catalogue
     mirrors the migration on every literal column, (c) code sets match both ways, (d) uniqueness.
   - Golden-set control fields proven faithful: a new test rebuilds each case's
     `control_title_ar` / `control_description_ar` / `severity_weight` from `SEEDED_CONTROLS` by code and
     asserts literal equality. The runtime and the rated eval now ground on the **same** control text.

2. **`docs(adr): ADR-0012 explanation HTTP surface`** — resolves the open questions ADR-0009/0011 left,
   and links both ADRs to the resolution.

3. **`feat(api): on-demand gap explanation endpoint`** — `POST /tenants/{id}/explanations` +
   `explain_tenant_gap` application-service + tests + router registration.

## Decisions made this session (and why)

- **Dedicated endpoint, NOT readiness enrichment.** Enrichment would put *N* explanation resolutions on
  the deterministic headline path (even cached: *N* cache reads + *N* re-gates; cold: *N* model calls
  before the report renders) and couple an AI/cache failure to the product's core output. A dedicated,
  lazy endpoint isolates the blast radius, keeps the readiness route literally AI-free, and matches the
  product truth — the owner reads the gap *list* first and wants prose only for the gap they click.
- **POST, not GET — for cacheability semantics, not the side effect.** `GET` is safe + cacheable, so
  intermediaries may cache a response independently of our own cache + gate; an HTTP-cached copy of a
  nondeterministic, gated result could outlive a `prompt_version` bump and never be re-gated. `POST`
  prevents that structurally. (The weaker "it writes a cache row" argument is deliberately not the basis
  — a purist calls the write an implementation detail, and `ON CONFLICT DO NOTHING` makes it idempotent.)
- **Re-derive the structured verdict via the engine; never read the finding, never parse the rationale.**
  The explainer needs the engine's structured `unsatisfied_codes`, which the persisted finding does not
  carry. The endpoint loads the tenant's current answers and runs `build_control_decider(...)` for the
  control — the same engine path `run_check` uses and the C4a identity test proves faithful. Control
  text comes from `SEEDED_CONTROLS`, passed into the **unchanged** pass-in `build_gap_context` (C4a's
  frozen signature is not reopened).
- **The application-service owns `session_scope` (mirrors `run_check`).** There is no request-scoped
  session DI in this codebase; the proven pattern is thin route → service that opens its own
  transaction. `explain_gap` is unchanged — it already takes `session`. The explainer is injected like
  `run_check`'s decider (`Explainer | None`, default `gemini_explainer_from_settings`); a parallel
  test-only `prompt_version` seam exists because the content-hash cache is tenant-agnostic and persistent
  (a test needs a fresh key for a deterministic miss). Validation runs **before** the explainer is built,
  so the 404 paths need no `GEMINI_API_KEY`.
- **No persistence onto `findings.ai_explanation_ar`.** Serve via the C3b content-hash cache (the store
  of record). Persisting onto the finding would recouple lifecycles C3b deliberately separated (SCD
  Type-2 finding rows vs. a content-keyed, tenant-agnostic cache). The column stays a placeholder.
- **`pdpl.api` permitted to import the decision core under contract 1.** Contract 1 walls only the four
  `pdpl.services.*` decision modules from the AI layer; `pdpl.api` is not a source of it, and contract 2
  forbids `pdpl.ai` importing the core, not `pdpl.api`. The composition (engine + `explain_gap` + Gemini)
  lives in `pdpl.api`, outside `pdpl.explanations` (contract 7). **7 contracts kept, 0 broken, none added.**

## Bounded gap logged (not hidden)

- **Re-derive can disagree with the displayed report — user-visible.** The cache key uses the **freshly
  re-derived** status + rationale, not the stored finding's. If the owner changed answers *without
  re-running a check*, the endpoint explains a status that may **differ from what the readiness report
  shows them at that moment** — a user-visible inconsistency, not merely an internal diff. Accepted for
  the MVP (the correct flow is to re-run the check); recorded in ADR-0012 Consequences.

## Tests

- New offline: control drift tests in `test_catalog_seed_drift.py` (row-preservation vs. the genuine
  pre-refactor original, catalogue mirror, code-set parity, uniqueness) and the golden-set control-text
  identity test in `test_eval_golden_set.py`.
- New integration (real Supabase) in `test_api_explanations.py`: miss → `ai_verified` + cache write;
  second call → re-gated cache hit (explainer not re-invoked); the **KEYSTONE end-to-end** — a poisoned
  cache row (unsafe text inserted via the low-level repo, bypassing gate-before-put) is re-gated on read
  and replaced by the fallback, proving the endpoint does not bypass the chokepoint; a compliance
  assertion → `gate_rejected` fallback, never cached. Transport: unknown tenant/control → 404, missing
  body → 422, correlation id echoed.
- Full suite green (**227 passed**); `.importlinter` 7 kept, 0 broken.

## Out of scope (later)

v2 prompt; scheduling / continuous monitoring; auth; real email/WhatsApp channels; real billing;
LLM-as-judge; allowlist-groundedness; any load/scale work (Phase 5). Prompt-version governance remains
the one open question from ADR-0009 not yet resolved.
