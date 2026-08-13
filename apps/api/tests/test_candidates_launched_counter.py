"""
Unit tests for the candidates_launched counter semantics used across
the backfill (jobs.py), auto_assign_service, and admin_analytics.

These tests exercise the aggregation logic with in-process data to pin:
  (a) a candidate stored under both key variants (ref + numeric) counts once
  (b) an empty/null engage_interview_id does not count
  (c) two candidates sharing the same interview_id (shared-email edge) count as 1
  (d) jobdiva.local candidates with no phone are filtered out
  (e) jobdiva.local duplicates whose embedded phone matches a real candidate are filtered out
  (f) empty phone digits do not false-positive as duplicates
"""
import re
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


def _mirror_candidates_sql_filter(sc_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Python mirror of the SQL WHERE clauses added in candidates.py to
    exclude dummy jobdiva.local records from the Pair UI rank list and count.

    Rules mirrored:
      1. Skip Auto_*@jobdiva.com synthetic emails.
      2. Skip jobdiva.local candidates with an empty phone column (unreachable).
      3. Skip jobdiva.local applicants whose embedded phone matches a real
         candidate's phone for the same job (phone-duplicate).
         Guard: both sides must have non-empty digit strings to avoid '' = '' match.
    """
    filtered = []

    # Pre-process: normalise phones once to avoid repeated regex calls.
    for row in sc_rows:
        row["_norm_phone"] = re.sub(r"\D", "", row.get("phone") or "")
        email = row.get("email") or ""
        local_part = email.split("@")[0] if "@" in email else ""
        row["_norm_email_phone"] = re.sub(r"\D", "", local_part)

    for row in sc_rows:
        email = row.get("email") or ""
        norm_phone = row["_norm_phone"]

        # Rule 1: Skip Auto_*@jobdiva.com
        if re.search(r"Auto!_.*@jobdiva\.com", email, re.IGNORECASE):
            continue

        # Rule 2: Skip jobdiva.local with empty phone column
        if "jobdiva.local" in email.lower() and norm_phone == "":
            continue

        # Rule 3: Skip jobdiva.local whose embedded phone matches a real candidate
        is_duplicate = False
        embedded_phone = row["_norm_email_phone"]
        if "jobdiva.local" in email.lower() and embedded_phone != "":
            for other in sc_rows:
                if other["candidate_id"] == row["candidate_id"]:
                    continue
                other_email = other.get("email") or ""
                other_norm_phone = other["_norm_phone"]
                if (
                    "jobdiva.local" not in other_email.lower()
                    and other_norm_phone != ""  # Guard: sc2.phone must be non-empty
                    and other_norm_phone == embedded_phone
                ):
                    is_duplicate = True
                    break
        if is_duplicate:
            continue

        filtered.append(row)

    return filtered


# ---------------------------------------------------------------------------
# Original counter tests
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


# ---------------------------------------------------------------------------
# jobdiva.local filter tests (mirrors candidates.py SQL WHERE clauses)
# ---------------------------------------------------------------------------

def test_jobdiva_local_with_no_phone_is_filtered():
    """Candidates with a dummy jobdiva.local email and an empty phone column
    are unreachable placeholders and must be filtered out."""
    sc_rows = [
        {"candidate_id": "C1", "email": "pair-1234567890@no-email.jobdiva.local", "phone": ""},
        {"candidate_id": "C2", "email": "real@example.com", "phone": "1234567890"},
    ]
    filtered = _mirror_candidates_sql_filter(sc_rows)
    assert len(filtered) == 1
    assert filtered[0]["candidate_id"] == "C2"


def test_jobdiva_local_duplicate_is_filtered():
    """jobdiva.local applicants whose embedded phone matches a real candidate's
    DB phone must be filtered out as phone-duplicates."""
    sc_rows = [
        {"candidate_id": "C1", "email": "pair-1234567890@no-email.jobdiva.local", "phone": ""},  # dup
        {"candidate_id": "C2", "email": "real@example.com", "phone": "1234567890"},  # real
    ]
    filtered = _mirror_candidates_sql_filter(sc_rows)
    assert len(filtered) == 1
    assert filtered[0]["candidate_id"] == "C2"


def test_empty_phone_digit_false_positive_guard():
    """Ensure that '' = '' in SQL does not incorrectly mark an unrelated real
    candidate (with no phone on file) as a duplicate of a dummy record with no
    digits in its email local part.

    Before the guard, both sides normalize to '' → equality satisfied → valid
    candidate incorrectly hidden. With the guard (embedded_phone != '' AND
    other_norm_phone != ''), the match is skipped.
    """
    sc_rows = [
        {"candidate_id": "C1", "email": "pair-empty@no-email.jobdiva.local", "phone": ""},  # no digits in local part
        {"candidate_id": "C2", "email": "real@example.com", "phone": ""},  # real candidate, no phone
    ]
    filtered = _mirror_candidates_sql_filter(sc_rows)
    # C1 is filtered by Rule 2 (jobdiva.local + empty phone column).
    # C2 must NOT be hidden — Rule 3 must not trigger because embedded_phone is empty.
    assert len(filtered) == 1
    assert filtered[0]["candidate_id"] == "C2"


def test_valid_jobdiva_candidate_with_real_phone_passes():
    """A jobdiva.local candidate that has a real phone number in the DB column
    (not just embedded in the email) should pass through the filter."""
    sc_rows = [
        {"candidate_id": "C1", "email": "pair-1234567890@no-email.jobdiva.local", "phone": "1234567890"},
        {"candidate_id": "C2", "email": "real@example.com", "phone": "9876543210"},
    ]
    filtered = _mirror_candidates_sql_filter(sc_rows)
    assert len(filtered) == 2
