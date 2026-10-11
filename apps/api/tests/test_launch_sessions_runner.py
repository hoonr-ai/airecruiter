"""Launch-session runner: claims, leases, fencing, shutdown, cancel, review,
side effects — and a crash after EVERY unit of a full launch.

MemoryStore (the store contract twin, see test_launch_sessions_store.py) and
FakeWorld (D1/D2 contracts: session + send claims, deterministic bulk ids,
by-job reconcile, a fake Pairbot) stand in for Postgres and the network.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from core import tasks as core_tasks
from services.launch_sessions import schema as S
from services.launch_sessions.context import Lease
from services.launch_sessions.errors import OwnershipLost
from services.launch_sessions.runner import LaunchSessionRunner, backoff_s
from services.launch_sessions.stages import launching
from services.launch_sessions.store import Fence
from tests.launch_sessions_fakes import (
    CrashPlan,
    FakeWorld,
    MemoryStore,
    add_job,
    cand,
    candidate_event,
    new_session_fields,
)
from tests.launch_sessions_harness import TEST_ENV, Runner, drive, full_search_world, states


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    core_tasks.stopping.clear()
    yield
    core_tasks.stopping.clear()


def run(coro):
    return asyncio.run(coro)


EXPECTED = {"c1": "launched", "c2": "launched", "c3": "skipped", "c4": "launched", "c5": "needs_contact"}


# ── the happy path ───────────────────────────────────────────────────────────

def test_full_search_session_runs_every_stage_to_completed():
    """Also pins: cross submissions scheduled exactly once (first attempt)."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    s = run(drive(store, world, sid))

    assert s["status"] == S.STATUS_COMPLETED and s["stage"] == S.STAGE_DONE
    assert states(store, sid) == EXPECTED
    assert store.cands[sid]["c3"]["reason"] == "below_floor"
    # c2's looked-up phone REPLACED its (empty) contact in the save payload.
    assert world.saved_rows["c2"]["phone"] == "+15125550123"
    # One POST per person, under the deterministic session bulk ids.
    assert world.pairbot.post_count == {"c1": 1, "c2": 1, "c4": 1}
    bulk_ids = {b for b, _ in world.pairbot.posts}
    assert bulk_ids <= set((s["checkpoint"]["s7"]["inflight"]).keys())
    assert all(b == launching.bulk_id_for(sid, 1, rec["batch"]) for b, rec in s["checkpoint"]["s7"]["inflight"].items())
    # run_launch never fires side effects or provisions inline for a session.
    assert world.launch_calls and all(not c["fire_side_effects"] and not c["provision_inline"] for c in world.launch_calls)
    # S7b provisioned the launched rows; S8 fired the side effects ONCE.
    assert sorted(sum(world.provision_calls, [])) == ["c1", "c2", "c4"]
    assert len(world.side_effects) == 1
    fx = world.side_effects[0]
    assert fx["sent_count"] == 3 and fx["is_initial_launch"] and fx["notify_recruiters"]
    assert fx["send_job_posting_email"] is True  # clean: 0 failed, 0 unconfirmed
    assert s["side_effects_fired_at"] is not None
    # Timings: stage stamps, first interview, the NR timing event, T2 stamp.
    for col in ("sourcing_started_at", "sourcing_done_at", "selection_done_at", "enrich_done_at",
                "save_started_at", "save_done_at", "launch_started_at", "first_interview_created_at",
                "last_batch_done_at", "completed_at"):
        assert s[col] is not None, col
    assert world.timing_events == [{"session_id": sid, "status": "completed"}]
    assert world.first_launch_stamps
    # Leftover session claims released (none left 'queued' for this session).
    assert not [r for r in store.sourced.values() if (r.get("data") or {}).get("engage_status") == "queued"]
    assert s["counts"]["sent"] == 3 and s["counts"]["needs_contact"] == 1
    # review-parity #7: S6's total survives; rows_in_saved is what is left.
    assert s["counts"]["saved"] == s["counts"]["saved_total"] == 3 and s["counts"]["rows_in_saved"] == 0
    assert s["counts"]["failed_batches"] == 0 and s["counts"]["no_interview"] == 0
    assert world.search_calls == [("A0", True), ("A1", False)]


# ── a crash after every unit ────────────────────────────────────────────────

def _clean_run_points() -> List[str]:
    """Every instrumented unit of a clean run, in order (computed at
    collection time, under the same knobs the tests run with)."""
    from unittest.mock import patch
    import os

    with patch.dict(os.environ, TEST_ENV):
        plan = CrashPlan()
        store, world, job, fields = full_search_world(plan)
        store.create_session(fields)
        plan.n, plan.log = 0, []
        run(drive(store, world, fields["id"]))
    return list(plan.log)


