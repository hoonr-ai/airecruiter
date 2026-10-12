"""Unit D1 — the launch-platform SQL against a real Postgres.

Claims, the claims-aware idempotency read, the by-job write-through, the
provisioning claim, the 'session' launch_runs status and the /candidates/save
rescore merge are all single statements whose correctness lives in jsonb
predicates — exactly what an SQL-dispatch fake cannot check. Most tests run in
TEMP tables inside one transaction that is always rolled back; the race tests
(two connections) use real tables in a throwaway schema that is dropped after.

Skipped when no Postgres is reachable: set LAUNCH_PLATFORM_TEST_DSN (falls
back to LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").
"""
import asyncio
import json
import os
import threading
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from routers import candidates as rc  # noqa: E402
from routers import engagement as eng  # noqa: E402

_DSN = os.getenv("LAUNCH_PLATFORM_TEST_DSN") or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")
JOB, REF = "J1", "26-001"

_FIXTURE_SQL = """
CREATE TEMP TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, jobdiva_id TEXT, candidate_id TEXT, source TEXT DEFAULT 'LinkedIn-Exa',
  name TEXT, email TEXT, phone TEXT, headline TEXT, location TEXT, profile_url TEXT, image_url TEXT,
  resume_id TEXT, resume_text TEXT, data JSONB, status TEXT, resume_match_percentage DOUBLE PRECISION,
  dnc_stopped_at TIMESTAMPTZ, created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now(),
  UNIQUE (jobdiva_id, candidate_id, source));
CREATE TEMP TABLE engage_interview_audit (
  id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
  interview_id VARCHAR(255), candidate_name VARCHAR(255), candidate_email VARCHAR(255),
  payload JSONB, response JSONB, status VARCHAR(50) DEFAULT 'sent',
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, jobdiva_id TEXT, outreach_stopped_at TIMESTAMPTZ,
  pair_launched_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, recruiter_emails TEXT, customer_name TEXT);
INSERT INTO monitored_jobs (job_id, jobdiva_id, recruiter_emails) VALUES ('J1', '26-001', '["r@x.com"]');
"""


