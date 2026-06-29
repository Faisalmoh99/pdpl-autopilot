# 2026-06-28 — Portfolio/docs: second case study (model drift) + README wiring

A documentation/portfolio session, not an engineering one — no `src/` logic changed. What happened
today, factually:

## What happened

- **Second deep case study published.** `docs/case-studies/model-drift-same-model-ab.md` — the
  model-drift discovery via a same-model A/B: under a byte-identical v1 prompt, `ropa-non_compliant`
  went 548 → 907 characters between two dates (stable across every current-model run), crossing the
  800-char gate bound; the cause was the `gemini-2.5-flash` alias re-pointed upstream. The honest limit
  is stated plainly — the drift is **behaviorally evidenced, not provable** from the artifact (every run
  records only the alias, no dated snapshot). Every number traces to the four `eval-runs/` artifacts and
  ADR-0013. Shipped via **PR #12** (squash-merged, branch deleted).

- **README wired (same pattern as case study #1).** The case study was added to three surfaces: the
  "Start here" reading path as item **#5** (replacing the raw v2/drift build-log link — the build-log is
  not orphaned, it stays in the body drift-section tail), the body "same-model A/B" section tail
  (narrated walkthrough, ahead of the build-log + ADR-0013), and the **AI-Product** row of the
  six-domain table (case study linked first, "What it proves" gaining "that also caught model drift").
  Reading-path items #1 and #5 are now the two narrated case studies. Restraint kept — no Observability
  row, no over-linking.

- **`attribution.pr` held on a second PR.** PR #12's description carried no Claude byline and no
  sessionUrl (verified against the raw body on GitHub) — the setting proven on PR #11 holds again.

## Lessons

<!-- (author's own — to be filled in if this session warrants one) -->
