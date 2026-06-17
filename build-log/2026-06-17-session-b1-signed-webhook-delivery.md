# 2026-06-17 — Phase 3 Session B1: signed webhook delivery

Session A proved an alert can never be *lost* (durability: the outbox row commits
atomically with the finding transition). Session B is *delivery* — actually sending
the alert reliably. B is split so each half gets a focused session: **B1 (this
session) is the `WebhookNotifier`** — the concrete `Notifier` behind the port that
performs one signed send with an honest typed failure. **B2 (next) is the worker** —
the claim / retry / backoff / dead-letter / idempotency engine that drives this
notifier.

No new ADR: ADR-0008 already covers this. The decision/checks/scoring/alerts core
stays AI-free — the notifier and httpx live outside the import-linter–guarded core
(guard still 1 kept, 0 broken).

## What landed

- **`WebhookNotifier`** (`src/pdpl/notifications/webhook.py`). POSTs the alert payload
  as JSON to a configured URL, HMAC-SHA256 signed, behind the existing `Notifier`
  port. It owns exactly one thing — a single signed send with a bounded deadline and
  a typed failure — and nothing about retry/backoff (that is the worker, B2). Fails
  fast at construction if URL/secret are absent.
- **Typed notifier errors** (`src/pdpl/notifications/port.py`). `TransientNotifierError`
  and `PermanentNotifierError` under the existing `NotifierError` base. The notifier
  classifies: timeout / connection error / HTTP 5xx / HTTP 429 → transient; HTTP 4xx
  (other than 429) → permanent; an unclassifiable status (1xx/3xx) → base
  `NotifierError`, which **propagates** so the worker's default branch decides (B2).
- **Webhook settings** (`src/pdpl/config.py`). `ALERT_WEBHOOK_URL` and
  `ALERT_WEBHOOK_SECRET` (`SecretStr`) are **optional at import** — the API and most
  tests never touch alerting, so a missing value must not stop the process from
  serving. Fail-fast moves to the notifier's construction instead, so a misconfigured
  worker still cannot run. `ALERT_WEBHOOK_TIMEOUT_SECONDS` defaults to 5.
- **httpx promoted to a production dependency** (`pyproject.toml`). It was dev-only
  (the test client); the worker delivers over it in production, so it belongs in the
  runtime deps. Removed from the dev list to avoid a duplicate.
- **Test doubles + tests.** `tests/stubs.py` holds a shared `StubNotifier` behind the
  port (success / transient / permanent, records calls) — the reason the port was
  abstracted in A, and what B2's worker tests will reuse. `tests/test_webhook_notifier.py`
  drives the real notifier through `httpx.MockTransport` (no network). **23 new tests;
  full suite 101 passing** against the real Supabase project.

## Security / reliability properties PROVEN in tests

- **Signs the exact bytes sent.** The body is serialized once (deterministic JSON,
  sorted keys) and the *same* bytes are both signed and transmitted — the test
  recomputes the HMAC over `"{timestamp}.{body}"` from the captured request and
  asserts the `X-PDPL-Signature: sha256=…` header matches. No serialize-twice gap
  where the signed bytes and the sent bytes could differ.
- **Replay-resistant by a signed timestamp.** `X-PDPL-Timestamp` is included *inside*
  the signed string, so a receiver can reject a stale replay on freshness without the
  signature still validating. (Receiver-side freshness enforcement and honoring a 429
  `Retry-After` are deferred — noted, not built.)
- **The signing secret never reaches the logs.** Asserted with `structlog`'s
  `capture_logs`: the secret string appears in no log event; only a short,
  non-reversible `signing_key_fingerprint` (first 8 hex of its SHA-256) is logged, so
  a key mismatch is diagnosable without leaking the key. The secret stays a `SecretStr`
  end to end.
- **A single overall wall-clock send deadline.** `asyncio.timeout` wraps the whole
  send so connect/read/write do **not** stack into an unbounded total; a slow send is
  bounded and surfaces as a transient failure. This ceiling matters beyond this
  session: **in B2 the worker holds the outbox row lock across this send, so this
  deadline is the lock-hold ceiling.**
- **Honest typed classification.** Transient vs permanent vs unclassifiable are each
  asserted, so the worker (B2) can route retry-worthy from dead-letter-worthy from
  default without re-inspecting HTTP internals.

## Decisions worth remembering

- **The notifier classifies failure; the worker owns policy.** The notifier says *how*
  a send failed (typed error); it does not decide retry counts, backoff, or
  dead-lettering. That separation is what lets B2's reliability logic wrap the port
  unchanged for a future email/WhatsApp swap-in.
