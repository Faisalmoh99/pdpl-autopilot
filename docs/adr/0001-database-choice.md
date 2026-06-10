# ADR-0001: Database Choice — PostgreSQL vs Firestore

- **Status:** Accepted, amended 2026-06-11 (hosting model)
- **Date:** 2026-06-11
- **Deciders:** Faisal (sole engineer)

> ### Amendment (2026-06-11) — hosting model
>
> The hosting model has been changed from **self-hosted PostgreSQL on a Hetzner VPS** to **managed PostgreSQL via Supabase**. Rationale: as a solo developer, the operational burden of self-hosted PostgreSQL — backups, monitoring, upgrades, hardening, on-call — is not a cost I can carry alongside building the product itself.
>
> The core decision in this ADR — **PostgreSQL over Firestore** — is **unchanged**. Only the deployment model and the consequences that flow from it are revised. The original *Decision* and *Consequences* sections are preserved below as written; superseding text appears in the *Amended Decision (2026-06-11)* and *Revised consequences (under the amendment)* sub-sections that follow them.

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

> The **hosting portion** of this decision is superseded by the 2026-06-11 amendment (see *Amended Decision (2026-06-11)* below). The PostgreSQL-over-Firestore core is unchanged. Original text preserved for history:

**PostgreSQL is the primary store for PDPL Autopilot.**

- A single PostgreSQL instance, run via Docker Compose alongside the FastAPI service on a Hetzner VPS for the MVP.
- Tenant isolation is enforced via a `tenant_id` column on every domain table plus query-layer guards. Row-Level Security (RLS) is deferred to a later ADR once the access patterns are concrete.
- The `audit_log` table is append-only at the database level: a dedicated DB role with `INSERT` only, `UPDATE` and `DELETE` revoked; a trigger blocks `TRUNCATE`.

Firestore is rejected as the primary store. It may be revisited — **only via a new ADR** — if a future feature genuinely requires push-to-client real-time, and the polyglot-persistence cost is justified there.

## Amended Decision (2026-06-11)

PostgreSQL remains the primary store. The hosting model changes as follows:

- **Managed PostgreSQL via Supabase** replaces the self-hosted instance on Hetzner.
- **Supabase is used as managed Postgres ONLY.** The application accesses it from FastAPI via **SQLAlchemy + asyncpg** over a standard PostgreSQL connection string. Schema and migrations are owned by me via **Alembic**, version-controlled in this repo.
- We deliberately do **not** use:
  - Supabase's auto-generated REST / GraphQL APIs (PostgREST),
  - the Supabase client SDK for data access (`supabase-js` / `supabase-py`),
  - Supabase Auth.

  This is to preserve the **SQL / relational learning goal** of the project. The technical heart of the product is the deterministic SQL layer — scoring, audit, gap detection — and outsourcing it to a generated API would gut the point of building it. Authentication is implemented in the FastAPI layer (specifics deferred to a later ADR).
- Tenant isolation via `tenant_id` columns and the deferred-RLS plan are unchanged.
- The append-only `audit_log` invariant is still enforced at the database level (revoked permissions on a dedicated role, plus a trigger blocking `TRUNCATE`), expressed as Alembic migrations.

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

### What I additionally give up under the amendment
- **Vendor lock-in to a managed-Postgres provider — Supabase specifically.** The data layer itself stays portable: vanilla SQL + Alembic + SQLAlchemy means migrating to another managed Postgres (Neon, RDS, Cloud SQL) or back to self-hosted is a connection-string change and a re-run of migrations, not a rewrite. But account, billing, dashboard, support flow, and the operational habits I build are now Supabase-shaped — and *that* lock-in is real.
- **Free-tier constraints as a real ceiling.** Database size, egress, backup retention, and the project-pausing-on-inactivity rule are not abstractions; they will bite. The MVP and the 10-tenant eval set fit comfortably; growth beyond that forces a paid tier or a provider move.
- **Some control I would have had self-hosted.** Choice of PostgreSQL major version, available extensions, fine-grained tuning, and *when* an upgrade happens are Supabase's calls within the tier I'm on, not mine.
- **A piece of the original learning goal.** Operating PostgreSQL end-to-end (backups, PITR, pooling, hardening) is genuinely valuable CTO-track knowledge that I am explicitly deferring. I am betting that shipping the product matters more right now than mastering ops from day one.

