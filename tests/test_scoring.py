"""Readiness scoring + gap report — ADR-0007.

Two layers, matching the service's two layers:

  * PURE unit tests over `compute_score` / `build_gap_report`. No database.
    These drive every status exhaustively — including `unknown` and
    `not_applicable`, which the live engine does not emit yet — proving the
    scoring function is TOTAL and the rules (partial=0.5, unknown scores 0,
    not_assessed excluded from the score, not_applicable excluded entirely,
    score=None when nothing is assessed) hold exactly.

  * INTEGRATION tests over `score_tenant` / `gap_report` against the same
    Supabase project as the app (see tests/conftest.py). These prove the read
    (active_controls LEFT JOIN current_findings) and end-to-end correctness on
    REAL findings produced by record_answers -> run_check.

The worked example used throughout (3 of 10 controls assessed):

    PDPL-ART31-ROPA            w=5   compliant      credit 1.0 -> 5.0
    PDPL-ART4-DSR-ACCESS       w=7   non_compliant  credit 0.0 -> 0.0
    PDPL-ART12-PRIVACY-NOTICE  w=7   partial        credit 0.5 -> 3.5
    (the other 7 controls)            not_assessed   excluded from score

    weighted_achieved = 8.5 ; weighted_assessed = 19.0
    score    = 8.5 / 19.0 * 100 = 44.74
    coverage = 3 assessed / 10 applicable = 30.0
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
import pytest
import uuid6

from pdpl.services.answers import record_answers
from pdpl.services.checks import TenantNotFound, run_check
from pdpl.services.scoring import (
    GapItem,
    ReadinessScore,
    build_gap_report,
    compute_score,
    gap_report,
    score_tenant,
)


# =====================================================================
# PURE unit tests — compute_score. No database.
# =====================================================================
def test_score_is_none_when_nothing_assessed():
    # Only not_assessed / not_applicable -> no honest number to report.
    result = compute_score(
        [("not_assessed", 5.0), ("not_assessed", 9.0), ("not_applicable", 7.0)]
    )
    assert result.score is None
    assert result.assessed_controls == 0
    # not_applicable is out of scope; only the two not_assessed are applicable.
    assert result.applicable_controls == 2
    assert result.coverage == 0.0


def test_worked_example_weighted_score_and_separate_coverage():
    rows = [
        ("compliant", 5.0),
        ("non_compliant", 7.0),
        ("partial", 7.0),
        # seven controls we have not assessed yet
        *[("not_assessed", w) for w in (9.0, 6.0, 7.0, 10.0, 6.0, 8.0, 9.0)],
    ]
    result = compute_score(rows)

    assert result.weighted_achieved == 8.5  # 5*1.0 + 7*0.0 + 7*0.5
    assert result.weighted_assessed == 19.0  # 5 + 7 + 7
    assert result.score == 44.74  # 8.5/19 * 100, rounded
    assert result.assessed_controls == 3
    assert result.applicable_controls == 10
    assert result.coverage == 30.0  # 3 / 10 — reported SEPARATELY from score


def test_partial_counts_as_half_not_zero_not_full():
    # One partial control alone -> exactly 50, never 0 (non_compliant) nor 100.
    assert compute_score([("partial", 8.0)]).score == 50.0


def test_unknown_is_assessed_and_scores_zero():
    # unknown: the engine ran but could not decide -> assessed, credit 0.
    result = compute_score([("unknown", 6.0), ("compliant", 6.0)])
    assert result.assessed_controls == 2  # unknown IS assessed
    assert result.score == 50.0  # (0 + 6) / 12 * 100
    assert result.counts["unknown"] == 1


def test_not_applicable_excluded_from_every_denominator():
    # not_applicable must not move score or coverage at all.
    with_na = compute_score([("compliant", 5.0), ("not_applicable", 9.0)])
    without_na = compute_score([("compliant", 5.0)])
    assert with_na.score == without_na.score == 100.0
    assert with_na.coverage == without_na.coverage == 100.0
    assert with_na.applicable_controls == 1  # the not_applicable one is gone


def test_not_assessed_drags_coverage_but_not_score():
    # Assessed part is perfect (100), but coverage reflects the unanswered half.
    result = compute_score([("compliant", 5.0), ("not_assessed", 5.0)])
    assert result.score == 100.0  # of what was assessed
    assert result.coverage == 50.0  # only half the catalogue assessed
    assert result.assessed_controls == 1
    assert result.applicable_controls == 2


def test_counts_cover_every_status():
    rows = [
        ("compliant", 1.0),
        ("partial", 1.0),
        ("non_compliant", 1.0),
        ("unknown", 1.0),
        ("not_assessed", 1.0),
        ("not_applicable", 1.0),
    ]
    counts = compute_score(rows).counts
    for status in (
        "compliant",
        "partial",
        "non_compliant",
        "unknown",
        "not_assessed",
        "not_applicable",
    ):
        assert counts[status] == 1


def test_score_is_deterministic_same_rows_same_result():
    rows = [("compliant", 5.0), ("partial", 7.0), ("non_compliant", 7.0)]
    assert compute_score(rows) == compute_score(list(rows))


def test_unrecognised_status_raises_not_silently_scored():
    with pytest.raises(ValueError):
        compute_score([("totally_bogus", 5.0)])


def test_zero_weight_assessed_does_not_divide_by_zero():
    # Defensive: weights are CHECK > 0 in the DB, but the pure function must
    # not blow up if ever handed zero-weight assessed rows.
    result = compute_score([("compliant", 0.0)])
    assert result.score is None


# =====================================================================
# PURE unit tests — build_gap_report. No database.
# =====================================================================
def test_gap_report_filters_and_orders_by_severity_desc():
    # rows: (code, title_en, title_ar, status, rationale, weight)
    rows = [
        ("PDPL-ART31-ROPA", "ROPA", "سجل", "compliant", "all satisfied", 5.0),
        ("PDPL-ART4-DSR-ACCESS", "Access", "وصول", "non_compliant", "none", 7.0),
        ("PDPL-ART20-BREACH-NOTIFY-72H", "Breach", "تسرب", "partial", "1 of 2", 10.0),
        ("PDPL-ART25-RETENTION-LIMITS", "Retention", "احتفاظ", "not_assessed", "u", 6.0),
        ("PDPL-ART5-LAWFUL-BASIS", "Lawful", "أساس", "not_applicable", "n/a", 9.0),
    ]
    gaps = build_gap_report(rows)

    # compliant + not_applicable excluded; three real gaps remain.
    codes = [g.control_code for g in gaps]
    assert codes == [
        "PDPL-ART20-BREACH-NOTIFY-72H",  # 10.0
        "PDPL-ART4-DSR-ACCESS",  # 7.0
        "PDPL-ART25-RETENTION-LIMITS",  # 6.0
    ]
    assert all(isinstance(g, GapItem) for g in gaps)
    # Status + rationale + severity + both titles carried through for the top gap.
    top = gaps[0]
    assert top.status == "partial"
    assert top.rationale == "1 of 2"
    assert top.severity_weight == 10.0
    assert top.title_en == "Breach"
    assert top.title_ar == "تسرب"


def test_gap_report_tie_break_is_control_code():
    rows = [
        ("PDPL-Z", "Z", "ز", "non_compliant", "r", 7.0),
        ("PDPL-A", "A", "أ", "partial", "r", 7.0),
    ]
    gaps = build_gap_report(rows)
    # Equal severity -> alphabetical by code, deterministically.
    assert [g.control_code for g in gaps] == ["PDPL-A", "PDPL-Z"]


def test_gap_report_unknown_is_a_gap():
    rows = [("PDPL-X", "X", "إكس", "unknown", "could not decide", 8.0)]
    gaps = build_gap_report(rows)
    assert len(gaps) == 1
    assert gaps[0].status == "unknown"


def test_gap_report_rejects_unrecognised_status():
    with pytest.raises(ValueError):
        build_gap_report([("PDPL-X", "X", "إكس", "bogus", "r", 5.0)])


# =====================================================================
# Helpers for the integration tests (mirror tests/test_decision_engine.py).
# =====================================================================
async def _create_test_tenant(app_database_url: str, label: str) -> UUID:
    tenant_id = uuid6.uuid7()
    conn = await asyncpg.connect(app_database_url, statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO tenants (id, name, business_type)
            VALUES ($1::uuid, $2, 'saas')
            """,
            str(tenant_id),
            f"test_scoring_{label}_{tenant_id}",
        )
    finally:
        await conn.close()
    return tenant_id


