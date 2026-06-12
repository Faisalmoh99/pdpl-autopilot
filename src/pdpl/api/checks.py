"""POST /tenants/{tenant_id}/checks — trigger a check run for a tenant.

Thin HTTP wrapper around `pdpl.services.checks.run_check`. The route
deliberately does NOT expose a way to override the status decider —
production traffic always uses the default. The decider injection
exists only for tests that need to drive a specific transition.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from pdpl.services.checks import TenantNotFound, run_check

router = APIRouter()


class CheckRunOut(BaseModel):
    check_run_id: UUID
    tenant_id: UUID
    kind: str


@router.post(
    "/tenants/{tenant_id}/checks",
    response_model=CheckRunOut,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_check(tenant_id: UUID) -> CheckRunOut:
    try:
        check_run_id = await run_check(tenant_id, kind="manual")
    except TenantNotFound:
        raise HTTPException(status_code=404, detail="tenant not found")
    return CheckRunOut(
        check_run_id=check_run_id,
        tenant_id=tenant_id,
        kind="manual",
    )
