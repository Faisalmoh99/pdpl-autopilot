"""`deterministic_fallback` — the safe floor when the AI is unavailable, errors,
truncates, or the gate rejects its output (ADR-0011 §4).

It is NOT runtime-gated: if the floor itself could fail the gate, there would
be no floor. Its safety is therefore guaranteed BY CONSTRUCTION, and the test
(`tests/test_explanation_fallback.py`) proves the construction holds — it does
NOT claim "the fallback always passes the gate for every possible input."

WHAT IS GUARANTEED BY CONSTRUCTION (the safety properties, always):
  - no compliance assertion — every template constant below is checked against
    the denylist VERBATIM at authoring time (a static string has no excuse to
    trip the gate; the test asserts it);
  - Arabic — the templates are Arabic prose;
  - references the control — every rendering opens with `control_title_ar`;
  - tenant-agnostic — only the control title + the static question text; never a
    tenant answer or any PII.

WHAT IS NOT GUARANTEED: the LENGTH bound. A control with many unsatisfied
questions yields a long enumeration that can exceed the 800-char bound. That is
acceptable — length is a CONTENT check, not a safety one (the C3a lesson:
`finishReason` detects truncation, the length bound does not). A long-but-safe
fallback is served directly. The enumeration is NEVER truncated to force the
bound — truncation is exactly the C3a failure we refuse to reintroduce.

CRITICAL — `not_assessed` asserts NO gap (the C3a #6 fix). The model wrongly
asserted a deficiency on a `not_assessed` case once; a hand-written template
falls into the same trap just as easily. Only `non_compliant`/`partial` name
the shortfall, because the DETERMINISTIC engine decided it — and they name it
FACTUALLY (what the engine decided), never with an evaluative judgment word.
"""

from __future__ import annotations

from pdpl.ai.explainer import GapContext

# Factual intros for the genuinely-decided gaps — describe WHAT THE ENGINE
# DECIDED, never a judgment word (no «قصور»). Both match the same factual tone.
_GAP_INTRO = {
    "non_compliant": "هذا البند غير مكتمل ولم تُستوفَ متطلباته.",
    "partial": "هذا البند مستوفى جزئياً، وبقيت متطلبات لم تُستوفَ.",
}

# The requirements list header — rendered ONLY when there are unsatisfied
# questions, so the rare no-question case never shows a dangling empty list.
_REQUIREMENTS_HEADER = "المتطلبات التالية لم تُستوفَ:"

# `not_assessed` (and any unknown status): assert NO gap. State plainly that the
# control was not checked, and that this does NOT mean a deficiency exists.
_NOT_ASSESSED = (
    "لم يُقيَّم هذا البند آلياً بعد، ويحتاج مراجعة يدوية. "
    "هذا لا يعني وجود قصور — يعني فقط أنه لم يُفحص."
)

_MANUAL_REVIEW = "يُنصح بمراجعة هذا البند يدوياً للتأكد."


def deterministic_fallback(ctx: GapContext) -> str:
    """Render the safe deterministic floor for one gap (ADR-0011 §4).

    `non_compliant`/`partial`: the factual intro, then the unsatisfied questions'
    Arabic text (dropped entirely when there are none), then a manual-review
    line. Any other status (incl. `not_assessed`): assert no gap and ask for a
    manual review — never fabricate a deficiency.
    """
    title = ctx.control_title_ar
    intro = _GAP_INTRO.get(ctx.status)
    if intro is None:
        # not_assessed, or any unknown/unexpected status -> the no-gap floor.
        return f"{title}: {_NOT_ASSESSED}"

    lines = [f"{title}: {intro}"]
    if ctx.unsatisfied_questions_ar:
        lines.append(_REQUIREMENTS_HEADER)
        lines.extend(f"- {q}" for q in ctx.unsatisfied_questions_ar)
    lines.append(_MANUAL_REVIEW)
    return "\n".join(lines)
