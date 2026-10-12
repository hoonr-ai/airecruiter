"""Launch pipeline optimizations ("pipeline optimization.md" Fixes 1, 2, 3, 6, 9)."""
import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Dict, List

import pytest

import core.config as cfg
from core import tasks as core_tasks
from routers import engagement as eng


def _run(coro):
    return asyncio.run(coro)


async def _collect(resp) -> List[Dict[str, Any]]:
    out = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        if chunk.startswith("data: "):
            out.append(json.loads(chunk[6:]))
    return out


# ---------------------------------------------------------------- Fix 9 ----

def test_spawn_keeps_reference_and_logs_failures(caplog):
    async def boom():
        raise ValueError("kaput")

    async def ok():
        await asyncio.sleep(0.01)
        return 1

    async def main():
        t1 = core_tasks.spawn(ok(), name="ok")
        t2 = core_tasks.spawn(boom(), name="boom")
        assert core_tasks.pending_count() >= 1
        await asyncio.gather(t1, t2, return_exceptions=True)
        await asyncio.sleep(0)
        assert core_tasks.pending_count() == 0

    with caplog.at_level("ERROR"):
        _run(main())
    assert any("background_task_failed name=boom" in r.getMessage() for r in caplog.records)


def test_drain_cancels_stragglers():
    async def main():
        t = core_tasks.spawn(asyncio.sleep(30), name="slow")
        await core_tasks.drain(timeout=0.05)
        return t

    t = _run(main())
    assert t.cancelled()


# ---------------------------------------------------------------- Fix 6 ----

class _FakeCursor:
    def __init__(self, live: Dict[str, Dict[str, Any]], dnc: set):
        self.live, self.dnc = live, dnc
        self.sql = ""
        self.params = None
        self.connection = self
        self.executed: List[str] = []

    def rollback(self):
        pass

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        self.executed.append(sql)

    def fetchall(self):
        if "DISTINCT ON (candidate_id)" in self.sql:
            return [self.live[c] for c in self.params[0] if c in self.live]
        if "dnc_stopped_at IS NOT NULL" in self.sql:
            return [{"candidate_id": c} for c in self.params[0] if c in self.dnc]
        return []

    def fetchone(self):
        return None

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self, **_kw):
        return self.cur

    def close(self):
        pass

    def commit(self):
        pass


def test_generate_payload_single_query_preserves_request_order(monkeypatch):
    def row(cid, name):
        return {"candidate_id": cid, "name": name, "email": f"{cid}@x.com",
                "phone": "5551234567", "headline": "", "location": "", "data": {}}
    live = {"c3": row("c3", "Cee Three"), "c1": row("c1", "Cee One"), "c2": row("c2", "Cee Two")}
    cur = _FakeCursor(live, dnc={"c9"})
    monkeypatch.setattr(eng, "_get_db_connection", lambda: _FakeConn(cur))

    from types import SimpleNamespace

    async def _no_gender(*_a, **_k):
        return SimpleNamespace(gender_label="default", confidence=0.0, source="test")
    monkeypatch.setattr(eng, "infer_gender_from_name_ai", _no_gender, raising=False)

    res = _run(eng._generate_payload_for(
        eng.GeneratePayloadRequest(candidate_ids=["c2", "c9", "c1", "c8", "c3"], job_id="J1")
    ))
    payload = json.loads(res["payload"])
    ids = [r.get("source_candidate_id") for r in payload["resumes"]]
    # c9 is DNC (dropped), c8 unknown (stub kept in position).
    assert ids == ["c2", "c1", "c8", "c3"]
    assert res["dnc_blocked_ids"] == ["c9"]
    per_candidate = [s for s in cur.executed if "FROM sourced_candidates" in s]
    assert len(per_candidate) == 2  # one live ANY() + one DNC ANY(), not N+1


def test_fetch_live_rows_schema_drift_fallback():
    class Drift(_FakeCursor):
        def execute(self, sql, params=None):
            if "resume_match_percentage" in sql:
                raise RuntimeError("column does not exist")
            super().execute(sql, params)
    cur = Drift({"a": {"candidate_id": "a"}}, set())
    assert list(eng._fetch_live_sourced_rows(cur, ["a", "a", "b"])) == ["a"]


# ------------------------------------------------------- Fix 1 / Fix 2 ----

