# PDPL Autopilot

A PDPL readiness assistant for **small and medium Saudi businesses** — it scores a company's
compliance with the Saudi Personal Data Protection Law, flags each gap, and explains it in Arabic.

> **This is a learning product, not a shipping product.** It exists to grow CTO / advisory-level
> engineering judgment: every architectural decision is written down as an ADR with its alternatives,
> every AI claim is measured numerically, and the scale finding is a *measured cause*, not a guess.
> Intentional scope gaps (scheduler, auth, UI) are stated openly below — deciding what **not** to
> build yet is part of the exercise.

If you're evaluating the engineering, skip to **[Two stories worth 60 seconds](#two-stories-worth-60-seconds)**
or jump straight to the **[Start here reading path](#start-here-a-reading-path)**.

---

## What this is

The deterministic engine decides a control's status (`compliant` / `partial` / `non_compliant` /
`not_assessed`), scores readiness, and records an immutable audit entry. AI never makes that call —
it only turns the machine's verdict into a short Arabic explanation, and only after a deterministic
gate has checked it. The whole product is built around that one safety line.

| Layer | What actually runs |
|---|---|
| API | FastAPI (Python, async) |
| Database | PostgreSQL — managed via Supabase |
| AI | Google Gemini (`gemini-2.5-flash`), behind a port, for explanation only |

