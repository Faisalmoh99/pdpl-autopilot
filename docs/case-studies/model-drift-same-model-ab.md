# Case study: model drift caught by a same-model A/B

> The strongest AI-product instinct is not trusting a number — it is asking *which
> model produced it.* This is the record of a quality baseline quietly invalidated
> by a model that changed underneath a frozen prompt, how a same-model A/B isolated
> the prompt from the model, and the honest limit on what the evidence can prove.

**Project:** PDPL Autopilot — a compliance-readiness assistant for small Saudi
businesses.
**Phase:** post-Phase-5 AI-PM session, 2026-06-27.
**Primary sources:** [ADR-0013 — Prompt-Version Governance](../adr/0013-prompt-version-governance.md)
(the governance ritual, trigger (c)), the Phase-5 build-log for the v2 prompt
iteration, and the four `eval-runs/` artifacts (v1 @ 2026-06-22, v1 re-run and v2
@ 2026-06-27). Every number below traces to one of those artifacts.

---

## 1. Context

PDPL Autopilot drafts Arabic explanations of compliance gaps with Gemini, behind a
deterministic gate. Their quality is measured, not asserted: a 14-case golden set
is run at temperature 0, the gate pass-rate is recorded, and a human rates each
case 1–5. The v1 prompt's pinned baseline was `gate_pass_rate = 1.00` and a mean
human `quality_score = 4.79` (12 of 14 cases scored 5; the mean dragged down by two
`not_assessed` cases that asserted a confirmed gap on a control whose compliance is
unknown). The v2 iteration was a single, surgical change — make the question
framing **status-aware**, so `not_assessed` reads as neutral "points for human
review" instead of "unsatisfied requirements," with `non_compliant` / `partial`
rendering left byte-identical. One hypothesis, one variable. Measuring it surfaced
something the prompt change did not cause.

## 2. The anomaly

The v2 measurement was run as a clean before/after. The "before" was the saved v1
artifact from 2026-06-22; the "after" was v2 on 2026-06-27. But the governance
ritual (ADR-0013) also called for re-running **v1 itself** on the current model. On
that re-run, one case moved that should not have moved at all.

Under a **byte-identical v1 prompt**, the `ropa-non_compliant` explanation went from
**548 characters** (2026-06-22) to **907 characters** (2026-06-27). The gate has an
800-character bound; at 548 the explanation passed, at 907 it was rejected. The same
prompt, the same case, the same temperature — and a 359-character swing across a
gate threshold, between two dates. Nothing in the prompt had changed.

The 907 was not a one-off. It reproduced on **every** current-model run — the v1
re-baseline and both v2 runs all land at exactly 907 characters — while 548 appears
only on the older date. The older model paraphrased the control clause; the current
model copies it verbatim, and the verbatim copy is what tips the length over the
gate bound.

## 3. The diagnosis

With the prompt frozen, the case fixed, and the temperature at 0, the input did not
change between the two dates. The one thing that *can* change without any commit on
our side is the model behind the alias. The configured model is `gemini-2.5-flash`
— a GA alias the provider re-points to a newer underlying snapshot over time. The
behavioural signature fits exactly that: a frozen prompt producing a materially
different, longer, more verbatim output, **reproducibly** (three current-model runs
at 907, never 548 again). Reproducibility is what separates this from sample noise —
a single shifted sample could be temperature-0 jitter; the same 359-character jump
on every current-model run is a changed model.

(The alias is `gemini-2.5-flash`. There is no "-live" or dated suffix in any
artifact — the configured string is the bare GA alias, and that is exactly the
problem the next two sections turn on.)

## 4. The honest limit — behaviorally evidenced, not provable

This is the maturity point, and it cuts against the conclusion. The drift is
**behaviorally evidenced** — 548 → 907 under a frozen prompt, stable across runs —
but it is **not provable from the artifact.** Every run records only the alias
`gemini-2.5-flash`; the live API response's `modelVersion` returns the same alias,
with no dated snapshot, no fingerprint, nothing that pins *which* model produced
*which* output. A grep across all four artifacts confirms it: there is no snapshot
field anywhere to point to.

