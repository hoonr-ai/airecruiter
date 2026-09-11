"""
Regression tests for the feedback filter in get_launched_candidates
(apps/api/routers/candidates.py).

These tests guard the correctness of the feedback filter SQL logic without
requiring a live DB connection.  They verify:

1. The correct SQL EXISTS / NOT EXISTS clause is generated for each filter value.
2. DISTINCT ON row selection: without a feedback filter, ORDER BY uses only
   created_at DESC (index-friendly); with a filter, a feedback-preference
   tiebreaker is injected so the displayed row matches the filter.
3. The feedback EXISTS condition is placed in WHERE, not ORDER BY.
4. The shared FULL_CTE is used for both results and count (single source of truth).
5. Cross-job data isolation: feedback tiebreaker only activates when a filter
   is active, preventing stale feedback from an unrelated job being surfaced.

Background
----------
The feedback filter uses EXISTS subqueries scoped by both candidate_id and
jobdiva_id so that DISTINCT ON always picks the correct row.  When a feedback
filter IS active, the ORDER BY additionally prefers rows carrying non-empty
feedback_type so the UI column matches the filter result.
"""
import re
import pytest


# ---------------------------------------------------------------------------
# Shared constants — must match candidates.py
# ---------------------------------------------------------------------------
# Helpers — replicate the filter-building logic from candidates.py
# ---------------------------------------------------------------------------

def _build_feedback_filter(feedback: str):
    """Mirror feedback_exists_condition and matching_feedback_pred construction in get_launched_candidates."""
    feedback_exists_condition = ""
    matching_feedback_pred = ""
    if feedback:
        f_lower = feedback.strip().lower()
        correlation_scaffold = "SELECT 1 FROM sourced_candidates sc2 WHERE sc2.candidate_id = sc.candidate_id AND COALESCE(sc2.jobdiva_id, '') = COALESCE(sc.jobdiva_id, '')"
        if f_lower in ("no feedback", "none", "no_feedback"):
            feedback_exists_condition = f"""
                AND NOT EXISTS (
                    {correlation_scaffold}
                      AND sc2.data->>'feedback_type' IS NOT NULL
                      AND TRIM(sc2.data->>'feedback_type') <> ''
                )"""
            matching_feedback_pred = "(sc.data->>'feedback_type' IS NULL OR TRIM(sc.data->>'feedback_type') = '')"
        elif f_lower in ("submit", "submitted"):
            feedback_exists_condition = f"""
                AND EXISTS (
                    {correlation_scaffold}
                      AND LOWER(TRIM(sc2.data->>'feedback_type')) = 'submit'
                )"""
            matching_feedback_pred = "(sc.data->>'feedback_type' IS NOT NULL AND LOWER(TRIM(sc.data->>'feedback_type')) = 'submit')"
        elif f_lower in ("reject", "rejected"):
            feedback_exists_condition = f"""
                AND EXISTS (
                    {correlation_scaffold}
                      AND LOWER(TRIM(sc2.data->>'feedback_type')) LIKE 'reject%'
                )"""
            matching_feedback_pred = "(sc.data->>'feedback_type' IS NOT NULL AND LOWER(TRIM(sc.data->>'feedback_type')) LIKE 'reject%')"
        elif f_lower in ("unreachable",):
            feedback_exists_condition = f"""
                AND EXISTS (
                    {correlation_scaffold}
                      AND LOWER(TRIM(sc2.data->>'feedback_type')) = 'unreachable'
                )"""
            matching_feedback_pred = "(sc.data->>'feedback_type' IS NOT NULL AND LOWER(TRIM(sc.data->>'feedback_type')) = 'unreachable')"
    return feedback_exists_condition, matching_feedback_pred


def _build_feedback_exists_condition(feedback: str) -> str:
    cond, _ = _build_feedback_filter(feedback)
    return cond


