# ADR-0003: Audit-Log Immutability Mechanism

- **Status:** Accepted
- **Date:** 2026-06-11
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0001 — Database Choice](0001-database-choice.md), [CLAUDE.md — Immutable audit log mandate](../../CLAUDE.md)

## Context

`CLAUDE.md` mandates an *immutable audit log for every check and decision*. [ADR-0001](0001-database-choice.md) made the policy commitment — *"the `audit_log` table is append-only at the database level"* — and named the moving parts in one sentence: an `INSERT`-only role, revoked `UPDATE`/`DELETE`, and a trigger blocking `TRUNCATE`. It did not specify *how those pieces are wired*, *which role does what*, or *what the Supabase hosting model implies for the guarantee*. This ADR fills those gaps so the first Alembic migration can be written confidently and so we can defend the audit guarantee to a regulator.

### What "immutable" means here

A row, once `INSERT`ed into `audit_log`, cannot be modified or removed by the application — including by a compromised application credential — without an explicit administrative action that itself leaves a trace. Immutability is a property of the *table* under the *normal* connection used by the running service. It is not — and cannot be — a property of *every possible* connection to the database, because someone with full superuser privileges can do anything by definition. The goal is to make the application path provably append-only and to keep the privileged path narrow and auditable.

## Decision drivers

- **Defense in depth.** A single mechanism that fails closes the whole guarantee. We want at least two independent gates so a misconfiguration in one is not catastrophic.
- **Cover all destructive verbs.** `UPDATE` and `DELETE` are not enough — `TRUNCATE` is a separate privilege in PostgreSQL and is not covered by `REVOKE DELETE`. So is `DROP TABLE`.
- **Supabase fits inside this story.** Supabase's `service_role` key bypasses **RLS only**; table-level grants and triggers still apply to it. We must not assume `service_role` is a "back door" that defeats this design — but we must also not let our normal service code authenticate as a role that *can* mutate the table.
- **Operability.** A redaction will eventually be required (a user demands erasure of personal data that landed in an audit payload). The mechanism must not paint us into a corner; it must force the redaction into a controlled, audited path rather than a silent UPDATE.

## Decision

A **two-role separation** with **revoked destructive grants** on `audit_log`, plus a **`BEFORE TRUNCATE` trigger** that hard-fails the statement. The application uses one role; schema and policy changes (Alembic migrations, manual ops) use another.

### Roles

| Role | Purpose | Privileges on `audit_log` |
|---|---|---|
| `pdpl_app` | The application's normal connection (FastAPI → asyncpg) | `SELECT`, `INSERT` only |
| `pdpl_migrations` | Alembic migrations and deliberate admin actions | All DDL/DML, as required to evolve the schema |

`pdpl_app` does **not** hold `UPDATE`, `DELETE`, `TRUNCATE`, or `REFERENCES`-via-self on `audit_log`. The grant pattern is:

```sql
GRANT SELECT, INSERT ON audit_log TO pdpl_app;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM pdpl_app;
```

On Supabase, `pdpl_migrations` is the existing `postgres` superuser used for the *direct* (port 5432) connection that Alembic runs against. We do not create a third role; we name the existing one in policy so the boundary is explicit. `pdpl_app` is a new role created by the first migration.

### TRUNCATE trigger (independent gate)

`REVOKE TRUNCATE` covers the role but not the table itself when a future migration mis-grants. The trigger is the belt to the grant's suspenders:

```sql
CREATE FUNCTION audit_log_block_truncate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'TRUNCATE on audit_log is not permitted: append-only invariant (ADR-0003)';
END;
$$;

CREATE TRIGGER audit_log_no_truncate
  BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT
  EXECUTE FUNCTION audit_log_block_truncate();
```

A `TRUNCATE` from *any* role — including `pdpl_migrations` — raises an exception. To genuinely empty the table (for a documented reason), an operator must explicitly `DROP TRIGGER audit_log_no_truncate;` first — an action that itself is loud, deliberate, and reviewable in version control if it ever happens via migration.

### What the migration role can still do

`pdpl_migrations` *can* `UPDATE` or `DELETE` rows in `audit_log` — that's intentional. A migration role unable to alter its own tables cannot fix a structural mistake. The defense is that there is no application path to this role: it is used only by Alembic and only with the direct (non-pooler) Supabase connection string, which never leaves the developer's machine or the CI environment. The application never holds this credential.

### Redactions

If we ever need to redact personal data from an audit payload (e.g., to honor an erasure request), the path is:

1. Insert a new row of `event_type = 'audit.redaction'` recording the redaction (who, when, why, which target row).
2. Update the target row's `payload` column under `pdpl_migrations`, replacing the personal data with a tombstone.

Step 1 is mandatory and append-only. Step 2 is a deliberate administrative action under a credential the application does not hold. The audit trail of the redaction itself is permanent. This flow is sketched here for completeness; its full design is deferred to the future "erasure" ADR flagged in `docs/02-data-model.md`.

## What this does *not* protect against

This ADR is explicit about its boundaries so it isn't oversold:

- **Supabase platform compromise.** If Supabase's control plane is breached, all bets are off. This is in-scope for "triggers to revisit" in ADR-0001, not solvable in the schema.
- **A leaked `pdpl_migrations` credential.** Same risk as any superuser leak. The mitigation is operational (secret storage, rotation, not committing to repo), not structural.
- **A future migration that mis-grants.** A migration that does `GRANT UPDATE ON audit_log TO pdpl_app` undoes the guarantee. The defense is review discipline plus the TRUNCATE trigger — and a periodic schema audit (cheap, scriptable, deferred until we have a CI pipeline).
- **`TRUNCATE` via partitioning** — `audit_log` is not partitioned in the MVP. If we partition it later, each partition needs the trigger or a child policy. To be addressed when partitioning is on the table.

## Consequences

### What we gain
- The application's credentials cannot mutate the audit log under any code path — not even by a SQL-injection vulnerability that escapes parameterization. The mutation simply errors at the DB.
- The TRUNCATE trigger means an accidentally over-broad migration grant still cannot wipe history.
- The redaction path is explicitly designed *and* audited, instead of being whatever-someone-types-into-psql.

### What we give up
- One more role to manage in operations. Connection strings and secrets multiply slightly; the FastAPI service must connect as `pdpl_app`, Alembic must connect as `pdpl_migrations`.
- The TRUNCATE trigger blocks even legitimate test-database resets. Test environments will either drop-and-recreate the schema (cheap) or explicitly drop the trigger as part of the test setup. This is a tiny friction that protects against a much larger silent failure.

### Triggers to revisit
- A move to partitioned `audit_log` (when row count justifies it) — needs the trigger replicated on each partition or restated as a policy on the parent.
- A hosting move off Supabase that exposes additional administrative roles whose grant inheritance differs from Supabase's `postgres` role — re-audit the grant graph at that time.
- A regulator or customer contract requiring a stronger property than "append-only at the database layer" — e.g., a hash-chained or externally-witnessed audit log. This would be additive, not a replacement.
