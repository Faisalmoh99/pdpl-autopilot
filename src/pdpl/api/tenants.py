"""POST /tenants — the first vertical slice.

Creates a tenant row AND its `tenant.created` audit_log row in ONE
transaction. The audit row is the proof that the application can write
through pdpl_app's INSERT grant on audit_log; the immutability of that
row is proved separately by tests/test_audit_immutability.py, which
connects directly as pdpl_app and exercises UPDATE/DELETE/TRUNCATE.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import uuid6
from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from pdpl.db.audit import write_event
from pdpl.db.session import session_scope
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter

router = APIRouter()
_log = get_logger("pdpl.tenants")

BusinessType = Literal["ecommerce", "clinic", "saas", "other"]


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    business_type: BusinessType


class TenantOut(BaseModel):
    id: UUID
    name: str
    business_type: BusinessType
    status: str


_INSERT_TENANT_SQL = text(
    """
    INSERT INTO tenants (id, name, business_type)
    VALUES (:id, :name, :business_type)
    RETURNING id, name, business_type, status
    """
)


@router.post(
    "/tenants",
    response_model=TenantOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant(body: TenantCreate) -> TenantOut:
    tenant_id = uuid6.uuid7()
    async with session_scope() as session:
        result = await session.execute(
            _INSERT_TENANT_SQL,
            {
                "id": tenant_id,
                "name": body.name,
                "business_type": body.business_type,
            },
        )
        row = result.first()
        assert row is not None

        await write_event(
            session,
            event_type="tenant.created",
            actor_type="system",
            actor_id="api:POST /tenants",
            tenant_id=tenant_id,
            entity_type="tenant",
            entity_id=tenant_id,
            payload={
                "name": body.name,
                "business_type": body.business_type,
            },
        )

    counter("tenant.created", business_type=body.business_type)
    _log.info(
        "tenant.created",
        tenant_id=str(tenant_id),
        business_type=body.business_type,
    )
    return TenantOut(
        id=row.id,
        name=row.name,
        business_type=row.business_type,
        status=row.status,
    )
