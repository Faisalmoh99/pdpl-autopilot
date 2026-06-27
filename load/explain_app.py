"""Load-only ASGI app: the REAL explanation orchestration + an injected-latency
stub standing in for Gemini (ADR-0014 §1 target 3 / §7).

This lives in load/, NOT in src/ — it never enters the pdpl package and is never
on main's serving path. It is run via `uvicorn load.explain_app:app` for the
explanation-path pool sweep ONLY (`load/pool_sweep.py explain`).

It exposes ONE load route that drives the REAL `explain_tenant_gap`
orchestration — tenant read -> re-derive `ControlDecision` -> build `GapContext`
-> `explain_gap` (get -> [MISS] -> call -> GATE -> put) — with two load-only
substitutions, BOTH isolated here so main's behaviour is untouched (the same
discipline as the pool-size knob, ADR-0014 §5):

  1. **The explainer is a `LatencyStubExplainer`**: `await asyncio.sleep(HOLD)`
     then a known-good Arabic explanation that PASSES the gate. This is the exact
     I/O shape of a slow Gemini call — the connection-relevant time is spent
     awaiting, the event loop stays free — and NEVER the real API (cost + rate
     limit, ADR-0014 §3). It is the real-path analogue of `probe_app.py`'s pure
     `asyncio.sleep`, but holding the connection across the REAL orchestration
     (cache + gate + put), not a bare query.

  2. **`prompt_version` is a fresh `uuid4().hex` PER REQUEST**, forcing a cache
     MISS every time (the documented test seam on `explain_tenant_gap`). Without
     it, once the ~20 seeded gaps are cached every request is a HIT that holds NO
     connection across the call — the hold-time shape under study vanishes and
     BEFORE/AFTER both flatten. Forcing a miss isolates the MISS path (the only
     shape §7 changes), exactly as the probe holds on every request. This fresh
     key lives ONLY here; `api/explanations.py` keeps the governed PROMPT_VERSION
     (ADR-0013) unchanged.

THE A/B FREEZE: this file + the stub text + the k6 script + the sweep mode are
FROZEN after the BEFORE run. The §7 refactor touches ONLY src/ (orchestrator +
endpoint + test call-sites), so the only variable between BEFORE and AFTER is the
session_scope connection lifecycle — the before/after number is unconfounded.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from uuid import UUID

from pdpl.ai.explainer import ExplainerOutput, GapContext
from pdpl.api.explanations import explain_tenant_gap
from pdpl.main import create_app

# Seconds the stub awaits per request (pure async wait — holds whatever the
# orchestration holds across it, frees the loop). 50ms matches the probe.
_HOLD_SECONDS = float(os.environ.get("LOAD_EXPLAIN_HOLD_SECONDS", "0.05"))

# The seeded non-compliant gap (seed_load.py answers all four ART12 questions
# 'no' -> non_compliant). Every load tenant has this gap.
_CONTROL = os.environ.get("LOAD_EXPLAIN_CONTROL", "PDPL-ART12-PRIVACY-NOTICE")

# A known-good Arabic explanation that PASSES the gate for the ART12
# privacy-notice control: it references «إشعار الخصوصية» / «البيانات» (salient
# title tokens of «إفصاح إشعار الخصوصية لأصحاب البيانات»), is Arabic above the
# 0.75 ratio, sits within the 20..800 length bounds, and asserts no compliance.
# PROVEN against `verify_explanation` under the seeded tenant's real GapContext by
# `load/check_gate.py` BEFORE the sweep — so every request exercises the FULL miss
# path (call -> gate PASS -> put), not call-only.
_GOOD_AR = (
    "لا يتوفر لديك إشعار الخصوصية الذي يوضّح لأصحاب البيانات أغراض معالجة "
    "بياناتهم وحقوقهم النظامية. أضف إشعار الخصوصية إلى موقعك كخطوة أولى "
    "لمعالجة هذه الثغرة."
)


class LatencyStubExplainer:
    """An Explainer (structurally — satisfies the port) that awaits `HOLD`
    seconds then returns a known-good, gate-passing Arabic explanation.

    The await is the I/O shape of a slow external (Gemini) call: the connection
    the orchestration holds across it stays checked out, while the event loop is
    free. No network, no real API. Returns no provenance (a stub)."""

    async def explain(self, ctx: GapContext) -> ExplainerOutput:
        await asyncio.sleep(_HOLD_SECONDS)
        return ExplainerOutput(text=_GOOD_AR)


app = create_app()


@app.post("/_loadexplain/{tenant_id}")
async def loadexplain(tenant_id: UUID) -> dict:
    """Drive the REAL explanation orchestration for one seeded tenant's gap, with
    the injected-latency stub and a fresh-key forced cache MISS (see module doc).

    Returns `source` so k6 can assert `ai_verified` — proving the full miss path
    ran (call -> gate PASS -> put); a `fallback` would mean the gate rejected the
    stub text and `put` never ran (an incomplete miss path)."""
    result = await explain_tenant_gap(
        tenant_id,
        _CONTROL,
        explainer=LatencyStubExplainer(),
        prompt_version=uuid.uuid4().hex,
    )
    return {"source": result.source}
