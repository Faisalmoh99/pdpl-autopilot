"""Prompt-version governance guard (ADR-0013).

The content-hash cache key (`pdpl.db.ai_explanations.compute_cache_key`) keys on
six fields — `prompt_version, model, control_code, status, rationale, lang`. But
the prompt the model actually sees embeds FAR more than that: the control's
Arabic title + description, its severity, and the unsatisfied questions' text
(all rendered by `build_user_prompt`), plus the `SYSTEM_INSTRUCTION` and the
rendering logic itself. NONE of that embedded surface is a cache-key field — it
is "frozen per `prompt_version`". So a change to the template OR to the seeded
text the prompt embeds changes the model's output WITHOUT changing the key,
which would silently serve a stale cached explanation (and silently invalidate
the eval baseline). The governing invariant (ADR-0013):

    Everything that influences the model's output but is NOT a cache-key field
    must be frozen for a given prompt_version.

This test mechanically enforces the two GUARDABLE triggers of that invariant —
(a) the prompt template / rendering, and (b) re-seeding the catalog text the
prompt embeds — by pinning a hash of the ACTUAL rendered prompt surface to the
current `PROMPT_VERSION`. (The third trigger, a `modelVersion` alias re-point,
is a post-call value that is not a key field and cannot be guarded here; it is a
mandatory human review, ADR-0013 §triggers / ADR-0011 §6.)

WHY HASH THE REAL OUTPUT, NOT A HAND-LISTED SET OF FIELDS: if the guard re-listed
the fields it thinks the prompt uses, a field added to the prompt later (exactly
as C4b added `severity_weight` + `control_description_ar`) would silently escape
it — the guard would have the very blind spot it exists to catch. Computing the
hash from `build_user_prompt`'s actual output over the catalog means any
rendering or field change is captured automatically.
"""

from __future__ import annotations

import hashlib

from pdpl.ai.explainer import GapContext
from pdpl.ai.prompt import PROMPT_VERSION, SYSTEM_INSTRUCTION, build_user_prompt
from pdpl.catalog import (
    SEEDED_CONTROLS,
    prompts_ar_for,
    question_codes_for_control,
)

# The gap statuses the explainer renders (compliant is not a gap). Covers every
# branch of the prompt's `_STATUS_AR` map.
_GAP_STATUSES = ("non_compliant", "partial", "not_assessed")


def _compute_prompt_surface_hash() -> str:
    """Hash the ACTUAL prompt surface: `SYSTEM_INSTRUCTION` plus
    `build_user_prompt` rendered over every seeded control x the three gap
    statuses, with the embedded control/question text drawn from the catalogue.

    Deterministic and offline (no DB, no model). Because it hashes the rendered
    OUTPUT, it captures the template, the rendering logic (`_STATUS_AR`,
    `_severity_ar`), AND every embedded seeded field — including any field added
    to the prompt in the future — without re-listing them here. The catalogue's
    spread of severities and its mix of with-/without-question controls exercise
    both `_severity_ar` and both branches of `build_user_prompt`.
    """
    parts = [SYSTEM_INSTRUCTION]
    for control in sorted(SEEDED_CONTROLS, key=lambda c: c.code):
        questions = prompts_ar_for(question_codes_for_control(control.code))
        for status in _GAP_STATUSES:
            ctx = GapContext(
                control_code=control.code,
                control_title_ar=control.title_ar,
                control_description_ar=control.description_ar,
                status=status,
                rationale="",  # not part of the rendered surface (build_user_prompt ignores it)
                severity_weight=control.severity_weight,
                unsatisfied_questions_ar=questions,
            )
            parts.append(build_user_prompt(ctx))
    # NUL separator: no rendered Arabic prose contains it, so no two distinct
    # part-sets can collide by content running together.
    blob = "\n\x00\n".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Pinned, CAPTURED from the real `build_user_prompt` output on this
# PROMPT_VERSION (not hand-written). When the prompt surface legitimately
# changes, bump PROMPT_VERSION and update BOTH constants below in the same edit
# — and re-run the eval (see the failure message).
_PINNED_PROMPT_VERSION = "gap-ar-v1"
_PINNED_SURFACE_HASH = (
    "3ffc37f77ed493a185ff1617f47bc6f43e9f20af38082dfa6296ca8f3c5a1d9e"
)


def test_prompt_surface_hash_is_deterministic() -> None:
    """The surface hash is stable across calls — a pure function of the prompt +
    catalogue, so the pin below is meaningful."""
    assert _compute_prompt_surface_hash() == _compute_prompt_surface_hash()


def test_prompt_surface_is_pinned_to_prompt_version() -> None:
    """THE GUARD (ADR-0013 §enforcement).

    The current prompt surface must match the pinned hash AND the pinned version
    must match the current PROMPT_VERSION — the two move together. A drift in
    either fails here, before a stale cache row or an invalid eval baseline can
    ship.
    """
    current_hash = _compute_prompt_surface_hash()
    assert (PROMPT_VERSION, current_hash) == (
        _PINNED_PROMPT_VERSION,
        _PINNED_SURFACE_HASH,
    ), (
        "The prompt surface changed (template, rendering, or the seeded "
        "control/question text the prompt embeds). This silently bypasses the "
        "content-hash cache key, which does NOT see the embedded text. You MUST:\n"
        "  (a) bump PROMPT_VERSION in pdpl/ai/prompt.py (single counter: "
        "gap-ar-vN -> vN+1) and add a changelog line for the change;\n"
        "  (b) update _PINNED_PROMPT_VERSION and _PINNED_SURFACE_HASH in this "
        "test to the new values;\n"
        "  (c) RE-RUN the eval and RE-RATE: the human quality_score baseline "
        "(4.79 on gap-ar-v1) is pinned to v1 via `quality_score_run` and does "
        "NOT carry to a new prompt version.\n"
        "(If instead the catalog seed changed unintentionally, "
        "tests/test_catalog_seed_drift.py is the place that fails first.)"
    )
