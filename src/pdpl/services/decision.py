"""Control-status decision engine — 100% deterministic (ADR-0006).

This module is the product's core safety line. It turns a tenant's
questionnaire answers into a real per-control verdict
(`compliant` / `non_compliant` / `partial` / `not_assessed`) using nothing
but plain Python rules.

    AI reads / suggests / explains. Deterministic logic decides.

No function in this module calls a model, a network service, or any
non-deterministic source. Given the same answers it returns the same
`(status, rationale)` every time. This module imports NOTHING from the AI
layer, and a verdict cannot be constructed any other way. That is the
guarantee, enforced by the module boundary — not a convention.

Rules are simple per-control functions in a registry (`_RULES`). For 3-5
controls a declarative rule engine is premature (see ADR-0006); the
registry boundary keeps a later migration to data-driven rules contained.

The `rationale` each rule returns is a deterministic explanation of *what
made the status what it is*. It is the value of `findings.rationale`. It is
NOT `findings.ai_explanation_ar` — that future Arabic prose explanation is
added by the AI layer only AFTER this layer has decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class ControlDecision:
    """The STRUCTURED result of deciding one control.

    Adds `unsatisfied_codes` to the long-standing `(status, rationale)` pair:
    the question codes that drive the gap — those answered 'no' (non_compliant
    / partial) or those left unanswered (not_assessed), in display order; empty
    for `compliant` and for a control with no engine rule.

    This is the FAITHFUL source the AI layer grounds on (C3a): the C4 runtime
    turns these codes into the unsatisfied questions' Arabic text via
    `pdpl.catalog.prompts_ar_for` and hands it to the explainer — never by
    re-parsing the formatted `rationale` string. The eval's golden set is
    generated from this same structured source, so the eval measures exactly
    what production sends.
    """

    status: str
    rationale: str
    unsatisfied_codes: tuple[str, ...]


# A rule maps the tenant's answers (question_code -> 'yes'/'no') to a
# deterministic, structured verdict for one control.
Rule = Callable[[dict[str, str]], ControlDecision]


# ---------------------------------------------------------------------
# Reading the tenant's answers — latest answer per question.
#
# Answers are append-only evidence rows (ADR-0005): changing an answer
# inserts a new row, never overwrites. The engine therefore reads the row
# with the greatest collected_at per question_code (created_at breaks ties).
# idx_evidence_tenant_collected covers this access pattern.
# ---------------------------------------------------------------------
_SELECT_LATEST_ANSWERS_SQL = text(
    """
    SELECT DISTINCT ON (payload->>'question_code')
           payload->>'question_code' AS question_code,
           payload->>'answer'        AS answer
    FROM evidence
    WHERE tenant_id = :tenant_id
      AND type = 'questionnaire_answer'
    ORDER BY payload->>'question_code', collected_at DESC, created_at DESC
    """
)


async def load_tenant_answers(
    session: AsyncSession, tenant_id: UUID
) -> dict[str, str]:
    """Return {question_code: latest_answer} for one tenant.

    Reads inside the caller's transaction so the engine sees a consistent
    snapshot together with the findings it is about to write.
    """
    rows = (
        await session.execute(_SELECT_LATEST_ANSWERS_SQL, {"tenant_id": tenant_id})
    ).all()
    return {row.question_code: row.answer for row in rows}


# ---------------------------------------------------------------------
# The shared yes/no evaluation (status mapping from ADR-0006 §3).
# ---------------------------------------------------------------------
def _evaluate_yes_no(
    answers: dict[str, str], question_codes: list[str], *, label: str
) -> ControlDecision:
    """Apply the yes/no status mapping over one control's questions.

    - any required question unanswered -> not_assessed
    - all answered 'yes'               -> compliant
    - all answered 'no'                -> non_compliant
    - some yes, some no                -> partial

    `unsatisfied_codes` carries the codes behind the gap, in question (display)
    order: the unanswered ones for not_assessed, the non-'yes' ones otherwise.
    """
    missing = [qc for qc in question_codes if qc not in answers]
    if missing:
        return ControlDecision(
            "not_assessed",
            f"{label}: not assessed — unanswered question(s): {', '.join(missing)}",
            tuple(missing),
        )

    total = len(question_codes)
    satisfied = [qc for qc in question_codes if answers[qc] == "yes"]
    gaps = [qc for qc in question_codes if answers[qc] != "yes"]
    n_yes = len(satisfied)

    if n_yes == total:
        return ControlDecision(
            "compliant", f"{label}: all {total} question(s) satisfied", ()
        )
    if n_yes == 0:
        return ControlDecision(
            "non_compliant",
            f"{label}: none of {total} question(s) satisfied",
            tuple(gaps),
        )
    return ControlDecision(
        "partial",
        f"{label}: {n_yes} of {total} question(s) satisfied; gap(s): {', '.join(gaps)}",
        tuple(gaps),
    )


# ---------------------------------------------------------------------
# Per-control rules. One function per control code (ADR-0006 §1).
# The question codes here MUST match those seeded by migration 0004.
# ---------------------------------------------------------------------
def _rule_privacy_notice(answers: dict[str, str]) -> tuple[str, str]:
    return _evaluate_yes_no(
        answers,
        [
            "Q-ART12-NOTICE-EXISTS",
            "Q-ART12-NOTICE-PURPOSES",
            "Q-ART12-NOTICE-RECIPIENTS",
            "Q-ART12-NOTICE-RIGHTS",
        ],
        label="privacy notice",
    )


def _rule_dsr_access(answers: dict[str, str]) -> tuple[str, str]:
    return _evaluate_yes_no(
        answers,
        ["Q-ART4-ACCESS-PROCESS", "Q-ART4-ACCESS-TIMEFRAME"],
        label="right of access",
    )


def _rule_breach_notify(answers: dict[str, str]) -> tuple[str, str]:
    return _evaluate_yes_no(
        answers,
        ["Q-ART20-BREACH-PROCEDURE", "Q-ART20-BREACH-72H"],
        label="breach notification (72h)",
    )


def _rule_ropa(answers: dict[str, str]) -> tuple[str, str]:
    return _evaluate_yes_no(
        answers,
        ["Q-ART31-ROPA-MAINTAINED"],
        label="records of processing",
    )


# control code -> rule. Controls absent here are intentionally not covered
# by the engine yet and resolve to 'not_assessed' (ADR-0006 §3).
_RULES: dict[str, Rule] = {
    "PDPL-ART12-PRIVACY-NOTICE": _rule_privacy_notice,
    "PDPL-ART4-DSR-ACCESS": _rule_dsr_access,
    "PDPL-ART20-BREACH-NOTIFY-72H": _rule_breach_notify,
    "PDPL-ART31-ROPA": _rule_ropa,
}


def build_control_decider(
    answers: dict[str, str],
) -> Callable[[str], ControlDecision]:
    """Build the STRUCTURED decider, closing over the tenant's answers.

    Returns a `Callable[[str], ControlDecision]` — the full verdict including
    `unsatisfied_codes`. The C4 runtime uses this to ground the explainer in
    the unsatisfied questions' text (via `pdpl.catalog`), and the eval generates
    its golden set from it. A control with no registered rule resolves to
    `not_assessed` with no codes.
    """

    def _decide(control_code: str) -> ControlDecision:
        rule = _RULES.get(control_code)
        if rule is None:
            return ControlDecision(
                "not_assessed",
                "no deterministic rule registered for this control yet",
                (),
            )
        return rule(answers)

    return _decide


def build_deterministic_decider(
    answers: dict[str, str],
) -> Callable[[str], tuple[str, str]]:
    """Build the `(status, rationale)` decider for `run_check`, closing over the
    tenant's answers.

    Returns a `Callable[[str], (status, rationale)]` so it slots into the
    exact seam `run_check` already uses — the per-control loop, SCD Type 2
    transition logic, and idempotency dedup are untouched (ADR-0006 §5). It is
    a thin projection of `build_control_decider`, so the two can never disagree.
    """
    decide = build_control_decider(answers)

    def _decide(control_code: str) -> tuple[str, str]:
        decision = decide(control_code)
        return (decision.status, decision.rationale)

    return _decide
