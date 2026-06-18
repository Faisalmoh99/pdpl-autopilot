"""Gap-explanation orchestration (ADR-0009 §4) — the app-layer wiring.

This is the *application orchestration* that sequences: call the (untrusted)
`Explainer` -> run the (trusted) `verify_explanation` gate -> on failure fall
back to the deterministic `rationale`. It lives OUTSIDE the decision core,
because it imports `pdpl.ai` (which the core may never touch) and
`pdpl.verification`; putting it in `pdpl.services.*` would violate the
core-must-not-import-AI contract.

The decision core neither produces nor consumes AI output — it only emits the
deterministic verdict the explainer later reads as data. The fallback path is
modelled on the worker's discipline (ADR-0008): a failure is caught and turned
into a safe outcome, never propagated to the user.

The HTTP / findings wiring that calls this is deferred (ADR-0009 open
questions); this module is a pure, testable function the C1 tests drive
directly.
"""

from __future__ import annotations

from pdpl.explanations.orchestrator import explain_gap

__all__ = ["explain_gap"]
