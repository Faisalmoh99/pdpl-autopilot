# 2026-06-23 — C3b: ai_explanations content-hash cache (table + repository)

Phase 4, Session C3b adds the **persistence** half of the gap-explanation feature: a dedicated,
immutable `ai_explanations` cache so a *verified* explanation is computed once and reused, decoupled
from the finding's SCD Type-2 lifecycle (ADR-0009 §6). This session is **persistence only** — it does
**not** touch the eval (which calls the model directly, so a cache buys it nothing) and it does
**not** wire the cache into `explain_gap`; runtime wiring is deferred to C4.

## What landed

- **migration `0006_ai_explanations`** — the 6-column table exactly as ADR-0009 §6 specifies
  (`content_hash` PK, `lang`, `text`, `prompt_version`, `model`, `created_at`), all `NOT NULL`. No
  extra observability columns: a future debug need is a future migration, not a reason to widen now.
  Grants **mirror `audit_log`** (migration 0002 / ADR-0003), NOT `outbox`: `SELECT + INSERT` only,
  `UPDATE/DELETE/TRUNCATE` revoked — immutability enforced at the **role**, not by app discipline.
- **`pdpl.db.ai_explanations`** — a thin repository: pure `compute_cache_key`, `get`, `put`. Plain
  DB layer like `db/audit.py` / `db/outbox.py`; imports nothing from `pdpl.ai`, the decision core, or
  the verifier, so the trust boundary never crosses the cache.
- **13 tests**, all green; `.importlinter` contracts still green.

## Decisions made this session (and why)

- **Key serialization = canonical JSON** (`json.dumps(payload, sort_keys=True, ensure_ascii=False)`)
  → `sha256` hex (64 char, matches the `text` PK). Rejected `\n`-delimited `key=value`: it is **not**
  collision-safe if a field (e.g. `rationale`) contains a newline — the separator would collide with
  an in-field newline. JSON escaping makes the framing unambiguous regardless of field content.
- **Normalization = NFC + whitespace only, NOT `verification.normalize`.** The C1 Arabic fold is
  *lossy by design* (alef variants, hamza, ة/ه, ى/ي, lowercasing) — correct for tolerant denylist
  **matching**, but **wrong for an identity key**: lossy folding would collide two distinct inputs
  onto one row and serve the wrong explanation. Verified against the **real** rationale: it is pure
  ASCII machine text (`"privacy notice: 2 of 4 question(s) satisfied; gap(s): Q-..."`), so NFC is
  *defensive* today and becomes *load-bearing* automatically if a keyed field ever carries Arabic.
- **Structural exclusion, not behavioural.** The excluded fields (`control_title_ar`,
  `control_description_ar`, `severity_weight`, `unsatisfied_questions_ar`) are **not parameters** of
  `compute_cache_key` — exclusion is enforced by the signature. Deliberately **no** "exclusion" test:
  it would have to vary fields the function does not accept, i.e. a vacuous test. Key-sensitivity
  covers the six included fields.
- **`put` = `ON CONFLICT DO NOTHING`** → the **first** verified explanation for a key wins
  permanently; a concurrent second `put` is a no-op. Model non-determinism (even at temp 0) means a
  later generation is silently discarded — the intended **compute-once / reuse** semantic, not an
  accident. A refresh is a `prompt_version` bump (a new key), never an in-place edit.
- **Verified-only is the CALLER's contract.** `put` cannot and does not verify — its docstring says
  so explicitly. The cache enforces no safety; the deterministic gate (`pdpl.verification`,
  ADR-0010 §1) is the guarantee. No reader should mistake the cache for a safety layer.
- **Wiring deferred to C4.** `explain_gap`/the orchestrator are untouched; the intended C4 sequence
  (`get` → miss → `explain` → **gate** → `put` verified only → return) is documented in the repo
  docstring but not built — it depends on C4 open questions (where the explanation is triggered, the
  endpoint shape, where the session comes from).

## Test output (provenance)

Manual migration cycle (Phase-3 convention — automated downgrade on shared Supabase would drop the
table other tests/the app depend on; see bounded gap below):

```
UPGRADE   0005_outbox -> 0006_ai_explanations         (clean)
DOWNGRADE 0006_ai_explanations -> 0005_outbox         (clean)
UPGRADE   0005_outbox -> 0006_ai_explanations         (clean, re-applied)
final: 0006_ai_explanations (head)
```

Post-upgrade schema/grants verified directly against Supabase:

```
COLUMNS: content_hash text NOT NULL | lang text NOT NULL | text text NOT NULL
         | prompt_version text NOT NULL | model text NOT NULL | created_at timestamptz NOT NULL
PRIMARY KEY: content_hash
pdpl_app grants: ['INSERT', 'SELECT']   (no UPDATE/DELETE/TRUNCATE)
```

```
tests/test_ai_explanations_cache.py ......... 13 passed in 6.61s
tests/test_architecture.py .                   1 passed   (importlinter contracts green)
```

## Bounded gaps (surfaced, not hidden)

- **No automated downgrade coverage.** The migration up/down/up cycle is verified **manually** and
  captured above as provenance; there is no pytest that runs `alembic downgrade`, because doing so on
  the shared Supabase project would drop the live table. A disposable-Postgres test harness (the
  alternative) is a separate test-infra decision, not built here. Same honesty pattern as the
  model-alias gap below.
- **`model` in the key busts only on a STRING change.** If the provider silently re-points the
  `gemini-2.5-flash` alias to a new snapshot, the key does not change and a stale explanation could be
  served. Mitigation (pin a dated model id) is discipline, deferred — recorded here, not built.
  **The cache amplifies the C3a model-alias gap:** without persistence a silent snapshot swap was a
  one-off stale answer; now the first explanation under the unchanged alias is **persisted** and
  re-served indefinitely (the `DO NOTHING` "first write wins" semantic), so the staleness sticks until
  a `prompt_version`/`model` string change busts the key. Same fix (pin a dated model id), now with a
  sharper reason. Logged design gap, not testable behaviour.

## Out of scope (unchanged from the brief)

C4 runtime/HTTP/findings wiring; the v2 prompt; real billing; cache eviction/TTL (immutable rows —
no cleanup); LLM-as-judge.
