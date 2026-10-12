"""Unit D1 — launch-platform changes inside `_send_bulk_interview_core` and the
Pairbot reconcile / claim / provisioning helpers around it.

Pairbot's DuplicateInterview answer may point at another job's interview
(older Pairbot deduped per person + role TITLE across jobs) or at the
interview of the person's other PAIR id (a twin; current Pairbot dedupes
within one bulk only). These tests pin that such a row is recorded as
launched only when Pairbot's job-scoped record (by-job) lists an interview
under its OWN id, and is otherwise failed + blocked, on every launch path —
and that every path posts only what it won in the send claim. The DB is an
SQL-dispatch fake (tests/test_launch_platform_sql_pg.py runs the same
statements against a real Postgres, from two connections where it matters).
"""
import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from routers import engagement as eng
from services.engage_status import format_engage_status, LAUNCH_IN_FLIGHT_STATUSES


JOB, REF = "J1", "26-001"


class FakeDB:
    def __init__(self):
        self.log: List[tuple] = []
        self.idem_rows: List[tuple] = []
        self.idem_error: Optional[Exception] = None
        # Send claim: by default every id has a row and is won.
        self.claim_lost: set = set()
        self.rowless: set = set()
        self.claim_error: Optional[Exception] = None
        self.commits = 0

    def respond(self, sql: str, params: Any):
        if sql is eng._ENGAGE_IDEMPOTENCY_SQL:
            if self.idem_error is not None:
                raise self.idem_error
            return self.idem_rows
        if sql is eng._SEND_CLAIM_SQL:
            if self.claim_error is not None:
                raise self.claim_error
            return [(c, c not in self.claim_lost) for c in params["ids"] if c not in self.rowless]
        if sql is eng._RELEASE_SEND_CLAIM_SQL:
            return [(c,) for c in params["ids"]]
        if "SELECT outreach_stopped_at, recruiter_emails" in sql:
            return [(None, '["r@x.com"]')]
        if "SELECT customer_name" in sql:
            return [("Acme",)]
        if "SELECT candidate_id, data, name, headline, resume_text, source" in sql:
            return [(c, {}, f"N {c}", "", "", "LinkedIn-Exa") for c in params[0]]
        if "RETURNING candidate_id" in sql and "UPDATE sourced_candidates" in sql:
            return [(params[2] if isinstance(params, tuple) else "x",)]
        if "RETURNING id" in sql:
            return [(1,)]
        return []

    def conn(self):
        db = self

        class Cur:
            def __init__(self):
                self._rows: List[Any] = []

            def execute(self, sql, params=None):
                db.log.append((sql, params))
                self._rows = db.respond(sql, params)

            def fetchall(self):
                return list(self._rows or [])

            def fetchone(self):
                return (self._rows or [None])[0]

            def close(self):
                pass

        class Conn:
            def cursor(self, **_kw):
                return Cur()

            def commit(self):
                db.commits += 1

            def rollback(self):
                pass

            def close(self):
                pass

        return Conn()

    # --- readers over the log -------------------------------------------------
    def engage_writes(self) -> List[tuple]:
        """(status, interview_id, candidate_id) of each _write_candidate_engage_status."""
        return [
            (p[0], p[2], p[3]) for s, p in self.log
            if "'{engage_interview_id}'" in s and "UPDATE sourced_candidates" in s
        ]

    def block_writes(self) -> List[tuple]:
        return [(p[0], p[1]) for s, p in self.log if "'{engage_block_reason}'" in s]

    def audit_inserts(self) -> List[tuple]:
        """(candidate_id, interview_id, status) of the core's audit INSERTs."""
        out = []
        for s, p in self.log:
            if "INSERT INTO engage_interview_audit" in s and "VALUES" in s:
                if "candidate_name" in s:      # created / no-interview rows
                    out.append((p[0], p[2], p[7]))
                elif "interview_id" in s:      # duplicate rows
                    out.append((p[0], p[2], p[5]))
                else:                          # whole-batch failure
                    out.append((p[0], None, p[4]))
        return out

    def reconcile_sent(self) -> List[tuple]:
        """(candidate_id, interview_id, keys) written by _record_reconciled_interviews."""
        return [
            (p[2], p[0], p[3]) for s, p in self.log
            if "'engage_status', 'sent'" in s and "UPDATE sourced_candidates" in s
        ]

    def send_claims(self) -> List[dict]:
        return [p for s, p in self.log if s is eng._SEND_CLAIM_SQL]

    def send_releases(self) -> List[dict]:
        return [p for s, p in self.log if s is eng._RELEASE_SEND_CLAIM_SQL]

    def processing_stamps(self) -> List[tuple]:
        return [(s, p) for s, p in self.log if "UPDATE sourced_candidates" in s and "'{engage_status}'" in s
                and "'{engage_interview_id}'" not in s]


