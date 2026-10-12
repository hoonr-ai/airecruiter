"""Unit D1 — `run_launch` (the /engage/launch pipeline as a module-level async
generator) and the launch-platform hooks around it.

The wire contract is pinned against output captured from the code BEFORE the
extraction (develop @9f5a71de, the endpoint's inline `_work`): the same faked
pipeline must still produce byte-identical SSE chunks and the same side
effects. Everything a launch session adds rides in an `_internal` key the
endpoint strips.
"""
import asyncio
import contextlib
import json
from typing import Any, Dict, List

import pytest

import core.config as cfg
from core import tasks as core_tasks
from routers import engagement as eng


# Captured from routers/engagement.py at 9f5a71de with the fakes below.
GOLDEN = json.loads(r'''{
    "mixed": {
        "chunks": [
            "data: {\"type\": \"start\", \"total_candidates\": 8, \"total_batches\": 4, \"batch_size\": 2, \"run_id\": \"RUN-FIXED\"}\n\n",
            "data: {\"type\": \"batch\", \"index\": 0, \"status\": \"completed\", \"sent\": 1, \"no_interview\": 0, \"already_sent\": 0, \"excluded\": 0, \"employer_unverified\": 1, \"bulk_id\": \"b0\"}\n\n",
            "data: {\"type\": \"batch\", \"index\": 1, \"status\": \"failed\", \"error\": \"pairbot down\", \"candidate_ids\": [\"c-2\"]}\n\n",
            "data: {\"type\": \"batch\", \"index\": 2, \"status\": \"completed\", \"sent\": 1, \"no_interview\": 1, \"already_sent\": 1, \"excluded\": 1, \"employer_unverified\": 0, \"bulk_id\": \"b2\"}\n\n",
            "data: {\"type\": \"batch\", \"index\": 3, \"status\": \"completed\", \"sent\": 0, \"no_interview\": 0, \"already_sent\": 0, \"excluded\": 2, \"bulk_id\": null}\n\n",
            "data: {\"type\": \"done\", \"aborted\": false, \"totals\": {\"sent\": 2, \"already_sent\": 1, \"failed_batches\": 1, \"no_interview\": 1, \"excluded\": 4, \"employer_unverified\": 1}, \"skipped_already_sent\": [\"c-9\"], \"excluded_candidates\": [{\"candidate_id\": \"c-3\", \"name\": \"N c-3\", \"reason\": \"no usable phone or email at launch\"}, {\"candidate_id\": \"c-8\", \"name\": \"\", \"reason\": \"client employee\"}, {\"candidate_id\": \"c-6\", \"name\": \"N c-6\", \"reason\": \"no usable phone or email at launch\"}, {\"candidate_id\": \"c-7\", \"name\": \"N c-7\", \"reason\": \"no usable phone or email at launch\"}], \"employer_unverified\": [{\"candidate_id\": \"c-0\", \"name\": \"N c-0\", \"employer_verification\": \"unverified\"}], \"failed_candidate_ids\": [\"c-2\"]}\n\n"
        ],
        "emails": [
            {
                "job_id": "J1",
                "candidate_count": 2,
                "send_job_posting": false,
                "app_base_url": "https://pairqa.example"
            }
        ],
        "syncs": [
            "J1"
        ],
        "core_ids": [
            [
                "c-0"
            ],
            [
                "c-2"
            ],
            [
                "c-4",
                "c-5"
            ]
        ]
    },
    "abort": {
        "chunks": [
            "data: {\"type\": \"start\", \"total_candidates\": 6, \"total_batches\": 3, \"batch_size\": 2, \"run_id\": \"RUN-FIXED\"}\n\n",
            "data: {\"type\": \"batch\", \"index\": 0, \"status\": \"completed\", \"sent\": 2, \"no_interview\": 0, \"already_sent\": 0, \"excluded\": 0, \"employer_unverified\": 0, \"bulk_id\": \"bx\"}\n\n",
            "data: {\"type\": \"error\", \"status_code\": 409, \"message\": \"Job activity has been stopped.\", \"index\": 1, \"candidate_ids\": [\"a-2\", \"a-3\", \"a-4\", \"a-5\"]}\n\n",
            "data: {\"type\": \"done\", \"aborted\": true, \"totals\": {\"sent\": 2, \"already_sent\": 0, \"failed_batches\": 1, \"no_interview\": 0, \"excluded\": 0, \"employer_unverified\": 0}, \"skipped_already_sent\": [], \"excluded_candidates\": [], \"employer_unverified\": [], \"failed_candidate_ids\": [\"a-2\", \"a-3\", \"a-4\", \"a-5\"]}\n\n"
        ],
        "emails": [
            {
                "job_id": "J1",
                "candidate_count": 2,
                "send_job_posting": false,
                "app_base_url": ""
            }
        ],
        "syncs": [],
        "core_ids": [
            [
                "a-0",
                "a-1"
            ],
            [
                "a-2",
                "a-3"
            ]
        ]
    }
}''')


