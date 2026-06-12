# 2026-06-12 — App foundation + first vertical slice

## What landed

- **ADR-0004** — `docs/adr/0004-application-foundation-and-observability.md`. Settles five decisions: structured logging (structlog), correlation-ID strategy (middleware + contextvars), metrics scope (thin emission abstraction; defer Prometheus), secrets loading (pydantic-settings + SecretStr + fail-fast), and how the runtime authenticates as `pdpl_app`.
- **Migration 0002** — `migrations/versions/0002_pdpl_app_login.py`. `ALTER ROLE pdpl_app WITH LOGIN PASSWORD <from PDPL_APP_PASSWORD env>` + defensive re-issue of the ADR-0003 grant pattern. `audit_log` stays restricted to SELECT + INSERT for the application role.
- **`src/pdpl/`** — FastAPI scaffold:
  - `config.py` — pydantic-settings Settings with SecretStr for the runtime DB URL.
  - `observability/logging.py` — structlog wired to JSON, contextvars merged in, stdlib logging routed through the same formatter, secret-key dropper as defense-in-depth.
  - `observability/correlation.py` — middleware that reads/generates `X-Request-ID` (UUID v7), binds it to structlog contextvars, exposes it to the audit writer via a module-level ContextVar.
  - `observability/metrics.py` — `counter()` / `histogram()` emit structlog events for now. No registry, no Prometheus client.
  - `db/session.py` — async engine as `pdpl_app`, `statement_cache_size=0` so the Supavisor pooler will work without code changes.
  - `db/audit.py` — `write_event()` helper that INSERTs into `audit_log` inside the caller's transaction and stamps the current correlation_id.
  - `errors.py` — global exception handler returns a clean error shape with the correlation_id embedded.
  - `api/health.py` — `GET /health` runs `SELECT 1` against the DB.
  - `api/tenants.py` — `POST /tenants` writes the tenant row + the `tenant.created` audit_log row in one transaction.
  - `main.py` — app factory with lifespan, middleware, and routers.
- **Tests** — 11 passing, against the real Supabase project:
  - `tests/test_health.py` — health + correlation-ID echo.
  - `tests/test_tenants.py` — POST /tenants writes both rows atomically, the incoming `X-Request-ID` is what lands in `audit_log.correlation_id`, validation errors return 422 with the project's error shape.
  - `tests/test_audit_immutability.py` — connecting directly as `pdpl_app`: INSERT succeeds, UPDATE / DELETE / TRUNCATE all fail. **This is the test that converts ADR-0003 from a schema invariant into an application-level guarantee.**
- **Reversibility proven** — `alembic downgrade base` → `alembic upgrade head` ran cleanly, tests pass against the re-migrated DB.

## Decisions worth remembering

- **Role IS the enforcement, not the network path.** ADR-0003 binds because the runtime connects as `pdpl_app`; whether that connection goes through Supavisor (6543) or direct (5432) is a perf/operability choice, not a security one. The forbidden fallback is the runtime ever connecting as `postgres`/owner. Surface this if Supavisor refuses the custom role; never paper over it with a privileged credential.
- **The thin metrics abstraction is real engineering, not a punt.** Call sites are stable today (`metrics.counter("tenant.created")`); the backend changes when Prometheus arrives, with zero call-site churn. An empty Grafana dashboard is decoration. The deferral trigger is recorded in ADR-0004 Consequences: first real tenant, or ~100 req/min, or first real incident.
- **Correlation ID closes the loop in *one* identifier.** Incoming `X-Request-ID` (or generated UUID v7) → structlog contextvar (every log line) → audit_log.correlation_id column (every audit row) → outgoing response header. Verified live: the value `01963b5e-1234-...-cafe` we sent in came back in the response header AND was written into the `audit_log` row by the same request.
- **Statement cache off for asyncpg.** Required by Supavisor transaction-mode pool; harmless on direct. Set unconditionally on the engine so switching to the pooler is a connection-string change only.
- **0001 is read-only history.** Migration 0002 added the LOGIN + password rather than editing 0001. Editing an applied migration is the category of mistake we do not start making in Phase 2.

## What's explicitly deferred (and why)

- **Prometheus + Grafana** — call surface ready; collection stack waits for real traffic. ADR-0004 records the trigger.
- **OpenTelemetry / distributed tracing** — single-process today; correlation ID is enough. Reopen when a second service or external callout matters.
- **Log shipping** — stdout JSON only. Hetzner deploy ADR decides this.
- **Liveness vs readiness split on `/health`** — one endpoint is fine until an orchestrator distinguishes the two.
- **Authentication** — `POST /tenants` is unauthenticated; `audit_log.actor_id` is the string `api:POST /tenants`. Auth gets its own ADR.

## Definition-of-Done check

- [x] ADR for the architectural decision — ADR-0004.
- [x] Logging + correlation ID — structlog JSON, X-Request-ID end-to-end.
- [x] Error handling — global exception handler, clean error shape, correlation_id embedded. Retry-on-external-calls N/A this iteration (no outbound calls yet).
- [x] Tests — 11 passing; the immutability suite is the load-bearing one.
- [x] No secrets in code — `.env` is the source; `SecretStr` + log-processor redaction.
- [x] Build-log entry — this file.

## Honest pieces

- I started with the **direct connection (5432) as pdpl_app**, not the Supavisor pooler, because I don't have the pooler region from the user. ADR-0004 explicitly allows this as a documented fallback — the security property is identical (it's the role, not the port). Switch to the pooled URL whenever we have the region; no code change.
- Tests run against the **real Supabase project**, not a throwaway Postgres. At zero traffic this is the right tradeoff (provisioning a per-run DB is friction we don't need yet). The cost is that `audit_log` accumulates one row per immutability-test run forever, by design — *which is fine, because that is what append-only means.* The trigger to add ephemeral test infrastructure is the same as the Prometheus trigger: first real tenant.
- One test fix-up that's worth noting for future-me: pytest-asyncio creates a new event loop per test by default, but our async DB engine is built once and bound to the loop it first saw. The fix was pinning the loop to session scope in `pyproject.toml`. Default config would have produced a green local run and a red CI run — easy way to lose an afternoon.

## Lessons (my words)

1. I don't blindly trust my Python or the app layer. The real enforcement lives in the
   database: pdpl_app simply lacks UPDATE/DELETE on audit_log (the permission layer), and
   a TRUNCATE trigger blocks the one operation grants can't. The test proves that even if
   the app tried to tamper, it would fail.

2. Atomicity: a tenant and its audit record commit together or not at all, in a single
   transaction. It is engineering- and legally-impossible for a tenant to exist without
   the audit record that documents it.

3. The automated test is what turned the ADR's design promises into real, proven security
   guarantees. We prove our security claims in code, not in slogans.

## Next session candidates

1. SQLAlchemy ORM models matching the migration; flip `target_metadata = Base.metadata` in `migrations/env.py`.
2. Seed the `controls` table from a reviewed PDPL obligation list (its own migration; not autogenerated).
3. Authentication ADR — what guards `POST /tenants` and how `audit_log.actor_id` becomes a real user reference.
4. First scheduled check (`check_runs` + `findings`) — proves the SCD Type 2 transition flow end-to-end.
