# 2026-06-16 — Phase 3 Session A: alert durability (transactional outbox)

Phase 3 is Reliability. The product had **no external integration** yet, so the
reliability patterns CLAUDE.md mandates (build rule 4: retry + backoff +
idempotency + failure path/DLQ) had nothing to wrap. This phase introduces the
first one — alerting — and builds it reliably from line one. Session A delivers
the **durability** half: *the alert can never be lost.* Session B will deliver
**delivery**: the worker that actually sends it.

The design is settled whole in **ADR-0008** (transactional outbox), then split:
A builds the data path (enqueue), B builds the send path (worker + webhook).

## What landed

- **ADR-0008 — reliable alerting via a transactional outbox** (`docs/adr/0008-...md`).
  Surfaces and decides: outbox over naive in-process retry (lost-on-crash, blocks
  the request path, no real DLQ); a Postgres-polling worker with `FOR UPDATE SKIP
  LOCKED` over Redis/Celery (no new infra for one producer at low volume); the
  Notifier **port** that reliability wraps (webhook-with-HMAC first; email/WhatsApp
  deferred swap-ins behind the same port); the worsening-transition **trigger
  policy**; idempotency-key derivation; full-jitter backoff + max-attempts →
  dead-letter; the four-state machine; and an honest statement of the
  at-least-once (not exactly-once) guarantee and the held-transaction-across-send
  simplification. The full table is specified now so Session B adds no migration.

- **`outbox` table** (`migrations/versions/0005_outbox.py`). Hand-written, with a
  clean downgrade (verified round-trip). Columns: `id` (app UUID v7, no default),
  `tenant_id` (FK CASCADE), `topic`, `payload` jsonb, `idempotency_key`
  (UNIQUE), `status` (`pending`/`sent`/`failed`/`dead_letter` CHECK), `attempts`
  (CHECK ≥ 0), `next_attempt_at` (default `now()`), `last_error`, `sent_at`
  (CHECK `status != 'sent' OR sent_at IS NOT NULL`), `created_at`, `updated_at`.
  Indexes: partial claim index `idx_outbox_due` on `(next_attempt_at) WHERE status
  IN ('pending','failed')`; `uniq_outbox_idempotency_key`; `idx_outbox_tenant_created`.
  **Grants differ from `audit_log`:** `pdpl_app` gets `SELECT, INSERT, UPDATE`, NO
  `DELETE` — enqueue inserts, the worker selects + updates status. (audit_log is
  INSERT+SELECT-only; the worker here must UPDATE.)

- **Notifier port — interface only** (`src/pdpl/notifications/port.py`). An
  `OutboxAlert` dataclass, a `NotifierError`, and a `runtime_checkable`
  `Notifier` Protocol (`async def send(alert) -> None`, raises on failure). No
  concrete implementation in Session A; the worker and the webhook impl are
  Session B. The reliability machinery will wrap this contract, not a vendor.

- **Worsening-transition policy** (`src/pdpl/services/alerts.py`). A pure,
  AI-free `is_worsening_transition(from, to)` over the severity order
  `compliant(3) > partial(2) > non_compliant(1)`; unranked states
  (`not_assessed`/`unknown`/`not_applicable`) never source or target an alert.
  **The import-linter guard was extended to cover `pdpl.services.alerts`** — classifying
  whether a compliance change is an alarm is a verdict-adjacent decision, so it
  rides in the same AI-free core as decision/checks/scoring.

- **Atomic enqueue** (`src/pdpl/db/outbox.py` + `src/pdpl/services/checks.py`).
  `enqueue_alert(session, ...)` is a plain DB write — like `db/audit.py` — that
  inserts one outbox row plus an `alert.enqueued` audit event inside the caller's
  transaction. `run_check` calls it on a worsening transition using the SAME
  session, so the outbox row, the finding, and the audit row commit (or roll back)
  together. **No notifier import, no network call** in `run_check` — the core
  stays clean and the guard holds. The completion audit/metric/log now carry
  `alerts_enqueued`.

- **Tests** — `tests/test_outbox_enqueue.py`, **18 new**, full suite **78 passing**
  against the real Supabase project.

## The durability guarantee, proven

- **Atomic enqueue.** A worsening transition writes **exactly one** outbox row,
  with the transition payload, the derived idempotency key, `pending` /
  `attempts=0` / immediately-due defaults, and an `alert.enqueued` audit row — all
  in the finding's transaction.
- **Crash-before-commit = no orphan of either kind.** A forced failure inside
  `run_check`'s transaction, *after* a worsening transition + enqueue but before
  commit, rolls back wholly: the finding stays at its prior status **and** no
  outbox row exists. The alert can neither leak ahead of a verdict that didn't
  commit, nor be lost behind one that did.
