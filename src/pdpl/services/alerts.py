"""Alert trigger policy — deterministic, AI-free (ADR-0008 §4).

The report is the current-state surface; the alert is the *change* surface.
A first assessment establishes state — it is not a change — so it must not
alert. This module classifies a finding status transition as "worsening"
(alertable) or not, and nothing else.

It is part of the import-linter–guarded deterministic core (.importlinter):
classifying whether a compliance change is an alarm is a verdict-adjacent
decision and must stay provably free of AI. It performs no I/O.
"""

from __future__ import annotations

# Severity order over the three real verdict states only (ADR-0006).
# Higher is better. A transition is "worsening" iff both endpoints are
# ranked and the destination is strictly worse than the source.
_SEVERITY: dict[str, int] = {
    "compliant": 3,
    "partial": 2,
    "non_compliant": 1,
}


def is_worsening_transition(from_status: str, to_status: str) -> bool:
    """True iff this transition is an alertable worsening (ADR-0008 §4).

    Only transitions BETWEEN ranked verdict states, moving to a strictly
    worse one, alert. `not_assessed` / `unknown` / `not_applicable` are
    unranked, so:
      - a first assessment (`not_assessed -> *`) never alerts — it
        establishes state, it is not a worsening;
      - knowledge loss (`compliant/partial -> not_assessed`) never alerts —
        it is not a verdict worsening;
      - improving transitions never alert (deferred to a future digest).
    """
    from_rank = _SEVERITY.get(from_status)
    to_rank = _SEVERITY.get(to_status)
    if from_rank is None or to_rank is None:
        return False
    return to_rank < from_rank
