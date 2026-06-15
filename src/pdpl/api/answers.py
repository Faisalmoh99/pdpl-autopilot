"""POST /tenants/{tenant_id}/answers — record questionnaire answers.

Thin HTTP wrapper around `pdpl.services.answers.record_answers`. The route is
transport only:

  * pydantic validates SHAPE — a non-empty list of {question_code, answer},
    each a non-empty string, with no duplicate question_code in one request.
  * the SERVICE owns SEMANTICS — that the question exists and the answer is
    valid for its answer_type. We deliberately do NOT constrain `answer` to
    yes/no here: valid answers are answer_type-dependent, and baking the
    yes/no set into the transport layer would break the moment a non-yes_no
    question type lands. The service is the single source of truth.

Exception mapping: UnknownQuestion / InvalidAnswer -> 422, TenantNotFound ->
404, all carrying the request correlation_id via the global error handler.
record_answers validates before any write and runs in one transaction, so a
bad submission rolls back wholly — the route adds no partial-write paths.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from pdpl.services.answers import (
    InvalidAnswer,
    UnknownQuestion,
    record_answers,
)
from pdpl.services.checks import TenantNotFound

router = APIRouter()


class AnswerItem(BaseModel):
    question_code: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)


class AnswersIn(BaseModel):
    answers: list[AnswerItem] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _no_duplicate_question_codes(self) -> "AnswersIn":
        # A duplicate question_code in one request is ambiguous: the service
        # takes a {code: answer} map, so a list with duplicates would silently
        # collapse to last-wins. Reject it explicitly (-> 422) instead. This is
        # SHAPE validation of the request, not domain logic. Cross-time
        # resubmission of the same question is a different thing entirely and
        # is allowed — it appends a new evidence row, latest-wins (ADR-0005).
        codes = [item.question_code for item in self.answers]
        dupes = sorted({c for c in codes if codes.count(c) > 1})
        if dupes:
            raise ValueError(f"duplicate question_code(s) in request: {dupes}")
        return self


class AnswersOut(BaseModel):
    tenant_id: UUID
    recorded: list[UUID]
    count: int


@router.post(
    "/tenants/{tenant_id}/answers",
    response_model=AnswersOut,
    status_code=status.HTTP_201_CREATED,
)
async def submit_answers(tenant_id: UUID, body: AnswersIn) -> AnswersOut:
    answers = {item.question_code: item.answer for item in body.answers}
    try:
        recorded = await record_answers(tenant_id, answers)
    except TenantNotFound:
        raise HTTPException(status_code=404, detail="tenant not found")
    except (UnknownQuestion, InvalidAnswer) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return AnswersOut(
        tenant_id=tenant_id,
        recorded=recorded,
        count=len(recorded),
    )