_POINTS = _clean_run_points()


@pytest.mark.parametrize("at", range(1, len(_POINTS) + 1), ids=lambda i: f"{i}-{_POINTS[i - 1]}")
def test_a_crash_after_any_unit_resumes_to_the_same_outcome(at):
    plan = CrashPlan()
    store, world, job, fields = full_search_world(plan)
    store.create_session(fields)
    plan.n, plan.log, plan.at = 0, [], at
    sid = fields["id"]
    s = run(drive(store, world, sid))

    assert plan.fired == _POINTS[at - 1]
    assert s["status"] == S.STATUS_COMPLETED, (s["status"], s["error"])
    assert states(store, sid) == EXPECTED
    # Never posted twice: a crash around a POST is settled by Pairbot's record.
    assert all(n == 1 for n in world.pairbot.post_count.values()), world.pairbot.post_count
    assert set(world.pairbot.interviews) == {"c1", "c2", "c4"}
    # Side effects at most once (exactly once unless the crash fell between
    # claiming the guard and spawning — then never: at-most-once delivery).
    assert len(world.side_effects) <= 1
    if plan.fired != "store.claim_side_effects":
        assert len(world.side_effects) == 1 and world.side_effects[0]["sent_count"] == 3
    # A paid lookup is repeated only when its answer was lost with the crash.
    assert world.enrich_calls.count("c2") <= 2
    # Cross submissions: at most once per session (the flag is written before
    # the first stream starts, so a crash right there skips it — never twice).
    assert sum(1 for _, cross in world.search_calls if cross) <= 1


# ── claims, leases and fencing ──────────────────────────────────────────────

def test_one_worker_claims_a_session_and_the_global_cap_holds():
    store = MemoryStore()
    world = FakeWorld(store)
    sids = []
    for i in range(5):
        job = add_job(store, job_id=f"{2000 + i}", ref=f"26-{i:03d}")
        f = new_session_fields(job)
        store.create_session(f)
        sids.append(f["id"])
    runners = [Runner(store, world) for _ in range(6)]

    async def claim_all():
        return [await r._claim(5) for r in runners]

    claims = run(claim_all())
    taken = [c.session_id for cs in claims for c in cs]
    assert len(taken) == 3 == len(set(taken))  # PAIR_LAUNCH_GLOBAL_CONCURRENCY=3
    assert sum(1 for s in store.sessions.values() if s["status"] == "running") == 3


def test_an_expired_lease_is_reclaimed_and_the_old_owner_is_fenced_out():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    a = Runner(store, world, owner="A")
    taken: Dict[str, Any] = {}
    original = world.search_events

    async def hijacking(request, *, schedule_cross_submissions):
        async for ev in original(request, schedule_cross_submissions=schedule_cross_submissions):
            if not taken:
                # A stalls; its lease runs out and B claims the session.
                store.expire_lease(sid)
                taken["claims"] = store.claim("B", 1, global_cap=3, lease_s=180)
            yield ev

    world.search_events = hijacking

    async def go():
        await a.tick()
        await a.settle_tasks()

    run(go())
    s = store.sessions[sid]
    assert s["owner"] == "B" and s["claims"] == 2
    # A wrote nothing after losing it: no s1 flush by A survived as owner A,
    # and A did not release / fail the session.
    assert s["status"] == "running" and s["attempt"] == 0
    with pytest.raises(OwnershipLost):
        store.update_session(Fence(sid, "A", 1), values={"stage": "done"})
    # B (the new owner) finishes the launch.
    world.search_events = original
    s = run(drive(store, world, sid))
    assert s["status"] == "completed" and states(store, sid) == EXPECTED


def test_heartbeat_detects_ownership_loss_and_a_failing_heartbeat_self_fences(monkeypatch):
    store = MemoryStore()
    job = add_job(store)
    f = new_session_fields(job)
    store.create_session(f)
    [claim] = store.claim("A", 1, global_cap=3, lease_s=180)
    lease = Lease(store, Fence(f["id"], "A", claim.claims))
    monkeypatch.setenv("PAIR_LAUNCH_HEARTBEAT_S", "1")

    async def db(fn, *a, **k):
        return fn(*a, **k)

    store.expire_lease(f["id"])
    store.claim("B", 1, global_cap=3, lease_s=180)

    async def beat():
        await asyncio.wait_for(lease.heartbeat_loop(db), timeout=5)

    run(beat())  # one 1 s heartbeat interval
    assert lease.lost and lease.stop_reason() == "ownership_lost"

    fresh = Lease(store, Fence(f["id"], "B", 2))
    fresh.last_ok -= 10_000  # no successful renewal for longer than the lease
    assert fresh.stop_reason() == "lease_expired"