class _Shared:
    """One connection for the module under test: its own commit/close are
    no-ops so the TEMP fixture survives until the final rollback."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def pg(monkeypatch):
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_FIXTURE_SQL)
            for stmt in eng._LAUNCH_DDL:
                cur.execute(stmt.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE IF NOT EXISTS"))
        shared = _Shared(conn)
        monkeypatch.setattr(eng, "_get_db_connection", lambda: shared)
        monkeypatch.setattr(rc, "get_db_connection", lambda: shared)
        monkeypatch.setattr(eng, "_stamp_first_launch", lambda key: None)
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _row(conn, cid, data, job=JOB, source="LinkedIn-Exa"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, data) VALUES (%s, %s, %s, %s::jsonb)",
            (job, cid, source, json.dumps(data)),
        )


def _audit(conn, cid, interview_id, job=REF):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status) VALUES (%s, %s, %s, 'Initiated')",
            (cid, job, interview_id),
        )


def _data(conn, cid, job=JOB):
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM sourced_candidates WHERE candidate_id = %s AND jobdiva_id = %s", (cid, job))
        return cur.fetchone()[0]


def _audits(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT jobdiva_id, interview_id, status FROM engage_interview_audit WHERE candidate_id = %s ORDER BY id", (cid,))
        return cur.fetchall()


# ------------------------------------------------------------- claims ----

def _seed_claim_rows(conn):
    _row(conn, "a", {})
    _row(conn, "b", {"engage_status": "failed", "keep": 1})
    _row(conn, "c", {"engage_status": "sent", "engage_interview_id": "iv-c"})
    _row(conn, "d", {"engage_status": "processing"})
    _row(conn, "e", {"engage_status": "failed", "engage_block_reason": "duplicate_role_other_job"})
    _row(conn, "f", {"engage_status": "failed", "engage_interview_id": "iv-f", "engage_score": "40"})
    _row(conn, "g", {})
    _audit(conn, "g", "iv-g")                                     # launched per the audit trail
    _row(conn, "h", {})
    _row(conn, "h", {"engage_status": "sent"}, job=REF)           # a sent twin row under the ref
    _row(conn, "i", {"engage_status": "queued", "engage_session_id": "S-OTHER"})
    _row(conn, "j", {})
    _row(conn, "j", {"engage_status": ""}, job=REF)
    _row(conn, "k", {}, job="OTHER-JOB")


ALL = list("abcdefghijk")


def test_claim_takes_only_claimable_people_and_is_exclusive(pg):
    _seed_claim_rows(pg)
    assert eng.claim_candidates_for_session((JOB, REF), ALL, "S-1") == ["a", "b", "j"]
    for cid in ("a", "b"):
        d = _data(pg, cid)
        assert d["engage_status"] == "queued" and d["engage_session_id"] == "S-1"
    assert _data(pg, "b")["keep"] == 1
    assert _data(pg, "a")["engage_status_before_claim"] == ""
    assert _data(pg, "b")["engage_status_before_claim"] == "failed"
    assert _data(pg, "j", job=REF)["engage_status"] == "queued"   # every row of the person
    assert _data(pg, "i")["engage_session_id"] == "S-OTHER"       # another session's row untouched
    # Idempotent for the same session (resume) — the pre-claim status survives
    # the re-claim — and exclusive for any other.
    assert eng.claim_candidates_for_session((JOB, REF), ALL, "S-1") == ["a", "b", "j"]
    assert _data(pg, "b")["engage_status_before_claim"] == "failed"
    assert eng.claim_candidates_for_session((JOB, REF), ALL, "S-2") == []


def test_release_hands_back_only_this_sessions_queued_rows(pg):
    _seed_claim_rows(pg)
    eng.claim_candidates_for_session((JOB, REF), ALL, "S-1")
    assert eng.release_session_claims("S-2", (JOB, REF)) == 0
    assert eng.release_session_claims("S-1", (JOB, REF), candidate_ids=["a"]) == 1
    assert _data(pg, "a") == {"engage_status": ""}
    assert eng.release_session_claims("S-1", (JOB, REF)) == 3   # b + both j rows
    # A previously failed row goes back to 'failed' (auto-launch only takes '').
    assert _data(pg, "b") == {"engage_status": "failed", "keep": 1}
    assert _data(pg, "i")["engage_status"] == "queued"          # S-OTHER keeps its row
    assert eng.claim_candidates_for_session((JOB, REF), ALL, "S-2") == ["a", "b", "j"]


def test_release_of_a_claim_made_before_the_restore_key_reads_blank(pg):
    _row(pg, "o", {"engage_status": "queued", "engage_session_id": "S-1"})
    assert eng.release_session_claims("S-1", (JOB, REF)) == 1
    assert _data(pg, "o") == {"engage_status": ""}


# ------------------------------------------------------------- send claim ----

def test_send_claim_takes_only_sendable_people(pg):
    _seed_claim_rows(pg)
    claimed, with_rows, dups = eng._claim_rows_for_send((JOB, REF), ALL + ["zz"], None)
    assert dups == {}
    assert claimed == ["a", "b", "j"]
    assert with_rows == set(ALL) - {"k"}                       # k is another job's; zz has no row
    assert _data(pg, "a")["engage_status"] == "sending"
    assert _data(pg, "j", job=REF)["engage_status"] == "sending"
    assert _data(pg, "b")["keep"] == 1
    assert _data(pg, "i")["engage_status"] == "queued"          # a session's row is not legacy's
    # Exclusive: nobody wins a row that is already 'sending'.
    assert eng._claim_rows_for_send((JOB, REF), ALL, None)[0] == []
    assert eng.claim_candidates_for_session((JOB, REF), ALL, "S-9") == []


def test_send_claim_takes_a_sessions_own_queued_rows_only_for_that_session(pg):
    _row(pg, "q", {})
    assert eng.claim_candidates_for_session((JOB, REF), ["q"], "S-1") == ["q"]
    assert eng._claim_rows_for_send((JOB, REF), ["q"], None)[0] == []      # legacy
    assert eng._claim_rows_for_send((JOB, REF), ["q"], "S-2")[0] == []     # another session
    assert eng._claim_rows_for_send((JOB, REF), ["q"], "S-1")[0] == ["q"]
    d = _data(pg, "q")
    assert d["engage_status"] == "sending" and d["engage_session_id"] == "S-1"
    assert d["engage_status_before_claim"] == ""


def test_release_send_claim_returns_session_rows_to_queued_and_others_to_failed(pg):
    _row(pg, "q", {})
    _row(pg, "l", {})
    _row(pg, "p", {"engage_status": "processing"})
    eng.claim_candidates_for_session((JOB, REF), ["q"], "S-1")
    eng._claim_rows_for_send((JOB, REF), ["q"], "S-1")
    eng._claim_rows_for_send((JOB, REF), ["l"], None)
    assert eng._release_send_claim((JOB, REF), ["q", "l", "p"], "S-1") == 2
    assert _data(pg, "q")["engage_status"] == "queued" and _data(pg, "q")["engage_session_id"] == "S-1"
    assert _data(pg, "l")["engage_status"] == "failed"
    assert _data(pg, "p")["engage_status"] == "processing"      # not 'sending': untouched
    assert eng.release_session_claims("S-1", (JOB, REF)) == 1
    assert _data(pg, "q")["engage_status"] == ""


def test_legacy_idempotency_read_treats_claims_and_blocks_correctly(pg):
    _seed_claim_rows(pg)
    eng.claim_candidates_for_session((JOB, REF), ["a"], "S-1")

    def read(session):
        with pg.cursor() as cur:
            cur.execute(eng._ENGAGE_IDEMPOTENCY_SQL, (session, session, ALL, JOB, REF))
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    legacy = read("")
    assert legacy["a"] == (True, None)                       # queued by a session → already sent
    assert legacy["b"] == (False, None)                      # failed → retryable
    assert legacy["c"][0] and legacy["d"][0] and legacy["f"][0] and legacy["h"][0]
    assert legacy["e"] == (False, "duplicate_role_other_job")
    assert legacy["i"] == (True, None) and legacy["j"] == (False, None)
    owner = read("S-1")
    assert owner["a"] == (False, None)                       # its own claim is sendable
    assert owner["i"] == (True, None)                        # someone else's is not


# --------------------------------------------------- by-job write-through ----

def test_record_reconciled_interviews_writes_sent_once_and_never_demotes(pg):
    _row(pg, "p", {"engage_status": "processing", "engage_bulk_id": "bk-1", "engage_session_id": "S-1"})
    _row(pg, "e", {"engage_status": "failed", "engage_block_reason": "duplicate_unverified"})
    _row(pg, "f", {"engage_status": "failed", "engage_interview_id": "iv-f", "engage_score": "40"})
    _row(pg, "w", {"engage_status": "in_progress", "engage_interview_id": "iv-w"})
    _audit(pg, "w", "iv-w")
    found = {"p": "iv-p", "e": "iv-e", "f": "iv-f", "w": "iv-w"}
    written = eng._record_reconciled_interviews((JOB, REF), found)
    assert sorted(written) == ["e", "f", "p"]                 # w: row moved on + audit present
    p = _data(pg, "p")
    assert p["engage_status"] == "sent" and p["engage_interview_id"] == "iv-p"
    assert p["engage_bulk_id"] == "bk-1" and p["engage_session_id"] == "S-1" and p["engage_updated_at"]
    e = _data(pg, "e")
    assert e["engage_status"] == "sent" and "engage_block_reason" not in e
    assert _data(pg, "f")["engage_status"] == "failed"        # an interview Fail is not pulled back
    assert _data(pg, "w")["engage_status"] == "in_progress"
    assert _audits(pg, "p") == [(REF, "iv-p", "Initiated")]
    assert _audits(pg, "f") == [(REF, "iv-f", "Initiated")]    # the missing audit row is added
    assert _audits(pg, "w") == [(REF, "iv-w", "Initiated")]    # never duplicated
    # A second pass writes nothing.
    assert eng._record_reconciled_interviews((JOB, REF), found) == []
    assert len(_audits(pg, "p")) == 1


def test_reconcile_with_pairbot_end_to_end(pg, monkeypatch):
    _row(pg, "p", {"engage_status": "processing"})
    _row(pg, "q", {"engage_status": "processing"})

    async def by_job(key):
        return {"bulk_in_progress": False,
                "interviews": [{"interview_id": "iv-p", "source_candidate_id": "p", "jobdiva_id": REF}]}
    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", by_job)
    out = asyncio.run(eng.reconcile_with_pairbot((JOB, REF), ["p", "q"], session_id="S-1"))
    assert out["sent"] == {"p": "iv-p"} and out["missing"] == ["q"] and out["error"] is None
    assert _data(pg, "p")["engage_interview_id"] == "iv-p"
    assert _data(pg, "q")["engage_status"] == "processing"


# ------------------------------------------------------ provisioning ----

def test_provisioning_claim_takes_launched_unprovisioned_rows_once(pg):
    _row(pg, "s1", {"engage_status": "sent", "engage_interview_id": "iv-1"})
    _row(pg, "s2", {"engage_status": "sent", "engage_interview_id": "iv-2", "jobdiva_candidate_id": "JD-2"})
    _row(pg, "s3", {})
    _row(pg, "s4", {"engage_status": "failed"})
    _audit(pg, "s4", "iv-4")
    _row(pg, "s5", {"engage_status": "sent"})
    _row(pg, "s6", {"engage_status": "in_progress", "engage_interview_id": "iv-6"})
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE sourced_candidates SET data = data || jsonb_build_object("
            "'jobdiva_provisioning_claimed_at', (now() - interval '20 minutes')::text) WHERE candidate_id = 's6'"
        )
        cur.execute(
            "UPDATE sourced_candidates SET data = data || jsonb_build_object("
            "'jobdiva_provisioning_claimed_at', now()::text) WHERE candidate_id = 's5'"
        )
    ids = ["s1", "s2", "s3", "s4", "s5", "s6"]
    assert eng._claim_rows_for_provisioning([JOB, REF], ids) == ["s1", "s4", "s6"]
    assert _data(pg, "s1")["jobdiva_provisioning_claimed_at"]
    assert eng._claim_rows_for_provisioning([JOB, REF], ids) == []     # live claims now


# ------------------------------------------------------- launch_runs ----

def test_session_runs_are_never_swept_or_finalized(pg, monkeypatch):
    sid = "6f1c2b9e-1d1a-4c55-9f43-2b1f4a0f8e11"
    eng.register_session_launch_run("00000000-0000-0000-0000-0000000000a1", JOB,
                                    requested_by="Me@X.com", launch_session_id=sid)
    eng.register_session_launch_run("00000000-0000-0000-0000-0000000000a1", JOB)   # idempotent
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO launch_runs (id, job_id, status, last_event_at) "
            "VALUES ('00000000-0000-0000-0000-0000000000b2', 'J1', 'running', now() - interval '2 hours')"
        )
        cur.execute("UPDATE launch_runs SET last_event_at = now() - interval '2 hours'")
    assert eng._sweep_stale_launch_runs() == 1
    eng._launch_finalize("00000000-0000-0000-0000-0000000000a1", "completed")
    with pg.cursor() as cur:
        cur.execute("SELECT id::text, status, requested_by, launch_session_id::text FROM launch_runs ORDER BY id")
        rows = cur.fetchall()
    assert rows == [
        ("00000000-0000-0000-0000-0000000000a1", "session", "me@x.com", sid),
        ("00000000-0000-0000-0000-0000000000b2", "failed", None, None),
    ]


# ------------------------------------------- /candidates/save rescore merge ----

def test_save_rescore_merges_and_keeps_launch_bookkeeping(pg, monkeypatch):
    from core.auth import UserIdentity
    from models import CandidateSaveRecord, CandidatesSaveRequest
    import services.no_contact as nc

    stored = {
        "engage_status": "sent", "engage_interview_id": "iv-L1", "engage_session_id": "S-1",
        "engage_bulk_id": "bk-1", "engage_block_reason": "", "jobdiva_candidate_id": "JD-9",
        "jobdiva_provisioning_claimed_at": "2026-10-11T10:00:00+00:00", "engage_last_response": {"data": None},
        "older_key": 1,
    }
    _row(pg, "L1", stored, job=REF)
    scores = iter([50.0, 71.0])

    def scorer(payload, criteria):
        s = next(scores)
        return {"score": s, "status": "done", "score_details": {"s": s}, "matched_skills": ["java"],
                "missing_skills": [], "explainability": ["x"], "scored_at": "2026-10-11T00:00:00+00:00"}

    async def no_gate(_job):
        return None
    monkeypatch.setattr(rc, "_verify_job_access_by_id", lambda *a, **k: None)
    monkeypatch.setattr(rc, "_enforce_recruiter_email_gate", no_gate)
    monkeypatch.setattr(rc, "stamp_job_launched_by", lambda *a, **k: None)
    monkeypatch.setattr(rc, "load_dnc_phone_set", lambda: set())
    monkeypatch.setattr(nc, "check_no_contact", lambda c: None)
    monkeypatch.setattr(rc, "_build_resume_matching_criteria", lambda job: None)
    monkeypatch.setattr(rc, "_compute_resume_matching", scorer)

    # Direct call — the launch runner's way in (user is a plain keyword).
    res = asyncio.run(rc.save_candidates(
        CandidatesSaveRequest(jobdiva_id=JOB, candidates=[CandidateSaveRecord(
            candidate_id="L1", name="Pat Doe", email="pat@x.com", phone="4155550100",
            source="LinkedIn-Exa", match_score=None, is_selected=True)]),
        user=UserIdentity(email="rec@x.com", role="recruiter"),
    ))
    assert res["saved_ids"] == ["L1"] and res["status"] == "success"
    d = _data(pg, "L1", job=REF)
    for key in ("engage_status", "engage_interview_id", "engage_session_id", "engage_bulk_id",
                "engage_block_reason", "jobdiva_candidate_id", "jobdiva_provisioning_claimed_at",
                "engage_last_response"):
        assert d[key] == stored[key], key
    assert d["older_key"] == 1
    assert d["match_score"] == 71.0 and d["resume_matching_score"] == 71.0   # the rescore ran
    assert d["score_locked_from_step5"] is False and d["is_selected"] is True
    with pg.cursor() as cur:
        cur.execute("SELECT resume_match_percentage FROM sourced_candidates WHERE candidate_id = 'L1'")
        assert cur.fetchone()[0] == 71.0
        cur.execute("SELECT pair_launched_at IS NOT NULL FROM monitored_jobs WHERE job_id = 'J1'")
        assert cur.fetchone()[0] is True


# ----------------------------------------------- Pairbot fakes for the core ----

def _fake_pairbot(monkeypatch, *, by_job=None, failed_interviews=None, resolve_delay=None):
    """Fakes everything the launch core reaches outside Postgres. Pairbot
    creates an interview for every posted resume unless ``failed_interviews``
    (by resume index) says otherwise. ``resolve_delay`` ({candidate_id: s})
    makes employer resolution sleep — the window between a legacy call's
    idempotency read and its POST."""
    import services.employer_resolution as er

    posts = []

    async def post(url, payload_obj, **_k):
        posts.append(json.loads(json.dumps(payload_obj)))

        class _R:
            status_code = 202
            content = b"{}"
            text = "{}"

            def json(self):
                return {"bulk_id": payload_obj.get("bulk_id")}
        return _R()

    async def wait(bulk_id, **_k):
        payload = next(p for p in posts if p.get("bulk_id") == bulk_id)
        failed = failed_interviews or {}
        data = [
            {"source_candidate_id": r["source_candidate_id"], "interview_id": f"iv-{r['source_candidate_id']}"}
            for i, r in enumerate(payload["resumes"]) if i not in failed
        ]
        return {"status": "creation_completed", "data": data,
                "failed_interviews": [dict(item, resume_index=i) for i, item in failed.items()]}

    async def fetch_by_job(key):
        return by_job() if callable(by_job) else (by_job or {"bulk_in_progress": False, "interviews": []})

    async def resolve(rows):
        ids = [str(r.get("candidate_id")) for r in rows]
        await asyncio.sleep(max([(resolve_delay or {}).get(c, 0.0) for c in ids] or [0.0]))
        return {c: {} for c in ids}

    async def no_freshness(_rows):
        return None

    def spawn(coro, name=None):
        coro.close()

    monkeypatch.setattr(eng, "_post_to_pairbot", post)
    monkeypatch.setattr(eng, "_wait_for_pairbot_creation", wait)
    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", fetch_by_job)
    monkeypatch.setattr(eng, "_fetch_stored_employer_signals", lambda _c, _ids: {})
    monkeypatch.setattr(eng, "_spawn_task", spawn)
    monkeypatch.setattr(er, "resolve_employer_signals", resolve)
    monkeypatch.setattr(er, "stamp_resume_freshness", no_freshness)
    return posts


def _launch_request(ids):
    return eng.SendBulkInterviewRequest(
        payload=json.dumps({
            "jd": {"job_id": JOB, "jobdiva_id": REF, "title": "Java Developer"},
            "resumes": [{"source_candidate_id": c, "name": f"N {c}", "email": f"{c}@x.com", "phone": "4155550100"}
                        for c in ids],
        }),
        real_candidate_ids=list(ids),
    )


def _posted_ids(posts):
    return [r["source_candidate_id"] for p in posts for r in p["resumes"]]


# ------------------------------------------- no double post across paths ----

@pytest.mark.parametrize("with_session", [True, False])
def test_two_legacy_launches_and_a_session_post_each_person_once(pg, monkeypatch, with_session):
    """The legacy reads run first and then sit in employer resolution; the
    session (or the faster legacy call) claims and posts meanwhile. Every
    later call loses the send claim instead of posting again."""
    for cid in ("a", "b"):
        _row(pg, cid, {})
    posts = _fake_pairbot(monkeypatch, resolve_delay={"a": 0.2})
    ids = ["a", "b"]

    async def legacy(delay):
        await asyncio.sleep(delay)
        # precomputed_signals=None → this call resolves employers (slowly).
        return await eng._send_bulk_interview_core(_launch_request(ids))

    async def session():
        await asyncio.sleep(0.05)                       # after both legacy reads
        assert eng.claim_candidates_for_session((JOB, REF), ids, "S-1") == ids
        return await eng._send_bulk_interview_core(
            _launch_request(ids), precomputed_signals={c: {} for c in ids}, claimed_session_id="S-1",
        )

    async def main():
        third = session() if with_session else legacy(0.1)
        return await asyncio.gather(legacy(0.0), legacy(0.01), third)

    results = asyncio.run(main())
    assert sorted(_posted_ids(posts)) == ["a", "b"]           # one post per person, total
    assert len(posts) == 1
    winners = [r for r in results if r.get("data")]
    assert len(winners) == 1 and sorted(d["interview_id"] for d in winners[0]["data"]) == ["iv-a", "iv-b"]
    for r in results:
        if r is not winners[0]:
            assert r["success"] is True and sorted(r["skipped_already_sent"]) == ["a", "b"]
    for cid in ids:
        assert _data(pg, cid)["engage_interview_id"] == f"iv-{cid}"
        assert [a[1] for a in _audits(pg, cid)] == [f"iv-{cid}"]


# ----------------------------------------------------------------- twins ----

def _seed_twin(conn):
    # X launched earlier (an Exa row); Y is X's JobDiva twin — same person.
    _row(conn, "X", {"engage_status": "sent", "engage_interview_id": "iv-X", "jobdiva_candidate_id": "Y"}, job=REF)
    _audit(conn, "X", "iv-X")
    _row(conn, "Y", {}, job=REF)


def _launched(conn):
    from services import launched_candidates as lc
    return lc.count_launched_candidates(conn, [REF, JOB])


def test_reconcile_never_records_a_twin_with_its_twins_interview(pg, monkeypatch):
    _seed_twin(pg)

    async def by_job(key):
        return {"bulk_in_progress": False, "interviews": [{"interview_id": "iv-X", "source_candidate_id": "X"}]}
    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", by_job)
    out = asyncio.run(eng.reconcile_with_pairbot((JOB, REF), ["Y"], existing_interview_ids={"Y": "iv-X"}))
    assert out["sent"] == {} and out["missing"] == []
    assert out["duplicate_of"] == {"Y": {"interview_id": "iv-X", "source_candidate_id": "X"}}
    assert _audits(pg, "Y") == [] and _data(pg, "Y", job=REF) == {}
    assert _launched(pg) == 1


def test_a_twin_duplicate_from_pairbot_is_blocked_and_counts_once(pg, monkeypatch):
    _seed_twin(pg)
    posts = _fake_pairbot(
        monkeypatch,
        by_job={"bulk_in_progress": False, "interviews": [{"interview_id": "iv-X", "source_candidate_id": "X"}]},
        failed_interviews={0: {"error_type": "DuplicateInterview", "existing_interview_id": "iv-X",
                               "error": "Duplicate interview for candidate and role"}},
    )
    res = asyncio.run(eng._send_bulk_interview_core(_launch_request(["Y"]), precomputed_signals={"Y": {}}))
    assert _posted_ids(posts) == ["Y"]
    assert res["excluded_candidates"] == [{
        "candidate_id": "Y", "name": "", "reason": "already has a PAIR interview for this job under another profile",
    }]
    y = _data(pg, "Y", job=REF)
    assert y["engage_status"] == "failed" and y["engage_interview_id"] == ""
    assert y["engage_block_reason"] == "duplicate_person_this_job"
    assert _audits(pg, "Y") == [(REF, "", "duplicate_this_job")]
    assert _launched(pg) == 1
    # Never re-posted by any path.
    assert eng._claim_rows_for_send((JOB, REF), ["Y"], None)[0] == []


def test_reconcile_holds_missing_while_a_bulk_is_creating(pg, monkeypatch):
    _row(pg, "q", {"engage_status": "processing"})

    async def by_job(key):
        return {"bulk_in_progress": True, "in_progress_details": {"bulk_id": "bk-1"}, "interviews": []}
    monkeypatch.setattr(eng, "_fetch_pairbot_job_interviews", by_job)
    out = asyncio.run(eng.reconcile_with_pairbot((JOB, REF), ["q"]))
    assert out["missing"] == [] and out["bulk_in_progress"] is True


# --------------------------------------------- two connections (row locks) ----

@pytest.fixture()
def pg_pair(monkeypatch):
    """Real tables in a throwaway schema, an autocommit seeding connection and
    two worker connections. `_get_db_connection` hands each thread the
    connection it registered (see _use)."""
    try:
        admin = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    schema = f"d1_launch_{uuid.uuid4().hex[:12]}"
    admin.autocommit = True
    workers = []
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'SET search_path TO "{schema}"')
            cur.execute(_FIXTURE_SQL.replace("CREATE TEMP TABLE", "CREATE TABLE"))
        for _ in range(2):
            workers.append(psycopg2.connect(
                _DSN, connect_timeout=3, options=f"-c search_path={schema} -c lock_timeout=15000",
            ))
        local = threading.local()
        monkeypatch.setattr(eng, "_get_db_connection", lambda: local.conn)
        monkeypatch.setattr(eng, "_stamp_first_launch", lambda key: None)
        yield admin, workers, local
    finally:
        for w in workers:
            try:
                w.rollback()
                w.close()
            except Exception:
                pass
        with admin.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


class _Gated:
    """A worker connection whose commit waits for `release` (set `reached`
    first): its transaction — and every lock it took — stays open until then."""

    def __init__(self, conn, *, gate: bool):
        self._conn = conn
        self.reached = threading.Event()
        self.release = threading.Event()
        if not gate:
            self.release.set()

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        self.reached.set()
        assert self.release.wait(20), "gate never released"
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        pass


def _in_thread(local, conn, fn, *args):
    box = {}

    def run():
        local.conn = conn
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def _waiting_on_a_lock(admin, worker) -> bool:
    with admin.cursor() as cur:
        cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (worker.get_backend_pid(),))
        row = cur.fetchone()
    return bool(row) and row[0] == "Lock"


def _seed(admin, cid, data, job=JOB):
    with admin.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, data) VALUES (%s, %s, %s::jsonb)",
            (job, cid, json.dumps(data)),
        )


def _status(admin, cid, job=JOB):
    with admin.cursor() as cur:
        cur.execute("SELECT data->>'engage_status' FROM sourced_candidates WHERE candidate_id = %s AND jobdiva_id = %s",
                    (cid, job))
        return cur.fetchone()[0]


def _race(admin, workers, local, first, second):
    """Run `first` on worker 0 and hold its transaction open; start `second`
    on worker 1, prove it is blocked on a row/advisory lock, then let the
    first commit. Returns (first result, second result)."""
    a = _Gated(workers[0], gate=True)
    b = _Gated(workers[1], gate=False)
    ta, box_a = _in_thread(local, a, *first)
    assert a.reached.wait(10), box_a
    tb, box_b = _in_thread(local, b, *second)
    deadline = time.monotonic() + 10
    while not _waiting_on_a_lock(admin, workers[1]):
        assert time.monotonic() < deadline and tb.is_alive(), f"second call never blocked: {box_b}"
        time.sleep(0.02)
    a.release.set()
    ta.join(10)
    tb.join(10)
    assert "error" not in box_a and "error" not in box_b, (box_a, box_b)
    return box_a["value"], box_b["value"]


def test_a_legacy_send_claim_and_a_session_claim_never_both_win(pg_pair):
    admin, workers, local = pg_pair
    for cid in ("a", "b"):
        _seed(admin, cid, {})
    _seed(admin, "b", {"engage_status": "failed"}, job=REF)           # b has two rows
    legacy, session = _race(
        admin, workers, local,
        (eng._claim_rows_for_send, (JOB, REF), ["a", "b"], None),
        (eng.claim_candidates_for_session, (JOB, REF), ["b", "a"], "S-1"),
    )
    assert legacy[0] == ["a", "b"] and session == []
    assert _status(admin, "b") == "sending" and _status(admin, "b", job=REF) == "sending"


def test_a_session_claim_blocks_a_concurrent_legacy_send_claim(pg_pair):
    admin, workers, local = pg_pair
    _seed(admin, "a", {})
    session, legacy = _race(
        admin, workers, local,
        (eng.claim_candidates_for_session, (JOB, REF), ["a"], "S-1"),
        (eng._claim_rows_for_send, (JOB, REF), ["a"], None),
    )
    assert session == ["a"] and legacy == ([], {"a"}, {})
    assert _status(admin, "a") == "queued"


def test_two_send_claims_in_opposite_order_do_not_deadlock(pg_pair):
    admin, workers, local = pg_pair
    for cid in ("a", "b"):
        _seed(admin, cid, {})
        _seed(admin, cid, {}, job=REF)
    first, second = _race(
        admin, workers, local,
        (eng._claim_rows_for_send, (JOB, REF), ["a", "b"], None),
        (eng._claim_rows_for_send, (JOB, REF), ["b", "a"], None),
    )
    assert first[0] == ["a", "b"] and second[0] == []


def test_concurrent_reconciles_insert_one_audit_row(pg_pair):
    admin, workers, local = pg_pair
    # Already 'sent' with its id, so the UPDATE takes no row lock: only the
    # advisory lock keeps the two NOT EXISTS checks from both inserting.
    _seed(admin, "w", {"engage_status": "sent", "engage_interview_id": "iv-w"})
    first, second = _race(
        admin, workers, local,
        (eng._record_reconciled_interviews, (JOB, REF), {"w": "iv-w"}),
        (eng._record_reconciled_interviews, (JOB, REF), {"w": "iv-w"}),
    )
    assert first == ["w"] and second == []
    with admin.cursor() as cur:
        cur.execute("SELECT count(*) FROM engage_interview_audit WHERE candidate_id = 'w'")
        assert cur.fetchone()[0] == 1
