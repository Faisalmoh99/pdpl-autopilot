# 2026-06-15 — HTTP surface for answers + readiness; Phase 2 complete

Phase 2's deterministic core was done and tested; what it lacked was a way in
and a way to read the result. This session adds both as THIN HTTP wrappers —
`POST /tenants/{id}/answers` over `record_answers`, and `GET /tenants/{id}/readiness`
over the scoring service — and with that the Phase 2 MVP build (usable surface +
embedded observability) is complete.

## What landed

- **POST /tenants/{id}/answers** (`src/pdpl/api/answers.py`). Thin wrapper over
  `record_answers`. pydantic validates SHAPE only — a non-empty list of
  `{question_code, answer}` (each a non-empty string), with **no duplicate
  question_code** in one request (a `model_validator`, since the service takes a
  `{code: answer}` map and a duplicate would silently collapse to last-wins). The
  SERVICE owns SEMANTICS: `answer` is deliberately NOT constrained to yes/no at
  the transport layer (valid answers are answer_type-dependent; baking yes/no in
  would break the first non-yes_no question type). Exception mapping:
  `UnknownQuestion` / `InvalidAnswer` → 422, `TenantNotFound` → 404, all carrying
  the request correlation_id via the global error handler. record_answers
  validates before any write in one transaction, so a bad submission rolls back
  wholly — the route adds no partial-write path. Returns 201 with the new
  evidence ids.
- **GET /tenants/{id}/readiness** (`src/pdpl/api/readiness.py`). Thin wrapper over
  the new `readiness_report`. Read-only — it does NOT run a check. Response fields
  are deliberately named (ADR-0007): `readiness_score` (a readiness/maturity
  indicator, **null** when nothing assessed — never 0, never 100), `coverage_pct`,
  `assessed_controls`, `applicable_controls`, `counts`, and `gaps` (each with
  `control_code`, `title_en`, `title_ar`, `status`, `rationale`, `severity_weight`,
  ordered by severity DESC). **No field named `compliance_score`.** Unknown tenant
  → 404.
- **`readiness_report(tenant_id)` service function** (`src/pdpl/services/scoring.py`).
  One `_load_control_statuses` read feeds both `compute_score` and
  `build_gap_report`, returning a `ReadinessReport(score, gaps)` from ONE
  consistent snapshot — so the score and the gap list can never disagree about the
  current findings (no two-read skew). The route stays a single service call;
  orchestration lives in the service, not the route.
- **`title_ar` threaded through the gap path** (`src/pdpl/services/scoring.py`).
  `GapItem` gained `title_ar`; the read SQL, `_load_control_statuses`, and
  `build_gap_report` carry both titles, so the API returns the Arabic control
  title alongside the English one.
- **Error-handler hardening** (`src/pdpl/errors.py`). The validation handler now
  wraps `exc.errors()` in `jsonable_encoder` — mirroring FastAPI's own handler.
  Without it, a custom `model_validator` raising `ValueError` lands a
  non-serialisable exception object in each error's `ctx`, and the raw
  `json.dumps` turned a 422 into a 500. Found by the duplicate-question test;
  fixed for every future validator, not just this one.
- **Routers registered** (`src/pdpl/main.py`): `answers_router`, `readiness_router`.
- **Tests** — 13 new (`tests/test_api_answers.py` ×8, `tests/test_api_readiness.py`
  ×5), full suite **60 passing** against the real Supabase project. The
  `build_gap_report` unit tests were updated for the new `title_ar` tuple shape.

## Decisions worth remembering

- **Validation has one owner: the service.** The route does SHAPE (is it a list of
  strings? duplicates?), the service does SEMANTICS (does the question exist? is
  the answer valid for its type?). Putting `Literal["yes","no"]` in pydantic was
  explicitly rejected — it would duplicate the rule and break non-yes_no types
  later. This is the cleanest expression of "route = transport, service = logic":
  the route does not re-validate domain rules, it maps the service's exceptions to
  status codes.
- **No-op resubmit appends; no dedup in the route.** Re-submitting the same answer
  writes a new evidence row (append-only, latest-wins — ADR-0005). Skipping the
  write would require a read-before-write comparison, which is logic that belongs
  in the service if ever wanted, not the route — and it is premature at MVP answer
  volumes. History stays honest (a re-attestation is itself a fact).
