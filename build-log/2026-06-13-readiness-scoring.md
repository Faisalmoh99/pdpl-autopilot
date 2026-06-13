# 2026-06-13 — Readiness score, gap report, and the AI/deterministic boundary made mechanical

Two things landed. First, the boundary the last session flagged as "enforced by
convention, not by a failing build" is now a failing build: an architectural
fitness function bans the deterministic core from importing the AI layer or any
LLM SDK. Second, the per-control verdicts from ADR-0006 now aggregate into the
product's headline output — a readiness score (reported alongside coverage) and
a gap report — 100% deterministically, AI provably absent.

## What landed

- **Architectural fitness function** (`.importlinter`, `tests/test_architecture.py`).
  An import-linter `forbidden` contract: the deterministic core
  (`services/decision`, `services/checks`, `services/scoring`) may not import —
  directly or transitively — the reserved AI namespace `pdpl.ai` or any LLM SDK
  (`anthropic`, `openai`, `google`, `vertexai`, `cohere`, `litellm`,
  `langchain`). grimp analyses imports statically, so an SDK is caught even when
  it is not installed. A pytest test runs `lint-imports` and asserts a clean
  exit, so a violation FAILS the suite — the same gate every other invariant
  uses. Proven both ways: contract `KEPT` clean, and injecting a fake
  `import anthropic` into `scoring.py` flips it to `BROKEN` with the offending
  chain printed.
- **ADR-0007 — Readiness scoring model** (`docs/adr/0007-readiness-scoring-model.md`).
  Weighted readiness **score reported separately from coverage**; `partial`=0.5,
  `unknown`=assessed/0.0, `not_assessed` excluded from the score (counts only
  against coverage), `not_applicable` excluded entirely; no assessed controls →
  `score = None`. Read as active controls LEFT JOIN current findings. Gap report
  lists every non-done control ordered by severity. The output is labelled a
  *readiness/maturity indicator*, never a compliance % or fine-risk number.
- **Scoring service** (`src/pdpl/services/scoring.py`). A **pure** core
  (`compute_score`, `build_gap_report`) that is total over every status and needs
  no database, plus thin DB-reading wrappers (`score_tenant`, `gap_report`) over
  the LEFT-JOIN read. Part of the deterministic core; imports nothing from any AI
  layer. Computes on read, persists nothing, writes a structured log + metric but
  no `audit_log` row.
- **Tests** (`tests/test_scoring.py`), 20 new. Pure unit tests drive every status
  exhaustively (including `unknown` / `not_applicable`, which the live engine
  never emits yet) — proving partial=0.5, unknown scores 0, not_assessed excluded
  from the score, not_applicable excluded entirely, score=None when nothing
  assessed, determinism, and that an unrecognised status raises. Integration tests
  against the live Supabase project prove the worked example end-to-end from real
  `record_answers → run_check` findings (score 44.74, coverage 30.0), gap-report
  ordering + filtering, and `TenantNotFound` on an inactive tenant. **Full suite:
  47 passing.**
- **Data-model doc** updated: readiness scoring marked DECIDED → ADR-0007.

## Decisions worth remembering

- **import-linter over a hand-rolled pytest import-graph test.** The declarative
  contract is the teaching artifact — it *is* the architectural-fitness-function
  pattern, the same shift from discipline to mechanism that role grants gave the
  audit log. We still gate it through `pytest` (a test shells out to
  `lint-imports`) because there is no CI yet, so the existing test bar enforces
  it. Two gotchas paid down: `include_external_packages = True` is required to
  name third-party SDKs, and external forbidden modules must be top-level
  packages — `google.genai` is rejected, so we forbid the whole `google`
  namespace (we use none of it, so this is strictly safer).
- **The deterministic core is three modules, not one.** Guarding only
  `decision.py` would have been theatre: `checks.py` orchestrates the verdict and
  `scoring.py` aggregates it. All three are banned from AI. `pdpl.ai` is reserved
  now so Phase 4's AI layer is forbidden from the decision path from day one.
