"""Readiness scoring + gap report — 100% deterministic (ADR-0007).

This module aggregates the per-control verdicts produced by the decision
engine (ADR-0006) into the product's headline output: a tenant's readiness
score and its gap report. Like the decision engine, it is part of the
deterministic core — it imports NOTHING from the AI layer, and the
architectural fitness function (`.importlinter`, tests/test_architecture.py)
fails the build if it ever does.

Two honest numbers, never one (ADR-0007 §1):

    SCORE    — a weighted readiness indicator over the controls we have
               actually assessed. NOT a compliance percentage and NOT a
               fine-risk number. A maturity signal.
    COVERAGE — how much of the applicable catalogue has been assessed at all.
               A score of 100 over 20% coverage is "perfect on what little we
               looked at", not "compliant".

The status -> credit map (ADR-0007 §2-4):

    compliant      -> 1.0   assessed
    partial        -> 0.5   assessed   (always also an OPEN GAP in the report)
    non_compliant  -> 0.0   assessed
    unknown        -> 0.0   assessed   (engine ran, could not decide)
    not_assessed   -> ---    not assessed (counts only against COVERAGE)
    not_applicable -> ---    excluded entirely (out of every denominator)

The scoring function is TOTAL over every status the data model allows: an
unrecognised status is a programming error and raises, never silently scores.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from pdpl.db.session import session_scope
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter
from pdpl.services.checks import TenantNotFound

_log = get_logger("pdpl.scoring")


# ---------------------------------------------------------------------
# The status model (ADR-0007 §2-4). Kept as data so the scoring function
# is total and auditable: every status the data model's CHECK allows has
# exactly one entry here. A status missing from these sets is a bug, and
# `_credit` raises on it rather than guessing.
# ---------------------------------------------------------------------
# Earns credit toward the score; counts in the score denominator.
_SCORE_CREDIT: dict[str, float] = {
    "compliant": 1.0,
    "partial": 0.5,
    "non_compliant": 0.0,
    "unknown": 0.0,
}
# Assessed but excluded from the score denominator (no evidence to score).
# Still counts as "applicable" -> drags COVERAGE down, honestly.
_NOT_SCORED = frozenset({"not_assessed"})
# Excluded from every denominator — the control does not apply to this tenant.
_EXCLUDED = frozenset({"not_applicable"})

# The full set the data model permits. Used to reject anything unexpected.
_ALL_STATUSES = frozenset(_SCORE_CREDIT) | _NOT_SCORED | _EXCLUDED

# A control with no current finding row has never been evaluated for this
# tenant — semantically identical to not_assessed (ADR-0007 §1, the
# active_controls LEFT JOIN current_findings read).
_MISSING_FINDING_STATUS = "not_assessed"


@dataclass(frozen=True)
class ReadinessScore:
    """The deterministic, explainable scoring result for one tenant.

    `score` is None when nothing has been assessed yet (no compliant/partial/
    non_compliant/unknown finding): there is no honest number to report, so we
    report none — not 0 (looks failing) and not 100 (looks perfect). Read it
    together with `coverage`.
    """

    score: float | None  # weighted readiness over assessed controls, 0-100
    coverage: float  # share of applicable controls assessed, 0-100
    counts: dict[str, int]  # status -> count, over applicable controls
    weighted_achieved: float  # Σ(weight · credit) over assessed controls
    weighted_assessed: float  # Σ(weight) over assessed controls
    applicable_controls: int  # controls in scope (excludes not_applicable)
    assessed_controls: int  # applicable controls with a real verdict


@dataclass(frozen=True)
class GapItem:
    """One row of the gap report: a control that is not 'done'."""

    control_code: str
    title_en: str
    status: str
    rationale: str
    severity_weight: float


def _credit(status: str) -> float:
    """Score credit for a status. Raises on any status outside the model."""
    if status not in _SCORE_CREDIT:
        raise ValueError(f"status {status!r} is not a scored status")
    return _SCORE_CREDIT[status]


# ---------------------------------------------------------------------
# Pure core: (status, weight) rows -> ReadinessScore. No DB, no I/O, total
# over every status. This is what the exhaustive unit tests drive directly.
# ---------------------------------------------------------------------
def compute_score(rows: list[tuple[str, float]]) -> ReadinessScore:
    """Aggregate (status, severity_weight) rows into a ReadinessScore.

    `rows` is one entry per applicable-or-not control for a tenant. Every
    status must be one the data model allows; an unknown status raises.
    """
    counts: dict[str, int] = {s: 0 for s in _ALL_STATUSES}
    weighted_achieved = 0.0
    weighted_assessed = 0.0
    applicable = 0
    assessed = 0

    for status, weight in rows:
        if status not in _ALL_STATUSES:
            raise ValueError(f"unrecognised finding status: {status!r}")
        counts[status] += 1

        if status in _EXCLUDED:
            continue
        applicable += 1

        if status in _NOT_SCORED:
            continue
        assessed += 1
        weighted_assessed += weight
        weighted_achieved += weight * _credit(status)

    # Score is undefined when nothing scorable was assessed, or (defensively)
    # when assessed controls all carry zero weight — never divide by zero.
    score: float | None
    if assessed == 0 or weighted_assessed == 0:
        score = None
    else:
        score = round(weighted_achieved / weighted_assessed * 100, 2)

    coverage = round(assessed / applicable * 100, 2) if applicable else 0.0

    return ReadinessScore(
        score=score,
        coverage=coverage,
        counts=counts,
        weighted_achieved=round(weighted_achieved, 2),
        weighted_assessed=round(weighted_assessed, 2),
        applicable_controls=applicable,
        assessed_controls=assessed,
    )


# ---------------------------------------------------------------------
# Pure core: control rows -> ordered gap report. A "gap" is any control that
# is not done and not excluded: non_compliant, partial, unknown, not_assessed.
# compliant controls are not gaps; not_applicable controls are out of scope.
# Ordered by severity DESC, then control_code for a stable, deterministic
# tie-break (ADR-0007).
# ---------------------------------------------------------------------
_GAP_STATUSES = frozenset({"non_compliant", "partial", "unknown", "not_assessed"})


def build_gap_report(
    rows: list[tuple[str, str, str, str, float]],
) -> list[GapItem]:
    """Build the ordered gap list from (code, title_en, status, rationale, weight) rows.

    Includes every control that is a gap; excludes compliant and
    not_applicable. Validates the status against the data model so an
    unexpected value fails loudly rather than being silently dropped.
    """
    gaps: list[GapItem] = []
    for code, title_en, status, rationale, weight in rows:
        if status not in _ALL_STATUSES:
            raise ValueError(f"unrecognised finding status: {status!r}")
        if status not in _GAP_STATUSES:
            continue
        gaps.append(
            GapItem(
                control_code=code,
                title_en=title_en,
                status=status,
                rationale=rationale,
                severity_weight=weight,
            )
        )
    # Highest severity first; control_code breaks ties deterministically.
    gaps.sort(key=lambda g: (-g.severity_weight, g.control_code))
    return gaps


# ---------------------------------------------------------------------
# DB read. Active controls LEFT JOIN the tenant's CURRENT findings
# (valid_to IS NULL). A control with no current finding row is treated as
# not_assessed (ADR-0007 §1). One read for both outputs.
# ---------------------------------------------------------------------
_SELECT_TENANT_ACTIVE_SQL = text(
    "SELECT id FROM tenants WHERE id = :tenant_id AND status = 'active'"
)
_SELECT_CONTROL_STATUSES_SQL = text(
    """
    SELECT c.code               AS code,
           c.title_en           AS title_en,
           c.severity_weight    AS severity_weight,
           f.status             AS status,
           f.rationale          AS rationale
    FROM controls c
    LEFT JOIN findings f
           ON f.control_id = c.id
          AND f.tenant_id = :tenant_id
          AND f.valid_to IS NULL
    WHERE c.effective_from <= now()
      AND (c.effective_to IS NULL OR c.effective_to > now())
    ORDER BY c.code
    """
)


async def _load_control_statuses(
    tenant_id: UUID,
) -> list[tuple[str, str, float, str, str]]:
    """Read (code, title_en, weight, status, rationale) per active control.

    Verifies the tenant is active first. Missing finding -> not_assessed with
    a fixed deterministic rationale.
    """
    async with session_scope() as session:
        tenant_row = (
            await session.execute(_SELECT_TENANT_ACTIVE_SQL, {"tenant_id": tenant_id})
        ).first()
        if tenant_row is None:
            raise TenantNotFound(str(tenant_id))

        result_rows = (
            await session.execute(
                _SELECT_CONTROL_STATUSES_SQL, {"tenant_id": tenant_id}
            )
        ).all()

    out: list[tuple[str, str, float, str, str]] = []
    for r in result_rows:
        status = r.status if r.status is not None else _MISSING_FINDING_STATUS
        rationale = (
            r.rationale
            if r.rationale is not None
            else "no check run has evaluated this control for this tenant yet"
        )
        out.append((r.code, r.title_en, float(r.severity_weight), status, rationale))
    return out


async def score_tenant(tenant_id: UUID) -> ReadinessScore:
    """Compute one tenant's readiness score from its CURRENT findings.

    A derived read — it persists nothing (no scores table yet, ADR-0007). It
    emits a structured log line and a metric, but writes no audit_log row: the
    audit log records persisted state changes, and a transient score is not
    one. When a scores table or scheduled scoring lands, a persisted
    `score.computed` audit event becomes appropriate.
    """
    rows = await _load_control_statuses(tenant_id)
    result = compute_score([(status, weight) for _, _, weight, status, _ in rows])

    counter(
        "scoring.computed",
        has_score=str(result.score is not None),
    )
    _log.info(
        "scoring.computed",
        tenant_id=str(tenant_id),
        score=result.score,
        coverage=result.coverage,
        applicable_controls=result.applicable_controls,
        assessed_controls=result.assessed_controls,
    )
    return result


async def gap_report(tenant_id: UUID) -> list[GapItem]:
    """The tenant's open gaps, highest severity first (ADR-0007).

    Lists every control that is not done — non_compliant, partial, unknown,
    and not-yet-assessed — each with its deterministic status, rationale, and
    severity. compliant and not_applicable controls are excluded.
    """
    rows = await _load_control_statuses(tenant_id)
    gaps = build_gap_report(
        [(code, title, status, rationale, weight) for code, title, weight, status, rationale in rows]
    )

    _log.info(
        "scoring.gap_report",
        tenant_id=str(tenant_id),
        gap_count=len(gaps),
    )
    return gaps
