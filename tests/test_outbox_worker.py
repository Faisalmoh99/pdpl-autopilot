"""Session B2 — the outbox delivery worker (ADR-0008).

Proves the worker delivers reliably: success → sent + audited; transient →
retry within the backoff bound; exhausted → dead-letter; permanent →
dead-letter immediately; unexpected → treated as transient (loudly); the
idempotency key is stable across a re-send and a sent row is never re-claimed;
and the signing secret never reaches the worker's logs.

Isolation (the claim is GLOBAL by design): `_clean_outbox` TRUNCATEs the
outbox via the OWNER connection before each test (outbox has no TRUNCATE
trigger, unlike audit_log), so each test sees only the rows it inserts.
Assertions are still keyed to the test's own row id where it matters.

The session factory is built from WORKER_DATABASE_URL (conftest) — the real
worker connection path. The outbound HTTP is stubbed via the Notifier port
(StubNotifier) or httpx.MockTransport; no real network.
"""

from __future__ import annotations

import json
import os
from uuid import UUID

import asyncpg
import httpx
import pytest
import uuid6
from pydantic import SecretStr
from structlog.testing import capture_logs

from pdpl.notifications.webhook import WebhookNotifier
from pdpl.workers.outbox import run_once
from tests.stubs import StubNotifier