def test_shutdown_releases_the_lease_without_failing_the_session():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    original = world.search_events

    async def stopping_mid_stream(request, *, schedule_cross_submissions):
        async for ev in original(request, schedule_cross_submissions=schedule_cross_submissions):
            core_tasks.mark_stopping()
            yield ev

    world.search_events = stopping_mid_stream
    r = Runner(store, world, owner="A")

    async def go():
        await r.tick()
        assert not await r.settle_tasks()

    run(go())
    s = store.sessions[sid]
    # Released: owner cleared (a late heartbeat of A can no longer re-lease it).
    assert s["status"] == "running" and s["owner"] is None and s["claims"] == 1
    assert s["lease_until"] <= store.t and s["attempt"] == 0 and s["not_before"] is None
    # A stopping worker claims nothing new.
    assert run(Runner(store, world).tick()) == 0
    core_tasks.stopping.clear()
    world.search_events = original
    assert run(drive(store, world, sid))["status"] == "completed"


def test_cancelled_task_releases_synchronously():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]

    async def go():
        started = asyncio.Event()
        blocker = asyncio.Event()
        original = world.search_events

        async def hang(request, *, schedule_cross_submissions):
            async for ev in original(request, schedule_cross_submissions=schedule_cross_submissions):
                started.set()
                await blocker.wait()
                yield ev

        world.search_events = hang
        r = Runner(store, world, owner="A")
        await r.tick()
        await started.wait()
        task = r.spawned[0]
        task.cancel()  # the shutdown drain's cancel
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    s = store.sessions[sid]
    assert s["status"] == "running" and s["lease_until"] <= store.t  # adoptable at once


def test_stage_errors_back_off_then_fail_after_max_attempts(monkeypatch):
    monkeypatch.setenv("PAIR_LAUNCH_MAX_ATTEMPTS", "3")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    world.search_raise_after = {"A0": 0}

    async def one_round():
        r = Runner(store, world)
        await r.tick()
        assert not await r.settle_tasks()

    run(one_round())
    s = store.sessions[sid]
    assert s["attempt"] == 1 and s["status"] == "running"
    assert s["not_before"] == store.t + dt.timedelta(seconds=backoff_s(1))
    assert "search A0 died" in s["error"]
    # Not claimable before not_before.
    assert run(Runner(store, world).tick()) == 0
    for _ in range(2):
        world.search_raise_after = {"A0": 0}
        store.advance(1000)
        run(one_round())
    s = store.sessions[sid]
    assert s["status"] == "failed" and s["attempt"] == 2  # third failure → failed
    assert "sourcing failed" in s["error"]
    assert world.side_effects == []


def test_access_denied_is_retried_then_fails_access_revoked(monkeypatch):
    monkeypatch.setenv("PAIR_LAUNCH_ACCESS_DENIED_ATTEMPTS", "2")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.save_script = [HTTPException(status_code=403, detail="no")] * 5
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "failed" and s["error"] == "access revoked"
    assert s["attempt"] == 1


def test_outreach_stopped_on_save_cancels_the_session():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.save_script = [HTTPException(status_code=409, detail="Job activity has been stopped.")]
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "cancelled" and s["error"] == "outreach stopped"
    assert world.pairbot.posts == [] and world.side_effects == []


def test_outreach_stopped_by_pairbot_mid_launch_cancels_with_recruiter_email_only(monkeypatch):
    monkeypatch.setenv("PAIR_LAUNCH_BATCH_SIZE", "1")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.outreach_stop_at_batch = 1
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "cancelled" and s["error"] == "outreach stopped"
    assert len(world.pairbot.posts) == 1
    # What /engage/launch fires on a 409 abort: recruiter email + applicant
    # sync (initial launch), never the job posting.
    assert [e["notify_recruiters"] for e in world.side_effects] == [True]
    assert world.side_effects[0]["is_initial_launch"] is True
    assert world.side_effects[0]["send_job_posting_email"] is False
    # The launched person still gets their JobDiva application.
    assert sum(world.provision_calls, []) == ["c1"]
    # The two never-posted claims went back to their pre-claim status.
    queued = [r for r in store.sourced.values() if (r.get("data") or {}).get("engage_status") == "queued"]
    assert queued == []


# ── cancel ───────────────────────────────────────────────────────────────────

async def _rounds(store, world, n):
    for _ in range(n):
        r = Runner(store, world)
        await r.tick()
        errors = await r.settle_tasks()
        assert not errors, errors
        store.advance(1000)


