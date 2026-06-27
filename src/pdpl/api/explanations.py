"""POST /tenants/{tenant_id}/explanations — on-demand Arabic gap explanation.

The HTTP surface for the explanation feature (ADR-0012). A thin route over an
application-service (`explain_tenant_gap`) that mirrors the proven
`run_check` shape: it opens ONE `session_scope`, re-derives the STRUCTURED
`ControlDecision` from the tenant's current answers via the real engine, builds
the `GapContext` from the seeded catalogue, and runs `explain_gap` — the single
safety chokepoint (ADR-0011 §2). The returned text is ALWAYS safe to show:
verified AI prose, a re-gated cache hit, or the deterministic floor.

WHY THE COMPOSITION LIVES HERE (not in `pdpl.explanations`): contract 7
(`explanations-no-decision-core`, ADR-0011 §7) forbids the orchestration layer
from importing the decision core, so the wiring that joins the engine to
`explain_gap` must live outside it — in this endpoint layer. `pdpl.api` is NOT
walled from the core by contract 1 (only the four `pdpl.services.*` decision
modules are), so it legally imports `build_control_decider` (the engine),
`control_by_code` (the catalogue), `build_gap_context` / `explain_gap`
(`pdpl.explanations`), and `gemini_explainer_from_settings` (`pdpl.ai`). The
seven contracts stay green; no new contract is needed.

WHY POST, NOT GET (ADR-0012 §2): `GET` carries safe + cacheable HTTP semantics —
intermediaries may cache the response independently of our cache + gate. That is
wrong for a result produced by a nondeterministic model call behind a safety
gate (an HTTP-cached copy could outlive a `prompt_version` bump and never be
re-gated). `POST` is non-cacheable by default, so it prevents that structurally.

WHY RE-DERIVE, NOT READ THE FINDING (ADR-0012 §3): the explainer needs the
engine's STRUCTURED `unsatisfied_codes`, which the persisted finding does not
carry; re-deriving is the only faithful source (the alternative — parsing codes
out of the formatted `rationale` — is forbidden, ADR-0011 §1). Bounded gap: if
the owner changed answers without re-running a check, the re-derived status can
differ from the displayed report — accepted for the MVP (ADR-0012 Consequences).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from pdpl.ai.explainer import Explainer
from pdpl.ai.gemini import gemini_explainer_from_settings
from pdpl.ai.prompt import PROMPT_VERSION
from pdpl.catalog import control_by_code
from pdpl.config import get_settings
from pdpl.db.session import session_scope
from pdpl.explanations import ExplanationResult, build_gap_context, explain_gap
from pdpl.services.checks import TenantNotFound
from pdpl.services.decision import build_control_decider, load_tenant_answers

router = APIRouter()


class UnknownControl(Exception):
    """Raised when an explanation is requested for a control_code not in the
    seeded catalogue. The route translates this to HTTP 404."""


_SELECT_TENANT_ACTIVE_SQL = text(
    "SELECT id FROM tenants WHERE id = :tenant_id AND status = 'active'"
)


async def explain_tenant_gap(
    tenant_id: UUID,
    control_code: str,
    *,
    explainer: Explainer | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> ExplanationResult:
    """Produce a verified Arabic explanation for one tenant's control gap.

    Mirrors `run_check`'s ownership: opens its own `session_scope`, does the read
    + re-derive + `explain_gap` inside that one transaction, and defaults the
    explainer to the real Gemini call. Tests inject a `StubExplainer` (the route
    does not expose the override — production always uses the default), exactly
    as `run_check(decider=...)` is injected.

    `prompt_version` is a test-only seam, like `explainer`: the route never
    overrides it (production always uses `PROMPT_VERSION`). It is part of the
    content-hash cache key, which is tenant-agnostic and persistent (C3b), so a
    test that needs a deterministic cache MISS passes a unique version to get a
    fresh key — the same reason `run_check` exposes a decider override.

    Validation happens BEFORE the explainer is constructed, so the 404 paths
    (unknown control / unknown tenant) never need a configured `GEMINI_API_KEY`.
    """
    # Validate the control first (cheap, no DB, no explainer): a code not in the
    # seeded catalogue is a client error, surfaced as 404 before any work.
    try:
        control = control_by_code(control_code)
    except KeyError as exc:
        raise UnknownControl(control_code) from exc

    settings = get_settings()

    # A SHORT read transaction: validate the tenant + re-derive the structured
    # verdict, then RELEASE the connection before the (slow) explainer call. The
    # connection is no longer held across the model call (ADR-0014 §7 hold-time
    # fix); `explain_gap` owns its own short cache-read / verified-put
    # transactions internally.
    async with session_scope() as session:
        tenant_row = (
            await session.execute(_SELECT_TENANT_ACTIVE_SQL, {"tenant_id": tenant_id})
        ).first()
        if tenant_row is None:
            raise TenantNotFound(str(tenant_id))

        # Re-derive the STRUCTURED verdict from the tenant's current answers,
        # inside this transaction — the same engine path `run_check` uses and the
        # C4a identity test proves faithful to the rated golden set. Never a
        # parse of the formatted rationale (ADR-0011 §1).
        answers = await load_tenant_answers(session, tenant_id)
        decision = build_control_decider(answers)(control_code)

    # Pure assembly — no DB, so it runs after the read transaction is released.
    ctx = build_gap_context(
        control_code=control_code,
        control_title_ar=control.title_ar,
        control_description_ar=control.description_ar,
        status=decision.status,
        rationale=decision.rationale,
        severity_weight=control.severity_weight,
        unsatisfied_codes=decision.unsatisfied_codes,
    )

    # Construct the real explainer only now (after validation), so the 404 paths
    # above never require GEMINI_API_KEY. Fails fast if misconfigured.
    chosen = (
        explainer if explainer is not None else gemini_explainer_from_settings(settings)
    )

    return await explain_gap(
        ctx,
        chosen,
        model=settings.gemini_model,
        prompt_version=prompt_version,
    )


class ExplainIn(BaseModel):
    control_code: str = Field(..., min_length=1)


class ExplainOut(BaseModel):
    control_code: str
    # Always safe to show: verified AI prose, a re-gated cache hit, or the floor.
    text: str
    # "ai_verified" | "cache_hit" | "fallback" — lets the client know whether it
    # is showing a degraded (fallback) explanation.
    source: str
    # Set only when source == "fallback": why we fell back.
    reason: str | None = None


@router.post("/tenants/{tenant_id}/explanations", response_model=ExplainOut)
async def create_explanation(tenant_id: UUID, body: ExplainIn) -> ExplainOut:
    try:
        result = await explain_tenant_gap(tenant_id, body.control_code)
    except TenantNotFound:
        raise HTTPException(status_code=404, detail="tenant not found")
    except UnknownControl:
        raise HTTPException(status_code=404, detail="control not found")
    return ExplainOut(
        control_code=body.control_code,
        text=result.text,
        source=result.source,
        reason=result.reason,
    )