@pytest.fixture
def fake_launch(monkeypatch):
    """Fake payload generation + Pairbot send; records each batch's call."""
    calls: List[Dict[str, Any]] = []
    state = {"delay": 0.0, "fail_idx": None, "abort_idx": None, "dnc": set()}
    monkeypatch.setattr(eng, "_LAUNCH_BATCH_DELAY_SECONDS", 0.0)

    async def fake_generate(req):
        resumes = [{"source_candidate_id": c, "name": c, "email": f"{c}@x.com", "phone": ""}
                   for c in req.candidate_ids if c not in state["dnc"]]
        return {"payload": json.dumps({"jd": {"job_id": req.job_id}, "resumes": resumes}),
                "dnc_blocked_ids": [c for c in req.candidate_ids if c in state["dnc"]]}

    async def fake_core(req, precomputed_signals=None, launch_id=None, batch_idx=None):
        first = req.real_candidate_ids[0]
        idx = int(first.split("-")[1]) // 75
        calls.append({"idx": idx, "ids": list(req.real_candidate_ids),
                      "precomputed": precomputed_signals, "t": time.perf_counter()})
        await asyncio.sleep(state["delay"])
        if state["abort_idx"] == idx:
            raise eng.HTTPException(status_code=409, detail="outreach stopped")
        if state["fail_idx"] == idx:
            return {"success": False, "message": "pairbot down"}
        return {"success": True, "bulk_id": f"b{idx}",
                "data": [{"interview_id": f"i-{c}"} for c in req.real_candidate_ids]}

    monkeypatch.setattr(eng, "_generate_payload_for", fake_generate)
    monkeypatch.setattr(eng, "_send_bulk_interview_core", fake_core)
    return calls, state


def _launch(ids, **kw):
    req = eng.LaunchRequest(job_id="J1", candidate_ids=ids, batch_size=75, **kw)

    async def go():
        return await _collect(await eng.launch_bulk_interviews(req))
    return _run(go())


def test_gate_resolved_once_for_300_id_launch(monkeypatch, fake_launch):
    calls, _ = fake_launch
    ids = [f"c-{i}" for i in range(300)]
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", True)
    monkeypatch.setattr(cfg, "LAUNCH_BATCH_PARALLELISM", 2)

    class Cur:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchone(self):
            return ("J1", "JD1")

        def fetchall(self):
            return [(c, {}, c, "", "", "jobdiva") for c in ids]

        def close(self):
            pass

    monkeypatch.setattr(eng, "_get_db_connection", lambda: _FakeConn(Cur()))
    monkeypatch.setattr(eng, "_fetch_stored_employer_signals", lambda _c, _ids: {})
    import services.employer_resolution as er
    n = {"resolve": 0}

    async def fake_resolve(cands):
        n["resolve"] += 1
        return {"c-1": {"title": "Eng"}}

    async def fake_stamp(cands):
        for c in cands:
            if c["candidate_id"] == "c-2":
                c["resume_updated_at"] = "2026-01-01"
    monkeypatch.setattr(er, "resolve_employer_signals", fake_resolve)
    monkeypatch.setattr(er, "stamp_resume_freshness", fake_stamp)

    events = _launch(ids)
    assert n["resolve"] == 1
    assert len(calls) == 4
    pre = calls[0]["precomputed"]
    assert pre is not None and set(pre) == set(ids)
    assert pre["c-1"] == {"title": "Eng"} and pre["c-2"]["resume_updated_at"] == "2026-01-01"
    assert all(c["precomputed"] is pre for c in calls)
    assert events[0]["type"] == "start" and events[0]["run_id"]
    assert events[-1]["totals"]["sent"] == 300


def test_gate_flag_off_passes_no_precomputed(monkeypatch, fake_launch):
    calls, _ = fake_launch
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    _launch([f"c-{i}" for i in range(80)])
    assert [c["precomputed"] for c in calls] == [None, None]


def test_batches_run_concurrently(monkeypatch, fake_launch):
    calls, state = fake_launch
    state["delay"] = 0.3
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_BATCH_PARALLELISM", 4)
    t = time.perf_counter()
    events = _launch([f"c-{i}" for i in range(300)])
    assert time.perf_counter() - t < 0.9  # sequential would be >= 1.2s
    assert events[-1]["totals"]["sent"] == 300
    assert sorted(e["index"] for e in events if e["type"] == "batch") == [0, 1, 2, 3]


def test_parallelism_one_is_sequential_and_ordered(monkeypatch, fake_launch):
    calls, state = fake_launch
    state["delay"] = 0.05
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_BATCH_PARALLELISM", 1)
    events = _launch([f"c-{i}" for i in range(300)])
    assert [e["index"] for e in events if e["type"] == "batch"] == [0, 1, 2, 3]
    assert [c["idx"] for c in calls] == [0, 1, 2, 3]
    for a, b in zip(calls, calls[1:]):
        assert b["t"] - a["t"] >= 0.05


def test_retry_set_only_contains_sendable_ids(monkeypatch, fake_launch):
    _, state = fake_launch
    state["fail_idx"] = 1
    state["dnc"] = {"c-80"}
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_BATCH_PARALLELISM", 3)
    events = _launch([f"c-{i}" for i in range(225)])
    done = events[-1]
    expected = [f"c-{i}" for i in range(75, 150) if i != 80]
    assert done["failed_candidate_ids"] == expected
    assert done["totals"]["failed_batches"] == 1
    assert done["totals"]["sent"] == 150