def test_waiting_for_pairbot_does_not_count_attempts_and_cancel_finalizes_with_a_reconcile_pass():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    world.pairbot_mode = {"c1": "processing", "c2": "processing", "c4": "processing"}
    run(_rounds(store, world, 12))
    s = store.sessions[sid]
    assert s["status"] == "running" and s["stage"] == S.STAGE_LAUNCHING
    assert s["attempt"] == 0  # RetryLater waits never count as failures
    assert {c for c, st in states(store, sid).items() if st == S.ROW_LAUNCHING} == {"c1", "c2", "c4"}
    assert world.reconcile_calls and all(not call.get("missing") for call in world.reconcile_calls)
    assert world.pairbot.post_count == {"c1": 1, "c2": 1, "c4": 1}

    # The recruiter cancels; Pairbot then finishes the bulk with c1 only.
    outcome, _ = store.cancel_session(sid, "cancelled by user")
    assert outcome == "requested"
    world.pairbot.creating.clear()
    world.pairbot.create(["c1"])
    s = run(drive(store, world, sid))
    assert s["status"] == "cancelled" and s["error"] == "cancelled by user"
    st = states(store, sid)
    assert st["c1"] == "launched" and st["c2"] == st["c4"] == "failed"
    assert store.cands[sid]["c2"]["reason"] == "not_created"
    # Never re-posted; the never-created rows are released as 'failed'.
    assert world.pairbot.post_count == {"c1": 1, "c2": 1, "c4": 1}
    data = {cid: r["data"] for (k, cid), r in store.sourced.items()}
    assert data["c2"]["engage_status"] == "failed" and "engage_session_id" not in data["c2"]
    # Recruiter email only (1 launched).
    assert len(world.side_effects) == 1
    assert world.side_effects[0]["sent_count"] == 1 and world.side_effects[0]["send_job_posting_email"] is False


def test_unsettled_rows_become_unconfirmed_after_settle_max(monkeypatch):
    monkeypatch.setenv("PAIR_LAUNCH_SETTLE_MAX_S", "0")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    world.pairbot.down = True  # by-job unreachable: never re-post
    world.pairbot_mode = {"c1": "processing", "c2": "processing", "c4": "processing"}
    s = run(drive(store, world, sid))
    assert s["status"] == "completed"
    st = states(store, sid)
    assert {c for c, v in st.items() if v == S.ROW_UNCONFIRMED} == {"c1", "c2", "c4"}
    assert world.pairbot.post_count == {"c1": 1, "c2": 1, "c4": 1}
    assert s["recheck_at"] is not None and s["counts"]["unconfirmed"] == 3
    assert world.side_effects == []  # nothing proven sent

    # Housekeeping re-checks: Pairbot is back and has c1; c2/c4 provably not.
    world.pairbot.down = False
    world.pairbot.creating.clear()
    world.pairbot.create(["c1"])
    store.set_column(sid, "recheck_at", store.t)
    r = LaunchSessionRunner(store=store, fx=world, owner="hk")
    assert run(r.recheck_unconfirmed()) == 1
    st = states(store, sid)
    assert st["c1"] == "launched" and st["c2"] == "failed" and st["c4"] == "failed"
    assert store.sessions[sid]["recheck_at"] is None


# ── QA review ────────────────────────────────────────────────────────────────

def test_review_hands_the_session_to_the_recruiter_and_confirmed_contacts_are_final():
    store, world, job, fields = full_search_world()
    fields["input"]["require_review"] = True
    store.create_session(fields)
    sid = fields["id"]
    s = run(drive(store, world, sid))
    assert s["status"] == S.STATUS_AWAITING_INPUT and s["stage"] == S.STAGE_REVIEW
    assert s["owner"] is None and s["lease_until"] is None and s["review_started_at"] is not None
    assert run(Runner(store, world).tick()) == 0  # nothing claims a session in review
    assert world.pairbot.posts == [] and world.save_calls == []

    # QA confirms test contacts: c1 phone only, c4 nothing (→ no fallback to
    # its real contact), c2 email only; c5 not confirmed (removed).
    store.advance(120)
    waited_ms = int((store.t - store.sessions[sid]["review_started_at"]).total_seconds() * 1000)
    row = store.confirm_review(sid, {
        "c1": {"phone": "+15550000001", "email": None},
        "c2": {"phone": None, "email": "qa2@test.dev"},
        "c4": {"phone": None, "email": None},
    }, {"confirmed": True, "count": 3})
    assert row["status"] == "queued" and row["human_wait_ms"] == waited_ms >= 120_000
    s = run(drive(store, world, sid))
    assert s["status"] == "completed"
    st = states(store, sid)
    assert st["c5"] == "skipped" and store.cands[sid]["c5"]["reason"] == "review_removed"
    assert st["c4"] == "needs_contact"  # its real contact was NOT used
    assert world.saved_rows["c1"]["phone"] == "+15550000001" and world.saved_rows["c1"]["email"] is None
    assert world.saved_rows["c2"]["email"] == "qa2@test.dev" and world.saved_rows["c2"]["phone"] is None
    assert set(world.pairbot.interviews) == {"c1", "c2"}


