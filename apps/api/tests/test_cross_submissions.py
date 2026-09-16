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
    assert cs.screen_result_label("failed") == "Fail"
    assert cs.screen_result_label("in_progress") == "In Progress"
    assert cs.screen_result_label("completed", "passed") == "Pass"
    assert cs.screen_result_label("completed", "failed") == "Fail"
    assert cs.screen_result_label("sent") == "Pending"


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
        _row(candidate_id="c", email="c@x.com", engage_status="failed"),
        _row(candidate_id="d", email="d@x.com", engage_status="sent"),       # launched, never answered
        _row(candidate_id="e", email="e@x.com", engage_status="processing"),
        _row(candidate_id="f", email="f@x.com", engage_status=None),
    ]
    got = {c["candidate_id"]: c["screen_result"] for c in _select(rows)}
    assert got == {"a": "Pass", "b": "In Progress", "c": "Fail"}


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
