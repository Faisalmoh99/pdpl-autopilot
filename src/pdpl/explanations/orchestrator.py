"""`explain_gap` — the runtime orchestration: cache + gate + fallback
(ADR-0011 §2).

The sequence is the load-bearing safety seam of the feature:

    key  = compute_cache_key(...)
    hit  = get(cache, key)
    if hit:  RE-GATE(hit) -> pass: serve cache_hit ; fail: anomaly + fallback
    miss:    explain -> GATE -> pass: put + serve ai_verified
                             -> fail/error: fallback (never put)

Two invariants a reader must see by reading it:

  1. GATE BEFORE PUT, ALWAYS; PUT VERIFIED TEXT ONLY. The orchestrator is the
     writer, and verified-only is ITS contract — the cache enforces no safety
     (ADR-0009 §6).
  2. THE GATE IS THE SINGLE CHOKEPOINT every user-facing string passes through —
     fresh OR cached. Re-gating the cache read (refining ADR-0009 §6's read
     semantic) makes the safety property INDEPENDENT of trusting every write
     path: even a row injected by some other route is re-checked on read. The
     gate is deterministic and costs microseconds, so this is free relative to
     the call it guards.

A re-gate failure is an anomaly (`cache_regate_failed`): logged at error +
counted as the standing signal, and replaced by the fallback for that read.
Because the row is immutable at the DB role (INSERT+SELECT grant only, ADR-0003
/ C3b), a poisoned row is served as fallback on EVERY read until a
`prompt_version` bump produces a new key — the counter is how we'd know.

`model_version` provenance (ADR-0011 §6) is wired in C4b's modelVersion-capture
commit; the field exists on `ExplanationResult` now and stays `None` until the
`Explainer` port surfaces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from pdpl.ai.explainer import Explainer, ExplainerError, GapContext
from pdpl.ai.prompt import PROMPT_VERSION
from pdpl.db.ai_explanations import compute_cache_key, get, put
from pdpl.explanations.fallback import deterministic_fallback
from pdpl.observability.logging import get_logger
from pdpl.observability.metrics import counter
from pdpl.verification import verify_explanation

_log = get_logger("pdpl.explanations")

Source = Literal["cache_hit", "ai_verified", "fallback"]
FallbackReason = Literal["gate_rejected", "explainer_error", "cache_regate_failed"]


@dataclass(frozen=True)
class ExplanationResult:
    """The structured outcome of `explain_gap` (ADR-0011 §3).

    `text` is always safe to show the user — verified AI prose, a verified cache
    hit, or the deterministic floor. `reason` is set ONLY for `source ==
    "fallback"`. `model_version` is the model that PRODUCED a fresh `ai_verified`
    output (provenance, ADR-0011 §6); it is NOT in the cache key (unknown before
    the call) and is `None` for `cache_hit` / `fallback`.
    """

    text: str
    source: Source
    reason: FallbackReason | None = None
    model_version: str | None = None


def _passes_gate(text: str, ctx: GapContext) -> tuple[bool, list[str]]:
    """Run the gate; return (passed, failed_check_names) for logging."""
    verdict = verify_explanation(
        text,
        control_code=ctx.control_code,
        control_title_ar=ctx.control_title_ar,
    )
    failed = [name for name, c in verdict.checks.items() if not c.passed]
    return verdict.passed, failed


def _fallback(
    ctx: GapContext, *, reason: FallbackReason, failed_checks: list[str]
) -> ExplanationResult:
    """Produce the deterministic floor and record why we fell back. The single
    place a degraded-but-safe outcome is created, so every fallback is logged
    and counted consistently however it was triggered."""
    counter("explanations.served", source="fallback")
    counter("explanations.fallback", reason=reason)
    _log.info(
        "explanations.fallback",
        control_code=ctx.control_code,
        reason=reason,
        failed_checks=failed_checks,
    )
    return ExplanationResult(
        text=deterministic_fallback(ctx), source="fallback", reason=reason
    )


async def explain_gap(
    session: AsyncSession,
    ctx: GapContext,
    explainer: Explainer,
    *,
    model: str,
    prompt_version: str = PROMPT_VERSION,
) -> ExplanationResult:
    """Produce a verified Arabic gap explanation — from cache, fresh from the
    model, or the deterministic floor — safe on EVERY result (ADR-0011 §2).

    `session` is the caller's transaction (the C4b endpoint owns its scope);
    `model` is the requested model id, part of the cache key.
    """
    key = compute_cache_key(
        prompt_version=prompt_version,
        model=model,
        control_code=ctx.control_code,
        status=ctx.status,
        rationale=ctx.rationale,
        lang=ctx.lang,
    )

    hit = await get(session, key)
    if hit is not None:
        passed, failed = _passes_gate(hit, ctx)
        if passed:
            counter("explanations.cache", result="hit")
            counter("explanations.served", source="cache_hit")
            _log.info("explanations.cache_hit", control_code=ctx.control_code)
            return ExplanationResult(text=hit, source="cache_hit")
        # A cached row that fails the re-gate is an anomaly: never served.
        counter("explanations.cache_regate_failed")
        _log.error(
            "explanations.cache_regate_failed",
            control_code=ctx.control_code,
            cache_key=key,
            failed_checks=failed,
        )
        return _fallback(ctx, reason="cache_regate_failed", failed_checks=failed)

    counter("explanations.cache", result="miss")

    try:
        out = await explainer.explain(ctx)
    except ExplainerError as exc:
        _log.warning(
            "explanations.explainer_error",
            control_code=ctx.control_code,
            error=type(exc).__name__,
        )
        return _fallback(ctx, reason="explainer_error", failed_checks=[])

    candidate = out.text
    passed, failed = _passes_gate(candidate, ctx)
    if not passed:
        return _fallback(ctx, reason="gate_rejected", failed_checks=failed)

    await put(
        session,
        key,
        text=candidate,
        lang=ctx.lang,
        prompt_version=prompt_version,
        model=model,
    )
    counter("explanations.served", source="ai_verified")
    _log.info(
        "explanations.verified",
        control_code=ctx.control_code,
        model=model,
        model_version=out.model_version,
    )
    # Provenance: the concrete model that produced THIS verified text (ADR-0011
    # §6). None for a stub; populated for the real Gemini call.
    return ExplanationResult(
        text=candidate, source="ai_verified", model_version=out.model_version
    )
