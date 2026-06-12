# ADR-0004: Application Foundation and Observability

- **Status:** Accepted
- **Date:** 2026-06-12
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0001 — Database Choice](0001-database-choice.md), [ADR-0003 — Audit-Log Immutability](0003-audit-log-immutability.md), [CLAUDE.md — observability mandate (rule 3)](../../CLAUDE.md)

## Context

Phase 1 produced the data layer — seven tables, the `pdpl_app` role with revoked destructive grants on `audit_log`, the `BEFORE TRUNCATE` trigger, and Alembic migration `0001_initial_schema`. The schema *defines* the append-only invariant. Nothing yet *enforces* it on the application path because there is no application yet.

This ADR settles the five decisions that have to be made before the first line of FastAPI code lands, so that observability is wired in from request one and so that the audit-log guarantee from ADR-0003 actually binds the running service — not just the schema diagram.

`CLAUDE.md` rule 3 mandates *structured (JSON) logging + a correlation ID per request + metrics. No `print`.* This ADR translates that mandate into specific, defensible choices, and is explicit about the parts of "observability" we are **not** standing up yet and the trigger that would change that.

## Decision drivers

- **One identifier end-to-end.** A single correlation ID must thread `request log line → service log line → DB query log line → `audit_log.correlation_id` column → response header`. Without that, "trace this user action" becomes log archaeology.
- **The role is the enforcement, not the network path.** Per ADR-0003, audit-log immutability is enforced by the *role* the application authenticates as, not by which port or pooler the connection traverses. The runtime config must make it impossible for the running service to ever hold the `pdpl_migrations` credential.
- **Observability without theater.** The mandate is real but the project is at zero traffic. Standing up a Prometheus + Grafana stack today would consume a day's work to watch an empty dashboard. The honest move is to instrument the *emission points* now (so call sites are stable) and defer the *collection stack* until there is something to look at.
- **Fail loud at boot, not in production.** Missing secrets, malformed URLs, and unreachable databases should crash the process during startup. Discovering them mid-request is worse than the noise of a hard boot failure.

## Decision

### 1. Structured logging — **`structlog`**

`structlog` is the logging library. Concretely:

- A `configure_logging()` call at app startup wires `structlog` with the following processor chain (production):
  1. `structlog.contextvars.merge_contextvars` — pulls `correlation_id` and any other `bind_contextvars`'d keys into every event.
  2. `structlog.processors.add_log_level`
  3. `structlog.processors.TimeStamper(fmt="iso", utc=True)`
  4. A custom processor that drops any key whose name matches `password|secret|token|api_key` (defense-in-depth against accidental leakage).
  5. `structlog.processors.JSONRenderer()`
- `stdlib` `logging` is configured to route through the same processors so that logs from SQLAlchemy, Uvicorn, FastAPI, and asyncpg emerge as the same JSON shape.
- `print(...)` is banned by convention; if a real linter rule is wanted later, add `flake8-print`. Not enforced via tooling in this iteration.

**Why not loguru:** ergonomic but opinionated; its API forces every other library through a `propagate=True` shim and reframes config in a way that fights stdlib integration. The benefit (cleaner caller-side syntax) does not offset the integration cost.

**Why not stdlib + JSON formatter only:** workable, but the `contextvars` plumbing for the correlation ID would have to be done by hand on every logger via `logging.Filter`. `structlog` ships `merge_contextvars` as a one-liner. Saves real code on a feature we *must* get right.

### 2. Correlation ID — middleware + `contextvars`

A FastAPI middleware sits early in the chain and does:

1. Read incoming `X-Request-ID` header.
2. If absent or malformed, generate a fresh UUID v7 (`uuid6.uuid7()`) — same identifier strategy as the rest of the system (see `docs/02-data-model.md`).
3. `structlog.contextvars.bind_contextvars(correlation_id=...)` so every log line in this request carries it automatically.
4. Store the value in a module-level `correlation_id_var: ContextVar[UUID | None]` so non-logging code (specifically the audit-log writer) can read it without it being passed through every function signature.
5. Echo it back as `X-Request-ID` on the response.
6. On exit, `clear_contextvars()` to keep no state across requests.

The audit-log writer **always** reads the correlation ID from the contextvar and writes it into `audit_log.correlation_id`. This is the link that closes the loop between logs and audit history.

