# Case study: the load-testing hypothesis inversion

> An engineer's strongest signal is not being right on the first guess — it is
> correcting himself by measurement. This is the record of a going-in hypothesis
> that direct measurement killed, the optimization that replaced it, and the
> safety invariant that never moved.

**Project:** PDPL Autopilot — a compliance-readiness assistant for small Saudi
businesses.
**Phase:** 5 (Scale), stages 1–3, executed 2026-06-26 / 2026-06-27.
**Primary sources:** [ADR-0014 — Load-Testing Methodology](../adr/0014-load-testing-methodology.md)
(methodology, the §7 fix, the sweep tables) and the Phase-5 build-logs. Every
number below traces to one of those artifacts.

---

## 1. Context

PDPL Autopilot serves AI-drafted explanations of compliance gaps, but an AI
explanation is never returned directly: it passes a deterministic gate before it
reaches a user. The explanation request path therefore does real work — read the
tenant's decision, build the gap context, call the model, **gate the output**,
then cache it. The system has zero real users and zero production traffic; Phase 5
was a deliberate skill exercise with one deliverable: *a breaking-point number
with a named cause, plus one optimization that transfers to production.* Load-
testing the explanation path mattered because it is the one path that holds a
database connection across a slow external (model) call — the exact shape where a
small connection pool can become the ceiling.

## 2. The going-in hypothesis

