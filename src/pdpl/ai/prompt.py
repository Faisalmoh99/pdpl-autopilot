"""The gap-explanation prompt — version `gap-ar-v1` (ADR-0009 §1, C3a).

Kept in its own module so prompt-version governance is one obvious place
(ADR-0009 open question). `PROMPT_VERSION` is part of the C3b content-hash
cache key: bump it whenever the system instruction, the rendering, or the
seeded question wording changes, so a stale cached explanation is never served
(ADR-0009 §6).

Design (ADR-0009 §1 / the safety line): explain WHY this is a gap to a
non-technical Saudi business owner, in clear Arabic, give ONE concrete
remediation step, bind to the control — and NEVER decide. The model must never
assert compliance; the deterministic gate (`pdpl.verification`) is the runtime
guarantee, but the prompt is the first line: a model that does not assert
compliance produces fewer fallbacks.
"""

from __future__ import annotations

from pdpl.ai.explainer import GapContext

PROMPT_VERSION = "gap-ar-v1"

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
    if ctx.unsatisfied_questions_ar:
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