So the claim is bounded precisely. We can say, with strong evidence, *the model's
behaviour changed.* We cannot say, from provenance, *the alias was re-pointed from
snapshot X to snapshot Y on date Z* — the artifact does not carry that, and claiming
it would invent a provenance we do not have. Stating the inference as an inference,
not dressing it as proof, is the discipline; the same honesty the load-testing case
study applies to its un-measured production hold-time.

## 5. Why it matters

The danger is not the longer output itself — it is what the drift does to the
baseline. The 4.79 mean was measured on the **older** model. The moment the model
changed, comparing the new v2 prompt to that 4.79 mixes two variables: the prompt
change *and* the model change. A v2 "win" or "loss" against 4.79 could be either,
and the number alone cannot tell them apart. The historical baseline silently went
stale.

This is precisely the case ADR-0013 anticipated. A `modelVersion` alias re-point is
**trigger (c)** in the governance ritual — and unlike the prompt-template and
seed-text triggers, it is *not* mechanically guarded (a post-call value cannot be a
cache-key field), so it is classified as a **mandatory human review**. Trigger (c)
fired for real here, exactly as the ADR predicted it eventually would.

## 6. The fix — a same-model A/B

The methodological fix is to remove the confound rather than reason around it:
**re-run v1 on the current model**, and compare **v1-now vs v2-now**. Both arms then
share one model, so the prompt is the only variable, and upstream drift cannot
masquerade as a prompt win.

On the five rated `not_assessed` cases — the actual subject of the v2 change — the
human mean rose from **v1-now 4.40 to v2-now 4.75**, with the two gap-asserting
cases up (`dsr-access` 3.00 → 4.50, `ropa-not_assessed` 4.00 → 4.25) and the three
already-neutral cases holding at 5.00. Means are labelled by their surface and never
conflated: the **five rated `not_assessed`** are 4.40 → 4.75; the **seven
`not_assessed`** are v2-now 4.82; **all fourteen** are v2-now 4.84. Zero regression
across the fourteen. Release: accepted.

The gate tells a consistent story once the model is held fixed. The v2 change itself
stayed gate-clean — the five `not_assessed` cases pass the gate in every run. The
overall `gate_pass_rate` reads 0.93, not 1.00 — but it reads 0.93 for **v1-now too**
(`0.93 == 0.93`), because the single failing case is `ropa-non_compliant` at 907 in
both arms. That 1.00-floor was a property of the 2026-06-22 model, not of v1 the
prompt; under the current model v1 itself scores 0.93. v2 does not weaken the gate
relative to v1, which is the correct floor to hold.

A note on that rejected case, framed the same way across every artifact in this
repo: `ropa-non_compliant` being gate-rejected at 907 characters is the **gate
working**, not a v2 regression. It is an inherited v1 weakness — the model copying a
control clause verbatim and inflating the length — that the changed model tipped
over the bound. It fails identically in v1-now and v2-now, and it is tracked
separately as [issue #9](https://github.com/Faisalmoh99/pdpl-autopilot/issues/9).

## 7. The deferred follow-up

The clean fix worked, but it leaves the underlying blind spot open: drift was
*inferred from behaviour* because the artifact could not *show* it. The next
increment is to close that gap — persist the live `modelVersion` the explainer
already parses at call time into the eval artifact, so a future trigger-(c) drift is
**detectable from the artifact, not merely inferable from behaviour.** This narrows
the blind spot without fully closing it (the alias can still hide a snapshot the
provider does not surface), so trigger (c) stays a human review — better
instrumented. It is deliberately deferred to its own session (one variable at a
time) and tracked as [issue #10](https://github.com/Faisalmoh99/pdpl-autopilot/issues/10).
Naming what you would build next, and why it is separate, is part of the work.

## 8. The one universal lesson

**A baseline is only valid against the model it was measured on.** Under a moving
alias, a historical quality number is not a fixed reference point — it silently
expires the moment the provider re-points the alias, and a comparison against it
quietly mixes two variables. The only clean comparison is a **same-model A/B**: re-
run the old arm on the current model so the prompt is the single thing that differs.
And when the evidence is behavioural rather than provenanced, the discipline is to
say so exactly — *the behaviour changed* is what the data supports; *which snapshot,
when* is not, until the artifact is built to record it.
