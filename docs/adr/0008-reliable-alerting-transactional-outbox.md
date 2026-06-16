# ADR-0008: Reliable Alerting via a Transactional Outbox

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0002 — Findings History Model](0002-findings-history-model.md), [ADR-0004 — Application Foundation & Observability](0004-application-foundation-and-observability.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [Data Model](../02-data-model.md), [Product Definition](../product-definition.md), [CLAUDE.md — build rule 4 (reliability)](../../CLAUDE.md)

## Context

The product promise is *"warn before a fine"* (`docs/product-definition.md`): continuous
monitoring must tell a tenant when their compliance state **changes for the worse** —
before a regulator does. Phase 2 made that change observable: `run_check` detects a
finding's status transition (SCD Type 2, ADR-0002) inside one transaction. Phase 3
(Reliability) turns *"a worsening change happened"* into *"an alert was reliably
delivered."*

This is the project's **first external integration**. Until now there has been nothing to
wrap with the reliability patterns CLAUDE.md mandates (build rule 4: *retry with
exponential backoff + an idempotency key + a failure path / DLQ; never duplicate an
operation*). An outbound alert is exactly that — a side effect on a remote system that can
fail, time out, or be delivered twice. We build it reliably from line one.

This ADR is also bounded by the project's **core safety line** (CLAUDE.md): *AI reads /
suggests / explains; deterministic logic decides / scores / classifies.* Alerting must not
become a back door through which a non-deterministic component influences a verdict. As
designed here, the decision/checks/scoring core stays AI-free: enqueuing an alert is **a
plain DB write in the same transaction as the finding** — no notifier import, no network
call, nothing that touches the import-linter–guarded core (ADR-0006, `.importlinter`).

### What this ADR covers vs defers

This ADR designs the **whole** alerting pipeline so the data model is built once. The
implementation is split across two sessions (see *Implementation split* below). The outbox
table created now carries every column the worker needs, so Session B adds no migration.

## Decision drivers

- **An alert must never be lost** — not on a process crash, not on a remote outage.
- **The request path must not block** on a remote send. `run_check` is called from
  `POST /tenants/{id}/checks`; it must return as soon as the finding is durable.
- **Atomicity with the verdict** — an alert may exist *only* for a transition that actually
  committed, and a committed worsening transition must *always* leave an alert behind. No
  dual-write gap in either direction.
- **The core stays AI-free and the guard holds** — enqueuing is a DB write, not a
  dependency on the notifier or any vendor.
- **No new infrastructure** unless the volume justifies it. One producer, low volume, zero
  external integrations elsewhere in the product.

## Decision

A **transactional outbox**: a worsening transition writes an `outbox` row in the *same
transaction* as the finding. A separate **Postgres-polling worker** claims pending rows
with `FOR UPDATE SKIP LOCKED`, sends each through a **Notifier port**, and marks them
`sent` / reschedules with **full-jitter exponential backoff** / **dead-letters** after
`max_attempts`. Delivery is **at-least-once**, made effectively-once by a per-alert
**idempotency key** the receiver can dedupe on.

### 1. Transactional outbox, not naive in-process retry

The rejected alternative — fire the webhook inline after the transition, retrying in
memory — breaks three ways at once:

- **Lost alerts on crash.** If the process dies after the finding commits but before/during
  the send, the alert is gone and there is no record it was ever owed.
- **It blocks the request path.** Inline retry with backoff holds the HTTP response open
  for seconds-to-minutes while a remote endpoint misbehaves.
- **Dual-write inconsistency + no real DLQ.** Send-then-rollback alerts on a finding that
  doesn't exist; commit-then-fail-to-send loses the alert. In-memory retries vanish on
  restart — there is no durable failure state to inspect or replay.

The outbox makes the alert *intent* a row that commits **atomically with the finding**:
either both land or neither does. A crash leaves the row `pending` — the worker picks it up
later. The request returns the moment the finding is durable. A failed alert ends in a
durable `dead_letter` row, not a lost in-memory retry. This is build rule 4, realised.

### 2. Worker: Postgres polling with `FOR UPDATE SKIP LOCKED` — not Redis/Celery

A broker (Redis + Celery/RQ) would add a new infrastructure dependency and a new deployment
unit to serve **one producer at low volume**, while the rest of the product has *zero*
external integrations. That is over-engineering at this stage. We already run Postgres, and
the queue-on-Postgres pattern is well understood:

```sql
SELECT ... FROM outbox
WHERE status IN ('pending','failed') AND next_attempt_at <= now()
ORDER BY next_attempt_at
FOR UPDATE SKIP LOCKED
LIMIT :batch;
```

`SKIP LOCKED` lets multiple workers (if we ever scale out) claim disjoint rows without
blocking each other; at MVP we run a single worker. The `next_attempt_at` column drives
both first-delivery and retry scheduling through the *same* query.

**Not `LISTEN/NOTIFY` as the primary trigger:** a NOTIFY is lost if the worker is down at
that instant, and it carries no notion of *"try again later."* Polling + `next_attempt_at`
handles missed events and scheduled retries uniformly. NOTIFY may later be layered on as a
latency optimisation — it is not needed now.

**Revisit trigger:** move to a real broker when throughput or fan-out (many notifier
targets per alert) makes single-worker polling the bottleneck — not before.

### 3. The Notifier PORT — reliability wraps the port, not a vendor

The worker depends on an abstract `Notifier` port (`async def send(alert) -> None`, raises
on failure), **not** on a concrete vendor. All the reliability machinery — backoff, retry,
dead-lettering, idempotency — lives *around the port*, so it is written once and reused by
every implementation.

- **First concrete implementation (Session B):** an outbound **webhook, HMAC-signed** with
  a secret from settings.
- **Deferred swap-ins behind the same port:** email, WhatsApp. **Noted, not built.** When
  they land, the worker, backoff, DLQ, and idempotency logic do not change — only a new
  `Notifier` implementation is added.

The port also makes the worker testable with a **stub notifier** that simulates transient
and permanent failures — exactly what the Session B failure-path tests need.

### 4. Trigger policy: worsening transitions only

**The report is the current-state surface; the alert is the change surface.** A first
assessment *establishes* state — it is not a change — so it must not alert. Continuous
monitoring's value, and the "warn before a fine" promise, is the **regression after a known
state**.

A strict severity order is defined over the three real verdict states only:

```
compliant (3)  >  partial (2)  >  non_compliant (1)
```

An alert is enqueued **iff** both `from` and `to` are in this set **and** `to` is strictly
worse than `from` (`severity[to] < severity[from]`).

`not_assessed`, `unknown`, and `not_applicable` are **unranked** and never source or target
an alert:

- **`not_assessed` is never a source.** The first assessment of a control moves it out of
  `not_assessed`; that establishes state, it is not a worsening — so a fresh tenant's
  baseline run (every control first-seen) and a first real verdict raise **no** alerts.
  This is deliberate: it keeps onboarding quiet instead of dispatching a storm of alerts
  the day a tenant fills the questionnaire.
- **`compliant`/`partial` → `not_assessed` does NOT alert.** That is *knowledge loss*
  (evidence withdrawn, control un-assessed), not a verdict worsening. We do not cry wolf on
  a state we can no longer judge.
- **Improving transitions are deferred.** `non_compliant → compliant` is good news; a
  future digest may surface it, but it is not an urgent alert.

This worsening-only policy is itself a recorded product decision, not just an
implementation detail — it defines what the product treats as an alarm.

### 5. Idempotency key derivation

Every transition creates **exactly one** new finding row with a fresh UUID v7 — a value
that is 1:1 with the transition. The idempotency key is derived from it:

```
idempotency_key = "alert:finding-transition:{new_finding_id}"
```

stored in `outbox.idempotency_key` as `text NOT NULL UNIQUE`. Two guarantees follow:

- **No duplicate enqueue.** Even if `run_check` logic ever tried to enqueue the same
  transition twice, the unique constraint rejects the second row at the DB layer.
- **No duplicate send.** The worker carries the same key into the webhook (an
  `Idempotency-Key` header) so the receiver can dedupe a re-delivery.

**Honest about the guarantee:** across a network we get **at-least-once delivery** (a crash
after a successful send but before the row is marked `sent` causes a resend). Exactly-once
is not achievable end-to-end; the idempotency key is what makes at-least-once
*effectively-once* **for a receiver that honours it**. We state this plainly rather than
pretend to exactly-once.

### 6. Backoff, max attempts, dead-letter, secrets

- **Full-jitter exponential backoff** (the canonical AWS pattern):

  ```
  next_attempt_at = now() + random_between(0, min(base * 2**attempts, cap))
  ```

  **Why full jitter even though the thundering-herd problem it solves is NOT live here**
  (single target, low volume, one worker): backoff *is* the Phase-3 reliability lesson, and
  we build the real, correct pattern rather than a toy. Plain exponential backoff
  synchronises retries across many clients into coordinated spikes; full jitter spreads
  them uniformly. It costs nothing to do right now and is the pattern we want in muscle
  memory. Tests assert `next_attempt_at` falls within the expected **bound**
  `(now, now + cap]`, not an exact value (the delay is randomised by design).

- **Max attempts → dead-letter.** After `max_attempts` (default 5) failed sends the row
  moves to `dead_letter` — durably preserved (not lost), never retried again (not retried
  forever). A dead-lettered alert is an operational signal for a human.

- **Secrets via pydantic-settings / `SecretStr`** (mirrors `src/pdpl/config.py`). The
  webhook URL and HMAC signing secret are env-driven; the signing secret is a `SecretStr`
  and **never** appears in logs — at most a key fingerprint is logged, never the secret. A
  Session B test asserts the secret string never appears in captured log output.

### 7. State machine

Four states, as carried in `outbox.status`:

| State | Meaning | Claimable? |
|---|---|---|
| `pending` | Enqueued, not yet attempted. Inserted with `next_attempt_at = now()`. | Yes, when `next_attempt_at <= now()` |
| `failed` | A previous attempt failed transiently; a retry is scheduled (`attempts > 0`, future `next_attempt_at`). | Yes, when `next_attempt_at <= now()` |
| `sent` | Delivered successfully. Terminal. | No |
| `dead_letter` | `max_attempts` exhausted. Terminal; needs a human. | No |

`pending` rows set `next_attempt_at = now()` so the **single** claim query
(`status IN ('pending','failed') AND next_attempt_at <= now()`) covers both `pending` and
`failed` uniformly. Keeping `failed` distinct from `pending` is for observability ("how many
alerts are actively failing right now?"), which a 3-state model would lose.

### 8. Worker transaction model: claim-send-commit per row (with honest trade-off)

The worker processes **one row per transaction**: `BEGIN → SELECT … FOR UPDATE SKIP LOCKED
LIMIT 1 → send via Notifier → UPDATE status → COMMIT`. Simple, and the row lock is bounded
to a single row plus a single send.

**The simplification, stated honestly:** this holds the transaction open **across the
network send**. That is acceptable here because (a) there is one worker at low volume, and
(b) a **strict, short send timeout is mandatory** — a hung send must not hold the
transaction/row lock open indefinitely. The crash-after-send-before-commit window leaves the
row claimable again, and the idempotency key (§5) covers the resulting resend.

**Consequence for connectivity:** because the worker relies on `FOR UPDATE` inside a
held transaction, it must connect in **session mode / on the direct connection** as
`pdpl_app` — **not** through Supavisor's transaction pooler (port 6543), which does not
support a transaction held across statements with row locks.

**Deferred scale path:** a **lease / visibility-timeout** model (claim by stamping
`next_attempt_at` into the future and committing immediately, send *outside* any
transaction, then a second short transaction to record the result). It removes the
held-transaction-across-IO smell and recovers a crashed worker's in-flight rows after the
lease expires — but it adds a "stuck lease" recovery path to build and test. We adopt it
when concurrency or send latency makes the held transaction a real problem, not before.

### 9. Audit policy

Aligned with build rule 6 (immutable audit log for every decision), but kept
domain-significant:

- `alert.enqueued` — written **in the same transaction** as the finding transition, so the
  audit trail and the outbox row are atomic together.
- `alert.sent` — written by the worker on successful delivery.
- `alert.dead_lettered` — written by the worker when a row exhausts `max_attempts`.

**No audit row per transient retry.** Retries are operational noise, not domain events;
they live in structured logs and in the outbox row's own `attempts` / `next_attempt_at` /
`last_error` columns. `audit_log` stays a record of things that matter to the compliance
story, not a delivery-attempt log.

## Schema (created now; full table so Session B needs no migration)

`outbox`:

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid` PK | App-generated UUID v7. No `DEFAULT` (identifier strategy, `docs/02-data-model.md`). |
| `tenant_id` | `uuid NOT NULL` | `REFERENCES tenants(id) ON DELETE CASCADE`. |
| `topic` | `text NOT NULL` | The outbox event kind, e.g. `finding.worsened`. Free-text now; lets future event types share the table. |
| `payload` | `jsonb NOT NULL` | Alert content: tenant, control, from/to status, finding & run ids, correlation id. **No secrets.** |
| `idempotency_key` | `text NOT NULL UNIQUE` | `alert:finding-transition:{finding_id}` (§5). |
| `status` | `text NOT NULL DEFAULT 'pending'` | `CHECK IN ('pending','sent','failed','dead_letter')`. |
| `attempts` | `int NOT NULL DEFAULT 0` | `CHECK >= 0`. Incremented per send attempt. |
| `next_attempt_at` | `timestamptz NOT NULL DEFAULT now()` | Drives the claim query; pending rows are immediately due. |
| `last_error` | `text` | Last failure message (ops). Not an audit event. |
| `sent_at` | `timestamptz` | `CHECK (status != 'sent' OR sent_at IS NOT NULL)`. |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | App/worker sets on mutation. |

Indexes:

- `idx_outbox_due` — partial `(next_attempt_at) WHERE status IN ('pending','failed')`: the
  claim query's index, kept small by excluding terminal rows.
- `uniq_outbox_idempotency_key` — the `UNIQUE` constraint on `idempotency_key`.
- `idx_outbox_tenant_created` — `(tenant_id, created_at DESC)` for per-tenant ops reads.

Grants (**differ from `audit_log`**): `pdpl_app` gets `SELECT, INSERT, UPDATE` — enqueue
inserts, the worker selects and updates status. **No `DELETE`.** This is deliberately *not*
`audit_log`'s INSERT+SELECT-only pattern: the worker must `UPDATE` rows to mark them
`sent`/`failed`/`dead_letter`.

## Consequences

**Positive**
- An alert can never be lost: it is as durable as the finding that caused it, and a failed
  send ends in an inspectable `dead_letter` row.
- The request path never blocks on a remote endpoint.
- The reliability machinery is written once around the port; new channels are pure
  additions.
- The deterministic core is untouched and the import-linter guard still holds — enqueuing
  is a DB write.

**Negative / accepted**
- At-least-once, not exactly-once (§5) — mitigated by the idempotency key for a cooperating
  receiver.
- The worker holds a transaction across the network send (§8) — bounded by a mandatory
  short timeout and one-row-per-transaction; the lease model is the documented scale path.
- The worker cannot use the transaction pooler (§8) — it connects in session mode / direct.

## Implementation split

- **Session A (this change) — durability: the alert can never be lost.** The `outbox`
  migration (full table + grants + clean downgrade); the `enqueue_alert` DB helper; wiring
  `run_check` to enqueue atomically on a worsening transition; the worsening-transition
  trigger policy (deterministic, in the guarded core); the `Notifier` **port interface**
  (abstract only). Tests: atomic enqueue (exactly one row; crash-before-commit leaves no
  orphan finding **and** no orphan outbox row); trigger policy (worsening enqueues; baseline
  / improving / knowledge-loss do not); idempotency at the DB layer (unique key).
- **Session B — delivery: the alert is sent reliably.** The HMAC-signed webhook `Notifier`
  implementation; the polling worker (claim / send / full-jitter backoff / dead-letter /
  idempotent send); settings additions (`SecretStr`). Tests: sends and marks `sent`;
  transient failure → retry within the backoff bound; permanent failure → `dead_letter`;
  never sent twice; signing secret never in logs.

## Open questions (deferred)

- **Lease / visibility-timeout worker model** (§8) — when held-transaction-across-IO
  becomes a real constraint.
- **`LISTEN/NOTIFY` latency optimisation** (§2) — layered on polling if alert latency
  matters.
- **email / WhatsApp Notifier implementations** (§3) — behind the same port.
- **Improving-transition digest** (§4) — good-news summary, separate from urgent alerts.
- **Per-tenant notifier routing / subscriptions** — today there is a single configured
  target; a real product needs per-tenant destinations and preferences. Out of MVP scope.
