"""Cross submissions — selection rules, identity, email, endpoint guards.

No DB: `select_candidates` is pure; `run_for_job` is exercised with a mocked
connection; the router is checked with `ast` the same way
test_candidates_router_auth.py pins the candidates router.
"""
import ast
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services import cross_submissions as cs

API_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=60)


def _row(
    *,
    candidate_id="c1",
    source="JobDiva",
    email="a@example.com",
    phone="",
    prior_key="26-11111",
    prior_jobdiva_id=None,
    engage_status="passed",
    days_ago=5,
    hf=None,
    score=8,
    total=10,
    name="Ada",
    extra_blob=None,
):
    blob = {
        "engage_status": engage_status,
        "engage_updated_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "engage_score": score,
        "engage_total_score": total,
    }
    if hf is not None:
        blob["engage_hard_filter_status"] = hf
    if extra_blob:
        blob.update(extra_blob)
    return {
        "id": 1,
        "prior_key": prior_key,
        "candidate_id": candidate_id,
        "source": source,
        "name": name,
        "email": email,
        "phone": phone,
        "headline": "Data Engineer",
        "location": "Austin, TX",
        "resume_text": "python spark",
        "data": json.dumps(blob),
        "resume_match_percentage": 70,
        "updated_at": NOW - timedelta(days=days_ago),
        "prior_job_id": "1001",
        "prior_jobdiva_id": prior_jobdiva_id or prior_key,
        "prior_title": "Senior Data Engineer",
        "prior_enhanced_title": None,
        "prior_customer_name": "Acme",
    }


def _select(rows, scorer=None, **kw):
    defaults = dict(
        new_job_base_refs={"26-22222"},
        existing_person_keys=set(),
        cutoff=CUTOFF,
        scorer=scorer if scorer is not None else (lambda payload, criteria: {"score": 80}),
        criteria=object(),
    )
    defaults.update(kw)
    return cs.select_candidates(rows, **defaults)


# ---------------------------------------------------------------------------
# Identity + labels
# ---------------------------------------------------------------------------
def test_person_key_prefers_email_then_phone_then_source_id():
    assert cs.person_key("A@X.com", "415-555-0100") == "email:a@x.com"
    assert cs.person_key("", "+1 (415) 555-0100") == "phone:4155550100"
    assert cs.person_key("Auto_123@jobdiva.com", "", "JobDiva", "77") == "id:jobdiva:77"
    assert cs.person_key("Available upon request", "12345", "LinkedIn", "u1") == "id:linkedin:u1"


def test_base_job_ref_strips_version_suffix():
    assert cs.base_job_ref("26-06182-v2") == "26-06182"
    assert cs.base_job_ref("26-06182") == "26-06182"
    assert cs.base_job_ref("26-06182-V10") == "26-06182"
    assert cs.base_job_ref(None) == ""


def test_screen_result_label_matches_rank_list_vocabulary():
    assert cs.screen_result_label("passed") == "Pass"
    assert cs.screen_result_label("failed", None, 3) == "Fail"       # a scored screen that failed
    assert cs.screen_result_label("in_progress") == "In Progress"
    assert cs.screen_result_label("completed", "passed") == "Pass"
    assert cs.screen_result_label("completed", "failed") == "Fail"
    assert cs.screen_result_label("sent") == "Pending"


@pytest.mark.parametrize("raw", ["failed", "fail", "rejected", "FAILED", " failed "])
def test_a_failed_outreach_with_no_score_is_pending_not_fail(raw):
    # Pairbot only writes a score for a screen that was completed. A raw
    # "failed" with none is an outreach miss — the rank list reads it Pending,
    # and so must this list (it used to say Fail).
    assert cs.screen_result_label(raw) == "Pending"
    assert cs.screen_result_label(raw, None, None) == "Pending"
    assert cs.screen_result_label(raw, None, "") == "Pending"
    assert cs.screen_result_label(raw, None, 0) == "Fail"            # a 0 IS a score


def test_label_never_drifts_from_the_shared_rank_list_rule():
    from services.engage_status import format_engage_status

    for raw in (*cs.RESPONDED_STATUSES, "sent", "processing", "queued", "", None):
        for score in (None, 0, 3.5):
            for hf in (None, "", "passed", "failed"):
                expected = format_engage_status(str(raw or "").strip().lower(), None if score is None else float(score), hf or "")
                assert cs.screen_result_label(raw, hf, score) == expected, (raw, score, hf)


def test_parse_ts_handles_iso_z_and_naive():
    assert cs.parse_ts("2026-09-01T10:00:00Z") == datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    assert cs.parse_ts("2026-09-01 10:00:00") == datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    assert cs.parse_ts("") is None
    assert cs.parse_ts("garbage") is None


def test_format_screen_score():
    assert cs.format_screen_score(8, 10) == "80%"
    assert cs.format_screen_score(7.5, None) == "7.5"
    assert cs.format_screen_score(None, 10) == ""


