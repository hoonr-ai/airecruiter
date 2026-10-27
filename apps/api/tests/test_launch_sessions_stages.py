"""Launch-session stages one by one: S1 replay / interruption, S2 selection
reasons (the server's "already launched" predicate), S3 lookups, S5 split +
DNC re-check, S6 save mapping, S7's resume decision table.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from core import tasks as core_tasks
from services.launch_sessions import rules
from services.launch_sessions import schema as S
from services.launch_sessions.context import Lease, StageContext
from services.launch_sessions.errors import RetryLater
from services.launch_sessions.stages import enriching, launching, saving, selecting, sourcing, splitting
from services.launch_sessions.store import Fence
from tests.launch_sessions_fakes import (
    FakeWorld,
    MemoryStore,
    add_job,
    cand,
    candidate_event,
    new_session_fields,
    patch_event,
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


def claimed_ctx(store: MemoryStore, world: FakeWorld, fields: Dict[str, Any], *, stage: str,
                rows: Optional[List[Dict[str, Any]]] = None, checkpoint: Optional[Dict[str, Any]] = None) -> StageContext:
    """A session claimed by owner 'T' at ``stage`` and a StageContext on it."""
    store.create_session(fields, rows or [])
    sid = fields["id"]
    store.set_column(sid, "stage", stage)
    if checkpoint:
        store.set_column(sid, "checkpoint", checkpoint)
    [claim] = store.claim("T", 1, global_cap=3, lease_s=180, session_id=sid)
    runner = Runner(store, world, owner="T")
    return StageContext(runner, Lease(store, Fence(sid, "T", claim.claims)), store.get_session(sid))


def sourced(store: MemoryStore, key: str, cid: str, source: str = "LinkedIn-Exa", **data: Any) -> None:
    store.sourced[(key, cid)] = {"source": source, "data": dict(data)}


def row_rec(r: Dict[str, Any], state: str = S.ROW_POOLED, **extra: Any) -> Dict[str, Any]:
    return {"candidate_id": r["candidate_id"], "source": r.get("source"), "row": r,
            "match_score": r.get("match_score"), "state": state, **extra}


# ── S1 sourcing ──────────────────────────────────────────────────────────────

def _final_pool(store, sid):
    return {cid: (c["state"], c["row"].get("match_score")) for cid, c in store.cands[sid].items()}


def test_s1_interrupted_attempt_is_rerun_and_finished_attempts_are_replayed():
    clean_store, clean_world, _, clean_fields = full_search_world()
    clean_store.create_session(clean_fields)
    run(drive(clean_store, clean_world, clean_fields["id"]))

    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.search_raise_after = {"A1": 1}  # attempt 2 dies after its first row
    s = run(drive(store, world, fields["id"]))

    assert s["status"] == "completed" and s["attempt"] == 1
    # Attempt 1 was NOT searched again (rebuilt from its stored events);
    # attempt 2 was re-run from its start; cross submissions once.
    assert world.search_calls == [("A0", True), ("A1", False), ("A1", False)]
    assert _final_pool(store, fields["id"]) == _final_pool(clean_store, clean_fields["id"])
    assert s["checkpoint"]["s1"]["next_attempt"] == 2
    assert store.stream.get(fields["id"]) in (None, {})  # deleted after selection


def test_s1_replay_rebuilds_the_exact_stream_state():
    store, world, job, fields = full_search_world()
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SOURCING)
    live = rules.StreamRun()
    events = {"A0": world.search_script["A0"], "A1": world.search_script["A1"]}
    stats = []
    for attempt, label in enumerate(("A0", "A1")):
        live.begin_attempt()
        evs = [e for e in events[label] if e.get("type") != "keepalive"]
        live.apply_many(evs)
        store.stream_append(ctx.fence, attempt, 0, evs)
        stats.append({"began": True})
    replayed = run(sourcing.replay(ctx, 2, stats))
    assert replayed.table == live.table
    assert replayed.accumulated == live.accumulated
    assert replayed.qualified() == live.qualified()
    assert replayed.absorbed() == live.absorbed()


def test_s1_stops_relaxing_once_qualified_reaches_the_target():
    store, world, job, fields = full_search_world()
    fields["input"]["qualified_target"] = 2
    store.create_session(fields)
    s = run(drive(store, world, fields["id"]))
    assert [label for label, _ in world.search_calls] == ["A0"]
    assert s["checkpoint"]["s1"]["qualified"] >= 2
    assert "c4" not in store.cands[fields["id"]]


def test_s1_records_progress_for_the_browser():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    s = run(drive(store, world, fields["id"]))
    ui = s["checkpoint"]["s1"]["ui"]
    assert ui["source_statuses"]["JobDiva-TalentSearch"]["status"] == "ok"
    assert s["checkpoint"]["s1"]["attempts_total"] == 2
    assert [a["label"] for a in s["checkpoint"]["s1"]["attempts"]] == ["initial", "relax 1"]
    assert s["counts"]["pool"] == 5


def test_s1_a_stageless_hydration_patch_excludes_the_row_like_the_browser():
    """review-parity #1: hydration patches carry no stage and must still merge."""
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    world.search_script = {"A0": [
        candidate_event(cand("x1", 90)),
        patch_event("x1", {"offer_status": "Offer Accepted"}, stage=None),
        candidate_event(cand("x2", 80)),
    ]}
    fields = new_session_fields(job, inp={"search_attempts": [{"label": "i", "payload": {"boolean_string": "A0"}}]})
    store.create_session(fields)
    run(drive(store, world, fields["id"]))
    c = store.cands[fields["id"]]["x1"]
    assert (c["state"], c["reason"]) == ("skipped", "exclusion:Offer Accepted")
    assert store.cands[fields["id"]]["x2"]["state"] == "launched"