def test_409_aborts_remaining_batches_sequential(monkeypatch, fake_launch):
    calls, state = fake_launch
    state["abort_idx"] = 1
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_BATCH_PARALLELISM", 1)
    events = _launch([f"c-{i}" for i in range(300)])
    err = [e for e in events if e["type"] == "error"][0]
    assert err["candidate_ids"] == [f"c-{i}" for i in range(75, 300)]
    assert [c["idx"] for c in calls] == [0, 1]
    assert events[-1]["aborted"] is True
    assert events[-1]["totals"]["failed_batches"] == 1


# ---------------------------------------------------------------- Fix 3 ----

def test_idempotency_key_is_order_independent():
    a = eng._launch_batch_idempotency_key("L", 2, ["b", "a"])
    b = eng._launch_batch_idempotency_key("L", 2, ["a", "b"])
    assert a == b == hashlib.sha1(b"L:2:a,b").hexdigest()
    assert a != eng._launch_batch_idempotency_key("L", 3, ["a", "b"])


def test_async_launch_returns_202_and_records_events(monkeypatch, fake_launch):
    calls, _ = fake_launch
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", True)
    writes: List[tuple] = []

    def fake_db(sql, params, fetch=False):
        writes.append((sql, params))
        return [] if fetch else None
    monkeypatch.setattr(eng, "_launch_db", fake_db)

    async def go():
        resp = await eng.launch_bulk_interviews(eng.LaunchRequest(
            job_id="J1", candidate_ids=[f"c-{i}" for i in range(80)], batch_size=75,
            async_launch=True,
        ))
        await core_tasks.drain(timeout=5)
        return resp

    resp = _run(go())
    assert resp.status_code == 202
    launch_id = json.loads(resp.body)["launch_id"]
    assert all(c["idx"] in (0, 1) for c in calls) and len(calls) == 2
    appends = [p for s, p in writes if "INSERT INTO launch_events" in s]
    assert [p[1] for p in appends] == list(range(len(appends)))  # seq 0..n-1
    events = [json.loads(p[2]) for p in appends]
    assert events[0]["type"] == "start" and events[0]["launch_id"] == launch_id
    assert events[-1]["type"] == "done"
    assert any("status = %s, finished_at" in s and p[0] == "completed" for s, p in writes)


def test_async_flag_off_keeps_sse(monkeypatch, fake_launch):
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", False)
    events = _launch(["c-0"], async_launch=True)
    assert events[-1]["type"] == "done"


def test_webhook_or_sse_prefers_first_success(monkeypatch):
    async def slow_sse(bulk_id, timeout_seconds=600.0):
        await asyncio.sleep(5)

    async def hook(bulk_id, timeout_seconds=600.0):
        return {"status": "creation_completed", "data": [1]}
    monkeypatch.setattr(eng, "_wait_for_pairbot_creation", slow_sse)
    monkeypatch.setattr(eng, "_poll_launch_batch_creation", hook)
    assert _run(eng._wait_for_creation_webhook_or_sse("b"))["data"] == [1]


def test_webhook_or_sse_falls_back_when_one_fails(monkeypatch):
    async def sse(bulk_id, timeout_seconds=600.0):
        await asyncio.sleep(0.05)
        return {"status": "creation_completed", "data": [2]}

    async def hook(bulk_id, timeout_seconds=600.0):
        raise RuntimeError("db down")
    monkeypatch.setattr(eng, "_wait_for_pairbot_creation", sse)
    monkeypatch.setattr(eng, "_poll_launch_batch_creation", hook)
    assert _run(eng._wait_for_creation_webhook_or_sse("b"))["data"] == [2]


def test_pairbot_creation_webhook(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import pairbot_webhook

    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    seen: List[tuple] = []

    def fake_db(sql, params, fetch=False):
        seen.append(params)
        if "UPDATE launch_batches" in sql:
            return [("row",)] if params[3] == "bulk-1" else []
        return []
    monkeypatch.setattr(eng, "_launch_db", fake_db)
    app = FastAPI()
    app.include_router(pairbot_webhook.router)
    client = TestClient(app)

    def post(body):
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
        return client.post("/api/v1/webhooks/pairbot/creation", content=raw,
                           headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})

    r = post({"bulk_id": "bulk-1", "status": "creation_completed", "data": []})
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert seen[0][0] == "creation_completed"
    assert post({"bulk_id": "nope", "status": "creation_failed"}).json()["status"] == "ignored"
    assert post({"bulk_id": "bulk-1", "status": "creating"}).json()["status"] == "ignored"
    bad = client.post("/api/v1/webhooks/pairbot/creation", content=b"{}",
                      headers={"X-Hub-Signature-256": "sha256=00"})
    assert bad.status_code == 401