# ---------------------------------------------------------------------------
# Selection rules
# ---------------------------------------------------------------------------
def test_only_responded_statuses_are_listed():
    rows = [
        _row(candidate_id="a", email="a@x.com", engage_status="passed"),
        _row(candidate_id="b", email="b@x.com", engage_status="in_progress"),
        _row(candidate_id="c", email="c@x.com", engage_status="failed"),     # scored → a real Fail, 80 < the 90 bar
        _row(candidate_id="d", email="d@x.com", engage_status="sent"),       # launched, never answered
        _row(candidate_id="e", email="e@x.com", engage_status="processing"),
        _row(candidate_id="f", email="f@x.com", engage_status=None),
        _row(candidate_id="g", email="g@x.com", engage_status="failed", score=None, total=None),  # outreach miss
    ]
    got = {c["candidate_id"]: c["screen_result"] for c in _select(rows)}
    assert got == {"a": "Pass", "b": "In Progress", "g": "Pending"}


# ---------------------------------------------------------------------------
# Who may be listed: a past Pass, a strong undecided match, or an exceptional failed one
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("result, score, expected", [
    ("Pass", 60, True), ("Pass", 59.9, False), ("Pass", 95, True),
    ("Pending", 79.9, False), ("Pending", 80, True), ("Pending", 100, True),
    ("In Progress", 79.9, False), ("In Progress", 80, True),
    ("Fail", 100, True), ("Fail", 90, True), ("Fail", 89.9, False), ("Fail", 85, False),
    ("Pass", None, False), ("Pending", None, False), ("Fail", None, False),  # Step-5 "N/A": no number to vouch with
    ("Pass", "junk", False),
    ("", 99, False), (None, 99, False), ("Mystery", 99, False),
])
def test_qualifies_for_list_by_screen_outcome(result, score, expected):
    assert cs.qualifies_for_list(result, score, min_score=60, undecided_min_score=80, failed_min_score=90) is expected


def test_the_undecided_and_failed_bars_are_never_below_the_floor():
    # If the floor is tuned above a bar, that outcome must not be the looser of the two.
    assert cs.qualifies_for_list("Pending", 85, min_score=90, undecided_min_score=80) is False
    assert cs.qualifies_for_list("Pending", 90, min_score=90, undecided_min_score=80) is True
    assert cs.qualifies_for_list("Fail", 92, min_score=95, failed_min_score=90) is False
    assert cs.qualifies_for_list("Fail", 95, min_score=95, failed_min_score=90) is True


def test_each_screen_outcome_has_its_own_match_bar():
    scores = {
        "pass_lo": 60, "pass_below": 59, "pend_ok": 80, "pend_lo": 79, "prog_ok": 91, "prog_lo": 70,
        "fail_hi": 99, "fail_edge": 90, "fail_lo": 89,
    }
    rows = [
        _row(candidate_id="pass_lo", email="1@x.com", engage_status="passed"),
        _row(candidate_id="pass_below", email="2@x.com", engage_status="passed"),
        _row(candidate_id="pend_ok", email="3@x.com", engage_status="failed", score=None, total=None),
        _row(candidate_id="pend_lo", email="4@x.com", engage_status="failed", score=None, total=None),
        _row(candidate_id="prog_ok", email="5@x.com", engage_status="in_progress"),
        _row(candidate_id="prog_lo", email="6@x.com", engage_status="in_progress"),
        _row(candidate_id="fail_hi", email="7@x.com", engage_status="failed", score=2, total=10),
        _row(candidate_id="fail_edge", email="8@x.com", engage_status="failed", score=2, total=10),
        _row(candidate_id="fail_lo", email="9@x.com", engage_status="failed", score=2, total=10),
    ]

    def scorer(payload, criteria):
        return {"score": scores[payload["candidate_id"]]}

    got = _select(rows, scorer=scorer, min_score=60, undecided_min_score=80, failed_min_score=90)
    assert [(c["candidate_id"], c["screen_result"]) for c in got] == [
        ("fail_hi", "Fail"),            # still labelled Fail — only listed because the match is exceptional
        ("prog_ok", "In Progress"),
        ("fail_edge", "Fail"),
        ("pend_ok", "Pending"),
        ("pass_lo", "Pass"),
    ]


def test_a_past_pass_beats_a_more_recent_undecided_screen():
    rows = [
        _row(candidate_id="old", email="p@x.com", prior_key="26-11111", engage_status="passed", days_ago=40),
        _row(candidate_id="new", email="p@x.com", prior_key="26-44444", engage_status="in_progress", days_ago=2),
    ]
    (c,) = _select(rows)
    assert c["screen_result"] == "Pass" and c["prior_jobdiva_id"] == "26-11111"


