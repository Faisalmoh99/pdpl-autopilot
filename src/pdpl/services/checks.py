"""Check service — orchestrates a check_run and the per-control findings.

A single deterministic pass for one tenant:
    open check_run -> for each active control, ask the decider for a
    status, then write the new finding row (or close-then-open the
    previous row on a status change, or skip on no-change) -> close
    check_run. Everything inside one transaction, so a mid-run failure
    rolls the whole run back.

The status decision is delegated to a `StatusDecider` callable. The
real engine now lives in pdpl.services.decision (ADR-0006): when no
decider is injected, run_check loads the tenant's latest questionnaire
answers inside the transaction and builds the deterministic decider as
the default. The engine is 100% deterministic; AI is never in this path.

`baseline_decider` (returns 'not_assessed' for every control) is retained
for explicit baseline runs and as the documented "decides nothing"
reference, but it is no longer the default.

Tests inject custom deciders to drive specific transitions. The HTTP
route does NOT expose a decider override — production code only ever
calls run_check() with the default.
"""

from __future__ import annotations

from typing import Callable
from uuid import UUID

import uuid6
from sqlalchemy import text

from pdpl.db.audit import write_event
from pdpl.db.outbox import enqueue_alert
from pdpl.db.session import session_scope
from pdpl.observability.correlation import current_correlation_id
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter
from pdpl.services.alerts import is_worsening_transition
from pdpl.services.decision import build_deterministic_decider, load_tenant_answers

_log = get_logger("pdpl.checks")


class TenantNotFound(Exception):
    """Raised when run_check is asked to run for a tenant that does not exist
    or is not in 'active' status. The route translates this to HTTP 404."""


# (status, rationale) — both required. The rationale is the deterministic
# explanation per ADR-0002 / docs/02-data-model.md "findings.rationale".
StatusDecider = Callable[[str], tuple[str, str]]


def baseline_decider(_control_code: str) -> tuple[str, str]:
    return (
        "not_assessed",
        "baseline check run: deterministic decision engine not yet "
        "implemented (see docs/02-data-model.md, deferred ADR on the "
        "control-status decision engine)",
    )


_SELECT_TENANT_ACTIVE_SQL = text(
    "SELECT id FROM tenants WHERE id = :tenant_id AND status = 'active'"
)
_SELECT_ACTIVE_CONTROLS_SQL = text(
    """
    SELECT id, code
    FROM controls
    WHERE effective_from <= now()
      AND (effective_to IS NULL OR effective_to > now())
    ORDER BY code
    """
)
_SELECT_CURRENT_FINDINGS_SQL = text(
    """
    SELECT id, control_id, status
    FROM findings
    WHERE tenant_id = :tenant_id AND valid_to IS NULL
    """
)
_INSERT_CHECK_RUN_SQL = text(
    """
    INSERT INTO check_runs (id, tenant_id, kind, status, correlation_id)
    VALUES (:id, :tenant_id, :kind, 'running', :correlation_id)
    """
)
_COMPLETE_CHECK_RUN_SQL = text(
    """
    UPDATE check_runs
    SET status = 'completed', completed_at = now()
    WHERE id = :id
    """
)
_CLOSE_OLD_FINDING_SQL = text(
    """
    UPDATE findings
    SET valid_to = now()
    WHERE id = :id AND valid_to IS NULL
    """
)
_INSERT_FINDING_SQL = text(
    """
    INSERT INTO findings (
        id, tenant_id, control_id, check_run_id,
        status, rationale
    )
    VALUES (
        :id, :tenant_id, :control_id, :check_run_id,
        :status, :rationale
    )
    """
)


