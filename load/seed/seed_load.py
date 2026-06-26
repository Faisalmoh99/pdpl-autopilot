"""Seed the local load-test database with FAITHFUL, runtime-produced fixtures
(ADR-0014 §3).

The discipline (same as the C3a/C4b identity tests): the load test must read
rows the runtime actually produces, never a hand-fabricated shape. So every
tenant here is created and assessed through the REAL production paths:

    create_tenant(...)      # the real POST /tenants handler (insert + audit row)
    record_answers(...)     # the real ADR-0005 evidence-write service
    run_check(tenant_id)    # the real deterministic engine + SCD-Type-2 write

There is NO direct INSERT into `findings`, and NO tenant is seeded by copying
another's rows. Each of the N tenants passes through `run_check`, so the
readiness load measures the true data shape, and the explanation path (Phase 3)
finds a non-compliant control produced by the real `decide()` path.

The answer set is chosen to yield a realistic MIX via the engine:
    - the four ART12 privacy-notice questions = 'no'  -> non_compliant
      (this is the explanation target — a real gap from real answers)
    - the two ART4 access questions          = 'yes' -> compliant
    - breach (ART20) and ROPA (ART31)        unanswered -> not_assessed
=> readiness reports a non_compliant gap + two not_assessed gaps per tenant.

SAFETY: this script loads `load/.env.load` with override=True (so it wins over
any exported APP_DATABASE_URL — build-log 2026-06-25 — and over the real .env)
and then REFUSES to run unless APP_DATABASE_URL is local. It can never touch
Supabase.

Prerequisites (see load/README.md): the local DB is up and migrated
(`alembic upgrade head` with this same env), so the schema + seeded controls
and questions (migrations 0003/0004) already exist.

Output: writes the created tenant ids to `load/seed/tenant_ids.json`, which the
k6 scripts read so each VU targets a different tenant (avoiding fake same-row
contention).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_LOAD_DIR = Path(__file__).resolve().parent.parent  # repo/load/
_ENV_FILE = _LOAD_DIR / ".env.load"
_TENANT_IDS_OUT = _LOAD_DIR / "seed" / "tenant_ids.json"

N_TENANTS = 20

# A real, deterministic gap from real answers (see module docstring).
_ANSWERS: dict[str, str] = {
    "Q-ART12-NOTICE-EXISTS": "no",
    "Q-ART12-NOTICE-PURPOSES": "no",
    "Q-ART12-NOTICE-RECIPIENTS": "no",
    "Q-ART12-NOTICE-RIGHTS": "no",
    "Q-ART4-ACCESS-PROCESS": "yes",
    "Q-ART4-ACCESS-TIMEFRAME": "yes",
}


def _load_env_or_die() -> None:
    """Load the load env and refuse to proceed against a non-local DB."""
    if not _ENV_FILE.exists():
        sys.exit(f"missing {_ENV_FILE}")
    # override=True is the safety mechanism: an exported APP_DATABASE_URL (which
    # the build-log notes silently overrides .env) must NOT win here.
    load_dotenv(_ENV_FILE, override=True)
    url = os.environ.get("APP_DATABASE_URL", "")
    if not any(host in url for host in ("localhost", "127.0.0.1")):
        sys.exit(
            "REFUSING TO SEED: APP_DATABASE_URL is not local "
            f"(got {url!r}). The load seed only ever targets the local Docker "
            "Postgres — never Supabase."
        )


async def _seed() -> None:
    # Imported AFTER the env is set so Settings() binds to the LOCAL load DB.
    from sqlalchemy import text

    from pdpl.api.tenants import TenantCreate, create_tenant
    from pdpl.db.session import dispose_engine, session_scope
    from pdpl.services.answers import record_answers
    from pdpl.services.checks import run_check

    tenant_ids: list[str] = []
    try:
        # PROVE the effective role is pdpl_app, NOT the system superuser. Under
        # loopback trust a userless connection would enter as superuser and
        # BYPASS the grants — the writes would still work but they would no
        # longer pass through the restricted role, making the data shape
        # unfaithful (and a later immutability check would falsely pass). We
        # fail loud rather than seed under the wrong role.
        async with session_scope() as session:
            role = (await session.execute(text("SELECT current_user"))).scalar_one()
        print(f"seed effective role: current_user={role!r}")
        if role != "pdpl_app":
            sys.exit(
                f"REFUSING TO SEED: effective role is {role!r}, not 'pdpl_app'. "
                "The load seed MUST write through the restricted role so the "
                "data shape is faithful (ADR-0003 grants)."
            )

        for i in range(N_TENANTS):
            tenant = await create_tenant(
                TenantCreate(name=f"load_test_tenant_{i:02d}", business_type="other")
            )
            await record_answers(tenant.id, _ANSWERS)
            await run_check(tenant.id)  # REAL write path — never a hand INSERT
            tenant_ids.append(str(tenant.id))

        # Summarise the CURRENT-finding status mix across all load tenants, to
        # confirm the real engine produced the expected realistic mix.
        async with session_scope() as session:
            mix = (
                await session.execute(
                    text(
                        "SELECT status, count(*) AS n FROM findings "
                        "WHERE valid_to IS NULL GROUP BY status ORDER BY status"
                    )
                )
            ).all()
    finally:
        await dispose_engine()

    _TENANT_IDS_OUT.write_text(json.dumps(tenant_ids, indent=2) + "\n")
    print(f"seeded {len(tenant_ids)} tenants via run_check -> {_TENANT_IDS_OUT}")
    print("current-finding status mix (all load tenants):")
    for row in mix:
        print(f"  {row.status:>14} : {row.n}")


def main() -> None:
    _load_env_or_die()
    asyncio.run(_seed())


if __name__ == "__main__":
    main()
