"""`explain_gap` — wire the explainer to the safety gate (ADR-0009 §4).

The sequence is deliberately tiny and pure so it is trivially testable and
its safety property is obvious by reading it: produce a candidate, verify it,
and return it ONLY if the gate passes — otherwise return the deterministic
`rationale`. Nothing the model produces reaches the caller unverified.

The fallback is shaped as a single funnel (`_fallback`) so the C3 addition —
an `Explainer` that raises (the real Gemini call, with the reliability
taxonomy) — drops in additively: an `except` around `explainer.explain` will
route to the same `_fallback`, without reworking the gate-rejection path. C1
ships only the gate-rejection branch; `StubExplainer` does not raise.
"""

from __future__ import annotations

from pdpl.ai.explainer import Explainer, GapContext
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter
from pdpl.verification import VerificationVerdict, verify_explanation

_log = get_logger("pdpl.explanations")


def _fallback(ctx: GapContext, *, reason: str, failed_checks: list[str]) -> str:
    """Return the deterministic `rationale` and record why we fell back.

    The single place a degraded-but-safe outcome is produced. A gate rejection
    (C1) and, later, an explainer failure (C3) both funnel through here, so the
    fallback is logged and counted consistently however it was triggered.
    """
    counter("explanations.fallback", reason=reason)
    _log.info(
        "explanations.fallback",
        control_code=ctx.control_code,
        reason=reason,
        failed_checks=failed_checks,
    )
    return ctx.rationale


async def explain_gap(ctx: GapContext, explainer: Explainer) -> str:
    """Produce a verified Arabic gap explanation, or the deterministic
    fallback.

    Returns the explainer's text only if it passes `verify_explanation`;
    otherwise returns `ctx.rationale`. The caller is therefore safe on every
    result regardless of model quality (ADR-0009 §3-4).
    """
    candidate = await explainer.explain(ctx)

    verdict: VerificationVerdict = verify_explanation(
        candidate,
        control_code=ctx.control_code,
        control_title_ar=ctx.control_title_ar,
    )
    if not verdict.passed:
        failed = [name for name, c in verdict.checks.items() if not c.passed]
        return _fallback(ctx, reason="gate_rejected", failed_checks=failed)

    counter("explanations.verified")
    _log.info("explanations.verified", control_code=ctx.control_code)
    return candidate