- **COVERAGE separate from SCORE is the structural answer to the trust metric.**
  `product-definition.md` makes false reassurance the cardinal sin. A single
  number (2 compliant + 8 not_assessed → "100%?" "20%?") can lie; two numbers
  cannot. A score of 100 at 20% coverage reads as "perfect on the little we
  looked at," not "compliant." The model is *structurally* incapable of
  masquerading thin coverage as compliance.
- **`partial` = 0.5, and it is *also always an open gap*.** Half credit can never
  inflate to "done," and because the gap report lists every partial regardless of
  its score contribution, the 0.5 cannot reassure the user into thinking the
  control is handled. That dual treatment is what makes half-credit safe rather
  than flattering.
- **`score = None` when nothing is assessed — not 0, not 100.** 0 reads as "you
  failed everything," 100 as "you passed everything"; both are false before any
  assessment. Absence of a number is the honest output, paired with coverage 0.
- **Pure core + thin DB wrapper.** Making `compute_score` / `build_gap_report`
  pure functions over `(status, weight)` rows let the exhaustive status tests run
  with no database and made the function trivially total — the DB read became a
  small, separately tested seam.
- **No audit row for a transient score (flagged, agreed).** A score is a derived
  read, not persisted state; an `audit_log` row per report refresh would bloat the
  immutable log for no state change. Log + metric only. The reserved
  `score.computed` event waits for a persisted `scores` table / scheduled scoring.

## What's explicitly deferred (and why)

- **HTTP endpoint** `GET /tenants/{id}/readiness` — a thin wrapper over the two
  service functions, deferred consistent with the answers/checks pattern. The
  service function is the stable seam.
- **Score persistence / trends** — no `scores` table, so no history of the score
  over time and no `score.computed` audit event yet. No requirement has forced a
  stored score.
- **Per-category sub-scores** — breaking the number down by `controls.category`.
  A natural follow-up once categories have enough controls to be meaningful.
- **Weighted coverage** — coverage is a control count for explainability; a
  severity-weighted coverage would stop gaming by assessing only trivial
  controls. Revisit if that becomes real.
- **`finding_evidence` linking** — still deferred from ADR-0005/0006; unchanged.

## Definition-of-Done check

- [x] Design/ADR — ADR-0007, Accepted.
- [x] Logging + metric — `scoring.computed` / `scoring.gap_report` structured logs
      + `scoring.computed` counter. No audit row, by design (see above).
- [x] Error handling — `TenantNotFound` on inactive/unknown tenant; an
      unrecognised status raises rather than scoring silently.
- [x] Tests — 20 new, 47 total passing against real Supabase. The pure-core tests
      cover statuses the live engine cannot yet produce, so the function is proven
      total.
- [x] No secrets in code.
- [x] Build-log entry — this file.
- [x] Bonus invariant — the AI/deterministic boundary is now a failing build, not
      a convention.

## Honest pieces

- **The score is only as meaningful as the non-authoritative questions and
  weights.** The seed `severity_weight` values are a working approximation, not a
  reviewed risk model. The score is a *relative readiness indicator on our current
  instrument*, surfaced as such — not an absolute compliance measure. A future me
  must still not show this to a customer as "you are N% compliant."
- **The fitness function guards imports, not behaviour.** It proves the decision
  path does not *import* AI; it cannot prove a future dev won't pass an
  AI-produced value in through some other seam. The architecture makes the safe
  path the natural one (a verdict has no AI construction path), but the guarantee
  is "no AI import in the core," precisely.
- Tests still share the live Supabase project and leave data behind (unchanged
  trade-off). The integration scoring tests create their own tenants.

## Lessons (Faisal)

Converting the AI/deterministic line from a sentence in CLAUDE.md into a failing
build is the highest-leverage thing this session did — bigger than the score
itself. The pattern is the one worth internalising: a principle you can only
*state* is a principle you will eventually break; a principle a tool *checks* is
an invariant. import-linter is the standard name for "express an architectural
rule as data and fail the build on violation" (a fitness function). The same move
the DB role grants made for the audit log, now made for the decision boundary.
The other reusable lesson: a scoring rule that can produce one reassuring number
is a trust bug waiting to happen — splitting SCORE from COVERAGE made the
dangerous output unrepresentable, which is always better than documenting that it
is dangerous.