def test_a_past_pass_beats_a_more_recent_fail():
    rows = [
        _row(candidate_id="old", email="p@x.com", prior_key="26-11111", engage_status="passed", days_ago=40),
        _row(candidate_id="new", email="p@x.com", prior_key="26-44444", engage_status="failed", score=2, days_ago=1),
    ]
    (c,) = _select(rows)
    assert c["screen_result"] == "Pass"


def test_without_a_pass_the_most_recent_screen_decides():
    # An older undecided row must not resurrect someone whose latest screen was a
    # Fail: at 80 the Fail misses its 90 bar, and the older 80%-qualifying row is not used instead.
    rows = [
        _row(candidate_id="old", email="p@x.com", prior_key="26-11111", engage_status="in_progress", days_ago=30),
        _row(candidate_id="new", email="p@x.com", prior_key="26-44444", engage_status="failed", score=2, days_ago=1),
    ]
    assert _select(rows) == []
    # …and when the match is exceptional the person is listed as the Fail they last were.
    (c,) = _select(rows, scorer=lambda p, crit: {"score": 95})
    assert c["screen_result"] == "Fail" and c["prior_jobdiva_id"] == "26-44444"


# ---------------------------------------------------------------------------
# Rows already stored (the label + eligibility are re-applied on read)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stored, status, score, expected", [
    ("Fail", "failed", None, "Pending"),      # the bug: an outreach miss stamped Fail
    ("Fail", "fail", None, "Pending"),
    ("Fail", "rejected", "", "Pending"),
    ("Fail", "failed", 3, "Fail"),            # a scored Fail stays a Fail
    ("Fail", "failed", 0, "Fail"),
    ("Fail", "completed", None, "Fail"),      # hard-filter fail: not re-derivable, never touched
    ("Pass", "passed", 8, "Pass"),
    ("In Progress", "in_progress", None, "In Progress"),
    ("Pending", "sent", None, "Pending"),
    (None, None, None, "Pending"),
    ("", "failed", None, "Pending"),
])
def test_effective_screen_result_corrects_a_stored_outreach_miss(stored, status, score, expected):
    assert cs.effective_screen_result(stored, status, score) == expected


def _list_with(rows):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = JOB
    cur.fetchall.return_value = rows
    with patch.object(cs, "get_db_connection", return_value=conn):
        return cs.list_for_job("26-22222")


def _stored(id, result, status, match, *, eng_score=None):
    return {"id": id, "candidate_id": f"c{id}", "source": "JobDiva", "prior_jobdiva_candidate_id": None,
            "screened_at": NOW, "screen_result": result, "engage_status": status,
            "engage_score": eng_score, "match_score": match}


def test_list_for_job_applies_the_per_outcome_match_bars():
    rows = _list_with([
        _stored(1, "Pass", "passed", 65, eng_score=8),
        _stored(2, "Fail", "failed", 91),                  # old row: really an outreach miss → Pending, 91 ≥ 80
        _stored(3, "Fail", "failed", 72),                  # …but at 72 not strong enough
        _stored(4, "Fail", "failed", 99, eng_score=2),     # a real Fail, exceptional match → shown as Fail
        _stored(5, "In Progress", "in_progress", 80),
        _stored(6, "In Progress", "in_progress", 79),
        _stored(7, "Pass", "passed", 50, eng_score=8),     # below the relevance floor
        _stored(8, "Fail", "failed", 89, eng_score=2),     # a real Fail, but under the 90 bar
    ])
    assert [(r["id"], r["screen_result"]) for r in rows] == [
        (1, "Pass"), (2, "Pending"), (4, "Fail"), (5, "In Progress"),
    ]


def test_a_pending_row_is_never_shown_as_failed_in_the_list():
    rows = _list_with([_stored(i, "Fail", "failed", 85 + i) for i in range(5)])
    assert rows and {r["screen_result"] for r in rows} == {"Pending"}


def test_added_blob_records_the_corrected_screen_result():
    row = {**CS_ROW, "screen_result": "Fail", "engage_status": "failed", "engage_score": None}
    assert cs.build_added_blob({}, row)["cross_submission"]["screen_result"] == "Pending"


def test_rows_outside_lookback_window_are_dropped():
    rows = [
        _row(candidate_id="recent", email="r@x.com", days_ago=59),
        _row(candidate_id="old", email="o@x.com", days_ago=61),
    ]
    assert [c["candidate_id"] for c in _select(rows)] == ["recent"]


def test_screened_at_prefers_completed_at_over_updated_at():
    completed = (NOW - timedelta(days=3)).isoformat()
    rows = [_row(candidate_id="a", email="a@x.com", days_ago=1, extra_blob={"engage_completed_at": completed})]
    (c,) = _select(rows)
    assert c["screened_at"] == cs.parse_ts(completed)


