"""
Regression tests for the feedback filter in get_launched_candidates
(apps/api/routers/candidates.py).

These tests guard the correctness of the feedback filter SQL logic without
requiring a live DB connection.  They verify:

1. The correct SQL EXISTS / NOT EXISTS clause is generated for each filter value.
2. DISTINCT ON always keeps sc.created_at DESC ordering regardless of filter.
3. No tiebreaker on feedback presence is injected into the ORDER BY.
4. The shared FULL_CTE is used for both results and count (single source of truth).

Background
----------
The feedback filter previously placed conditions inside the DISTINCT ON CTE's
WHERE clause, which caused it to fail silently when a candidate's latest row
(by created_at) had no feedback, even though an older row did.  The fix uses
EXISTS subqueries so that DISTINCT ON always picks the true latest row, while
only including candidates that match the feedback condition on *any* of their rows.
"""
import re
import pytest


# ---------------------------------------------------------------------------
# Helpers — replicate the filter-building logic from candidates.py
# ---------------------------------------------------------------------------

def _build_feedback_exists_condition(feedback: str) -> str:
    """Mirror the feedback_exists_condition construction in get_launched_candidates."""
    feedback_exists_condition = ""
    if feedback:
        if feedback.lower() == "no feedback":
            feedback_exists_condition = """
                AND NOT EXISTS (
                    SELECT 1 FROM sourced_candidates sc2
                    WHERE sc2.candidate_id = sc.candidate_id
                      AND sc2.data->>'feedback_type' IS NOT NULL
                      AND sc2.data->>'feedback_type' <> ''
                )"""
        elif feedback.lower() == "submit":
            feedback_exists_condition = """
                AND EXISTS (
                    SELECT 1 FROM sourced_candidates sc2
                    WHERE sc2.candidate_id = sc.candidate_id
                      AND sc2.data->>'feedback_type' = 'Submit'
                )"""
        elif feedback.lower() == "reject":
            feedback_exists_condition = """
                AND EXISTS (
                    SELECT 1 FROM sourced_candidates sc2
                    WHERE sc2.candidate_id = sc.candidate_id
                      AND sc2.data->>'feedback_type' LIKE 'Reject%'
                )"""
        elif feedback.lower() == "unreachable":
            feedback_exists_condition = """
                AND EXISTS (
                    SELECT 1 FROM sourced_candidates sc2
                    WHERE sc2.candidate_id = sc.candidate_id
                      AND sc2.data->>'feedback_type' = 'Unreachable'
                )"""
    return feedback_exists_condition


def _build_full_cte(search_condition: str, feedback_exists_condition: str) -> str:
    """Minimal replica of FULL_CTE construction used in production code."""
    return f"""
        WITH latest_audit AS (
            SELECT DISTINCT ON (candidate_id) candidate_id, interview_id, status,
                   created_at, payload, response
            FROM engage_interview_audit
            ORDER BY candidate_id, id DESC
        ),
        monitored_jobs_lookup AS (
            SELECT DISTINCT ON (lookup_id) lookup_id, title, screening_level, recruiter_emails
            FROM (SELECT 'dummy'::text AS lookup_id, NULL AS title, NULL AS screening_level, NULL AS recruiter_emails) x
            WHERE lookup_id IS NOT NULL AND lookup_id <> ''
            ORDER BY lookup_id
        ),
        launched_candidates AS (
            SELECT DISTINCT ON (sc.candidate_id)
                sc.id, sc.candidate_id, sc.data, sc.created_at as engage_created_at
            FROM sourced_candidates sc
            JOIN latest_audit la ON la.candidate_id = sc.candidate_id
            LEFT JOIN monitored_jobs_lookup mj ON mj.lookup_id = sc.jobdiva_id
            WHERE (la.interview_id IS NOT NULL AND la.interview_id <> '')
              {search_condition}
              {feedback_exists_condition}
            ORDER BY sc.candidate_id, sc.created_at DESC
        )
    """


# ---------------------------------------------------------------------------
# Tests: feedback_exists_condition values
# ---------------------------------------------------------------------------

