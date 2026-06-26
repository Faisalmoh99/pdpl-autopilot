"""Reset the local load-test database to a clean state (ADR-0014 §2).

Replaces `docker compose down -v` now that the sandbox is a Homebrew-managed
local Postgres rather than a Docker volume: it DROPs and re-CREATEs the
`pdpl_load` database as the local superuser, so every load run starts clean
before the migrations rebuild the schema + seed.

Why this is a SEPARATE step (not part of seed_load.py): `DROP DATABASE` cannot
run inside a transaction, cannot run while connected to the target database, and
needs superuser. seed_load.py connects as `pdpl_app` to `pdpl_load` itself, so
it structurally cannot drop its own database. This script connects to the
`postgres` maintenance database as the local superuser instead.

SAFETY: loopback only. It refuses any admin DSN whose host is not localhost, so
it can never drop a remote database — the same refusal discipline as
seed_load.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

# Load .env.load directly (override=True) rather than trusting the shell env:
# LOAD_DB_ADMIN_DSN contains spaces, which `set -a && source .env.load` truncates
# at the first space. Reading the file here makes the DSN immune to that.
load_dotenv(Path(__file__).resolve().parent / ".env.load", override=True)

# User is intentionally omitted: libpq defaults it to the OS user, who is the
# Homebrew cluster's superuser — so nothing machine-specific is committed.
_ADMIN_DSN = os.environ.get(
    "LOAD_DB_ADMIN_DSN", "host=localhost port=5432 dbname=postgres"
)
_TARGET_DB = "pdpl_load"


def _assert_local(dsn: str) -> None:
    if not any(host in dsn for host in ("localhost", "127.0.0.1")):
        sys.exit(f"REFUSING TO RESET: admin DSN is not local (got {dsn!r}).")


def main() -> None:
    _assert_local(_ADMIN_DSN)
    conn = psycopg2.connect(_ADMIN_DSN)
    try:
        conn.autocommit = True  # DROP/CREATE DATABASE cannot run in a transaction
        with conn.cursor() as cur:
            # Terminate any lingering connections to the target so DROP succeeds
            # (e.g. a still-running uvicorn from a previous run).
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (_TARGET_DB,),
            )
            cur.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(_TARGET_DB)
                )
            )
            cur.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(_TARGET_DB))
            )
    finally:
        conn.close()
    print(f"reset database {_TARGET_DB!r} (dropped + recreated, clean)")


if __name__ == "__main__":
    main()
