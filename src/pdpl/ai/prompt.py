"""The gap-explanation prompt — version `gap-ar-v2` (ADR-0009 §1, C3a; ADR-0013).

Kept in its own module so prompt-version governance is one obvious place.

PROMPT-VERSION GOVERNANCE (ADR-0013 — the rule, enforced by mechanism). The
content-hash cache key (`pdpl.db.ai_explanations`) keys on six fields, but the
prompt below embeds MORE than the key sees: the control's `title_ar` +
`description_ar`, its `severity_weight` (via `_severity_ar`), the unsatisfied
questions' text, the `SYSTEM_INSTRUCTION`, and the rendering itself. None of that
is a key field, so it must be FROZEN for a given `PROMPT_VERSION`. The invariant:
*everything that influences the model's output but is not a cache-key field must
be frozen per `prompt_version`.* Therefore **bump `PROMPT_VERSION`** (single
counter, `gap-ar-vN -> vN+1`) whenever:

  (a) this template / rendering changes (`SYSTEM_INSTRUCTION`, `build_user_prompt`,
      `_STATUS_AR`, `_severity_ar`); OR
  (b) the catalog text the prompt embeds is re-seeded (a question's `prompt_ar`,
      or a control's `title_ar` / `description_ar` / `severity_weight`).

A bump retires the old cache namespace (old keys are never read again, recomputed
lazily — ADR-0013 §5) AND requires RE-RUNNING the eval + re-rating: the human
`quality_score` baseline (4.79) is pinned to `gap-ar-v1` via `quality_score_run`
and does not carry forward. `tests/test_prompt_version_governance.py` pins a hash
of the real rendered surface to `PROMPT_VERSION` and fails the build on an
un-bumped change. (Triggers (a)/(b) are guarded there; a `modelVersion` alias
re-point — ADR-0011 §6 — is a post-call value that cannot be guarded and is a
mandatory human review instead.) Changing the configured `model` alias is NOT a
bump — `model` is itself a key field, so it busts the cache on its own.

PER-VERSION CHANGELOG (one line per bump — what changed and why):
  - gap-ar-v1 — initial prompt (C3a): WHY-this-is-a-gap + one remediation step,
    binds to the control, never asserts compliance. The rendering has embedded
    `title_ar` + `description_ar` + the severity line + the unsatisfied questions
    since C3a; the eval's v1 rating (4.79) was produced against this surface. C4b
    did not change this template — it moved the control text to `SEEDED_CONTROLS`
    (drift-pinned, re-seedable), which is what makes trigger (b) real.
  - gap-ar-v2 — status-aware question framing. v1 rendered the questions under
    «المتطلبات غير المستوفاة (تحتاج معالجة)» REGARDLESS of status; for
    `not_assessed` that contradicts SYSTEM_INSTRUCTION rule (5) and primed the
    model to ASSERT a confirmed gap (the dsr-access / ropa not_assessed cases,
    human-rated 3 & 4 against the v1 artifact — the only two that dragged the 4.79
    mean down). v2 renders `not_assessed` with NEUTRAL, non-action-item framing
    (outside automated-assessment scope / compliance may already be satisfied /
    points for human review, not confirmed gaps / do not assume a breach), the
    SAME epistemic message whether or not unsatisfied questions exist.
    `non_compliant` / `partial` framing is UNCHANGED (their gap IS engine-confirmed).
    The gate (`pdpl.verification`), the `SYSTEM_INSTRUCTION`, and the verbatim
    control-text quoting (a separate, deferred v1 quirk that lowered no score) are
    all UNTOUCHED — one variable. Re-run the eval + re-rate: the v1 4.79 baseline
    is pinned to `gap-ar-v1` and does NOT carry forward.

Design (ADR-0009 §1 / the safety line): explain WHY this is a gap to a
non-technical Saudi business owner, in clear Arabic, give ONE concrete
remediation step, bind to the control — and NEVER decide. The model must never
assert compliance; the deterministic gate (`pdpl.verification`) is the runtime
guarantee, but the prompt is the first line: a model that does not assert
compliance produces fewer fallbacks.
"""

from __future__ import annotations

from pdpl.ai.explainer import GapContext

PROMPT_VERSION = "gap-ar-v2"