class _Resp:
    def __init__(self, status: int, body: Dict[str, Any]):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode()
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def pb(monkeypatch):
    """Fake DB + Pairbot. `state` steers the creation event and by-job."""
    db = FakeDB()
    state: Dict[str, Any] = {
        "creation": {"status": "creation_completed", "data": [], "failed_interviews": []},
        "by_job": {"bulk_in_progress": False, "interviews": []},
        "by_job_error": None, "wait_error": None,
        "posts": [], "by_job_calls": [], "spawned": [], "t2": [], "provisioned": [],
    }
    monkeypatch.setattr(eng, "_get_db_connection", db.conn)
    monkeypatch.setattr(eng, "_fetch_stored_employer_signals", lambda _c, _ids: {})

    async def post(url, payload_obj, **_k):
        state["posts"].append(json.loads(json.dumps(payload_obj)))
        return _Resp(202, {"bulk_id": payload_obj.get("bulk_id")})

    async def wait(bulk_id, **_k):
        if state["wait_error"] is not None:
            raise state["wait_error"]
        return state["creation"]

    async def by_job(job_key):
        state["by_job_calls"].append(job_key)
        if state["by_job_error"] is not None:
            raise state["by_job_error"]
        return state["by_job"]

    def spawn(coro, name=None):
        qual = getattr(coro, "__qualname__", str(coro))
        state["spawned"].append(qual)
        if "provisioning" in qual:
            # Drive it inline (the fake provisioner never suspends) so the
            # ids it was handed are observable.
            try:
                coro.send(None)
            except StopIteration:
                return
        coro.close()

    async def provision(ids, job_id, **_k):
        state["provisioned"].append((list(ids), job_id))
        return {"success": len(ids)}

    monkeypatch.setattr(eng, "_post_to_pairbot", post)
    monkeypatch.setattr(eng, "_wait_for_pairbot_creation", wait)
    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", by_job)
    monkeypatch.setattr(eng, "_spawn_task", spawn)
    monkeypatch.setattr(eng, "_stamp_first_launch", lambda key: state["t2"].append(key))
    monkeypatch.setattr(eng, "_provision_batch_to_jobdiva", provision)
    state["db"] = db
    return state


def _payload(ids, *, order=None) -> str:
    return json.dumps({
        "jd": {"job_id": JOB, "jobdiva_id": REF, "title": "Java Developer"},
        "resumes": [
            {"source_candidate_id": c, "name": f"N {c}", "email": f"{c}@x.com", "phone": "4155550100"}
            for c in (order or ids)
        ],
    })


def _send(ids, *, order=None, **kw):
    req = eng.SendBulkInterviewRequest(payload=_payload(ids, order=order), real_candidate_ids=list(ids))
    signals = {c: {} for c in ids}  # covers every id: no employer resolution
    return asyncio.run(eng._send_bulk_interview_core(req, precomputed_signals=signals, **kw))


def _dup(idx, existing="iv-999"):
    return {"resume_index": idx, "candidate_name": "N", "error": "Duplicate interview for candidate and role",
            "error_type": "DuplicateInterview", "existing_interview_id": existing}


# ------------------------------------------------- DuplicateInterview ----

def test_same_title_other_job_duplicate_is_never_recorded_sent(pb):
    pb["creation"] = {"status": "creation_completed",
                      "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}],
                      "failed_interviews": [_dup(1)]}
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-a", "source_candidate_id": "a"}]}
    res = _send(["a", "b"])
    db = pb["db"]
    assert res["success"] is True
    assert [r["candidate_id"] for r in res["data"]] == ["a"]
    assert "b" not in res["skipped_already_sent"]
    assert res["excluded_candidates"] == [{
        "candidate_id": "b", "name": "",
        "reason": "already has a PAIR interview for another job with the same title",
    }]
    assert ("failed", "", "b") in db.engage_writes()
    assert not [w for w in db.engage_writes() if w[2] == "b" and w[0] == "sent"]
    assert db.block_writes() == [("duplicate_role_other_job", "b")]
    assert ("b", "", "duplicate_other_job") in db.audit_inserts()
    (dup_audit,) = [p for s, p in db.log if "INSERT INTO engage_interview_audit" in s and p[5] == "duplicate_other_job"]
    assert json.loads(dup_audit[4]) == {"error_type": "DuplicateInterview",
                                        "error": "Duplicate interview for candidate and role",
                                        "existing_interview_id": "iv-999", "resume_index": 1}
    assert db.reconcile_sent() == []
    assert pb["by_job_calls"] == [JOB, REF]
    # Not provisioned to JobDiva for this job either.
    assert pb["provisioned"] == [(["a"], JOB)]


def test_duplicate_of_this_jobs_interview_counts_as_already_launched(pb):
    pb["creation"] = {"status": "creation_completed", "data": [], "failed_interviews": [_dup(0, "iv-b")]}
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-b", "source_candidate_id": "b"}]}
    res = _send(["b"])
    db = pb["db"]
    assert res["skipped_already_sent"] == ["b"] and res["data"] == [] and res["excluded_candidates"] == []
    assert db.reconcile_sent() == [("b", "iv-b", [JOB, REF])]
    assert not [w for w in db.engage_writes() if w[2] == "b"]   # never overwritten to failed
    assert db.block_writes() == []
    assert pb["t2"] == [REF]                                   # reconcile wrote an interview