# ── S2 selecting ─────────────────────────────────────────────────────────────

def test_s2_reasons_with_the_server_launched_predicate():
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    ref, jid = job["jobdiva_id"], job["job_id"]
    rows = [
        cand("audit1", 95), cand("data1", 94), cand("jd-77", 93, source="JobDiva-TalentSearch"),
        cand("fly1", 92), cand("app1", 91, source="JobDiva-Applicants"), cand("fail1", 90),
        cand("orph1", 89), cand("blk1", 88), cand("offer1", 87, offer_status="Offer Accepted"),
        cand("nc1", 86, no_contact=True), cand("unsc1", None), cand("low1", 40),
        cand("e1", 70), cand("e2", 65),
        cand("dupA", 80, email="same@mail.test", phone="", source="LinkedIn-Exa"),
        cand("dupB", 60, email="same@mail.test", phone="", source="Dice"),
    ]
    store.audit.append({"candidate_id": "audit1", "jobdiva_id": ref, "interview_id": "iv-a"})
    sourced(store, ref, "data1", engage_status="sent", engage_interview_id="iv-d")
    sourced(store, ref, "launchedX", engage_status="sent", engage_interview_id="iv-x", jobdiva_candidate_id="jd-77")
    sourced(store, jid, "fly1", engage_status="processing")
    sourced(store, ref, "app1", source="JobDiva-Applicants", engage_status="")
    sourced(store, ref, "fail1", engage_status="failed")
    sourced(store, ref, "orph1", engage_status="")
    sourced(store, ref, "blk1", engage_status="failed", engage_block_reason="duplicate_role_other_job")
    fields = new_session_fields(job, inp={"launch_count": 4, "min_score": 60})
    run_ = rules.StreamRun()
    run_.begin_attempt()
    run_.apply_many([candidate_event(r) for r in rows])
    table_recs = [row_rec(r) for r in run_.table]
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SELECTING, rows=table_recs + [row_rec(rows[-1], S.ROW_DROPPED)])
    ctx.memo["stream_run"] = run_
    result = run(selecting.run(ctx))
    assert result.next_stage == S.STAGE_ENRICHING
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[fields["id"]].items()}
    assert got["audit1"] == ("skipped", "launched")
    assert got["data1"] == ("skipped", "launched")
    assert got["jd-77"] == ("skipped", "launched")  # the provisioned twin
    assert got["fly1"] == ("skipped", "in_flight")
    assert got["app1"] == ("skipped", "applicant_backlog")
    assert got["blk1"] == ("skipped", "blocked:duplicate_role_other_job")
    assert got["offer1"] == ("skipped", "exclusion:Offer Accepted")
    assert got["nc1"] == ("skipped", "no_contact")
    assert got["unsc1"] == ("skipped", "unscored")
    assert got["low1"] == ("skipped", "below_floor")
    assert got["dupB"] == ("skipped", "identity_duplicate:dupA")
    # Re-selectable: a failed launch and an orphan save; cap 4 by score.
    selected = [cid for cid, (st, _) in got.items() if st == "selected"]
    assert sorted(selected) == sorted(["fail1", "orph1", "dupA", "e1"])
    assert got["e2"] == ("skipped", "over_limit")
    sel = store.sessions[fields["id"]]["counts"]["selection"]
    assert sel["selected"] == 4 and sel["below_floor"] == 1 and sel["unscored"] == 1
    assert store.sessions[fields["id"]]["selection_done_at"] is not None


