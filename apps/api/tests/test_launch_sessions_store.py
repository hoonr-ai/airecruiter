"""The launch-session store contract, run against BOTH stores.

``memory``: tests/launch_sessions_fakes.MemoryStore (what the runner tests use).
``pg``: services/launch_sessions/store.PgStore on a real Postgres — tables in
a throwaway schema, dropped afterwards. Skipped when no Postgres is reachable:
set LAUNCH_SESSIONS_TEST_DSN (falls back to LAUNCH_PLATFORM_TEST_DSN, then
LAUNCH_REPORT_TEST_DSN, then "dbname=postgres").

The same assertions on both keep the in-memory twin honest, so the runner
suite's claims about fencing, leases and idempotency hold for the SQL too.
The pg-only tests at the bottom race real connections (SKIP LOCKED, the
global-cap advisory lock).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from typing import Any, Dict, List

import pytest

from services.launch_sessions import schema as S
from services.launch_sessions.errors import OwnershipLost
from services.launch_sessions.store import Fence, PgStore
from tests.launch_sessions_fakes import MemoryStore, add_job, new_session_fields

_DSN = (
    os.getenv("LAUNCH_SESSIONS_TEST_DSN") or os.getenv("LAUNCH_PLATFORM_TEST_DSN")
    or os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")
)

_PG_FIXTURE_SQL = """
CREATE TABLE monitored_jobs (
  job_id TEXT PRIMARY KEY, jobdiva_id TEXT, recruiter_emails TEXT, outreach_stopped_at TIMESTAMPTZ,
  pair_added_at TIMESTAMPTZ, first_launch_at TIMESTAMPTZ, customer_name TEXT, created_at TEXT);
CREATE TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, jobdiva_id TEXT, candidate_id TEXT, source TEXT, data JSONB,
  updated_at TIMESTAMP DEFAULT now(), UNIQUE (jobdiva_id, candidate_id, source));
CREATE TABLE engage_interview_audit (
  id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
  interview_id VARCHAR(255), status VARCHAR(50));
