# ADR-0013: Prompt-Version Governance

- **Status:** Accepted
- **Date:** 2026-06-24
- **Deciders:** Faisal (sole engineer)
- **Related:** [ADR-0009 — AI Gap-Explanation Layer](0009-ai-gap-explanation-layer.md), [ADR-0010 — AI Explanation Eval Methodology](0010-ai-explanation-eval-methodology.md), [ADR-0011 — Runtime Explanation Orchestration](0011-runtime-explanation-orchestration.md), [CLAUDE.md — core decision principle](../../CLAUDE.md)

## Context

ADR-0009 §6 made `prompt_version` part of the content-hash cache key so that "a
prompt or model change correctly busts the cache", and left **how and when
`prompt_version` is bumped** as an explicit open question ("for now it is a
constant in `pdpl.ai`; a lightweight process can come when prompts change
often"). This ADR is that process. It is governance only — **no prompt wording
changes here** (a v2 prompt is a separate session with a comparative run and a
human re-rating).

The reason this matters is a real, latent gap surfaced while writing it. The
cache key (`pdpl.db.ai_explanations.compute_cache_key`) keys on six fields —
`prompt_version, model, control_code, status, rationale, lang`. But the prompt
the model actually receives (`pdpl.ai.prompt.build_user_prompt` +
`SYSTEM_INSTRUCTION`) embeds **much more** than those fields:

- the control's `title_ar` and `description_ar`,
- the control's `severity_weight`, via `_severity_ar` → the «الأهمية» line,
- the unsatisfied questions' Arabic text (`unsatisfied_questions_ar`),
- the `SYSTEM_INSTRUCTION`, the `_STATUS_AR` map, and the rendering logic itself.

(The template has rendered all of these since C3a; the eval's v1 rating was
produced against this surface. What C4b changed is the *source* of the control
text — it became the drift-pinned, re-seedable `pdpl.catalog.SEEDED_CONTROLS`,
which is what makes "re-seeding control text", trigger (b) below, a real and
trackable bump trigger rather than a one-off hand-edit.)

**None of that embedded surface is a cache-key field.** ADR-0009 §6 justified
omitting `control_title_ar` / `control_description_ar` / `severity_weight` /
`unsatisfied_questions_ar` from the key because they are *static functions of
`control_code`* — true **within** a `prompt_version`, but they stop being static
the moment the seed text is re-issued. So a change to the template **or** to the
seeded text the prompt embeds changes the model's output **without changing the
key** — which would silently serve a stale cached explanation, and silently
invalidate the eval's human-rated baseline (`gate_pass_rate = 1.0`,
`quality_score = 4.79`, pinned to `gap-ar-v1` via `quality_score_run`,
ADR-0010 / C3a).

## Decision drivers

- **The safety/quality guarantee must not depend on remembering a convention.**
  "Enforce by mechanism, not by convention" is the project identity (the
  `.importlinter` contracts, the catalogue drift test). A version bump is a
  one-liner that is easy to forget exactly when it matters most.
- **The eval baseline is version-bound.** A quality rating is only meaningful for
  the prompt it was produced against; a silent prompt change orphans it.
- **The cache is immutable at the DB role** (INSERT + SELECT only, ADR-0003 /
  C3b). A bad cached row cannot be deleted, so the version namespace is the only
  retirement lever.

## Decision

### 1. The governing invariant

> **Everything that influences the model's output but is NOT a cache-key field
> must be frozen for a given `prompt_version`.**

A bump to `prompt_version` is the act of declaring a new frozen baseline.

### 2. What forces a bump (the trigger list)

**Mandatory and mechanically guarded (§4):**

- **(a) The prompt template / rendering** in `pdpl.ai.prompt`: `SYSTEM_INSTRUCTION`,
  `build_user_prompt`, the `_STATUS_AR` map, the `_severity_ar` thresholds — any
  change to what is sent or how it is rendered.
