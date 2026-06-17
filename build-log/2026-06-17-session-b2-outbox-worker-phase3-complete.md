# 2026-06-17 — Phase 3 Session B2: the outbox worker; Phase 3 complete

The last piece. Session A proved an alert can never be *lost* (durable, atomic
enqueue on a worsening transition). Session B1 built the *send* — the HMAC-signed
`WebhookNotifier` behind the Notifier port. **B2 is the worker that connects the two**:
it claims due outbox rows, sends them through the notifier, and records the outcome —
retrying with backoff, dead-lettering, idempotently. With it, an enqueued alert is
actually delivered. **This closes Phase 3 (Reliability).**

No new ADR: ADR-0008 designed this whole pipeline; B2 implements its deferred worker
half. The decision/checks/scoring/alerts core stays AI-free — the worker lives outside
the import-linter–guarded core (guard still 1 kept, 0 broken) and only does I/O.

## What landed

- **The worker** (`src/pdpl/workers/outbox.py`).
  - **Its own engine.** Built inline from `WORKER_DATABASE_URL` — the worker's OWN
    session-level / direct connection as `pdpl_app` (never the transaction pooler;
    `FOR UPDATE` + a held transaction do not fit it; never the owner role). A
    mechanism, correct by construction — not a deployment note. `pdpl_app` has UPDATE
    on `outbox` (migration 0005), which is what lets the worker record an outcome.
  - **`run_once(session_factory, notifier, ...) -> WorkerStats`.** Drains the
    currently-due rows (`status IN ('pending','failed') AND next_attempt_at <= now()`)
    and returns — no busy-loop. One claim-send-commit per row: claim with `FOR UPDATE
    SKIP LOCKED LIMIT 1`, send inside the transaction (lock held across the send,
    bounded by B1's deadline), then update status. A send failure is **caught, not
    propagated** (propagating would roll back the claim), so the transaction commits
    the new status.
  - **Outcome routing.** `TransientNotifierError` → `failed` + full-jitter backoff
    `now() + random(0, min(base*2^(attempts-1), cap))`, then `dead_letter` after
    `max_attempts`; `PermanentNotifierError` → `dead_letter` immediately;
    any other/unclassified exception → treated as **transient** (retry, bounded) and
    logged loudly; success → `sent`.
  - **Audit + tracing.** `alert.sent` / `alert.dead_lettered` written in the SAME
    transaction as the status update; **no audit row per transient retry** (those live
    in structured logs + the row's `attempts`/`next_attempt_at`/`last_error`). The
    row's `correlation_id` (from the enqueue payload) is bound into the worker's logs
    and audit — completing end-to-end tracing from `run_check` to delivery.
  - **`run_forever`** wraps `run_once` with a clean SIGINT/SIGTERM shutdown (a tick
    that raises is logged and the loop continues — the worker never dies on one bad
    tick), and the entry point `python -m pdpl.workers.outbox` builds the engine +
    `WebhookNotifier` from settings (fail-fast if unset) and disposes the engine on
    exit.
- **Settings** (`src/pdpl/config.py`): `WORKER_DATABASE_URL` (`SecretStr`),
  `OUTBOX_MAX_ATTEMPTS` (5), `OUTBOX_BACKOFF_BASE_SECONDS` (60),
  `OUTBOX_BACKOFF_CAP_SECONDS` (3600), `OUTBOX_POLL_INTERVAL_SECONDS` (5) — all optional
  at import (the API boots without them). Documented in `.env.example` as worker-only,
  alongside the B1 webhook vars (which had been undocumented).
- **Tests** — `tests/test_outbox_worker.py` (9) + a `worker_session_factory` fixture in
  `conftest.py` built from `WORKER_DATABASE_URL`. **Full suite 110 passing** against the
  real Supabase project.

## Reliability properties proven

- **At-least-once + idempotency, not exactly-once.** A successful send marks `sent`;
  a sent (or dead-lettered) row is terminal and never re-claimed. Across a transient
  failure and a forced re-claim, **both sends carry the same idempotency key** — so a
  receiver dedupes the at-least-once resend. Stated plainly: a crash between a
  successful send and the commit re-sends; exactly-once is not on offer.
- **Full-jitter backoff within bound.** A transient failure reschedules
  `next_attempt_at` inside `[claim_time, claim_time + 60s]` for the first retry
  (asserted as a BOUND, not an exact value — the delay is randomised by design).
- **Transient → retry, permanent → dead-letter, unexpected → transient.** Each routed
  outcome is asserted: a 4xx-class permanent failure dead-letters on the first attempt
  (no wasted retries); a transient failure retries until `max_attempts` (5) then
  dead-letters; an exception the notifier could not classify is retried (bounded) and
  logged loudly.
- **`FOR UPDATE SKIP LOCKED` prevents a double-claim.** Two concurrent transactions
  claiming the same row: the first locks it, the second's identical claim returns
  nothing (skips, does not block).
- **The signing secret never reaches the worker's logs.** Driving the worker with the
  real `WebhookNotifier` (secret in a `SecretStr`) over a mock transport, the secret
  appears in no captured log event.
- **Dead-lettered, not lost.** An exhausted/permanent alert ends in a durable
  `dead_letter` row with `last_error` — an operational signal, never silently dropped.

## Decisions worth remembering

- **The notifier classifies; the worker owns policy.** All retry/backoff/dead-letter
  logic is in the worker, around the port — so a future email/WhatsApp notifier needs
  no change to the reliability machinery.
- **No `tenant_id` filter on `run_once` for test convenience.** The claim is global by
  design. Test isolation is solved in the tests — an owner-connection `TRUNCATE` before
  each worker test (outbox has no TRUNCATE trigger, unlike `audit_log`) plus
  assertions keyed to each test's own row — not by shaping a production API for tests
  (the same line we held in B1 on the dev-only hook). The outbox *does* carry a
  `tenant_id` column, so a tenant-scoped ops flush is a clean future feature when
  actually needed — a deliberate decision for then, not bolted on now.
- **The worker's engine is built inline**, mirroring `db/session.py`'s `connect_args`
  (`statement_cache_size=0`), rather than refactoring the app's hot path — the worker's
  distinct connection stays fully isolated. The two `connect_args` must stay in sync (a
  cross-reference comment marks it).
- **Tests exercise the real worker connection.** The session factory is built from
  `WORKER_DATABASE_URL`, not `APP_DATABASE_URL`, so the tests run over the actual
  session-level/direct path the worker uses — verified reachable here (the existing
  suite already runs against this `pdpl_app` direct endpoint).

## Phase 3 — complete

Reliability is done end-to-end for the first external integration:

- **Durability (A):** a worsening finding transition enqueues an outbox row atomically
  with the finding — the alert can never be lost.
- **Delivery mechanism (B1):** an HMAC-signed webhook with a bounded send deadline and
  typed transient/permanent failures, behind a Notifier port.
- **Reliable delivery (B2):** a Postgres-polling worker — claim with `SKIP LOCKED`,
  full-jitter backoff, dead-lettering, idempotent at-least-once sends, domain-only
  audit, end-to-end correlation.

CLAUDE.md build rule 4 (retry + backoff + idempotency key + a failure path/DLQ) is now
realised, and the AI/deterministic boundary held throughout (the core never imports the
notifier or the worker).

## Definition-of-Done check

- [x] Design/ADR — ADR-0008 (implementation split A/B1/B2; no new decision).
- [x] Logging + correlation ID — the row's correlation_id threads into worker logs +
      `alert.sent`/`alert.dead_lettered` audit; tick stats logged.
- [x] Error handling + reliability — backoff/retry/dead-letter/idempotency, all tested.
- [x] Tests — 9 new, 110 total passing against real Supabase; no real network.
- [x] No secrets in code — `WORKER_DATABASE_URL` / webhook secret via `SecretStr`; the
      signing secret asserted absent from worker logs; `.env` untracked, `.env.example`
      documents the shape.
- [x] Build-log entry — this file.

## Honest pieces

- **No scheduler/daemonization.** `run_forever` + the entry point exist, but running it
  as a supervised service (systemd / a container restart policy / continuous monitoring)
  is deliberately out of scope. Until something runs `python -m pdpl.workers.outbox`,
  enqueued alerts sit `pending` — durably, by design.
- **The held-transaction-across-send simplification stands** (ADR-0008 §8): bounded by
  B1's send deadline and one row per transaction. The lease / visibility-timeout model
  remains the documented scale path, not built.
- **At-least-once, owned honestly.** The idempotency key makes resends safe *for a
  receiver that honours it*; receiver-side dedupe/freshness enforcement is out of this
  repo.
- **Still synthetic tenants, no auth.** Unchanged; tests create their own tenants and
  leave data behind, and now TRUNCATE the (artifact-only) outbox before worker tests.

## What's still deferred (unchanged)

Real email/WhatsApp notifier implementations; worker scheduling/daemonization/continuous
monitoring; lease/visibility-timeout recovery; receiver-side webhook verification;
tenant-scoped alert routing/subscriptions; the AI explanation layer; authentication;
`finding_evidence` linking; score persistence.

## Lessons (Faisal)

Lesson (Faisal): Don't chase the illusion of exactly-once delivery — at-least-once + idempotency is more robust, simpler, and gives the same effective outcome. Guaranteeing a network message arrives exactly once is an engineering nightmare (distributed leases, complex state machines). Instead we leaned on the per-row claim-send-commit model and let Postgres carry the crash-safety for free:

- The worker crashes mid-send: Postgres aborts the transaction, the row lock releases, and the row returns to its prior state — the next tick re-claims it as if nothing happened. No stuck rows, no lost alert.

- The send succeeds but the worker crashes before commit: the next worker re-claims and re-sends (at-least-once), and the Idempotency-Key from B1 lets the receiver recognize the duplicate and drop it.

The result is a crash-resistant system built the simplest way possible. And FOR UPDATE SKIP LOCKED pushes concurrency control into the database itself: many workers can run in parallel and the DB keeps them off the same row, with zero orchestration code in the app.

This is the flip side of deferring the lease/visibility-timeout pattern — rollback + idempotency already give crash-safety, so the heavier coordination machinery isn't needed yet.
