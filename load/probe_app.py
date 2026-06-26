"""Load-only ASGI app: the real app + a HOLD-TIME probe route (ADR-0014 §4/§7).

This lives in load/, NOT in src/ — it never enters the pdpl package and is never
on main's serving path. It is run via `uvicorn load.probe_app:app` for the
hold-time pool sweep ONLY.

The probe isolates connection HOLD-TIME as the single variable. It checks out a
real pooled connection (the SAME engine/pool the app uses, via session_scope),
issues a trivial query, then `await asyncio.sleep(HOLD)` — a PURE async sleep:

  - it holds the pooled connection for HOLD seconds (like a slow external call),
  - while leaving the event loop FREE (no time.sleep, no CPU work, no real I/O).

This is exactly the real Gemini call's shape (I/O wait: connection held, CPU
idle). Under concurrency it makes the 15-connection pool — not the event loop —
the binding resource, flipping the constraint that readiness/checks showed.
Re-running the pool-size sweep against this probe should make throughput TRACK
pool size (unlike the flat deterministic paths), completing the causal contrast.
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text

from pdpl.db.session import session_scope
from pdpl.main import create_app

# Seconds to hold the pooled connection per request (pure async wait).
_HOLD_SECONDS = float(os.environ.get("LOAD_PROBE_HOLD_SECONDS", "0.05"))

app = create_app()


@app.get("/_loadprobe/hold")
async def hold() -> dict:
    async with session_scope() as session:
        # Touch the DB so a real pooled connection is genuinely checked out and
        # held for the whole sleep (not just nominally).
        await session.execute(text("SELECT 1"))
        await asyncio.sleep(_HOLD_SECONDS)  # holds the connection; frees the loop
    return {"held_s": _HOLD_SECONDS}