*(Containerisation and a Hetzner deployment are planned but not yet stood up — see
[Intentional scope gaps](#intentional-scope-gaps). The stack above is only what runs today.)*

---

## Two stories worth 60 seconds

These two are the point of the project — each is a case of thinking that **corrected itself by
measurement**.

### 1. The load test that inverted its own hypothesis

The going-in hypothesis was that the system's hot paths were **pool-bound**: SQLAlchemy's default
pool (5 + 10 = 15) and the Supabase Nano server-side cap (also 15) made 15 the obvious ceiling, so
latency *should* knee when concurrency hit it.

Direct measurement **inverted that.** A `pg_stat_activity` sample plus a pool-size sweep
(`pool_size ∈ {5,10,15,25}`) and a hold-time probe showed that on loopback the binding constraint is
the **single uvicorn event loop / CPU**, not the pool: growing the pool 5× bought ~6–12% throughput.
The pool was fully *used* but not the *limit* — two different things the sweep separated. k6 alone
would have deceived; the resource counter did not.

The one optimization (ADR-0014 §7) followed from that: move the Gemini call **outside
`session_scope`** so no pooled connection is held across the external call, then open a short new
transaction only for the verified write. On the real explanation miss-path, at the production
`pool=15`:

> **218 RPS (pool-bound) → 435 RPS (event-loop-bound).**

The headline isn't "faster locally" — on loopback the network is absent. The transferable result is
that the fix **removed the pool as the binding constraint**: in production, the networked ~100 ms
Gemini hold-time is exactly the regime where the 15-connection Nano pool (which *cannot* be grown)
becomes the real limit — and releasing the connection across the call is what removes it.

→ [ADR-0014 — Load-Testing Methodology](docs/adr/0014-load-testing-methodology.md) ·
[build-log: the §7 hold-time fix](build-log/2026-06-27-phase5-load-testing-stage3-explanation-holdtime-fix.md)

### 2. The same-model A/B that caught model drift

While re-rating a v2 prompt, the eval surfaced something the prompt change didn't cause: under a
**byte-identical v1 prompt**, the `ropa-non_compliant` case went from **548 → 907 characters** between
two measurement dates, stable across two runs each. At 907 it crosses the gate's **800-character**
bound and is rejected to the safe fallback. With no prompt change between the runs, the cause is the
model: the `gemini-2.5-flash` alias was re-pointed to a newer snapshot upstream.

**Honest limit (this is the maturity signal, not a weakness):** this is *behaviourally* evidenced,
not provable from the artifact — the eval run records only the **alias**, not a dated model snapshot.
The admission is stated plainly because pretending to a provenance we don't have would be the
opposite of the discipline this project is practising.

The fix was methodological: re-run v1 on the *current* model and compare **v1-now vs v2-now**, so the
prompt is the only variable and upstream drift can't masquerade as a prompt win.

→ [build-log: v2 prompt + the drift finding](build-log/2026-06-27-v2-prompt-not-assessed-neutral-framing.md) ·
[ADR-0013 — Prompt-Version Governance](docs/adr/0013-prompt-version-governance.md)

---

## What the engineering proves

Six domains, each with evidence you can open and check.

| Domain | What it proves | Evidence |
|---|---|---|
| **System Design** | 14 ADRs, each with real alternatives and trade-offs; multi-tenant relational schema with application-level tenant scoping; PostgreSQL chosen over Firestore on the domain's relational shape | [ADR-0001](docs/adr/0001-database-choice.md) · [ADR-0002](docs/adr/0002-findings-history-model.md) · [data model](docs/02-data-model.md) |
| **AI Product** | The AI-vs-deterministic safety line; a reusable eval harness measuring quality numerically; a surgical v2 prompt iteration | [ADR-0009](docs/adr/0009-ai-gap-explanation-layer.md) · [ADR-0010](docs/adr/0010-ai-explanation-eval-methodology.md) · [ADR-0013](docs/adr/0013-prompt-version-governance.md) |
| **Observability** | structlog JSON logging, one correlation ID threaded end-to-end (request → DB → audit row → response), validated under load via `pg_stat_activity` | [ADR-0004](docs/adr/0004-application-foundation-and-observability.md) · [ADR-0014](docs/adr/0014-load-testing-methodology.md) |
| **Reliability** | Transactional outbox so an alert is never lost on a crash; a failure path (DLQ); retry with full-jitter backoff and an idempotency key; an HMAC-signed webhook | [ADR-0008](docs/adr/0008-reliable-alerting-transactional-outbox.md) |
| **Scale** | The hypothesis inversion — 218 RPS pool-bound → 435 RPS event-loop-bound — derived from a pre-registered knee rule, not an eyeballed bend | [ADR-0014](docs/adr/0014-load-testing-methodology.md) |
| **Architectural guard** | import-linter contracts mechanically forbid the deterministic core from importing the AI layer (or any LLM SDK), enforced in the test suite; the gate-before-put invariant holds on every user-facing string | [.importlinter](.importlinter) · [test_architecture.py](tests/test_architecture.py) · [ADR-0011](docs/adr/0011-runtime-explanation-orchestration.md) |

---

## The safety line

The core principle (`CLAUDE.md`): **AI reads / suggests / explains; deterministic logic decides /
scores / classifies.** A compliance decision must never reach the user straight from an AI output.

```
deterministic engine  →  one GeminiExplainer    →  deterministic gate      →  user sees
(decides the status)     (drafts Arabic text)       (verify_explanation)       verified text
                                                          │ on failure
                                                          ▼
                                                  deterministic fallback message
```

There is exactly **one** explainer — no multi-agent system, no orchestration framework. When the
gate rejects an AI output (an unsafe compliance assertion, an over-long string, a poisoned cache
row), the user is served a plain **deterministic fallback message**. Safety is a property of the
*mechanism*, not of any eval score.

→ [ADR-0009 — AI Gap-Explanation Layer](docs/adr/0009-ai-gap-explanation-layer.md) ·
[ADR-0011 — Runtime Explanation Orchestration](docs/adr/0011-runtime-explanation-orchestration.md)

### How the v2 prompt iteration was measured

The eval is the measurement instrument; each mean is reported **by its surface and never mixed**.
Human quality scores (0–5), same 14-case golden set, same-model A/B:

| Surface | v1-now | v2-now |
|---|---|---|
| five rated `not_assessed` (the A/B subject) | 4.40 | **4.75** |
| seven `not_assessed` (all v2-affected) | — | 4.82 |
| all fourteen | — | 4.84 |
| `gate_pass_rate`, the five `not_assessed` (our change) | 1.00 | 1.00 |

The v2 change targeted only the `not_assessed` framing; the five cases it touched pass the gate
**1.00 in every run**. The *overall* gate_pass_rate is **0.93 in both v1-now and v2-now** — a single
`ropa-non_compliant` rejection where the model emitted 907 characters (> the 800-char bound). That is
the **gate working**, an inherited v1 weakness *outside* the v2 change, not a regression.

---

## Intentional scope gaps

Stated as judgment, not apology. The MVP effort went into the decision core, the AI safety line,
reliability, and one measured scale finding — proving *that* core was the right risk to spend on
first. Deliberately deferred:

- **Scheduler / continuous monitoring** — the product's eventual "warn before a fine" loop. Deferred
  until the decision engine and alert durability it depends on are proven (the outbox is built; the
  scheduler that drives it is not).
- **Authentication** — there are zero real users and zero production traffic; standing up auth before
  the core is validated would be effort on the wrong risk.
- **UI** — the product is exercised through its HTTP API and tests; a front end is out of MVP scope.
- **Containerisation / Hetzner deployment** — planned in `CLAUDE.md`, not yet stood up.

→ [product definition — MVP scope](docs/product-definition.md) ·
[ADR-0014 — explicitly out-of-scope list](docs/adr/0014-load-testing-methodology.md)

---

## Start here: a reading path

Short on time? Read these four, in order:

1. **[ADR-0014 — Load-Testing Methodology](docs/adr/0014-load-testing-methodology.md)** — the
   hypothesis inversion; how the breaking point was found by a pre-registered rule and a causal sweep.
2. **[ADR-0009 — AI Gap-Explanation Layer](docs/adr/0009-ai-gap-explanation-layer.md)** — the
   AI-vs-deterministic safety line, the load-bearing architectural decision.
3. **[ADR-0001 — Database Choice](docs/adr/0001-database-choice.md)** — a decision owned with real
   alternatives (PostgreSQL vs Firestore).
4. **[build-log — v2 prompt + the drift finding](build-log/2026-06-27-v2-prompt-not-assessed-neutral-framing.md)** —
   AI-PM judgment: catching model drift with a same-model A/B.

---

## Repository structure

```
docs/
  adr/                  – 14 architecture decision records (the design trail)
  02-data-model.md      – the relational data model
  product-definition.md – scope, target user, success metrics
build-log/              – a note after every working session
src/pdpl/               – application source (services, ai, verification, db, api, workers, eval)
tests/                  – deterministic unit/architecture tests
load/                   – k6 + load-only harnesses (outside src/, never imported by the app)
eval-runs/              – saved eval artifacts (the rated golden-set runs)
```

## Running the tests

```bash
source load/.env.load
pytest
```

Expect **222 passed / 8 failed**. The 8 failures are **environmental, not broken logic**: the outbox
tests require a live Supabase connection (`tests/conftest.py`) and fail with a connection error when
one isn't configured. They pass on the same `main` against a live database.
