"""Async launch robustness: launch_events replay, stuck-run handling,
launch_id stream authorization and the conditional Pairbot webhook."""
import asyncio
import hashlib
import hmac
import json
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

import core.config as cfg
from core import tasks as core_tasks
from core.auth import UserIdentity
from routers import engagement as eng


class FakeLaunchDB:
    """In-memory stand-in for _launch_db, dispatching on SQL text."""

    def __init__(self):
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.events: Dict[str, List[tuple]] = {}
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.event_reads: List[int] = []

    def __call__(self, sql, params, fetch=False):
        if "INSERT INTO launch_runs" in sql:
            lid, job_id, requested_by = params
            self.runs[lid] = {"status": "running", "job_id": job_id,
                              "requested_by": requested_by, "stale": False}
            return None
        if "INSERT INTO launch_events" in sql:
            lid, seq, payload, _ = params
            evs = self.events.setdefault(lid, [])
            if all(s != seq for s, _p in evs):
                evs.append((seq, json.loads(payload)))
            if lid in self.runs:
                self.runs[lid]["stale"] = False
            return None
        if "UPDATE launch_runs SET status = 'failed'" in sql:
            ids = [params[1]] if len(params) > 1 else list(self.runs)
            out = []
            for lid in ids:
                r = self.runs.get(lid)
                if r and r["status"] == "running" and r["stale"]:
                    r["status"] = "failed"
                    out.append((lid,))
            return out
        if "UPDATE launch_runs SET status = %s" in sql:
            status, lid = params
            r = self.runs.get(lid)
            if r and r["status"] == "running":
                r["status"] = status
            return None
        if "SELECT status, COALESCE" in sql:
            r = self.runs.get(params[1])
            return [(r["status"], r["stale"])] if r else []
        if "FROM launch_events" in sql:
            lid, after = params
            self.event_reads.append(after)
            return sorted([e for e in self.events.get(lid, []) if e[0] > after])
        if "SELECT job_id, requested_by" in sql:
            r = self.runs.get(params[0])
            return [(r["job_id"], r["requested_by"])] if r else []
        if "UPDATE launch_batches" in sql:
            status, _ev, _err, bulk = params
            b = self.batches.get(bulk)
            if b is None or b["status"] in ("creation_completed", "creation_failed"):
                return []
            b["status"] = status
            return [("row",)]
        if "SELECT status FROM launch_batches" in sql:
            b = self.batches.get(params[0])
            return [(b["status"],)] if b else []
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.fixture
def db(monkeypatch):
    fake = FakeLaunchDB()
    monkeypatch.setattr(eng, "_launch_db", fake)
    return fake


async def _drain_stream(gen) -> List[Dict[str, Any]]:
    out = []
    async for chunk in gen:
        if chunk.startswith("data: "):
            out.append(json.loads(chunk[6:]))
    return out


# ------------------------------------------------------ launch_events ----

def test_stream_replays_from_start_and_reads_only_new_seqs(db):
    lid = "L1"
    db.runs[lid] = {"status": "running", "job_id": "J", "requested_by": None, "stale": False}
    for i in range(3):
        eng._launch_append_event(lid, i, {"type": "batch", "i": i})

    async def go():
        gen = eng._launch_progress_stream(lid)
        first = []
        async for chunk in gen:
            if chunk.startswith("data: "):
                first.append(json.loads(chunk[6:]))
            else:  # keepalive: write more, then finish the run
                eng._launch_append_event(lid, 3, {"type": "done"})
                eng._launch_finalize(lid, "completed")
        return first

    a = asyncio.run(go())
    assert [e.get("i", e["type"]) for e in a] == [0, 1, 2, "done"]
    assert db.event_reads == [-1, 2]  # second poll reads only seq > 2
    # A second connection replays the same events in the same order.
    b = asyncio.run(_drain_stream(eng._launch_progress_stream(lid)))
    assert b == a


def test_append_event_is_idempotent_per_seq(db):
    db.runs["L"] = {"status": "running", "job_id": "J", "requested_by": None, "stale": False}
    eng._launch_append_event("L", 0, {"type": "start"})
    eng._launch_append_event("L", 0, {"type": "dup"})
    assert db.events["L"] == [(0, {"type": "start"})]


# --------------------------------------------------------- stuck runs ----

def test_cancelled_async_launch_writes_failed(monkeypatch, db):
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", True)
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(eng, "_LAUNCH_BATCH_DELAY_SECONDS", 0.0)

    async def hang(*a, **k):
        await asyncio.sleep(60)
    monkeypatch.setattr(eng, "_run_one_batch", hang)

    async def go():
        resp = await eng.launch_bulk_interviews(eng.LaunchRequest(
            job_id="J1", candidate_ids=["c-0"], async_launch=True))
        await asyncio.sleep(0.05)
        await core_tasks.drain(timeout=0.05)  # shutdown: cancels the launch
        return json.loads(resp.body)["launch_id"]

    lid = asyncio.run(go())
    assert db.runs[lid]["status"] == "failed"
    types = [p["type"] for _s, p in db.events[lid]]
    assert types[0] == "start" and types[-1] == "error"


def test_stale_sweep_marks_running_failed(db):
    db.runs["old"] = {"status": "running", "job_id": "J", "requested_by": None, "stale": True}
    db.runs["live"] = {"status": "running", "job_id": "J", "requested_by": None, "stale": False}
    db.runs["done"] = {"status": "completed", "job_id": "J", "requested_by": None, "stale": True}
    assert eng._sweep_stale_launch_runs() == 1
    assert [db.runs[k]["status"] for k in ("old", "live", "done")] == ["failed", "running", "completed"]


