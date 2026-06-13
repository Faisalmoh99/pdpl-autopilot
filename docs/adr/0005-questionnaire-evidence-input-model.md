# ADR-0005: Questionnaire & Evidence Input Model

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0001 — Database Choice](0001-database-choice.md), [ADR-0002 — Findings History Model](0002-findings-history-model.md), [ADR-0003 — Audit-Log Immutability](0003-audit-log-immutability.md), [ADR-0006 — Control Status Decision Engine](0006-control-status-decision-engine.md), [Data Model](../02-data-model.md)

## Context

Phase 2 stood up the `check_run` → `findings` pipeline with SCD Type 2 history, but the decider was a stub returning `'not_assessed'` for every control. Before a real decision engine (ADR-0006) can decide anything, the product needs **real input about a tenant** to decide *from*.

The data model (`docs/02-data-model.md`) flagged this as a deferred ADR: *"Questionnaire & evidence input model — the schema of the initial readiness questionnaire, how its answers materialise into `evidence` rows, and how uploaded documents feed the same pipeline."*

This ADR settles the input half: where questions live, how a tenant's answers are stored, and what happens when an answer changes. Document upload / parsing stays out of scope — it is a separate AI-reading concern and a later ADR.

The MVP target is small and deliberate: **a handful of yes/no questions for 3–5 controls**, enough to drive a real `compliant` / `non_compliant` / `partial` verdict.

## Decision drivers

- **Respect the existing schema.** The `evidence` table already declares `type = 'questionnaire_answer'`, a `jsonb payload`, a `source`, and a `collected_at`. The schema was *designed* for questionnaire answers to land here. A new parallel table would contradict that design.
- **Relational and queryable over config-as-code.** The learning goal favours data that can be joined, inspected in `psql`, and reasoned about with SQL — not logic buried in application config.
- **Consistency with the audit + SCD Type 2 ethos.** The whole product treats history as sacred (immutable audit log, versioned findings, versioned controls). The input layer should not be the one place that silently overwrites the past.
- **No half-designs.** Same principle as the rest of the data model: do not bake in machinery (version columns, soft-delete) that a real requirement has not yet forced.

## Decision

### 1. Questions live in a seeded `questions` table (questions-as-data)

A new global table, one row per question, each linked to exactly one control:

```
questions
  id            uuid        PK   (app-generated UUID v7; no DEFAULT — see data-model identifier strategy)
  control_id    uuid        NOT NULL  REFERENCES controls(id) ON DELETE CASCADE
  code          text        NOT NULL UNIQUE   CHECK (code LIKE 'Q-%')
  prompt_en     text        NOT NULL
  prompt_ar     text        NOT NULL
  answer_type   text        NOT NULL DEFAULT 'yes_no'  CHECK (answer_type IN ('yes_no'))
  display_order int         NOT NULL DEFAULT 0
  created_at    timestamptz NOT NULL DEFAULT now()
  updated_at    timestamptz NOT NULL DEFAULT now()
```

- **Identity is `code`** (e.g. `Q-ART12-NOTICE-PURPOSES`), stable across re-seeds. Answers reference the question by `code`, never by `id`, so re-seeding the catalogue never orphans stored answers.
- `ON DELETE CASCADE` from `controls`: a question is meaningless without its control. (Controls are never hard-deleted in practice — they are retired via `effective_to` — so this rarely fires; it is the correct semantic regardless.)
- **`answer_type` is constrained to `'yes_no'` for now.** The MVP needs nothing else. The column exists so the constraint can be widened (`'single_choice'`, `'scale'`) without a structural change when a real need appears.

**Why not questions-as-code (app config / a Python dict):** it buries product content in the application, makes "which questions belong to control X?" a code-read instead of a `JOIN`, and cannot be inspected or audited like a table. The seed approach is consistent with how `controls` already live in the DB.

**What this table deliberately omits:**
- **No `weight` column.** Readiness scoring is a deferred ADR; adding a weight now would invite inferring a scoring design that does not exist yet (the same trap the data model warns about for `controls.severity_weight`).
- **No `effective_from` / `effective_to` versioning.** Controls are temporally versioned because SDAIA amends the *law*; questions are our *measurement instrument* for a control, keyed by stable `code`. Versioning them now is over-engineering — if a question's wording changes we re-seed under the same or a new `code`. Revisit only if we ever need to prove "which exact question text did this tenant answer in 2026?" — not an MVP requirement.

### 2. A tenant's answers are stored as rows in the existing `evidence` table — one row per answer

An answer **is** evidence for a control. It lands as:

```
evidence.type         = 'questionnaire_answer'
evidence.source       = 'questionnaire:v1'
evidence.payload      = {"question_code": "Q-ART12-NOTICE-PURPOSES", "answer": "yes"}
evidence.collected_at = <when the tenant answered>
```

