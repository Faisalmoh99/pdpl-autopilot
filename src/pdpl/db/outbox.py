"""Transactional outbox writer (ADR-0008).

The enqueue path is a plain DB write — like db/audit.py. It inserts one
`outbox` row inside the caller's transaction, so the alert intent commits
atomically with the finding transition that caused it. It does NOT import
the Notifier or make any network call: that keeps run_check (and the rest
of the import-linter–guarded core) free of any alerting dependency. The
worker (Session B) is what reads these rows and sends them.

Idempotency: the key is derived 1:1 from the new finding row's id, and the
column carries a UNIQUE constraint — a duplicate enqueue is rejected at the
DB layer (ADR-0008 §5).
"""

from __future__ import annotations

import json
from uuid import UUID

import uuid6
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pdpl.db.audit import write_event
from pdpl.observability.correlation import current_correlation_id

# The single outbox event kind today (ADR-0008). Free-text in the table so
# future event types can share it.
TOPIC_FINDING_WORSENED = "finding.worsened"


def idempotency_key_for_finding(finding_id: UUID) -> str:
    """The per-alert idempotency key, 1:1 with a transition (ADR-0008 §5).

    Every worsening transition creates exactly one new finding row with a
    fresh UUID v7, so its id uniquely identifies the alert. The worker
    carries this key into the webhook so a receiver can dedupe a re-delivery.
    """
    return f"alert:finding-transition:{finding_id}"


_INSERT_OUTBOX_SQL = text(
    """
    INSERT INTO outbox (id, tenant_id, topic, payload, idempotency_key)
    VALUES (:id, :tenant_id, :topic, CAST(:payload AS jsonb), :idempotency_key)
    """
)


async def enqueue_alert(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    finding_id: UUID,
    control_id: UUID,
    control_code: str,
    from_status: str,
    to_status: str,
    check_run_id: UUID,
    correlation_id: UUID | None = None,
) -> UUID:
    """Insert one outbox row for a worsening transition, plus an
    `alert.enqueued` audit event, inside the caller's transaction.

    Pass the SAME session run_check uses, so the outbox row, the finding,
    and the audit row all commit (or roll back) together. Returns the new
    outbox row id. The new row is `pending` with `next_attempt_at = now()`
    (immediately due) by table default.
    """
    outbox_id = uuid6.uuid7()
    cid = correlation_id if correlation_id is not None else current_correlation_id()
    payload = {
        "tenant_id": str(tenant_id),
        "control_id": str(control_id),
        "control_code": control_code,
        "from_status": from_status,
        "to_status": to_status,
        "finding_id": str(finding_id),
        "check_run_id": str(check_run_id),
        "correlation_id": str(cid) if cid is not None else None,
    }
    await session.execute(
        _INSERT_OUTBOX_SQL,
        {
            "id": outbox_id,
            "tenant_id": tenant_id,
            "topic": TOPIC_FINDING_WORSENED,
            "payload": json.dumps(payload),
            "idempotency_key": idempotency_key_for_finding(finding_id),
        },
    )
    await write_event(
        session,
        event_type="alert.enqueued",
        actor_type="system",
        actor_id="service:checks.run_check",
        tenant_id=tenant_id,
        entity_type="outbox",
        entity_id=outbox_id,
        payload={
            "topic": TOPIC_FINDING_WORSENED,
            "control_code": control_code,
            "from_status": from_status,
            "to_status": to_status,
            "finding_id": str(finding_id),
            "check_run_id": str(check_run_id),
        },
        correlation_id=cid,
    )
    return outbox_id
