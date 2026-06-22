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

from pdpl.catalog import SEEDED_QUESTIONS

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