# ── side effects ─────────────────────────────────────────────────────────────

def test_a_pass_that_sends_nobody_never_uses_the_guard_and_a_later_child_sends_its_email():
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    rows = [cand("n1", 80, email="", phone=""), cand("n2", 80, email="", phone="")]
    world.search_script = {"A0": [candidate_event(r) for r in rows]}
    fields = new_session_fields(job, inp={"search_attempts": [{"label": "i", "payload": {"boolean_string": "A0"}}]})
    store.create_session(fields)
    sid = fields["id"]
    s = run(drive(store, world, sid))
    assert s["status"] == "completed" and s["counts"]["sent"] == 0
    assert s["side_effects_fired_at"] is None and world.side_effects == []
    assert s["counts"]["side_effects"] == "nothing_sent"

    # The needs-contact child (recruiter email only) launches both.
    from services.launch_sessions import control
    from services.launch_sessions.models import NeedsContactRequest
    import services.launch_sessions.runner as runner_mod

    r = Runner(store, world)
    runner_mod._runner = r
    try:
        status, out = run(control.needs_contact(
            store.get_session(sid),
            NeedsContactRequest(contacts={"n1": {"phone": "+15125550101"}, "n2": {"email": "n2@mail.test"}}),
            type("U", (), {"email": "r@x.com"})(),
        ))
    finally:
        runner_mod._runner = None
    assert status == 201
    child_id = out["session"]["id"]
    assert states(store, sid) == {"n1": "handed_off", "n2": "handed_off"}
    s = run(drive(store, world, child_id))
    assert s["status"] == "completed" and s["counts"]["sent"] == 2
    assert len(world.side_effects) == 1
    fx = world.side_effects[0]
    assert fx["notify_recruiters"] and not fx["is_initial_launch"] and not fx["send_job_posting_email"]


def test_a_failed_spawn_releases_the_side_effects_guard():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.side_effects_ok = False
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed" and s["counts"]["side_effects"] == "failed"
    assert s["side_effects_fired_at"] is None


def test_a_no_interview_answer_is_not_a_failed_batch_for_the_job_posting_email():
    """/engage/launch counts failed BATCHES: Pairbot creating no interview
    for one person (a completed batch) keeps the launch clean."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.pairbot_mode = {"c4": "reject"}
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed" and states(store, fields["id"])["c4"] == "failed"
    assert store.cands[fields["id"]]["c4"]["reason"] == "pairbot_rejected"
    assert world.side_effects[0]["send_job_posting_email"] is True
    assert world.side_effects[0]["sent_count"] == 2
    assert s["counts"]["no_interview"] == 1 and s["counts"]["failed_batches"] == 0


def test_a_failed_batch_suppresses_the_job_posting_email():
    """A batch with no Pairbot answer is a failed batch (even though by-job
    later proves the person launched): no job-posting email, as legacy."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.pairbot_mode = {"c4": "timeout"}
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed" and states(store, fields["id"]) == EXPECTED
    assert s["counts"]["failed_batches"] == 1
    assert world.side_effects[0]["send_job_posting_email"] is False
    assert world.pairbot.post_count["c4"] == 1


# ── New Relic / tracing ──────────────────────────────────────────────────────

def test_new_relic_transactions_events_and_trace_adoption(monkeypatch):
    import core.newrelic as nr
    import core.pipeline_trace as pt

    txns: List[str] = []
    events: List[Dict[str, Any]] = []
    ignored: List[int] = []
    adopted: List[Any] = []

    class FakeTxn:
        def __init__(self, name, *a, **k):
            txns.append(name)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(nr, "background_transaction", FakeTxn)
    monkeypatch.setattr(nr, "record_custom_event", lambda t, p=None: events.append({"type": t, **(p or {})}))
    monkeypatch.setattr(nr, "ignore_transaction", lambda: ignored.append(1))
    monkeypatch.setattr(pt, "adopt_run", lambda run_id, job_id=None: adopted.append((run_id, job_id)))

    store, world, job, fields = full_search_world()
    sid = fields["id"]
    assert run(Runner(store, world).tick()) == 0 and ignored == [1]  # an empty tick is dropped
    store.create_session(fields)
    run(drive(store, world, sid))

    stages = ["sourcing", "selecting", "enriching", "splitting", "saving", "launching", "provisioning", "done"]
    assert txns == [f"launch_session/{st}:{sid}" for st in stages]
    assert adopted and all(a == (sid, "1001") for a in adopted)
    sess = [e for e in events if e["type"] == "PairLaunchSession"]
    assert [e["stage"] for e in sess if e["status"] == "stage_done"][:3] == ["created", "sourcing", "selecting"]
    assert sess[-1]["status"] == "completed" and sess[-1]["session_id"] == sid
    for e in sess:
        assert not any(k in str(e).lower() for k in ("@mail.test", "person c", "+1512"))
        assert all(isinstance(v, (int, float, str)) for v in e.values())