- **Optional-at-import, fail-fast-at-construction.** Putting the webhook URL/secret as
  required global settings would have forced every API run and test to set them.
  Making them optional in `Settings` but mandatory in `WebhookNotifier.__init__` keeps
  the app bootable while still guaranteeing the worker cannot start misconfigured.
- **One overall deadline, not per-phase timeouts.** httpx's per-phase timeouts can
  still stack; the authoritative bound is the single `asyncio.timeout` around the
  send, with the httpx client timeout as a secondary guard.

## Logged-deferred to B2 (do not lose)

- **The worker gets its OWN session-mode / direct connection as `pdpl_app`** — a
  dedicated worker DB URL, **not** the transaction-pooler `APP_DATABASE_URL`. `FOR
  UPDATE` + a held transaction do not fit the transaction pooler. This is a
  **mechanism** (correct by construction), not a deployment note. Reuse the
  `session_scope` pattern bound to the worker's own engine. Never the owner role.
- **Default exception branch.** A failure that is neither `TransientNotifierError` nor
  `PermanentNotifierError` (including the base `NotifierError` the notifier raises for
  an unclassifiable status) → the worker treats it as **transient** (retry, bounded by
  max_attempts) and logs loudly.

Also confirmed for B2 (unchanged): full-jitter backoff (base 60s / cap 1h / 5
attempts) → `dead_letter`; `run_once()` / `run_forever()` with SIGINT/SIGTERM;
`correlation_id` threaded into worker logs + audit; audit `alert.sent` /
`alert.dead_lettered` only (no row per transient retry).

## Definition-of-Done check

- [x] Design/ADR — covered by ADR-0008; no new architectural decision.
- [x] Logging — structured; the secret is provably absent (fingerprint only).
- [x] Error handling — typed transient/permanent classification + a bounded deadline;
      retry/backoff/DLQ are B2.
- [x] Tests — 23 new, 101 total passing against real Supabase; no real network (port
      stub + httpx MockTransport).
- [x] No secrets in code — secret via `SecretStr`, asserted never logged.
- [x] Build-log entry — this file.

## Honest pieces

- **No real delivery is wired yet.** B1 is the send *mechanism*; nothing calls it on a
  schedule. The worker that claims outbox rows and invokes this notifier is B2 — until
  then, enqueued alerts sit `pending` (durably, by design).
- **At-least-once, not exactly-once** (ADR-0008 §5). The `Idempotency-Key` header is
  set here so a receiver can dedupe a re-delivery; the resend window itself is a worker
  concern (B2).
- **Receiver-side trust is out of scope.** We sign and timestamp; enforcing signature
  freshness and rejecting replays is the receiver's job and is not part of this repo.

## What's still deferred (unchanged)

The B2 worker (claim/retry/backoff/DLQ/idempotent send); real email/WhatsApp notifier
implementations; worker scheduling/daemonization/continuous monitoring;
lease/visibility-timeout recovery; the AI explanation layer; authentication.

## Lessons (Faisal)

The send timeout isn't a network nicety — it's a guard on a database lock. Next session the worker will claim an alert with FOR UPDATE SKIP LOCKED and hold that row's transaction open across the HTTP send. If the webhook hangs and the timeout is loose, that transaction stays open indefinitely — tying up its connection and sitting idle-in-transaction (holding the lock, blocking vacuum). That's the real cost, not other workers blocking on the row: SKIP LOCKED makes concurrent workers skip a locked row, not wait on it. A single strict overall deadline (5s) bounds how long the transaction and its lock can ever be held.

The notifier classifies; the worker runs the retry policy. The send module owns no retry logic and no give-up rule — it only maps the outcome to a typed error: TransientNotifierError on a network failure / 5xx / 429 (the worker retries later with backoff), or PermanentNotifierError on a 4xx (the worker dead-letters it immediately — retrying a 400 is wasted effort). All retry/DLQ policy lives in the worker.

HMAC needs byte-precision, and two properties must not be conflated. Integrity / anti-forgery comes from signing the exact bytes sent over the wire — any change in JSON key order or whitespace breaks verification, so you sign the literal payload, never a re-serialized copy. Anti-replay is separate: the timestamp is signed into the payload so the receiver can reject stale or replayed requests by freshness — a valid signature alone does not stop a replay. And the signing secret stays in settings (SecretStr), never reaching the logs (a fingerprint at most) — enforced and tested, not assumed.