def test_s2_provided_mode_trusts_the_choice_but_dedups_and_has_no_floor():
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    rows = [cand("p1", None), cand("p2", 20), cand("p3", 70, email="x@mail.test", phone=""),
            cand("p4", 50, email="x@mail.test", phone="", source="Dice")]
    fields = new_session_fields(job, mode=S.MODE_PROVIDED, inp={"launch_count": None})
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SELECTING, rows=[row_rec(r) for r in rows])
    run(selecting.run(ctx))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[fields["id"]].items()}
    assert got["p1"][0] == got["p2"][0] == got["p3"][0] == "selected"
    assert got["p4"] == ("skipped", "identity_duplicate:p3")


# ── S3 enriching ─────────────────────────────────────────────────────────────

def test_s3_lookups_counters_and_per_row_checkpoint():
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    li = "https://www.linkedin.com/in/someone"
    rows = [
        cand("a", 80),  # has both: not looked up
        cand("b", 80, email="", phone="", profile_url=li + "-b"),
        cand("c", 80, email="", phone=""),  # no LinkedIn
        cand("d", 80, email=""),  # phone only, no LinkedIn → already reachable
        cand("e", 80, email="", phone="", linkedin_url=li + "-e"),
        cand("f", 80, email="", phone="", resume_text=f"see {li}-f for more"),
        cand("g", 80, email="", phone="", profile_url=li + "-g"),
    ]
    world.enrich_answers = {
        "b": {"status": "success", "email": "b@found.test", "email_provider": "exa",
              "lookup": {"kipplo": "no_credits"}},
        "e": {"status": "skipped"},
    }
    world.enrich_drop = {"f"}
    recs = [row_rec(r, S.ROW_SELECTED) for r in rows]
    recs[-1]["contact"] = {"done": True, "outcome": "no_contact"}  # g: answered before a crash
    fields = new_session_fields(job)
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_ENRICHING, rows=recs)
    res = run(enriching.run(ctx))
    assert res.next_stage == S.STAGE_SPLITTING
    assert sorted(world.enrich_calls) == ["b", "e", "f"]  # never a, c, d, or the done g
    contact = {cid: c["contact"] for cid, c in store.cands[fields["id"]].items()}
    assert contact["b"]["outcome"] == "succeeded" and contact["b"]["override"] == {"email": "b@found.test"}
    assert contact["c"]["outcome"] == "missing_linkedin"
    assert contact["d"]["outcome"] == "already_reachable"
    assert contact["e"]["outcome"] == "not_eligible"
    assert contact["f"]["outcome"] == "failed"
    counts = store.sessions[fields["id"]]["counts"]["enrich"]
    assert counts["succeeded"] == 1 and counts["missing_linkedin"] == 1 and counts["already_reachable"] == 1
    assert counts["not_eligible"] == 1 and counts["no_contact"] == 2 and counts["failed"] == 1
    assert counts["email_found_by"] == {"exa": 1} and counts["kipplo_issues"] == {"no_credits": 1}
    assert store.sessions[fields["id"]]["enrich_done_at"] is not None


def test_s3_goes_to_review_when_required():
    store = MemoryStore()
    job = add_job(store)
    fields = new_session_fields(job, inp={"require_review": True})
    ctx = claimed_ctx(store, FakeWorld(store), fields, stage=S.STAGE_ENRICHING,
                      rows=[row_rec(cand("a", 80), S.ROW_SELECTED)])
    assert run(enriching.run(ctx)).next_stage == S.STAGE_REVIEW


# ── S5 splitting ─────────────────────────────────────────────────────────────