# ── review fixes: hand-back, failed batches, finalize provisioning ──────────

def test_finishing_parks_posted_unconfirmed_rows_so_no_later_launch_posts_them_again(monkeypatch):
    """review-durability #1: rows posted with no answer (D1 puts them back to
    'queued') that end unconfirmed are parked 'processing', never released;
    a never-posted claim IS released; a new session posts nobody twice; the
    re-check later proves them launched and provisions them."""
    monkeypatch.setenv("PAIR_LAUNCH_SETTLE_MAX_S", "0")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    world.excluded = {"c2": "no usable phone or email at launch"}  # claimed, never posted
    world.pairbot_mode = {"c1": "timeout", "c4": "timeout"}  # Pairbot HAS them, PAIR got no answer
    world.pairbot.down = True  # by-job unreachable while settling
    s = run(drive(store, world, sid))
    assert s["status"] == "completed"
    assert {c for c, v in states(store, sid).items() if v == S.ROW_UNCONFIRMED} == {"c1", "c4"}
    data = {cid: r["data"] for (k, cid), r in store.sourced.items()}
    index = launching.posted_index(s["checkpoint"]["s7"]["inflight"])
    for cid in ("c1", "c4"):
        assert data[cid]["engage_status"] == "processing" and data[cid]["engage_session_id"] == sid
        assert data[cid]["engage_bulk_id"] == index[cid][0]
    assert data["c2"]["engage_status"] == "" and "engage_session_id" not in data["c2"]
    assert s["counts"]["claims"] == {"released": 1, "parked": 2}
    assert world.provision_calls == []  # nothing proven launched yet

    # Pairbot is back; the recruiter clicks again before the re-check runs.
    world.pairbot.down = False
    world.pairbot_mode = {}
    world.excluded = {}
    f2 = new_session_fields(job, inp=fields["input"])
    store.create_session(f2)
    s2 = run(drive(store, world, f2["id"]))
    assert s2["status"] == "completed"
    assert world.pairbot.post_count == {"c1": 1, "c4": 1, "c2": 1}
    assert store.cands[f2["id"]]["c1"]["reason"] == "in_flight"

    # The re-check proves c1 / c4 launched and gives them their JobDiva application.
    store.set_column(sid, "recheck_at", store.t)
    assert run(LaunchSessionRunner(store=store, fx=world, owner="hk").recheck_unconfirmed()) == 1
    assert states(store, sid)["c1"] == states(store, sid)["c4"] == S.ROW_LAUNCHED
    assert sorted(sum(world.provision_calls[-1:], [])) == ["c1", "c4"]