- **Trigger policy.** Baseline (every control first-seen → `not_assessed`),
  improving transitions, and knowledge-loss (`compliant/partial → not_assessed`)
  enqueue nothing; only a worsening between ranked verdicts does. Asserted both as
  a pure-function truth table (13 cases) and end-to-end through `run_check`.
- **Idempotency at the DB level.** The `UNIQUE` `idempotency_key`
  (`alert:finding-transition:{finding_id}`, 1:1 with a transition) rejects a
  duplicate enqueue at the database, independent of application discipline.

## Decisions worth remembering

- **Enqueue is a DB write, not a dependency.** Keeping `enqueue_alert` in
  `db/outbox.py` (mirroring `db/audit.py`) means `run_check` imports a writer, not
  the notifier — the import-linter core never gains an alerting/AI edge. The whole
  reliability story sits on the worker side of the table.
- **The report is the current-state surface; the alert is the change surface.** A
  first assessment *establishes* state, so `not_assessed` is never an alert source
  — which is exactly what keeps onboarding from dispatching an alert storm the day
  a tenant fills the questionnaire. Knowledge loss (`→ not_assessed`) is not a
  verdict worsening, so it does not alert either.
- **Full jitter even though its problem isn't live.** Single target, one worker —
  no thundering herd to spread. We build the canonical full-jitter pattern anyway
  because backoff *is* the Phase-3 lesson, and tests assert the delay falls within
  the expected **bound**, not an exact value (Session B).
- **Four states, one claim query.** `pending` rows set `next_attempt_at = now()`
  so `status IN ('pending','failed') AND next_attempt_at <= now()` covers both
  uniformly; `failed` is kept distinct only for the observability it buys.
- **Audit stays domain-significant.** `alert.enqueued` (atomic here) + `alert.sent`
  / `alert.dead_lettered` (Session B). **No audit row per transient retry** —
  retries live in structured logs and the outbox row's
  `attempts`/`next_attempt_at`/`last_error`.

## Definition-of-Done check

- [x] Design/ADR — ADR-0008 (the whole pipeline; implementation split A/B recorded
      in it).
- [x] Logging + correlation ID — `enqueue_alert` threads the run's correlation_id
      into the outbox payload and the `alert.enqueued` audit row;
      `check_run.completed` carries `alerts_enqueued`.
- [x] Error handling + reliability — this *is* the reliability feature; Session A
      proves the atomic/rollback path. Retry/backoff/DLQ land in Session B.
- [x] Tests — 18 new, 78 total passing against real Supabase, including the
      crash-before-commit rollback and the DB-level idempotency rejection.
- [x] No secrets in code — the signing secret is introduced in Session B via
      `SecretStr`; nothing secret lands in Session A.
- [x] Build-log entry — this file.

## Honest pieces

- **At-least-once, not exactly-once.** Proven here only at the DB layer (the unique
  key). The network-side guarantee — a crash after a successful send but before the
  row is marked `sent` causes a resend — is real and is mitigated by carrying the
  idempotency key into the delivery for a cooperating receiver. That path is built
  and tested in Session B.
- **The worker holds a transaction across the send (by design, Session B).** Bounded
  by a mandatory short timeout and one-row-per-transaction; the lease /
  visibility-timeout model is the documented scale path, not built now. Consequence:
  the worker must connect session-mode/direct as `pdpl_app`, not via the transaction
  pooler.
- **Still synthetic tenants, no auth.** Unchanged trade-off; tests create their own
  tenants and leave data behind.

## Deferred to Session B (delivery)

- The **HMAC-signed webhook** `Notifier` implementation (secret via `SecretStr`).
- The **polling worker**: claim with `FOR UPDATE SKIP LOCKED`, send via the
  Notifier, mark `sent` / reschedule with full-jitter backoff / move to
  `dead_letter` after `max_attempts`; idempotent.
- Tests: sends and marks `sent`; transient failure → retry within the backoff
  **bound**; permanent failure → `dead_letter` (not lost, not retried forever);
  never sent twice; signing secret never appears in logs.

Still deferred beyond Phase 3 (unchanged): the AI explanation layer, document
reading, scheduling/continuous monitoring, authentication, `finding_evidence`
linking, score persistence, email/WhatsApp notifier implementations.

## Lessons (Faisal)

Instead of treating the alert as a network send command, we turned it into a plain data row in the database. Because that row is written inside the same transaction as the finding transition (the close-old + insert-new that detected the worsening), we inherit Postgres's atomicity:

- Either the transition and its alert commit together — a fast local INSERT, milliseconds, so the user's request returns immediately without ever waiting on a network call.

- Or, if anything interrupts before commit (a crash, an exception, a dropped connection), they roll back together. It is impossible for the finding to change without its alert being recorded, or the reverse.

This is durability of intent, not delivery — whether the alert actually reaches anyone is a separate, retryable concern (Session B). The answer itself was saved earlier in its own request; run_check reads it, detects the worsening, and enqueues atomically with the transition.