def test_same_job_and_its_versions_are_excluded():
    rows = [
        _row(candidate_id="same", email="s@x.com", prior_key="26-22222"),
        _row(candidate_id="v2", email="v@x.com", prior_key="26-22222-v2"),
        _row(candidate_id="numeric_key", email="n@x.com", prior_key="1001", prior_jobdiva_id="26-22222-v3"),
        _row(candidate_id="other", email="o@x.com", prior_key="26-33333"),
    ]
    assert [c["candidate_id"] for c in _select(rows)] == ["other"]


def test_persons_already_on_the_new_job_are_excluded():
    rows = [
        _row(candidate_id="by_email", email="dup@x.com"),
        _row(candidate_id="by_phone", email="", phone="(415) 555-0100"),
        _row(candidate_id="by_id", email="Auto_1@jobdiva.com", source="JobDiva"),
        _row(candidate_id="fresh", email="fresh@x.com"),
    ]
    existing = {"email:dup@x.com", "phone:4155550100", "id:jobdiva:by_id"}
    assert [c["candidate_id"] for c in _select(rows, existing_person_keys=existing)] == ["fresh"]


def test_same_person_across_jobs_is_listed_once_with_latest_screen():
    rows = [
        _row(candidate_id="j1", email="p@x.com", prior_key="26-11111", engage_status="failed", days_ago=20),
        _row(candidate_id="j2", email="p@x.com", prior_key="26-44444", engage_status="passed", days_ago=2),
    ]
    got = _select(rows)
    assert len(got) == 1
    assert got[0]["prior_jobdiva_id"] == "26-44444"
    assert got[0]["screen_result"] == "Pass"


def test_dnc_and_synthetic_contact_shape():
    # Synthetic JobDiva emails never surface as a contact, the row still lists.
    rows = [_row(candidate_id="a", email="Auto_9@jobdiva.com", phone="5125550100")]
    (c,) = _select(rows)
    assert c["email"] == ""
    assert c["phone"] == "5125550100"


@pytest.mark.parametrize("source, candidate_id, blob, expected", [
    ("JobDiva-TalentSearch", "123", {}, "123"),                                # own id IS the profile
    ("JobDiva", "123", {"jobdiva_candidate_id": "999"}, "123"),                # own id beats a stored one
    ("LinkedIn-Exa", "exa_linkedin.com/in/x", {"jobdiva_candidate_id": "456"}, "456"),  # profile Launch PAIR made
    ("LinkedIn", "789", {}, ""),                                               # numeric-looking, not JobDiva
    ("LinkedIn", "789", {"jobdiva_candidate_id": "789"}, "789"),               # emitter stamp proves it
    ("Dice", "d1", {"jobdiva_candidate_id": "Auto_1"}, ""),                    # stored value not an id
    ("Dice", "d1", None, ""),
])
def test_jobdiva_profile_id_for_follows_launch_trust_order(source, candidate_id, blob, expected):
    assert cs.jobdiva_profile_id_for(source, candidate_id, blob) == expected


def test_selected_candidates_carry_the_jobdiva_profile_id():
    rows = [
        _row(candidate_id="111", source="JobDiva-Applicants", email="a@x.com"),
        _row(candidate_id="exa_linkedin.com/in/b", source="LinkedIn-Exa", email="b@x.com",
             extra_blob={"jobdiva_candidate_id": "222"}),
        _row(candidate_id="c3", source="Dice", email="c@x.com"),
    ]
    got = {c["candidate_id"]: c["jobdiva_candidate_id"] for c in _select(rows)}
    assert got == {"111": "111", "exa_linkedin.com/in/b": "222", "c3": ""}


def test_list_for_job_reads_jobdiva_profile_id_from_the_prior_row():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = JOB
    cur.fetchall.return_value = [
        {"id": 1, "candidate_id": "exa_x", "source": "LinkedIn-Exa", "prior_jobdiva_candidate_id": "555",
         "screened_at": NOW, "screen_result": "Pass", "match_score": 80},
        {"id": 2, "candidate_id": "321", "source": "JobDiva", "prior_jobdiva_candidate_id": None,
         "screened_at": NOW, "screen_result": "Pass", "match_score": 70},  # prior row gone: a JobDiva row's own id still works
    ]
    with patch.object(cs, "get_db_connection", return_value=conn):
        rows = cs.list_for_job("26-22222")
    assert [r["jobdiva_candidate_id"] for r in rows] == ["555", "321"]
    assert all("prior_jobdiva_candidate_id" not in r for r in rows)
    sql = cur.execute.call_args_list[-1].args[0]
    assert "LEFT JOIN LATERAL" in sql and "sc.jobdiva_id IN (cs.prior_jobdiva_id, cs.prior_job_id)" in sql


