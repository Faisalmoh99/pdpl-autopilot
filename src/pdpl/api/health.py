"""GET /health — liveness + real DB connectivity check.

Single endpoint for now per ADR-0004 "What this ADR does not decide" §
"Health-check distinction between liveness and readiness". Split when a
load balancer or orchestrator needs the distinction.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from pdpl.db.session import get_engine
from pdpl.observability.logging import get_logger

router = APIRouter()
_log = get_logger("pdpl.health")


@router.get("/health")
async def health() -> dict:
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1 AS ok"))
            row = result.first()
            if row is None or row.ok != 1:
                raise RuntimeError("health check returned unexpected row")
    except Exception as exc:
        _log.error("health.db_unreachable", error=str(exc))
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"status": "ok"}