# =====================================================================
# INTEGRATION tests — score_tenant / gap_report against Supabase.
# =====================================================================
async def test_score_tenant_on_real_findings_matches_worked_example(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "worked")

    # Drive three real verdicts via the REAL engine; leave the rest unanswered.
    await record_answers(
        tenant_id,
        {
            "Q-ART31-ROPA-MAINTAINED": "yes",  # ROPA (w=5)  -> compliant
            "Q-ART4-ACCESS-PROCESS": "no",  # DSR-ACCESS (w=7) -> non_compliant
            "Q-ART4-ACCESS-TIMEFRAME": "no",
            "Q-ART12-NOTICE-EXISTS": "yes",  # PRIVACY (w=7) -> partial
            "Q-ART12-NOTICE-PURPOSES": "yes",
            "Q-ART12-NOTICE-RECIPIENTS": "no",
            "Q-ART12-NOTICE-RIGHTS": "no",
        },
    )
    await run_check(tenant_id, kind="manual")

    result = await score_tenant(tenant_id)

    assert isinstance(result, ReadinessScore)
    # Score is over the THREE assessed controls (5*1 + 7*0 + 7*0.5)/19.
    assert result.score == 44.74
    assert result.weighted_achieved == 8.5
    assert result.weighted_assessed == 19.0
    assert result.assessed_controls == 3
    # All 10 seeded controls are applicable; coverage reported separately.
    assert result.applicable_controls == 10
    assert result.coverage == 30.0
    assert result.counts["compliant"] == 1
    assert result.counts["partial"] == 1
    assert result.counts["non_compliant"] == 1
    assert result.counts["not_assessed"] == 7


