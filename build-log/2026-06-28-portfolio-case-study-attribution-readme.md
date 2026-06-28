# 2026-06-28 — Portfolio/docs: first case study, attribution settings, README wiring

A documentation/portfolio session, not an engineering one — no `src/` logic changed. What happened
today, factually:

## What happened

- **First deep case study published.** `docs/case-studies/load-testing-hypothesis-inversion.md` — the
  Phase-5 load-testing hypothesis inversion, narrated (going-in pool-bound hypothesis → inverted to
  event-loop-bound by direct measurement → the §7 fix → safety held). Every number traces to ADR-0014
  and the Phase-5 build-logs. Shipped via **PR #11** (squash-merged, branch deleted).

- **Attribution settings — solo narrative going forward.** `.claude/settings.json` now sets
  `attribution.commit = ""` and `attribution.pr = ""`, disabling the Co-Authored-By commit trailer and
  the PR byline + sessionUrl. The deprecated `includeCoAuthoredBy` boolean is intentionally not used.
  **Going-forward only — no history rewrite**; existing trailers stay as honest history. **PR #11 was
  the first PR to prove `attribution.pr` works** — its description carried no Claude byline and no
  sessionUrl (verified against the raw body on GitHub).

- **README wiring.** The case study was added to three discovery surfaces: the "Start here" reading path
  as item **#1** (count four → five, with ADR-0014 reframed as the methodology behind it), the **Scale**
  row of the six-domain table (narrative + decision doc), and the body Scale tail (case study → ADR-0014
  → build-log). Restraint kept elsewhere (no Observability row, no out-of-scope list).

- **Line-60 estimate fix.** The README body's `~100 ms` Gemini hold-time was reframed as an estimate
  ("not measured on this system") for cross-surface consistency with the case study — the load-bearing
  number now reads as an estimate on every public surface, not just one.

## Context (this phase)

The repo was made public earlier this phase — the precondition for today's portfolio work. That step
carried its own security tail: a security audit (clean — no live secret in tracked files), the Supabase
project-ref scrubbed from tracked files (commit `a6114ba`), and the database password rotated. Those
actions happened in prior sessions, not today; recorded here only as the background that made a public
case study + README possible.

## Lessons

<!-- (author's own — to be filled in if this session warrants one) -->