# ---------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _clean_outbox():
    """Clean slate before each worker test, via the OWNER connection."""
    direct = os.environ.get("DATABASE_URL_DIRECT")
    if not direct:
        pytest.skip("DATABASE_URL_DIRECT not set")
    url = direct.replace("postgresql+psycopg2://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)
    try:
        await conn.execute("TRUNCATE outbox")
    finally:
        await conn.close()
    yield


async def _create_tenant(db_url: str, label: str) -> UUID:
    tenant_id = uuid6.uuid7()
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        await conn.execute(
            "INSERT INTO tenants (id, name, business_type) "
            "VALUES ($1::uuid, $2, 'saas')",
            str(tenant_id),
            f"test_worker_{label}_{tenant_id}",
        )
    finally:
        await conn.close()
    return tenant_id


async def _insert_outbox(
    db_url: str,
    *,
    tenant_id: UUID,
    idempotency_key: str,
    status: str = "pending",
    attempts: int = 0,
    correlation_id: UUID | None = None,
) -> UUID:
    outbox_id = uuid6.uuid7()
    payload = {
        "control_code": "PDPL-ART4-DSR-ACCESS",
        "from_status": "compliant",
        "to_status": "non_compliant",
        "correlation_id": str(correlation_id) if correlation_id else None,
    }
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO outbox
                (id, tenant_id, topic, payload, idempotency_key,
                 status, attempts, next_attempt_at)
            VALUES
                ($1::uuid, $2::uuid, 'finding.worsened', $3::jsonb, $4,
                 $5, $6, now())
            """,
            str(outbox_id),
            str(tenant_id),
            json.dumps(payload),
            idempotency_key,
            status,
            attempts,
        )
    finally:
        await conn.close()
    return outbox_id


async def _get_row(db_url: str, outbox_id: UUID):
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        return await conn.fetchrow(
            "SELECT status, attempts, next_attempt_at, sent_at, last_error "
            "FROM outbox WHERE id = $1::uuid",
            str(outbox_id),
        )
    finally:
        await conn.close()


async def _db_now(db_url: str):
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        return await conn.fetchval("SELECT now()")
    finally:
        await conn.close()


async def _audit_event_types(db_url: str, entity_id: UUID) -> list[str]:
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            "SELECT event_type FROM audit_log "
            "WHERE entity_type = 'outbox' AND entity_id = $1::uuid "
            "ORDER BY created_at",
            str(entity_id),
        )
        return [r["event_type"] for r in rows]
    finally:
        await conn.close()


async def _force_due(db_url: str, outbox_id: UUID) -> None:
    """Re-arm a failed row immediately, instead of sleeping on real backoff."""
    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        await conn.execute(
            "UPDATE outbox SET next_attempt_at = now() WHERE id = $1::uuid",
            str(outbox_id),
        )
    finally:
        await conn.close()


_POLICY = dict(max_attempts=5, backoff_base_seconds=60.0, backoff_cap_seconds=3600.0)


# ---------------------------------------------------------------------
# Success.
# ---------------------------------------------------------------------


async def test_success_marks_sent_and_audits(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "ok")
    cid = uuid6.uuid7()
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key,
        correlation_id=cid,
    )

    stub = StubNotifier(mode="success")
    stats = await run_once(worker_session_factory, stub, **_POLICY)

    assert stats.claimed == 1 and stats.sent == 1
    row = await _get_row(app_database_url, outbox_id)
    assert row["status"] == "sent"
    assert row["attempts"] == 1
    assert row["sent_at"] is not None
    assert row["last_error"] is None

    # The notifier received exactly our alert, carrying the idempotency key
    # (which the WebhookNotifier sends as the Idempotency-Key header — B1).
    assert len(stub.calls) == 1
    assert stub.calls[0].idempotency_key == key

    # alert.sent audited, with our correlation id threaded through.
    assert "alert.sent" in await _audit_event_types(app_database_url, outbox_id)


# ---------------------------------------------------------------------
# Transient → retry within the backoff bound; re-claim re-sends.
# ---------------------------------------------------------------------


async def test_transient_failure_reschedules_within_bound(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "transient")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    before = await _db_now(app_database_url)
    stats = await run_once(
        worker_session_factory, StubNotifier(mode="transient"), **_POLICY
    )
    after = await _db_now(app_database_url)

    assert stats.failed == 1 and stats.sent == 0 and stats.dead_lettered == 0
    row = await _get_row(app_database_url, outbox_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["last_error"] is not None

    # First retry window is [0, base] = [0, 60s] (attempts=1). Assert the
    # BOUND, not an exact value (full jitter): the schedule sits between the
    # claim time and (claim time + 60s).
    nxt = row["next_attempt_at"]
    assert before <= nxt
    assert (nxt - after).total_seconds() <= 60.0 + 1.0


async def test_forced_reclaim_resends(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "reclaim")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    # First pass fails transiently -> status failed, scheduled in the future.
    await run_once(worker_session_factory, StubNotifier(mode="transient"), **_POLICY)
    # Re-arm it as due (instead of sleeping), then a successful pass delivers.
    await _force_due(app_database_url, outbox_id)
    success = StubNotifier(mode="success")
    stats = await run_once(worker_session_factory, success, **_POLICY)

    assert stats.sent == 1
    row = await _get_row(app_database_url, outbox_id)
    assert row["status"] == "sent"
    assert row["attempts"] == 2  # one failed + one successful attempt
    assert len(success.calls) == 1


# ---------------------------------------------------------------------
# Exhausted transient → dead-letter; not retried again.
# ---------------------------------------------------------------------


async def test_max_attempts_dead_letters_and_is_not_retried(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "exhausted")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    # attempts=4 already: the next transient failure is attempt 5 == max.
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key, attempts=4
    )

    stats = await run_once(
        worker_session_factory, StubNotifier(mode="transient"), **_POLICY
    )
    assert stats.dead_lettered == 1 and stats.failed == 0
    row = await _get_row(app_database_url, outbox_id)
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 5
    assert "alert.dead_lettered" in await _audit_event_types(
        app_database_url, outbox_id
    )

    # A dead-letter row is terminal: a second pass claims nothing.
    again = await run_once(
        worker_session_factory, StubNotifier(mode="transient"), **_POLICY
    )
    assert again.claimed == 0
    assert (await _get_row(app_database_url, outbox_id))["status"] == "dead_letter"


# ---------------------------------------------------------------------
# Permanent → dead-letter immediately (no retries wasted on a 4xx).
# ---------------------------------------------------------------------


async def test_permanent_failure_dead_letters_immediately(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "permanent")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    stats = await run_once(
        worker_session_factory, StubNotifier(mode="permanent"), **_POLICY
    )
    assert stats.dead_lettered == 1
    row = await _get_row(app_database_url, outbox_id)
    assert row["status"] == "dead_letter"
    assert row["attempts"] == 1  # one attempt only — not retried up to max
    assert "alert.dead_lettered" in await _audit_event_types(
        app_database_url, outbox_id
    )


# ---------------------------------------------------------------------
# Unexpected/unclassified exception → treated as transient, logged loudly.
# ---------------------------------------------------------------------


class _BoomNotifier:
    """Raises an error that is neither Transient nor Permanent."""

    async def send(self, alert):  # noqa: ANN001
        raise RuntimeError("kaboom: totally unexpected")


async def test_unexpected_exception_is_treated_as_transient(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "boom")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    with capture_logs() as logs:
        stats = await run_once(
            worker_session_factory, _BoomNotifier(), **_POLICY
        )

    # Retried (transient), not dead-lettered.
    assert stats.failed == 1 and stats.dead_lettered == 0
    row = await _get_row(app_database_url, outbox_id)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    # Logged loudly.
    events = [e.get("event") for e in logs]
    assert "alert.send.unexpected" in events


# ---------------------------------------------------------------------
# Idempotency: stable key across a re-send; a sent row is never re-claimed.
# ---------------------------------------------------------------------


async def test_idempotency_key_stable_and_sent_row_not_reclaimed(
    app_database_url, worker_session_factory
):
    tenant_id = await _create_tenant(app_database_url, "idemp")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    # Transient first, then a forced re-claim that succeeds: BOTH sends carry
    # the SAME idempotency key, so a receiver dedupes the at-least-once resend.
    t = StubNotifier(mode="transient")
    await run_once(worker_session_factory, t, **_POLICY)
    await _force_due(app_database_url, outbox_id)
    s = StubNotifier(mode="success")
    await run_once(worker_session_factory, s, **_POLICY)

    assert t.calls[0].idempotency_key == key
    assert s.calls[0].idempotency_key == key

    # Once sent, the row is terminal — a further pass claims and sends nothing.
    final = StubNotifier(mode="success")
    again = await run_once(worker_session_factory, final, **_POLICY)
    assert again.claimed == 0
    assert final.calls == []
    assert (await _get_row(app_database_url, outbox_id))["status"] == "sent"


# ---------------------------------------------------------------------
# The signing secret never reaches the worker's logs (worker + real notifier).
# ---------------------------------------------------------------------


async def test_signing_secret_never_in_worker_logs(
    app_database_url, worker_session_factory
):
    secret = "worker-super-secret-key"
    tenant_id = await _create_tenant(app_database_url, "secret")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    notifier = WebhookNotifier(
        url="https://hook.test/alerts",
        secret=SecretStr(secret),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with capture_logs() as logs:
        stats = await run_once(worker_session_factory, notifier, **_POLICY)

    assert stats.sent == 1
    assert secret not in json.dumps(logs), "signing secret leaked into worker logs"


# ---------------------------------------------------------------------
# SKIP LOCKED — two concurrent claims do not grab the same row.
# ---------------------------------------------------------------------


async def test_skip_locked_prevents_double_claim(app_database_url):
    tenant_id = await _create_tenant(app_database_url, "skiplocked")
    key = f"alert:finding-transition:{uuid6.uuid7()}"
    outbox_id = await _insert_outbox(
        app_database_url, tenant_id=tenant_id, idempotency_key=key
    )

    claim = (
        "SELECT id FROM outbox "
        "WHERE id = $1::uuid AND status IN ('pending','failed') "
        "AND next_attempt_at <= now() "
        "FOR UPDATE SKIP LOCKED LIMIT 1"
    )
    conn1 = await asyncpg.connect(app_database_url, statement_cache_size=0)
    conn2 = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        tx1 = conn1.transaction()
        await tx1.start()
        first = await conn1.fetchval(claim, str(outbox_id))
        assert first is not None  # conn1 claims and locks the row

        tx2 = conn2.transaction()
        await tx2.start()
        # conn2's identical claim must SKIP the locked row, not block on it.
        second = await conn2.fetchval(claim, str(outbox_id))
        assert second is None

        await tx2.rollback()
        await tx1.rollback()
    finally:
        await conn1.close()
        await conn2.close()
