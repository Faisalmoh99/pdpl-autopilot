# ADR-0001: Database Choice — PostgreSQL vs Firestore

- **Status:** Accepted
- **Date:** 2026-06-11
- **Deciders:** Faisal (sole engineer)

## Context

PDPL Autopilot must persist compliance state for many tenants. Before writing a single line of code, we need to pick a primary store and own that choice with eyes open. The product definition (`docs/product-definition.md`) and `CLAUDE.md` together imply a specific data shape and a specific set of access patterns; the database has to support them natively, not by heroics.

The realistic alternatives are **PostgreSQL** (forced by the relational nature of the domain, but new to me operationally) and **Firestore** (which I already know and which is fully managed).

### Requirements derived from the product definition

1. **Relational entities.** The domain is `tenants`, `controls` (PDPL obligations), `evidence` (uploaded documents, questionnaire answers, scheduled-check results), `findings` (per-tenant × per-control gap status), and an `audit_log`. These have unavoidable cross-references: a finding belongs to a tenant and a control and is supported by zero-or-more evidence rows; an audit-log entry references a decision which references a check which references a control.

2. **Query patterns.**
   - **Per-tenant gap report.** Join `controls × latest_findings × evidence`, filtered by `tenant_id`. *(MVP: initial readiness questionnaire → gap report.)*
   - **Readiness scoring aggregation.** Aggregate weighted finding statuses per tenant into a single numeric score. *(MVP: readiness score.)*
   - **Deadline tracking.** Range queries on remediation deadlines and PDPL's 72-hour breach-notification window, ordered by due date. *(MVP: continuous monitoring + alerts.)*
   - **Cross-tenant analytics.** "Which controls fail most across all tenants" — required to measure the ≥90% gap-detection target and <10% false-positive rate against the synthetic eval set of 10 companies.
   - **Audit-log reads.** Filter and order by `tenant_id` + time range. Rare, but must remain readable indefinitely.

3. **Multi-tenancy isolation.** Every query is scoped to a single tenant by default. A leak of tenant A's evidence into tenant B's gap report is a product-killing bug.

4. **Immutable / append-only audit log.** `CLAUDE.md` mandates an *immutable audit log for every check and decision*. Updates and deletes on `audit_log` must be prevented at the database layer, not only the application layer.

5. **Transactional consistency for deterministic scoring.** `CLAUDE.md` mandates that *deterministic logic decides / scores / classifies*. A score computation must see a consistent snapshot of its inputs — no torn reads where half come from before an update and half from after.

### Honest scoring

| Requirement | PostgreSQL | Firestore |
|---|---|---|
| Multi-entity joins (gap report) | Native, indexed | Not supported server-side; N+1 reads or aggressive denormalization |
| Aggregations for scoring | `SUM`/`AVG`/`GROUP BY`, window functions | Limited (`count`/`sum`/`avg` on simple queries only, no `GROUP BY`) |
| Range + equality filters mixed (deadlines) | Composite indexes, freely combined | Long-standing single-inequality limitation; index gymnastics |
| Cross-tenant analytical queries | First-class SQL | Painful; `collectionGroup` queries are expensive and read-billed |
| Multi-row transactional snapshots | MVCC, `SERIALIZABLE` available | Transactions capped (~500 docs), contention-prone |
| Immutability enforcement | DB-level: `REVOKE UPDATE, DELETE` + triggers | Application-level security rules only (easier to misconfigure) |
| Schema enforcement | Strong types, `CHECK`, `NOT NULL` | Schemaless; wrong field type → silent compliance bug |
| Backup / point-in-time recovery | `pg_dump`, WAL archiving, mature tooling | Managed export; less granular |
| **Real-time listeners** | Polling or `LISTEN/NOTIFY` (manual plumbing) | **Native, first-class** |
| **Ops simplicity** | Self-managed on Hetzner VPS: backups, pooling, upgrades, monitoring | **Fully managed; zero ops** |
| **My existing familiarity** | New for this project — real learning curve | **Already comfortable** — faster early velocity |
| Cost at MVP scale | Fixed VPS cost (~€5–10/month) | Generous free tier; effectively €0 |

### Where Firestore is genuinely the better fit

These are not handwaves. They are real costs of leaving Firestore:

