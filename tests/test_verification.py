"""Unit tests for the deterministic verification gate (ADR-0009 §3).

Pure and offline — no database, no network, no model. They drive
`verify_explanation` directly and assert each of the four checks passes/fails
in isolation, that paraphrase still passes the references-control check via a
salient keyword + Arabic normalization, and that the structured verdict
reports the right per-check results (ADR-0010 §3).
"""

from __future__ import annotations

from pdpl.verification import verify_explanation

# A control to verify against, mirroring the seeded privacy-notice control
# (migration 0004 / ADR-0006). The title is the layperson Arabic hook.
_CODE = "PDPL-ART12-PRIVACY-NOTICE"
_TITLE_AR = "الإشعار بالخصوصية"

# A known-good Arabic explanation: names the control, asserts nothing about
# compliance, is genuinely Arabic, and is within the length bounds.
_GOOD_AR = (
    "لا يتوفر لديك إشعار خصوصية يوضّح للعميل أغراض المعالجة وحقوقه. "
    "أضف الإشعار بالخصوصية إلى موقعك كخطوة أولى لمعالجة هذه الثغرة."
)


def _verify(text: str):
    return verify_explanation(text, control_code=_CODE, control_title_ar=_TITLE_AR)


# --------------------------------------------------------------------------
# Check 1 — no compliance assertion (the safety-critical check).
# --------------------------------------------------------------------------
def test_compliance_assertion_caught_in_arabic() -> None:
    verdict = _verify(
        "وضعك ممتاز، أنت ملتزم بالنظام تماماً ولا داعي لأي إجراء إضافي الآن."
    )
    assert verdict.no_compliance_assertion.passed is False
    assert verdict.passed is False


def test_compliance_assertion_caught_in_english() -> None:
    # English assertion embedded; the denylist matches case-insensitively
    # after normalization.
    verdict = _verify(
        "Good news for your business: You Are Compliant with the regulation."
    )
    assert verdict.no_compliance_assertion.passed is False
    assert verdict.passed is False


def test_no_compliance_assertion_passes_for_clean_text() -> None:
    assert _verify(_GOOD_AR).no_compliance_assertion.passed is True


# --------------------------------------------------------------------------
# Check 2 — references the control (title substring OR salient token OR code).
# --------------------------------------------------------------------------
def test_references_control_passes_on_full_title() -> None:
    verdict = _verify(
        "الإشعار بالخصوصية غير مكتمل لديك؛ يجب أن يوضّح الأغراض والحقوق للعميل."
    )
    assert verdict.references_control.passed is True


def test_references_control_paraphrase_still_passes_via_salient_token() -> None:
    # Paraphrase: does NOT contain the full title "الإشعار بالخصوصية", and
    # writes the obligation with the hamza variant «الإشعار». Normalization
    # folds إ->ا so the salient token «الاشعار» still matches — proving
    # paraphrase does not false-reject (ADR-0009 §3 check 2).
    text = "يوضّح الإشعار للعميل كيف تُعالَج بياناته، ويجب أن يكون واضحاً ومتاحاً له."
    verdict = verify_explanation(
        text, control_code=_CODE, control_title_ar=_TITLE_AR
    )
    assert verdict.references_control.passed is True
    assert "salient title token" in verdict.references_control.reason


def test_references_control_fails_for_generic_text() -> None:
    verdict = _verify(
        "هذا الأمر مهم جداً ويجب الاهتمام به بشكل كبير وفوري دون أي تأخير."
    )
    assert verdict.references_control.passed is False


# --------------------------------------------------------------------------
# Check 3 — Arabic ratio.
# --------------------------------------------------------------------------
def test_arabic_ratio_passes_for_arabic_text() -> None:
    assert _verify(_GOOD_AR).arabic.passed is True


def test_arabic_ratio_fails_for_mostly_english_text() -> None:
    verdict = _verify(
        "This control is not satisfied; you must add a privacy notice document."
    )
    assert verdict.arabic.passed is False


def test_arabic_ratio_tolerates_an_embedded_english_term() -> None:
    # An embedded Latin acronym must not by itself fail an otherwise-Arabic
    # explanation (threshold 0.75).
    verdict = _verify(
        "نظام PDPL يتطلب وجود الإشعار بالخصوصية لكي يفهم العميل أغراض المعالجة."
    )
    assert verdict.arabic.passed is True


# --------------------------------------------------------------------------
# Check 4 — length bounds (20..600 on trimmed text).
# --------------------------------------------------------------------------
def test_length_fails_when_too_short() -> None:
    assert _verify("قصير").within_length_bounds.passed is False


def test_length_fails_when_too_long() -> None:
    assert _verify("ا" * 601).within_length_bounds.passed is False


def test_length_passes_within_bounds() -> None:
    assert _verify(_GOOD_AR).within_length_bounds.passed is True


# --------------------------------------------------------------------------
# The structured verdict (ADR-0010 §3): per-check booleans + overall.
# --------------------------------------------------------------------------
def test_good_text_passes_every_check() -> None:
    verdict = _verify(_GOOD_AR)
    assert verdict.passed is True
    assert all(c.passed for c in verdict.checks.values())


def test_verdict_exposes_the_four_named_checks() -> None:
    verdict = _verify(_GOOD_AR)
    assert set(verdict.checks) == {
        "no_compliance_assertion",
        "references_control",
        "arabic",
        "within_length_bounds",
    }


def test_verdict_reports_each_failed_check_independently() -> None:
    # An English compliance assertion that also references nothing: it should
    # fail check 1 (assertion), check 2 (no reference) and check 3 (English),
    # while still being within length bounds — and `passed` is the conjunction.
    verdict = _verify("You are fully compliant. No gaps. Nothing to do.")
    assert verdict.no_compliance_assertion.passed is False
    assert verdict.references_control.passed is False
    assert verdict.arabic.passed is False
    assert verdict.within_length_bounds.passed is True
    assert verdict.passed is False