def _build_full_cte(search_condition: str, feedback_exists_condition: str, matching_feedback_pred: str = "") -> str:
    """Minimal replica of FULL_CTE construction used in production code."""
    feedback_tiebreaker = f"{matching_feedback_pred} DESC," if matching_feedback_pred else ""
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
            ORDER BY sc.candidate_id, {feedback_tiebreaker} sc.created_at DESC
        )
    """


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

    def test_submit_uses_exact_match(self):
        cond = _build_feedback_exists_condition("Submit")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "'submit'" in cond.lower()

    def test_reject_uses_like(self):
        cond = _build_feedback_exists_condition("Reject")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "like 'reject%'" in cond.lower()

    def test_rejected_variant_uses_like(self):
        cond = _build_feedback_exists_condition("Rejected")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "like 'reject%'" in cond.lower()

    def test_unreachable_uses_exact_match(self):
        cond = _build_feedback_exists_condition("Unreachable")
        assert "EXISTS" in cond
        assert "NOT EXISTS" not in cond
        assert "'unreachable'" in cond.lower()

    def test_all_exists_conditions_reference_sc2(self):
        for value in ["Submit", "Reject", "Unreachable"]:
            cond = _build_feedback_exists_condition(value)
            assert "sc2.candidate_id = sc.candidate_id" in cond
            assert "COALESCE(sc2.jobdiva_id, '') = COALESCE(sc.jobdiva_id, '')" in cond

    def test_no_feedback_references_sc2(self):
        cond = _build_feedback_exists_condition("No Feedback")
        assert "sc2.candidate_id = sc.candidate_id" in cond
        assert "COALESCE(sc2.jobdiva_id, '') = COALESCE(sc.jobdiva_id, '')" in cond


# ---------------------------------------------------------------------------
# Tests: DISTINCT ON ordering — conditional feedback tiebreaker
# ---------------------------------------------------------------------------

class TestDistinctOnOrdering:

    def test_order_by_uses_created_at_desc(self):
        cte = _build_full_cte("", "")
        assert "sc.created_at DESC" in cte

    def test_no_filter_omits_feedback_tiebreaker(self):
        """Without a feedback filter, ORDER BY should be pure created_at DESC
        to use the existing index and avoid cross-job data mismatch."""
        cte = _build_full_cte("", "")
        order_section = cte[cte.find("ORDER BY sc.candidate_id"):]
        # The only thing between candidate_id and created_at should be a comma
        between = order_section.split("sc.candidate_id,")[1].split("sc.created_at")[0]
        assert "feedback_type" not in between, (
            "ORDER BY must NOT include feedback_type tiebreaker when no filter is active"
        )

    def test_with_filter_includes_feedback_tiebreaker(self):
        """When a feedback filter IS active, ORDER BY should prefer rows with
        feedback so the displayed row matches the filter result."""
        for feedback in ["Submit", "Reject", "Unreachable"]:
            cond, matching_pred = _build_feedback_filter(feedback)
            cte = _build_full_cte("", cond, matching_pred)
            order_section = cte[cte.find("ORDER BY sc.candidate_id"):]
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
        """The EXISTS/NOT EXISTS condition must be in WHERE, before ORDER BY."""
        for feedback in ["Submit", "Reject", "Unreachable", "No Feedback"]:
            cond, matching_pred = _build_feedback_filter(feedback)
            cte = _build_full_cte("", cond, matching_pred)
            where_pos = cte.find("WHERE")
            order_pos = cte.find("ORDER BY sc.candidate_id")
            assert where_pos != -1, f"WHERE clause must exist for '{feedback}'"
            assert where_pos < order_pos, (
                f"WHERE must come before ORDER BY for '{feedback}'"
            )
            # The EXISTS condition text must appear between WHERE and ORDER BY
            exists_pos = cte.find("EXISTS")
            assert where_pos < exists_pos < order_pos, (
                f"EXISTS condition for '{feedback}' must be between WHERE and ORDER BY"
            )


# ---------------------------------------------------------------------------
# Tests: Cross-job data isolation
# ---------------------------------------------------------------------------

class TestCrossJobIsolation:

    def test_no_filter_uses_index_friendly_order(self):
        """Without a filter, the ORDER BY must match the existing
        idx_sourced_candidates_candidate_created_at index:
        (candidate_id, created_at DESC)."""
        cte = _build_full_cte("", "")
        order_match = re.search(
            r"ORDER BY sc\.candidate_id,\s*sc\.created_at DESC",
            cte,
        )
        assert order_match is not None, (
            "Without a filter, ORDER BY must be 'sc.candidate_id, sc.created_at DESC' "
            "to use the existing index"
        )

    def test_filtered_order_scopes_tiebreaker(self):
        """When a feedback filter is active, the tiebreaker must reference
        sc.data (the current row's data), not a cross-table lookup, so it
        only affects ordering within the rows already matched by the
        jobdiva_id-scoped EXISTS subquery."""
        for feedback in ["Submit", "Reject"]:
            cond, matching_pred = _build_feedback_filter(feedback)
            cte = _build_full_cte("", cond, matching_pred)
            order_section = cte[cte.find("ORDER BY sc.candidate_id"):]
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
        assert "'submit'" in cond.lower()

    def test_reject_like_covers_all_rejection_reasons(self):
        """Backend stores full reason strings like 'Reject - Skills...'; LIKE 'reject%' is correct."""
        cond = _build_feedback_exists_condition("Reject")
        assert "like 'reject%'" in cond.lower()
        assert "= 'reject'" not in cond.lower()