### 3. Metrics scope now — **thin emission abstraction; defer Prometheus**

What we build now:

```python
# src/pdpl/observability/metrics.py
def counter(name: str, value: int = 1, **labels: str) -> None: ...
def histogram(name: str, value: float, **labels: str) -> None: ...
```

Both functions emit a `structlog` event with `event_type="metric"`, `metric_kind="counter" | "histogram"`, `metric_name`, `value`, and the labels as top-level keys. That's it. No registry, no label-cardinality validation, no Prometheus client, no `/metrics` endpoint.

**Why this is not under-engineering:** call sites are the durable part. `metrics.counter("tenant.created")` is what we will write today and what we want still to be in the code on the day we wire Prometheus. The *backend* of the abstraction can be swapped without touching a single call site.

**What we are explicitly not building yet:**
- A `/metrics` endpoint in Prometheus exposition format.
- A `prometheus_client` dependency.
- Grafana, Prometheus, node_exporter, scrape configs, or an alerts file.

**Trigger to upgrade:** the first of:
- First real (non-test) tenant is onboarded.
- Sustained traffic reaches ~100 req/min.
- A real incident occurs whose investigation would have been faster with a dashboard.

When any of those fires, we add `prometheus_client`, swap the `metrics.counter/histogram` implementations to call into a `CollectorRegistry`, mount `/metrics`, and stand up Prometheus + Grafana. The fact that no call site has to change is the entire point of writing the abstraction today.

This is a deliberate honest tradeoff against `CLAUDE.md` rule 3. We are honoring the *emission* half of the rule from line one. We are honoring the *collection* half on the trigger above. An empty Prometheus dashboard is not observability; it is decoration.

### 4. Secrets loading — `pydantic-settings`, fail fast, `SecretStr`

- `src/pdpl/config.py` defines `Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`.
- Passwords and connection strings are typed as `SecretStr` so their `repr()` returns `**********`. This protects against incidental leakage through tracebacks and `repr(settings)` calls in logs.
- The settings object is instantiated **once** at module import time. If any required env var is missing, Pydantic raises `ValidationError` and the process exits before serving a single request.
- The drop-secret-keys `structlog` processor (decision 1) is the second line of defense against accidental leakage. We do not rely on it alone.
- `Settings` instances are never `model_dump()`'d into a log. The log processor would catch secrets even if we did, but the discipline is "the secret never leaves the Settings object."

The env-var contract is:

| Var | Used by | Required at | Notes |
|---|---|---|---|
| `DATABASE_URL_DIRECT` | Alembic only | Migration run | Direct connection (5432), `psycopg2`, as the `postgres` (migration) role. |
| `APP_DATABASE_URL` | FastAPI runtime | App startup | `asyncpg` URL as `pdpl_app`. Pooled (6543) preferred; direct (5432) acceptable fallback — see §5. |
| `PDPL_APP_PASSWORD` | Alembic migration `0002` | Migration run only | The password applied to the `pdpl_app` role by migration `0002`. Runtime does **not** read this var. |

`PDPL_APP_PASSWORD` is intentionally a migration-time-only secret. The runtime gets the same password via `APP_DATABASE_URL` (embedded in the URL) and has no independent need for it. Keeping the two separate makes the migration safe to run from CI without surfacing the runtime URL there.

### 5. Authenticating as `pdpl_app` — runtime role split

This is the decision that makes ADR-0003 *binding*, not aspirational.