def test_s5_dnc_recheck_on_the_effective_phone_and_duplicate_rules():
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    rows = [
        cand("dnc", 80, phone=""),  # its looked-up phone is on the DNC list
        cand("p1", 80, phone="+1 512 555 0001", email="p1@mail.test"),
        cand("p2", 80, phone="+1 512 555 0001", email="p2@mail.test"),  # dup phone, unique email → ready
        cand("q1", 80, phone="+1 512 555 0002", email="q@mail.test"),
        cand("q2", 80, phone="+1 512 555 0002", email="q@mail.test"),  # both shared → needs contact
    ]
    recs = [row_rec(r, S.ROW_SELECTED) for r in rows]
    recs[0]["contact"] = {"done": True, "outcome": "succeeded", "override": {"phone": "+1 (512) 555-9999"}}
    world.dnc = {rules.normalize_dnc_phone("+15125559999")}
    fields = new_session_fields(job)
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SPLITTING, rows=recs)
    run(splitting.run(ctx))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[fields["id"]].items()}
    assert got["dnc"] == ("skipped", "dnc")
    assert got["p1"][0] == got["p2"][0] == "ready"
    assert got["q1"] == got["q2"] == ("needs_contact", "duplicate_contact")
    needs = store.cands[fields["id"]]["q1"]["contact"]["needs"]
    assert needs["duplicatePhone"] and needs["duplicateEmail"]
    split = store.sessions[fields["id"]]["counts"]["split"]
    assert split == {"ready": 2, "needs_contact": 2, "dnc": 1, "hard_filter": 0,
                     "duplicate_email": 2, "duplicate_phone": 4}


# ── S6 saving ────────────────────────────────────────────────────────────────

def test_s6_maps_the_save_answer_and_retries_transient_errors(monkeypatch):
    monkeypatch.setattr(saving.asyncio, "sleep", _no_sleep)
    store = MemoryStore()
    job = add_job(store, customer_name="Acme")
    world = FakeWorld(store)
    rows = [cand(f"r{i}", 80) for i in range(1, 6)]
    rows.append(cand("cc", 80, company="Acme Inc", headline="Engineer at Acme"))
    world.save_script = [
        {"dnc": ["r2"]},
        HTTPException(status_code=503, detail="busy"), {"nc": ["r3"], "unsaved": ["r4"]},
        HTTPException(status_code=400, detail="bad payload"),
    ]
    fields = new_session_fields(job)
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SAVING, rows=[row_rec(r, S.ROW_READY) for r in rows])
    run(saving.run(ctx))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[fields["id"]].items()}
    assert got["r1"] == ("saved", None)
    assert got["r2"] == ("skipped", "dnc")
    assert got["r3"] == ("skipped", "no_contact")
    assert got["r4"] == ("failed", "not_saved")
    assert got["r5"] == ("failed", "save_rejected")
    assert store.cands[fields["id"]]["r5"]["failure_stage"] == "save"
    assert store.cands[fields["id"]]["r5"]["error"] == "bad payload"
    assert store.cands[fields["id"]]["r5"]["batch_index"] == 2
    assert got["cc"] == ("skipped", "client_conflict")  # never sent to save
    assert world.save_calls == [["r1", "r2"], ["r3", "r4"], ["r3", "r4"], ["r5"]]
    assert world.saved_rows["r1"]["match_score"] == 80 and world.saved_rows["r1"]["is_selected"] is True


async def _no_sleep(*_a, **_k):
    return None


# ── S7 resume decision table ─────────────────────────────────────────────────

def _launching_ctx(store, world, *, posted_age_s: Dict[str, float]):
    job = add_job(store)
    ref = job["jobdiva_id"]
    fields = new_session_fields(job)
    sid = fields["id"]
    now = dt.datetime.now(dt.timezone.utc)
    posted = {}
    rows = []
    for cid, age in posted_age_s.items():
        bulk = f"bulk-{cid}"
        posted[bulk] = {"batch": 0, "attempt": 1, "ids": [cid],
                        "posted_at": (now - dt.timedelta(seconds=age)).isoformat()}
        sourced(store, ref, cid, engage_status="processing", engage_session_id=sid, engage_bulk_id=bulk)
        rows.append(row_rec(cand(cid, 80), S.ROW_LAUNCHING))
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_LAUNCHING, rows=rows,
                      checkpoint={"s7": {"launch_attempt": 1, "inflight": posted, "settle_since": None}})
    return ctx, sid