- **One evidence row per individual answer**, not one row per questionnaire submission. Granularity at the answer level is what lets a future `finding_evidence` link cite *the specific answers* behind a verdict (the reverse-lookup index `idx_finding_evidence_evidence` exists for exactly this). A single bundled submission row would collapse that traceability.
- **No new `answers` table.** The schema already models "the raw substrate that feeds findings" as `evidence`. A separate table would duplicate `tenant_id`, `collected_at`, the audit hooks, and the `finding_evidence` join — for no gain.
- `answer` values for `yes_no` questions are the literals `'yes'` / `'no'`. The recording service validates the question `code` exists and the answer is in the allowed set before writing; validation is deterministic, not best-effort.

### 3. Answers are append-only; the engine reads the latest answer per question

When a tenant changes an answer, we **do not overwrite**. We `INSERT` a new `evidence` row with a newer `collected_at`. The decision engine reads, per `(tenant_id, question_code)`, the row with the **greatest `collected_at`** (ties broken by `created_at`) and ignores the rest.

This is the deliberate, honest answer to the mutability question:

- It is **less code, not more**, than building an update path: `evidence` has no `updated_at` and no update path today, so "insert a new row and read the latest" requires *zero* new mutation machinery.
- It gives **history and audit for free**, fully consistent with the SCD Type 2 findings and the immutable audit log. "What did the tenant previously answer, and when did they change it?" is answerable from `evidence` alone.
- It makes the payoff scenario natural: *change one answer → re-run the check → the control's status transitions → the old finding closes and a new one opens.* The re-run reads the new latest answer; nothing had to be mutated in place.

**Frank trade-off — is this over-engineering?** A *separate versioned answers table with explicit version numbers* would be over-engineering for the MVP. Append-and-read-latest on `evidence` is **not** — it is the path of least resistance given the schema, and it happens to be the historically honest one. We get the audit-friendly behaviour without paying for a versioning subsystem.

The cost we accept: the engine's read is "latest per question," a `DISTINCT ON (question_code) ... ORDER BY question_code, collected_at DESC` query, slightly more than a flat `SELECT`. At MVP answer volumes this is negligible, and `idx_evidence_tenant_collected` already covers the access pattern.

### 4. The seeded questions are a NON-AUTHORITATIVE starter set

Exactly as with the controls seed (migration 0003): the seeded questions have **not** been legally reviewed. They are a working approximation intended only to exercise the input → decision → finding pipeline. They MUST NOT be presented to a customer as an authoritative PDPL self-assessment. This is marked in:

- the migration's module docstring,
- a `non_authoritative: true` flag in the `audit_log` row recording the seed.

When the SDAIA-reviewed control catalogue lands (deferred ADR), the questions are replaced wholesale alongside it, not amended in place.

## Consequences

**Positive**
- The product can now accept real input with zero new top-level entities — one new table (`questions`) plus rows in an existing one (`evidence`).
- History, audit, and the future `finding_evidence` traceability all work *because* of the per-answer, append-only choice — they were not bolted on.
- The decision engine (ADR-0006) reads a clean, well-defined input: "the latest answer per question for this tenant."

**Negative / deferred**
- **No `finding_evidence` linking yet.** This session writes findings with a deterministic `rationale` but does **not** yet link the finding to the specific answer-evidence rows it was derived from. That linkage is a real, separable decision (it changes `run_check`'s write path, not just the decider) and is flagged as a follow-up — see Open questions below. *Deliberately not buried.*
- **No HTTP endpoint for recording answers this session.** Answers are recorded via a service function (`record_answers`) proven by tests. A thin `POST /tenants/{id}/answers` wrapper (mirroring the checks route) is deferred to keep this session's surface small. The service function is the stable seam; the route is a later, cheap addition.
- **Document upload is untouched.** `evidence.type = 'document_upload'` and the AI reading/extraction path remain a separate future ADR.

## Open questions (deferred)

- **`finding_evidence` population.** When `run_check`'s engine decides a control's status from a tenant's answers, should it write `finding_evidence` rows linking the finding to those answers? Yes, eventually — it completes the "why this verdict, and from which evidence" story. Deferred to its own change so this session keeps `run_check`'s write path to "only the decider gets real."
- **Answer validation depth.** Today: validate `code` exists and `answer ∈ {yes,no}`. A richer questionnaire (multi-choice, conditional questions, "not applicable" answers) will need a fuller validation model. Not now.
- **Multi-answer transactional submission semantics.** `record_answers` writes all answers in one transaction. Partial-submission / resume-later UX is a product question, not a data one, and is deferred.
