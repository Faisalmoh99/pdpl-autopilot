# 2026-06-26 — Phase 5 (Scale): load-testing stages 1+2 + the causal pool-vs-CPU proof

Deliberate load testing in a sandbox (no production traffic exists — this is a skill exercise). The
goal: a *breaking-point number derived from a rule*, plus one *transferable* optimization. The headline
outcome **inverted the going-in hypothesis**, and that inversion — reached by measurement, not
assumption — is the real result. Methodology + full data: **ADR-0014** (with its "Findings" section).

## What was built (all load tooling lives in `load/`, outside `src/` — zero contract interaction)

- **Sandbox:** local Homebrew Postgres 16 on `localhost` (ADR-0014 §2 deviation from the originally-
  specified Docker — no container runtime was installed; the load-bearing drivers, *loopback* so the
  network is not a variable + *full pool control*, are preserved). Ephemerality via `load/reset_db.py`
  (`DROP`/`CREATE DATABASE`) instead of a Docker volume.
- **Faithful seed** (`load/seed/seed_load.py`): 20 tenants, **each built through the real `run_check`
  path** (no hand-inserted findings), producing a realistic mix (per tenant: 1 non_compliant, 1
  compliant, 8 not_assessed). Proven to write as the restricted role (`current_user=pdpl_app`), not the
  loopback superuser — which matters because superuser would bypass the ADR-0003 grants.
- **k6 scripts** (`load/k6/{readiness,checks,probe}.js`) + sweep drivers (`checks_sweep.py`,
  `pool_sweep.py`) that sample `pg_stat_activity` alongside k6.
- **Pool-size knob** (`DB_POOL_SIZE` / `DB_MAX_OVERFLOW`, ADR-0014 §5): the ONLY `src/` change. Unset =
  byte-for-byte the prior behaviour (SQLAlchemy 5+10=15); verified, and `lint-imports` stays 7/7 green.
- **Hold-time probe** (`load/probe_app.py`): a load-only ASGI wrapper (real app + one probe route),
  never on main's serving path; holds a pooled connection across a pure `asyncio.sleep`.

## The pre-registered knee rule (so the number is from a rule, not the eye)

Baseline at low VU fixes `p95_base`; knee = first VU where **p95 ≥ 2×p95_base AND throughput flattens**.
Both conditions required; committed in ADR-0014 §4 before any run.

## Findings (the three-way causal contrast)

1. **Both deterministic paths saturate the pool (`conn=15` above 15 VU) but are not pool-bound.**
   readiness ceiling ~2000 req/s, checks ~1230 req/s, both zero errors, p95 climbs linearly while
   throughput plateaus. (An earlier "readiness has zero pool-wait" claim was **corrected by direct
   `pg_stat_activity` measurement** — the read path saturates the 15 connections too.)
2. **Pool-size sweep {5,10,15,25} @ VU=30 → both FLAT** (5× pool buys ~12% read / ~6.3% write): the single-worker **event
   loop / CPU** is the binding constraint on loopback, not the pool. Using the pool ≠ being limited by
   it; the sweep is what separates them.
3. **Hold-time probe (50 ms async hold) → throughput TRACKS pool size linearly (POOL-bound).** Same
   system, same `conn=15`, same one worker; the only changed variable is connection **hold-time**.
   ⇒ **Hold-time alone decides whether the pool or the event loop binds.**

## Why it matters / §7 vindicated

Loopback removes the network, which is *why* the deterministic paths are event-loop-bound here
(hold-time ≈ ms). Production's networked DB (~80–120 ms RTT) puts real hold-time in the **probe's
regime**, where the 15-connection Nano pool binds. So the production optimization is **reduce
hold-time, not grow the pool** (ADR-0014 §7, now evidence-backed) — concretely, move the explanation
path's Gemini call **outside `session_scope`**. That fix is framed, **not built** (no optimization
before the data; the data now points at it).

## Process notes (honest)

- Two earlier sweep runs were **contaminated** (check_runs/audit_log bloat → checkpoint/autovacuum
  stalls inside 30 s windows). Fixed by reset+reseed per pool and taking the **max** of repeats
  (contamination only depresses throughput). Not smoothed over — re-run clean before trusting.
- `zsh` does not word-split unquoted vars (a sweep loop ran once with the whole VU list); and
  `set -a && source .env.load` truncates `LOAD_DB_ADMIN_DSN` at its first space — `reset_db.py` now
  loads `.env.load` itself to be immune.
- A guard check cannot gate the action it shares a shell block with: a `grep` for residual `<…%` bounds
  ran immediately before `git commit` in the same block, flagged a leftover, and the commit ran anyway
  (fixed by amend). Same enforce-by-mechanism discipline as the rest of the project — a check must be
  able to STOP the step, not merely print before it.

## Lessons

- **توقّعت، قِست، صحّحت — مرّتين.** دخلت الجلسة بفرضية واضحة: مسار الكتابة (checks) pool-bound، لأن الـ transaction الطويل يحجز الـ connection فيشبع الـ pool عند 15 قبل القراءة. القياس المباشر قلب الفرضية مرّتين: أولاً صحّحت ادّعاء "readiness صفر pool-wait" بعد ما قست pg_stat_activity فعلياً (طلع conn=15 يشبع على القراءة أيضاً)؛ ثانياً قلب الـ pool sweep الفرضية الأساسية كلها — كلا المسارين event-loop-bound، لا pool-bound. الدرس مو النتيجة، بل أن القياس المباشر غيّر فهمي مرّتين، والتمسّك بالفرضية بلا قياس كان بيوصّلني لتحسين خاطئ (تكبير pool ما ينفع).

- **الأداة تخدع، المورد لا.** منحنى k6 وحده يخدعك: شكل صعود الـ latency واستقرار الـ throughput متطابق تماماً سواء الاختناق في الـ CPU/event-loop أو في الـ pool. التوقيع واحد، المورد مختلف. الفيصل الوحيد هو القياس المباشر للمورد — pg_stat_activity للاتصالات الحيّة، وpool checkout-wait. الرسمة تقول "انكسر"؛ المورد وحده يقول "أيّ مورد". أي استنتاج عن عنق الزجاجة من k6 وحده، بلا قياس المورد، تخمين.

- **وهم الـ loopback.** بيئة localhost مخادعة: زمن حجز الـ connection فيها ~1ms، فالـ pool=15 فائض بكثير ويختفي اختناقه الحقيقي. في الإنتاج (DB عبر الشبكة، RTT ~80-120ms) يتضخّم زمن الحجز ~100×، وعندها فقط يصير الـ pool هو القيد المُلزِم. أثبتّها بالـ probe (async sleep محقون يحاكي ظرف الشبكة): هناك تتبّع الـ throughput حجم الـ pool خطّياً — pool-bound. ما تختبره على loopback ليس ما تشغّله في الإنتاج، وزمن حجز الـ connection هو المتغيّر الذي يقلب الصورة بينهما — وهو بالضبط لماذا تحسين §7 هو تقليل hold-time، لا تكبير pool (الإنتاج مسقوف 15 أصلاً).

## Status

Stages 1+2 complete with the full causal story documented. Stage 3 (the real explanation path under a
stub explainer, and the hold-time optimization with a before/after number) is the next step — the
probe already demonstrates the regime it lives in.
