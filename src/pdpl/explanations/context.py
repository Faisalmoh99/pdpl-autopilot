"""`build_gap_context` — the pure runtime assembler + the live catalog join
(ADR-0011 §1).

This is the moment the C3a `unsatisfied_questions_ar` join stops being an
eval-time reconstruction and becomes the live grounding the model receives:
the engine's STRUCTURED gap codes are joined to their readable Arabic prompts
via `pdpl.catalog.prompts_ar_for`, and the result is exactly what the golden
set was generated from — so reuse is an IDENTITY, not an approximation
(`tests/test_explanation_context.py` proves it verbatim across the golden set).

Pure and tenant-agnostic: no DB, no network, no PII. It assembles a
`GapContext` from the gap's static control text plus the deterministic
verdict's FIELDS.

It takes the verdict's fields (`status`, `rationale`, `unsatisfied_codes`) as
plain arguments, NOT a `pdpl.services.decision.ControlDecision` object: the
`explanations-no-decision-core` contract (ADR-0011 §7) forbids this layer from
importing the decision core, and "the verdict crosses as data" is precisely
that boundary. The caller (the C4b endpoint, which legally imports both the
engine and this layer) destructures the `ControlDecision` it computed and
passes `decision.unsatisfied_codes` here — the STRUCTURED source, never a parse
of the formatted `rationale` string.

The SOURCE of the control's static text (`control_title_ar`,
`control_description_ar`, `severity_weight`) is a wiring concern deferred to
C4b (DB `controls` read vs. a future `pdpl.catalog.SEEDED_CONTROLS` leaf); here
it arrives as arguments.
"""

from __future__ import annotations

from collections.abc import Iterable

from pdpl.ai.explainer import GapContext
from pdpl.catalog import prompts_ar_for


def build_gap_context(
    *,
    control_code: str,
    control_title_ar: str,
    control_description_ar: str,
    status: str,
    rationale: str,
    severity_weight: float,
    unsatisfied_codes: Iterable[str] = (),
    lang: str = "ar",
) -> GapContext:
    """Assemble the tenant-agnostic `GapContext` the explainer is grounded on.

    `unsatisfied_codes` are the engine's structured gap codes
    (`ControlDecision.unsatisfied_codes`); they are joined through the catalogue
    into their verbatim Arabic prompts in deterministic order. A control with no
    gap codes (the `()` default, e.g. a no-rule control) yields an empty
    `unsatisfied_questions_ar`, and the model then binds to the control TITLE
    alone (ADR-0009 §2 / the prompt's no-requirements branch).
    """
    return GapContext(
        control_code=control_code,
        control_title_ar=control_title_ar,
        control_description_ar=control_description_ar,
        status=status,
        rationale=rationale,
        severity_weight=severity_weight,
        unsatisfied_questions_ar=prompts_ar_for(unsatisfied_codes),
        lang=lang,
    )