async def test_score_tenant_is_deterministic(app, app_database_url):
    tenant_id = await _create_test_tenant(app_database_url, "determ")
    await record_answers(tenant_id, {"Q-ART31-ROPA-MAINTAINED": "yes"})
    await run_check(tenant_id, kind="manual")

    first = await score_tenant(tenant_id)
    second = await score_tenant(tenant_id)
    assert first == second  # same current findings -> identical score


async def test_score_none_before_any_assessment(app, app_database_url):
    # A tenant with a check run but zero answers: every control not_assessed
    # -> score is None (not 0, not 100), coverage 0, full catalogue applicable.
    tenant_id = await _create_test_tenant(app_database_url, "fresh")
    await run_check(tenant_id, kind="manual")

    result = await score_tenant(tenant_id)
    assert result.score is None
    assert result.assessed_controls == 0
    assert result.applicable_controls == 10
    assert result.coverage == 0.0


async def test_score_none_with_no_check_run_at_all(app, app_database_url):
    # No check run -> no findings -> LEFT JOIN yields not_assessed for every
    # active control. Still a defined, honest answer: None / 0% coverage.
    tenant_id = await _create_test_tenant(app_database_url, "nocheck")
    result = await score_tenant(tenant_id)
    assert result.score is None
    assert result.applicable_controls == 10
    assert result.coverage == 0.0


async def test_gap_report_lists_right_controls_ordered_by_severity(
    app, app_database_url
):
    tenant_id = await _create_test_tenant(app_database_url, "gaps")
    await record_answers(
        tenant_id,
        {
            "Q-ART31-ROPA-MAINTAINED": "yes",  # compliant -> NOT a gap
            "Q-ART4-ACCESS-PROCESS": "no",  # non_compliant (w=7) -> gap
            "Q-ART4-ACCESS-TIMEFRAME": "no",
            "Q-ART20-BREACH-PROCEDURE": "yes",  # partial (w=10) -> gap
            "Q-ART20-BREACH-72H": "no",
        },
    )
    await run_check(tenant_id, kind="manual")

    gaps = await gap_report(tenant_id)
    by_code = {g.control_code: g for g in gaps}

    # Compliant ROPA must NOT appear.
    assert "PDPL-ART31-ROPA" not in by_code

    # The two answered gaps carry the right status + severity.
    assert by_code["PDPL-ART20-BREACH-NOTIFY-72H"].status == "partial"
    assert by_code["PDPL-ART20-BREACH-NOTIFY-72H"].severity_weight == 10.0
    assert by_code["PDPL-ART4-DSR-ACCESS"].status == "non_compliant"

    # Unanswered controls surface as not_assessed gaps too.
    assert any(g.status == "not_assessed" for g in gaps)

    # Ordered by severity DESC — the 10.0 breach control leads; weights are
    # non-increasing down the list.
    weights = [g.severity_weight for g in gaps]
    assert weights == sorted(weights, reverse=True)
    assert gaps[0].control_code == "PDPL-ART20-BREACH-NOTIFY-72H"

    # Every item carries a deterministic rationale (never empty) and both
    # the English and Arabic control titles, read straight from `controls`.
    assert all(g.rationale for g in gaps)
    assert all(g.title_en and g.title_ar for g in gaps)


async def test_score_and_gap_report_reject_inactive_tenant(app, app_database_url):
    bogus = uuid6.uuid7()
    with pytest.raises(TenantNotFound):
        await score_tenant(bogus)
    with pytest.raises(TenantNotFound):
        await gap_report(bogus)
