"""Shared driver for the launch-session runner tests (MemoryStore + FakeWorld)."""

from __future__ import annotations

import asyncio
import itertools
from typing import Any, Dict, List, Optional

from services.launch_sessions import schema as S
from services.launch_sessions.runner import LaunchSessionRunner
from tests.launch_sessions_fakes import (
    Crash,
    CrashPlan,
    FakeWorld,
    MemoryStore,
    add_job,
    cand,
    candidate_event,
    new_session_fields,
    patch_event,
)

_owners = itertools.count(1)

# Fast, deterministic knobs for every runner test.
TEST_ENV = {
    "PAIR_LAUNCH_REPOST_AFTER_S": "0",
    "PAIR_LAUNCH_FRESH_BULK_S": "0",
    "PAIR_LAUNCH_SETTLE_MAX_S": "1800",
    "PAIR_LAUNCH_STREAM_FLUSH_EVENTS": "2",
    "PAIR_LAUNCH_SAVE_BATCH": "2",
    "PAIR_LAUNCH_HEARTBEAT_S": "3600",
    "PAIR_LAUNCH_GLOBAL_CONCURRENCY": "3",
    "PAIR_LAUNCH_SEARCH_CONCURRENCY": "3",
    "PAIR_LAUNCH_WORKER_CONCURRENCY": "1",
}


class Runner(LaunchSessionRunner):
    """A runner whose spawned claim tasks the test awaits."""

    def __init__(self, store: Any, fx: Any, owner: Optional[str] = None) -> None:
        self.spawned: List[asyncio.Task] = []

        def spawn(coro, name=None, nr_txn=True):
            task = asyncio.ensure_future(coro)
            self.spawned.append(task)
            return task

        super().__init__(store=store, fx=fx, owner=owner or f"worker-{next(_owners)}", spawn=spawn)

    async def settle_tasks(self) -> List[BaseException]:
        errors: List[BaseException] = []
        while self.spawned:
            task = self.spawned.pop(0)
            try:
                await task
            except BaseException as exc:  # noqa: BLE001 — Crash is a simulated SIGKILL
                errors.append(exc)
        return errors


async def drive(store: MemoryStore, world: FakeWorld, sid: str, *, rounds: int = 40,
                stop_at_input: bool = True, advance_s: float = 1000.0) -> Dict[str, Any]:
    """Tick (as a fresh worker after every simulated crash) until the session
    is terminal (or awaiting input). Between rounds the clock moves past any
    lease / not_before."""
    runner = Runner(store, world)
    for _ in range(rounds):
        s = store.sessions[sid]
        if s["status"] in S.TERMINAL_STATUSES or (stop_at_input and s["status"] == S.STATUS_AWAITING_INPUT):
            return dict(s)
        try:
            await runner.tick()
        except Crash:
            runner = Runner(store, world)  # died right after claiming
            store.advance(advance_s)
            continue
        errors = await runner.settle_tasks()
        if any(isinstance(e, Crash) for e in errors):
            runner = Runner(store, world)  # the process died: another worker adopts it
        other = [e for e in errors if not isinstance(e, Crash)]
        if other:
            raise other[0]
        store.advance(advance_s)
    raise AssertionError(f"session {sid} did not finish: {store.sessions[sid]['status']}/{store.sessions[sid]['stage']}")


def full_search_world(crash: Optional[CrashPlan] = None):
    """The standard full-search scenario: two attempts, five people.

      c1 90 → 92 (scored patch), contacts         → launched
      c2 85, no contact, LinkedIn → lookup finds a phone → launched
      c3 40                                       → skipped below_floor
      c4 75 (attempt 2), contacts                 → launched
      c5 88 (attempt 2), no contact, no LinkedIn  → needs_contact
    """
    store = MemoryStore(crash or CrashPlan())
    job = add_job(store)
    world = FakeWorld(store)
    c1 = cand("c1", 90)
    c2 = cand("c2", 85, email="", phone="", profile_url="https://www.linkedin.com/in/c-two")
    c3 = cand("c3", 40)
    c4 = cand("c4", 75)
    c5 = cand("c5", 88, email="", phone="")
    world.search_script = {
        "A0": [
            {"type": "stage", "data": "Searching JobDiva Talent Search"},
            candidate_event(c1), candidate_event(c2), {"type": "keepalive"}, candidate_event(c3),
            patch_event("c1", {"match_score": 92}),
            {"type": "source_status", "data": {"source": "JobDiva-TalentSearch", "status": "ok"}},
        ],
        "A1": [candidate_event(c4), candidate_event(c5), {"type": "summary", "data": {"summary": {}}}],
    }
    world.enrich_answers = {"c2": {"status": "success", "phone": "+15125550123", "phone_provider": "apollo"}}
    fields = new_session_fields(job, inp={
        "search_attempts": [{"label": "initial", "payload": {"boolean_string": "A0"}},
                            {"label": "relax 1", "payload": {"boolean_string": "A1"}}],
        "launch_count": 10, "min_score": 60, "qualified_target": 50,
    })
    return store, world, job, fields


def states(store: MemoryStore, sid: str) -> Dict[str, str]:
    return {cid: c["state"] for cid, c in store.cands[sid].items()}