- **(b) Re-seeding the catalog text the prompt embeds**: a question's `prompt_ar`,
  or a control's `title_ar` / `description_ar` / `severity_weight`. (Re-seeding is
  itself a new migration + a catalogue update, pinned by
  `tests/test_catalog_seed_drift.py`; this rule adds: it also requires a
  `prompt_version` bump, because the embedded text is not in the cache key.)

**Mandatory human review, NOT auto-bumped:**

- **(c) A `modelVersion` alias re-point** — the provider silently re-points the GA
  alias (`gemini-2.5-flash`) to a newer underlying snapshot. This is the
  detect-not-prevent gap (ADR-0011 §6): `modelVersion` is a **post-call** value
  and **not** a cache-key field, so it **cannot** be mechanically guarded the way
  (a)/(b) are. The runtime already logs the returned `modelVersion` and warns on a
  requested-vs-returned mismatch; a detected mismatch is a **mandatory human
  review**, and a bump is the prescribed action when a clean, re-rated baseline is
  wanted. It is deliberately not automatic.

**NOT triggers — these are cache-key fields, so a change is self-busting:**
`model` (changing the configured alias, e.g. flash → pro, already varies the key
on its own — no bump needed), `control_code`, `status`, `rationale`, `lang`, and
`prompt_version` itself.

### 3. Versioning scheme: a single counter + a changelog

`prompt_version` is a single monotonic counter — `gap-ar-v1 → gap-ar-v2 → …` —
for **both** template and seed-text changes. They are not separated into version
dimensions (e.g. a `v1.1` for seed-only): the cache key does not parse the
version's structure, it only checks the string differs; and a seed-only change
changes the model's actual output just as a template change does, so from the
cache and the eval's perspective both are a new baseline that retires old rows
and requires re-rating. The *reason* for each bump is recorded in a
**per-version changelog** in `pdpl/ai/prompt.py` (one line: what changed and
why), which is where provenance belongs — not encoded into the version string.

### 4. Enforcement: a mechanical guard (`tests/test_prompt_version_governance.py`)

A drift test, mirroring the catalogue drift discipline, pins a hash of the
**actual rendered prompt surface** to the current `PROMPT_VERSION`:

- the hash is computed by **calling `build_user_prompt`** over every seeded
  control × the three gap statuses, plus `SYSTEM_INSTRUCTION` — it hashes the
  real OUTPUT, **never a hand-listed set of fields**. This is the load-bearing
  design choice: a field added to the prompt later (exactly as C4b added
  `severity_weight` and `description_ar`) is captured automatically, so the guard
  cannot suffer the blind spot it exists to catch;
- the pinned hash is **captured from the real function** on the current version,
  not hand-written (the C3a/C4b golden discipline);
- a drift in the template, the rendering, or the embedded seeded text changes the
  hash and **fails the build** until `PROMPT_VERSION` and the pin are moved
  together. The failure message is actionable and names all three required
  actions: **(a)** bump `PROMPT_VERSION` (+ changelog line), **(b)** update the
  pinned version + hash, **(c)** re-run the eval and re-rate — the human
  `quality_score` baseline is version-bound and does not carry forward.

Trigger (c) — `modelVersion` drift — is **not** covered by this guard (a post-call
value, not a key field); it remains the documented human review of §2.

### 5. Cache consequence (confirmed)

A bump creates a **new key namespace**: every key under the new `prompt_version`
is a miss on first request, so explanations are **recomputed lazily** (miss →
explain → gate → put) under the new version. Old rows — keyed by the old
`prompt_version` — are **never read again**: they are effectively retired,
remaining immutable and unreferenced. **No migration, no cleanup, no backfill** is
required (and none is possible at the `pdpl_app` role, which cannot DELETE;
optional housekeeping to prune by `prompt_version` would need a privileged role
and is out of scope). Because rows are immutable, **a `prompt_version` bump is
the only way to retire a cached explanation** — including recovering from a
systematically poor explanation that nonetheless passes the gate: you abandon the
namespace rather than edit a row.