def test_jobs_sharing_screen_report_matches_the_exact_prior_job():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = {"job_id": "1001", "jobdiva_id": "26-11111", "parent_job_id": "26-00001"}
    cur.fetchall.return_value = [{"job_ref": "26-22222"}, {"job_ref": None}]
    with patch.object(cs, "get_db_connection", return_value=conn):
        refs = cs.jobs_sharing_screen_report("26-11111", "c1")
    assert refs == ["26-22222"]
    sql, params = cur.execute.call_args_list[-1].args
    assert "FROM cross_submissions" in sql
    assert params[0] == "c1"
    assert params[1] == params[2] == ["26-11111", "1001"]  # ref + numeric id; never the parent version
    conn.close.assert_called_once()


def test_relevance_floor_ranking_and_cap():
    scores = {"hi": 91, "mid": 65, "low": 42, "edge": 60}
    rows = [_row(candidate_id=k, email=f"{k}@x.com") for k in scores]

    def scorer(payload, criteria):
        return {"score": scores[payload["candidate_id"]], "matched_skills": ["python"], "missing_skills": []}

    got = _select(rows, scorer=scorer, min_score=60)
    assert [c["candidate_id"] for c in got] == ["hi", "mid", "edge"]
    assert got[0]["match_score"] == 91
    assert got[0]["matched_skills"] == ["python"]

    capped = _select(rows, scorer=scorer, min_score=60, max_listed=2)
    assert [c["candidate_id"] for c in capped] == ["hi", "mid"]


def test_scoring_failure_on_one_row_does_not_sink_the_list():
    rows = [_row(candidate_id="boom", email="b@x.com"), _row(candidate_id="ok", email="o@x.com")]

    def scorer(payload, criteria):
        if payload["candidate_id"] == "boom":
            raise RuntimeError("scorer exploded")
        return {"score": 75}

    assert [c["candidate_id"] for c in _select(rows, scorer=scorer)] == ["ok"]


def test_max_scored_budget_takes_most_recent_first():
    rows = [
        _row(candidate_id="old", email="o@x.com", days_ago=40),
        _row(candidate_id="new", email="n@x.com", days_ago=1),
    ]
    got = _select(rows, max_scored=1)
    assert [c["candidate_id"] for c in got] == ["new"]


def test_prescreen_runs_before_scoring_and_drops_rejected_rows():
    rows = [_row(candidate_id="qa", email="q@x.com"), _row(candidate_id="da", email="d@x.com")]
    scored = []

    def scorer(payload, criteria):
        scored.append(payload["candidate_id"])
        return {"score": 90}

    def gate(payload, criteria):
        return (payload["candidate_id"] == "da", "data analyst ~ senior data analyst" if payload["candidate_id"] == "da" else "title_mismatch")

    stats = {}
    got = _select(rows, scorer=scorer, prescreen=gate, stats=stats)
    assert [c["candidate_id"] for c in got] == ["da"]
    assert scored == ["da"]  # rejected rows are never scored
    assert got[0]["role_match"] == "data analyst ~ senior data analyst"
    assert stats == {"gated_out": 1, "gated_reasons": {"title_mismatch": 1}, "scored": 1}


def test_prescreen_error_fails_closed_for_that_row_only():
    rows = [_row(candidate_id="boom", email="b@x.com"), _row(candidate_id="ok", email="o@x.com")]

    def gate(payload, criteria):
        if payload["candidate_id"] == "boom":
            raise RuntimeError("taxonomy down")
        return True, "match"

    stats = {}
    got = _select(rows, prescreen=gate, stats=stats)
    assert [c["candidate_id"] for c in got] == ["ok"]
    assert stats["gated_reasons"] == {"prescreen_error": 1}


def test_gated_out_rows_do_not_consume_the_scoring_budget():
    rows = [
        _row(candidate_id="new-offrole", email="n@x.com", days_ago=1),
        _row(candidate_id="older-onrole", email="o@x.com", days_ago=20),
    ]
    got = _select(rows, prescreen=lambda p, c: (p["candidate_id"] == "older-onrole", ""), max_scored=1)
    assert [c["candidate_id"] for c in got] == ["older-onrole"]


def test_no_prescreen_keeps_legacy_behaviour():
    rows = [_row(candidate_id="a", email="a@x.com")]
    (c,) = _select(rows)
    assert "role_match" not in c


def test_scoring_payload_matches_refresh_endpoint_shape():
    row = _row()
    blob = json.loads(row["data"])
    payload = cs.build_scoring_payload(row, blob)
    assert set(payload) >= {"candidate_id", "name", "headline", "location", "resume_text", "skills", "experience_years", "enhanced_info", "data", "match_score"}
    assert payload["resume_text"] == "python spark"


# ---------------------------------------------------------------------------
# Orchestration (mocked DB)
# ---------------------------------------------------------------------------
def _mock_conn(job_row, claim_ok=True, existing=(), prior=(), inserted_ids=(1,)):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    fetchone_results = [job_row, {"job_id": job_row["job_id"]} if claim_ok else None]
    fetchone_results += [{"id": i} for i in inserted_ids]
    cur.fetchone.side_effect = fetchone_results + [None] * 50
    cur.fetchall.side_effect = [list(existing), list(prior)]
    return conn, cur


