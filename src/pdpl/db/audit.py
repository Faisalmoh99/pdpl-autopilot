"""audit_log writer — append-only by DB-layer grants (ADR-0003).

The writer never UPDATEs or DELETEs. It only INSERTs. The DB enforces this
even if the writer is misused, because the app connects as `pdpl_app` and
pdpl_app has had UPDATE/DELETE/TRUNCATE revoked on audit_log.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from pdpl.observability.correlation import current_correlation_id


_INSERT_SQL = text(
    """
    INSERT INTO audit_log (
        id, tenant_id, actor_type, actor_id, event_type,
        entity_type, entity_id, payload, correlation_id
    )
    VALUES (
        :id, :tenant_id, :actor_type, :actor_id, :event_type,
        :entity_type, :entity_id, CAST(:payload AS jsonb), :correlation_id
    )
    """
)


async def write_event(
    session: AsyncSession,
    *,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    tenant_id: UUID | None = None,
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: UUID | None = None,
) -> UUID:
    """Insert an audit_log row inside the caller's transaction.

    Pass the same `session` you use for the business write so both rows
    commit atomically. The correlation ID defaults to the current request's
    contextvar — pass an explicit value only in tests or scheduled jobs.
    """
    audit_id = uuid6.uuid7()
    cid = correlation_id if correlation_id is not None else current_correlation_id()
    await session.execute(
        _INSERT_SQL,
        {
            "id": audit_id,
            "tenant_id": tenant_id,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": json.dumps(payload or {}),
            "correlation_id": cid,
        },
    )
    return audit_id
