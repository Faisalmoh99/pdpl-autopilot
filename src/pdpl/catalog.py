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
class SeededControl:
    """One seeded control — a verbatim mirror of the literal columns migration
    0003 seeds into `controls` (`id` is generated, so it is not mirrored).

    Tenant-agnostic by construction: static, non-personal control metadata. This
    is the authoritative copy of the control's Arabic text + weight for the
    RUNNING APP — the C4b explanation endpoint reads `title_ar`,
    `description_ar`, and `severity_weight` from here to build the `GapContext`
    it grounds the explainer on, so the rated golden set (`pdpl.eval`) and the
    runtime read the SAME control text (faithfulness, ADR-0011 §"control-text
    gap"). `tests/test_catalog_seed_drift.py` pins this to migration 0003
    VERBATIM and offline.
    """

    code: str  # stable natural key (PDPL-...), matches `controls.code`
    title_en: str
    title_ar: str  # the obligation named in Arabic (the layperson hook)
    description_en: str
    description_ar: str  # the fuller Arabic description the explainer may use
    category: str
    severity_weight: float  # the control's weight (ADR-0007); Numeric(4,2) in DB
    effective_from: str  # ISO date the control takes effect (DATE in DB)


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


# The authoritative seeded control set. VERBATIM mirror of migration 0003's
# seed — drift-tested offline against the migration's own frozen constant
# (`tests/test_catalog_seed_drift.py`). Order in this tuple is irrelevant to
# identity; lookup is by `code`. NON-AUTHORITATIVE starter set pending SDAIA
# review — same caveat the migration carries.
SEEDED_CONTROLS: tuple[SeededControl, ...] = (
    SeededControl(
        code="PDPL-ART4-DSR-ACCESS",
        title_en="Right of access to personal data",
        title_ar="حق الوصول إلى البيانات الشخصية",
        description_en="The data subject has the right to obtain access to their personal data held by the controller, including the categories of data, purposes of processing, and recipients.",
        description_ar="لصاحب البيانات الشخصية الحق في الوصول إلى بياناته الشخصية المحفوظة لدى جهة التحكم، بما في ذلك فئات البيانات وأغراض المعالجة والجهات المستلمة.",
        category="data_subject_rights",
        severity_weight=7.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART4-DSR-CORRECT",
        title_en="Right to correction of personal data",
        title_ar="حق تصحيح البيانات الشخصية",
        description_en="The data subject has the right to request correction of their personal data when it is inaccurate, incomplete, or outdated.",
        description_ar="لصاحب البيانات الشخصية الحق في طلب تصحيح بياناته الشخصية إذا كانت غير صحيحة أو غير مكتملة أو غير محدثة.",
        category="data_subject_rights",
        severity_weight=6.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART4-DSR-DELETE",
        title_en="Right to deletion of personal data",
        title_ar="حق حذف البيانات الشخصية",
        description_en="The data subject has the right to request deletion of their personal data when it is no longer necessary for the purposes for which it was collected, subject to legal retention obligations.",
        description_ar="لصاحب البيانات الشخصية الحق في طلب حذف بياناته الشخصية متى انتفت الحاجة إليها للغرض الذي جمعت من أجله، مع مراعاة الالتزامات النظامية للاحتفاظ.",
        category="data_subject_rights",
        severity_weight=7.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART5-LAWFUL-BASIS",
        title_en="Lawful basis for processing personal data",
        title_ar="الأساس النظامي لمعالجة البيانات الشخصية",
        description_en="Personal data may only be processed for a specific, declared, and legitimate purpose, with a lawful basis such as explicit consent, performance of a contract, or compliance with a legal obligation.",
        description_ar="لا يجوز معالجة البيانات الشخصية إلا لغرض محدد ومعلن ومشروع، استناداً إلى أساس نظامي كالموافقة الصريحة أو تنفيذ عقد أو الالتزام بنص نظامي.",
        category="lawful_basis",
        severity_weight=9.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART12-PRIVACY-NOTICE",
        title_en="Privacy notice disclosure to data subjects",
        title_ar="إفصاح إشعار الخصوصية لأصحاب البيانات",
        description_en="The controller must disclose to data subjects the purposes of processing, categories of data collected, recipients, retention periods, and rights, in clear and accessible language prior to collection.",
        description_ar="يجب على جهة التحكم إفصاح أغراض المعالجة وفئات البيانات المجموعة والجهات المستلمة ومدد الاحتفاظ وحقوق صاحب البيانات بلغة واضحة وسهلة الوصول قبل عملية الجمع.",
        category="transparency",
        severity_weight=7.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART19-SECURITY-MEASURES",
        title_en="Technical and organisational security measures",
        title_ar="التدابير الفنية والتنظيمية لحماية البيانات",
        description_en="The controller must implement technical and organisational measures appropriate to the risk to protect personal data against unauthorised access, disclosure, loss, alteration, or destruction.",
        description_ar="يجب على جهة التحكم اتخاذ التدابير الفنية والتنظيمية الملائمة لمستوى الخطر لحماية البيانات الشخصية من الوصول غير المصرح به والإفصاح والفقد والتعديل والإتلاف.",
        category="security",
        severity_weight=9.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART20-BREACH-NOTIFY-72H",
        title_en="Personal data breach notification within 72 hours",
        title_ar="إشعار تسرب البيانات الشخصية خلال 72 ساعة",
        description_en="In the event of a personal data breach likely to harm data subjects, the controller must notify the competent authority within 72 hours of becoming aware of the breach, and notify affected data subjects without undue delay.",
        description_ar="في حال وقوع تسرب للبيانات الشخصية قد يضر بأصحابها، يجب على جهة التحكم إبلاغ الجهة المختصة خلال 72 ساعة من علمها بالحادثة، وإبلاغ أصحاب البيانات المتأثرين دون تأخير غير مبرر.",
        category="breach_notification",
        severity_weight=10.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART25-RETENTION-LIMITS",
        title_en="Retention limits for personal data",
        title_ar="حدود الاحتفاظ بالبيانات الشخصية",
        description_en="Personal data must not be retained beyond the period necessary for the purpose of processing, unless a separate legal or regulatory obligation requires longer retention.",
        description_ar="لا يجوز الاحتفاظ بالبيانات الشخصية لمدة تتجاوز ما تستلزمه أغراض المعالجة، ما لم يوجد التزام نظامي مستقل يستوجب مدة احتفاظ أطول.",
        category="retention",
        severity_weight=6.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART29-CROSS-BORDER",
        title_en="Cross-border transfer of personal data",
        title_ar="نقل البيانات الشخصية خارج المملكة",
        description_en="Transfer of personal data outside the Kingdom requires an appropriate legal basis and adequate safeguards, in accordance with the conditions set by the competent authority.",
        description_ar="يستلزم نقل البيانات الشخصية خارج المملكة وجود أساس نظامي مناسب وضمانات كافية وفق الضوابط التي تحددها الجهة المختصة.",
        category="cross_border_transfer",
        severity_weight=8.0,
        effective_from="2023-09-14",
    ),
    SeededControl(
        code="PDPL-ART31-ROPA",
        title_en="Records of processing activities",
        title_ar="سجل عمليات معالجة البيانات الشخصية",
        description_en="The controller must maintain a record of personal-data processing activities including purposes, categories of data subjects and data, recipients, retention periods, and security measures, and make it available to the competent authority on request.",
        description_ar="يجب على جهة التحكم مسك سجل عمليات معالجة البيانات الشخصية شاملاً الأغراض وفئات أصحاب البيانات وفئات البيانات والجهات المستلمة ومدد الاحتفاظ والتدابير الأمنية، وإتاحته للجهة المختصة عند الطلب.",
        category="records_of_processing",
        severity_weight=5.0,
        effective_from="2023-09-14",
    ),
)


# The authoritative seeded question set. VERBATIM mirror of migration 0004's
# seed — drift-tested offline against the migration's own frozen constant. Order
# in this tuple is irrelevant to identity; the control->ordered-codes view is
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


# control code -> control, for O(1) verbatim lookup by the C4b endpoint.
_CONTROLS_BY_CODE: dict[str, SeededControl] = {c.code: c for c in SEEDED_CONTROLS}

# code -> question, for O(1) verbatim lookup in the join.
_BY_CODE: dict[str, SeededQuestion] = {q.code: q for q in SEEDED_QUESTIONS}


def control_by_code(control_code: str) -> SeededControl:
    """The seeded control for `control_code`, the authoritative source of its
    static Arabic text + weight for the running app (ADR-0011 §"control-text
    gap"). The C4b endpoint reads `title_ar` / `description_ar` /
    `severity_weight` from here to build the `GapContext`, so the runtime and
    the rated golden set ground on the SAME control text.

    Raises KeyError on an unknown control — a request for a control not in the
    seeded catalogue is a bug (the endpoint validates the code first), not a
    silent empty explanation.
    """
    return _CONTROLS_BY_CODE[control_code]


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