def _install(monkeypatch) -> Dict[str, Any]:
    """The faked pipeline the golden capture ran against."""
    rec: Dict[str, Any] = {"core_calls": [], "spawned": [], "emails": [], "syncs": []}
    monkeypatch.setattr(eng, "_LAUNCH_BATCH_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(eng, "_trace_new_run", lambda job_id: "RUN-FIXED")
    monkeypatch.setattr(cfg, "LAUNCH_GATE_ONCE", False)
    monkeypatch.setattr(cfg, "LAUNCH_BATCH_PARALLELISM", 1)
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", False)
    contactless = {"c-3", "c-6", "c-7"}
    dnc = {"c-1"}

    async def fake_generate(req):
        resumes = []
        for c in req.candidate_ids:
            if c in dnc:
                continue
            if c in contactless:
                resumes.append({"source_candidate_id": c, "name": f"N {c}", "email": "", "phone": ""})
            else:
                resumes.append({"source_candidate_id": c, "name": f"N {c}", "email": f"{c}@x.com", "phone": "5551234567"})
        return {"payload": json.dumps({"jd": {"job_id": req.job_id}, "resumes": resumes}),
                "dnc_blocked_ids": [c for c in req.candidate_ids if c in dnc]}

    async def fake_core(req, precomputed_signals=None, launch_id=None, batch_idx=None, **kw):
        ids = list(req.real_candidate_ids)
        rec["core_calls"].append({"ids": ids, "kw": dict(kw), "launch_id": launch_id, "batch_idx": batch_idx})
        if ids == ["c-0"]:
            return {"success": True, "bulk_id": "b0",
                    "data": [{"candidate_id": "c-0", "interview_id": "i-0"}],
                    "skipped_already_sent": [], "excluded_candidates": [],
                    "employer_unverified": [{"candidate_id": "c-0", "name": "N c-0", "employer_verification": "unverified"}]}
        if ids == ["c-2"]:
            return {"success": False, "message": "pairbot down", "data": []}
        if ids == ["c-4", "c-5"]:
            return {"success": True, "bulk_id": "b2",
                    "data": [{"candidate_id": "c-4", "interview_id": "i-4"}, {"candidate_id": "c-5", "interview_id": ""}],
                    "skipped_already_sent": ["c-9"],
                    "excluded_candidates": [{"candidate_id": "c-8", "name": "", "reason": "client employee"}],
                    "employer_unverified": []}
        if ids == ["a-2", "a-3"]:
            raise eng.HTTPException(status_code=409, detail="Job activity has been stopped.")
        return {"success": True, "bulk_id": "bx", "data": [{"candidate_id": c, "interview_id": f"i-{c}"} for c in ids],
                "skipped_already_sent": [], "excluded_candidates": [], "employer_unverified": []}

    def fake_spawn(coro, name=None):
        rec["spawned"].append(name)
        coro.close()

    def fake_email(**kw):
        rec["emails"].append(kw)

        async def _noop():
            return None
        return _noop()

    class _Sync:
        def synchronize_job_applicants(self, job_id):
            rec["syncs"].append(job_id)

            async def _noop():
                return None
            return _noop()

    monkeypatch.setattr(eng, "_generate_payload_for", fake_generate)
    monkeypatch.setattr(eng, "_send_bulk_interview_core", fake_core)
    monkeypatch.setattr(eng, "_spawn_task", fake_spawn)
    monkeypatch.setattr(eng, "_send_pair_launch_email", fake_email)
    monkeypatch.setattr(eng, "auto_assign_service", _Sync())
    return rec


SCENARIOS = {
    "mixed": dict(job_id="J1", candidate_ids=[f"c-{i}" for i in range(8)], batch_size=2,
                  is_initial_launch=True, notify_recruiters=True, send_job_posting_email=True,
                  app_base_url="https://pairqa.example"),
    "abort": dict(job_id="J1", candidate_ids=["a-0", "a-1", "a-2", "a-3", "a-4", "a-5"], batch_size=2,
                  notify_recruiters=True, app_base_url=""),
}


async def _raw(resp) -> List[str]:
    out = []
    async for chunk in resp.body_iterator:
        out.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return out


def _events(request, **kw) -> List[Dict[str, Any]]:
    async def go():
        async with contextlib.aclosing(eng.run_launch(request, **kw)) as gen:
            return [e async for e in gen]
    return asyncio.run(go())


# ----------------------------------------------------------- wire format ----

@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_endpoint_wire_output_is_byte_identical_to_before_the_extraction(monkeypatch, name):
    rec = _install(monkeypatch)
    resp = asyncio.run(eng.launch_bulk_interviews(eng.LaunchRequest(**SCENARIOS[name])))
    chunks = asyncio.run(_raw(resp))
    want = GOLDEN[name]
    assert chunks == want["chunks"]
    assert rec["emails"] == want["emails"]
    assert rec["syncs"] == want["syncs"]
    assert [c["ids"] for c in rec["core_calls"]] == want["core_ids"]
    # The legacy call shape: no session kwargs reach the core.
    assert all(c["kw"] == {} for c in rec["core_calls"])
    assert all("_internal" not in ch for ch in chunks)


def test_async_path_records_wire_events_without_internal(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(eng, "_spawn_task", core_tasks.spawn)  # the launch itself must run
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", True)
    writes: List[tuple] = []

    def fake_db(sql, params, fetch=False):
        writes.append((sql, params))
        return [] if fetch else None
    monkeypatch.setattr(eng, "_launch_db", fake_db)

    async def go():
        await eng.launch_bulk_interviews(eng.LaunchRequest(**SCENARIOS["mixed"], async_launch=True))
        await core_tasks.drain(timeout=5)
    asyncio.run(go())
    events = [json.loads(p[2]) for s, p in writes if "INSERT INTO launch_events" in s]
    wire = [json.loads(c[len("data: "):]) for c in GOLDEN["mixed"]["chunks"]]
    # Same events as the SSE stream (+ the async-only launch_id on start).
    assert [{k: v for k, v in e.items() if k != "launch_id"} for e in events] == wire
    assert not any("_internal" in e for e in events)


# ---------------------------------------------------------- run_launch ----

def test_run_launch_events_carry_internal_per_candidate_outcome(monkeypatch):
    _install(monkeypatch)
    events = _events(eng.LaunchRequest(**SCENARIOS["mixed"]))
    assert events[0]["_internal"] == {"batches": [["c-0", "c-1"], ["c-2", "c-3"], ["c-4", "c-5"], ["c-6", "c-7"]]}
    by_idx = {e["index"]: e["_internal"] for e in events if e["type"] == "batch"}
    assert by_idx[0] == {
        "status": "completed", "candidate_ids": ["c-0"], "sent_ids": ["c-0"], "no_interview_ids": [],
        # c-1 is on the Do-Not-Contact list: dropped before the send and reported
        # (internal channel only) so a launch session doesn't re-plan it forever.
        "failed_ids": [], "skipped_ids": [], "excluded_ids": ["c-1"], "bulk_id": "b0",
    }
    assert by_idx[1]["status"] == "failed" and by_idx[1]["failed_ids"] == ["c-2"]
    assert by_idx[1]["excluded_ids"] == ["c-3"]  # dropped pre-send by the contact gate
    assert by_idx[2]["sent_ids"] == ["c-4"] and by_idx[2]["no_interview_ids"] == ["c-5"]
    assert by_idx[2]["skipped_ids"] == ["c-9"] and by_idx[2]["excluded_ids"] == ["c-8"]
    assert by_idx[3]["candidate_ids"] == [] and by_idx[3]["excluded_ids"] == ["c-6", "c-7"]
    done = events[-1]["_internal"]
    assert done["sent_ids"] == ["c-0", "c-4"] and done["no_interview_ids"] == ["c-5"]
    assert done["failed_ids"] == ["c-2"] and done["not_run_ids"] == [] and done["stopped"] is False
    assert "c-1" in done["excluded_ids"]
    # Stripping `_internal` gives exactly the wire events.
    wire = [json.loads(c[len("data: "):]) for c in GOLDEN["mixed"]["chunks"]]
    assert [eng._strip_internal(e) for e in events] == wire


def test_should_stop_leaves_unstarted_batches_resumable(monkeypatch):
    rec = _install(monkeypatch)
    started = {"n": 0}
    real_core = eng._send_bulk_interview_core

    async def counting_core(req, *a, **k):
        started["n"] += 1
        return await real_core(req, *a, **k)
    monkeypatch.setattr(eng, "_send_bulk_interview_core", counting_core)

    events = _events(
        eng.LaunchRequest(job_id="J1", candidate_ids=["z-0", "z-1", "z-2", "z-3", "z-4"], batch_size=2),
        should_stop=lambda: started["n"] >= 1,
    )
    assert [c["ids"] for c in rec["core_calls"]] == [["z-0", "z-1"]]
    done = events[-1]
    assert done["aborted"] is False and done["totals"]["failed_batches"] == 0
    assert done["failed_candidate_ids"] == []
    assert done["_internal"]["stopped"] is True
    assert done["_internal"]["not_run_ids"] == ["z-2", "z-3", "z-4"]
    assert not any(e["type"] == "error" for e in events)


def test_should_stop_that_raises_stops_fail_safe(monkeypatch):
    rec = _install(monkeypatch)

    def boom():
        raise RuntimeError("lease check failed")
    events = _events(eng.LaunchRequest(job_id="J1", candidate_ids=["z-0"]), should_stop=boom)
    assert rec["core_calls"] == []
    assert events[-1]["_internal"]["not_run_ids"] == ["z-0"]


def test_fire_side_effects_false_fires_nothing(monkeypatch):
    rec = _install(monkeypatch)
    _events(eng.LaunchRequest(**SCENARIOS["mixed"]), fire_side_effects=False)
    assert rec["emails"] == [] and rec["syncs"] == []


def test_side_effects_guard_is_consulted_once_and_can_veto(monkeypatch):
    rec = _install(monkeypatch)
    calls: List[int] = []
    _events(eng.LaunchRequest(**SCENARIOS["mixed"]), side_effects_guard=lambda: calls.append(1) or False)
    assert calls == [1] and rec["emails"] == [] and rec["syncs"] == []

    rec = _install(monkeypatch)
    _events(eng.LaunchRequest(**SCENARIOS["mixed"]), side_effects_guard=lambda: True)
    assert rec["emails"] == GOLDEN["mixed"]["emails"] and rec["syncs"] == ["J1"]

    rec = _install(monkeypatch)

    def broken():
        raise RuntimeError("db down")
    _events(eng.LaunchRequest(**SCENARIOS["mixed"]), side_effects_guard=broken)
    assert rec["emails"] == [] and rec["syncs"] == []  # at most once: unsure → skip


def test_guard_not_consulted_when_nothing_was_sent(monkeypatch):
    rec = _install(monkeypatch)
    calls: List[int] = []
    _events(eng.LaunchRequest(job_id="J1", candidate_ids=["c-2"], notify_recruiters=True),
            side_effects_guard=lambda: calls.append(1) or True)
    assert calls == [] and rec["emails"] == []


def test_session_hooks_reach_the_core_per_batch(monkeypatch):
    rec = _install(monkeypatch)

    def bulk_id_for(idx, ids):
        return f"bulk-{idx}"
    _events(
        eng.LaunchRequest(job_id="J1", candidate_ids=["z-0", "z-1", "z-2"], batch_size=2),
        launch_id="L-1", run_id="SESSION-RUN", claimed_session_id="S-1",
        provision_inline=False, bulk_id_for=bulk_id_for,
    )
    assert [c["batch_idx"] for c in rec["core_calls"]] == [0, 1]
    for c in rec["core_calls"]:
        assert c["launch_id"] == "L-1"
        assert c["kw"] == {"claimed_session_id": "S-1", "provision_inline": False, "bulk_id_for": bulk_id_for}


def test_bulk_id_for_without_launch_id_still_passes_the_batch_index(monkeypatch):
    rec = _install(monkeypatch)
    _events(eng.LaunchRequest(job_id="J1", candidate_ids=["z-0"]), bulk_id_for=lambda i, ids: "x")
    assert rec["core_calls"][0]["batch_idx"] == 0 and rec["core_calls"][0]["launch_id"] is None


def test_given_run_id_is_adopted_not_replaced(monkeypatch):
    _install(monkeypatch)
    adopted: List[tuple] = []
    monkeypatch.setattr(eng, "_adopt_trace_run", lambda rid, job: adopted.append((rid, job)))
    monkeypatch.setattr(eng, "_trace_new_run", lambda job_id: pytest.fail("minted a new run id"))
    events = _events(eng.LaunchRequest(job_id="J1", candidate_ids=["z-0"]), run_id="S-RUN")
    assert events[0]["run_id"] == "S-RUN" and adopted == [("S-RUN", "J1")]


def test_adopt_trace_run_is_fail_open(monkeypatch):
    import core.pipeline_trace as pt

    def boom(*_a, **_k):
        raise RuntimeError("x")
    monkeypatch.setattr(pt, "adopt_run", boom, raising=False)
    eng._adopt_trace_run("r", "J")  # must not raise


def test_run_launch_cancellation_runs_cleanup_and_propagates(monkeypatch):
    _install(monkeypatch)
    cleared: List[str] = []

    async def hang(*_a, **_k):
        await asyncio.sleep(60)
    monkeypatch.setattr(eng, "_run_one_batch", hang)
    import services.jobdiva_activity as ja

    async def clear(job_id):
        cleared.append(job_id)
    monkeypatch.setattr(ja, "clear_job_active", clear)

    async def go():
        seen = []

        async def consume():
            async with contextlib.aclosing(eng.run_launch(eng.LaunchRequest(job_id="J1", candidate_ids=["z-0"]))) as gen:
                async for e in gen:
                    seen.append(e["type"])
        t = asyncio.ensure_future(consume())
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return seen
    assert asyncio.run(go()) == ["start"]
    assert cleared == ["J1"]


# ----------------------------------------------------- side effects fn ----

def test_fire_launch_side_effects_matches_the_endpoint_rules(monkeypatch):
    rec = _install(monkeypatch)
    assert eng.fire_launch_side_effects("J", sent_count=0, notify_recruiters=True, send_job_posting_email=True,
                                        is_initial_launch=True, app_base_url="") is False
    assert rec["emails"] == [] and rec["syncs"] == []
    assert eng.fire_launch_side_effects("J", sent_count=3, notify_recruiters=False, send_job_posting_email=True,
                                        is_initial_launch=False, app_base_url="") is False
    assert rec["emails"] == []
    assert eng.fire_launch_side_effects("J", sent_count=3, notify_recruiters=True, send_job_posting_email=False,
                                        is_initial_launch=False, app_base_url="u") is True
    assert rec["emails"] == [{"job_id": "J", "candidate_count": 3, "send_job_posting": False, "app_base_url": "u"}]
    assert rec["syncs"] == []
    assert eng.fire_launch_side_effects("J2", sent_count=1, notify_recruiters=False, send_job_posting_email=True,
                                        is_initial_launch=True, app_base_url="") is True
    assert rec["syncs"] == ["J2"] and rec["emails"][-1]["send_job_posting"] is True


def test_fire_launch_side_effects_reports_a_spawn_failure(monkeypatch):
    _install(monkeypatch)

    def broken_spawn(coro, name=None):
        coro.close()
        raise RuntimeError("no loop")
    monkeypatch.setattr(eng, "_spawn_task", broken_spawn)
    assert eng.fire_launch_side_effects("J", sent_count=1, notify_recruiters=True, send_job_posting_email=False,
                                        is_initial_launch=False, app_base_url="") is False


# -------------------------------------------- launch_runs / session link ----

class _RunsDB:
    def __init__(self, fail_session_column=False):
        self.calls: List[tuple] = []
        self.fail_session_column = fail_session_column

    def __call__(self, sql, params, fetch=False):
        self.calls.append((sql, params))
        if "launch_session_id" in sql and "INSERT INTO launch_runs" in sql and self.fail_session_column:
            raise RuntimeError('column "launch_session_id" does not exist')
        return [] if fetch else None


def _async_launch(monkeypatch, db, **kw):
    _install(monkeypatch)
    monkeypatch.setattr(cfg, "LAUNCH_ASYNC", True)
    monkeypatch.setattr(eng, "_launch_db", db)

    async def go():
        resp = await eng.launch_bulk_interviews(eng.LaunchRequest(
            job_id="J1", candidate_ids=["z-0"], async_launch=True, **kw))
        await core_tasks.drain(timeout=5)
        return resp
    return asyncio.run(go())


def _run_inserts(db):
    return [(s, p) for s, p in db.calls if "INSERT INTO launch_runs" in s]


def test_launch_session_id_is_stored_on_the_async_run(monkeypatch):
    db = _RunsDB()
    sid = "6f1c2b9e-1d1a-4c55-9f43-2b1f4a0f8e11"
    resp = _async_launch(monkeypatch, db, launch_session_id=sid.upper())
    assert resp.status_code == 202
    (sql, params), = _run_inserts(db)
    assert "launch_session_id" in sql and params[3] == sid and "'running'" in sql


def test_malformed_or_absent_launch_session_id_keeps_the_old_insert(monkeypatch):
    for kw in ({}, {"launch_session_id": "not-a-uuid"}):
        db = _RunsDB()
        _async_launch(monkeypatch, db, **kw)
        (sql, params), = _run_inserts(db)
        assert "launch_session_id" not in sql and len(params) == 3


def test_missing_session_column_falls_back_to_the_old_insert(monkeypatch):
    db = _RunsDB(fail_session_column=True)
    resp = _async_launch(monkeypatch, db, launch_session_id="6f1c2b9e-1d1a-4c55-9f43-2b1f4a0f8e11")
    assert resp.status_code == 202
    assert [len(p) for _s, p in _run_inserts(db)] == [4, 3]


def test_launch_session_id_is_optional_in_the_request_model():
    assert eng.LaunchRequest(job_id="J", candidate_ids=[]).launch_session_id is None


def test_ddl_adds_the_session_column():
    assert any("ADD COLUMN IF NOT EXISTS launch_session_id uuid" in s for s in eng._LAUNCH_DDL)


def test_register_session_launch_run_uses_the_session_status(monkeypatch):
    db = _RunsDB()
    monkeypatch.setattr(eng, "_launch_db", db)
    eng.register_session_launch_run("L1", "J1", requested_by=" Me@X.com ",
                                    launch_session_id="6f1c2b9e-1d1a-4c55-9f43-2b1f4a0f8e11")
    (sql, params), = db.calls
    assert "ON CONFLICT (id) DO NOTHING" in sql
    assert params == ("L1", "J1", "me@x.com", "session", "6f1c2b9e-1d1a-4c55-9f43-2b1f4a0f8e11")


def test_sweep_and_finalize_only_ever_touch_running_runs(monkeypatch):
    db = _RunsDB()
    monkeypatch.setattr(eng, "_launch_db", db)
    eng._sweep_stale_launch_runs()
    eng._launch_finalize("L1", "completed")
    for sql, _p in db.calls:
        assert "status = 'running'" in sql
    assert eng.LAUNCH_RUN_SESSION_STATUS == "session"


# ---------------------------------------------------------- New Relic ----

def test_streaming_endpoints_suppress_apdex(monkeypatch):
    import core.newrelic as nr
    calls: List[str] = []
    monkeypatch.setattr(nr, "suppress_apdex", lambda: calls.append("x"), raising=False)
    _install(monkeypatch)
    asyncio.run(_raw(asyncio.run(eng.launch_bulk_interviews(eng.LaunchRequest(job_id="J1", candidate_ids=[])))))
    assert calls == ["x"]
    resp = asyncio.run(eng.stream_engagement_status(request=object(), bulk_id="b-1"))
    assert resp.media_type == "text/event-stream" and calls == ["x", "x"]


def test_suppress_apdex_absent_or_broken_is_harmless(monkeypatch):
    import core.newrelic as nr
    monkeypatch.delattr(nr, "suppress_apdex", raising=False)
    eng._nr_suppress_apdex()

    def boom():
        raise RuntimeError("agent")
    monkeypatch.setattr(nr, "suppress_apdex", boom, raising=False)
    eng._nr_suppress_apdex()


def test_generate_payload_event_carries_no_payload(monkeypatch):
    import core.newrelic as nr
    events: List[tuple] = []
    monkeypatch.setattr(nr, "is_enabled", lambda: True)
    monkeypatch.setattr(nr, "record_custom_event", lambda t, d: events.append((t, d)))
    monkeypatch.setattr(nr, "record_message", lambda *a, **k: None)
    payload = json.dumps({"resumes": [{"name": "Jane Doe", "email": "jane@x.com", "phone": "4155550100"}]})
    eng._log_generate_payload_response_to_newrelic(
        {"success": True, "payload": payload, "candidate_count": 1, "dnc_blocked_count": 0,
         "dnc_blocked_ids": ["d1"]})
    eng._log_generate_payload_response_to_newrelic(
        {"success": False, "error": "duplicate key jane@x.com", "error_type": "UniqueViolation",
         "candidate_ids": ["c1", "c2"], "job_id": "J1"}, level="error")
    (t1, ok), (t2, err) = events
    assert t1 == t2 == "EngageGeneratePayload"
    assert "response" not in ok and "response" not in err
    blob = json.dumps(events)
    assert "Jane" not in blob and "jane@x.com" not in blob and "4155550100" not in blob
    assert ok["candidate_count"] == 1 and ok["success"] is True and ok["response_size_bytes"] > 0
    assert err == {**err, "success": False, "requested_count": 2, "job_id": "J1", "error_type": "UniqueViolation"}
