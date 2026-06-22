"""The golden-set loader and its in-memory case shape (ADR-0010 §4).

The golden set is a version-controlled YAML data file (`data/golden_set.yaml`),
not code fixtures: the human-edited parts — Arabic expectations and quality
criteria — are far more legible as data, and storing it as a durable artifact
is the point (ADR-0010 §4, open question §). The eval runs fully OFFLINE: each
case is an extracted `GapContext` (no live DB read, no network), so the harness
reproduces the same numbers anywhere.

Each case carries TWO distinct parts, kept apart on purpose (ADR-0010 §2/§4):
  - `input`  — a real `GapContext` (the control facts + the deterministic
    status/rationale). Its `rationale` is provably faithful engine output: the
    `source_answers` regenerate it via the real decision engine, asserted by
    `tests/test_eval_golden_set.py` (drift protection). Its
    `unsatisfied_questions_ar` is faithful the same way — generated from the
    engine's structured `unsatisfied_codes` joined through
    `pdpl.catalog.prompts_ar_for`, the exact path the C4 runtime feeds the
    model, and rebuilt + literal-equality-checked by the same test.
  - `expect` / `quality_*` — the human layer. `must_contain`/`must_not_contain`
    are Layer-A per-case assertions and `quality_score` is the Layer-B human
    rating; BOTH are scored in C3 against the real model, not against the stub
    (ADR-0010 §2). They are version-controlled empty now, for the engineer to
    fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pdpl.ai.explainer import GapContext

# The golden set ships beside this module so the eval finds it with no config.
_DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "golden_set.yaml"


@dataclass(frozen=True)
class GoldenCase:
    """One golden-set case: the input `GapContext` plus the human expectations.

    `gap` is all the harness needs in C2 (it computes Layer-A metrics from it).
    The expectation fields are loaded and structurally validated now but scored
    in C3 (ADR-0010 §2). `source_answers` is provenance: the questionnaire
    answers that produce this case's status/rationale through the real
    deterministic engine, used only by the faithfulness test.
    """

    id: str
    gap: GapContext
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    source_answers: dict[str, str] = field(default_factory=dict)
    quality_criteria: str = ""
    quality_score: float | None = None


def load_golden_set(path: Path = _DEFAULT_PATH) -> list[GoldenCase]:
    """Parse the golden-set YAML into `GoldenCase`s. Pure: reads one file, no DB."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases: list[GoldenCase] = []
    for entry in raw["cases"]:
        i = entry["input"]
        gap = GapContext(
            control_code=i["control_code"],
            control_title_ar=i["control_title_ar"],
            control_description_ar=i["control_description_ar"],
            status=i["status"],
            rationale=i["rationale"],
            severity_weight=float(i["severity_weight"]),
            unsatisfied_questions_ar=tuple(i.get("unsatisfied_questions_ar") or ()),
        )
        expect = entry.get("expect") or {}
        cases.append(
            GoldenCase(
                id=entry["id"],
                gap=gap,
                must_contain=list(expect.get("must_contain") or []),
                must_not_contain=list(expect.get("must_not_contain") or []),
                source_answers=dict(entry.get("source_answers") or {}),
                quality_criteria=entry.get("quality_criteria") or "",
                quality_score=entry.get("quality_score"),
            )
        )
    return cases