def test_a_batch_failing_before_its_post_fails_its_rows_instead_of_looping():
    """review-durability #2: nothing posted, nothing recorded → the rows fail
    (retryable by a retry-failed child), their claims are handed back, the
    session completes; the failed batch keeps the job posting back."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    calls = {"n": 0}

    async def failing_run_launch(request, **kw):
        calls["n"] += 1
        ids = list(request.candidate_ids)
        yield {"type": "start", "_internal": {"batches": [ids]}}
        yield {"type": "batch", "index": 0, "status": "failed", "error": "generate-payload returned no payload",
               "candidate_ids": ids,
               "_internal": {"status": "failed", "candidate_ids": ids, "sent_ids": [], "no_interview_ids": [],
                             "failed_ids": ids, "skipped_ids": [], "excluded_ids": [], "bulk_id": None}}
        yield {"type": "done", "_internal": {"sent_ids": [], "no_interview_ids": [], "skipped_ids": [],
                                             "excluded_ids": [], "failed_ids": ids, "not_run_ids": [],
                                             "stopped": False}}

    world.run_launch = failing_run_launch
    s = run(drive(store, world, sid))
    assert s["status"] == "completed" and calls["n"] == 1 and s["attempt"] == 0
    for cid in ("c1", "c2", "c4"):
        row = store.cands[sid][cid]
        assert (row["state"], row["reason"], row["failure_stage"]) == ("failed", "launch_failed", "engage")
        assert row["error"] == "generate-payload returned no payload"
    data = {cid: r["data"] for (k, cid), r in store.sourced.items()}
    assert all(data[c]["engage_status"] == "" and "engage_session_id" not in data[c] for c in ("c1", "c2", "c4"))
    assert s["counts"]["failed_batches"] == 1 and s["counts"]["sent"] == 0
    assert world.pairbot.posts == []


def test_passes_that_move_no_row_count_as_a_failure(monkeypatch):
    """Belt and braces: three passes that leave the same rows plannable are a
    counted failure (bounded by max attempts), never an endless wait."""
    monkeypatch.setenv("PAIR_LAUNCH_MAX_ATTEMPTS", "2")
    store, world, job, fields = full_search_world()
    store.create_session(fields)

    async def silent_run_launch(request, **kw):
        yield {"type": "start", "_internal": {"batches": [list(request.candidate_ids)]}}
        yield {"type": "done", "_internal": {"not_run_ids": [], "stopped": False}}

    world.run_launch = silent_run_launch
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "failed" and "no progress" in s["error"]
    data = {cid: r["data"] for (k, cid), r in store.sourced.items()}
    assert not [c for c, d in data.items() if d.get("engage_status") == "queued"]  # claims handed back


def test_a_user_cancel_mid_launch_still_provisions_who_was_launched(monkeypatch):
    """review-durability #3: legacy provisioned per batch; a session that ends
    cancelled after launching people provisions them in its finalize path."""
    monkeypatch.setenv("PAIR_LAUNCH_BATCH_SIZE", "1")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    orig = world.run_launch

    async def cancel_after_first(request, **kw):
        n = 0
        async for evt in orig(request, **kw):
            if evt.get("type") == "batch":
                n += 1
                if n == 1:
                    store.cancel_session(sid, "cancelled by user")
            yield evt

    world.run_launch = cancel_after_first
    s = run(drive(store, world, sid))
    assert s["status"] == "cancelled"
    assert states(store, sid)["c1"] == S.ROW_LAUNCHED and sum(world.provision_calls, []) == ["c1"]
    assert world.pairbot.post_count == {"c1": 1}
    # The never-posted claims were handed back.
    data = {cid: r["data"] for (k, cid), r in store.sourced.items()}
    assert not [c for c, d in data.items() if d.get("engage_status") == "queued"]
    assert world.side_effects[0]["send_job_posting_email"] is False


def test_a_failed_claim_release_is_retried_before_the_session_finishes():
    """review-durability #4: a DB blip while handing the claims back fails the
    finish (retried) — the terminal status never hides a leaked claim."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.excluded = {"c4": "no usable phone or email at launch"}  # claimed, never posted
    real_release = world.release_claims
    blips = {"n": 0}

    def flaky_release(*a, **k):
        if blips["n"] == 0:
            blips["n"] += 1
            raise RuntimeError("could not connect to server")
        return real_release(*a, **k)

    world.release_claims = flaky_release
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed" and blips["n"] == 1 and s["attempt"] == 1
    d = store.sourced[(job["jobdiva_id"], "c4")]["data"]
    assert d.get("engage_status") == "" and "engage_session_id" not in d
    assert len(world.side_effects) == 1


