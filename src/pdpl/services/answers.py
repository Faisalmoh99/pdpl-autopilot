"""Record a tenant's questionnaire answers as evidence (ADR-0005).

An answer IS evidence for a control. Each answer is written as one row in
the existing `evidence` table (type='questionnaire_answer'), payload
`{"question_code": ..., "answer": ...}`. Answers are append-only: changing
an answer inserts a NEW row with a newer collected_at; nothing is
overwritten. The decision engine reads the latest answer per question
(see pdpl.services.decision.load_tenant_answers).

This is a service function with no HTTP route this session — the route is a
deferred, thin wrapper (ADR-0005). All writes for one call commit in a
single transaction; validation happens BEFORE any write, so a bad answer
set writes nothing.
"""

from __future__ import annotations

import json
from uuid import UUID

import uuid6
from sqlalchemy import text

from pdpl.db.audit import write_event
from pdpl.db.session import session_scope
from pdpl.observability.correlation import current_correlation_id
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter
from pdpl.services.checks import TenantNotFound

_log = get_logger("pdpl.answers")

QUESTIONNAIRE_SOURCE = "questionnaire:v1"
_ALLOWED_ANSWERS: frozenset[str] = frozenset({"yes", "no"})


class UnknownQuestion(Exception):
    """A provided question code does not exist in the `questions` catalogue."""


class InvalidAnswer(Exception):
    """A provided answer is not in the allowed set for its question type."""


_SELECT_TENANT_ACTIVE_SQL = text(
    "SELECT id FROM tenants WHERE id = :tenant_id AND status = 'active'"
)
_SELECT_KNOWN_CODES_SQL = text("SELECT code FROM questions WHERE code = ANY(:codes)")
_INSERT_EVIDENCE_SQL = text(
    """
    INSERT INTO evidence (id, tenant_id, type, source, payload, collected_at)
    VALUES (
        :id, :tenant_id, 'questionnaire_answer', :source,
        CAST(:payload AS jsonb), now()
    )
    """
)


async def record_answers(
    tenant_id: UUID,
    answers: dict[str, str],
    *,
    source: str = QUESTIONNAIRE_SOURCE,
    correlation_id: UUID | None = None,
) -> list[UUID]:
    """Record questionnaire answers for a tenant. Returns new evidence ids.

    `answers` maps question_code -> 'yes'/'no'. Validation (tenant active,
    every code known, every answer allowed) runs first; on any failure the
    transaction rolls back and no evidence is written.
    """
    cid = correlation_id if correlation_id is not None else current_correlation_id()

    if not answers:
        return []

    # Validate answer values up front — deterministic, before touching the DB.
    bad = {code: val for code, val in answers.items() if val not in _ALLOWED_ANSWERS}
    if bad:
        raise InvalidAnswer(
            "answers must be one of "
            f"{sorted(_ALLOWED_ANSWERS)}; got: {bad}"
        )

    written: list[UUID] = []

    async with session_scope() as session:
        tenant_row = (
            await session.execute(_SELECT_TENANT_ACTIVE_SQL, {"tenant_id": tenant_id})
        ).first()
        if tenant_row is None:
            raise TenantNotFound(str(tenant_id))

        # Every provided code must exist in the questions catalogue.
        codes = list(answers.keys())
        known = {
            row.code
            for row in (
                await session.execute(_SELECT_KNOWN_CODES_SQL, {"codes": codes})
            ).all()
        }
        unknown = sorted(set(codes) - known)
        if unknown:
            raise UnknownQuestion(f"unknown question code(s): {unknown}")

        for code, answer in answers.items():
            evidence_id = uuid6.uuid7()
            await session.execute(
                _INSERT_EVIDENCE_SQL,
                {
                    "id": evidence_id,
                    "tenant_id": tenant_id,
                    "source": source,
                    "payload": json.dumps(
                        {"question_code": code, "answer": answer}
                    ),
                },
            )
            await write_event(
                session,
                event_type="evidence.recorded",
                actor_type="system",
                actor_id="service:answers.record_answers",
                tenant_id=tenant_id,
                entity_type="evidence",
                entity_id=evidence_id,
                payload={
                    "question_code": code,
                    "answer": answer,
                    "source": source,
                },
                correlation_id=cid,
            )
            written.append(evidence_id)

    counter("answers.recorded", count=str(len(written)))
    _log.info(
        "answers.recorded",
        tenant_id=str(tenant_id),
        count=len(written),
        source=source,
    )
    return written