- **Duplicate-in-one-request (422) vs cross-time resubmission (append) are
  distinct.** The first is an ambiguous request the transport can't faithfully map
  to a `{code: answer}` map → reject. The second is a legitimate change over time →
  append. Keeping them distinct avoids conflating "malformed request" with "the
  tenant changed their mind."
- **One consistent read for the readiness report.** Calling `score_tenant` and
  `gap_report` separately from the route would have meant two DB reads, two
  tenant-active checks, and a TOCTOU window where the findings could change between
  them. `readiness_report` reads once and computes both — the route stays thin AND
  the two numbers always agree.
- **The 500-on-validator bug was a real latent defect, not a test artifact.** Any
  future `model_validator` raising `ValueError` would have hit it. `jsonable_encoder`
  is the correct fix (it is what FastAPI's default handler does), so the fix went in
  the shared handler rather than being worked around in the route.
- **Field naming carries the ADR-0007 honesty into the wire format.**
  `readiness_score` (nullable), `coverage_pct`, and no `compliance_score` — the JSON
  itself refuses to let the number read as a compliance percentage.

## Phase 2 — complete

This closes the Phase 2 MVP build: relational schema + immutable audit log
(ADR-0001/0002/0003), application foundation + embedded observability
(ADR-0004), questionnaire/evidence input (ADR-0005), the deterministic decision
engine (ADR-0006), the readiness scoring + gap report (ADR-0007), the AI/
deterministic boundary enforced as a failing build (import-linter), and now the
HTTP surface for the two user flows — record answers, read the readiness report.
The initial-questionnaire → gap-report + readiness-score flow from
`product-definition.md` is end-to-end usable over HTTP, deterministically, with
AI provably absent from the decision path.

## Definition-of-Done check

- [x] Design/ADR — no new architectural decision; both routes implement deferred
      follow-ups already settled in ADR-0005 (answers route) and ADR-0007
      (readiness route). The one discovered decision (error-handler hardening) is
      recorded above.
- [x] Logging + correlation ID — request correlation_id threads to the
      `evidence.recorded` audit rows and back out on the `X-Request-ID` response
      header (asserted); readiness GET echoes the header and logs
      `scoring.readiness_report`.
- [x] Error handling — service exceptions mapped (422/404); the shared validation
      handler hardened against non-serialisable validator errors.
- [x] Tests — 13 new, 60 total passing against real Supabase, including rollback
      on bad input and the honest zero-case over HTTP.
- [x] No secrets in code.
- [x] Build-log entry — this file.

## Honest pieces

- **The endpoints are thin by construction, but "thin" still hid a 500.** The
  duplicate-question validator surfaced a serialization bug in shared error
  handling — a reminder that even a transport-only layer needs its error paths
  tested, not just its happy path.
- **Still no authentication.** Both routes take a `tenant_id` in the path with no
  caller identity — fine for a solo-dev MVP against synthetic tenants, NOT fine
  before a real tenant's data is on the line. Auth is the pre-real-tenant trigger,
  the same family as data-residency and the erasure/right-to-be-forgotten design:
  all three must land before onboarding a real customer, none is needed to keep
  building against synthetic data. Flagged, not started.
- **The readiness number inherits ADR-0006/0007's caveat:** a `compliant` means
  "answered the non-authoritative starter questions affirmatively," not
  "PDPL-compliant in law." The wire format avoids `compliance_score` precisely so
  this is not misread.
- Tests still share the live Supabase project and leave data behind (unchanged
  trade-off); the API tests create their own tenants via `POST /tenants`.

## What's still deferred (unchanged, none started)

Authentication (pre-real-tenant trigger), the AI explanation layer
(`ai_explanation_ar`), document reading/parsing, scheduling/continuous
monitoring + alerts, score persistence/trends (a `scores` table +
`score.computed` audit event), and `finding_evidence` linking.

## Lessons (Faisal)

The session's real lesson is about where validation lives. The temptation with a
"thin" route is to validate everything at the edge with pydantic `Literal`s
because it is easy and gives nice 422s — but that quietly creates a second source
of truth that drifts from the service and, here, would have hard-coded a yes/no
assumption that the data model already designed past (`answer_type`). Drawing the
line as "route validates shape, service validates meaning" keeps the service
authoritative and the route genuinely thin. Second lesson: a transport layer is
not exempt from testing its failure paths — the only reason the 500-on-validator
bug was caught is that the duplicate-question test asserted 422 and got a 500
instead. Happy-path-only tests on "thin" code are how latent 500s ship.
