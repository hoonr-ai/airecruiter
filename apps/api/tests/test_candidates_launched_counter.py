"""
Unit tests for the candidates_launched counter semantics used across
the backfill (jobs.py), auto_assign_service, and admin_analytics.

These tests exercise the aggregation logic with in-process data to pin:
  (a) a candidate stored under both key variants (ref + numeric) counts once
  (b) an empty/null engage_interview_id does not count
  (c) two candidates sharing the same interview_id (shared-email edge) count as 1
"""
from typing import Any, Dict, List, Optional


def _count_distinct_interviews(rows: List[Dict[str, Any]]) -> int:
    """Mirror of COUNT(DISTINCT NULLIF(sc.data->>'engage_interview_id', ''))."""
    seen = set()
    for row in rows:
        iid = (row.get("engage_interview_id") or "").strip()
        if iid:
            seen.add(iid)
    return len(seen)


def _simulate_backfill(
    job_ref: str,
    job_num: str,
    sc_rows: List[Dict[str, Any]],
) -> int:
    """Simulate the OR-join backfill: collect all rows for the monitored job
    regardless of which key variant they were stored under, then count distinct
    interviews — mirroring the GROUP BY m.job_id query in jobs.py."""
    job_rows = [r for r in sc_rows if r["jobdiva_id"] in (job_ref, job_num)]
    return _count_distinct_interviews(job_rows)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_candidate_stored_under_both_keys_counts_once():
    """_write_candidate_engage_status stamps the same interview_id on rows under
    both the ref (alphanumeric) and numeric key variants. The OR-join backfill
    must deduplicate them — a single launched candidate should count as 1."""
    sc_rows = [
        {"jobdiva_id": "26-06183", "candidate_id": "C1", "engage_interview_id": "I1"},
        {"jobdiva_id": "31920112", "candidate_id": "C1", "engage_interview_id": "I1"},
    ]
    assert _simulate_backfill("26-06183", "31920112", sc_rows) == 1


def test_empty_engage_interview_id_does_not_count():
    """Candidates that were sourced but never launched have an empty or null
    engage_interview_id and must not inflate the counter."""
    sc_rows = [
        {"jobdiva_id": "26-06183", "candidate_id": "C1", "engage_interview_id": ""},
        {"jobdiva_id": "26-06183", "candidate_id": "C2", "engage_interview_id": None},
        {"jobdiva_id": "26-06183", "candidate_id": "C3", "engage_interview_id": "I1"},
    ]
    assert _simulate_backfill("26-06183", "31920112", sc_rows) == 1


def test_two_candidates_sharing_interview_id_count_as_one():
    """When two candidates share an email they both receive the same interview_id
    (shared-email edge case in engagement.py). The counter measures distinct
    interviews, so they collapse to 1, not 2."""
    sc_rows = [
        {"jobdiva_id": "26-06183", "candidate_id": "C1", "engage_interview_id": "I3"},
        {"jobdiva_id": "26-06183", "candidate_id": "C2", "engage_interview_id": "I3"},
    ]
    assert _simulate_backfill("26-06183", "99999999", sc_rows) == 1


def test_multiple_distinct_interviews_count_correctly():
    """Sanity-check: two candidates with different interview_ids count as 2."""
    sc_rows = [
        {"jobdiva_id": "26-06183", "candidate_id": "C1", "engage_interview_id": "I1"},
        {"jobdiva_id": "26-06183", "candidate_id": "C2", "engage_interview_id": "I2"},
    ]
    assert _simulate_backfill("26-06183", "31920112", sc_rows) == 2


def test_only_rows_for_this_job_are_counted():
    """Rows from a different job must not leak into the count."""
    sc_rows = [
        {"jobdiva_id": "26-06183", "candidate_id": "C1", "engage_interview_id": "I1"},
        {"jobdiva_id": "OTHER-JOB", "candidate_id": "C2", "engage_interview_id": "I2"},
    ]
    assert _simulate_backfill("26-06183", "31920112", sc_rows) == 1