The honest belief before any measurement: **the write/explanation path is
pool-bound, and the 15-connection cap is the ceiling.** The number 15 was not
arbitrary. SQLAlchemy's default `QueuePool` is `pool_size=5 + max_overflow=10 =
15` connections client-side; the managed Supabase project (Nano compute) caps the
server-side pool at 15 as well. Two independent ceilings that happen to be equal —
a coincidence, not a design, but it made 15 the natural number to test toward. The
reasoning felt airtight: a long transaction holds its pooled connection, so above
15 concurrent requests the pool saturates and throughput flatlines. That is a
clean, plausible story. It was also wrong, and only a measurement could show it.

## 3. The measurement that inverted it

Three instruments, run in a local loopback sandbox (network removed on purpose, so
the measured latency is *system behaviour only*):

**Direct connection counting (`pg_stat_activity`).** Above 15 VUs, both
deterministic paths pegged `conn = 15` — the pool was fully *used*. Readiness
ceilinged at ~2000 req/s (p95 2.3 → 40 ms, knee ≈ 20 VU); checks at ~1230 req/s
(p95 3.7 → 69 ms, knee ≈ 10–15 VU); zero errors on both. Saturated, as predicted.

**The pool-size sweep ({5, 10, 15, 25} connections, load fixed at VU=30).** If the
pool were the constraint, throughput should track pool size. It did not:

| pool_size | readiness req/s | checks req/s |
|---:|---:|---:|
| 5  | 1904 | 1224 |
| 10 | 2096 | 1235 |
| 15 | 2116 | 1274 |
| 25 | 2132 | 1301 |

A **5× larger pool bought ~12% on reads (1904 → 2132) and ~6.3% on writes
(1224 → 1301).** Essentially flat. The pool was fully used and yet not the limit —
two different facts the sweep pulled apart. On loopback the single uvicorn **event
loop / CPU** was the binding constraint, not connections.

**The hold-time probe.** A load-only route held one pooled connection across a pure
`asyncio.sleep(50 ms)` — connection pinned, event loop free: the exact shape of a
slow external call. Same VU=30 sweep:

| pool_size | probe req/s | p95 |
|---:|---:|---:|
| 5  | 85  | 365 ms |
| 10 | 156 | 206 ms |
| 15 | 223 | 154 ms |
| 25 | 428 | 103 ms |

Throughput **tracked pool size linearly — 5× pool → 5.0× throughput.** Pool-bound,
the exact opposite of the deterministic paths. Same system, same `conn = 15`
saturation, same single worker; the only changed variable was **connection
hold-time.**

**The lesson the three instruments together teach: the tool deceives, the resource
does not.** k6's throughput-and-latency shape is identical whether the bottleneck
is CPU or the pool — latency climbs, throughput flattens, the same curve both
times. The curve says *"it broke."* Only a direct resource counter says *"which
resource."* "Fully used" and "the limit" are two different claims, and the sweep is
what separated them.

## 4. The two self-corrections

This is the heart of the case study — not a clean linear success, but thinking
catching its own errors.

**Self-correction 1 — the inverted hypothesis.** It came in two beats. First a
smaller claim fell: an earlier "readiness shows zero pool-wait" assertion was
contradicted by direct `pg_stat_activity` measurement — the read path saturates the
15 connections too. Then the main hypothesis fell: the sweep showed *neither*
deterministic path is pool-bound. The going-in belief ("the write path is
pool-bound, 15 is the ceiling") was not refined — it was inverted. Had I trusted
the reasoning and skipped the sweep, I would have "fixed" the wrong thing:
enlarging a pool that was never the constraint.

**Self-correction 2 — the loopback illusion.** After the fix (next section), the
local number jumped from 218 to 435 req/s at pool=15. The tempting headline writes
itself: *"2× faster."* It is false. On loopback there is no network, so releasing
the connection simply lets the 50 ms sleep run concurrently instead of serializing
on 15 connections — a property of the sandbox, not a production gain. Catching that
illusion *before* publishing the number is the second correction: the real result
is not a local speed-up, it is a **change of binding constraint** that transfers.

## 5. The §7 fix — reduce hold-time, never grow the pool

The optimization was pre-committed as a *principle* before the data existed, for
one reason: transferability. Production (Supabase Nano) is capped at 15 server-side
connections, so "grow the pool" is unavailable exactly where it would matter. The
only fix that transfers is one that shortens how long each request holds a
connection.

Concretely, on the explanation path: **move the model call outside `session_scope`.**
`explain_gap` no longer borrows the caller's session. It owns two short
transactions — one to read the cache, one for the verified `put` — with the
external call sitting **between** them, holding no pooled connection at all. The
caller's tenant-read transaction is closed before the explainer is even
constructed.

For the record, this is *not* the kind of fix one reaches for reflexively. It is
not an outbox, not a dead-letter queue, not retry jitter, not a bigger pool. It is
a connection-lifecycle change: hold the scarce resource for less wall-clock time.

## 6. Before / after — the A/B freeze

The before/after is only trustworthy if exactly one thing changed between the runs.
So the entire harness — the load app, the stubbed explainer text, the k6 script,
the sweep driver — was built and the BEFORE sweep run *first*. Then **only `src/`
changed** (the refactor above). The AFTER sweep ran on the byte-identical harness,
so the single variable between the two numbers is the `session_scope` connection
lifecycle.

| pool_size | BEFORE req/s (call **inside** scope) | AFTER req/s (call **outside** scope) |
|---:|---:|---:|
| 5  | 79  | 501 |
| 10 | 146 | 471 |
| 15 | **218** | **435** |
| 25 | 362 | 378 |
| **sweep response** | tracks pool **4.6×** → **pool-bound** | declines 501 → 378 → **event-loop-bound** |

BEFORE reproduced the abstract probe almost exactly (probe@15 = 223 req/s,
explanation@15 = 218) — the synthetic finding, now confirmed on the real
orchestration: throughput tracks pool size, `conn_peak == pool_size`, p95 falls as
the pool grows. AFTER, throughput **no longer tracks the pool — it declines**
(501 → 378, ~25%) as the pool grows. That decline is *stronger* evidence than a
flat line: the extra connections now add pure overhead on the single event loop
rather than capacity. The pool is not merely non-binding; it has been **removed as
the binding constraint.**

**The headline is that constraint change, not the local 218 → 435 jump.** The local
jump is the loopback illusion from §4. The transferable claim is this: in
production the database is networked and the model call is a real hold — an
estimated ~100 ms model hold-time (typical for flash-class latency, not measured on
this system) — which puts the explanation path squarely in the probe's pool-bound
regime, where the 15-connection Nano pool, *which cannot be grown*, is the real
limit. Moving the call outside `session_scope` is exactly what removes that pool as
the constraint in the place it actually binds. No projected production RPS is
claimed here, because none was measured; the defensible claim is the change of
binding resource, not a new number.

## 7. The safety invariant held

The optimization changed the connection lifecycle. It did **not** move the safety
chokepoint, and that was the non-negotiable condition.

- **Gate before put, put verified-only.** The deterministic gate still runs
  in-process immediately before `put`. Splitting one transaction into two did not
  reorder it.
- **Re-gate on the cache HIT.** The re-gate now runs on the hit text after the read
  transaction closes; it is pure (no DB), so releasing the connection first cannot
  weaken it.
- **The keystone was re-run on both paths, green.** A fresh compliance-assertion is
  gate-rejected → fallback (`gate_rejected`); a poisoned cache row is re-gated on
  HIT → rejected → fallback (`cache_regate_failed`). The poisoned text is never
  served.
- **All seven `.importlinter` contracts stayed green.** No architectural boundary
  was crossed.

The principle, stated plainly: **an optimization that weakens the safety invariant
is not an optimization.** The whole point was to get faster on the resource that
actually binds in production *without moving the safety gate one inch* — and to
prove both halves, with the sweep and with the keystone, rather than assert them.

## 8. The one universal lesson

**Performance assumptions must be measured, not reasoned from shape.** The going-in
hypothesis was coherent, specific, and wrong; the curve that "confirmed" it looked
identical to the curve that refuted it. What distinguished them was never the
shape — it was a direct measurement of the resource. The deliverable of this phase
was never a throughput trophy. It was a number with a cause, an optimization that
transfers to the one place the pool truly binds, and a safety line that did not
move while the system got faster.