def test_a_twin_is_blocked_never_recorded_with_its_twins_interview(pb):
    """by-job lists the existing interview under the person's OTHER PAIR id:
    one interview must not count as two launched candidates."""
    pb["creation"] = {"status": "creation_completed", "data": [], "failed_interviews": [_dup(0, "iv-77")]}
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-77", "source_candidate_id": "JD-555"}]}
    res = _send(["b"])
    db = pb["db"]
    assert res["skipped_already_sent"] == []
    assert res["excluded_candidates"] == [{
        "candidate_id": "b", "name": "",
        "reason": "already has a PAIR interview for this job under another profile",
    }]
    assert db.reconcile_sent() == []                           # no 'sent', no audit row with iv-77
    assert ("failed", "", "b") in db.engage_writes()
    assert db.block_writes() == [("duplicate_person_this_job", "b")]
    assert ("b", "", "duplicate_this_job") in db.audit_inserts()
    assert pb["t2"] == [] and pb["provisioned"] == []


def test_twin_in_the_same_bulk_launches_one_and_blocks_the_other(pb):
    # X and Y are one person; Pairbot created X and answered Y with X's interview.
    pb["creation"] = {"status": "creation_completed",
                      "data": [{"source_candidate_id": "x", "interview_id": "iv-x"}],
                      "failed_interviews": [_dup(1, "iv-x")]}
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-x", "source_candidate_id": "x"}]}
    res = _send(["x", "y"])
    db = pb["db"]
    assert [r["candidate_id"] for r in res["data"]] == ["x"]
    assert [e["candidate_id"] for e in res["excluded_candidates"]] == ["y"]
    assert ("sent", "iv-x", "x") in db.engage_writes()
    assert not [w for w in db.engage_writes() if w[2] == "y" and w[1]]
    assert db.block_writes() == [("duplicate_person_this_job", "y")]
    assert pb["provisioned"] == [(["x"], JOB)]


