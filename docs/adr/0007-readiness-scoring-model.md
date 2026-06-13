# ADR-0007: Readiness Scoring Model

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0002 — Findings History Model](0002-findings-history-model.md), [ADR-0005 — Questionnaire & Evidence Input Model](0005-questionnaire-evidence-input-model.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [Data Model](../02-data-model.md), [Product Definition](../product-definition.md), [CLAUDE.md — core decision principle](../../CLAUDE.md)

## Context

ADR-0006 turns a tenant's questionnaire answers into per-control verdicts
(`compliant` / `partial` / `non_compliant` / `not_assessed`). That is the
engine; it produces statuses, one per control. It does **not** aggregate them.

The product's headline promise (`product-definition.md`) is *"show me where I
stand on PDPL"* — a single, glanceable readiness signal plus the list of gaps
to fix. This ADR defines how the per-control statuses, weighted by
`controls.severity_weight`, aggregate into that signal.

The data model flagged this as deferred: *"Readiness scoring algorithm
(ADR-pending). How findings statuses + `controls.severity_weight` aggregate
into a single readiness score. Not designed yet."* This ADR settles it.

Like the decision engine, this layer is **100% deterministic — no AI**. It is
part of the deterministic core enforced by the architectural fitness function
(see ADR-0007 §6 below): `services/scoring.py` may not import the AI layer or
any LLM SDK, and the build fails if it does.

## Decision drivers

- **Trust over a flattering number.** `product-definition.md` makes the
  false-positive rate the single most important metric: *"false alarms break
  trust."* A scoring design that can show a reassuring number while most of the
  catalogue is unassessed is itself a false signal. The scoring model must be
  structurally incapable of that.
- **Explainable, not a black box.** A readiness number a small-business owner
  cannot decompose is worse than no number. Every output must be reconstructable
  by hand from the component counts and weights.
- **Honest about absence.** "We have not assessed this" is a first-class state
  (`not_assessed`), not a zero. The model must never quietly treat *unknown* as
  *failing* or as *passing*.
- **Total and deterministic.** Given the same current findings, the same score —
  every time. The function must be defined for every status the data model
  allows, with no silent fall-through.

## Decision

### 1. Report COVERAGE separately from SCORE — never one number

The central trap: a tenant with 2 `compliant` and 8 `not_assessed` controls.
Is that 100% (of what we looked at) or 20% (of the whole catalogue)? **Both
framings are true, and either one alone is misleading.** So we report two:

- **SCORE** — a weighted readiness indicator computed **only over the controls
  actually assessed**. It answers: *"of what we have evaluated, how ready are
  you?"*
- **COVERAGE** — the share of applicable controls that have been assessed at
  all. It answers: *"how much of the picture have we actually looked at?"*

A `score` of 100 at 20% `coverage` reads, correctly, as *"perfect on the little
we have assessed — 80% still unlooked-at,"* not *"compliant."* The two numbers
are always presented together; the score is **never** shown alone. This is the
direct structural answer to the product's false-positive / trust concern.

### 2. The SCORE is a weighted readiness indicator — NOT a compliance %

The score uses `controls.severity_weight` (a control's importance, 0–10 in the
seed) so that missing a weight-10 breach-notification obligation costs more than
missing a weight-5 records obligation. For each **assessed** control *i* with
weight *wᵢ* and a status **credit** *cᵢ*:

```
score = ( Σ wᵢ·cᵢ  /  Σ wᵢ ) × 100      over assessed controls only
```

**What the score is NOT, stated plainly and carried into the UI copy:** it is a
**readiness / maturity indicator**, not a compliance percentage and not a
fine-risk estimate. ADR-0006 already records that a `compliant` here means
"answered the non-authoritative starter questions affirmatively," not
"PDPL-compliant in law." The score inherits that limit. The output is labelled
"readiness score"; the product must never render it as "you are N% compliant."

### 3. Status → credit and denominator membership

| Status | Credit *cᵢ* | In SCORE denominator? | In COVERAGE denominator? |
|---|---|---|---|
| `compliant` | 1.0 | yes | yes |
| `partial` | **0.5** | yes | yes |
| `non_compliant` | 0.0 | yes | yes |
| `unknown` | 0.0 | yes (assessed) | yes |
| `not_assessed` | — | **no** | yes |
| `not_applicable` | — | **no** | **no** |

- **`partial` counts as 0.5 — half credit, not zero and not full.** A compliance
  product's cardinal sin is over-stating readiness, so partial **never** reaches
  full credit. But scoring it as zero would erase the engine's deliberate
  distinction between *partial* and *non_compliant* and would under-credit a
  tenant who has done half the work. Half credit is the trust-preserving middle:
  it cannot inflate to "done," and — critically — **a `partial` control always
  also appears as an open gap in the gap report (§5)**, so the user is never
  reassured by it regardless of its 0.5 contribution to the number.

- **`not_assessed` is excluded from the SCORE denominator, not scored as zero.**
  You cannot score what you have not assessed. Counting it as zero would be a
  false alarm — punishing a tenant for not having answered yet. Its effect shows
  up honestly in COVERAGE instead.

- **`unknown` is assessed and scores 0.0.** Its data-model meaning is *"the
  engine ran and could not decide despite having input"* — we **did** look, so it
  counts toward coverage; but it earned no readiness, so it scores zero and is
  surfaced in the gap report as needing attention. (The current engine never
  emits `unknown` — ADR-0006 §3 — but the scoring model defines its treatment now
  so the first rule that produces it has a defined home.)

- **`not_applicable` is excluded from every denominator.** A control scoped out
  for a tenant must not move the score or the coverage in either direction.
  (Also unused today; defined now for the same reason.)

### 4. The zero case: no honest number → `score = None`

If a tenant has **no assessed controls** (everything `not_assessed` /
`not_applicable`), there is no defensible score:

- `0` would read as *"you failed everything"* — false; we have not assessed
  anything.
- `100` would read as *"you passed everything"* — false for the same reason.

So the score is **`None`** (absent), with `coverage = 0`. The output is
explicitly "not enough assessed to score yet," never a misleading number. The
implementation also guards `Σ wᵢ = 0` defensively so it can never divide by zero,
even though `severity_weight` is `CHECK > 0` in the schema.

### 5. The gap report: every control that is not "done", worst first

A read over the tenant's **current** findings (`valid_to IS NULL`) joined to the
active controls. A control is a **gap** if its status is `non_compliant`,
`partial`, `unknown`, or `not_assessed` (surfaced as *"not yet assessed"*).
`compliant` controls are not gaps; `not_applicable` controls are out of scope.
Each gap carries its **status**, its **deterministic rationale** (from
ADR-0006, never an AI explanation), and its **severity_weight**. Gaps are
ordered by `severity_weight` **DESC**, ties broken by `control_code` for a
stable, deterministic order.

### 6. Output shape — explainable, not a grade

The score is a small, fully decomposable record, never a letter grade:

```
ReadinessScore(
    score:               float | None,   # weighted readiness over assessed, 0–100
    coverage:            float,           # % of applicable controls assessed, 0–100
    counts:              {status: int},   # every status, over applicable controls
    weighted_achieved:   float,           # Σ wᵢ·cᵢ  over assessed controls
    weighted_assessed:   float,           # Σ wᵢ     over assessed controls
    applicable_controls: int,             # controls in scope (excl. not_applicable)
    assessed_controls:   int,             # applicable controls with a real verdict
)
```

Anyone can recompute the score from `weighted_achieved / weighted_assessed` and
the coverage from `assessed_controls / applicable_controls`. Nothing is hidden.

### 7. Read source — active controls LEFT JOIN current findings

The score and the gap report read **active controls LEFT JOIN the tenant's
current findings** (`valid_to IS NULL`). A control with **no** current finding
row — a tenant that has never run a check, or a control added after the last run
— is treated as `not_assessed`, not omitted. This makes the denominator the full
applicable catalogue rather than "whatever findings happen to exist," so coverage
is honest by construction.

## Consequences

**Positive**
- The product makes its headline output — a readiness signal + a gap list — from
  real per-control verdicts, deterministically, with AI provably absent.
- The COVERAGE/SCORE split makes the false-positive-of-reassurance failure mode
  *structurally impossible*: a high score over thin coverage cannot masquerade as
  compliance.
- The scoring function is pure and total over every status, so it is exhaustively
  unit-testable without a database, and the DB read is a thin, separately tested
  wrapper.

**Negative / accepted**
- **The score is only as meaningful as the (non-authoritative) questions and
  weights.** The seed `severity_weight` values are a working approximation, not a
  legally reviewed risk model (data-model warning). A real catalogue will revise
  both. The score is a *relative readiness indicator on our current instrument*,
  surfaced as such, not an absolute compliance measure.
- **No score is persisted.** This session computes the score on read; there is no
  `scores` table and no history of scores over time. Trend lines ("you improved
  from 40 to 70 this month") are a later concern — see Open questions.
- **No audit row for a score computation.** A score is a derived, transient read,
  not a persisted state change, so it writes a structured log line + a metric but
  **no `audit_log` row**. When scores become persisted (a `scores` table or
  scheduled scoring), a `score.computed` audit event becomes appropriate — the
  event name is already reserved in the data model.

## Open questions (deferred)

- **Score history / trends.** Persisting a score per check run (a `scores` table)
  to show movement over time, and the accompanying `score.computed` audit event.
  Not now — no requirement has forced a stored score yet.
- **Per-category sub-scores.** Breaking the single readiness number down by
  `controls.category` (consent, security, breach…) so a tenant sees *where* they
  are weak. A natural follow-up once there are enough controls per category to be
  meaningful.
- **Coverage weighting.** Coverage is currently a simple control count
  (`assessed / applicable`), chosen for explainability. A weighted coverage
  (by `severity_weight`) would stop a tenant from inflating coverage by assessing
  only trivial controls. Revisit if that gaming becomes real; the count form is
  the honest default for the MVP.
- **HTTP endpoint.** A read-only `GET /tenants/{id}/readiness` (score + gaps) is a
  thin wrapper over these service functions, deferred consistent with the
  answers/checks pattern — the service function is the stable seam.
- **Weights as a reviewed risk model.** `severity_weight` becomes meaningful only
  with the SDAIA-reviewed catalogue. Until then the score is explicitly a
  relative indicator on a non-authoritative instrument.
```
