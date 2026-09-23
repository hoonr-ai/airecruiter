"""
Regression tests for the feedback filter in get_launched_candidates
(apps/api/routers/candidates.py).

These tests guard the correctness of the feedback filter SQL logic without
requiring a live DB connection.  They verify:

1. Action filters constrain the selected source row; No Feedback uses a
   job-scoped NOT EXISTS clause.
2. DISTINCT ON row selection, one row per (job, candidate) across both of the
   job's keys: without a feedback filter, ORDER BY prefers a person's row that
   carries the decision only when they have a second row under the job's
   other key, then created_at DESC; with a filter, a feedback-preference
   tiebreaker is injected first so the displayed row matches the filter.
3. The feedback condition is placed in WHERE, not ORDER BY.
4. The shared FULL_CTE is used for both results and count (single source of truth).
5. Cross-job data isolation: feedback tiebreaker only activates when a filter
   is active, preventing stale feedback from an unrelated job being surfaced.

Background
----------
Submit, Reject, and Unreachable constrain the selected source row directly,
so DISTINCT ON cannot return an unfiltered duplicate. No Feedback uses a
job-scoped NOT EXISTS check.
"""
import re
import pytest
from routers.candidates import (
    _build_feedback_filter_condition,
    _launched_candidates_sql,
    _launched_filter_conditions,
)


# ---------------------------------------------------------------------------
# Helpers — use the shipped filter-condition and statement builders, so these
# tests check the SQL the endpoint actually runs (this file used to keep its
# own copy of the CTE, which silently kept the old cross-job audit lookup).
# ---------------------------------------------------------------------------

def _build_feedback_exists_condition(feedback: str) -> str:
    cond, _ = _build_feedback_filter_condition(feedback)
    return cond


def _shipped_statements(feedback: str = ""):
    """(rows_sql, count_sql) exactly as get_launched_candidates builds them."""
    search, _params, exists_cond, order_by = _launched_filter_conditions(
        None, None, feedback or None, None, None, None, None
    )
    return _launched_candidates_sql(search, exists_cond, order_by)


def _build_full_cte(feedback: str = "") -> str:
    """The shipped rows statement for a feedback filter (no other filters)."""
    return _shipped_statements(feedback)[0]


def _as_spliced(fragment: str) -> str:
    """A filter fragment as it appears in the statement: '%' is escaped at the
    splice because the statement runs with a params tuple."""
    return fragment.replace("%", "%%")


# ---------------------------------------------------------------------------
# Tests: feedback_exists_condition values
# ---------------------------------------------------------------------------

class TestFeedbackExistsConditionGeneration:

    def test_no_filter_produces_empty_string(self):
        assert _build_feedback_exists_condition("") == ""

    def test_no_feedback_checks_null_or_empty(self):
        cond = _build_feedback_exists_condition("No Feedback")
        assert "NOT EXISTS" in cond
        assert "feedback_type" in cond
        assert "IS NOT NULL" in cond
        assert "<> ''" in cond

    def test_no_feedback_case_insensitive(self):
        assert _build_feedback_exists_condition("no feedback") == _build_feedback_exists_condition("No Feedback")

    def test_submit_constrains_current_source_row(self):
        cond = _build_feedback_exists_condition("Submit")
        assert "sc2" not in cond
        assert "'submit'" in cond.lower()

    def test_reject_uses_like(self):
        cond = _build_feedback_exists_condition("Reject")
        assert "sc2" not in cond
        assert "like 'reject%'" in cond.lower()

    def test_rejected_variant_uses_like(self):
        cond = _build_feedback_exists_condition("Rejected")
        assert "sc2" not in cond
        assert "like 'reject%'" in cond.lower()

    def test_unreachable_uses_exact_match(self):
        cond = _build_feedback_exists_condition("Unreachable")
        assert "sc2" not in cond
        assert "'unreachable'" in cond.lower()

    def test_action_filters_reference_current_row(self):
        for value in ["Submit", "Reject", "Unreachable"]:
            cond = _build_feedback_exists_condition(value)
            assert "sc.data" in cond
            assert "sc2" not in cond

    def test_no_feedback_references_sc2(self):
        cond = _build_feedback_exists_condition("No Feedback")
        assert "sc2.candidate_id = sc.candidate_id" in cond
        # Either of the job's keys, not just the row's own raw key.
        assert "sc2.jobdiva_id IN (sc.jobdiva_id, mj.numeric_job_id, mj.job_ref)" in cond


# ---------------------------------------------------------------------------
# Tests: DISTINCT ON ordering — conditional feedback tiebreaker
# ---------------------------------------------------------------------------

ORDER_BY = "ORDER BY COALESCE(mj.job_key, sc.jobdiva_id), sc.candidate_id"
TWIN_GATE = "CASE WHEN COUNT(*) OVER job_person > 1 THEN"