def _script_reconcile(world, result):
    calls = []

    async def fake(job_keys, ids, *, session_id, own_bulk_ids):
        calls.append({"ids": sorted(ids), "own": sorted(own_bulk_ids)})
        out = {"sent": {}, "unrecorded": {}, "duplicate_of": {}, "missing": [], "bulk_in_progress": False,
               "in_progress_bulk_id": None, "own_bulks_creating": [], "error": None}
        out.update(result)
        return out

    world.reconcile = fake
    return calls


def test_s7_settle_decision_table(monkeypatch):
    monkeypatch.setenv("PAIR_LAUNCH_REPOST_AFTER_S", "600")
    monkeypatch.setenv("PAIR_LAUNCH_FRESH_BULK_S", "30")
    store = MemoryStore()
    world = FakeWorld(store)
    ctx, sid = _launching_ctx(store, world, posted_age_s={
        "sent": 900, "unrec": 900, "dup": 900, "old": 900, "fresh": 120, "pending": 900, "brand_new": 5,
    })
    calls = _script_reconcile(world, {
        "sent": {"sent": "iv-1"}, "unrecorded": {"unrec": "iv-2"},
        "duplicate_of": {"dup": {"interview_id": "iv-3", "source_candidate_id": "twin"}},
        "missing": ["old", "fresh"],
    })
    with pytest.raises(RetryLater) as wait:
        run(launching.settle(ctx))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[sid].items()}
    assert got["sent"] == ("launched", "reconciled")
    assert got["unrec"] == ("launched", "unrecorded")
    assert got["dup"] == ("skipped", "duplicate_person_this_job")
    assert got["old"] == ("saved", None)  # Pairbot never created it, posted 15 min ago → re-post
    assert store.sourced[("26-001", "old")]["data"]["engage_status"] == "queued"
    assert "old" not in sum((r["ids"] for r in store.sessions[sid]["checkpoint"]["s7"]["inflight"].values()), [])
    assert got["fresh"] == ("launching", None)  # posted 2 min ago: wait, never re-post yet
    assert got["pending"] == got["brand_new"] == ("launching", None)  # unresolved
    assert 470 <= wait.value.delay_s <= 480  # until "fresh" is 10 min old
    # own_bulk_ids: every recorded bulk except the one posted 5 s ago.
    assert "bulk-brand_new" not in calls[0]["own"] and "bulk-fresh" in calls[0]["own"]
    assert store.sessions[sid]["checkpoint"]["s7"]["settle_since"]


def test_s7_settle_never_reposts_while_a_bulk_is_creating_or_by_job_is_down():
    for result in ({"bulk_in_progress": True, "own_bulks_creating": ["bulk-x"]}, {"error": "by-job 503"}):
        store = MemoryStore()
        world = FakeWorld(store)
        ctx, sid = _launching_ctx(store, world, posted_age_s={"x": 3600})
        _script_reconcile(world, result)
        with pytest.raises(RetryLater):
            run(launching.settle(ctx))
        assert store.cands[sid]["x"]["state"] == "launching"
        assert store.sourced[("26-001", "x")]["data"]["engage_status"] == "processing"


def test_s7_final_settle_marks_unresolved_unconfirmed_and_never_created_failed():
    store = MemoryStore()
    world = FakeWorld(store)
    ctx, sid = _launching_ctx(store, world, posted_age_s={"gone": 3600, "unknown": 3600})
    _script_reconcile(world, {"missing": ["gone"]})
    run(launching.settle(ctx, final=True))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[sid].items()}
    assert got["gone"] == ("failed", "not_created")
    assert got["unknown"] == ("unconfirmed", "unconfirmed")
    data = store.sourced[("26-001", "gone")]["data"]
    assert data["engage_status"] == "failed" and "engage_session_id" not in data


def test_s7_a_post_pairbot_accepted_but_never_answered_is_reconciled_not_reposted():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.pairbot_mode = {"c1": "timeout"}
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed"
    assert states(store, fields["id"])["c1"] == "launched"
    assert store.cands[fields["id"]]["c1"]["reason"] == "reconciled"
    assert world.pairbot.post_count["c1"] == 1


