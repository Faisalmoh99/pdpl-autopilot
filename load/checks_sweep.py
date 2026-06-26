"""Drive the POST /checks load sweep AND capture server-side pool evidence
(ADR-0014 §1/§4 — target 2, the write path).

k6 latency alone cannot tell pool-exhaustion from a CPU ceiling. The decisive
discriminator is server-side: how many connections `pdpl_app` actually holds on
Postgres during the load. SQLAlchemy's pool opens at most 15 (pool_size 5 +
overflow 10). So per VU level we sample `pg_stat_activity` in a background thread
while k6 runs, and record the PEAK:

  - peak total pdpl_app backends  -> if it pegs at 15, the pool is maxed;
                                     if it stays below 15, the pool is not even
                                     the binding constraint (CPU-bound, like the
                                     read path).
  - peak "busy" backends (active / idle-in-transaction) -> requests mid-flight.

When offered VUs exceed the peak total (capped at 15) the excess requests are,
by construction, blocked on pool checkout — that IS the checkout-wait, proven
server-side without instrumenting production code.

Loopback only (same env + admin DSN as reset_db.py). Reads nothing secret.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

_LOAD_DIR = Path(__file__).resolve().parent
load_dotenv(_LOAD_DIR / ".env.load", override=True)

_ADMIN_DSN = os.environ["LOAD_DB_ADMIN_DSN"]
_K6 = os.environ.get("K6_BIN", "/opt/homebrew/bin/k6")
_LEVELS = [3, 5, 10, 15, 20, 30, 50]
_DURATION = "30s"
_TREND = "avg,med,p(50),p(95),p(99),max"
_OUT = Path("/tmp/k6out")

# Which k6 script to drive (so the SAME pg sampler covers both the read and the
# write path for an apples-to-apples connection-count comparison). Default: the
# write path. Pass "readiness" to sweep GET /readiness instead.
_TARGET = sys.argv[1] if len(sys.argv) > 1 else "checks"
_SCRIPT = _LOAD_DIR / "k6" / f"{_TARGET}.js"


class PgSampler(threading.Thread):
    """Polls pg_stat_activity for pdpl_app connection counts until stopped."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_evt = threading.Event()
        self.max_total = 0
        self.max_busy = 0

    def run(self) -> None:
        conn = psycopg2.connect(_ADMIN_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        q = (
            "SELECT count(*), "
            "count(*) FILTER (WHERE state IN "
            "('active','idle in transaction','idle in transaction (aborted)')) "
            "FROM pg_stat_activity WHERE usename = 'pdpl_app'"
        )
        while not self._stop_evt.is_set():
            cur.execute(q)
            total, busy = cur.fetchone()
            self.max_total = max(self.max_total, total)
            self.max_busy = max(self.max_busy, busy)
            time.sleep(0.1)
        conn.close()

    def stop(self) -> None:
        self._stop_evt.set()


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for n in _LEVELS:
        summary = _OUT / f"{_TARGET}_{n}.json"
        sampler = PgSampler()
        sampler.start()
        subprocess.run(
            [
                _K6, "run", "--quiet",
                f"--summary-trend-stats={_TREND}",
                f"--summary-export={summary}",
                "-e", "SCENARIO=soak",
                "-e", f"SOAK_VUS={n}",
                "-e", f"SOAK_DURATION={_DURATION}",
                str(_SCRIPT),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        sampler.stop()
        sampler.join(timeout=3)

        m = json.loads(summary.read_text())["metrics"]
        d, rq, fa = m["http_req_duration"], m["http_reqs"], m["http_req_failed"]
        rows.append(
            {
                "vus": n,
                "p50": d["p(50)"], "p95": d["p(95)"], "p99": d["p(99)"],
                "rate": rq["rate"], "reqs": int(rq["count"]),
                "err_pct": fa.get("value", 0.0) * 100,
                "conn_peak": sampler.max_total,
                "busy_peak": sampler.max_busy,
            }
        )
        print(
            f"level VUs={n:>2}: conn_peak={sampler.max_total} "
            f"busy_peak={sampler.max_busy} p95={d['p(95)']:.1f}ms "
            f"req/s={rq['rate']:.0f} err={fa.get('value', 0.0) * 100:.2f}%"
        )

    base = rows[0]["p95"]
    print(f"\nbaseline (VU=3) p95 = {base:.2f} ms  ->  2x threshold = {2 * base:.2f} ms\n")
    hdr = (
        f"{'VUs':>4} | {'p50':>7} | {'p95':>7} | {'p99':>7} | {'req/s':>7} | "
        f"{'reqs':>7} | {'err%':>5} | {'conn_pk':>7} | {'busy_pk':>7} | {'p95>=2x':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        flag = "YES" if r["p95"] >= 2 * base else "no"
        print(
            f"{r['vus']:>4} | {r['p50']:>7.2f} | {r['p95']:>7.2f} | {r['p99']:>7.2f} | "
            f"{r['rate']:>7.0f} | {r['reqs']:>7} | {r['err_pct']:>5.2f} | "
            f"{r['conn_peak']:>7} | {r['busy_peak']:>7} | {flag:>7}"
        )
    print("\nconn_pk = peak pdpl_app backends on Postgres (pool ceiling = 15)")
    print("busy_pk = peak active / idle-in-transaction backends")


if __name__ == "__main__":
    main()