### New ops burden I am taking on

> Superseded by the amendment. See *Revised ops burden (under the amendment)* below. Original text preserved for history:

- **Backups.** `pg_dump` on a schedule with an offsite copy; WAL archiving for PITR added later. Restore must be tested at least once before any real customer data exists.
- **Connection pooling.** FastAPI + asyncpg connection management; PgBouncer if connection counts get awkward.
- **Monitoring.** Disk usage, connection count, slow-query log, replication lag (if/when a replica is added).
- **Upgrades.** Major-version upgrades (`pg_upgrade`) are a real exercise, not a click.
- **Security hardening.** PostgreSQL bound to the Docker network only; no public `5432`; strong passwords via environment variables; TLS when the connection leaves the VPS.
- **Disaster recovery.** Documented runbook for VPS loss. Hetzner snapshots are not a backup strategy on their own.

### Revised ops burden (under the amendment)

Supabase now owns most of the work I had committed to:

- **Backups** — automatic daily backups on the platform (subject to free-tier retention limits noted below).
- **Connection pooling** — Supabase provides Supavisor (their pooler) in front of Postgres.
- **Major-version upgrades** — managed by Supabase.
- **Security hardening of the host and network** — Supabase's responsibility.
- **Disaster recovery infrastructure** — Supabase's responsibility.

What honestly **remains mine**:

- **Schema and migrations.** All schema changes live in Alembic migrations under version control. The Supabase dashboard is for inspection only; no schema edits via the UI.
- **Free-tier awareness.** The free tier has real limits that affect operations:
  - **Project pausing after ~7 days of inactivity.** A genuine risk for a project I may not touch every week. Mitigation: a scheduled keep-alive (cron / `pg_cron` / GitHub Action), or upgrade to a paid tier before this matters.
  - **Short backup retention** on the free tier. For data I cannot afford to lose, I take an independent `pg_dump` to my own storage on a schedule.
  - **Database size and egress caps.** Track usage and define an upgrade trigger well before hitting them.
- **Connection configuration.** Pooler URL vs direct URL (Alembic migrations require the direct connection), `sslmode=require`, and sensible client timeouts.
- **Key management.** The Supabase **service-role key bypasses RLS entirely** and is effectively a master key for the project. It lives only on the server, only in environment variables, and never reaches a client. Because we are not using the Supabase SDK, no Supabase key needs to touch a browser at all — this is a deliberate simplification of the key-leakage surface.
- **Tenant isolation at the application layer**, until RLS is added in a later ADR.
- **Audit-log immutability.** Revoking `UPDATE` / `DELETE` from the application role, and the anti-`TRUNCATE` trigger, are still on me to author as Alembic migrations — Supabase does not provide these guarantees out of the box.

### Triggers to revisit this decision (updated under the amendment)
- A confirmed product need for real-time push to the user — would re-open the Firestore comparison, not just the hosting choice.
- **Free-tier limits being hit** — database size, egress, backup retention, or project-pausing causing real operational pain. Action: upgrade Supabase tier, or move to another provider.
- **A compliance reason to move off Supabase.** For a *PDPL* product specifically, this is the trigger most likely to fire: SDAIA guidance, or a customer contract, requiring Saudi-region data residency that Supabase cannot offer at the chosen tier. Check this before signing the first paying tenant.
- **Cost crossover** — when a paid Supabase tier exceeds the cost of self-hosting on Hetzner plus an honest estimate of my own ops time. My time is not free; revisit honestly, not aspirationally.
- **A Supabase-specific reliability or outage pattern** that makes the immutability / audit guarantees we owe customers harder to keep than they would be on self-hosted Postgres.

Each would warrant a follow-up ADR, not a quiet migration.
