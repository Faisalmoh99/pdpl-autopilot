"""Prove the load stub's text PASSES the gate under the seeded tenant's REAL
GapContext, BEFORE the explanation sweep (ADR-0014 §7 confirmation 2).

If `_GOOD_AR` failed the gate, every load request would fall to the deterministic
floor: `put` would never run, and the measured "miss path" would be call-only —
an incomplete shape that would invalidate the before/after. This script runs the
EXACT runtime derivation `explain_tenant_gap` uses (load answers -> decide ->
build_gap_context) for the first seeded tenant, then runs `verify_explanation` on
the stub text against that context, and FAILS LOUD unless every check passes.

It reuses `_GOOD_AR` / `_CONTROL` verbatim from `load/explain_app.py`, so the
thing proven here is the exact thing served under load — one source of truth.

Loopback only; same `.env.load` + local-DB refusal as the other load scripts.
Run after a seed (so `tenant_ids.json` exists) and before the sweep.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

_LOAD_DIR = Path(__file__).resolve().parent
_ENV_FILE = _LOAD_DIR / ".env.load"
_TENANT_IDS = _LOAD_DIR / "seed" / "tenant_ids.json"


def _load_env_or_die() -> None:
    if not _ENV_FILE.exists():
        sys.exit(f"missing {_ENV_FILE}")
    load_dotenv(_ENV_FILE, override=True)
    url = os.environ.get("APP_DATABASE_URL", "")
    if not any(host in url for host in ("localhost", "127.0.0.1")):
        sys.exit(f"REFUSING: APP_DATABASE_URL is not local (got {url!r}).")


async def _check() -> None:
    # Imported AFTER the env is set so Settings() binds to the LOCAL load DB.
    from pdpl.catalog import control_by_code
    from pdpl.db.session import dispose_engine, session_scope
    from pdpl.explanations import build_gap_context
    from pdpl.services.decision import build_control_decider, load_tenant_answers
    from pdpl.verification import verify_explanation

    # Reuse the EXACT stub text + control the load app serves (one source of truth).
    from explain_app import _CONTROL, _GOOD_AR  # noqa: PLC0415

    tenant_id = UUID(json.loads(_TENANT_IDS.read_text())[0])
    control = control_by_code(_CONTROL)

    try:
        # The real runtime derivation `explain_tenant_gap` performs.
        async with session_scope() as session:
            answers = await load_tenant_answers(session, tenant_id)
            decision = build_control_decider(answers)(_CONTROL)
        ctx = build_gap_context(
            control_code=_CONTROL,
            control_title_ar=control.title_ar,
            control_description_ar=control.description_ar,
            status=decision.status,
            rationale=decision.rationale,
            severity_weight=control.severity_weight,
            unsatisfied_codes=decision.unsatisfied_codes,
        )
    finally:
        await dispose_engine()

    verdict = verify_explanation(
        _GOOD_AR,
        control_code=ctx.control_code,
        control_title_ar=ctx.control_title_ar,
    )
    print(f"tenant={tenant_id} control={_CONTROL} status={decision.status!r}")
    for name, c in verdict.checks.items():
        print(f"  [{'PASS' if c.passed else 'FAIL'}] {name}: {c.reason}")
    if not verdict.passed:
        sys.exit(
            "\nGATE REJECTS THE STUB TEXT — the load miss path would be "
            "call-only (no put). Fix _GOOD_AR before sweeping."
        )
    print("\nOK: stub text passes the gate -> full miss path (call -> gate -> put) "
          "is exercised under load.")


def main() -> None:
    _load_env_or_die()
    sys.path.insert(0, str(_LOAD_DIR))  # so `import explain_app` resolves
    asyncio.run(_check())


if __name__ == "__main__":
    main()
