"""Catalogue ↔ migration drift guard (C3a, ADR-0009 §2).

Pure and OFFLINE — no database, no Alembic run. It proves three things about
the seeded question text the explainer is grounded on:

  1. ROW PRESERVATION (the C3a restructure is safe): migration 0004 was
     rewritten from inline SQL into a parameterized bulk insert built from a
     frozen `_SEED_QUESTIONS` constant. `_ORIGINAL_SEED` below freezes the
     values as they were ORIGINALLY seeded; this test asserts the migration's
     constant reproduces them VERBATIM — turning "identical to today" from a
     claim into an offline proof. Editing the migration body is safe (Alembic
     keys on the revision id; an applied DB never re-runs it).

  2. SINGLE SOURCE OF TRUTH: `pdpl.catalog.SEEDED_QUESTIONS` mirrors the
     migration's frozen rows VERBATIM, on every literal column, with the same
     set of codes in both directions. The catalogue can never silently drift
     from what the migration seeded.

  3. The migration is loaded by a revision-prefix GLOB and executed in
     isolation via `spec_from_file_location` — its filename ("0004_…") is not a
     valid module name, and its top level has no side effects (only constants
     and function defs), so importing it to read `_SEED_QUESTIONS` is safe.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from pdpl.catalog import SEEDED_CONTROLS, SEEDED_QUESTIONS

# The migration's seed, frozen as a 5-tuple per row:
#   (control_code, code, prompt_en, prompt_ar, display_order)
# Copied verbatim from migration 0004 AS ORIGINALLY SEEDED. This is the golden
# the restructure must reproduce exactly — do not "tidy" these strings.
_ORIGINAL_SEED: tuple[tuple[str, str, str, str, int], ...] = (
    (
        "PDPL-ART12-PRIVACY-NOTICE",
        "Q-ART12-NOTICE-EXISTS",
        "Do you publish a privacy notice to data subjects before collecting their personal data?",
        "هل تنشر إشعار خصوصية لأصحاب البيانات قبل جمع بياناتهم الشخصية؟",
        1,
    ),
    (
        "PDPL-ART12-PRIVACY-NOTICE",
        "Q-ART12-NOTICE-PURPOSES",
        "Does the privacy notice state the purposes for which personal data is processed?",
        "هل يوضح إشعار الخصوصية أغراض معالجة البيانات الشخصية؟",
        2,
    ),
    (
        "PDPL-ART12-PRIVACY-NOTICE",
        "Q-ART12-NOTICE-RECIPIENTS",
        "Does the privacy notice identify the recipients or categories of recipients of the data?",
        "هل يحدد إشعار الخصوصية الجهات المستلمة للبيانات أو فئاتها؟",
        3,
    ),
    (
        "PDPL-ART12-PRIVACY-NOTICE",
        "Q-ART12-NOTICE-RIGHTS",
        "Does the privacy notice explain the data subject's rights and how to exercise them?",
        "هل يبيّن إشعار الخصوصية حقوق صاحب البيانات وكيفية ممارستها؟",
        4,
    ),
    (
        "PDPL-ART4-DSR-ACCESS",
        "Q-ART4-ACCESS-PROCESS",
        "Do you have a documented process for handling data-subject access requests?",
        "هل لديك إجراء موثّق للتعامل مع طلبات وصول أصحاب البيانات إلى بياناتهم؟",
        1,
    ),
    (
        "PDPL-ART4-DSR-ACCESS",
        "Q-ART4-ACCESS-TIMEFRAME",
        "Do you respond to access requests within a defined timeframe?",
        "هل تستجيب لطلبات الوصول خلال مدة زمنية محددة؟",
        2,
    ),
    (
        "PDPL-ART20-BREACH-NOTIFY-72H",
        "Q-ART20-BREACH-PROCEDURE",
        "Do you have a documented personal-data breach response procedure?",
        "هل لديك إجراء موثّق للاستجابة لتسرب البيانات الشخصية؟",
        1,
    ),
    (
        "PDPL-ART20-BREACH-NOTIFY-72H",
        "Q-ART20-BREACH-72H",
        "Does the procedure commit to notifying the competent authority within 72 hours of becoming aware of a breach?",
        "هل يلتزم الإجراء بإبلاغ الجهة المختصة خلال 72 ساعة من العلم بالتسرب؟",
        2,
    ),
    (
        "PDPL-ART31-ROPA",
        "Q-ART31-ROPA-MAINTAINED",
        "Do you maintain a record of personal-data processing activities (RoPA)?",
        "هل تحتفظ بسجل لعمليات معالجة البيانات الشخصية؟",
        1,
    ),
)

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _load_migration_0004() -> ModuleType:
    """Load migration 0004 by revision-prefix glob, without importing it as a
    dotted module (its filename is not a valid identifier)."""
    matches = sorted(_VERSIONS_DIR.glob("0004_*.py"))
    assert len(matches) == 1, f"expected exactly one 0004_*.py, found {matches}"
    spec = importlib.util.spec_from_file_location("_mig_0004", matches[0])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration_seed() -> tuple[tuple[str, str, str, str, int], ...]:
    return _load_migration_0004()._SEED_QUESTIONS


def test_migration_constant_reproduces_the_original_seed(migration_seed) -> None:
    """ROW PRESERVATION: the restructured migration's frozen constant equals the
    originally-seeded values, verbatim and in order."""
    assert migration_seed == _ORIGINAL_SEED


def test_catalog_mirrors_the_migration_verbatim(migration_seed) -> None:
    """SINGLE SOURCE OF TRUTH: the catalogue mirrors every literal column the
    migration seeds, exactly."""
    catalog_rows = {
        q.code: (q.control_code, q.prompt_en, q.prompt_ar, q.display_order)
        for q in SEEDED_QUESTIONS
    }
    migration_rows = {
        code: (control_code, prompt_en, prompt_ar, display_order)
        for (control_code, code, prompt_en, prompt_ar, display_order) in migration_seed
    }
    assert catalog_rows == migration_rows


def test_code_sets_match_in_both_directions(migration_seed) -> None:
    """No question exists on one side only — the mirror is total."""
    catalog_codes = {q.code for q in SEEDED_QUESTIONS}
    migration_codes = {code for (_c, code, _e, _a, _o) in migration_seed}
    assert catalog_codes == migration_codes


def test_catalog_codes_are_unique() -> None:
    codes = [q.code for q in SEEDED_QUESTIONS]
    assert len(codes) == len(set(codes))


# =====================================================================
# Controls (C4b, ADR-0011 §"control-text gap"). The SAME C3a discipline,
# one layer up: the explainer's control TEXT (`title_ar`/`description_ar`)
# and `severity_weight` must be a proven-faithful mirror of migration 0003,
# not hand-typed lookalikes. Mirrors the questions section above exactly.
# =====================================================================

# Migration 0003's control seed, frozen as an 8-tuple per row:
#   (code, title_en, title_ar, description_en, description_ar, category,
#    severity_weight, effective_from)
# Captured VERBATIM from `git show main:0003_seed_controls.py` — the genuine
# PRE-REFACTOR original (the inline SQL INSERT), extracted by parsing that SQL,
# NOT re-derived from the new `_SEED_CONTROLS` constant (which would make the
# proof circular). This is the golden the parameterized restructure must
# reproduce exactly — do not "tidy" these strings.
_ORIGINAL_SEED_CONTROLS: tuple[
    tuple[str, str, str, str, str, str, float, str], ...
] = (
    (
        "PDPL-ART4-DSR-ACCESS",
        "Right of access to personal data",
        "حق الوصول إلى البيانات الشخصية",
        "The data subject has the right to obtain access to their personal data held by the controller, including the categories of data, purposes of processing, and recipients.",
        "لصاحب البيانات الشخصية الحق في الوصول إلى بياناته الشخصية المحفوظة لدى جهة التحكم، بما في ذلك فئات البيانات وأغراض المعالجة والجهات المستلمة.",
        "data_subject_rights",
        7.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART4-DSR-CORRECT",
        "Right to correction of personal data",
        "حق تصحيح البيانات الشخصية",
        "The data subject has the right to request correction of their personal data when it is inaccurate, incomplete, or outdated.",
        "لصاحب البيانات الشخصية الحق في طلب تصحيح بياناته الشخصية إذا كانت غير صحيحة أو غير مكتملة أو غير محدثة.",
        "data_subject_rights",
        6.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART4-DSR-DELETE",
        "Right to deletion of personal data",
        "حق حذف البيانات الشخصية",
        "The data subject has the right to request deletion of their personal data when it is no longer necessary for the purposes for which it was collected, subject to legal retention obligations.",
        "لصاحب البيانات الشخصية الحق في طلب حذف بياناته الشخصية متى انتفت الحاجة إليها للغرض الذي جمعت من أجله، مع مراعاة الالتزامات النظامية للاحتفاظ.",
        "data_subject_rights",
        7.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART5-LAWFUL-BASIS",
        "Lawful basis for processing personal data",
        "الأساس النظامي لمعالجة البيانات الشخصية",
        "Personal data may only be processed for a specific, declared, and legitimate purpose, with a lawful basis such as explicit consent, performance of a contract, or compliance with a legal obligation.",
        "لا يجوز معالجة البيانات الشخصية إلا لغرض محدد ومعلن ومشروع، استناداً إلى أساس نظامي كالموافقة الصريحة أو تنفيذ عقد أو الالتزام بنص نظامي.",
        "lawful_basis",
        9.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART12-PRIVACY-NOTICE",
        "Privacy notice disclosure to data subjects",
        "إفصاح إشعار الخصوصية لأصحاب البيانات",
        "The controller must disclose to data subjects the purposes of processing, categories of data collected, recipients, retention periods, and rights, in clear and accessible language prior to collection.",
        "يجب على جهة التحكم إفصاح أغراض المعالجة وفئات البيانات المجموعة والجهات المستلمة ومدد الاحتفاظ وحقوق صاحب البيانات بلغة واضحة وسهلة الوصول قبل عملية الجمع.",
        "transparency",
        7.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART19-SECURITY-MEASURES",
        "Technical and organisational security measures",
        "التدابير الفنية والتنظيمية لحماية البيانات",
        "The controller must implement technical and organisational measures appropriate to the risk to protect personal data against unauthorised access, disclosure, loss, alteration, or destruction.",
        "يجب على جهة التحكم اتخاذ التدابير الفنية والتنظيمية الملائمة لمستوى الخطر لحماية البيانات الشخصية من الوصول غير المصرح به والإفصاح والفقد والتعديل والإتلاف.",
        "security",
        9.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART20-BREACH-NOTIFY-72H",
        "Personal data breach notification within 72 hours",
        "إشعار تسرب البيانات الشخصية خلال 72 ساعة",
        "In the event of a personal data breach likely to harm data subjects, the controller must notify the competent authority within 72 hours of becoming aware of the breach, and notify affected data subjects without undue delay.",
        "في حال وقوع تسرب للبيانات الشخصية قد يضر بأصحابها، يجب على جهة التحكم إبلاغ الجهة المختصة خلال 72 ساعة من علمها بالحادثة، وإبلاغ أصحاب البيانات المتأثرين دون تأخير غير مبرر.",
        "breach_notification",
        10.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART25-RETENTION-LIMITS",
        "Retention limits for personal data",
        "حدود الاحتفاظ بالبيانات الشخصية",
        "Personal data must not be retained beyond the period necessary for the purpose of processing, unless a separate legal or regulatory obligation requires longer retention.",
        "لا يجوز الاحتفاظ بالبيانات الشخصية لمدة تتجاوز ما تستلزمه أغراض المعالجة، ما لم يوجد التزام نظامي مستقل يستوجب مدة احتفاظ أطول.",
        "retention",
        6.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART29-CROSS-BORDER",
        "Cross-border transfer of personal data",
        "نقل البيانات الشخصية خارج المملكة",
        "Transfer of personal data outside the Kingdom requires an appropriate legal basis and adequate safeguards, in accordance with the conditions set by the competent authority.",
        "يستلزم نقل البيانات الشخصية خارج المملكة وجود أساس نظامي مناسب وضمانات كافية وفق الضوابط التي تحددها الجهة المختصة.",
        "cross_border_transfer",
        8.0,
        "2023-09-14",
    ),
    (
        "PDPL-ART31-ROPA",
        "Records of processing activities",
        "سجل عمليات معالجة البيانات الشخصية",
        "The controller must maintain a record of personal-data processing activities including purposes, categories of data subjects and data, recipients, retention periods, and security measures, and make it available to the competent authority on request.",
        "يجب على جهة التحكم مسك سجل عمليات معالجة البيانات الشخصية شاملاً الأغراض وفئات أصحاب البيانات وفئات البيانات والجهات المستلمة ومدد الاحتفاظ والتدابير الأمنية، وإتاحته للجهة المختصة عند الطلب.",
        "records_of_processing",
        5.0,
        "2023-09-14",
    ),
)


def _load_migration_0003() -> ModuleType:
    """Load migration 0003 by revision-prefix glob, without importing it as a
    dotted module (its filename is not a valid identifier)."""
    matches = sorted(_VERSIONS_DIR.glob("0003_*.py"))
    assert len(matches) == 1, f"expected exactly one 0003_*.py, found {matches}"
    spec = importlib.util.spec_from_file_location("_mig_0003", matches[0])
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration_control_seed() -> tuple[
    tuple[str, str, str, str, str, str, float, str], ...
]:
    return _load_migration_0003()._SEED_CONTROLS


def test_migration_0003_constant_reproduces_the_original_seed(
    migration_control_seed,
) -> None:
    """ROW PRESERVATION: the restructured migration's frozen `_SEED_CONTROLS`
    equals the genuine pre-refactor inline-SQL values, verbatim and in order."""
    assert migration_control_seed == _ORIGINAL_SEED_CONTROLS


def test_catalog_mirrors_the_migration_controls_verbatim(
    migration_control_seed,
) -> None:
    """SINGLE SOURCE OF TRUTH: the catalogue mirrors every literal column the
    migration seeds, exactly. This is what makes the explainer's control text
    provably faithful to what migration 0003 put in the DB."""
    catalog_rows = {
        c.code: (
            c.title_en,
            c.title_ar,
            c.description_en,
            c.description_ar,
            c.category,
            c.severity_weight,
            c.effective_from,
        )
        for c in SEEDED_CONTROLS
    }
    migration_rows = {
        code: (te, ta, de, da, cat, sev, eff)
        for (code, te, ta, de, da, cat, sev, eff) in migration_control_seed
    }
    assert catalog_rows == migration_rows


def test_control_code_sets_match_in_both_directions(
    migration_control_seed,
) -> None:
    """No control exists on one side only — the mirror is total."""
    catalog_codes = {c.code for c in SEEDED_CONTROLS}
    migration_codes = {row[0] for row in migration_control_seed}
    assert catalog_codes == migration_codes


def test_catalog_control_codes_are_unique() -> None:
    codes = [c.code for c in SEEDED_CONTROLS]
    assert len(codes) == len(set(codes))
