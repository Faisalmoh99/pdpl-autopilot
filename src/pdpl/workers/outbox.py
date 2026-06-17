"""Outbox delivery worker (ADR-0008, Session B2).

The worker that turns a durably-enqueued alert (Session A) into a delivered
one. It polls the `outbox` table, claims a due row with FOR UPDATE SKIP
LOCKED, sends it through the Notifier port (the HMAC webhook in production),
and records the outcome — marking `sent`, rescheduling with full-jitter
backoff, or dead-lettering after `max_attempts`.

Connection (a MECHANISM, not a deployment note): the worker builds its OWN
async engine from WORKER_DATABASE_URL — a session-level / direct connection
AS pdpl_app, never the transaction pooler (FOR UPDATE + a held transaction do
not fit the transaction pooler) and never the owner role. `pdpl_app` has
SELECT/INSERT/UPDATE on outbox (migration 0005); UPDATE is what lets the
worker record an outcome.

Transaction model (ADR-0008 §8): one row per claim, claim-send-commit in a
single short transaction with the row lock held across the send. The send is
bounded by the WebhookNotifier's overall deadline (B1), which is therefore the
lock-hold ceiling. A send failure does NOT propagate (that would roll back the
claim) — it is caught, mapped to a status change, and the transaction commits
the new status.

Delivery is at-least-once + idempotency (the Idempotency-Key carried by the
notifier), NOT exactly-once: a crash between a successful send and the commit
re-sends, and the receiver dedupes on the key.
"""

from __future__ import annotations

import asyncio
import json
import random
import signal
from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pdpl.config import Settings, get_settings
from pdpl.db.audit import write_event
from pdpl.notifications.port import (
    Notifier,
    OutboxAlert,
    PermanentNotifierError,
    TransientNotifierError,
)
from pdpl.notifications.webhook import webhook_notifier_from_settings
from pdpl.observability.logging import configure_logging, get_logger
from pdpl.observability.metrics import counter

import structlog

_log = get_logger("pdpl.workers.outbox")


@dataclass
class WorkerStats:
    """What one run_once() pass did. Returned for tests and tick logging."""

    claimed: int = 0
    sent: int = 0
    failed: int = 0
    dead_lettered: int = 0


# ---------------------------------------------------------------------
# The worker's own engine — same construction as db/session.py but bound to
# WORKER_DATABASE_URL (its own session-level/direct pdpl_app connection).
# Kept inline (not a shared import) to leave the app's hot path untouched;
# the connect_args mirror db/session.py and must stay in sync.
# ---------------------------------------------------------------------


def make_worker_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
        },
    )


def make_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )


# ---------------------------------------------------------------------
# SQL.
# ---------------------------------------------------------------------

_CLAIM_SQL = text(
    """
    SELECT id, tenant_id, topic, payload, idempotency_key, attempts
    FROM outbox
    WHERE status IN ('pending','failed') AND next_attempt_at <= now()
    ORDER BY next_attempt_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
    """
)
_MARK_SENT_SQL = text(
    """
    UPDATE outbox
    SET status = 'sent', sent_at = now(), attempts = :attempts,
        last_error = NULL, updated_at = now()
    WHERE id = :id
    """
)
_MARK_FAILED_SQL = text(
    """
    UPDATE outbox
    SET status = 'failed', attempts = :attempts, last_error = :err,
        next_attempt_at = now() + make_interval(secs => :delay),
        updated_at = now()
    WHERE id = :id
    """
)
_MARK_DEAD_SQL = text(
    """
    UPDATE outbox
    SET status = 'dead_letter', attempts = :attempts, last_error = :err,
        updated_at = now()
    WHERE id = :id
    """
)


# ---------------------------------------------------------------------
# Core processing.
# ---------------------------------------------------------------------


def _backoff_delay_cap(
    attempts: int, *, base_seconds: float, cap_seconds: float
) -> float:
    """Full-jitter cap for the NEXT attempt: min(base * 2^(attempts-1), cap).
    `attempts` is the post-increment count (1 after the first failure), so the
    first retry window is [0, base] and it doubles thereafter."""
    return min(base_seconds * (2 ** (attempts - 1)), cap_seconds)