def test_stream_sweeps_stale_run_and_ends_with_error(db):
    lid = "S"
    db.runs[lid] = {"status": "running", "job_id": "J", "requested_by": None, "stale": True}
    db.events[lid] = [(0, {"type": "start"})]
    events = asyncio.run(_drain_stream(eng._launch_progress_stream(lid)))
    assert [e["type"] for e in events] == ["start", "error"]
    assert db.runs[lid]["status"] == "failed"


def test_stream_has_hard_duration_cap(monkeypatch, db):
    monkeypatch.setattr(cfg, "LAUNCH_STREAM_MAX_SECONDS", 0.01, raising=False)
    lid = "C"
    db.runs[lid] = {"status": "running", "job_id": "J", "requested_by": None, "stale": False}
    events = asyncio.run(_drain_stream(eng._launch_progress_stream(lid)))
    assert events[-1]["type"] == "error" and "timed out" in events[-1]["message"]


# ---------------------------------------------------------------- auth ----

def _stream(launch_id):
    return asyncio.run(eng.stream_engagement_status(request=object(), launch_id=launch_id))


def test_launch_stream_requires_authentication(monkeypatch, db):
    def unauth(_req, _hdr):
        raise HTTPException(status_code=401, detail="Authentication required.")
    monkeypatch.setattr(eng, "get_current_user", unauth)
    db.runs["A"] = {"status": "completed", "job_id": "J", "requested_by": "me@x.com", "stale": False}
    with pytest.raises(HTTPException) as e:
        _stream("A")
    assert e.value.status_code == 401


def test_launch_stream_access_rules(monkeypatch, db):
    current = {"user": UserIdentity(email="me@x.com", role="recruiter")}
    monkeypatch.setattr(eng, "get_current_user", lambda _r, _h: current["user"])
    job_checks: List[str] = []

    def deny(job_id, user, **_k):
        job_checks.append(job_id)
        raise HTTPException(status_code=403, detail="no")
    monkeypatch.setattr(eng, "_verify_job_access_by_id", deny)
    db.runs["A"] = {"status": "completed", "job_id": "J", "requested_by": "me@x.com", "stale": False}

    assert _stream("A").media_type == "text/event-stream"  # requester
    current["user"] = UserIdentity(email="boss@x.com", role="admin")
    assert _stream("A").media_type == "text/event-stream"  # admin
    current["user"] = UserIdentity(email="other@x.com", role="recruiter")
    with pytest.raises(HTTPException) as e:
        _stream("A")
    assert e.value.status_code == 403 and job_checks == ["J"]
    monkeypatch.setattr(eng, "_verify_job_access_by_id", lambda *a, **k: None)
    assert _stream("A").media_type == "text/event-stream"  # has job access
    with pytest.raises(HTTPException) as e:
        _stream("missing")
    assert e.value.status_code == 404


def test_async_launch_records_requester(monkeypatch, db):
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", True)
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(eng, "get_current_user",
                        lambda _r, _h: UserIdentity(email="Me@X.com", role="recruiter"))

    async def one(idx, batch, **_k):
        return {"idx": idx, "status": "completed", "sent": len(batch), "already_sent": 0,
                "no_interview": 0, "employer_unverified_count": 0, "employer_unverified": [],
                "skipped": [], "excluded_records": [], "excluded_count": 0,
                "event": {"type": "batch", "index": idx}}
    monkeypatch.setattr(eng, "_run_one_batch", one)

    async def go():
        resp = await eng.launch_bulk_interviews(
            eng.LaunchRequest(job_id="J1", candidate_ids=["c-0"], async_launch=True),
            http_request=object(),
        )
        await core_tasks.drain(timeout=5)
        return json.loads(resp.body)["launch_id"]

    lid = asyncio.run(go())
    assert db.runs[lid]["requested_by"] == "me@x.com"
    assert db.runs[lid]["status"] == "completed"


# ------------------------------------------------------------- webhook ----

@pytest.fixture
def webhook(monkeypatch, db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import pairbot_webhook

    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("ENVIRONMENT", "production")
    app = FastAPI()
    app.include_router(pairbot_webhook.router)
    client = TestClient(app)

    def post(body):
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
        return client.post("/api/v1/webhooks/pairbot/creation", content=raw,
                           headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    return post


def test_webhook_moves_non_terminal_batch(webhook, db):
    db.batches["b1"] = {"status": "creating"}
    r = webhook({"bulk_id": "b1", "status": "creation_completed"})
    assert r.status_code == 200 and r.json() == {"status": "ok", "updated": True}
    assert db.batches["b1"]["status"] == "creation_completed"


def test_webhook_duplicate_terminal_is_idempotent(webhook, db):
    db.batches["b1"] = {"status": "creation_completed"}
    r = webhook({"bulk_id": "b1", "status": "creation_completed"})
    assert r.status_code == 200 and r.json() == {"status": "ok", "duplicate": True}


def test_webhook_late_event_does_not_overwrite_terminal(webhook, db):
    db.batches["b1"] = {"status": "creation_completed"}
    r = webhook({"bulk_id": "b1", "status": "creation_failed", "message": "late"})
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    assert db.batches["b1"]["status"] == "creation_completed"


def test_webhook_unknown_bulk_id(webhook, db):
    assert webhook({"bulk_id": "nope", "status": "creation_failed"}).json()["status"] == "ignored"