- **Real-time UI updates.** If the dashboard later needs live "compliance status changed" pushes, Firestore gives this for free. PostgreSQL requires building a WebSocket layer or `LISTEN/NOTIFY` plumbing myself.
- **Operational simplicity.** I would write zero lines of backup, monitoring, or connection-pooling code on Firestore. On PostgreSQL I now have to learn and own all of it.
- **My familiarity.** I already know Firestore. Choosing PostgreSQL slows down the first few weeks deliberately.
- **Cost at tiny scale.** Firestore's free tier comfortably covers the synthetic eval set and 10 tenants; a Hetzner VPS is a fixed monthly cost from day one.

### Where PostgreSQL is forced by the requirements

- The data model **is** relational. Controls, evidence, findings, and the audit log have unavoidable cross-references. Firestore would force either heavy denormalization (with update fan-out bugs) or per-page N+1 reads (with cost and latency penalties). Both undermine the *deterministic scoring* principle by making consistency harder to reason about.
- Cross-tenant analytical queries (measuring precision/recall, identifying "controls that fail most") are SQL-native and awkward in Firestore.
- The immutable audit log is a **compliance** requirement, not a logging convenience. DB-level immutability (revoked permissions plus trigger-enforced append-only) is materially stronger than application-level security rules.
- Deterministic scoring needs a transactional snapshot across multiple entities. PostgreSQL MVCC gives this naturally; Firestore transactions are too constrained.
- The MVP's required pattern is **scheduled checks**, not real-time push — which removes Firestore's biggest technical advantage from the MVP critical path.

## Decision

**PostgreSQL is the primary store for PDPL Autopilot.**

- A single PostgreSQL instance, run via Docker Compose alongside the FastAPI service on a Hetzner VPS for the MVP.
- Tenant isolation is enforced via a `tenant_id` column on every domain table plus query-layer guards. Row-Level Security (RLS) is deferred to a later ADR once the access patterns are concrete.
- The `audit_log` table is append-only at the database level: a dedicated DB role with `INSERT` only, `UPDATE` and `DELETE` revoked; a trigger blocks `TRUNCATE`.

Firestore is rejected as the primary store. It may be revisited — **only via a new ADR** — if a future feature genuinely requires push-to-client real-time, and the polyglot-persistence cost is justified there.

## Consequences

### What I gain
- Native joins, aggregations, and range queries that match the real access patterns.
- DB-enforced audit-log immutability — the strongest available guarantee for the most sensitive table.
- Transactional snapshots for deterministic scoring, by default.
- Schema enforcement that catches malformed evidence at write time, not at compliance-report time.
- SQL skills that are transferable to the broader compliance/B2B work I'm building toward.

### What I give up by leaving Firestore
- **Existing familiarity.** Early velocity will be slower while I learn PostgreSQL, asyncpg/SQLAlchemy, and migrations (Alembic).
- **Free, infinite-scale managed service.** I'm now paying a fixed VPS bill from day one.
- **Real-time listeners.** If the dashboard ever needs live push to the browser, I will have to build that plumbing myself.
- **One less moving part.** Firestore would have been one fewer thing in the stack to operate.

### New ops burden I am taking on
- **Backups.** `pg_dump` on a schedule with an offsite copy; WAL archiving for PITR added later. Restore must be tested at least once before any real customer data exists.
- **Connection pooling.** FastAPI + asyncpg connection management; PgBouncer if connection counts get awkward.
- **Monitoring.** Disk usage, connection count, slow-query log, replication lag (if/when a replica is added).
- **Upgrades.** Major-version upgrades (`pg_upgrade`) are a real exercise, not a click.
- **Security hardening.** PostgreSQL bound to the Docker network only; no public `5432`; strong passwords via environment variables; TLS when the connection leaves the VPS.
- **Disaster recovery.** Documented runbook for VPS loss. Hetzner snapshots are not a backup strategy on their own.

### Triggers to revisit this decision
- A confirmed product need for real-time push to the user (not just scheduled alerts).
- Operational load on the PostgreSQL instance exceeding what one person can reasonably babysit.
- A tenant whose data volume materially changes the cost calculus.

Each would warrant a follow-up ADR, not a quiet migration.
