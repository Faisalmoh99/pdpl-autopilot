# 2026-06-11 — Data model + first migration

## What landed
- `docs/adr/0002-findings-history-model.md` — append-only SCD Type 2 for `findings`.
- `docs/adr/0003-audit-log-immutability.md` — two-role separation + `BEFORE TRUNCATE` trigger.
- `docs/02-data-model.md` — entity reference, ERD (mermaid), invariants, indexing strategy, deferred decisions.
- Alembic scaffolding: `pyproject.toml`, `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `.env.example`.
- `migrations/versions/0001_initial_schema.py` — first migration: 7 tables + indexes + `pdpl_app` role + grants + TRUNCATE trigger.

## Decisions worth remembering
- **Findings history = SCD Type 2.** New row per status transition; `valid_to IS NULL` is the current state. A partial unique index on `(tenant_id, control_id) WHERE valid_to IS NULL` enforces "one current finding per tenant × control" at the DB layer. History becomes a SQL query, not log archaeology.
- **Audit-log immutability = role separation + trigger.** `pdpl_app` has only `SELECT`+`INSERT` on `audit_log`; `pdpl_migrations` (the existing Supabase `postgres` superuser used by Alembic) has full DDL. A `BEFORE TRUNCATE` trigger errors on any TRUNCATE, even from the migration role. Belt and suspenders.
- **UUID v7, app-generated.** No DB extension dependency; better B-tree locality than v4 for the hot append-only tables. `id` columns carry no DB DEFAULT.
- **Two distinct connection strings.** `DATABASE_URL_DIRECT` (port 5432, psycopg2 sync) for Alembic; `DATABASE_URL` (pooler, asyncpg) for the future FastAPI app. The pooler cannot run DDL.

## What's explicitly deferred (and why)
- **Authentication / users.** No `users` table; `audit_log.actor_id` is `text` so the eventual join is forward-compatible.
- **Erasure (PDPL right to be forgotten).** Needs its own ADR — storage backend, audit-log redaction semantics, cascading effects. No `deleted_at` columns added now to avoid baking in a half-design.
- **RLS.** Per ADR-0001. Indexes are ordered `(tenant_id, ...)` so they remain useful once RLS lands.
- **Control catalogue seed data.** First migration creates an empty `controls` table. Seeding happens in its own migration once the obligation list is reviewed against the SDAIA source.
- **SQLAlchemy ORM models.** Will arrive with the FastAPI app layer. Until then, `target_metadata = None` in `env.py` and migrations are hand-written — appropriate for a first migration full of grants, triggers, and partial indexes that autogenerate would not produce correctly.

## Definition-of-Done check
- [x] Has a design/ADR — three of them (ADR-0002, ADR-0003, plus the data-model doc).
- [ ] Logging + correlation ID — not yet wired (no app code). `correlation_id` columns are reserved in `check_runs` and `audit_log`.
- [ ] Error handling + retry on external calls — N/A this iteration.
- [ ] Tests / eval — N/A this iteration. First test target: a migration smoke test that runs `upgrade()` then `downgrade()` against a disposable database. Owed before any application code lands on top.
- [x] No secrets in code — `.env.example` only; `.env` is git-ignored.
- [x] Build-log entry — this file.

## Reflection — what actually made the Postgres call earn its keep

Easy reading of ADR-0001: *"the data is relational, so Postgres."* That's a preference dressed up as logic — it would have produced the same conclusion regardless of the MVP's shape.

The real decisive beat, the one that converts the decision from preference to judgement, is the one in the ADR's "Where PostgreSQL is forced by the requirements" section: **I checked where Firestore is *actually better* (native real-time push to the UI) and found that this advantage is *outside the MVP critical path*** — the product specification calls for **scheduled checks + alerts**, not live browser updates. So the strongest reason to keep Firestore evaporated against the actual requirements, not against my comfort zone.

The general principle worth carrying forward, stated honestly:

- **Not** "always pick the harder tool."
- **Not** "relational always wins."
- **Yes** "decide on the shape of the problem — including checking, deliberately, where the *other* option is stronger, and whether that strength applies here." Sometimes the answer will be the familiar tool. This time it wasn't.

The same discipline applies to the data-model decisions in this session: SCD Type 2 won not because "history is good" but because *continuous monitoring* is the product, and the alert pipeline reads from change events — making history a first-class table fact, not a forensic side-table.

## Next session
1. Wire FastAPI skeleton + the `src/pdpl/db/` package.
2. Add SQLAlchemy ORM models matching the migration; flip `target_metadata` in `env.py`.
3. Write the migration smoke test (upgrade → downgrade → upgrade against a throwaway Postgres).
4. Configure the FastAPI runtime to connect as `pdpl_app` (not `postgres`) so the audit-log immutability is *enforced* on the application path, not just *defined*.
