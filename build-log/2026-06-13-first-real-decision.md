# 2026-06-13 — First real deterministic decision

The product did its core job for the first time: real input about a tenant (yes/no questionnaire answers) turned into a real `compliant` / `non_compliant` / `partial` finding through a 100% deterministic engine. No AI in the path. The check pipeline existed before; until today its decider was a stub that decided nothing.

## What landed

- **ADR-0005 — Questionnaire & evidence input model** (`docs/adr/0005-questionnaire-evidence-input-model.md`). Questions live in a seeded `questions` table (questions-as-data). A tenant's answers are rows in the existing `evidence` table (`type='questionnaire_answer'`), one row per answer, append-only — changing an answer inserts a new row; the engine reads the latest per question. Non-authoritative starter set. Document upload stays deferred.
- **ADR-0006 — Control status decision engine** (`docs/adr/0006-control-status-decision-engine.md`). Simple per-control deterministic functions in a registry (`control_code → fn(answers) → (status, rationale)`). 100% deterministic; AI never in the decision path — stated as the product's safety guarantee. The `rationale` is a deterministic explanation, distinct from the future `ai_explanation_ar`. A declarative rule engine is premature for 4 controls; trigger to revisit recorded (≈20 rules, or a non-engineer authoring rules).
- **Migration 0004** (`migrations/versions/0004_seed_questions.py`). Creates `questions` (FK → controls, `code` UNIQUE with `Q-%` check, `answer_type` constrained to `yes_no`); grants `pdpl_app` DML; seeds **9 non-authoritative starter questions** across 4 controls (privacy notice ×4, DSR access ×2, 72h breach ×2, ROPA ×1); writes its own `audit_log` row with `non_authoritative: true`. Control IDs resolved by `code` subquery, so the seed is independent of the controls' v4 UUIDs.
- **`record_answers` service** (`src/pdpl/services/answers.py`). Writes answers as evidence in one transaction; validates tenant-active, every question code known, every answer in `{yes,no}` BEFORE any write — a bad set writes nothing. Append-only; no overwrite path. Emits `evidence.recorded` audit rows. No HTTP route this session (deferred; the service is the stable seam).
- **Decision engine** (`src/pdpl/services/decision.py`). `load_tenant_answers` reads the latest answer per question (`DISTINCT ON (question_code) … ORDER BY collected_at DESC`). `build_deterministic_decider(answers)` returns a closure that slots into `run_check`'s existing `decider(code)` seam. Imports nothing from any AI layer.
- **`run_check` wired to the real engine** (`src/pdpl/services/checks.py`). When no decider is injected, it loads the tenant's answers inside the transaction and builds the deterministic decider as the default — replacing `baseline_decider`. The SCD Type 2 close-old/open-new logic, audit writes, and dedup are untouched. `baseline_decider` retained for explicit baseline runs.
- **Tests** (`tests/test_decision_engine.py`), 8 new, all green (full suite **26 passing** against the real Supabase project): compliant / non_compliant / partial / not_assessed each from real answers via the real default engine; the payoff (change an answer → re-run → SCD Type 2 transition from a real cause, atomic handoff, evidence append proven); correlation_id threads `record_answers` + `run_check` + findings; `record_answers` rejects unknown question / bad answer and writes nothing.
- **Data-model doc** (`docs/02-data-model.md`) updated: ADR-0005 and ADR-0006 marked DECIDED; new deferred item added for `finding_evidence` population.

## Decisions worth remembering

- **An answer is evidence — no new entity.** The schema already declared `evidence.type='questionnaire_answer'`. Adding a parallel `answers` table would have duplicated `tenant_id`, `collected_at`, the audit hooks, and the `finding_evidence` join for no gain. Respecting the schema beat inventing structure.
- **Append-and-read-latest is the *less code* path, not over-engineering.** `evidence` has no update path; "insert a new row, read the latest" needs zero new mutation machinery and gives history + audit for free. A versioned answers table with explicit version numbers *would* have been over-engineering. The historically honest option was also the cheapest.
- **The decider seam was preserved with a closure.** `build_deterministic_decider(answers)` returns a `Callable[[str], (status, rationale)]`, so `run_check`'s per-control loop, SCD Type 2 logic, and dedup did not change. Swapping the stub for the real engine was a one-line default change inside the transaction. The hard-won mechanics stayed green.
- **`not_assessed` now covers two honest cases.** A control with a rule but missing answers, and a control with no rule at all, both resolve to `not_assessed` — both are truthfully "we have not assessed this." `unknown` and `not_applicable` stay reserved and unused; mixed answers are a meaningful `partial`, not an inability to decide.
- **Validation is deterministic and up front.** `record_answers` checks tenant, codes, and answer values before writing, inside one transaction — a bad submission rolls back wholly. No partial writes.

## What's explicitly deferred (and why)

- **`finding_evidence` population (ADR-pending).** The verdict's `rationale` explains *what* decided it; it does not yet link the finding to the specific answer rows it came from. That touches `run_check`'s write path (not just the decider), so it was kept out of "only the decider gets real." Flagged, not buried.
- **HTTP route for recording answers.** A thin `POST /tenants/{id}/answers` mirroring the checks route — deferred to keep this session's surface small. The service function is the seam; the route is a cheap later addition.
- **Document upload / AI reading.** `evidence.type='document_upload'` and the parse path remain a separate future ADR.
- **Readiness scoring.** Still ADR-pending; the engine produces per-control statuses only, it does not score.
- **Architectural fitness test banning AI imports in `decision.py`.** Logged as the next guardrail — see Lessons below.

## Definition-of-Done check

- [x] Design/ADR — ADR-0005 and ADR-0006, both Accepted.
- [x] Logging + correlation ID — `evidence.recorded`, `check_run.*`, `finding.*` audit rows all carry the correlation_id; test asserts the trace across answers + check.
- [x] Error handling — `TenantNotFound` / `UnknownQuestion` / `InvalidAnswer` fail loud and roll back; nothing written on a bad answer set.
- [x] Tests — 8 new, 26 total passing against real Supabase. The payoff (answer-change → transition) is the load-bearing one.
- [x] No secrets in code.
- [x] Build-log entry — this file.

## Honest pieces

- **A `compliant` here means "answered the starter questions affirmatively," not "PDPL-compliant in law."** Verdicts are only as good as the non-authoritative seeded questions. The ADRs, the migration, and the audit row all say so — a future me must still not show this to a customer.
- **The safety line is enforced by design + convention today, not by a failing build.** See the lesson below — this is the real caveat, logged as the next guardrail.
- Tests still share the live Supabase project and leave data behind (same trade-off as prior sessions). Append-only history can't be cleaned; the trigger to add ephemeral test DBs is unchanged.

## Lessons (Faisal)

The real win was enforcing the AI-vs-deterministic line in the
architecture, not in verbal recommendations — services/decision.py imports nothing from
the AI layer and a verdict has no other construction path, so the safe path is the natural
one. Honest caveat: today this is enforced by design + convention (and by the AI layer not
existing yet), NOT by a failing build — Python won't reject an import a future dev adds.
Making "the build fails if AI touches the decision path" literally true needs an
architectural fitness test / import-linter in CI that bans AI imports in the decision
module — the same shift from discipline to mechanism that role grants gave the audit log.
Logged as the next guardrail.
