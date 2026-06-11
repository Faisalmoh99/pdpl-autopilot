# ADR-0002: Findings History Model — Append-Only (SCD Type 2)

- **Status:** Accepted
- **Date:** 2026-06-11
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0001 — Database Choice](0001-database-choice.md)

## Context

A `finding` is the per-tenant × per-control verdict produced by a deterministic check: *"as of run X at time T, tenant A is `non_compliant` with control C, due to rationale R."* The MVP must answer not only **"where do I stand today?"** but also **"what changed, and when?"** — the latter is the core value proposition of *continuous monitoring*, and the trigger for the alert pipeline described in `docs/product-definition.md`.

This ADR decides how findings are stored over time. The decision shapes every downstream query: gap reports, scoring, alerting, the audit story, and the eventual analytics for the eval set.

### Requirements

1. **Latest state per (tenant, control)** must be fast — it backs the most common query (the gap report) and the readiness-score aggregation.
2. **Historical state** must be reconstructable for any past moment — required for the "compliance status changed on day D" alert and for defensible answers to audits ("show me my state on the day of the incident").
3. **Only one current finding** may exist per (tenant, control) at any time — enforced at the DB layer, not by application discipline.
4. **No silent overwrites** — a status change must leave a trail. The audit log records that *something* happened; the `findings` table itself should make the *before/after* directly inspectable without joining audit JSON payloads.
5. **Transactional consistency** with the check that produced it — a finding is born of a `check_run`, and that linkage must hold even across status transitions.

## Options considered

### Option A — Latest-only (UPDATE in place)

One row per (tenant_id, control_id). When status changes, `UPDATE` it.

- **Pros:** simplest possible model; queries are trivially `SELECT … WHERE tenant_id = ?`; minimal storage.
- **Cons:** **destroys history.** "When did this become non-compliant?" cannot be answered from the table; we'd have to reconstruct it from the audit log JSON — slow, fragile, and ties product features to a forensic table. Continuous monitoring's "compliance changed" alert has no native data shape to read from. Scoring trends over time become impossible without a parallel snapshots table.

### Option B — Append-only with `valid_from` / `valid_to` (SCD Type 2)

Every status transition is a new row. The previous row stays, with `valid_to` set to the transition timestamp. The "current" state for (tenant, control) is `WHERE valid_to IS NULL`.

- **Pros:** Full history is a first-class table fact, not a forensic exercise. The alert pipeline reads new rows directly. Scoring at any historical point is a single `WHERE valid_from <= T AND (valid_to IS NULL OR valid_to > T)`. Append-mostly write pattern is friendly to Postgres MVCC and matches the audit philosophy of the project.
- **Cons:** Queries for "the current state" need a `WHERE valid_to IS NULL` filter on every read — minor cognitive tax, mitigated by a clearly named view or a partial index. Storage grows with change frequency (acceptable: even on a noisy check we expect transitions in single-digit-per-control-per-year).

### Option C — Hybrid (current_findings + findings_history tables, trigger-synced)

A `current_findings` table holds one row per (tenant, control); a `findings_history` table holds the closed past rows; a trigger moves rows on update.

- **Pros:** Reads of "current" are slightly faster (no filter), and history queries are physically separated.
- **Cons:** Two tables to keep in sync, with the sync logic in a trigger — exactly the kind of cleverness that hides bugs. Doubles the surface for index/constraint mistakes. The performance edge over Option B is invisible at MVP scale and recoverable later by partitioning if we ever need it.

## Decision

**Option B — append-only with `valid_from` / `valid_to`.**

### How it works

- A finding is created by `INSERT` only. `valid_from = now()`, `valid_to = NULL`.
- A status transition is two operations **in a single transaction**:
  1. `UPDATE findings SET valid_to = now() WHERE id = <previous_current_id>`
  2. `INSERT INTO findings (…, valid_from = now(), valid_to = NULL, …)` referencing the new `check_run_id`.
- Both rows are tied to `check_run_id`s, so we can always trace which run *opened* and which run *closed* a state.

### DB-enforced invariants

- **Partial unique index** `uniq_findings_current ON (tenant_id, control_id) WHERE valid_to IS NULL` — guarantees that at any moment there is **at most one** current finding per (tenant, control). An application bug that tries to insert a second open row fails at the DB layer.
- **CHECK** `valid_to IS NULL OR valid_to > valid_from` — closed intervals must move forward in time.
- **Forbidden** at the schema layer: deleting findings. (Application policy; not enforced as a grant in this ADR — that protection is currently reserved for `audit_log` per [ADR-0003](0003-audit-log-immutability.md). If needed later, revoking `DELETE` from `pdpl_app` on `findings` is one-line.)

### Query patterns

| Pattern | Query shape |
|---|---|
| Current gap report | `WHERE tenant_id = ? AND valid_to IS NULL` |
| State as of past time T | `WHERE tenant_id = ? AND valid_from <= T AND (valid_to IS NULL OR valid_to > T)` |
| Change feed for alerts | `WHERE tenant_id = ? AND valid_from > <last_seen>` |
| "Which controls fail most across tenants" | `WHERE valid_to IS NULL AND status IN (...)` — partial index on `(control_id, status)` |

## Consequences

### What we gain
- History is a property of the table, not a property of the audit log. Product features ("you became non-compliant 3 days ago") are SQL queries, not log-replay.
- Defensible answers in an audit: "on date D, the system's recorded state was X" — single query, no JSON archaeology.
- Alerts on change become a `LISTEN/NOTIFY` or polling pattern over a single column (`valid_from`), not a JSON diff.
- A bug that tries to write a duplicate current finding fails *at the DB*, with a `unique_violation` we can route, log, and surface — instead of corrupting the gap report quietly.

### What we give up
- Every "current state" read carries a `WHERE valid_to IS NULL` filter. We will encode this in a SQL view (`v_findings_current`) and in the data-access layer so the filter isn't forgettable.
- Storage grows by ~1 row per (tenant, control) per transition. At 10 tenants × ~50 controls × <10 transitions/year, this is invisible. If a runaway check ever flips a finding on every run, the partial unique index will not stop that — only application-side deduplication (compare new status to current before inserting) will. This is the right shape; we add the dedup at application level.
- Two rows in a single transaction increases the chance of a partial write if we ever code the transition outside a transaction. Discipline: the data-access layer exposes a single `transition_finding()` operation that wraps both statements.

### Triggers to revisit
- A measured query-latency problem on the partial unique index at 10⁶ rows — at which point partitioning `findings` by `tenant_id` (or by `valid_to IS NULL`) is the next move, not Option C.
- A product need for materially different "current" semantics — e.g., a finding that is *simultaneously* in multiple states for different audiences (regulator vs internal). Not foreseen in the MVP.
- A regulatory ask for cryptographically signed history (hash chain) — would augment, not replace, this model.
