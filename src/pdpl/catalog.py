"""`pdpl.catalog` — the authoritative, tenant-agnostic seed catalogue (C3a).

A PURE LEAF module: it holds the seeded questionnaire's static, non-personal
metadata and a couple of deterministic lookups over it. It imports nothing
from the rest of `pdpl` (enforced by `.importlinter` — see the
`catalog-is-a-leaf` contract), so both the eval tooling (`pdpl.eval`) and the
future C4 app layer can depend on it without dragging in the decision core,
the AI layer, or the verifier.

WHY THIS EXISTS (ADR-0009 §2, C3a): the gap explanation the model receives
must be grounded in the *readable* Arabic text of the unsatisfied questions,
not the cryptic question CODES the deterministic `rationale` carries (e.g.
``gap(s): Q-ART12-NOTICE-RECIPIENTS``). `prompts_ar_for` is the pure join
``codes -> tuple[prompt_ar]`` that the C4 runtime feeds the model and that the
golden set is generated from — so the eval measures exactly what production
will send (identity, not approximation).

SINGLE SOURCE OF TRUTH — and its discipline. This module is the authoritative
copy of the seeded question text for the *running application*. Migration 0004
stays a frozen, self-contained historical snapshot (it does NOT import this
module, so it remains replayable forever); `tests/test_catalog_seed_drift.py`
pins the two together VERBATIM and offline. To revise question wording later:
add a NEW migration, update this catalogue, and bump the explainer's
`prompt_version` (so the C3b content-hash cache key is busted — ADR-0009 §6).
The drift test forbids silent divergence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class SeededQuestion:
    """One seeded questionnaire question — a verbatim mirror of the literal
    columns migration 0004 seeds into `questions` (`id`/timestamps are
    generated, `answer_type` is a server default, so they are not mirrored).

    Tenant-agnostic by construction: this is static control metadata, never a
    tenant's answer or any PII.
    """

    code: str  # stable natural key (Q-...), matches the engine's rule lists
    control_code: str  # the control this question belongs to (resolved by code)
    prompt_en: str
    prompt_ar: str  # the readable Arabic text the explainer grounds on
    display_order: int  # 1-based order within the control


# The authoritative seeded set. VERBATIM mirror of migration 0004's seed —
# drift-tested offline against the migration's own frozen constant. Order in
# this tuple is irrelevant to identity; the control->ordered-codes view is
# derived from `display_order`.
SEEDED_QUESTIONS: tuple[SeededQuestion, ...] = (
    # PDPL-ART12-PRIVACY-NOTICE — 4 questions (can drive a 'partial').
    SeededQuestion(
        code="Q-ART12-NOTICE-EXISTS",
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        prompt_en="Do you publish a privacy notice to data subjects before collecting their personal data?",
        prompt_ar="هل تنشر إشعار خصوصية لأصحاب البيانات قبل جمع بياناتهم الشخصية؟",
        display_order=1,
    ),
    SeededQuestion(
        code="Q-ART12-NOTICE-PURPOSES",
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        prompt_en="Does the privacy notice state the purposes for which personal data is processed?",
        prompt_ar="هل يوضح إشعار الخصوصية أغراض معالجة البيانات الشخصية؟",
        display_order=2,
    ),
    SeededQuestion(
        code="Q-ART12-NOTICE-RECIPIENTS",
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        prompt_en="Does the privacy notice identify the recipients or categories of recipients of the data?",
        prompt_ar="هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟",
        display_order=3,
    ),
    SeededQuestion(
        code="Q-ART12-NOTICE-RIGHTS",
        control_code="PDPL-ART12-PRIVACY-NOTICE",
        prompt_en="Does the privacy notice explain the data subject's rights and how to exercise them?",
        prompt_ar="هل يبيّن إشعار الخصوصية حقوق صاحب البيانات وكيفية ممارستها؟",
        display_order=4,
    ),
    # PDPL-ART4-DSR-ACCESS — 2 questions.
    SeededQuestion(
        code="Q-ART4-ACCESS-PROCESS",
        control_code="PDPL-ART4-DSR-ACCESS",
        prompt_en="Do you have a documented process for handling data-subject access requests?",
        prompt_ar="هل لديك إجراء موثّق للتعامل مع طلبات وصول أصحاب البيانات إلى بياناتهم؟",
        display_order=1,
    ),
    SeededQuestion(
        code="Q-ART4-ACCESS-TIMEFRAME",
        control_code="PDPL-ART4-DSR-ACCESS",
        prompt_en="Do you respond to access requests within a defined timeframe?",
        prompt_ar="هل تستجيب لطلبات الوصول خلال مدة زمنية محددة؟",
        display_order=2,
    ),
    # PDPL-ART20-BREACH-NOTIFY-72H — 2 questions.
    SeededQuestion(
        code="Q-ART20-BREACH-PROCEDURE",
        control_code="PDPL-ART20-BREACH-NOTIFY-72H",
        prompt_en="Do you have a documented personal-data breach response procedure?",
        prompt_ar="هل لديك إجراء موثّق للاستجابة لتسرب البيانات الشخصية؟",
        display_order=1,
    ),
    SeededQuestion(
        code="Q-ART20-BREACH-72H",
        control_code="PDPL-ART20-BREACH-NOTIFY-72H",
        prompt_en="Does the procedure commit to notifying the competent authority within 72 hours of becoming aware of a breach?",
        prompt_ar="هل يلتزم الإجراء بإبلاغ الجهة المختصة خلال 72 ساعة من العلم بالتسرب؟",
        display_order=2,
    ),
    # PDPL-ART31-ROPA — 1 question.
    SeededQuestion(
        code="Q-ART31-ROPA-MAINTAINED",
        control_code="PDPL-ART31-ROPA",
        prompt_en="Do you maintain a record of personal-data processing activities (RoPA)?",
        prompt_ar="هل تحتفظ بسجل لعمليات معالجة البيانات الشخصية؟",
        display_order=1,
    ),
)


# code -> question, for O(1) verbatim lookup in the join.
_BY_CODE: dict[str, SeededQuestion] = {q.code: q for q in SEEDED_QUESTIONS}


def question_codes_for_control(control_code: str) -> tuple[str, ...]:
    """The control's question codes in deterministic display order.

    A control with no seeded questions (the controls with no engine rule yet —
    security / lawful-basis / cross-border) yields an empty tuple, which is
    exactly what makes their `unsatisfied_questions_ar` collapse to ().
    """
    qs = sorted(
        (q for q in SEEDED_QUESTIONS if q.control_code == control_code),
        key=lambda q: q.display_order,
    )
    return tuple(q.code for q in qs)


def prompts_ar_for(codes: Iterable[str]) -> tuple[str, ...]:
    """The pure join the explainer is grounded on: gap question codes ->
    their verbatim Arabic prompts, in DETERMINISTIC order.

    Order follows (control_code, display_order) — never the caller's argument
    order — so the same gap always produces the same tuple (cache- and
    eval-stable). Duplicates are collapsed. Raises KeyError on an unknown code:
    a gap code with no catalogue entry is a bug, not a silent empty string.
    """
    unique = {c: None for c in codes}  # dedup, preserve first-seen for errors
    ordered = sorted(
        unique,
        key=lambda c: (_BY_CODE[c].control_code, _BY_CODE[c].display_order),
    )
    return tuple(_BY_CODE[c].prompt_ar for c in ordered)
