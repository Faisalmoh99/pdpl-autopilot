"""GET /tenants/{tenant_id}/readiness — the readiness report (read-only).

Thin HTTP wrapper around `pdpl.services.scoring.readiness_report`, which reads
the tenant's CURRENT findings in ONE snapshot and returns the score + gaps. It
does NOT run a check — that is POST /tenants/{id}/checks. So a tenant that has
never run a check reads, naturally from the active-controls LEFT JOIN, as
coverage 0%, readiness_score null, and every applicable control listed as a
not_assessed gap.

Field naming is deliberate (ADR-0007): `readiness_score` is a readiness /
maturity indicator, NOT a compliance percentage and NOT a fine-risk number.
It is `null` — never 0, never 100 — when no control has been assessed, because
neither "you failed everything" nor "you passed everything" is true before any
assessment. There is intentionally no field named `compliance_score`.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pdpl.services.checks import TenantNotFound
from pdpl.services.scoring import readiness_report

router = APIRouter()


class GapOut(BaseModel):
    control_code: str
    title_en: str
    title_ar: str
    status: str
    rationale: str
    severity_weight: float


class ReadinessOut(BaseModel):
    tenant_id: UUID
    # null when no control has been assessed yet — never 0, never 100.
    readiness_score: float | None
    coverage_pct: float
    assessed_controls: int
    applicable_controls: int
    counts: dict[str, int]
    gaps: list[GapOut]


@router.get(
    "/tenants/{tenant_id}/readiness",
    response_model=ReadinessOut,
)
async def get_readiness(tenant_id: UUID) -> ReadinessOut:
    try:
        report = await readiness_report(tenant_id)
    except TenantNotFound:
        raise HTTPException(status_code=404, detail="tenant not found")

    score = report.score
    return ReadinessOut(
        tenant_id=tenant_id,
        readiness_score=score.score,
        coverage_pct=score.coverage,
        assessed_controls=score.assessed_controls,
        applicable_controls=score.applicable_controls,
        counts=score.counts,
        gaps=[
            GapOut(
                control_code=g.control_code,
                title_en=g.title_en,
                title_ar=g.title_ar,
                status=g.status,
                rationale=g.rationale,
                severity_weight=g.severity_weight,
            )
            for g in report.gaps
        ],
    )