- Migration `0001` already created `pdpl_app NOLOGIN` and applied the grant pattern (`SELECT, INSERT, UPDATE, DELETE` on the operational tables; `SELECT, INSERT` only on `audit_log`; `UPDATE, DELETE, TRUNCATE` revoked on `audit_log`).
- **Migration `0002` (this ADR's companion) does two things**:
  1. `ALTER ROLE pdpl_app WITH LOGIN PASSWORD '<from env PDPL_APP_PASSWORD>'` so the role can actually open a connection.
  2. Re-issues the grant pattern as a defensive `GRANT … IF NOT ALREADY`. This is belt-and-suspenders: if `0001` ever drifted from the documented grants in a different environment, `0002` brings it back into compliance. `audit_log` stays restricted (no UPDATE, no DELETE).
- `0001` is **not** edited. It has been applied to Supabase; editing an applied migration is a category of mistake we do not start making in Phase 2. The role's password is added by `0002`, not by amending `0001`.

**Connection path — pooler preferred, direct acceptable:**

Per ADR-0003, the immutability guarantee is a property of the *role*, not of the *port*. The choice between Supavisor (6543) and direct (5432) is a performance/operability choice, not a security choice.

- Preference: `APP_DATABASE_URL` uses the Supavisor pooled URL (port 6543), username `pdpl_app.<project_ref>`. This survives connection storms and Supabase's free-tier connection cap.
- Acceptable fallback: if Supavisor refuses the custom `pdpl_app` role for any reason (auth driver quirk, role-name encoding), `APP_DATABASE_URL` uses the direct URL (port 5432) **still as `pdpl_app`**. Throughput is worse; the security property is identical.
- Forbidden fallback: the runtime URL connecting as `postgres` (or any owner role) "just to get it working." That silently nullifies ADR-0003. If we hit a wall, we say so and surface it; we do not paper over it with a privileged credential.

**Supavisor + asyncpg technical note (not its own ADR, but recorded so we don't relearn it):** Supavisor's transaction-mode pool does **not** support prepared statements. SQLAlchemy + asyncpg by default caches prepared statements. The fix is `connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0}` on `create_async_engine`. Comment in code references this ADR.

## What this ADR does **not** decide

Explicit, so a future reader does not think we missed it:

- **Authentication / users.** Per ADR-0001, deferred. This iteration's `POST /tenants` uses `actor_type='system'` and a service-account `actor_id` in `audit_log`. A future ADR covers user auth.
- **Rate limiting, CORS, CSRF.** Deferred until there is a real client. No public traffic exists yet.
- **Distributed tracing (OpenTelemetry).** Deferred. Correlation IDs cover the single-process tracing need today. When we have ≥ 2 services or external callouts whose latency matters, an ADR adds OTel.
- **Log shipping / aggregation.** stdout JSON only. No Loki / CloudWatch / Datadog yet. When we deploy to Hetzner, the deploy ADR decides this.
- **Health-check distinction between liveness and readiness.** `GET /health` does both jobs (process up + DB reachable) in one endpoint for now. When we introduce Kubernetes or a load balancer, the split becomes meaningful and gets its own short ADR.

## Consequences

### What we gain
- A single correlation ID joins logs, audit rows, and response headers from request one. The "who did what when" question has one identifier, not three.
- Audit-log immutability is enforced on the actual application connection, not just on the schema diagram. A SQL injection bug that escapes parameterization cannot mutate `audit_log` — the database errors at the verb level.
- The metrics call surface is stable today. Adding Prometheus later is a backend swap with no call-site churn.
- All secrets are in `SecretStr` and behind a fail-fast `Settings` object. Tracebacks and accidental `repr()`s don't leak them.
- Schema and runtime credentials are separated by role. The application can never accidentally run a DDL statement; the migrations never run with the application's reduced grants.

### What we explicitly are **not** building yet (and the trigger that changes that)
- **Prometheus + Grafana.** Trigger: first real tenant, or ~100 req/min, or first real incident.
- **OpenTelemetry.** Trigger: a second service joins the system, or an external call dominates latency budgets.
- **Log shipping to a central store.** Trigger: deploy to Hetzner — addressed in the deploy ADR.
- **Liveness vs readiness split.** Trigger: orchestration that distinguishes them (Kubernetes / load balancer health checks).

### Ops burden this adds
- One additional connection string (`APP_DATABASE_URL`) and one migration-time secret (`PDPL_APP_PASSWORD`) to manage in `.env` and in any future CI.
- A short setup checklist for any new environment: set `PDPL_APP_PASSWORD`, run migrations (which `ALTER ROLE`s with it), then set `APP_DATABASE_URL` to the corresponding pooled URL as `pdpl_app`.
- Password rotation = update env vars, re-run migration `0002` against the target environment. (A separate password-rotation runbook is deferred; when we get there, it gets its own short note in `docs/`.)

### Triggers to revisit this ADR
- Any of the four "not building yet" items above firing its trigger.
- A measured pain point with `structlog`'s integration with a library we add later — would prompt comparing `structlog` against `loguru` again with concrete data.
- A change in how Supabase exposes pooled connections that breaks the custom-role flow more deeply than the `statement_cache_size=0` workaround can cover.