def test_side_effects_fire_when_the_launch_ends_before_jobdiva_provisioning():
    """review-parity #4: the recruiter email goes out at the end of S7 (as
    legacy's), not after S7b; S8 never fires them twice."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    seen: List[int] = []
    orig = world.provision

    async def provision(job_id, ids):
        seen.append(len(world.side_effects))
        return await orig(job_id, ids)

    world.provision = provision
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed" and seen == [1] and len(world.side_effects) == 1
    assert s["counts"]["side_effects"] == "fired" and s["checkpoint"]["fx"]["outcome"] == "fired"


def test_a_cancel_racing_the_review_handoff_cancels_the_session():
    """review-durability #5b: the hand-off to the recruiter never happens over
    a pending cancel."""
    store, world, job, fields = full_search_world()
    fields["input"]["require_review"] = True
    store.create_session(fields)
    sid = fields["id"]
    orig = store.to_awaiting_input

    def cancel_then_handoff(fence, checkpoint=None):
        store.cancel_session(sid, "cancelled by user")
        return orig(fence, checkpoint)

    store.to_awaiting_input = cancel_then_handoff
    s = run(drive(store, world, sid))
    assert s["status"] == "cancelled" and s["error"] == "cancelled by user"
    assert world.pairbot.posts == [] and world.save_calls == []


def test_tick_and_claim_now_share_the_workers_one_slot():
    """review-durability #6b: the free slot is reserved before the claim is
    awaited."""
    store = MemoryStore()
    world = FakeWorld(store)
    sids = []
    for i in range(2):
        job = add_job(store, job_id=f"{3000 + i}", ref=f"26-9{i}")
        f = new_session_fields(job)
        store.create_session(f)
        sids.append(f["id"])
    r = Runner(store, world, owner="W")
    r.run_claim = lambda claim: asyncio.sleep(0)

    async def go():
        return await asyncio.gather(r.tick(), r.claim_now(sids[1]))

    res = run(go())
    running = [s for s in store.sessions.values() if s["status"] == "running" and s["owner"] == "W"]
    assert len(running) == 1 and sorted(res, key=str) in ([0, True], [1, False])


def test_a_full_search_is_handed_over_to_the_launch_cap_after_selection(monkeypatch):
    """review-parity #5: searches run under their own cap; the launch phase
    under PAIR_LAUNCH_GLOBAL_CONCURRENCY — a session whose search is done
    waits for a launch slot instead of exceeding the cap."""
    monkeypatch.setenv("PAIR_LAUNCH_GLOBAL_CONCURRENCY", "1")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    other = add_job(store, job_id="2001", ref="26-201")
    busy = new_session_fields(other, mode=S.MODE_PROVIDED)
    store.create_session(busy)
    [held] = store.claim("X", 1, global_cap=1, lease_s=10_000, session_id=busy["id"])  # the launch slot

    async def rounds():
        r = Runner(store, world)
        await r.tick()
        await r.settle_tasks()

    run(rounds())  # searched despite the full launch cap, then handed over
    s = store.sessions[sid]
    assert s["stage"] == S.STAGE_ENRICHING and s["status"] == "running" and s["owner"] is None
    assert world.enrich_calls == []
    run(rounds())
    assert store.sessions[sid]["stage"] == S.STAGE_ENRICHING  # still no launch slot
    store.release(Fence(busy["id"], "X", held.claims))
    store.cancel_session(busy["id"], "x")
    s = run(drive(store, world, sid))
    assert s["status"] == "completed" and states(store, sid) == EXPECTED


def test_stop_during_the_search_is_acted_on_at_the_next_flush():
    """The Stop button (POST cancel) while S1 streams: the next flush returns
    the flag — no heartbeat wait — and the partial pool stays visible."""
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    orig = world.search_events

    async def stop_after_first(request, *, schedule_cross_submissions):
        n = 0
        async for ev in orig(request, schedule_cross_submissions=schedule_cross_submissions):
            n += 1
            if n == 2:
                store.cancel_session(sid, "cancelled by user")
            yield ev

    world.search_events = stop_after_first
    s = run(drive(store, world, sid))
    assert s["status"] == "cancelled" and s["error"] == "cancelled by user"
    assert [label for label, _ in world.search_calls] == ["A0"]
    assert store.cands[sid] and all(c["state"] == S.ROW_POOLED for c in store.cands[sid].values())
    assert world.save_calls == [] and world.pairbot.posts == [] and world.side_effects == []


def test_a_do_not_contact_row_dropped_by_the_core_is_skipped_not_replanned(monkeypatch):
    """engagement._run_one_batch drops Do-Not-Contact rows before the send and
    reports them (internal channel) as excluded. The session must mark them
    skipped and finish; before that report existed it re-planned the row every
    pass and ended 'failed: made no progress'."""
    from tests.launch_sessions_harness import TEST_ENV, drive, full_search_world, states

    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    sid = fields["id"]
    orig = world.run_launch

    async def drop_c1_as_dnc(request, **kw):
        had_c1 = "c1" in request.candidate_ids
        request.candidate_ids = [c for c in request.candidate_ids if c != "c1"]
        reported = False
        async for e in orig(request, **kw):
            internal = e.get("_internal")
            if had_c1 and not reported and e.get("type") == "batch" and isinstance(internal, dict):
                internal["excluded_ids"] = list(internal.get("excluded_ids") or []) + ["c1"]
                reported = True
            if had_c1 and e.get("type") == "done" and isinstance(internal, dict):
                internal["excluded_ids"] = list(internal.get("excluded_ids") or []) + ["c1"]
            yield e

    world.run_launch = drop_c1_as_dnc
    s = asyncio.run(drive(store, world, sid, rounds=60))
    assert s["status"] == "completed", s.get("error")
    assert states(store, sid)["c1"] == "skipped"
    assert world.pairbot.post_count.get("c1", 0) == 0