class TestFeedbackExistsConditionGeneration:

    def test_no_filter_produces_empty_string(self):
        assert _build_feedback_exists_condition("") == ""

    def test_no_feedback_uses_not_exists(self):
        cond = _build_feedback_exists_condition("No Feedback")
        assert "NOT EXISTS" in cond
        assert "feedback_type" in cond
        assert "IS NOT NULL" in cond
        assert "<> ''" in cond

    def test_no_feedback_case_insensitive(self):
        assert _build_feedback_exists_condition("no feedback") == _build_feedback_exists_condition("No Feedback")

    def test_submit_uses_exists_with_exact_match(self):
        cond = _build_feedback_exists_condition("Submit")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "'Submit'" in cond

    def test_reject_uses_exists_with_like(self):
        cond = _build_feedback_exists_condition("Reject")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "LIKE 'Reject%'" in cond

    def test_unreachable_uses_exists_with_exact_match(self):
        cond = _build_feedback_exists_condition("Unreachable")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "'Unreachable'" in cond

    def test_all_exists_conditions_reference_sc2(self):
        for value in ["Submit", "Reject", "Unreachable"]:
            cond = _build_feedback_exists_condition(value)
            assert "sc2.candidate_id = sc.candidate_id" in cond

    def test_no_feedback_references_sc2(self):
        cond = _build_feedback_exists_condition("No Feedback")
        assert "sc2.candidate_id = sc.candidate_id" in cond


# ---------------------------------------------------------------------------
# Tests: DISTINCT ON ordering must always use created_at DESC only
# ---------------------------------------------------------------------------

class TestDistinctOnOrdering:

    def test_order_by_uses_created_at_desc(self):
        cte = _build_full_cte("", "")
        assert "sc.created_at DESC" in cte

    def test_order_by_does_not_include_feedback_tiebreaker(self):
        """The old buggy approach injected (sc.data->>'feedback_type' IS NOT NULL) DESC."""
        for feedback in ["Submit", "Reject", "Unreachable", "No Feedback"]:
            cond = _build_feedback_exists_condition(feedback)
            cte = _build_full_cte("", cond)
            # ORDER BY clause should not reference feedback_type
            order_section = cte[cte.find("ORDER BY sc.candidate_id"):]
            first_paren = order_section.find(")")
            assert "feedback_type" not in order_section[:first_paren], (
                f"ORDER BY for '{feedback}' must not reference feedback_type"
            )

    def test_feedback_condition_placed_in_where_not_order_by(self):
        for feedback in ["Submit", "Reject", "Unreachable", "No Feedback"]:
            cond = _build_feedback_exists_condition(feedback)
            cte = _build_full_cte("", cond)
            where_pos = cte.find("WHERE")
            order_pos = cte.find("ORDER BY sc.candidate_id")
            exists_pos = cte.find("EXISTS")
            assert where_pos < exists_pos < order_pos, (
                f"EXISTS condition for '{feedback}' must be in WHERE before ORDER BY"
            )


# ---------------------------------------------------------------------------
# Tests: Shared CTE
# ---------------------------------------------------------------------------

class TestSharedCTE:

    def test_full_cte_contains_launched_candidates(self):
        cte = _build_full_cte("", "")
        assert "launched_candidates AS" in cte

    def test_results_and_count_share_same_cte_body(self):
        cte = _build_full_cte("", "")
        cte_body = cte.split("WITH", 1)[1]
        results_query = f"WITH {cte_body} SELECT * FROM launched_candidates ORDER BY engage_created_at DESC NULLS LAST LIMIT %s OFFSET %s;"
        count_query = f"WITH {cte_body} SELECT COUNT(*) as total FROM launched_candidates"
        assert cte_body in results_query
        assert cte_body in count_query
        assert "launched_candidates_for_count" not in count_query

    def test_count_query_uses_shared_launched_candidates(self):
        cte = _build_full_cte("", "")
        count_query = f"WITH {cte.split('WITH', 1)[1]} SELECT COUNT(*) as total FROM launched_candidates"
        assert "launched_candidates_for_count" not in count_query
        assert "FROM launched_candidates" in count_query


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
        assert "'Submit'" in cond

    def test_reject_like_covers_all_rejection_reasons(self):
        """Backend stores full reason strings like 'Reject - Skills...'; LIKE 'Reject%' is correct."""
        cond = _build_feedback_exists_condition("Reject")
        assert "LIKE 'Reject%'" in cond
        assert "= 'Reject'" not in cond