JOB = {
    "job_id": "2002", "jobdiva_id": "26-22222", "title": "Data Engineer", "enhanced_title": None,
    "customer_name": "Globex", "recruiter_emails": json.dumps(["rec@pyramidci.com"]),
    "parent_job_id": None, "cross_submissions_checked_at": None,
}


def test_run_for_job_persists_and_emails_new_persons():
    prior = [_row(candidate_id="a", email="a@x.com")]
    conn, cur = _mock_conn(JOB, prior=prior)
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions", return_value=True) as notify:
        summary = cs.run_for_job("26-22222", criteria=object(), scorer=lambda p, c: {"score": 88}, force=False)

    assert summary["ran"] is True
    assert summary["selected"] == 1 and summary["new"] == 1 and summary["emailed"] is True
    notify.assert_called_once()
    kwargs = notify.call_args.kwargs
    assert kwargs["jobdiva_id"] == "26-22222"
    assert kwargs["recruiter_emails"] == ["rec@pyramidci.com"]
    assert kwargs["candidates"][0]["candidate_id"] == "a"
    # INSERT used ON CONFLICT DO NOTHING (the email-once claim) and notified_at was stamped.
    sqls = " ".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "ON CONFLICT (job_id, person_key) DO NOTHING" in sqls
    assert "SET notified_at = NOW()" in sqls


def test_run_for_job_passes_prescreen_and_reports_gate_stats():
    prior = [_row(candidate_id="qa", email="q@x.com", name="QA"), _row(candidate_id="da", email="d@x.com", name="DA")]
    conn, cur = _mock_conn(JOB, prior=prior)
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions", return_value=True) as notify:
        summary = cs.run_for_job(
            "26-22222", criteria=object(), scorer=lambda p, c: {"score": 88},
            prescreen=lambda p, c: (p["candidate_id"] == "da", "title_mismatch" if p["candidate_id"] == "qa" else "ok"),
        )
    assert summary["gated_out"] == 1 and summary["gated_reasons"] == {"title_mismatch": 1}
    assert summary["scored"] == 1 and summary["selected"] == 1
    assert [c["candidate_id"] for c in notify.call_args.kwargs["candidates"]] == ["da"]


def test_both_callers_wire_the_step5_role_gate_next_to_the_step5_scorer():
    """The service takes the gate as a parameter; both production callers must pass it."""
    for rel in ("routers/candidates.py", "routers/cross_submissions.py"):
        src = (API_ROOT / rel).read_text(encoding="utf-8")
        assert "prescreen=_passes_step5_role_gate" in src, rel
        assert "_compute_resume_matching" in src, rel


def test_run_for_job_throttled_skips_scan_and_email():
    conn, cur = _mock_conn(JOB, claim_ok=False)
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions") as notify:
        summary = cs.run_for_job("26-22222", criteria=object(), scorer=lambda p, c: {"score": 88})
    assert summary["ran"] is False and summary["skipped_reason"] == "throttled"
    notify.assert_not_called()
    assert cur.fetchall.call_count == 0


def test_run_for_job_no_new_rows_sends_nothing():
    prior = [_row(candidate_id="a", email="a@x.com")]
    conn, cur = _mock_conn(JOB, prior=prior, inserted_ids=())  # conflict → no RETURNING row
    with patch.object(cs, "get_db_connection", return_value=conn), \
         patch("core.email.notify_cross_submissions") as notify:
        summary = cs.run_for_job("26-22222", criteria=object(), scorer=lambda p, c: {"score": 88})
    assert summary["selected"] == 1 and summary["new"] == 0 and summary["emailed"] is False
    notify.assert_not_called()


def test_run_for_job_is_fail_open():
    with patch.object(cs, "get_db_connection", side_effect=RuntimeError("db down")):
        summary = cs.run_for_job("26-22222", criteria=None, scorer=None)
    assert summary["ran"] is False
    assert summary["skipped_reason"].startswith("error:")


