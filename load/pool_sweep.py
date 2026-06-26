"""Causal pool-size sweep (ADR-0014 §4 — the diagnostic that proves pool-bound
vs CPU-bound).

`conn_peak=15` proves the pool is fully USED, not that it is the BINDING
constraint. To separate the two, hold the offered load fixed ABOVE saturation
(VU=30) and vary the pool size {5,10,15,25} (max_overflow=0 so the pool total IS
pool_size). Then read the throughput response:

  - throughput TRACKS pool size (rises 5 -> 25)  => pool-bound (the pool is the
    binding constraint; more connections = more concurrent work).
  - throughput FLAT despite a bigger pool (15 -> 25 unchanged) => CPU-bound (the
    pool is full but the single event loop is the ceiling).

Run on BOTH paths; the expected CONTRAST is the lesson.

Measurement hygiene (learned from a first contaminated run):
  - The write path bloats check_runs/audit_log, and a checkpoint/autovacuum
    stall inside a 30s window tanks throughput for that cell. So we RESET + RESEED
    before every pool size (bounded write history) and take the MAX throughput
    over REPEATS runs per cell — contamination only ever DEPRESSES throughput, so
    the max is a robust estimate of the true ceiling.
  - Each pool size needs a fresh app process (the pool is built once at engine
    construction); we restart uvicorn per size with DB_POOL_SIZE via env ONLY —
    production/main is never touched. A short warmup follows health.

Loopback only; same env + admin DSN as the other load scripts.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

_LOAD_DIR = Path(__file__).resolve().parent
_REPO = _LOAD_DIR.parent
import os  # noqa: E402

_ENV = {**os.environ, **dotenv_values(_LOAD_DIR / ".env.load")}
_ADMIN_DSN = _ENV["LOAD_DB_ADMIN_DSN"]
_K6 = _ENV.get("K6_BIN", "/opt/homebrew/bin/k6")
_PY = str(_REPO / ".venv" / "bin" / "python")
_ALEMBIC = str(_REPO / ".venv" / "bin" / "alembic")
_OUT = Path("/tmp/k6out")

_POOL_SIZES = [5, 10, 15, 25]
_VUS = 30
_DURATION = "20s"
_REPEATS = 2
_PORT = 8000
_TREND = "avg,med,p(95),p(99),max"

# Mode: "det" (default) sweeps the deterministic readiness+checks paths on the
# real app; "probe" sweeps the hold-time probe on the load-only probe app, with
# a 50ms pure-async hold injected so the pool (not the event loop) binds.
_MODE = sys.argv[1] if len(sys.argv) > 1 else "det"
if _MODE == "probe":
    _APP = "load.probe_app:app"
    _TARGETS = ["probe"]
    _EXTRA_ENV = {"LOAD_PROBE_HOLD_SECONDS": "0.05"}
else:
    _APP = "pdpl.main:app"
    _TARGETS = ["readiness", "checks"]
    _EXTRA_ENV = {}


class PgSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop_evt = threading.Event()
        self.max_total = 0

    def run(self) -> None:
        conn = psycopg2.connect(_ADMIN_DSN)
        conn.autocommit = True
        cur = conn.cursor()
        while not self._stop_evt.is_set():
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE usename = 'pdpl_app'"
            )
            self.max_total = max(self.max_total, cur.fetchone()[0])
            time.sleep(0.1)
        conn.close()

    def stop(self) -> None:
        self._stop_evt.set()


def _wait_port_free(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket()
        try:
            s.connect(("localhost", _PORT))
            s.close()
            time.sleep(0.3)  # still bound
        except OSError:
            return  # refused -> free
    raise RuntimeError(f"port {_PORT} did not free")


def _wait_health(timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://localhost:{_PORT}/health", timeout=1
            ) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("app did not become healthy")


def _reset_and_seed() -> None:
    subprocess.run([_PY, "load/reset_db.py"], cwd=_REPO, env=_ENV, check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run([_ALEMBIC, "upgrade", "head"], cwd=_REPO, env=_ENV, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([_PY, "load/seed/seed_load.py"], cwd=_REPO, env=_ENV, check=True,
                   stdout=subprocess.DEVNULL)


def _warmup() -> None:
    ids = json.loads((_LOAD_DIR / "seed" / "tenant_ids.json").read_text())
    for i in range(120):
        tid = ids[i % len(ids)]
        try:
            urllib.request.urlopen(
                f"http://localhost:{_PORT}/tenants/{tid}/readiness", timeout=2
            ).read()
        except Exception:
            pass


def _start_app(pool_size: int) -> subprocess.Popen:
    env = {**_ENV, "DB_POOL_SIZE": str(pool_size), "DB_MAX_OVERFLOW": "0",
           **_EXTRA_ENV}
    proc = subprocess.Popen(
        [".venv/bin/uvicorn", _APP, "--workers", "1", "--port", str(_PORT)],
        cwd=_REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _wait_health()
    _warmup()
    return proc


def _run_once(target: str, pool_size: int, rep: int) -> dict:
    summary = _OUT / f"poolsweep_{target}_{pool_size}_{rep}.json"
    sampler = PgSampler()
    sampler.start()
    subprocess.run(
        [_K6, "run", "--quiet", f"--summary-trend-stats={_TREND}",
         f"--summary-export={summary}", "-e", "SCENARIO=soak",
         "-e", f"SOAK_VUS={_VUS}", "-e", f"SOAK_DURATION={_DURATION}",
         str(_LOAD_DIR / "k6" / f"{target}.js")],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sampler.stop()
    sampler.join(timeout=3)
    m = json.loads(summary.read_text())["metrics"]
    return {
        "rate": m["http_reqs"]["rate"],
        "p95": m["http_req_duration"]["p(95)"],
        "err_pct": m["http_req_failed"].get("value", 0.0) * 100,
        "conn_peak": sampler.max_total,
    }


def _measure(target: str, pool_size: int) -> dict:
    runs = [_run_once(target, pool_size, r) for r in range(_REPEATS)]
    best = max(runs, key=lambda x: x["rate"])  # max throughput = clean ceiling
    best["conn_peak"] = max(x["conn_peak"] for x in runs)
    return best


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict]] = {t: [] for t in _TARGETS}
    for pool_size in _POOL_SIZES:
        subprocess.run(["pkill", "-f", "bin/uvicorn"], check=False)
        _wait_port_free()
        _reset_and_seed()
        proc = _start_app(pool_size)
        try:
            for target in _TARGETS:
                r = _measure(target, pool_size)
                r["pool_size"] = pool_size
                results[target].append(r)
                print(f"pool={pool_size:>2} {target:>9}: req/s={r['rate']:>6.0f} "
                      f"conn_peak={r['conn_peak']:>2} p95={r['p95']:.1f}ms "
                      f"err={r['err_pct']:.2f}%", flush=True)
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            _wait_port_free()

    print(f"\n=== pool-size sweep @ VU={_VUS}, max_overflow=0 (pool total = pool_size), "
          f"max of {_REPEATS} ===", flush=True)
    for target in _TARGETS:
        print(f"\n{target}  (req/s vs pool_size):")
        print(f"  {'pool':>5} | {'req/s':>7} | {'conn_pk':>7} | {'p95(ms)':>8} | {'err%':>5}")
        print("  " + "-" * 44)
        for r in results[target]:
            print(f"  {r['pool_size']:>5} | {r['rate']:>7.0f} | {r['conn_peak']:>7} | "
                  f"{r['p95']:>8.1f} | {r['err_pct']:>5.2f}")
        rates = {r["pool_size"]: r["rate"] for r in results[target]}
        grew = rates[25] > rates[10] * 1.25
        print(f"  => {'TRACKS pool size -> POOL-BOUND' if grew else 'FLAT vs pool size -> CPU/event-loop-BOUND'}")


if __name__ == "__main__":
    main()