def test_s7_rows_launched_by_another_path_meanwhile_are_already_launched():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    original = world.claim_for_session

    def racing_claim(job_keys, ids, session_id, duplicates=None):
        # A legacy launch posted c4 between S6 and our claim.
        world._record_sent(list(job_keys), {"c4": "iv-legacy"})
        return original(job_keys, ids, session_id, duplicates=duplicates)

    world.claim_for_session = racing_claim
    s = run(drive(store, world, fields["id"]))
    c4 = store.cands[fields["id"]]["c4"]
    assert (c4["state"], c4["reason"]) == ("launched", "already_launched")
    assert "c4" not in world.pairbot.post_count
    assert s["counts"]["sent"] == 2 and s["counts"]["already_launched"] == 1


def test_s7_excluded_rows_carry_the_core_reason():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.excluded = {"c4": "employed by hiring client"}
    run(drive(store, world, fields["id"]))
    c4 = store.cands[fields["id"]]["c4"]
    assert (c4["state"], c4["reason"]) == ("skipped", "excluded:employed by hiring client")


def test_s7b_honours_the_provisioning_kill_switch():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    world.provisioning_on = False
    s = run(drive(store, world, fields["id"]))
    assert world.provision_calls == []
    assert s["counts"]["provision"]["disabled"] is True


# ── review fixes (parity) ────────────────────────────────────────────────────

def test_s2_launch_remaining_applies_the_floor_and_the_default_cap():
    """review-parity #2: the browser's "launch remaining" never launches below
    60% (min_score when given); QA quick launch and children have no floor."""
    rows = [cand("hi", 90), cand("lo", 35), cand("na", None)]
    for trigger, inp, expected in (
        ("launch_remaining", {"launch_count": None}, {"hi"}),
        ("launch_remaining", {"launch_count": None, "min_score": 30}, {"hi", "lo"}),
        ("qa_quick", {"launch_count": None, "min_score": 60}, {"hi", "lo", "na"}),
        ("needs_contact", {"launch_count": None, "min_score": 60}, {"hi", "lo", "na"}),
        ("button", {"launch_count": None, "min_score": 60}, {"hi"}),
        ("button", {"launch_count": None}, {"hi", "lo", "na"}),
    ):
        store = MemoryStore()
        job = add_job(store)
        world = FakeWorld(store)
        fields = new_session_fields(job, mode=S.MODE_PROVIDED, trigger=trigger, inp=inp)
        ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SELECTING, rows=[row_rec(r) for r in rows])
        run(selecting.run(ctx))
        got = {cid for cid, c in store.cands[fields["id"]].items() if c["state"] == "selected"}
        assert got == expected, (trigger, inp, got)
        sel = store.sessions[fields["id"]]["counts"]["selection"]
        if trigger == "launch_remaining":
            assert sel["limit"] == rules.AUTO_LAUNCH_DEFAULT_COUNT
        elif trigger == "button":
            assert sel["limit"] == 3  # the uploaded rows


def test_s1_qualified_is_the_parity_rule_whatever_min_score_is():
    """review-parity #6: the relaxation loop's stop count is the browser's
    fixed 60% rule, not the session's min_score."""
    store, world, job, fields = full_search_world()
    fields["input"].update(min_score=95, qualified_target=2)
    store.create_session(fields)
    s = run(drive(store, world, fields["id"]))
    assert [label for label, _ in world.search_calls] == ["A0"]  # c1 92 + c2 85 ≥ 60
    assert s["checkpoint"]["s1"]["qualified"] == 2


def test_s1_binds_every_search_to_the_sessions_job():
    """review-parity #1 (defence in depth): a stored payload for another job
    searches this job."""
    store, world, job, fields = full_search_world()
    fields["input"]["search_attempts"][0]["payload"]["job_id"] = "26-999"
    fields["input"]["search_attempts"][1]["payload"]["job_id"] = "26-001"
    seen = []
    orig = world.search_events

    def spy(request, *, schedule_cross_submissions, **kw):
        seen.append(request.get("job_id"))
        return orig(request, schedule_cross_submissions=schedule_cross_submissions, **kw)

    world.search_events = spy
    store.create_session(fields)
    run(drive(store, world, fields["id"]))
    assert seen == ["1001", "26-001"]


