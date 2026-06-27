# 2026-06-27 — v2 prompt iteration: status-aware framing for `not_assessed` (gap-ar-v2)

An AI-PM session (not load/infra). Phase 5 is closed. This revisits the
`GeminiExplainer` prompt to improve gap-explanation quality, using the eval
harness as the measurement instrument and ADR-0013 as the governance ritual.
**One hypothesis, one variable.**

## The v1 weakness — evidence, not vibes

The v1 baseline is `gate_pass_rate = 1.00`, mean human `quality_score = 4.79`,
`must_expectations_rate = 0.43` (the last is a CONTENT-FIDELITY DIAGNOSTIC,
blind to Arabic lexical flexibility — C3a lesson 1 — NOT a release gate).

The 4.79 mean hides the real signal: **12 of 14 cases scored 5**; the mean is
dragged down by exactly two, and both are `not_assessed`:

| case | status | quality_score | what the model did (v1 artifact) |
|---|---|---|---|
| `dsr-access-not_assessed` | not_assessed | **3** | «هذا البند **يمثل فجوة** … **عدم وجود آلية** … يعني أنك قد لا تتمكن» |
| `ropa-not_assessed` | not_assessed | **4** | «**عدم وجود هذا السجل** يعني أنك **لا تستوفي** التزامك» |

In both, on a `not_assessed` control (compliance UNKNOWN), the model **asserted a
confirmed gap** — breaking `SYSTEM_INSTRUCTION` rule (5) («لا تخترع فجوة»). This
is the AI *deciding* instead of *explaining* — a milder cousin of the
compliance-assertion problem the gate exists to stop.

**Root cause was in the rendering, not the model.** `build_user_prompt` rendered
the questions under «المتطلبات غير المستوفاة (تحتاج معالجة)» **regardless of
status**. For `not_assessed` the questions are *unanswered*, not *failed*, yet
the prompt labelled them «unsatisfied / need remediation» — a contradictory
signal (status says "not assessed", questions say "confirmed gaps"). 2 of the 4
`not_assessed`-with-questions cases succumbed; the well-behaved ones
(`breach-72h`, `privacy-notice`) and the three empty-question cases
(`security`/`lawful-basis`/`cross-border`, all 5) happened to stay neutral.

(The second known v1 quirk — verbatim control-clause copying in the remediation
tail, C3a — was deliberately LEFT. It inflates length and hollows
`references_control_rate`, but it lowered no `quality_score`. One variable.)

## The v2 hypothesis (single change)

Make the question framing **status-aware**: `not_assessed` gets NEUTRAL,
non-action-item framing that leaves compliance OPEN (outside automated-assessment
scope / may already be satisfied / points for human review, not confirmed gaps /
do not assume a breach). `non_compliant` / `partial` keep «غير المستوفاة (تحتاج
معالجة)» UNCHANGED — their gap IS engine-confirmed. The variable is the STATUS,
not the presence of questions, so BOTH `not_assessed` sub-branches (with and
without questions) carry the SAME epistemic message — internally consistent, and
it pre-emptively removes the same action-item lean from the three empty cases.

