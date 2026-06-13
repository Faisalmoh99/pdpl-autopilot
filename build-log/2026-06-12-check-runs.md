# 2026-06-12 — First check_run + SCD Type 2 transition proven

## What landed

- **Migration 0003** — `migrations/versions/0003_seed_controls.py`. Three coupled changes:
  - Renames `controls.title` → `title_en`, `controls.description` → `description_en`; adds `title_ar`, `description_ar` as NOT NULL (added nullable, populated by the seed, then `SET NOT NULL`).
  - Extends `findings.status` CHECK to include `'not_assessed'` (additive; `'unknown'` stays reserved for the future "engine ran but could not decide").
  - Seeds **10 non-authoritative starter controls** covering the most cited PDPL articles (DSR ×3, lawful basis, privacy notice, security measures, 72h breach, retention, cross-border, ROPA). Marked `non_authoritative=true` in the audit_log row the migration writes. Not legal advice; replaced wholesale by the future SDAIA-reviewed catalogue.
- **Check service** — `src/pdpl/services/checks.py`. `run_check(tenant_id, *, kind, decider, correlation_id)` opens a `check_run`, evaluates every active control, writes findings using the SCD Type 2 pattern (first-time → INSERT; same status → skip; different status → close-old + insert-new in ONE transaction), closes the `check_run`. Default `baseline_decider` returns `('not_assessed', "…engine not yet implemented…")`. Tests inject custom deciders.
- **HTTP route** — `src/pdpl/api/checks.py`. `POST /tenants/{tenant_id}/checks` is a thin wrapper; translates `TenantNotFound` → 404. No decider override exposed.
- **Tests** — `tests/test_check_runs.py`, 7 new tests, all green (full suite 18 passing):
  - Baseline run writes one current `not_assessed` finding per active control.
  - Transition closes old + opens new atomically; `old.valid_to == new.valid_from` byte-for-byte.
  - Partial unique index `uniq_findings_current` rejects a second open row via raw `asyncpg` INSERT.
  - Baseline re-run is a no-op (row identities unchanged across three runs).
  - HTTP route threads `X-Request-ID` → `check_runs.correlation_id` → every audit row.
  - 404 for unknown tenant from the route; `TenantNotFound` from the service.
- **Data-model doc** — `docs/02-data-model.md` updated: bilingual columns documented as a noted decision; `'not_assessed'` semantics explained; hand-written migration style confirmed as long-term (autogenerate stays off even when ORM models arrive); four deferred ADRs listed.

## Decisions worth remembering

- **Hand-written migrations are the long-term style.** Even when ORM models land, `target_metadata` stays `None`. Autogenerate cannot reproduce role grants, partial indexes, triggers, or data migrations correctly. Trade-off accepted: ORM ↔ DB drift won't be caught by Alembic. Plan is a schema-diff test when ORM models exist — not autogenerate.
- **`'not_assessed'` is not `'unknown'`.** Two distinct meanings: `not_assessed` = "no engine run yet" (baseline); `unknown` = "engine ran and could not decide" (future). Conflating them would have made the eventual real engine ambiguous. Worth the additive CHECK migration.
- **Two TEXT columns per language, not JSONB.** AR + EN only for the MVP, queries hit these fields directly, EXPLAIN/constraint-checks stay obvious. JSONB or a `control_translations` table becomes proportional only when a third language is real.
- **Postgres `now()` is constant within a transaction — that's the SCD Type 2 atomicity proof.** The transition closes the old row with `valid_to = now()` and inserts the new row with `valid_from = now()` (default), both in the same transaction. They are equal to the microsecond. The test asserts equality on this column to prove the close+insert was a single transaction, in-table — no audit-log archaeology required.
- **The dedup is at the application layer, the safety net is at the DB.** `run_check` skips the write if the decided status equals the current finding's status (per ADR-0002 §82). The partial unique index `uniq_findings_current` is the second wall — a bug that tries to write a duplicate current row fails at the DB with `UniqueViolationError`, not silently.
- **The route doesn't expose the decider.** Production traffic always uses `baseline_decider`. The decider parameter on `run_check` exists for tests. Keeps the production API surface honest about what we can decide today (nothing).
- **Migration writes its own audit row.** `actor_type='migration'`, `actor_id='migration:0003_seed_controls'`, payload carries the codes and `non_authoritative: true`. Same audit pipeline as the application — the seed is traceable end-to-end like any other event.

## What's explicitly deferred (and why)

- **Control-status decision engine (ADR-pending).** The deterministic rules that read `evidence` and yield a real status. Baseline decider stub exists only to exercise the SCD Type 2 mechanics. Trigger to design: when the questionnaire/evidence input model lands.
- **Readiness scoring algorithm (ADR-pending).** How findings + `severity_weight` → a single score. Don't infer it from the starter seed's weights — those were picked by intuition.
- **Questionnaire & evidence input model (ADR-pending).** Drives the decision engine above.
- **Full SDAIA-reviewed control catalogue.** The 10-row starter seed is a working approximation. When the reviewed source exists, it replaces this seed wholesale via a new migration. Not amended in place.
- **Bilingual content beyond AR + EN.** Two-column shape works for two languages. Revisit only when a third language is a real requirement.

## Definition-of-Done check

- [x] Design/ADR for the architectural decision — covered by ADR-0002 (decided previously). Phase 2 implementation notes recorded in `docs/02-data-model.md`.
- [x] Logging + correlation ID — `run_check` emits `check_run.started`, `finding.created`, `finding.transitioned`, `check_run.completed` audit rows, all carrying the request's correlation_id. Test asserts the trace end-to-end.
- [x] Error handling — `TenantNotFound` → 404 with the project's error shape; partial unique index rejects bad writes at the DB layer.
- [x] Tests — 7 new, 18 total passing against the real Supabase project. The transition + partial-unique tests are the load-bearing ones.
- [x] No secrets in code — no new secrets introduced.
- [x] Build-log entry — this file.

## Honest pieces

- The starter seed's 10 controls and severity weights are an **educated guess**, not a reading of the law. The migration says so, the audit row says so, the doc says so — but a future me reading this in 6 months should still know: do not show this to a customer.
- The check service treats `effective_from <= now() AND (effective_to IS NULL OR effective_to > now())` as "active". This is correct per the schema, but it means a control with `effective_from` in the future is silently invisible to `run_check`. That's the right behaviour; flagging it here so it's not a surprise the first time we schedule a future-effective amendment.
- Tests share the live Supabase project (same trade-off as the audit immutability suite). Every test leaves behind a tenant, 10 findings, a few audit rows. By design — append-only history can't be cleaned up, and we have the same trigger to add ephemeral test DBs as we had last session (first real tenant).

## Lessons (Faisal's words)

1. The convenient option (reuse the existing 'unknown' status, skip a migration) would
   have collapsed two genuinely different states into one: "never assessed" vs "assessed
   but indeterminate." We added 'not_assessed' instead. The deciding factor was asymmetric
   risk — an extra enum value is harmless, but un-conflating historical data later is a
   lossy migration. Name a state by what it means, not by what avoids work today.

2. We don't edit events — we record them, and they become history. This holds in two
   places: audit_log is append-only (immutable), and findings use SCD Type 2 (a status
   change closes the old row and opens a new one, never overwrites). State over time is a
   sequence of recorded facts, not a mutable current value.