def test_s6_a_payload_the_request_model_refuses_fails_only_its_batch(monkeypatch):
    """review-parity #9: a pydantic ValidationError is the browser's 422 —
    that batch fails (no retry, no values in the error), the next one saves."""
    from pydantic import BaseModel

    class _M(BaseModel):
        location: str

    try:
        _M(location={"city": "Austin"})
    except Exception as exc:  # the real error type the save request raises
        refused = exc
    monkeypatch.setattr(saving.asyncio, "sleep", _no_sleep)
    store = MemoryStore()
    job = add_job(store)
    world = FakeWorld(store)
    rows = [cand(f"v{i}", 80) for i in range(1, 5)]
    world.save_script = [refused]
    fields = new_session_fields(job)
    ctx = claimed_ctx(store, world, fields, stage=S.STAGE_SAVING, rows=[row_rec(r, S.ROW_READY) for r in rows])
    run(saving.run(ctx))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[fields["id"]].items()}
    assert got["v1"] == got["v2"] == ("failed", "save_rejected")
    assert got["v3"] == got["v4"] == ("saved", None)
    assert store.cands[fields["id"]]["v1"]["error"] == "invalid save payload (ValidationError)"
    assert world.save_calls == [["v1", "v2"], ["v3", "v4"]]
    counts = store.sessions[fields["id"]]["counts"]
    assert counts["saved"] == counts["saved_total"] == 2


def test_s7b_a_failing_chunk_is_retried_then_counted_never_failing_the_session(monkeypatch):
    """review-parity #10: provisioning never fails a launched session."""
    from services.launch_sessions.stages import provisioning

    monkeypatch.setattr(provisioning.asyncio, "sleep", _no_sleep)
    monkeypatch.setenv("PAIR_LAUNCH_PROVISION_CHUNK", "2")
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    calls = {"n": 0}
    orig = world.provision

    async def flaky(job_id, ids):
        calls["n"] += 1
        if "c1" in ids:
            raise RuntimeError("claim DB error")
        return await orig(job_id, ids)

    world.provision = flaky
    s = run(drive(store, world, fields["id"]))
    assert s["status"] == "completed" and s["attempt"] == 0
    prov = s["counts"]["provision"]
    assert prov["failed"] == 2 and prov["errors"] == 1 and prov["success"] == 1
    assert calls["n"] == 3 + 1  # three tries of the first chunk, one of the second


def test_s7_a_second_id_of_a_held_person_is_skipped_never_posted():
    store, world, job, fields = full_search_world()
    store.create_session(fields)
    # The person gate (routers.engagement._person_gate) says c4 is another id
    # of a person already launched for this job.
    world.person_duplicates = {"c4": ("exa_other", "email")}
    s = run(drive(store, world, fields["id"]))
    c4 = store.cands[fields["id"]]["c4"]
    assert (c4["state"], c4["reason"]) == ("skipped", "duplicate_person_this_job")
    assert "c4" not in world.pairbot.post_count
    assert s["counts"]["sent"] == 2


def test_s7_a_row_is_reposted_at_most_max_reposts_times(monkeypatch):
    """'missing' at by-job is judged by the echoed source_candidate_id only, so
    an interview Pairbot filed under another id looks missing forever: after
    PAIR_LAUNCH_MAX_REPOSTS re-posts the row is unconfirmed (parked and
    re-checked by housekeeping), never posted again."""
    monkeypatch.setenv("PAIR_LAUNCH_REPOST_AFTER_S", "600")
    store = MemoryStore()
    world = FakeWorld(store)
    ctx, sid = _launching_ctx(store, world, posted_age_s={"once": 900, "again": 900})
    store.sessions[sid]["checkpoint"]["s7"]["reposts"] = {"again": 1}
    _script_reconcile(world, {"missing": ["once", "again"]})
    run(launching.settle(ctx))
    got = {cid: (c["state"], c["reason"]) for cid, c in store.cands[sid].items()}
    assert got["once"] == ("saved", None)                       # first re-post allowed
    assert got["again"] == ("unconfirmed", "unconfirmed")       # limit reached: never posted again
    assert store.sessions[sid]["checkpoint"]["s7"]["reposts"] == {"again": 1, "once": 1}
    monkeypatch.setenv("PAIR_LAUNCH_MAX_REPOSTS", "0")
    store2 = MemoryStore()
    world2 = FakeWorld(store2)
    ctx2, sid2 = _launching_ctx(store2, world2, posted_age_s={"x": 900})
    _script_reconcile(world2, {"missing": ["x"]})
    run(launching.settle(ctx2))
    assert store2.cands[sid2]["x"]["state"] == "unconfirmed"
