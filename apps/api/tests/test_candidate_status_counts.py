from routers.candidates import _bucket_candidate_status, _match_submitted_id


def _bucket(
    raw_engage_status=None,
    audit_status=None,
    data_interview_id=None,
    audit_interview_id=None,
    raw_engage_score=None,
    raw_engage_candidate_score=None,
    raw_hard_filter_status=None,
):
    return _bucket_candidate_status(
        raw_engage_status,
        audit_status,
        data_interview_id,
        audit_interview_id,
        raw_engage_score,
        raw_engage_candidate_score,
        raw_hard_filter_status,
    )


def test_no_interview_id_is_not_launched_regardless_of_status():
    # Never launched: no interview id in blob or audit → N/A bucket,
    # even when a failed *launch* stamped engage_status='failed'.
    assert _bucket() == "not_launched"
    assert _bucket(raw_engage_status="failed") == "not_launched"
    assert _bucket(raw_engage_status="sent", data_interview_id="   ") == "not_launched"


def test_audit_interview_id_fallback_counts_as_launched():
    # Blob has no interview id but the audit row does (read-side fallback).
    assert _bucket(audit_interview_id="123", raw_engage_status="sent") == "pending"
    # Audit status fallback too: blob status empty → audit status wins.
    assert _bucket(audit_interview_id="123", audit_status="in_progress") == "in_progress"


def test_pending_states():
    assert _bucket(data_interview_id="1", raw_engage_status="sent") == "pending"
    assert _bucket(data_interview_id="1", raw_engage_status="processing") == "pending"
    assert _bucket(data_interview_id="1", raw_engage_status=None) == "pending"


def test_in_progress_is_the_partial_bucket():
    assert _bucket(data_interview_id="1", raw_engage_status="in_progress") == "in_progress"
    assert _bucket(data_interview_id="1", raw_engage_status="in progress") == "in_progress"


def test_completed_buckets_split_by_hard_filter():
    assert _bucket(data_interview_id="1", raw_engage_status="passed") == "pass"
    assert _bucket(data_interview_id="1", raw_engage_status="completed") == "pass"
    assert (
        _bucket(data_interview_id="1", raw_engage_status="completed", raw_hard_filter_status="failed")
        == "fail"
    )
    assert (
        _bucket(data_interview_id="1", raw_engage_status="completed", raw_hard_filter_status="not_hard_filter")
        == "pass"
    )


def test_failed_needs_a_score_to_count_as_fail():
    # Mirrors _format_engage_status: failed without a score stays Pending.
    assert _bucket(data_interview_id="1", raw_engage_status="failed") == "pending"
    assert _bucket(data_interview_id="1", raw_engage_status="failed", raw_engage_score="0") == "fail"
    assert _bucket(data_interview_id="1", raw_engage_status="failed", raw_engage_score="72.5") == "fail"
    # ->> extraction yields strings; candidate_score is the fallback field.
    assert (
        _bucket(data_interview_id="1", raw_engage_status="failed", raw_engage_candidate_score="4")
        == "fail"
    )
    # Garbage score text must not crash — treated as missing.
    assert _bucket(data_interview_id="1", raw_engage_status="failed", raw_engage_score="n/a") == "pending"


def test_match_submitted_id_by_own_or_provisioned_id():
    submitted = {"111": None, "222": None}
    # JobDiva-sourced row: its own candidate_id is the JobDiva id.
    assert _match_submitted_id(submitted, "111", None) == "111"
    # External row: matched via the provisioned jobdiva_candidate_id.
    assert _match_submitted_id(submitted, "linkedin-abc", "222") == "222"
    # Whitespace / int-typed ids still match.
    assert _match_submitted_id(submitted, " 111 ", None) == "111"
    assert _match_submitted_id(submitted, 111, None) == "111"
    assert _match_submitted_id(submitted, "999", "888") is None


def test_match_submitted_id_never_matches_blank():
    # Blank ids must not match anything, and an empty map matches nothing.
    assert _match_submitted_id({"111": None}, "", None) is None
    assert _match_submitted_id({"111": None}, None, "   ") is None
    assert _match_submitted_id({}, "111", "111") is None