def _coerce_payload(raw: object) -> dict:
    # A jsonb column read via raw SQL can come back as a str (asyncpg) or a
    # dict — accept both.
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}


def _parse_correlation_id(payload: dict) -> UUID | None:
    raw = payload.get("correlation_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def _mark_sent(
    session: AsyncSession, row, alert: OutboxAlert, attempt_no: int, cid
) -> str:
    await session.execute(
        _MARK_SENT_SQL, {"id": row.id, "attempts": attempt_no}
    )
    await write_event(
        session,
        event_type="alert.sent",
        actor_type="system",
        actor_id="worker:outbox",
        tenant_id=row.tenant_id,
        entity_type="outbox",
        entity_id=row.id,
        payload={
            "idempotency_key": alert.idempotency_key,
            "topic": row.topic,
            "attempts": attempt_no,
        },
        correlation_id=cid,
    )
    _log.info("alert.sent", attempts=attempt_no)
    counter("alert.sent", attempts=str(attempt_no))
    return "sent"


async def _dead_letter(
    session: AsyncSession,
    row,
    alert: OutboxAlert,
    attempt_no: int,
    err: str,
    cid,
    *,
    reason: str,
) -> str:
    await session.execute(
        _MARK_DEAD_SQL,
        {"id": row.id, "attempts": attempt_no, "err": err[:1000]},
    )
    await write_event(
        session,
        event_type="alert.dead_lettered",
        actor_type="system",
        actor_id="worker:outbox",
        tenant_id=row.tenant_id,
        entity_type="outbox",
        entity_id=row.id,
        payload={
            "idempotency_key": alert.idempotency_key,
            "topic": row.topic,
            "attempts": attempt_no,
            "reason": reason,
            "last_error": err[:1000],
        },
        correlation_id=cid,
    )
    _log.warning(
        "alert.dead_lettered", reason=reason, attempts=attempt_no, last_error=err
    )
    counter("alert.dead_lettered", reason=reason)
    return "dead_lettered"


async def _retry_later(
    session: AsyncSession,
    row,
    attempt_no: int,
    err: str,
    *,
    base_seconds: float,
    cap_seconds: float,
) -> str:
    delay_cap = _backoff_delay_cap(
        attempt_no, base_seconds=base_seconds, cap_seconds=cap_seconds
    )
    delay = random.uniform(0.0, delay_cap)  # full jitter
    await session.execute(
        _MARK_FAILED_SQL,
        {"id": row.id, "attempts": attempt_no, "err": err[:1000], "delay": delay},
    )
    # No audit row per transient retry (ADR-0008 §9) — it lives in this log
    # line and the row's attempts/next_attempt_at columns.
    _log.warning(
        "alert.send.transient",
        attempts=attempt_no,
        retry_delay_cap_seconds=delay_cap,
        last_error=err,
    )
    counter("alert.send.transient", attempts=str(attempt_no))
    return "failed"