async def run_check(
    tenant_id: UUID,
    *,
    kind: str = "manual",
    decider: StatusDecider | None = None,
    correlation_id: UUID | None = None,
) -> UUID:
    """Run one check pass for a tenant. Returns the new check_run id.

    Atomicity: every write inside one `session_scope` transaction.
    On any exception the whole run rolls back — no partial check_run,
    no orphan findings, no dangling audit rows.

    Idempotency: if the decider returns the same status as the current
    finding for a (tenant, control), no new row is written. A baseline
    re-run is therefore a no-op apart from the audit rows marking the
    check_run itself. The partial unique index `uniq_findings_current`
    is the DB-side safety net against the worst case.
    """
    cid = correlation_id if correlation_id is not None else current_correlation_id()
    check_run_id = uuid6.uuid7()

    async with session_scope() as session:
        tenant_row = (
            await session.execute(_SELECT_TENANT_ACTIVE_SQL, {"tenant_id": tenant_id})
        ).first()
        if tenant_row is None:
            raise TenantNotFound(str(tenant_id))

        # Default path: build the REAL deterministic decider from the
        # tenant's latest answers, read inside this transaction so the
        # engine and the findings it writes see one consistent snapshot.
        # An injected decider (tests only) bypasses this.
        if decider is None:
            answers = await load_tenant_answers(session, tenant_id)
            chosen_decider: StatusDecider = build_deterministic_decider(answers)
        else:
            chosen_decider = decider

        await session.execute(
            _INSERT_CHECK_RUN_SQL,
            {
                "id": check_run_id,
                "tenant_id": tenant_id,
                "kind": kind,
                "correlation_id": cid,
            },
        )
        await write_event(
            session,
            event_type="check_run.started",
            actor_type="system",
            actor_id="service:checks.run_check",
            tenant_id=tenant_id,
            entity_type="check_run",
            entity_id=check_run_id,
            payload={"kind": kind},
            correlation_id=cid,
        )

        current_rows = (
            await session.execute(
                _SELECT_CURRENT_FINDINGS_SQL, {"tenant_id": tenant_id}
            )
        ).all()
        current_by_control: dict[UUID, tuple[UUID, str]] = {
            row.control_id: (row.id, row.status) for row in current_rows
        }
        active_controls = (
            await session.execute(_SELECT_ACTIVE_CONTROLS_SQL)
        ).all()

        created = 0
        transitioned = 0
        unchanged = 0
        alerts_enqueued = 0

        for ctrl in active_controls:
            new_status, new_rationale = chosen_decider(ctrl.code)
            existing = current_by_control.get(ctrl.id)

            if existing is None:
                # First-ever finding for this (tenant, control).
                finding_id = uuid6.uuid7()
                await session.execute(
                    _INSERT_FINDING_SQL,
                    {
                        "id": finding_id,
                        "tenant_id": tenant_id,
                        "control_id": ctrl.id,
                        "check_run_id": check_run_id,
                        "status": new_status,
                        "rationale": new_rationale,
                    },
                )
                await write_event(
                    session,
                    event_type="finding.created",
                    actor_type="system",
                    actor_id="service:checks.run_check",
                    tenant_id=tenant_id,
                    entity_type="finding",
                    entity_id=finding_id,
                    payload={
                        "control_code": ctrl.code,
                        "status": new_status,
                        "check_run_id": str(check_run_id),
                    },
                    correlation_id=cid,
                )
                created += 1
                continue

            old_finding_id, old_status = existing
            if old_status == new_status:
                unchanged += 1
                continue

            # SCD Type 2 transition: close the old row, insert the new row.
            # Both statements are in this same transaction. Postgres `now()`
            # is constant within a transaction, so the old.valid_to equals
            # the new.valid_from to the microsecond — the history is a
            # clean handoff with no gap and no overlap.
            await session.execute(
                _CLOSE_OLD_FINDING_SQL, {"id": old_finding_id}
            )
            new_finding_id = uuid6.uuid7()
            await session.execute(
                _INSERT_FINDING_SQL,
                {
                    "id": new_finding_id,
                    "tenant_id": tenant_id,
                    "control_id": ctrl.id,
                    "check_run_id": check_run_id,
                    "status": new_status,
                    "rationale": new_rationale,
                },
            )
            await write_event(
                session,
                event_type="finding.transitioned",
                actor_type="system",
                actor_id="service:checks.run_check",
                tenant_id=tenant_id,
                entity_type="finding",
                entity_id=new_finding_id,
                payload={
                    "control_code": ctrl.code,
                    "from_status": old_status,
                    "to_status": new_status,
                    "closed_finding_id": str(old_finding_id),
                    "check_run_id": str(check_run_id),
                },
                correlation_id=cid,
            )
            transitioned += 1

            # Reliable alerting (ADR-0008): a worsening transition writes an
            # outbox row in THIS transaction — atomic with the finding, no
            # notifier import, no network call. The worker sends it later.
            if is_worsening_transition(old_status, new_status):
                await enqueue_alert(
                    session,
                    tenant_id=tenant_id,
                    finding_id=new_finding_id,
                    control_id=ctrl.id,
                    control_code=ctrl.code,
                    from_status=old_status,
                    to_status=new_status,
                    check_run_id=check_run_id,
                    correlation_id=cid,
                )
                alerts_enqueued += 1

        await session.execute(_COMPLETE_CHECK_RUN_SQL, {"id": check_run_id})
        await write_event(
            session,
            event_type="check_run.completed",
            actor_type="system",
            actor_id="service:checks.run_check",
            tenant_id=tenant_id,
            entity_type="check_run",
            entity_id=check_run_id,
            payload={
                "kind": kind,
                "controls_evaluated": len(active_controls),
                "findings_created": created,
                "findings_transitioned": transitioned,
                "findings_unchanged": unchanged,
                "alerts_enqueued": alerts_enqueued,
            },
            correlation_id=cid,
        )

    counter(
        "check_run.completed",
        kind=kind,
        created=str(created),
        transitioned=str(transitioned),
        unchanged=str(unchanged),
        alerts_enqueued=str(alerts_enqueued),
    )
    _log.info(
        "check_run.completed",
        check_run_id=str(check_run_id),
        tenant_id=str(tenant_id),
        kind=kind,
        controls_evaluated=len(active_controls),
        findings_created=created,
        findings_transitioned=transitioned,
        findings_unchanged=unchanged,
        alerts_enqueued=alerts_enqueued,
    )
    return check_run_id
