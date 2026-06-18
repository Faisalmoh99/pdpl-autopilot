"""The deterministic verification gate (ADR-0009 §3) — the TRUSTED module.

`verify_explanation` is the safety guarantee for the gap-explanation feature:
a pure, deterministic function that stands between any AI output and the user.
It imports NOTHING from `pdpl.ai` (the untrusted producer) or the decision
core (`pdpl.services.*`) — that independence is what makes it trustworthy, and
the `.importlinter` contracts enforce it mechanically (see
`tests/test_architecture.py`).

It is the ONE shared verifier: the runtime orchestration (`pdpl.explanations`)
and the eval harness (ADR-0010) both call this exact function, so the eval's
Layer-A numbers describe the gate that actually protects users — not a
divergent copy that could drift and lie.
"""

from __future__ import annotations

from pdpl.verification.verifier import (
    CheckResult,
    VerificationVerdict,
    verify_explanation,
)

__all__ = ["CheckResult", "VerificationVerdict", "verify_explanation"]