def test_run_for_job_respects_kill_switch():
    with patch.object(cs, "CROSS_SUBMISSIONS_ENABLED", False), \
         patch.object(cs, "get_db_connection") as db:
        summary = cs.run_for_job("26-22222", criteria=None, scorer=None)
    assert summary["skipped_reason"] == "disabled"
    db.assert_not_called()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def test_notify_cross_submissions_renders_rows_and_recipients():
    from core import email as email_mod

    cands = [{
        "name": "Ada <Lovelace>", "email": "ada@x.com", "phone": "5125550100", "headline": "Data Engineer",
        "candidate_id": "c1", "prior_jobdiva_id": "26-11111", "prior_job_title": "Sr Data Eng", "prior_customer_name": "Acme",
        "screen_result": "Pass", "screen_score_display": "80%", "match_score": 87.4,
        "screened_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }]
    with patch.object(email_mod, "_send", return_value=True) as send:
        ok = email_mod.notify_cross_submissions(
            jobdiva_id="26-22222", job_id="2002", job_title="Data Engineer", customer_name="Globex",
            recruiter_emails=["rec@pyramidci.com", "rec@pyramidci.com"], candidates=cands,
        )
    assert ok is True
    to_list, subject, html_body, plain = send.call_args.args[:4]
    assert to_list == [email_mod.PAIR_TEAM_EMAIL, "rec@pyramidci.com"]
    assert "Cross Submissions" in subject and "26-22222" in subject
    assert "Ada &lt;Lovelace&gt;" in html_body            # escaped
    assert "/jobs/26-11111/report?candidateId=c1" in html_body
    assert "87%" in html_body and "80%" in html_body and "Sep 01, 2026" in html_body
    assert "Ada <Lovelace>" in plain


def test_notify_cross_submissions_links_each_candidate_to_report_and_jobdiva_profile():
    from core import email as email_mod

    cands = [
        {"name": "Ada", "candidate_id": "exa_linkedin.com/in/ada", "jobdiva_candidate_id": "4455",
         "prior_jobdiva_id": "26-11111", "screen_result": "Pass", "match_score": 80},
        {"name": "Bob", "candidate_id": "77", "jobdiva_candidate_id": "",
         "prior_jobdiva_id": "26-11111", "screen_result": "Fail", "match_score": 70},
    ]
    with patch.object(email_mod, "_send", return_value=True) as send:
        email_mod.notify_cross_submissions(
            jobdiva_id="26-22222", job_id="2002", job_title="Data Engineer", customer_name="",
            recruiter_emails=[], candidates=cands, app_base_url="https://pairqa.pyramidci.com",
        )
    _, _, html_body, plain = send.call_args.args[:4]
    # Report link: the PRIOR job's report, candidate id URL-quoted.
    assert 'href="https://pairqa.pyramidci.com/jobs/26-11111/report?candidateId=exa_linkedin.com%2Fin%2Fada"' in html_body
    assert "https://pairqa.pyramidci.com/jobs/26-11111/report?candidateId=77" in html_body
    # JobDiva link: the person's profile id (not candidate_id), only when known.
    profile = "https://www1.jobdiva.com/employers/myreports/viewcandidate2_real.jsp?docids=-1&candidateid=4455"
    assert f'href="{profile.replace("&", "&amp;")}"' in html_body
    assert html_body.count("JobDiva profile") == 1
    assert html_body.count("Screen report") == 2
    assert f"JobDiva: {profile}" in plain
    assert "screen report: https://pairqa.pyramidci.com/jobs/26-11111/report?candidateId=77" in plain


def test_jobdiva_candidate_link_format():
    from core import email as email_mod

    assert email_mod.jobdiva_candidate_link("") == ""
    assert email_mod.jobdiva_candidate_link(" 123 ").endswith("/employers/myreports/viewcandidate2_real.jsp?docids=-1&candidateid=123")


def test_notify_cross_submissions_with_no_candidates_sends_nothing():
    from core import email as email_mod

    with patch.object(email_mod, "_send") as send:
        assert email_mod.notify_cross_submissions(
            jobdiva_id="x", job_id="1", job_title="", customer_name="", recruiter_emails=[], candidates=[],
        ) is False
    send.assert_not_called()


# ---------------------------------------------------------------------------
# Router guards + nginx allowlist
# ---------------------------------------------------------------------------
ROUTER_PATH = Path(__file__).resolve().parents[1] / "routers" / "cross_submissions.py"
NGINX_PATH = Path(__file__).resolve().parents[3] / "nginx-app-locations.conf"


def _router_routes():
    src = ROUTER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        paths = [
            d.args[0].value for d in node.decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name) and d.func.value.id == "router"
            and d.args and isinstance(d.args[0], ast.Constant)
        ]
        if paths:
            out.append((paths[0], ast.get_source_segment(src, node) or ""))
    return out


def test_every_cross_submissions_route_is_guarded():
    routes = _router_routes()
    assert len(routes) == 3
    for path, body in routes:
        assert "Depends(get_current_user)" in body, path
        assert "_verify_job_access_by_id(" in body, path


def test_cross_submissions_subpath_is_in_nginx_allowlist():
    content = NGINX_PATH.read_text(encoding="utf-8")
    match = re.search(r"location ~ \^/jobs/\(\[\^/\]\+\)/\((.*?)\)\(\/\.\*\)\?\$", content)
    assert match is not None
    assert "cross-submissions" in match.group(1).split("|")
    for path, _ in _router_routes():
        sub = re.match(r"^/jobs/\{[^/]+\}/([^/]+)", path).group(1)
        assert sub in match.group(1).split("|"), path


# ---------------------------------------------------------------------------
# "Add to this job" (rank-list panel)
# ---------------------------------------------------------------------------
CS_ROW = {
    "id": 7, "job_id": "2002", "jobdiva_id": "26-22222", "person_key": "email:a@x.com", "candidate_id": "c1",
    "source": "JobDiva", "prior_job_id": "1001", "prior_jobdiva_id": "26-11111", "prior_job_title": "Sr DE",
    "prior_customer_name": "Acme", "screen_result": "Pass", "screened_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    "match_score": 81.5, "matched_skills": '["python"]', "missing_skills": "[]",
}


def test_build_added_blob_strips_outreach_state_but_keeps_jobdiva_profile_id():
    prior = {
        "engage_status": "passed", "engage_interview_id": "iv-1", "engage_score": 8, "engage_last_response": {"x": 1},
        "engage_hard_filter_status": "passed", "phase": "phase2", "feedback_type": "Submit",
        "jobdiva_candidate_id": "jd-999", "enhanced_info": {"key_skills": ["python"]}, "skills": ["python"],
    }
    blob = cs.build_added_blob(prior, CS_ROW)
    for k in ("engage_status", "engage_interview_id", "engage_score", "engage_last_response", "engage_hard_filter_status", "phase", "feedback_type"):
        assert k not in blob, k
    assert blob["jobdiva_candidate_id"] == "jd-999"
    assert blob["enhanced_info"] == {"key_skills": ["python"]}
    assert blob["match_score"] == 81.5 and blob["resume_matching_status"] == "done"
    assert blob["matched_skills"] == ["python"]
    assert blob["cross_submission"]["from_jobdiva_id"] == "26-11111"
    assert blob["cross_submission"]["screen_result"] == "Pass"
    assert blob["cross_submission"]["screened_at"] == "2026-09-01T00:00:00+00:00"


def _add_conn(job_row, cs_row, prior_row, inserted):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [job_row, cs_row, prior_row, inserted]
    return conn, cur


def test_add_to_job_copies_prior_row_without_engage_state():
    prior = {
        "candidate_id": "c1", "source": "JobDiva", "name": "Ada", "email": "a@x.com", "phone": "1", "headline": "DE",
        "location": "Austin, TX", "profile_url": None, "image_url": None, "resume_id": "r1", "resume_text": "python",
        "data": json.dumps({"engage_status": "passed", "engage_interview_id": "iv", "jobdiva_candidate_id": "jd-1"}),
    }
    conn, cur = _add_conn(JOB, CS_ROW, prior, {"id": 55})
    with patch.object(cs, "get_db_connection", return_value=conn):
        out = cs.add_to_job("26-22222", 7, added_by="rec@pyramidci.com")

    assert out["status"] == "success" and out["already_present"] is False
    assert out["job_key"] == "26-22222" and out["candidate_id"] == "c1" and out["match_score"] == 81.5
    insert_call = next(c for c in cur.execute.call_args_list if "INSERT INTO sourced_candidates" in str(c.args[0]))
    assert "ON CONFLICT (jobdiva_id, candidate_id, source) DO NOTHING" in insert_call.args[0]
    params = insert_call.args[1]
    assert params[0] == "26-22222" and params[1] == "c1" and params[11] == "python"
    blob = json.loads(params[12])
    assert "engage_status" not in blob and "engage_interview_id" not in blob
    assert blob["jobdiva_candidate_id"] == "jd-1"
    assert blob["cross_submission"]["added_by"] == "rec@pyramidci.com"
    assert params[13] == 81.5
    assert any("SET added_at = COALESCE(added_at, NOW())" in str(c.args[0]) for c in cur.execute.call_args_list)
    conn.commit.assert_called_once()


def test_add_to_job_is_idempotent_when_row_already_present():
    prior = {"candidate_id": "c1", "source": "JobDiva", "name": "Ada", "data": "{}"}
    conn, _ = _add_conn(JOB, CS_ROW, prior, None)  # ON CONFLICT → no RETURNING row
    with patch.object(cs, "get_db_connection", return_value=conn):
        out = cs.add_to_job("26-22222", 7)
    assert out["already_present"] is True


@pytest.mark.parametrize("job_row, cs_row, prior_row", [
    (None, CS_ROW, {}),      # unknown job
    (JOB, None, {}),         # id belongs to another job / doesn't exist
    (JOB, CS_ROW, None),     # prior screened row deleted
])
def test_add_to_job_raises_lookup_error(job_row, cs_row, prior_row):
    conn, _ = _add_conn(job_row, cs_row, prior_row, None)
    with patch.object(cs, "get_db_connection", return_value=conn), pytest.raises(LookupError):
        cs.add_to_job("26-22222", 7)
    conn.rollback.assert_called()
