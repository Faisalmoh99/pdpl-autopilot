# ADR-0006: Control Status Decision Engine

- **Status:** Accepted
- **Date:** 2026-06-13
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0002 — Findings History Model](0002-findings-history-model.md), [ADR-0005 — Questionnaire & Evidence Input Model](0005-questionnaire-evidence-input-model.md), [Data Model](../02-data-model.md), [CLAUDE.md — core decision principle](../../CLAUDE.md)

## Context

The Phase 2 check service ships with a stub `baseline_decider` that returns `'not_assessed'` for every control. It proves the SCD Type 2 transition mechanics but decides nothing. The data model flagged the real thing as deferred: *"Control-status decision engine — what deterministic rules read `evidence` rows and yield a `status`."*

With ADR-0005 defining the input (latest yes/no answers per control, as `evidence` rows), this ADR defines the layer that turns those answers into a real `compliant` / `non_compliant` / `partial` / `not_assessed` verdict.

This is **the product's core safety line**, stated in `CLAUDE.md`:

> AI **reads / suggests / explains**. Deterministic logic **decides / scores / classifies**. A compliance decision must **never** reach the user directly from an AI output.

This ADR is where that principle becomes code.

## Decision drivers

- **Transparency over generality.** A compliance verdict must be explainable line-by-line. A rule a human can read top to bottom beats a clever generic engine whose behaviour is emergent.
- **Small surface, 3–5 controls.** The MVP needs to *prove the path works*, not cover the whole catalogue. The mechanism must be trivial to extend control-by-control, and trivial to read.
- **Preserve the proven `run_check` mechanics.** ADR-0002's SCD Type 2 close-old/open-new logic is tested and correct. Only the *decider* changes from stub to real — the transition machinery must not be touched.
- **The decider seam must stay test-injectable.** Existing tests inject a `Callable[[str], (status, rationale)]`. The real engine must slot into the same seam without breaking that contract.

## Decision

### 1. Rules are simple per-control deterministic functions in a registry

A registry maps `control_code → rule function`. Each rule function takes the tenant's answers and returns `(status, rationale)`:

```python
Rule = Callable[[dict[str, str]], tuple[str, str]]   # answers -> (status, rationale)
_RULES: dict[str, Rule] = {
    "PDPL-ART12-PRIVACY-NOTICE": _rule_privacy_notice,
    "PDPL-ART4-DSR-ACCESS":      _rule_dsr_access,
    "PDPL-ART20-BREACH-NOTIFY-72H": _rule_breach_notify,
    "PDPL-ART31-ROPA":           _rule_ropa,
}
```

Each function reads the specific `question_code`s for its control out of the answers dict and applies plain `if`/`else` logic. No shared interpreter, no expression language, no data-driven rule rows.

**Why not a declarative / data-driven rule structure** (rules-as-data in a table, an expression evaluator, a DSL): premature for 3–5 controls. A declarative engine pays off when *non-engineers* author hundreds of rules or rules change without deploys — neither is true here. Today it would add an interpreter to test and debug, and would make a verdict *harder* to explain, not easier. We can migrate to declarative rules later with no schema change if the catalogue grows; the registry boundary makes that a contained refactor. **Recorded as the future trigger:** revisit when rule count crosses ~20 or a non-engineer needs to author rules.

### 2. This layer is 100% deterministic — AI is NEVER in the decision path

No rule function calls a model, a network service, or any non-deterministic source. Given the same answers, a rule returns the same `(status, rationale)` every time. This is **the product's safety guarantee**, not an implementation detail:

- AI may later *read* a privacy policy and *suggest* an answer to a question — but that suggestion is recorded as `evidence` and a human/deterministic step owns it before it can move a verdict.
- AI may later *explain* a finding in Arabic prose — but only into `findings.ai_explanation_ar`, **after** this deterministic layer has set `findings.status` and `findings.rationale`.

The decision engine never imports the AI layer. The dependency direction is enforced by module boundaries: `services/decision.py` has no AI imports, and a verdict cannot be constructed any other way.

### 3. Status mapping (deterministic, per rule)

For a control whose questions are all `yes_no`:

