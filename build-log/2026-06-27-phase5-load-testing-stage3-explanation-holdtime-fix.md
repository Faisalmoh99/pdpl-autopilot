# 2026-06-27 — Phase 5 (Scale): stage 3 — the §7 hold-time fix on the REAL explanation path

The final seam that **closes Phase 5**. Stages 1+2 measured the deterministic paths and proved
(via a `asyncio.sleep` probe) that connection **hold-time** alone decides whether the pool or the
event loop binds — *abstractly*. Stage 3 reproduces that on the **real** explanation orchestration
(cache + gate + put), applies the one ADR-0014 §7 optimization (move the external call outside
`session_scope`), and measures the before/after. Methodology + full data: **ADR-0014 Findings §5–§8**.

## The discipline that makes the number valid: the A/B freeze

The before/after is only meaningful if **one** thing changed between the two runs. So: build the
ENTIRE harness first, run BEFORE, then touch ONLY `src/` (the refactor), then run AFTER on the
**byte-identical** harness. Anything changing in the stub/k6/route/seed/sweep between runs would
confound the headline.

## What was built (all in `load/`, off main's serving path — zero contract interaction)

- **`load/explain_app.py`** — a load-only ASGI app (mirrors `probe_app.py`): the real
  `explain_tenant_gap` orchestration with two load-only substitutions — a `LatencyStubExplainer`
  (`await asyncio.sleep(0.05)` → known-good Arabic, never the real Gemini API) and a per-request
  `uuid4().hex` `prompt_version` forcing a cache **MISS** every request (so the stub call, and the
  connection hold across it, actually happens). Both seams already existed on `explain_tenant_gap`
  for the C4b tests; main's `PROMPT_VERSION` (ADR-0013) is untouched.
- **`load/check_gate.py`** — proves the stub text **passes the gate** under the seeded tenant's real
  `GapContext` (all 4 checks PASS) BEFORE the sweep, so every request exercises the FULL miss path
  (call → gate → put), not call-only (which would mean `put` never runs).
- **`load/k6/explanations.js`** + an `explain` mode in **`load/pool_sweep.py`** — the same VU=30 pool
  sweep {5,10,15,25} the deterministic paths used, with reset+reseed per pool.

## The headline (ADR-0014 Findings §6)

| pool_size | BEFORE req/s (call inside scope) | AFTER req/s (call outside scope) |
|---:|---:|---:|
| 5  | 79  | 501 |
| 10 | 146 | 471 |
| 15 | **218** | **435** |
| 25 | 362 | 378 |
| sweep response | TRACKS pool (4.6×) → POOL-BOUND | does NOT track pool — DECLINES 501→378 → event-loop-BOUND |

BEFORE reproduces the probe almost exactly (probe@15 = 223, explanation@15 = 218) — the abstract
finding, now on the real path. AFTER, throughput does **not** track the pool — it **DECLINES**
(501→378, ~25% drop) as the pool grows. That decline is *stronger* evidence than a flat line: the
extra connections add pure **overhead** on the single event loop, not throughput — so the pool is not
merely non-binding, it is **removed as the binding constraint**. (Genuine, not an ordering/warm-up
artefact: the sweep resets+reseeds and starts a fresh, warmed app before **each** pool level.)

**The headline is the constraint change, NOT the local speed-up.** The local 218→435 jump is a
loopback artefact (no network, so releasing the connection lets the 50 ms run concurrently instead of
serializing on 15 connections) — the same illusion as stages 1+2. The transferable result: in
production the networked ~100 ms Gemini hold-time puts the path in the probe's pool-bound regime,
where the 15-connection Nano pool (un-growable) *is* the constraint — exactly what this fix removes.

## The refactor — connection lifecycle changed, safety chokepoint NOT (the non-negotiable)

`explain_gap` no longer takes the caller's session; it owns **two short transactions** (cache read,
verified put) with the external call **between** them holding no connection. `explain_tenant_gap`
closes its tenant-read transaction before constructing the explainer. Every ADR-0011 invariant
preserved and **verified green**:

- Gate-before-put, put-verified-only — the gate still runs in-process immediately before `put`; the
  order did not move.
- Re-gate-on-HIT — now runs on the hit text after the read txn closes; it is pure (no DB), so
  releasing the connection first does not weaken it.
- **Keystone re-run explicitly, both paths green:** fresh compliance-assertion → `gate_rejected`
  fallback; poisoned cache row → `cache_regate_failed` fallback. Poisoned text never served.
- **7/7 `.importlinter` contracts green** — `pdpl.explanations` importing `pdpl.db.session` is already
  permitted by Contract 7. Full suite: all explanation + architecture tests green; the only failures
  are the pre-existing `test_outbox_worker` env gap (`ConnectionRefusedError` on the local sandbox,
  also red on pristine `main`).

## Bounded gap accepted (documented, not fixed — ADR-0014 Findings §8)

The two-transaction split changes put-failure semantics: a gate-*passed* explanation may not be
cached if txn B fails (the request still returns the verified text; the next request regenerates).
Safety is unaffected — served text is always gated, and `put` is `ON CONFLICT DO NOTHING` so the
concurrent double-put was already a no-op (the race pre-existed the split). A cache-efficiency edge,
not a correctness/safety one. Out of scope for stage 3.

## Lesson

An optimization is only as good as the invariant it preserves. The whole point of stage 3 was to
make the system faster on the resource that actually binds in production **without** moving the
safety gate one inch — and to *prove* both halves with numbers (the sweep) and tests (the keystone),
not assert them. "Removed the pool as the binding constraint" is a defensible, transferable claim;
"got 2× faster locally" would have been the loopback illusion dressed up as a result.

## Lessons — Stage 3

- **فصلنا الأداء عن الأمان، وكسبنا الاثنين.** التحسين نقل نداء الـ explainer خارج
  session_scope فتحرّر الاتصال أثناء النداء (مكسب الأداء)، بينما بقي الـ gate قبل الـ put
  تماماً في موضعه (الأمان). الدرس أن الأداء والأمان concerns مستقلّان: تغيير دورة حياة
  الاتصال لا يستلزم لمس نقطة التحقّق — قدرنا نُحسّن مساراً كاملاً دون أن نمسّ الـ chokepoint.
  والأهم: لم نفترض نجاة الأمان، **أثبتناها** — الـ keystone رُكِّض صراحةً على المسارين بعد
  الـ refactor (fresh compliance-assertion مرفوض، و poisoned cache row مرفوض عبر re-gate
  على الـ HIT). تحسينٌ بلا إثبات أن الأمان نجا مقامرة، لا هندسة.

- **بعد إزالة القيد، الاتصالات الزائدة عبء لا ميزة.** قبل الإصلاح، الـ throughput يتتبّع
  حجم الـ pool خطّياً (79→362) لأن الاتصال كان مُحتجَزاً عبر النداء — pool-bound. بعد
  الإصلاح، الـ throughput لم يعد يتتبّع الـ pool، بل **انحدر** كلّما كبر (501→378): ما إن
  زال الاحتجاز كقيد، صارت الاتصالات الفائضة overhead صافياً على الـ event-loop الواحد
  (bookkeeping ومنافسة backends)، لا سعةً إضافية. الدرس أن "كبّر الـ pool" ليس حلّاً
  عامّاً — بعد تجاوز القيد الحقيقي، الزيادة تضرّ. الإصلاح الصحيح كان معمارياً (تقليل
  hold-time)، وهو وحده ما ينتقل للإنتاج حيث الـ pool=15 مسقوف.