## Consequences

**Positive**

- The cache-poisoning / stale-baseline class of bug is closed by a mechanism, not
  a reminder: a template or seed change cannot ship without a deliberate bump.
- The guard is honest about its boundary — it captures the two guardable triggers
  by hashing real output, and explicitly cedes the `modelVersion` trigger to human
  review (which the runtime already surfaces).
- The eval baseline can never be silently orphaned: the guard names re-rating as a
  required step of every bump.

**Negative / accepted**

- **A bump is now a small ritual** (version + pin + changelog + re-rate), not a
  one-character edit. Accepted — that friction is the point; it is paid only when
  the prompt or seed genuinely changes.
- **`modelVersion` drift stays detect-not-prevent.** The guarantee for trigger (c)
  rests on a human noticing the warning; the cache key cannot see a post-call
  value (ADR-0011 §6). Accepted, and surfaced not hidden.
- **Retired rows accumulate.** Old-version rows are dead weight after a bump.
  Negligible at this scale; a pruning job is deferred.

## Resolves

- **ADR-0009 open question** — "Prompt-version governance: how `prompt_version` is
  bumped and reviewed when the prompt changes." Resolved by §2–§5 above; the
  ADR-0009 line is marked RESOLVED by this ADR.

## First application — `gap-ar-v1 → gap-ar-v2` (2026-06-27)

The first real bump exercised the full ritual, and it behaved as designed.

- **The guard proved it can FAIL, not merely match.** With the v2 wording in place
  but the pin still on `gap-ar-v1`, `test_prompt_version_governance.py` FAILED
  (`gap-ar-v2 != gap-ar-v1` + surface-hash mismatch); only after bumping
  `PROMPT_VERSION` and re-capturing `_PINNED_SURFACE_HASH` from the real
  `_compute_prompt_surface_hash()` did it pass. A guard that can only match is
  vacuous; the proof it fails on an un-bumped change is the point (§4). The change
  itself was status-aware question framing for `not_assessed` only (the §2(a)
  template trigger) — `non_compliant` / `partial` rendering byte-identical.

- **Trigger (c) fired for real — and the detect-not-prevent limit bit exactly as
  ADR-0011 §6 / §2(c) predicted.** Across the bump the `gemini-2.5-flash` alias
  was (behaviorally) re-pointed to a newer snapshot: under the BYTE-IDENTICAL v1
  prompt, `ropa-non_compliant` moved 548 chars (2026-06-22, paraphrase, gate-pass)
  → 907 chars (2026-06-27, verbatim clause-copy, gate-fail), stable across two
  runs — not sample noise. This is a material model change under a frozen prompt.
  **It is NOT provable from provenance:** both the artifact and the live API
  response record only the alias `gemini-2.5-flash`, never a dated snapshot — so
  the drift is *inferred behaviorally*, exactly the post-call blind spot §2(c)
  cedes to human review. **Mitigation (applied, not deferred):** the orphaned-
  baseline risk this creates — the 4.79 mean was rated on the older model — was
  handled by a **same-model re-baseline**: v1 was re-run on the current model so
  the release comparison is v1-now vs v2-now (one variable, the prompt), and 4.79
  is marked HISTORICAL in `golden_set.yaml`, not a live baseline. The re-rate that
  §4(c) mandates was therefore a *same-model* re-rate, which is the only honest
  one once the model has drifted.

- **A schema follow-up is now framed (separate, deferred hypothesis).**
  `build_review_artifact` should persist the live `modelVersion` the explainer
  already parses (`pdpl.ai.gemini`, the `modelVersion` field) into the eval
  artifact, so a future trigger-(c) drift is **detectable from the artifact**, not
  merely inferable from behavior. This narrows — does not close — the §2(c) blind
  spot (the alias can still hide a snapshot the provider does not surface), so it
  remains human review, better instrumented. Not built here (one variable per
  session); recorded as the next governance increment.