# The system instruction: the rules, kept separate from the per-gap facts so
# the model treats them as governing, not as data to summarise.
SYSTEM_INSTRUCTION = (
    "أنت مساعد يشرح فجوات الامتثال لنظام حماية البيانات الشخصية (PDPL) السعودي "
    "لصاحب عمل صغير غير متخصص.\n"
    "التزم بالقواعد التالية حرفياً:\n"
    "1) اشرح بإيجاز لماذا هذا البند يمثل فجوة، بلغة عربية واضحة وبسيطة "
    "(جملتان إلى أربع جمل).\n"
    "2) لا تصدر حكماً ولا تقرّر الامتثال إطلاقاً. ممنوع منعاً باتاً قول إن "
    "المستخدم «ملتزم» أو «متوافق» أو «مطابق للنظام» أو أن وضعه «سليم» أو أنه "
    "«لا توجد ملاحظات». أنت تشرح فقط، والقرار جهة أخرى.\n"
    "3) قدّم خطوة علاجية واحدة ملموسة وقابلة للتنفيذ.\n"
    "4) اربط الشرح بالبند المعني واذكر التزامه بالكلمات (لا تكتفِ بكلام عام).\n"
    "5) لا تختلق أرقام مواد نظامية ولا تفاصيل غير معطاة لك. إذا كان البند «لم "
    "يُقيَّم آلياً» فاذكر بصراحة أنه يحتاج مراجعة، ولا تخترع فجوة.\n"
    "6) اكتب بالعربية فقط، نصاً عادياً بدون تنسيق أو رموز."
)

# Arabic, layperson-facing labels for the deterministic status — the developer
# tokens (non_compliant/…) never go to the model as-is.
_STATUS_AR = {
    "non_compliant": "يوجد قصور (لم تُستوفَ المتطلبات)",
    "partial": "مستوفى جزئياً (بقيت متطلبات)",
    "not_assessed": "لم يُقيَّم آلياً (يحتاج مراجعة)",
}


def _severity_ar(weight: float) -> str:
    if weight >= 8.0:
        return "عالية"
    if weight >= 6.0:
        return "متوسطة"
    return "منخفضة"


def build_user_prompt(ctx: GapContext) -> str:
    """Render the per-gap facts the model reasons over — the readable control
    text and the unsatisfied questions, never any tenant data or PII."""
    status_ar = _STATUS_AR.get(ctx.status, ctx.status)
    lines = [
        f"البند: {ctx.control_title_ar}",
        f"وصف البند: {ctx.control_description_ar}",
        f"الحالة: {status_ar}",
        f"الأهمية: {_severity_ar(ctx.severity_weight)}",
    ]
    if ctx.status == "not_assessed":
        # gap-ar-v2 (ADR-0013): not_assessed = OUTSIDE the automated-assessment
        # scope, NOT a confirmed gap. v1 rendered these questions under
        # «المتطلبات غير المستوفاة (تحتاج معالجة)» regardless of status, which
        # contradicted SYSTEM_INSTRUCTION rule (5) and primed the model to ASSERT
        # a gap (the dsr-access / ropa not_assessed regressions). The neutral,
        # non-action-item framing below leaves compliance OPEN and carries the
        # SAME epistemic message whether or not unsatisfied questions exist.
        if ctx.unsatisfied_questions_ar:
            lines.append(
                "بنود خارج نطاق التقييم الآلي — حالتها غير محسومة وقد تكون مستوفاة. هذه نقاط للمراجعة البشرية، لا فجوات مؤكَّدة؛ لا تفترض وجود إخلال:"
            )
            lines.extend(f"- {q}" for q in ctx.unsatisfied_questions_ar)
        else:
            lines.append(
                "هذا البند خارج نطاق التقييم الآلي — حالته غير محسومة وقد تكون مستوفاة، ويتطلّب مراجعة بشرية للتأكّد. لا تفترض وجود فجوة؛ اربط الشرح بعنوان البند."
            )
    elif ctx.unsatisfied_questions_ar:
        lines.append("المتطلبات غير المستوفاة (تحتاج معالجة):")
        lines.extend(f"- {q}" for q in ctx.unsatisfied_questions_ar)
    else:
        lines.append(
            "لا تتوفر متطلبات تفصيلية لهذا البند بعد؛ اربط الشرح بعنوان البند، "
            "ووضّح أنه يحتاج مراجعة يدوية."
        )
    lines.append(
        "\nاكتب الشرح والخطوة العلاجية وفق القواعد، بالعربية فقط."
    )
    return "\n".join(lines)