class TestDistinctOnOrdering:

    def test_order_by_uses_created_at_desc(self):
        cte = _build_full_cte()
        assert "sc.created_at DESC" in cte

    def test_no_filter_omits_feedback_tiebreaker(self):
        """Without a feedback filter, the only decision preference is the one
        gated on the person having a second row under the job's other key."""
        cte = _build_full_cte()
        order_section = cte[cte.find(ORDER_BY):]
        between = order_section.split("sc.candidate_id,")[1].split("sc.created_at")[0]
        # Every feedback_type reference sits behind the twin-row gate.
        assert between.count("feedback_type") == 1
        assert between.count(TWIN_GATE) == 2
        assert between.index(TWIN_GATE) < between.index("feedback_type")
        assert between.strip().startswith(TWIN_GATE), (
            "ORDER BY must NOT include the filter tiebreaker when no filter is active"
        )

    def test_with_filter_includes_feedback_tiebreaker(self):
        """When a feedback filter IS active, ORDER BY should prefer rows with
        feedback so the displayed row matches the filter result."""
        for feedback in ["Submit", "Reject", "Unreachable"]:
            cond, _ = _build_feedback_filter_condition(feedback)
            cte = _build_full_cte(feedback)
            order_section = cte[cte.find(ORDER_BY):]
            assert "feedback_type" in order_section, (
                f"ORDER BY must include feedback_type tiebreaker when '{feedback}' filter is active"
            )
            # feedback preference must come BEFORE created_at
            fb_pos = order_section.find("feedback_type")
            created_pos = order_section.find("sc.created_at DESC")
            assert fb_pos < created_pos, (
                f"feedback_type tiebreaker must come before created_at DESC for '{feedback}'"
            )

    def test_feedback_condition_placed_in_where_before_order_by(self):
        """The feedback condition must be in WHERE, before ORDER BY."""
        for feedback in ["Submit", "Reject", "Unreachable", "No Feedback"]:
            cond, _ = _build_feedback_filter_condition(feedback)
            cte = _build_full_cte(feedback)
            where_pos = cte.find("WHERE")
            order_pos = cte.find(ORDER_BY)
            assert where_pos != -1, f"WHERE clause must exist for '{feedback}'"
            assert where_pos < order_pos, (
                f"WHERE must come before ORDER BY for '{feedback}'"
            )
            condition_pos = cte.find(_as_spliced(cond.strip()))
            assert where_pos < condition_pos < order_pos, (
                f"Feedback condition for '{feedback}' must be between WHERE and ORDER BY"
            )


# ---------------------------------------------------------------------------
# Tests: Cross-job data isolation
# ---------------------------------------------------------------------------

class TestCrossJobIsolation:

    def test_distinct_rows_are_scoped_to_job_and_candidate(self):
        """The same candidate can be launched for multiple jobs with different
        feedback; the job is its canonical key, so a person stored under both
        the ref and the numeric job_id is still one row."""
        cte = _build_full_cte()
        assert "DISTINCT ON (COALESCE(mj.job_key, sc.jobdiva_id), sc.candidate_id)" in cte
        assert ORDER_BY + ", " in cte

    def test_no_filter_order_ends_with_newest_source_row(self):
        """Without a filter: job, candidate, the twin-gated decision
        preference, then the newest source row."""
        cte = _build_full_cte()
        order_match = re.search(
            r"ORDER BY COALESCE\(mj\.job_key, sc\.jobdiva_id\),\s*sc\.candidate_id,\s*"
            r"CASE WHEN COUNT\(\*\) OVER job_person > 1 THEN [^\n]+ END DESC NULLS LAST,\s*"
            r"CASE WHEN COUNT\(\*\) OVER job_person > 1 THEN [^\n]+ END DESC NULLS LAST,\s*"
            r"sc\.created_at DESC",
            cte,
        )
        assert order_match is not None

    def test_filtered_order_scopes_tiebreaker(self):
        """The tiebreaker must use the current row, never a cross-table lookup."""
        for feedback in ["Submit", "Reject"]:
            cond, _ = _build_feedback_filter_condition(feedback)
            cte = _build_full_cte(feedback)
            order_section = cte[cte.find(ORDER_BY):]
            # The tiebreaker must reference sc.data (same row), not sc2
            assert "sc.data" in order_section, (
                f"Tiebreaker for '{feedback}' must reference sc.data, not a cross-table join"
            )
            assert "sc2" not in order_section, (
                f"Tiebreaker for '{feedback}' must NOT reference sc2 in ORDER BY"
            )


# ---------------------------------------------------------------------------
# Tests: Shared CTE
# ---------------------------------------------------------------------------

class TestSharedCTE:

    def test_full_cte_contains_launched_candidates(self):
        cte = _build_full_cte()
        assert "launched_candidates AS" in cte

    def test_results_and_count_share_same_cte_body(self):
        for feedback in ["", "Submit", "Reject", "Unreachable", "No Feedback"]:
            rows_sql, count_sql = _shipped_statements(feedback)
            body = rows_sql.split("SELECT * FROM launched_candidates", 1)[0]
            assert count_sql.startswith(body), feedback
            assert "launched_candidates_for_count" not in count_sql

    def test_count_query_uses_shared_launched_candidates(self):
        _, count_sql = _shipped_statements()
        assert "launched_candidates_for_count" not in count_sql
        assert "FROM launched_candidates" in count_sql

    def test_spliced_fragments_leave_no_bare_percent(self):
        """A bare '%' in a statement run with params is read as a placeholder —
        the Rejected filter used to 500 the page with an IndexError."""
        for feedback in ["Submit", "Reject", "Rejected", "Unreachable", "No Feedback"]:
            rows_sql, count_sql = _shipped_statements(feedback)
            for sql in (rows_sql, count_sql):
                stripped = sql.replace("%%", "").replace("%s", "")
                assert "%" not in stripped, feedback


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------

class TestFeedbackFilterEdgeCases:

    def test_unknown_feedback_value_produces_no_condition(self):
        assert _build_feedback_exists_condition("SomeUnknownValue") == ""

    def test_empty_string_produces_no_condition(self):
        assert _build_feedback_exists_condition("") == ""

    def test_submit_filter_uses_exact_match(self):
        cond = _build_feedback_exists_condition("Submit")
        assert "'submit'" in cond.lower()

    def test_reject_like_covers_all_rejection_reasons(self):
        """Backend stores full reason strings like 'Reject - Skills...'; LIKE 'reject%' is correct."""
        cond = _build_feedback_exists_condition("Reject")
        assert "like 'reject%'" in cond.lower()
        assert "= 'reject'" not in cond.lower()
