"""Gap-explanation orchestration (ADR-0009 §4 / ADR-0011) — the app-layer wiring.

The runtime path that sequences cache + (untrusted) `Explainer` + the (trusted)
`verify_explanation` gate + the deterministic fallback floor. It lives OUTSIDE
the decision core: it imports `pdpl.ai` (which the core may never touch),
`pdpl.verification`, `pdpl.catalog`, and `pdpl.db`; putting it in
`pdpl.services.*` would violate the core-must-not-import-AI contract, and the
`explanations-no-decision-core` contract (ADR-0011 §7) keeps it from importing
the engine in turn — it reads a `ControlDecision` as DATA, never recomputes one.

  - `build_gap_context` (ADR-0011 §1): the pure assembler + the live catalog
    join — the runtime grounding the model receives, proven identical to what
    the eval rated.
  - `explain_gap` (ADR-0011 §2): cache get -> re-gate on hit / miss -> explain
    -> gate -> put -> return, with the gate as the single chokepoint every
    user-facing string passes through. Returns an `ExplanationResult`.

The HTTP / session / trigger wiring that calls this is deferred to C4b (ADR-0009
open questions, left open by ADR-0011).
"""

from __future__ import annotations

from pdpl.explanations.context import build_gap_context
from pdpl.explanations.orchestrator import ExplanationResult, explain_gap

__all__ = ["ExplanationResult", "build_gap_context", "explain_gap"]
