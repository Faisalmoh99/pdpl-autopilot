# ADR-0014: Load-Testing Methodology, Sandbox, and Breaking-Point Definition

- **Status:** Accepted
- **Date:** 2026-06-25
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0004 — Application Foundation and Observability](0004-application-foundation-and-observability.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [ADR-0007 — Readiness Scoring Model](0007-readiness-scoring-model.md), [ADR-0011 — Runtime Explanation Orchestration](0011-runtime-explanation-orchestration.md), [ADR-0012 — Explanation HTTP Surface](0012-explanation-http-surface.md), [CLAUDE.md — build rules](../../CLAUDE.md)

## Context

Phase 5 is a **deliberate skill exercise**, not a response to a production problem. The product has **zero real users and zero production traffic**. The goal is a documented, defensible answer to one question — *"at how many concurrent requests does this system's deterministic path start to degrade, and exactly which resource saturates first?"* — produced by load testing in a sandbox, plus **one measured architectural optimization** with a before/after number.

This is explicitly **not** an invitation to build for imagined scale. The deliverable is a *number with a cause* and a *transferable optimization*, not throughput infrastructure.

### What frames the breaking point we are hunting

Two facts, established by reading the code and the Supabase dashboard, set up the experiment before we run it:

1. **A coincidental double-15.** `src/pdpl/db/session.py` builds the async engine with **no explicit pool configuration**, so SQLAlchemy's `QueuePool` defaults apply: `pool_size=5 + max_overflow=10 = 15` client-side connections. The managed Supabase project (Nano compute) caps the server-side pool at **15** connections per user+db as well. These two ceilings are *independent* (one client-side, one server-side) and their equality is a **coincidence**, not a design — but it makes 15 the natural number to test toward.

2. **The explanation path holds a pooled connection across an external call.** In `POST /tenants/{id}/explanations` (`api/explanations.py`), the Gemini call in `explain_gap` runs **inside** the `session_scope` transaction. On a cache **miss**, one pooled connection is pinned for the **entire ~30 s** external call. This couples external-provider latency directly to connection-hold time — and is therefore the *future* bottleneck of the explanation path (framed here, measured in Phase 3, not yet fixed).

### What is deliberately out of scope (CLAUDE.md "Forbidden" / roadmap)

Real Gemini load (cost + Google's rate limit, which is *their* system not ours), the v2 prompt, scheduling/continuous monitoring, auth, production deployment, and **any optimization before the breaking-point data exists**.

## Decision drivers

- **Measure our system, not a third party.** A test whose knee is Google's rate limit teaches nothing about *our* breaking point. Confounds (external cost, rate limits, managed-pooler quirks) must be isolated out.
- **The load must read/produce shapes the runtime actually produces.** A knee measured on a hand-fabricated data shape is a knee on fiction. Seed faithfulness is the same discipline as the C3a/C4b identity tests: the load test exercises **production logic**, not a convenient stand-in.
- **The headline number must transfer to production.** The breaking point, and the optimization that improves it, must be stated at the **real** production pool config (15) — and the optimization must be one that *works on Nano*, where the pool cannot be grown.
- **Causal isolation over a single data point.** "It broke at N" is weak; "it broke at N, and the knee *tracks pool size* when we sweep it, proving the pool — not CPU or query plan — is the constraint" is the C-level signal.
- **Zero pollution, zero local-environment confounds.** The sandbox must not touch real Supabase data and must not re-trigger the local Supavisor SCRAM-cache connectivity gap logged 2026-06-25.

## Decision

### 1. Targets and ordering — the deterministic path first, two distinct signals

We load-test in this order, and we expect **two different knees for two different reasons** — that contrast is itself the lesson:

1. **`GET /tenants/{id}/readiness` first** — a single read (`scoring.py`: one `controls LEFT JOIN findings` query), short transaction, releases its pooled connection quickly. It isolates pool behaviour with no write noise. **Prediction (and an accepted finding either way):** because a fast read cycles its connection back to the pool quickly, readiness may **not** knee cleanly at 15 VUs — the knee may appear only at very high concurrency, or the path may be bound by CPU/query before the pool ever saturates. *If readiness does not knee at the pool ceiling, that is a finding, not a failure:* fast reads are not pool-constrained.
2. **`POST /tenants/{id}/checks` second** — a long write transaction (`checks.py`: per active control, an `INSERT finding` + an `INSERT audit_log` event, occasionally an outbox row, all in **one** `session_scope`). It holds its pooled connection far longer, so the **pool-exhaustion knee should appear near the ceiling (≈15 concurrent)**. This is where queuing on a held connection produces the classic "latency climbs while throughput flatlines" signal.
3. **`POST /tenants/{id}/explanations` third**, backed by a **stub**, never the real Gemini API (§3). This exercises the real orchestration (cache + gate + put) and the held-connection-across-an-external-call shape from Context fact 2.

**The cold-Gemini path is not load-tested.** It measures Google's rate limit and costs money — out of scope. A stub with an *injected latency* lets us measure **our** system's behaviour (a pooled connection held across a slow external call) without any dependency on Google.

### 2. Sandbox — a LOCAL Postgres on loopback, not managed Supabase

The load-test target is a **local Postgres 16, reached over `localhost`**, not the managed Supabase project. Three drivers, each decisive — and the **loopback** property is the load-test-critical one:

- **The network is eliminated as a variable (the load-test-critical driver).** Every query crosses only the loopback interface, so the measured latency is *system behaviour only*. A managed/cloud DB would route each query over the internet (this machine sits in Jeddah; the project is in `eu-central-1` Frankfurt, ~80–120 ms RTT), mixing network latency into p95 — the 2× knee rule would then sit on jittery network time, not pool behaviour, and "a number with a *cause*" would be unrecoverable. Loopback is non-negotiable for a load test.
- **Isolation from the local connectivity gap.** The 2026-06-25 build-log records a local IPv6 block forcing the Supavisor session pooler, plus a stale SCRAM verifier cache on that pooler. A local loopback Postgres sidesteps the pooler entirely — the run cannot be blocked by that environment quirk, and a cloud DB would *re-trigger* exactly it.
- **Full pool control.** We can set the server-side `max_connections` and the client-side pool independently and watch the knee move — impossible on the fixed managed Nano pool. This is what makes the §4 sweep a *causal* experiment.

**Implementation: a Homebrew-managed local Postgres 16, not Docker (deviation from the original decision, recorded honestly).** This ADR first specified a local Postgres in *Docker Compose*. At execution the machine had no container runtime installed (no Docker/Podman/Colima), and installing a VM-backed runtime was heavier than the exercise warranted. We switched to `brew install postgresql@16`. **The two load-bearing drivers are fully preserved** — it is still a *local loopback* Postgres (network eliminated, pooler sidestepped) with *full pool control*. Only the **ephemerality mechanism** changes: instead of `docker compose down -v` wiping a volume, the run begins with `load/reset_db.py` doing `DROP DATABASE IF EXISTS pdpl_load; CREATE DATABASE pdpl_load` as the local superuser (run before the migrations), giving the same from-clean guarantee. **Zero pollution** is preserved by construction: the seed refuses to run unless `APP_DATABASE_URL` is local, and `pdpl_load` is a dedicated throwaway database. The Homebrew cluster uses loopback `trust` auth, which is irrelevant to what we measure: audit-log immutability and the pool behaviour come from the **role grants** (ADR-0003), not from the auth method — `pdpl_app` is created with the identical grant pattern by the same migrations.

The schema is built by running the **real Alembic migrations** (`migrations/versions/0001`…`0006`) against the load database — including `0002_pdpl_app_login` (the `pdpl_app` role + grants) and `0003`/`0004` (the seeded controls and questions). The load DB is therefore byte-identical in structure and seed catalogue to production; only the *host* (loopback) and the *auth method* (local trust) differ.

### 3. Seed faithfulness — every fixture passes through production logic

The load fixtures must be rows the **runtime actually produces**, never hand-fabricated shapes. Concretely:

- **Multiple tenants (≈20)** so each k6 VU targets a *different* tenant, avoiding fake same-row contention that the runtime would never exhibit.
- **Every one of the ≈20 tenants' findings is built by calling the real `run_check`** at seed time (create tenant `active` → submit answers → `run_check(tenant_id)`). **No tenant's findings are hand-`INSERT`ed**, and **no** tenant is seeded by copying another's rows. The readiness load therefore reads only rows that passed the real decision engine + SCD-Type-2 write path — the knee is measured on the true data shape.
- **Explanation seed (framed now, built in Phase 3):** the non-compliant control that produces the gap must originate from **answers passing through the real `decide()` / `build_deterministic_decider` path** — so the explanation load tests real `GapContext` construction, not a stand-in `ControlDecision`. This is the same faithfulness rule as C3a/C4b, stated here so Phase 3 does not rebuild the seed.

### 4. Metrics and the pool sweep — observation, then causal isolation

**The headline number** is *"requests served concurrently before p95 latency degrades, at the production pool config (pool=15), single uvicorn worker."* It is reported at the realistic 15 — the honest, defensible, transferable figure — never at an artificially altered pool.

**The knee is defined operationally and pre-registered — before any run — so the breaking point is derived from a *rule*, never from an eyeballed "looks like it bends here."** p95 climbs gradually; there is rarely a sharp cliff, and a subjective visual call is exactly the looks-like-vs-provably trap this project rejects. The pre-registered rule:

1. A **baseline** run at low concurrency (1–5 VUs) fixes the reference p95 (`p95_base`).
2. The **knee** is the *first* VU level at which **both** hold simultaneously:
   - **p95 ≥ 2 × `p95_base`** (latency has materially degraded), **and**
   - **throughput has flattened** — req/s stops rising as VUs rise (Δ throughput per added VU ≈ 0). This supply/demand equality — more concurrency, no more completed work — *is* the signature of pool exhaustion, distinct from a path that is merely slow.

Both conditions are required: doubling alone could be a slow-but-scaling path; flattening alone could be a throughput ceiling without a latency problem. Their **conjunction** is the knee. The rule is committed here in writing; the run only *applies* it.

**The headline is single-worker-conditional, stated so it is not misread as an absolute production figure.** "N concurrent" holds for **one** uvicorn worker with **one** pool (§6 — the invariant that isolates the pool signal). Production behind multiple workers means **one pool per worker**, so the aggregate concurrency before degradation is different (roughly per-worker × workers, modulo the shared server-side 15 cap). The number we report is *"N concurrent per worker-with-its-own-pool,"* not a standalone production SLA.

**Signals collected:**

- **From k6:** latency percentiles (p50/p95/p99), throughput (req/s), error rate, and the VU level at which p95 begins to climb. Thresholds are **report-only** (`abortOnFail: false`) — we *observe* the knee, we do not abort at it.
- **From the existing observability (ADR-0004), under real load for the first time:** the structured-JSON counters (`check_run.completed`, `scoring.readiness_report`, `explanations.cache` hit/miss, `explanations.served`) and the per-request correlation ID. This is the first time Phase-2 observability is exercised under concurrency.
- **From Postgres / SQLAlchemy:** `pg_stat_activity` for live connection count, and the SQLAlchemy pool checkout-wait (a climbing checkout time with flat throughput is the pool-exhaustion fingerprint).

**The pool sweep is a DIAGNOSTIC, not an optimization.** We run the same load at `pool_size` ∈ {5, 10, 15} (via the knob in §5). **If the knee tracks the pool size, that proves the connection pool — not CPU or query plan — is the bottleneck.** This is causal isolation with the same rigor as the identity/keystone tests, and it is the *only* legitimate use of shrinking the pool.

**Explicitly rejected as vacuous:** framing "shrink the pool to 5, then expand it back to 15" as a before/after *optimization*. Expanding a pool you artificially shrank is **removing a self-imposed handicap**, not improving the system — a circular, meaningless before/after, the same empty pattern as a same-text idempotency test. The sweep earns its place **only** as causal evidence, never as a headline improvement.

### 5. The pool-size knob — config-only, transparent in production

To make the §4 sweep config-only (no code churn between runs), two optional settings are added: `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` (`config.py`), read in `session.py` when the engine is built.

**Transparency in production is a hard requirement:**

- **`None` ⇒ byte-for-byte the current behaviour.** When the env vars are unset (the production/main default), `_build_engine` passes **no** `pool_size`/`max_overflow` to `create_async_engine` at all — SQLAlchemy applies its existing defaults (5 + 10 = 15) **unchanged**. The knob is built as conditional kwargs; nothing about the engine changes on `main` when the env is not set.
- **No contract touched.** The values are read through the existing `get_settings()` call already imported by `session.py`; there is no new import across any `.importlinter` boundary. The seven contracts stay green.

### 6. Tooling and layout — k6, outside the app package

- **k6** (the roadmap's choice) drives load. Scripts live in **`load/`** at the repo root, **outside `src/pdpl/`** — they are operational tooling, never imported by the app, so they cannot interact with any architectural contract.
- **Closed-model load (`ramping-vus`)**, not arrival-rate: "N concurrent" *is* the VU count, and each VU holds one in-flight request — exactly the concurrency that maps to a pooled-connection checkout. Two scenarios per script: a **ramp** (`5 → 10 → 15 → 20 → 30 → 50 VUs`) that brackets the 15 ceiling from both sides to locate the knee, and a **soak** (constant VUs below the knee, several minutes) to surface drift/leaks over time.
- **One uvicorn worker** during a run. Multiple workers means one pool *per* worker, which confounds the pool-exhaustion knee. The single-worker invariant is stated in `load/README.md`.

### 7. The one optimization — architectural (hold-time), never "grow the pool"

After the breaking-point data exists, Phase 5 makes **one** measured improvement. Its nature is decided now as a **principle**, even though the concrete fix waits for the data:

**The optimization must reduce connection hold-time, not enlarge the pool.** The reason is transferability: production (Supabase Nano) is **capped at 15** server-side, so "increase the pool" **does not transfer** — it is unavailable where it matters. Only reducing how long each request holds a connection improves the real production ceiling.

Likely candidates (to be confirmed by the knee data, **not** pre-built):

- **Explanation path:** move the Gemini call **outside** `session_scope` — read and release the connection, make the external call holding *no* pooled connection, then open a short new transaction solely for the verified `put`. (This is ADR-0011/0012's seam, surfaced here as the hold-time fix.)
- **Deterministic write path:** shorten the `run_check` transaction's connection hold (e.g. reduce per-control round-trips), so the write path cycles connections faster under concurrency.

We do **not** pre-optimize: the actual fix is chosen and built only after the data shows the real knee.

## Findings (execution — 2026-06-26, single worker, loopback Postgres)

Stages 1 (`GET /readiness`) and 2 (`POST /checks`) were run, plus a causal pool-size sweep and a hold-time probe. The headline result **inverted the going-in hypothesis** (that the write path would be pool-bound), and the inversion is the lesson.

### 1. Both deterministic paths saturate the pool but are NOT pool-bound

Per-VU ramp (constant-VU levels, 30s each), with `pg_stat_activity` sampled for `pdpl_app`:

| path | throughput ceiling | conn peak (VU≥15) | p95 @ 3→50 VU | errors | knee (2× baseline + flat) |
|---|---|---|---|---|---|
| readiness | ~2000 req/s | 15 | 2.3 → 40 ms | 0% | ~20 VU |
| checks | ~1230 req/s | 15 | 3.7 → 69 ms | 0% | ~10–15 VU |

Both peg `conn=15` above 15 VU (the pool is fully **used**), both plateau in throughput while latency climbs linearly, both with zero errors (requests queue on checkout within the 30 s timeout, never exceeding it). An earlier claim that "readiness shows zero pool-wait" was **corrected by direct measurement**: the read path *also* saturates the 15 connections.

### 2. The pool-size sweep proves neither is pool-bound — the event loop is

Holding load fixed above saturation (VU=30) and varying `pool_size` ∈ {5,10,15,25} (`max_overflow=0`, so pool total = `pool_size`), max of two runs each, reset+reseed per pool to bound write bloat:

| pool_size | readiness req/s | checks req/s |
|---:|---:|---:|
| 5 | 1904 | 1224 |
| 10 | 2096 | 1235 |
| 15 | 2116 | 1274 |
| 25 | 2132 | 1301 |

**Both FLAT** — a 5× larger pool buys ~12% (read, 1904→2132) / ~6.3% (write, 1224→1301). Using all 15 connections and being *limited* by them are different things, and the sweep separates them: on loopback the single uvicorn **event loop / CPU** is the binding constraint, not the pool. The read/write gap (~1.6×) is per-request CPU cost on that one loop (the write does more work per request), independent of connection count.

### 3. A hold-time probe flips the binding resource — the causal proof

The probe (`load/probe_app.py`, load-only, never on main) holds a pooled connection across a **pure `asyncio.sleep(50 ms)`** — connection held, event loop free, exactly the shape of a slow external (Gemini) call. Same VU=30 pool-size sweep:

| pool_size | probe req/s | p95 |
|---:|---:|---:|
| 5 | 85 | 365 ms |
| 10 | 156 | 206 ms |
| 15 | 223 | 154 ms |
| 25 | 428 | 103 ms |

Throughput **TRACKS pool size linearly** (5× pool → 5.0× throughput) — **POOL-bound**, the opposite of the deterministic paths. Same system, same `conn=15` saturation, same single worker; the only changed variable is **connection hold-time**. Conclusion, proven causally: **hold-time alone decides whether the pool or the event loop is the binding constraint** — long hold → pool-bound, short hold → event-loop-bound.

### 4. What this means for production and for §7

Loopback deliberately removes the network, which is *why* the deterministic paths land in the event-loop-bound regime here (per-request hold-time ≈ ms). In production the DB is networked (~80–120 ms RTT), so real hold-time sits in the **probe's regime** — and the 15-connection Nano pool becomes the binding constraint, exactly as the probe shows. Therefore **§7 stands and is now evidence-backed**: the production optimization is to **reduce connection hold-time, not grow the pool**, and the concrete instance is the explanation path's Gemini call held inside `session_scope` (move it out — read/release, call holding no connection, short new txn for the `put`). The deterministic write path's hold-time fix is real but would show no loopback gain (it is event-loop-bound here); its payoff is in the networked regime. Raising the deterministic ceiling on *this* setup would instead need more workers — bounded in production by the shared 15-connection server-side cap (the genuine tension).

## Consequences

**Positive**

- A defensible, transferable headline: *"serves N concurrent before p95 degrades, at the production pool config (15), single worker"* — derived from a **pre-registered rule** (p95 ≥ 2× baseline **and** throughput flattens), not an eyeballed bend, and measured on faithful, production-logic data, not a fabricated shape.
- Causal isolation, not a lone data point: the pool sweep *proves* the pool is the constraint (or proves it is not, for the read path) — the same rigor as the identity/keystone tests.
- Phase-2 observability (counters + correlation ID) is validated under concurrency for the first time, with a documented signal-to-saturation mapping.
- The optimization is pre-committed to be **architectural and production-transferable** (hold-time), structurally ruling out the vacuous "grow the pool" non-fix.
- Zero blast radius: a disposable local loopback database (`DROP`/`CREATE` on each run), real data untouched, the local Supavisor connectivity gap sidestepped.

**Negative / accepted (bounded, surfaced not hidden)**

- **The read path may not knee at the pool ceiling.** Accepted and re-framed as a finding: fast reads cycle connections and are not pool-bound; the contrast with the write path is the intended lesson.
- **Local loopback Postgres ≠ managed Supabase byte-for-byte at the infra layer.** Same engine version and identical schema/seed (real migrations), but no Supavisor pooler, loopback `trust` auth instead of SCRAM, different host hardware, and Nano's CPU/IO profile is not reproduced. The *pool-exhaustion* mechanism transfers (15 is 15) and the auth method does not affect it; absolute latency numbers are sandbox-relative, and we report them as such.
- **Stub-backed explanation load does not exercise real model latency variance.** Deliberate: the cold-Gemini path is out of scope (cost + Google's rate limit). The injected-latency stub measures *our* hold-time behaviour, which is the part we can act on.
- **The optimization is framed before the data.** Only the *principle* (hold-time, not pool) is fixed now; the concrete change waits for the knee, honoring "no optimization before the breaking-point data exists."

## Open questions (deferred to Phase-3 explanation load)

- **How the stub explainer is injected without hitting Gemini.** The `POST /explanations` route hardcodes `gemini_explainer_from_settings` and does not expose an override. Leading candidate: add a `GEMINI_BASE_URL` setting and point it at a local fake-Gemini HTTP server returning a valid Arabic response (optionally with injected latency) — this preserves the **full** real orchestration path (cache + gate + put), cleaner than a load-only app factory. Resolved when Phase 3 begins.