Wording (Faisal's voice):
- with questions: «بنود خارج نطاق التقييم الآلي — حالتها غير محسومة وقد تكون
  مستوفاة. هذه نقاط للمراجعة البشرية، لا فجوات مؤكَّدة؛ لا تفترض وجود إخلال:»
- no questions: «هذا البند خارج نطاق التقييم الآلي — حالته غير محسومة وقد تكون
  مستوفاة، ويتطلّب مراجعة بشرية للتأكّد. لا تفترض وجود فجوة؛ اربط الشرح بعنوان البند.»

"نقاط للمراجعة" describes a STATE; it deliberately avoids "يلزم التحقق" which
would COMMAND an action (assertion in another form). The gate
(`pdpl.verification`), the `SYSTEM_INSTRUCTION`, and the `_STATUS_AR` status
label are all UNTOUCHED.

## Governance (ADR-0013 ritual — all required, all done)

- `PROMPT_VERSION` `gap-ar-v1` → `gap-ar-v2`; v1 preserved via git + changelog +
  the saved v1 artifact (NOT a second live prompt-builder — one counter, one
  effective surface, edit in place + bump the constant).
- a `gap-ar-v2` changelog line in `prompt.py` (what changed + why).
- the governance guard (`tests/test_prompt_version_governance.py`):
  `_PINNED_PROMPT_VERSION` + `_PINNED_SURFACE_HASH` updated **from the real
  `_compute_prompt_surface_hash()`**, not hand-typed. **The guard was proven able
  to FAIL** — run against the stale pin it failed (`gap-ar-v2 != gap-ar-v1` +
  hash mismatch) BEFORE the pin was moved; then it passed. (The grep/commit
  lesson: a guard that can only match is not a guard.)
- cache: the bump opens a new key namespace; v1 rows retire automatically (keyed
  by the old `prompt_version`), never read again — no migration, no backfill.

## Measurement design — for the manual after-run (Faisal's key/cost)

Same 14-case golden set, same `harness.run`, against real Gemini at temperature 0
via `python -m pdpl.eval.manual_gemini_run`. **before** = the saved v1 artifact
(`eval-runs/gemini-2.5-flash_gap-ar-v1_20260622T195344Z.yaml`, in hand).
**after** = a new `gap-ar-v2` artifact Faisal generates.

Release decision (NOT `must_expectations_rate`):
1. **Floor (mandatory, non-negotiable):** `gate_pass_rate` stays **1.00**. The
   change is to `build_user_prompt` (model INPUT) only; `verify_explanation` (the
   gate) is structurally untouched (not in the diff) — so the floor is
   structurally protected, and confirmed numerically by the after-run.
2. **Release signal (human `quality_score`, Layer B):** do the **five**
   `not_assessed` cases — the two regressed (`dsr-access` 3, `ropa` 4) PLUS the
   three already-5 empties (`security`/`lawful-basis`/`cross-border`) — hold or
   rise, with **ZERO regression** across them AND the other nine? Measure five,
   not two: the empties moved to new wording too, so they must be re-checked.
   Target: mean `quality_score` ≥ 4.79 with the two laggards up and nobody down.
3. `must_expectations_rate`: watch the «مراجعة» token on the not_assessed cases
   as a DIRECTION hint only — never the gate.

Then re-rate against the new artifact and update `golden_set.yaml`
(`quality_score` + `quality_score_run` → the new run_id) for all cases; the 4.79
is pinned to `gap-ar-v1` and does NOT carry forward.

## Measurement run — and a model-drift finding (ADR-0013 trigger (c))

Three costed runs (temp 0, gemini-2.5-flash):

| run | gate_pass_rate | failing case | must_expectations_rate |
|---|---|---|---|
| v1 OLD (22-06, the 4.79 baseline) | 1.00 | — | 0.43 |
| v1 NOW (27-06, same prompt, current model) | 0.93 | ropa-non_compliant (907) | 0.43 |
| v2 NOW (27-06) | 0.93 | ropa-non_compliant (907) | 0.71 |

**Behavioral model drift, PROVEN (ADR-0013 trigger (c)).** `ropa-non_compliant`
under the BYTE-IDENTICAL v1 prompt went 548 chars (22-06, paraphrase, gate-pass)
→ **907 chars (27-06, verbatim clause-copy, gate-fail)**, stable across runs. No
prompt change between them — the current `gemini-2.5-flash` produces the longer
verbatim copy where the 22-06 model paraphrased. This is the inherited v1
verbatim-copy weakness (C3a, deferred) now consistently tipped over the 800 gate
bound by a materially changed model.

HONEST LIMIT: this cannot be proven from the artifact — both runs record only the
ALIAS `gemini-2.5-flash`, and the live API response's `modelVersion` also returned
the alias (no dated snapshot, no fingerprint). The drift is proven *behaviorally*
(548→907 under a frozen prompt), not by a snapshot id. MITIGATION applied: a
same-model re-baseline (v1 NOW) isolates the prompt variable from the model
variable — the clean A/B is **v1-now vs v2-now**, NOT v2 vs the (now-confounded)
4.79.

DEFERRED FOLLOW-UP (separate hypothesis, NOT built here): `build_review_artifact`
should persist the live `modelVersion` (gemini.py already reads it at parse) so a
future drift is DETECTABLE from the artifact, not merely inferable from behavior.

## The clean A/B verdict (gate only — quality not yet read)

- **Floor reframed and met.** The "1.00 floor" was a property of the 22-06 model:
  under the CURRENT model, v1 ITSELF scores 0.93 (same ropa failure). v2 is
  `0.93 == 0.93` vs v1 on the same model — **v2 does not weaken the gate relative
  to v1**, which is the correct floor. The 0.93 is the inherited ropa weakness ×
  model drift, present in v1 too — NOT a v2 defect.
- **The five not_assessed (our actual change) pass the gate CLEAN in all three
  runs.** v2's change is gate-sound.
- **must_expectations_rate is a clean single-variable signal now:** flat
  0.43→0.43 across the drift (v1 old→now), jumps to 0.71 only with v2 on the same
  model → attributable purely to the prompt. Diagnostic, not the gate, but
  directionally confirms the not_assessed framing took effect.

## Quality read — the release decision (same-model, v1-now vs v2-now)

The clean A/B is v1-now vs v2-now (both current model); 4.79 is HISTORICAL (v1 @
older model), not a live baseline. Human `quality_score` (Faisal) on the five
not_assessed:

| not_assessed case | v1-now | v2-now |
|---|---|---|
| dsr-access | 3.00 | **4.50** |
| ropa | 4.00 | **4.25** |
| security-measures | 5.00 | 5.00 |
| lawful-basis | 5.00 | 5.00 |
| cross-border | 5.00 | 5.00 |
| **mean (five)** | **4.40** | **4.75** |

Means by surface (v2-now, same-model), never conflated:
- five RATED not_assessed (the A/B subject): v1-now **4.40** → v2-now **4.75** (ACCEPTED).
- seven not_assessed (all v2-affected): v2-now **4.82**.
- all fourteen (live mean): v2-now **4.84**.

The two gap-asserting cases rose; the three already-neutral held at full. The
nine others (v1-now vs v2-now): the seven byte-identical non_compliant/partial +
ropa-non_compliant held (gate-stable; ropa-non_compliant gate-fails in BOTH at
907 — the inherited weakness, not v2); the two not_assessed inside the nine
(privacy / breach) held or improved (privacy went from a hedged "الفجوة تكمن" to
clean neutral). **Zero regression across all fourteen.** Release: ACCEPTED.

ropa's v2 score is deliberately capped at 4.25 (not 5): the questions-branch is a
weaker neutrality than the empty-branch (cross-border's cleaner win), a documented
wording limit — see Lessons.

## State (CLOSED)

- Code + governance + guard: DONE, full suite green (`222 passed / 8 failed` — the
  8 are the Supabase-dependent outbox tests, not regressions). v2 working tree
  intact after the temporary v1 re-baseline (git stash, popped clean).
- Gate: v2 is gate-neutral vs v1 on the current model; the five not_assessed clean.
- Quality: v2-now ≥ v1-now on the five, zero regression on the fourteen. ACCEPTED.

## Lessons — v2 Prompt (gap-ar-v2)

- **التعديل الجراحي: فرضية واحدة، سطح مستهدف، صفر أثر جانبي.** غيّرنا تأطير حالات
  not_assessed فقط إلى حياد كامل (status-aware framing)، وتركنا non_compliant/partial
  byte-identical. النتيجة على A/B same-model: الحالات التي كانت تجزم بفجوة ارتفعت
  (dsr 3.00→4.50)، والحالات المحايدة أصلاً ثبتت عند درجتها الكاملة (5.00) — صفر انحدار
  عبر الأربع عشرة. الدرس أن التغيير الموجّه لفرضية واحدة يُقاس ويُثبَت بدقّة؛ لو غيّرنا
  البرومبت كاملاً ما عرفنا أي تعديل حسّن أي حالة. السبب الجذري كان في الـ rendering لا في
  الموديل (سطر status-blind يرسل إشارة متناقضة)، فالإصلاح الجراحي عالج المصدر، لا العَرَض.

- **فخّ الأسئلة: الفرع الفارغ أنظف حياداً من فرع-الأسئلة.** التأطير المحايد رفع الحالات
  المظلومة، لكن ظهر حدّ في الصياغة: حالة بأسئلة مسرودة تحت الترويسة (ropa) حيّدت جزئياً
  وبقيت تلمّح للفجوة (4.00→4.25)، بينما الفرع الفارغ (cross-border) حيّد صراحةً وعكس
  رسالتنا حرفياً ("قد تكون مستوفاة"). سرد الأسئلة يسحب انتباه الموديل نحو الجزم رغم
  الترويسة المحايدة. الدرس أن الحياد ليس موحّداً عبر الفروع — الفرع الفارغ أقوى بنيوياً،
  وتقوية ترويسة فرع-الأسئلة فرضية مستقبلية مستقلة، موثّقة لا مُملَّسة (كبحنا درجة ropa عند
  4.25 عمداً لنحفظ هذا التمييز في الإشارة).

- **خداع خطّ الأساس القديم: الموديل يتغيّر تحت نفس الاسم.** الـ alias (gemini-2.5-flash)
  أُعيد توجيهه لـ snapshot أحدث بين القياسين — أثبتناه سلوكياً (ropa-non_compliant انتقل
  548→907 بنفس البرومبت بالضبط، ثابتاً عبر تشغيلين، فلا يكون ضوضاء عيّنة). فالاعتماد على
  رقم تاريخي (4.79، مقيس على نموذج أقدم) يخدع: مقارنة v2 به تخلط متغيّرين — تغيير البرومبت
  وتغيير النموذج. الحل النظيف الوحيد هو A/B same-model في اللحظة نفسها: أعدنا تشغيل v1 على
  النموذج الحالي، فصار v1-now مقابل v2-now متغيّراً واحداً حقيقياً. الحدّ المعرفي الصادق:
  الـ artifact يسجّل الـ alias فقط لا snapshot مؤرّخ، فالـ drift غير قابل للإثبات منه —
  مُلمَّح لا مُثبَت بالـ provenance. الخطوة القادمة: تطوير الـ schema ليلتقط modelVersion
  الحيّ (الكود يقرأه أصلاً) فيصير الـ drift القادم قابلاً للكشف لا الاستنتاج.