def test_proven_duplicate_whose_reconcile_write_failed_is_recorded_by_the_core(pb, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("deadlock detected")
    monkeypatch.setattr(eng, "_record_reconciled_interviews", broken)
    pb["creation"] = {"status": "creation_completed", "data": [], "failed_interviews": [_dup(0, "iv-old")]}
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-b", "source_candidate_id": "b"}]}
    res = _send(["b"])
    db = pb["db"]
    assert res["skipped_already_sent"] == ["b"] and res["excluded_candidates"] == []
    # Its own (newest) interview, written in the core's own transaction.
    assert ("sent", "iv-b", "b") in db.engage_writes()
    (audit,) = [p for s, p in db.log if "INSERT INTO engage_interview_audit" in s]
    assert audit[0] == "b" and audit[2] == "iv-b" and audit[5] == "Initiated"
    assert json.loads(audit[4])["reconciled_from"] == "pairbot_by_job"
    assert db.block_writes() == []
    assert pb["t2"] == [REF]


def test_duplicate_is_blocked_unverified_when_by_job_is_down(pb):
    pb["creation"] = {"status": "creation_completed", "data": [], "failed_interviews": [_dup(0)]}
    pb["by_job_error"] = RuntimeError("Pairbot by-job check failed status=503")
    res = _send(["b"])
    db = pb["db"]
    assert db.block_writes() == [("duplicate_unverified", "b")]
    assert ("failed", "", "b") in db.engage_writes()
    assert res["excluded_candidates"][0]["candidate_id"] == "b"


def test_resume_index_maps_through_the_posted_payload_not_the_id_list(pb):
    # Posted resumes are [b, a]; index 0 is b even though real ids say [a, b].
    pb["creation"] = {"status": "creation_completed",
                      "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}],
                      "failed_interviews": [_dup(0)]}
    res = _send(["a", "b"], order=["b", "a"])
    assert [e["candidate_id"] for e in res["excluded_candidates"]] == ["b"]
    assert pb["db"].block_writes() == [("duplicate_role_other_job", "b")]


def test_map_duplicate_interviews_rules():
    payload = {"resumes": [{"source_candidate_id": "x"}, {"source_candidate_id": "y"}, "junk"]}
    items = [
        _dup(0, "e0"), _dup(1, "e1"), _dup(2), _dup(9), _dup(True),
        {"resume_index": 0, "error_type": "ValidationError"}, {"resume_index": "1", "error_type": "DuplicateInterview",
                                                                "existing_interview_id": "dup-again"},
    ]
    def existing(mapped):
        return {cid: item["existing_interview_id"] for cid, item in mapped.items()}
    assert existing(eng._map_duplicate_interviews(items, payload, ["x", "y"])) == {"x": "e0", "y": "e1"}
    assert existing(eng._map_duplicate_interviews(items, payload, ["y"])) == {"y": "e1"}      # not asked
    assert existing(eng._map_duplicate_interviews(items, payload, ["x", "y"], {"x"})) == {"y": "e1"}  # created
    assert existing(eng._map_duplicate_interviews([_dup(-1)], payload, ["x", "y"])) == {}       # no wrap-around
    assert eng._map_duplicate_interviews(None, payload, ["x"]) == {}
    assert eng._map_duplicate_interviews(items, {}, ["x"]) == {}


# -------------------------------------------------- idempotency read ----

def test_blocked_rows_are_not_posted_and_come_back_as_exclusions(pb):
    pb["db"].idem_rows = [("b", False, "duplicate_role_other_job"), ("c", True, None)]
    pb["creation"] = {"status": "creation_completed",
                      "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}]}
    res = _send(["a", "b", "c"])
    assert [r["source_candidate_id"] for r in pb["posts"][0]["resumes"]] == ["a"]
    assert res["skipped_already_sent"] == ["c"]
    assert res["excluded_candidates"] == [{
        "candidate_id": "b", "name": "",
        "reason": "already has a PAIR interview for another job with the same title",
    }]


def test_claimed_session_is_passed_to_the_idempotency_read(pb):
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a"], claimed_session_id="S-1")
    (params,) = [p for s, p in pb["db"].log if s is eng._ENGAGE_IDEMPOTENCY_SQL]
    assert params == ("S-1", "S-1", ["a"], JOB, REF)
    _send(["a"])
    params = [p for s, p in pb["db"].log if s is eng._ENGAGE_IDEMPOTENCY_SQL][-1]
    assert params[:2] == ("", "")


def test_idempotency_read_fails_closed_for_a_launch_session(pb):
    pb["db"].idem_error = RuntimeError("pool exhausted")
    res = _send(["a"], claimed_session_id="S-1")
    assert res["success"] is False and "nothing was sent" in res["message"]
    assert pb["posts"] == []


def test_idempotency_read_still_fails_open_on_the_legacy_path(pb):
    pb["db"].idem_error = RuntimeError("pool exhausted")
    pb["creation"] = {"status": "creation_completed", "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}]}
    res = _send(["a"])
    assert res["success"] is True and len(pb["posts"]) == 1


# ------------------------------------------------------------ send claim ----

def test_every_post_is_preceded_by_the_send_claim(pb, monkeypatch):
    db = pb["db"]
    claims_at_post: List[int] = []
    real_post = eng._post_to_pairbot

    async def post(url, payload_obj, **k):
        claims_at_post.append(len(db.send_claims()))
        return await real_post(url, payload_obj, **k)
    monkeypatch.setattr(eng, "_post_to_pairbot", post)
    pb["creation"] = {"status": "creation_completed", "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}]}
    _send(["a", "b"])
    assert claims_at_post == [1]
    assert db.send_claims() == [{"keys": [JOB, REF], "ids": ["a", "b"], "sid": ""}]
    _send(["a"], claimed_session_id="S-1")
    assert db.send_claims()[-1]["sid"] == "S-1"


def test_ids_lost_in_the_send_claim_are_not_posted_and_read_as_already_sent(pb):
    pb["db"].claim_lost = {"b"}
    pb["creation"] = {"status": "creation_completed",
                      "data": [{"source_candidate_id": "a", "interview_id": "iv-a"},
                               {"source_candidate_id": "c", "interview_id": "iv-c"}]}
    res = _send(["a", "b", "c"], order=["c", "b", "a"])
    assert [r["source_candidate_id"] for r in pb["posts"][0]["resumes"]] == ["c", "a"]
    assert res["skipped_already_sent"] == ["b"]
    assert sorted(r["candidate_id"] for r in res["data"]) == ["a", "c"]
    assert not [w for w in pb["db"].engage_writes() if w[2] == "b"]   # never touched
    (sql, params), = pb["db"].processing_stamps()
    assert params[2] == ["a", "c"]


def test_nothing_is_posted_when_every_id_lost_the_claim(pb):
    pb["db"].claim_lost = {"a"}
    res = _send(["a"])
    assert pb["posts"] == [] and res["success"] is True
    assert res["skipped_already_sent"] == ["a"] and "nothing to send" in res["message"]


@pytest.mark.parametrize("session", [None, "S-1"])
def test_a_send_claim_error_posts_nothing(pb, session):
    pb["db"].claim_error = RuntimeError("could not obtain lock")
    res = _send(["a"], claimed_session_id=session)
    assert res["success"] is False and res["message"] == "Launch claim failed; nothing was sent."
    assert pb["posts"] == [] and pb["db"].engage_writes() == []


def test_rowless_ids_post_unclaimed_on_legacy_but_never_for_a_session(pb):
    pb["db"].rowless = {"b"}
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a", "b"])
    assert [r["source_candidate_id"] for r in pb["posts"][-1]["resumes"]] == ["a", "b"]
    res = _send(["a", "b"], claimed_session_id="S-1")
    assert [r["source_candidate_id"] for r in pb["posts"][-1]["resumes"]] == ["a"]
    assert res["excluded_candidates"] == [{"candidate_id": "b", "name": "", "reason": "not saved for this job"}]


def test_a_session_posts_only_the_resumes_it_claimed(pb):
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a"], order=["a", "stray"], claimed_session_id="S-1")
    assert [r["source_candidate_id"] for r in pb["posts"][-1]["resumes"]] == ["a"]
    _send(["a"], order=["a", "stray"])                      # legacy payload untouched
    assert [r["source_candidate_id"] for r in pb["posts"][-1]["resumes"]] == ["a", "stray"]


@pytest.mark.parametrize("session", [None, "S-1"])
def test_no_answer_from_pairbot_releases_the_held_rows(pb, monkeypatch, session):
    import httpx

    async def down(url, payload_obj, **_k):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(eng, "_post_to_pairbot", down)
    pb["db"].claim_lost = {"b"}
    res = _send(["a", "b", "c"], claimed_session_id=session)
    assert res["success"] is False and "Server error" in res["message"]
    assert pb["db"].send_releases() == [{"keys": [JOB, REF], "ids": ["a", "c"], "sid": session or ""}]
    assert pb["db"].processing_stamps() == []


def test_a_cancel_during_the_post_releases_and_propagates(pb, monkeypatch):
    async def cancelled(url, payload_obj, **_k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(eng, "_post_to_pairbot", cancelled)
    with pytest.raises(asyncio.CancelledError):
        _send(["a"], claimed_session_id="S-1")
    assert pb["db"].send_releases() == [{"keys": [JOB, REF], "ids": ["a"], "sid": "S-1"}]


def test_a_non_2xx_answer_fails_the_held_rows_through_the_audit_loop(pb, monkeypatch):
    async def rejected(url, payload_obj, **_k):
        return _Resp(422, {"detail": "bad payload"})
    monkeypatch.setattr(eng, "_post_to_pairbot", rejected)
    res = _send(["a"])
    assert res["success"] is False
    assert ("failed", "", "a") in pb["db"].engage_writes()
    assert pb["db"].send_releases() == []


def test_release_send_claim_is_fail_open(monkeypatch):
    def no_db():
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(eng, "_get_db_connection", no_db)
    assert eng._release_send_claim((JOB, REF), ["a"], None) == 0
    assert eng._release_send_claim((JOB, REF), [], None) == 0
    assert eng._claim_rows_for_send(("", ""), ["a"], None) == ([], set())


# ------------------------------------------------------- bulk id / stamps ----

def test_bulk_id_for_gets_the_posted_ids_and_owns_the_bulk_id(pb):
    calls: List[tuple] = []

    def bulk_id_for(idx, ids):
        calls.append((idx, list(ids)))
        return "forced-bulk"
    pb["db"].idem_rows = [("c", True, None)]
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a", "b", "c"], order=["b", "a", "c"], batch_idx=3, bulk_id_for=bulk_id_for)
    assert calls == [(3, ["b", "a"])]                 # after filtering, in resume order
    assert pb["posts"][0]["bulk_id"] == "forced-bulk"
    (sql, params), = pb["db"].processing_stamps()
    assert "'{engage_bulk_id}'" in sql and params[:2] == ("processing", "forced-bulk")


def test_bulk_id_for_may_be_async(pb):
    async def bulk_id_for(idx, ids):
        return f"b-{idx}"
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a"], batch_idx=0, bulk_id_for=bulk_id_for)
    assert pb["posts"][0]["bulk_id"] == "b-0"


def test_bulk_id_for_failure_sends_nothing(pb):
    def boom(idx, ids):
        raise RuntimeError("checkpoint write failed")
    res = _send(["a"], batch_idx=0, bulk_id_for=boom, claimed_session_id="S-1")
    assert res["success"] is False and pb["posts"] == []
    assert pb["db"].processing_stamps() == []
    # The claim is handed back to the session ('queued'), not left 'sending'.
    assert pb["db"].send_releases() == [{"keys": [JOB, REF], "ids": ["a"], "sid": "S-1"}]


def test_legacy_processing_stamp_carries_the_bulk_id_too(pb):
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a"])
    (sql, params), = pb["db"].processing_stamps()
    assert params[0] == "processing" and params[1] == pb["posts"][0]["bulk_id"]


def test_stamp_without_bulk_id_keeps_the_old_statement(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(eng, "_get_db_connection", db.conn)
    eng._stamp_engage_status_batch(["a"], "failed", job_id_value=JOB, job_id_alt=REF)
    (sql, params), = db.log
    assert "engage_bulk_id" not in sql and params == ("failed", ["a"], JOB, REF)


# ------------------------------------------------- provisioning / T2 ----

def test_provision_inline_false_skips_the_background_provisioning(pb):
    pb["creation"] = {"status": "creation_completed", "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}]}
    _send(["a"], provision_inline=False)
    assert not [s for s in pb["spawned"] if "provisioning" in s] and pb["provisioned"] == []
    _send(["a"])
    assert pb["provisioned"] == [(["a"], JOB)]


def test_t2_is_stamped_with_the_ref_only_when_an_interview_was_created(pb):
    pb["creation"] = {"status": "creation_completed", "data": []}
    _send(["a"])
    assert pb["t2"] == []
    pb["creation"] = {"status": "creation_completed", "data": [{"source_candidate_id": "a", "interview_id": "iv-a"}]}
    _send(["a"])
    assert pb["t2"] == [REF]


def test_t2_is_not_stamped_on_a_dry_run(pb):
    req = eng.SendBulkInterviewRequest(payload=_payload(["a"]), real_candidate_ids=["a"], dry_run=True)
    asyncio.run(eng._send_bulk_interview_core(req, precomputed_signals={"a": {}}))
    assert pb["t2"] == []


def test_stamp_first_launch_is_fail_open(monkeypatch):
    import services.launch_timing as lt

    def boom(*_a, **_k):
        raise RuntimeError("db")
    monkeypatch.setattr(lt, "stamp_first_launch", boom)
    eng._stamp_first_launch("26-001")  # must not raise
    eng._stamp_first_launch("")


# -------------------------------------------- wait failure → by-job write ----

def test_wait_failure_writes_found_candidates_sent_and_fails_only_confirmed(pb):
    pb["wait_error"] = TimeoutError("600s")
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-a", "source_candidate_id": "a"}]}
    res = _send(["a", "b"])
    db = pb["db"]
    assert res["success"] is False
    assert db.reconcile_sent() == [("a", "iv-a", [JOB, REF])]
    failed = [p for s, p in db.log
              if "UPDATE sourced_candidates" in s and isinstance(p, tuple) and p[0] == "failed"]
    assert failed and failed[-1][1] == ["b"]
    assert pb["by_job_calls"] == [REF]


def test_confirm_writes_only_listed_interviews_with_an_id(pb):
    pb["by_job"] = {"bulk_in_progress": True, "interviews": [
        {"interview_id": "iv-a", "source_candidate_id": "a"}, {"source_candidate_id": "c"}]}
    out = asyncio.run(eng._confirm_failed_candidates_with_pairbot("bk", REF, ["a", "b", "c"], job_keys=(JOB, REF)))
    assert out == []                                 # still creating: nothing confirmed failed
    assert pb["db"].reconcile_sent() == [("a", "iv-a", [JOB, REF])]


# ----------------------------------------------------- reconcile_with_pairbot ----

def _reconcile(pb, ids, **kw):
    return asyncio.run(eng.reconcile_with_pairbot((JOB, REF), ids, **kw))


_EMPTY_RECONCILE = {"sent": {}, "unrecorded": {}, "duplicate_of": {}, "missing": [],
                    "bulk_in_progress": False, "in_progress_bulk_id": None,
                    "own_bulks_creating": [], "error": None}


def test_reconcile_reports_sent_and_missing(pb):
    pb["by_job"] = {"bulk_in_progress": False,
                    "interviews": [{"interview_id": "iv-2", "source_candidate_id": "a"},
                                   {"interview_id": "iv-1", "source_candidate_id": "a"},
                                   {"source_candidate_id": "c"}]}
    out = _reconcile(pb, ["a", "b", "c", "a"], session_id="S")
    assert out == dict(_EMPTY_RECONCILE, sent={"a": "iv-2"}, missing=["b"])
    assert pb["by_job_calls"] == [JOB, REF]
    assert pb["db"].reconcile_sent() == [("a", "iv-2", [JOB, REF])]


def test_reconcile_never_reports_missing_while_a_bulk_is_creating(pb):
    """Pairbot dedupes only within a bulk: re-posting 'missing' ids while our
    own bulk may still be creating would call them twice."""
    pb["by_job"] = {"bulk_in_progress": True, "in_progress_details": {"bulk_id": "bk-9", "phase": "creating"},
                    "interviews": [{"interview_id": "iv-2", "source_candidate_id": "a"}]}
    out = _reconcile(pb, ["a", "b"], session_id="S")
    assert out == dict(_EMPTY_RECONCILE, sent={"a": "iv-2"}, bulk_in_progress=True, in_progress_bulk_id="bk-9")


def _bulk_status(monkeypatch, statuses):
    seen: List[str] = []

    async def status(bulk_id, **_k):
        seen.append(bulk_id)
        value = statuses[bulk_id]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(eng, "_fetch_pairbot_bulk_status", status)
    return seen


def test_reconcile_own_bulks_done_lets_missing_through_despite_another_creating_bulk(pb, monkeypatch):
    pb["by_job"] = {"bulk_in_progress": True, "in_progress_details": {"bulk_id": "other-job-bulk"},
                    "interviews": []}
    seen = _bulk_status(monkeypatch, {"own-1": "creation_completed", "own-2": "completed"})
    out = _reconcile(pb, ["a"], own_bulk_ids=["own-1", "own-2", "own-1", ""])
    assert seen == ["own-1", "own-2"]
    assert out["missing"] == ["a"] and out["own_bulks_creating"] == []


@pytest.mark.parametrize("statuses,in_progress", [
    ({"own-1": "creating"}, "other"),                    # ours is still creating
    ({"own-1": RuntimeError("stream 502")}, "other"),    # unknown reads as creating
    ({"own-1": "creation_completed"}, "own-1"),          # by-job names ours
])
def test_reconcile_own_bulk_still_creating_or_unknown_blocks_missing(pb, monkeypatch, statuses, in_progress):
    pb["by_job"] = {"bulk_in_progress": True, "in_progress_details": {"bulk_id": in_progress}, "interviews": []}
    _bulk_status(monkeypatch, statuses)
    out = _reconcile(pb, ["a"], own_bulk_ids=["own-1"])
    assert out["missing"] == [] and out["error"] is None


class _StreamResp:
    def __init__(self, status: int, chunks: List[str]):
        self.status_code, self.chunks = status, chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def aiter_text(self):
        for chunk in self.chunks:
            yield chunk


class _StreamClient:
    def __init__(self, resp: _StreamResp):
        self.resp, self.urls = resp, []

    def __call__(self, *_a, **_k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def stream(self, method, url, headers=None):
        self.urls.append((method, url))
        return self.resp


@pytest.mark.parametrize("chunks,expected", [
    (['data: {"status": "crea', 'ting", "total": 3}\n\n', 'data: {"status": "x"}\n\n'], "creating"),
    ([': hi\r\n\r\n', 'data: {"status":"creation_completed","data":[]}\r\n\r\n'], "creation_completed"),
    (['data: {"status": "completed"}\n\n'], "completed"),
])
def test_fetch_pairbot_bulk_status_reads_the_first_event(monkeypatch, chunks, expected):
    client = _StreamClient(_StreamResp(200, chunks))
    monkeypatch.setattr(eng.httpx, "AsyncClient", client)
    monkeypatch.setattr(eng, "get_pair_auth_headers", lambda *a, **k: {})
    assert asyncio.run(eng._fetch_pairbot_bulk_status("bk-1")) == expected
    assert client.urls == [("GET", f"{eng.EXTERNAL_INTERVIEW_API_URL}/api/bulk-interviews/bk-1/stream")]


@pytest.mark.parametrize("resp", [_StreamResp(502, []), _StreamResp(200, [])])
def test_fetch_pairbot_bulk_status_raises_when_it_cannot_tell(monkeypatch, resp):
    monkeypatch.setattr(eng.httpx, "AsyncClient", _StreamClient(resp))
    monkeypatch.setattr(eng, "get_pair_auth_headers", lambda *a, **k: {})
    with pytest.raises(RuntimeError):
        asyncio.run(eng._fetch_pairbot_bulk_status("bk-1"))


def test_reconcile_skips_the_bulk_status_reads_when_nothing_is_creating(pb, monkeypatch):
    seen = _bulk_status(monkeypatch, {})
    out = _reconcile(pb, ["a"], own_bulk_ids=["own-1"])
    assert seen == [] and out["missing"] == ["a"]


def test_reconcile_reports_a_twin_as_duplicate_of_never_sent(pb):
    pb["by_job"] = {"bulk_in_progress": False, "interviews": [
        {"interview_id": "iv-X", "source_candidate_id": "X"}, {"interview_id": "iv-z"}]}
    out = _reconcile(pb, ["X", "Y", "Z", "W"],
                     existing_interview_ids={"Y": "iv-X", "Z": "iv-z", "W": "iv-elsewhere"})
    assert out["sent"] == {"X": "iv-X"}
    assert out["duplicate_of"] == {"Y": {"interview_id": "iv-X", "source_candidate_id": "X"},
                                   "Z": {"interview_id": "iv-z", "source_candidate_id": ""}}
    assert out["missing"] == ["W"]                          # its interview is not this job's
    assert pb["db"].reconcile_sent() == [("X", "iv-X", [JOB, REF])]


def test_reconcile_never_reports_missing_when_by_job_failed(pb, monkeypatch):
    async def flaky(job_key):
        if job_key == REF:
            raise RuntimeError("Pairbot by-job check failed status=502")
        return {"interviews": [{"interview_id": "iv-a", "source_candidate_id": "a"}]}
    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", flaky)
    out = _reconcile(pb, ["a", "b"])
    # What one key proved is still written; nothing is called missing.
    assert out["sent"] == {"a": "iv-a"} and out["missing"] == []
    assert "502" in out["error"]
    assert pb["db"].reconcile_sent() == [("a", "iv-a", [JOB, REF])]


def test_reconcile_write_failure_reports_unrecorded_not_sent(pb, monkeypatch):
    pb["by_job"] = {"interviews": [{"interview_id": "iv-a", "source_candidate_id": "a"}]}

    def broken(*_a, **_k):
        raise RuntimeError("deadlock")
    monkeypatch.setattr(eng, "_record_reconciled_interviews", broken)
    out = _reconcile(pb, ["a", "b"])
    assert out["sent"] == {} and out["unrecorded"] == {"a": "iv-a"}
    assert out["missing"] == ["b"] and "deadlock" in out["error"]


def test_reconcile_edge_inputs(pb):
    assert _reconcile(pb, []) == _EMPTY_RECONCILE
    out = asyncio.run(eng.reconcile_with_pairbot(("", None), ["a"]))
    assert out["error"] and out["missing"] == [] and pb["by_job_calls"] == []


def test_record_reconciled_uses_the_last_key_for_the_audit(pb):
    written = eng._record_reconciled_interviews((JOB, REF, REF), {"a": "iv-a", "b": ""})
    assert written == ["a"]
    (ins_sql, ins_params), = [(s, p) for s, p in pb["db"].log if "INSERT INTO engage_interview_audit" in s]
    assert "WHERE NOT EXISTS" in ins_sql and ins_params[:3] == ("a", REF, "iv-a")
    assert pb["t2"] == [REF]


def test_record_reconciled_locks_each_audit_insert_in_candidate_order(pb):
    eng._record_reconciled_interviews((JOB, REF), {"z": "iv-z", "a": "iv-a"})
    steps = [(p[0] if "pg_advisory_xact_lock" in s else ("insert", p[0]))
             for s, p in pb["db"].log
             if "pg_advisory_xact_lock" in s or "INSERT INTO engage_interview_audit" in s]
    assert steps == ["engage_audit:a:iv-a", ("insert", "a"), "engage_audit:z:iv-z", ("insert", "z")]


# ------------------------------------------------------------ claims ----

def test_claim_and_release_shapes(pb):
    db = pb["db"]
    assert eng.claim_candidates_for_session((JOB, REF), [], "S") == []
    assert eng.claim_candidates_for_session((JOB, REF), ["a"], "") == []
    assert eng.claim_candidates_for_session(("", ""), ["a"], "S") == []
    assert db.log == []                               # nothing to claim → no DB round-trip
    eng.claim_candidates_for_session((JOB, REF, JOB), ["a", "b", "a"], "S")
    (sql, params), = db.log
    assert sql is eng._CLAIM_SESSION_SQL and params == {"keys": [JOB, REF], "ids": ["a", "b"], "sid": "S"}
    db.log.clear()
    eng.release_session_claims("S", (JOB, REF), candidate_ids=[])
    assert db.log == []
    eng.release_session_claims("S", (JOB, REF))
    (sql, params), = db.log
    assert "ids" not in params and "candidate_id = ANY" not in sql and "'{}'::jsonb" in sql
    assert "engage_status_before_claim" in sql                  # restored, not blanked


# ------------------------------------------------------ provisioning ----

def test_provision_sent_candidates_claims_then_provisions_in_chunks(pb, monkeypatch):
    async def ids_for(job):
        return ("123", REF)
    monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", ids_for)
    claimed_calls: List[tuple] = []

    def claim(keys, ids):
        claimed_calls.append((keys, ids))
        return [c for c in ids if c != "x-1"]
    monkeypatch.setattr(eng, "_claim_rows_for_provisioning", claim)
    ids = [f"x-{i}" for i in range(30)]
    out = asyncio.run(eng.provision_sent_candidates(JOB, ids))
    assert claimed_calls == [([JOB, "123", REF], ids)]
    assert [len(c) for c, _ in pb["provisioned"]] == [25, 4]
    assert out["requested"] == 30 and out["claimed"] == 29 and out["not_claimed"] == 1
    assert out["success"] == 29


def test_provisioning_failure_is_counted_never_raised(pb, monkeypatch):
    async def ids_for(job):
        return ("123", REF)

    async def broken(ids, job_id, **_k):
        raise RuntimeError("JobDiva 500")
    monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", ids_for)
    monkeypatch.setattr(eng, "_claim_rows_for_provisioning", lambda keys, ids: list(ids))
    monkeypatch.setattr(eng, "_provision_batch_to_jobdiva", broken)
    out = asyncio.run(eng.provision_sent_candidates(JOB, ["a", "b"]))
    assert out["failed"] == 2 and out["success"] == 0
    assert asyncio.run(eng.provision_sent_candidates(JOB, []))["claimed"] == 0


# ------------------------------------------------------- engage status ----

def test_queued_and_in_flight_read_pending():
    for status in ("queued", "processing", "sending", "sent", "Initiated", "QUEUED"):
        assert format_engage_status(status, None, "") == "Pending"
    assert "queued" in LAUNCH_IN_FLIGHT_STATUSES
    # Unchanged rules around it.
    assert format_engage_status("passed", None, "") == "Pass"
    assert format_engage_status("failed", 40.0, "") == "Fail"
    assert format_engage_status("in_progress", None, "") == "In Progress"