| Tenant's answers to the control's questions | Status | Meaning |
|---|---|---|
| All required questions answered `yes` | `compliant` | Obligation met on the evidence given. |
| All required questions answered `no` | `non_compliant` | Obligation not met. |
| Some `yes`, some `no` | `partial` | Partially met — a real gap with a real partial. |
| One or more required questions unanswered | `not_assessed` | No evidence yet to decide from. |
| Control has **no** registered rule | `not_assessed` | Engine does not cover this control yet. |

- `'not_assessed'` keeps its data-model meaning: *no evidence / not evaluated.* It now covers two concrete cases — a control with a rule but missing answers, and a control with no rule at all. Both are honestly "we have not assessed this."
- `'unknown'` stays **reserved and unused**. Its data-model meaning is *"the engine ran and could not decide despite having input"* — a state our current yes/no rules never produce, because mixed answers are a meaningful `partial`, not an inability to decide. We do not emit `'unknown'` until a rule genuinely hits an undecidable input.
- `'not_applicable'` also stays unused for now (it needs a notion of scoping a control out for a tenant — a later concern).

### 4. The `rationale` is a deterministic explanation, NOT the AI layer

Every verdict carries a `rationale` string built from the answers that produced it — e.g. *"3 of 4 privacy-notice questions satisfied; missing: discloses retention period."* It states *what made the status what it is*, mechanically, from the inputs. It is:

- written by the rule function, never by a model;
- the value of `findings.rationale` (`NOT NULL` per the data model);
- **distinct from** `findings.ai_explanation_ar`, the future Arabic prose explanation the AI layer may add *after* verification.

### 5. The seam: a closure preserves the `run_check` call site

`run_check` today calls `chosen_decider(ctrl.code)` in its loop. To keep that loop untouched — and keep the existing test injection working — the real engine is exposed as a **decider factory**:

```python
def build_deterministic_decider(answers: dict[str, str]) -> StatusDecider:
    def _decide(control_code: str) -> tuple[str, str]:
        rule = _RULES.get(control_code)
        if rule is None:
            return ("not_assessed", "no deterministic rule registered for this control yet")
        return rule(answers)
    return _decide
```

`run_check` changes minimally: when no decider is injected, it loads the tenant's latest answers (within the same transaction, via a query defined in the decision module) and builds the deterministic decider as the default — replacing `baseline_decider`. The per-control loop, the SCD Type 2 close-old/open-new logic, the audit writes, and the idempotency dedup are **unchanged**. Tests that inject a `Callable[[str], (status, rationale)]` continue to work exactly as before.

The old `baseline_decider` is retained (not deleted) for explicit baseline runs and as the documented "decides nothing" reference, but it is no longer the default.

## Consequences

**Positive**
- The product makes its first real, defensible compliance verdict — from real input, with a deterministic rationale, with AI provably absent from the path.
- The registry makes adding a control's rule a localised, testable change.
- The seam means the engine swap is a one-line default change in `run_check`; the hard-won SCD Type 2 mechanics are untouched and stay green.

**Negative / accepted**
- **The rules are only as good as the (non-authoritative) questions.** Verdicts are correct *with respect to the seeded starter questions*, which are not legally reviewed (ADR-0005 §4). A `compliant` here means "answered the starter questions affirmatively," not "PDPL-compliant in law." This limitation is inherent to the MVP and is surfaced, not hidden.
- **Coverage is partial by design.** Only 4 controls have rules this session; the other 6 seeded controls return `not_assessed`. That is the correct, honest state — not a bug.
- **No `finding_evidence` link yet** (deferred in ADR-0005): the rationale explains the verdict, but the finding is not yet joined to the specific answer rows. Flagged, not buried.

## Open questions (deferred)

- **`'unknown'` emission.** The first rule that legitimately cannot decide on valid input will force defining when `'unknown'` is emitted vs `'partial'`. Not now.
- **Multi-evidence rules.** Today a rule reads only questionnaire answers. A real control may need to combine an answer *and* a parsed document *and* a scheduled-check result. The `dict[str, str]` answers input will generalise to a richer evidence view when document reading lands.
- **Readiness scoring.** How statuses + `controls.severity_weight` aggregate into a score is still a separate deferred ADR. This engine produces per-control statuses only; it does not score.