async def _process_row(
    session: AsyncSession,
    row,
    notifier: Notifier,
    *,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
) -> str:
    payload = _coerce_payload(row.payload)
    cid = _parse_correlation_id(payload)
    alert = OutboxAlert(
        id=row.id,
        topic=row.topic,
        idempotency_key=row.idempotency_key,
        payload=payload,
        attempts=row.attempts,
    )
    attempt_no = row.attempts + 1

    bind: dict[str, str] = {
        "outbox_id": str(row.id),
        "idempotency_key": row.idempotency_key,
        "attempt": str(attempt_no),
    }
    if cid is not None:
        bind["correlation_id"] = str(cid)

    with structlog.contextvars.bound_contextvars(**bind):
        try:
            await notifier.send(alert)
        except PermanentNotifierError as exc:
            return await _dead_letter(
                session, row, alert, attempt_no, f"permanent: {exc}", cid,
                reason="permanent",
            )
        except TransientNotifierError as exc:
            err = f"transient: {exc}"
        except Exception as exc:  # noqa: BLE001 — default branch is deliberate
            # Neither transient nor permanent (incl. the base NotifierError the
            # webhook raises for an unclassifiable status): treat as transient
            # (ADR-0008 / B2) and log loudly.
            _log.warning(
                "alert.send.unexpected",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            err = f"unexpected {type(exc).__name__}: {exc}"
        else:
            return await _mark_sent(session, row, alert, attempt_no, cid)

        # Transient (explicit or by default): retry until max_attempts, then
        # dead-letter.
        if attempt_no >= max_attempts:
            return await _dead_letter(
                session, row, alert, attempt_no,
                f"max attempts ({max_attempts}) reached: {err}", cid,
                reason="max_attempts_exhausted",
            )
        return await _retry_later(
            session, row, attempt_no, err,
            base_seconds=backoff_base_seconds,
            cap_seconds=backoff_cap_seconds,
        )


# ---------------------------------------------------------------------
# Public API: run_once + run_forever.
# ---------------------------------------------------------------------


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    max_attempts: int = 5,
    backoff_base_seconds: float = 60.0,
    backoff_cap_seconds: float = 3600.0,
    max_batch: int = 100,
) -> WorkerStats:
    """Drain the currently-due outbox rows and return. One claim-send-commit
    per row (its own short transaction); a send failure is recorded, not
    raised. Returns when no due row remains or `max_batch` is reached — never
    busy-loops. The claim is global; isolation in tests is by row identity and
    a clean-slate fixture, not by a tenant filter on this production API."""
    stats = WorkerStats()
    for _ in range(max_batch):
        async with session_factory() as session:
            async with session.begin():
                row = (await session.execute(_CLAIM_SQL)).first()
                if row is None:
                    return stats
                stats.claimed += 1
                outcome = await _process_row(
                    session,
                    row,
                    notifier,
                    max_attempts=max_attempts,
                    backoff_base_seconds=backoff_base_seconds,
                    backoff_cap_seconds=backoff_cap_seconds,
                )
        if outcome == "sent":
            stats.sent += 1
        elif outcome == "failed":
            stats.failed += 1
        elif outcome == "dead_lettered":
            stats.dead_lettered += 1
    return stats


async def run_forever(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    poll_interval_seconds: float,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_cap_seconds: float,
) -> None:
    """Thin loop around run_once with a clean SIGINT/SIGTERM shutdown. A tick
    that raises is logged and the loop continues — the worker never dies on a
    single bad tick."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. non-Unix
            pass

    _log.info("outbox_worker.started", poll_interval_seconds=poll_interval_seconds)
    while not stop.is_set():
        try:
            stats = await run_once(
                session_factory,
                notifier,
                max_attempts=max_attempts,
                backoff_base_seconds=backoff_base_seconds,
                backoff_cap_seconds=backoff_cap_seconds,
            )
            if stats.claimed:
                _log.info("outbox_worker.tick", **asdict(stats))
        except Exception:  # noqa: BLE001 — a tick must never kill the worker
            _log.error("outbox_worker.tick_failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            pass
    _log.info("outbox_worker.stopped")


def _build_runtime(settings: Settings):
    """Build the worker's engine, session factory, and notifier from settings.
    Fails fast (RuntimeError/ValueError) if the worker DB url or webhook config
    is missing — a misconfigured worker never starts."""
    if settings.worker_database_url is None:
        raise RuntimeError(
            "WORKER_DATABASE_URL is required to run the outbox worker "
            "(a session-level/direct pdpl_app connection)"
        )
    engine = make_worker_engine(settings.worker_database_url.get_secret_value())
    factory = make_session_factory(engine)
    notifier = webhook_notifier_from_settings(settings)
    return engine, factory, notifier


async def _amain() -> None:
    settings = get_settings()
    engine, factory, notifier = _build_runtime(settings)
    try:
        await run_forever(
            factory,
            notifier,
            poll_interval_seconds=settings.outbox_poll_interval_seconds,
            max_attempts=settings.outbox_max_attempts,
            backoff_base_seconds=settings.outbox_backoff_base_seconds,
            backoff_cap_seconds=settings.outbox_backoff_cap_seconds,
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    configure_logging(get_settings().log_level)
    asyncio.run(_amain())