"""


class MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self.store = MemoryStore()

    def add_job(self, job_id: str = "1001", ref: str = "26-001") -> Dict[str, Any]:
        return add_job(self.store, job_id=job_id, ref=ref)

    def expire_lease(self, sid: str) -> None:
        self.store.expire_lease(sid)

    def age(self, sid: str, column: str, seconds: float) -> None:
        self.store.set_column(sid, column, self.store.t - dt.timedelta(seconds=seconds))

    def future(self, sid: str, column: str, seconds: float) -> None:
        self.store.set_column(sid, column, self.store.t + dt.timedelta(seconds=seconds))

    def sourced(self, key: str, cid: str, source: str = "LinkedIn-Exa", **data: Any) -> None:
        self.store.sourced[(key, cid)] = {"source": source, "data": dict(data)}

    def audit(self, cid: str, key: str, iid: str) -> None:
        self.store.audit.append({"candidate_id": cid, "jobdiva_id": key, "interview_id": iid})

    def engage_data(self, key: str, cid: str) -> Dict[str, Any]:
        return dict(self.store.sourced[(key, cid)]["data"])

    def close(self) -> None:
        pass


class PgBackend:
    name = "pg"

    def __init__(self) -> None:
        import psycopg2

        self.psycopg2 = psycopg2
        try:
            admin = psycopg2.connect(_DSN, connect_timeout=3)
        except psycopg2.OperationalError as exc:
            pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
        self.schema = f"ls_test_{uuid.uuid4().hex[:10]}"
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {self.schema}")
        admin.close()
        with self._raw() as conn:
            with conn.cursor() as cur:
                cur.execute(_PG_FIXTURE_SQL)
                for stmt in S.SCHEMA_STATEMENTS:
                    cur.execute(stmt)
            conn.commit()
        self.store = PgStore(connect=self._raw)

    def _raw(self):
        conn = self.psycopg2.connect(_DSN, connect_timeout=3, options=f"-c search_path={self.schema}")
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        conn.commit()
        return conn

    def _exec(self, sql: str, params: Any = ()) -> None:
        conn = self._raw()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def add_job(self, job_id: str = "1001", ref: str = "26-001") -> Dict[str, Any]:
        self._exec(
            "INSERT INTO monitored_jobs (job_id, jobdiva_id, recruiter_emails, customer_name, created_at) "
            "VALUES (%s, %s, %s, 'Acme', '2026-10-01 10:00:00')",
            (job_id, ref, '["r@x.com"]'),
        )
        return {"job_id": job_id, "jobdiva_id": ref}

    def expire_lease(self, sid: str) -> None:
        self._exec("UPDATE launch_sessions SET lease_until = now() - interval '1 second' WHERE id = %s", (sid,))

    def age(self, sid: str, column: str, seconds: float) -> None:
        self._exec(f"UPDATE launch_sessions SET {column} = now() - make_interval(secs => %s) WHERE id = %s",
                   (seconds, sid))

    def future(self, sid: str, column: str, seconds: float) -> None:
        self._exec(f"UPDATE launch_sessions SET {column} = now() + make_interval(secs => %s) WHERE id = %s",
                   (seconds, sid))

    def sourced(self, key: str, cid: str, source: str = "LinkedIn-Exa", **data: Any) -> None:
        self._exec("INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, data) VALUES (%s, %s, %s, %s)",
                   (key, cid, source, json.dumps(data)))

    def audit(self, cid: str, key: str, iid: str) -> None:
        self._exec("INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status) "
                   "VALUES (%s, %s, %s, 'Initiated')", (cid, key, iid))

    def engage_data(self, key: str, cid: str) -> Dict[str, Any]:
        conn = self._raw()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM sourced_candidates WHERE jobdiva_id = %s AND candidate_id = %s", (key, cid))
                return dict(cur.fetchone()[0])
        finally:
            conn.close()

    def close(self) -> None:
        conn = self.psycopg2.connect(_DSN, connect_timeout=3)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {self.schema} CASCADE")
        conn.close()


@pytest.fixture(params=["memory", "pg"])
def be(request):
    backend = MemoryBackend() if request.param == "memory" else PgBackend()
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture()
def pg():
    backend = PgBackend()
    try:
        yield backend
    finally:
        backend.close()


def _create(be, job=None, **kw):
    job = job or be.add_job()
    fields = new_session_fields(job, **kw)
    row = be.store.create_session(fields, kw.pop("rows", None) or [], {"type": "created"})
    return fields, row


def _rows(*ids, state=S.ROW_POOLED):
    return [{"candidate_id": c, "source": "LinkedIn-Exa", "row": {"candidate_id": c, "resume_text": "long"},
             "match_score": 80, "state": state} for c in ids]


# ── create / idempotency / one active per job ────────────────────────────────

def test_create_is_idempotent_and_one_session_is_active_per_job(be):
    job = be.add_job()
    f1 = new_session_fields(job, key="k1")
    row = be.store.create_session(f1, _rows("a", "b"), {"type": "created"})
    assert row["id"] == f1["id"] and row["status"] == "queued" and row["cand_rev"] == 1
    assert be.store.create_session(dict(f1, id=str(uuid.uuid4())), [], None) is None  # same key
    assert be.store.find_by_key(job["job_id"], "k1")["id"] == f1["id"]
    f2 = new_session_fields(job, key="k2")
    assert be.store.create_session(f2, [], None) is None  # another active session
    assert be.store.active_session(job["job_id"])["id"] == f1["id"]
    other = be.add_job("2002", "26-002")
    assert be.store.create_session(new_session_fields(other, key="k1"), [], None) is not None  # key is per job
    be.store.cancel_session(f1["id"], "x")
    assert be.store.create_session(f2, [], None)["id"] == f2["id"]
    assert [e["seq"] for e in be.store.events_after(f1["id"])] == [1]


# ── claims and leases ────────────────────────────────────────────────────────

def test_claim_is_exclusive_and_an_expired_lease_is_reclaimed_with_fencing(be):
    f, _ = _create(be)
    sid = f["id"]
    [a] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    assert (a.session_id, a.claims) == (sid, 1)
    assert be.store.claim("B", 1, global_cap=3, lease_s=180) == []
    fa = Fence(sid, "A", 1)
    assert be.store.heartbeat(fa, lease_s=180) is False
    be.expire_lease(sid)
    [b] = be.store.claim("B", 1, global_cap=3, lease_s=180)
    assert (b.session_id, b.claims) == (sid, 2)
    # A is fenced out of every write.
    with pytest.raises(OwnershipLost):
        be.store.heartbeat(fa, lease_s=180)
    with pytest.raises(OwnershipLost):
        be.store.update_session(fa, values={"stage": "done"})
    with pytest.raises(OwnershipLost):
        be.store.set_states(fa, [{"candidate_id": "a", "state": "launched"}])
    with pytest.raises(OwnershipLost):
        be.store.assert_owner(fa, lease_s=180, margin_s=30)
    assert be.store.release(fa) is None
    assert be.store.append_event(sid, {"type": "x"}, fa) is None
    s = be.store.get_session(sid)
    assert s["owner"] == "B" and s["claims"] == 2 and s["stage"] == "created"


def test_global_cap_counts_live_leases_only(be):
    sids = []
    for i in range(4):
        job = be.add_job(f"{3000 + i}", f"26-3{i:02d}")
        f, _ = _create(be, job)
        sids.append(f["id"])
    got = be.store.claim("A", 5, global_cap=2, lease_s=180)
    assert len(got) == 2
    assert be.store.claim("B", 5, global_cap=2, lease_s=180) == []
    # A shutdown release (lease_until = now) frees the slot.
    assert be.store.release(Fence(got[0].session_id, "A", 1)) == 0
    again = be.store.claim("B", 5, global_cap=2, lease_s=180)
    assert len(again) == 1


def test_not_before_backoff_and_failure_counting(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    assert be.store.release(Fence(sid, "A", c.claims), not_before_s=600, error="boom", count_failure=True) == 1
    s = be.store.get_session(sid)
    assert s["attempt"] == 1 and s["error"] == "boom" and s["status"] == "running"
    assert be.store.claim("B", 1, global_cap=3, lease_s=180) == []
    be.age(sid, "not_before", 1)
    [c2] = be.store.claim("B", 1, global_cap=3, lease_s=180)
    assert c2.claims == 2 and be.store.get_session(sid)["not_before"] is None


def test_cancel_requested_sessions_are_still_claimed(be):
    f, _ = _create(be)
    sid = f["id"]
    be.store.claim("A", 1, global_cap=3, lease_s=180)
    assert be.store.cancel_session(sid, "stop")[0] == "requested"
    be.expire_lease(sid)
    [c] = be.store.claim("B", 1, global_cap=3, lease_s=180)
    assert c.session_id == sid and be.store.heartbeat(Fence(sid, "B", 2), lease_s=180) is True


def test_assert_owner_needs_lease_margin(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(sid, "A", c.claims)
    assert be.store.assert_owner(fence, lease_s=180, margin_s=30) is False
    be.future(sid, "lease_until", 10)
    with pytest.raises(OwnershipLost):
        be.store.assert_owner(fence, lease_s=180, margin_s=30)


# ── fenced writes ────────────────────────────────────────────────────────────

def test_update_session_merges_and_stamps(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(sid, "A", c.claims)
    r1 = be.store.update_session(fence, values={"stage": "sourcing"}, coalesce_now=["sourcing_started_at"],
                                 checkpoint={"s1": {"next_attempt": 1}}, counts={"pool": 3})
    first = r1["sourcing_started_at"]
    r2 = be.store.update_session(fence, coalesce_now=["sourcing_started_at"], set_now=["stage_started_at"],
                                 checkpoint={"s2": {"done": True}}, counts={"selected": 2})
    assert r2["sourcing_started_at"] == first and r2["stage_started_at"] is not None
    assert r2["checkpoint"] == {"s1": {"next_attempt": 1}, "s2": {"done": True}}
    assert r2["counts"] == {"pool": 3, "selected": 2} and r2["stage"] == "sourcing"
    with pytest.raises(ValueError):
        be.store.update_session(fence, values={"owner": "evil"})
    be.store.update_session(fence, checkpoint={"s7": {"inflight": {}}})
    be.store.set_checkpoint_path(fence, ["s7", "inflight", "bulk-1"], {"ids": ["a"]})
    be.store.set_checkpoint_path(fence, ["s9", "missing", "x"], 1)  # parent absent: no-op
    ck = be.store.get_session(sid)["checkpoint"]
    assert ck["s7"]["inflight"] == {"bulk-1": {"ids": ["a"]}} and "s9" not in ck


def test_candidate_states_and_keyset_paging(be):
    f, _ = _create(be, rows=_rows(*[f"c{i:02d}" for i in range(7)]))
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(sid, "A", c.claims)
    changed = be.store.set_states(fence, [
        {"candidate_id": "c01", "state": "selected", "contact": {"done": True}},
        {"candidate_id": "c02", "state": "skipped", "reason": "below_floor"},
        {"candidate_id": "zz", "state": "selected"},
    ])
    assert sorted(changed) == ["c01", "c02"]
    be.store.set_states(fence, [{"candidate_id": "c01", "contact": {"override": {"phone": "1"}}}])
    assert be.store.set_states(fence, [{"candidate_id": "c02", "state": "launched"}], from_states=["selected"]) == []
    rows = {r["candidate_id"]: r for r in be.store.candidate_rows(sid)}
    assert rows["c01"]["contact"] == {"done": True, "override": {"phone": "1"}}
    assert (rows["c02"]["state"], rows["c02"]["reason"]) == ("skipped", "below_floor")
    # Keyset paging (rev, id) never skips a row written while paging.
    seen: List[str] = []
    cursor = (0, "")
    first = True
    while True:
        page, total = be.store.candidates_page(sid, limit=3, after_rev=cursor[0], after_id=cursor[1])
        if first:
            be.store.set_states(fence, [{"candidate_id": "c00", "state": "selected"}])
            first = False
        seen += [r["candidate_id"] for r in page]
        assert all("resume_text" not in r["row"] for r in page)
        if not page:
            break
        cursor = (page[-1]["rev"], page[-1]["candidate_id"])
    assert set(seen) == {f"c{i:02d}" for i in range(7)}
    assert seen.count("c00") == 2  # re-read after its change
    full, _ = be.store.candidates_page(sid, limit=50, light=False)
    assert full[0]["row"]["resume_text"] == "long"
    counts = be.store.state_counts(sid)
    assert counts["states"] == {"selected": 2, "skipped": 1, "pooled": 4}
    assert counts["reasons"] == {"skipped": {"below_floor": 1}}


def test_stream_log_and_s1_flush(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(sid, "A", c.claims)
    be.store.stream_append(fence, 0, 0, [{"type": "candidate", "data": {"candidate_id": "a"}}])
    be.store.s1_flush(fence, attempt=1, chunk=0, events=[{"type": "stage", "data": "x"}],
                      rows=_rows("a"), checkpoint_s1={"next_attempt": 1}, counts={"pool": 1})
    be.store.stream_append(fence, 1, 1, [{"type": "keep"}])
    assert be.store.stream_load(sid) == {0: [{"type": "candidate", "data": {"candidate_id": "a"}}],
                                         1: [{"type": "stage", "data": "x"}, {"type": "keep"}]}
    s = be.store.get_session(sid)
    assert s["checkpoint"]["s1"] == {"next_attempt": 1} and s["counts"]["pool"] == 1
    assert be.store.candidate_ids(sid) == ["a"]
    be.store.stream_delete(fence, from_attempt=1)
    assert list(be.store.stream_load(sid)) == [0]
    be.expire_lease(sid)
    be.store.claim("B", 1, global_cap=3, lease_s=180)
    with pytest.raises(OwnershipLost):
        be.store.s1_flush(fence, attempt=2, chunk=0, events=[{"type": "x"}], rows=_rows("zz"),
                          checkpoint_s1={}, counts={})
    assert "zz" not in be.store.candidate_ids(sid)


def test_side_effects_guard_is_claimed_once(be):
    f, _ = _create(be)
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(f["id"], "A", c.claims)
    assert be.store.claim_side_effects(fence) is True
    assert be.store.claim_side_effects(fence) is False
    be.store.reset_side_effects(f["id"])
    assert be.store.claim_side_effects(fence) is True


def test_finish_and_recheck(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    row = be.store.finish(Fence(sid, "A", c.claims), status="completed", error=None,
                          counts={"sent": 2}, recheck_in_s=0)
    assert (row["status"], row["stage"]) == ("completed", "done") and row["completed_at"] is not None
    assert row["counts"]["sent"] == 2 and row["recheck_at"] is not None
    due = be.store.claim_rechecks(limit=5, every_s=300)
    assert [d["id"] for d in due] == [sid]
    assert be.store.claim_rechecks(limit=5, every_s=300) == []  # pushed ahead
    be.store.finish_recheck(sid, counts={"unconfirmed": 0}, done=True)
    assert be.store.get_session(sid)["recheck_at"] is None


# ── browser-side transitions ────────────────────────────────────────────────

def test_cancel_at_each_status(be):
    job = be.add_job()
    for status in ("collecting", "queued", "awaiting_input"):
        f = new_session_fields(job, status=status)
        be.store.create_session(f, [], None)
        outcome, row = be.store.cancel_session(f["id"], "cancelled by user")
        assert outcome == "cancelled" and row["status"] == "cancelled" and row["completed_at"] is not None
    f = new_session_fields(job)
    be.store.create_session(f, [], None)
    be.store.claim("A", 1, global_cap=3, lease_s=180)
    outcome, row = be.store.cancel_session(f["id"], "stop it")
    assert outcome == "requested" and row["status"] == "running" and row["cancel_requested"]
    be.store.finish(Fence(f["id"], "A", 1), status="cancelled", error="stop it", counts={})
    assert be.store.cancel_session(f["id"], "again")[0] == "not_active"


def test_collecting_upload_and_start(be):
    job = be.add_job()
    f = new_session_fields(job, mode="provided", status="collecting")
    be.store.create_session(f, [], None)
    sid = f["id"]
    assert be.store.start_session(sid) == (None, "empty")
    assert be.store.add_candidates(sid, _rows("a", "b"), max_total=3)["total"] == 2
    res = be.store.add_candidates(sid, _rows("b", "c", "d"), max_total=3)
    assert res["too_many"] and not res["ok"]
    assert be.store.add_candidates(sid, _rows("c"), max_total=3)["ok"]
    assert [r["ord"] for r in be.store.candidate_rows(sid)] == [0, 1, 2]
    row, reason = be.store.start_session(sid)
    assert row["status"] == "queued" and reason is None
    assert be.store.start_session(sid) == (None, "queued")
    assert be.store.add_candidates(sid, _rows("e"), max_total=9) == {
        "ok": False, "status": "queued", "total": 0, "too_many": False}


def test_review_handoff_and_confirm(be):
    f, _ = _create(be, rows=_rows("a", "b", "c", state=S.ROW_SELECTED))
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    row = be.store.to_awaiting_input(Fence(sid, "A", c.claims), {"review": {"count": 3}})
    assert row["status"] == "awaiting_input" and row["owner"] is None and row["lease_until"] is None
    assert be.store.claim("B", 1, global_cap=3, lease_s=180) == []
    row = be.store.confirm_review(sid, {"a": {"phone": "+15550001", "email": None},
                                        "b": {"phone": None, "email": None}}, {"confirmed": True})
    assert row["status"] == "queued" and row["checkpoint"]["review"] == {"confirmed": True}
    assert row["review_done_at"] is not None
    rows = {r["candidate_id"]: r for r in be.store.candidate_rows(sid)}
    assert rows["a"]["contact"]["final"] == {"phone": "+15550001", "email": None}
    assert rows["b"]["contact"]["final"] == {"phone": None, "email": None}
    assert (rows["c"]["state"], rows["c"]["reason"]) == ("skipped", "review_removed")
    assert be.store.confirm_review(sid, {}, {}) is None


def test_child_sessions(be):
    f, _ = _create(be, rows=_rows("a", "b", "c", state=S.ROW_NEEDS_CONTACT))
    sid = f["id"]
    job = {"job_id": f["job_id"], "jobdiva_id": f["jobdiva_ref"]}
    child_fields = new_session_fields(job, mode="provided", trigger="needs_contact", parent_session_id=sid, key="c1")
    # The parent is still active: the child conflicts.
    assert be.store.create_child(child_fields, _rows("a"), parent_id=sid, handed_off=["a"], skipped=["c"]) is None
    be.store.cancel_session(sid, "done")
    child = be.store.create_child(child_fields, _rows("a"), parent_id=sid, handed_off=["a"], skipped=["c"],
                                  event={"type": "created"}, parent_event={"type": "child_created"})
    assert child["parent_session_id"] == sid and child["status"] == "queued"
    rows = {r["candidate_id"]: r for r in be.store.candidate_rows(sid)}
    assert (rows["a"]["state"], rows["a"]["reason"]) == ("handed_off", f"child:{child['id']}")
    assert (rows["c"]["state"], rows["c"]["reason"]) == ("skipped", "skipped_by_recruiter")
    assert rows["b"]["state"] == "needs_contact"
    assert [r["candidate_id"] for r in be.store.candidate_rows(child["id"])] == ["a"]
    assert [c["id"] for c in be.store.children(sid)] == [child["id"]]
    assert be.store.events_after(sid)[-1]["payload"] == {"type": "child_created"}


def test_events_sequence(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    assert be.store.append_event(sid, {"type": "a"}) == 2
    assert be.store.append_event(sid, {"type": "b"}, Fence(sid, "A", c.claims)) == 3
    assert [e["seq"] for e in be.store.events_after(sid, 1)] == [2, 3]
    assert [e["payload"]["type"] for e in be.store.events_tail(sid, 2)] == ["a", "b"]


# ── housekeeping ─────────────────────────────────────────────────────────────

def test_housekeeping_expires_abandoned_uploads_and_reviews(be):
    job1, job2, job3 = be.add_job("4001", "26-401"), be.add_job("4002", "26-402"), be.add_job("4003", "26-403")
    up = new_session_fields(job1, mode="provided", status="collecting")
    be.store.create_session(up, [], None)
    fresh = new_session_fields(job3, mode="provided", status="collecting")
    be.store.create_session(fresh, [], None)
    rv = new_session_fields(job2, status="awaiting_input")
    be.store.create_session(rv, [], None)
    be.age(up["id"], "updated_at", 16 * 60)
    be.age(rv["id"], "review_started_at", 25 * 3600)
    out = be.store.housekeeping(collecting_ttl_min=15, review_ttl_h=24, retention_days=14, recheck_h=24)
    assert out["collecting_expired"] == [up["id"]] and out["review_expired"] == [rv["id"]]
    assert be.store.get_session(up["id"])["error"] == "upload abandoned"
    r = be.store.get_session(rv["id"])
    assert r["status"] == "cancelled" and r["error"] == "review expired" and r["human_wait_ms"] >= 25 * 3600 * 1000
    assert be.store.get_session(fresh["id"])["status"] == "collecting"


def test_housekeeping_retention_keeps_rows_under_recheck(be):
    job = be.add_job()
    f = new_session_fields(job)
    be.store.create_session(f, _rows("a"), None)
    be.store.claim("A", 1, global_cap=3, lease_s=180)
    be.store.finish(Fence(f["id"], "A", 1), status="completed", error=None, counts={}, recheck_in_s=60)
    be.age(f["id"], "completed_at", 15 * 86400)
    be.store.housekeeping(collecting_ttl_min=15, review_ttl_h=24, retention_days=14, recheck_h=24 * 30)
    assert be.store.candidate_ids(f["id"]) == ["a"]  # still being re-checked
    be.store.housekeeping(collecting_ttl_min=15, review_ttl_h=24, retention_days=14, recheck_h=24)
    assert be.store.candidate_ids(f["id"]) == []


# ── sourced_candidates facts ─────────────────────────────────────────────────

def test_launch_state_engage_facts_requeue_and_fail(be):
    job = be.add_job()
    jid, ref = job["job_id"], job["jobdiva_id"]
    sid = str(uuid.uuid4())
    be.sourced(ref, "a", engage_status="processing", engage_session_id=sid, engage_bulk_id="b1")
    be.sourced(jid, "a", engage_status="queued", engage_session_id=sid)
    be.sourced(ref, "b", engage_status="sending", engage_session_id="other")
    be.sourced(ref, "c", engage_status="sent", engage_interview_id="iv-c", jobdiva_candidate_id="jd-c")
    be.sourced(ref, "d", source="JobDiva-Applicants")
    be.audit("e", ref, "iv-e")
    facts = {r["candidate_id"]: r for r in be.store.job_launch_state([jid, ref]) if r["candidate_id"] != "a"}
    assert facts["c"]["interview_id"] == "iv-c" and facts["c"]["jobdiva_candidate_id"] == "jd-c"
    assert facts["e"]["audit_launched"] is True
    assert facts["d"]["source"] == "JobDiva-Applicants"
    engage = be.store.engage_rows([jid, ref], ["a", "b", "c"])
    assert sorted(engage["a"]["statuses"]) == ["processing", "queued"] and engage["a"]["bulk_ids"] == ["b1"]
    assert engage["c"]["interview_id"] == "iv-c"
    assert be.store.requeue_engage_rows([jid, ref], ["a", "b", "c"], sid) == ["a"]
    assert be.engage_data(ref, "a")["engage_status"] == "queued"
    assert be.engage_data(ref, "b")["engage_status"] == "sending"  # someone else's
    assert be.store.fail_engage_rows([jid, ref], ["a"], sid) == ["a"]
    data = be.engage_data(ref, "a")
    assert data["engage_status"] == "failed" and "engage_session_id" not in data


def test_resolve_job_prefers_the_numeric_row(be):
    be.add_job("5001", "26-501")
    job = be.store.resolve_job("26-501")
    assert job["job_id"] == "5001" and job["jobdiva_id"] == "26-501"
    assert be.store.resolve_job("nope") is None


# ── review fixes (durability / parity) ─────────────────────────────────────

def test_release_clears_the_owner_so_a_late_heartbeat_cannot_re_lease(be):
    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(sid, "A", c.claims)
    assert be.store.release(fence) == 0
    s = be.store.get_session(sid)
    assert s["owner"] is None and s["status"] == "running" and s["claims"] == c.claims
    with pytest.raises(OwnershipLost):  # the heartbeat that was in flight
        be.store.heartbeat(fence, lease_s=180)
    assert be.store.release(fence) is None
    [b] = be.store.claim("B", 1, global_cap=3, lease_s=180)
    assert b.session_id == sid and b.claims == c.claims + 1


def test_a_cancel_is_claimable_at_once_past_back_off_and_the_caps(be):
    jobs = [be.add_job(f"{7000 + i}", f"26-7{i:02d}") for i in range(2)]
    f1, _ = _create(be, jobs[0])
    [c] = be.store.claim("A", 1, global_cap=1, lease_s=180)
    be.store.release(Fence(f1["id"], "A", c.claims), not_before_s=600)  # RetryLater / back-off
    f2, _ = _create(be, jobs[1])
    [c2] = be.store.claim("B", 1, global_cap=1, lease_s=180)
    assert c2.session_id == f2["id"]  # the only global slot is taken now
    outcome, row = be.store.cancel_session(f1["id"], "stop")
    assert outcome == "requested" and row["not_before"] is None
    [c3] = be.store.claim("C", 1, global_cap=1, lease_s=180)
    assert c3.session_id == f1["id"]  # finalized now, not in 10 minutes
    # A runner that hits a wait after the cancel landed sets no back-off …
    be.store.release(Fence(f1["id"], "C", c3.claims), not_before_s=600)
    assert be.store.get_session(f1["id"])["not_before"] is None
    [c4] = be.store.claim("D", 1, global_cap=1, lease_s=180)
    # … but a failed finalize does (no hot loop on a persistent error).
    be.store.release(Fence(f1["id"], "D", c4.claims), not_before_s=600, cancel_skips_wait=False)
    assert be.store.get_session(f1["id"])["not_before"] is not None
    assert be.store.claim("E", 1, global_cap=1, lease_s=180) == []


def test_review_handoff_never_happens_over_a_pending_cancel(be):
    from services.launch_sessions.errors import CancelRequested

    f, _ = _create(be)
    sid = f["id"]
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    assert be.store.cancel_session(sid, "stop")[0] == "requested"
    with pytest.raises(CancelRequested):
        be.store.to_awaiting_input(Fence(sid, "A", c.claims), {"review": {"requested": True}})
    s = be.store.get_session(sid)
    assert s["status"] == "running" and s["owner"] == "A" and s["cancel_requested"]
    with pytest.raises(OwnershipLost):
        be.store.to_awaiting_input(Fence(sid, "A", c.claims + 1), {})


def test_search_and_launch_phases_have_their_own_caps_and_children_go_first(be):
    sids = {}
    for i, (name, mode, stage) in enumerate((
        ("s1", "full_search", "sourcing"), ("s2", "full_search", "created"), ("s3", "full_search", "selecting"),
        ("l1", "full_search", "enriching"), ("p1", "provided", "created"), ("p2", "provided", "created"),
    )):
        job = be.add_job(f"{8000 + i}", f"26-8{i:02d}")
        f = new_session_fields(job, mode=mode, stage=stage)
        be.store.create_session(f, [], None)
        sids[name] = f["id"]
    # Three searches fill the search cap; the launch cap is separate.
    got = be.store.claim("A", 2, global_cap=2, lease_s=180, search_cap=3)
    assert {c.session_id for c in got} == {sids["l1"], sids["p1"]}  # launch first, oldest first, cap 2
    got = be.store.claim("B", 5, global_cap=2, lease_s=180, search_cap=3)
    assert {c.session_id for c in got} == {sids["s1"], sids["s2"], sids["s3"]}
    assert be.store.claim("C", 5, global_cap=2, lease_s=180, search_cap=3) == []
    # A child waits behind no one in its phase.
    assert be.store.queue_position(sids["p2"]) == 0
    parent = sids["l1"]
    job = be.add_job("8100", "26-900")
    child = new_session_fields(job, mode="provided", trigger="needs_contact", parent_session_id=parent)
    be.store.create_session(child, [], None)
    assert be.store.queue_position(child["id"]) == 0 and be.store.queue_position(sids["p2"]) == 1
    assert be.store.queue_position(sids["l1"]) is None  # running, not waiting
    be.store.release(Fence(sids["l1"], "A", 1))
    [c] = be.store.claim("D", 1, global_cap=2, lease_s=180, search_cap=3)
    assert c.session_id == child["id"]
    # Past S7 (provisioning / done) a session is under no global cap: it is
    # adopted even with both caps full.
    job = be.add_job("8200", "26-950")
    wrap = new_session_fields(job, stage="provisioning")
    be.store.create_session(wrap, [], None)
    assert be.store.queue_position(wrap["id"]) == 0
    assert [c.session_id for c in be.store.claim("E", 5, global_cap=2, lease_s=180, search_cap=3)] == [wrap["id"]]


def test_a_session_past_s7_no_longer_holds_its_job(be):
    job = be.add_job()
    f, _ = _create(be, job)
    [c] = be.store.claim("A", 1, global_cap=3, lease_s=180)
    fence = Fence(f["id"], "A", c.claims)
    second = new_session_fields(job, key="k2")
    assert be.store.create_session(second, [], None) is None
    be.store.update_session(fence, values={"stage": "provisioning"})
    assert be.store.active_session(job["job_id"]) is None
    assert be.store.create_session(second, [], None)["id"] == second["id"]
    assert be.store.active_session(job["job_id"])["id"] == second["id"]
    # The provisioning session keeps running (fenced writes still work).
    be.store.update_session(fence, values={"stage": "done"})
    assert be.store.finish(fence, status="completed", error=None, counts={})["status"] == "completed"


def test_park_posted_rows_moves_only_this_sessions_queued_rows(be):
    job = be.add_job()
    jid, ref = job["job_id"], job["jobdiva_id"]
    sid = str(uuid.uuid4())
    be.sourced(ref, "a", engage_status="queued", engage_session_id=sid)
    be.sourced(jid, "a", engage_status="queued", engage_session_id=sid)
    be.sourced(ref, "b", engage_status="queued", engage_session_id="other")
    be.sourced(ref, "c", engage_status="sent", engage_session_id=sid, engage_interview_id="iv")
    be.sourced(ref, "d", engage_status="processing", engage_session_id=sid, engage_bulk_id="b0")
    be.sourced(ref, "e", engage_status="queued", engage_session_id=sid)
    got = be.store.park_posted_rows([jid, ref], {"a": "bulk-a", "b": "bulk-b", "c": "bulk-c", "d": "bulk-d",
                                                 "e": ""}, sid)
    assert got == ["a", "e"]
    a = be.engage_data(ref, "a")
    assert a["engage_status"] == "processing" and a["engage_bulk_id"] == "bulk-a" and a["engage_session_id"] == sid
    assert be.engage_data(jid, "a")["engage_status"] == "processing"
    assert be.engage_data(ref, "b")["engage_status"] == "queued"
    assert be.engage_data(ref, "d")["engage_bulk_id"] == "b0"
    e = be.engage_data(ref, "e")
    assert e["engage_status"] == "processing" and "engage_bulk_id" not in e
    # The housekeeping re-check can still fail a parked row Pairbot never created.
    assert be.store.fail_engage_rows([jid, ref], ["a"], sid) == ["a"]


def test_a_child_can_carry_its_parents_click(be):
    job = be.add_job()
    f = new_session_fields(job, mode="provided", trigger="needs_contact")
    clicked = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
    row = be.store.create_session(dict(f, clicked_at=clicked, human_wait_ms=600_000), [], None)
    assert row["clicked_at"] == clicked and row["human_wait_ms"] == 600_000
    plain = be.store.create_session(new_session_fields(be.add_job("9001", "26-991")), [], None)
    assert plain["clicked_at"] is not None and plain["human_wait_ms"] == 0


# ── pg only: real races ──────────────────────────────────────────────────────

def test_pg_concurrent_claims_never_double_claim_and_respect_the_cap(pg):
    for i in range(6):
        job = pg.add_job(f"{6000 + i}", f"26-6{i:02d}")
        pg.store.create_session(new_session_fields(job), [], None)
    barrier = threading.Barrier(8)
    results: List[List[Any]] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        barrier.wait()
        got = pg.store.claim(f"W{n}", 2, global_cap=3, lease_s=180)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed = [c.session_id for r in results for c in r]
    assert len(claimed) == 3 == len(set(claimed))


def test_pg_a_fenced_write_holds_the_row_against_a_claim(pg):
    job = pg.add_job()
    f = new_session_fields(job)
    pg.store.create_session(f, [], None)
    [c] = pg.store.claim("A", 1, global_cap=3, lease_s=180)
    pg.expire_lease(f["id"])
    conn = pg._raw()
    try:
        cur = conn.cursor()
        # A's fenced transaction is open (row locked) when B's tick runs:
        cur.execute("UPDATE launch_sessions SET updated_at = now() WHERE id = %s AND owner = 'A' AND claims = 1 "
                    "AND status = 'running' RETURNING id", (f["id"],))
        assert cur.fetchone()
        assert pg.store.claim("B", 1, global_cap=3, lease_s=180) == []  # SKIP LOCKED
        conn.commit()
    finally:
        conn.close()
    [b] = pg.store.claim("B", 1, global_cap=3, lease_s=180)
    assert b.claims == 2


def test_pg_engage_facts_follow_the_d1_claims(pg, monkeypatch):
    """The runner reads / requeues rows that D1's session claim and send
    claim wrote: the same SQL predicates, on one real table."""
    from routers import engagement as eng

    monkeypatch.setattr(eng, "_get_db_connection", pg._raw)
    pg.add_job()
    keys = ("1001", "26-001")
    pg.sourced("26-001", "a")
    pg.sourced("26-001", "b", engage_status="failed")
    sid = str(uuid.uuid4())
    assert eng.claim_candidates_for_session(keys, ["a", "b"], sid) == ["a", "b"]
    facts = pg.store.engage_rows(keys, ["a", "b"])
    assert facts["a"]["statuses"] == ["queued"] and facts["a"]["session_ids"] == [sid]
    claimed, _ = eng._claim_rows_for_send(keys, ["a"], sid)
    assert claimed == ["a"] and pg.store.engage_rows(keys, ["a"])["a"]["statuses"] == ["sending"]
    # Pairbot provably never got it: the runner requeues it and D1 re-claims it.
    assert pg.store.requeue_engage_rows(keys, ["a"], sid) == ["a"]
    assert eng.claim_candidates_for_session(keys, ["a"], sid) == ["a"]
    # Another session can't take either; the release restores the pre-claim status.
    assert eng.claim_candidates_for_session(keys, ["a", "b"], str(uuid.uuid4())) == []
    assert eng.release_session_claims(sid, keys) == 2
    assert pg.engage_data("26-001", "a")["engage_status"] == ""
    assert pg.engage_data("26-001", "b")["engage_status"] == "failed"
    state = {r["candidate_id"]: r for r in pg.store.job_launch_state(list(keys))}
    assert state["b"]["engage_status"] == "failed" and not state["b"]["audit_launched"]


def test_pg_finish_parks_a_posted_row_so_no_launch_path_can_post_it_again(pg, monkeypatch):
    """review-durability R1 on the real D1 SQL: a session row that D1's
    no-answer path put back to 'queued' is parked 'processing' at finish —
    neither a legacy send claim nor another session's claim can take it."""
    from routers import engagement as eng

    monkeypatch.setattr(eng, "_get_db_connection", pg._raw)
    pg.add_job()
    keys = ("1001", "26-001")
    pg.sourced("26-001", "c1")
    pg.sourced("26-001", "c2")
    sid = str(uuid.uuid4())
    assert eng.claim_candidates_for_session(keys, ["c1", "c2"], sid) == ["c1", "c2"]
    assert eng._claim_rows_for_send(keys, ["c1"], sid)[0] == ["c1"]
    eng._release_send_claim(keys, ["c1"], sid)  # posted, no answer from Pairbot
    assert pg.engage_data("26-001", "c1")["engage_status"] == "queued"
    # finish: c2 was never posted (released), c1 was (parked).
    assert eng.release_session_claims(sid, keys, ["c2"]) == 1
    assert pg.store.park_posted_rows(list(keys), {"c1": "bulk-1"}, sid) == ["c1"]
    assert pg.engage_data("26-001", "c2")["engage_status"] == ""
    assert eng._claim_rows_for_send(keys, ["c1"], None)[0] == []
    assert eng.claim_candidates_for_session(keys, ["c1"], str(uuid.uuid4())) == []
    assert pg.store.engage_rows(list(keys), ["c1"])["c1"]["bulk_ids"] == ["bulk-1"]
    state = {r["candidate_id"]: r for r in pg.store.job_launch_state(list(keys))}
    assert state["c1"]["engage_status"] == "processing"  # S2: in_flight
