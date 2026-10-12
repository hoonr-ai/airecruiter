"""The runner's per-claim state: the lease (heartbeat + self-fence) and the
StageContext every stage function receives.

A stage is ``async def run(ctx) -> StageResult``: it reads the session row and
its checkpoint from ``ctx``, does its units, and writes every result through
``ctx`` — always fenced (owner + claims), so a worker that lost the session
can never overwrite the new owner. Before every externally visible unit
(a Pairbot POST batch, a save batch, a paid contact lookup) it awaits
``ctx.assert_owner()``; between units it awaits ``ctx.boundary()``, which
raises when the process is stopping, the lease may have expired, ownership
was lost, or the recruiter cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from core import tasks as core_tasks
from services.launch_sessions import config as cfg
from services.launch_sessions import events as ev
from services.launch_sessions.errors import (
    CancelRequested,
    LeaseExpired,
    OwnershipLost,
    Shutdown,
)
from services.launch_sessions.store import Fence

logger = logging.getLogger(__name__)


@dataclass
class StageResult:
    """What the runner does after a stage returns.

    next_stage  advance (fenced) and run it in the same claim
    wait_input  hand the session to the recruiter (status awaiting_input)
    done        the stage finished the session itself (terminal status)
    """
    next_stage: Optional[str] = None
    wait_input: bool = False
    done: bool = False
    checkpoint: Dict[str, Any] = field(default_factory=dict)


class Lease:
    """One claim of one session by this worker.

    The heartbeat renews it every PAIR_LAUNCH_HEARTBEAT_S; a heartbeat that
    raises is NOT ownership loss, but once ``lease - margin`` seconds pass
    without a successful renewal the runner self-fences (starts nothing new):
    another worker may claim the session as soon as the lease runs out."""

    def __init__(self, store: Any, fence: Fence) -> None:
        self.store = store
        self.fence = fence
        self.last_ok = time.monotonic()
        self.cancel_requested = False
        self.lost = False
        # Set once the runner gave the session back (store.release clears the
        # owner): the heartbeat stops quietly instead of reporting a loss.
        self.released = False

    @property
    def deadline(self) -> float:
        return self.last_ok + cfg.lease_s() - cfg.self_fence_margin_s()

    def stop_reason(self) -> Optional[str]:
        if self.lost:
            return "ownership_lost"
        if time.monotonic() >= self.deadline:
            return "lease_expired"
        if core_tasks.is_stopping():
            return "stopping"
        if self.cancel_requested:
            return "cancel_requested"
        return None

    def check(self, *, allow_cancel: bool = False) -> None:
        reason = self.stop_reason()
        if allow_cancel and reason == "cancel_requested":
            return
        if reason == "ownership_lost":
            raise OwnershipLost(self.fence.session_id)
        if reason == "lease_expired":
            raise LeaseExpired(self.fence.session_id)
        if reason == "stopping":
            raise Shutdown(self.fence.session_id)
        if reason == "cancel_requested":
            raise CancelRequested(self.fence.session_id)

    def renewed(self, cancel_requested: bool) -> None:
        self.last_ok = time.monotonic()
        self.cancel_requested = self.cancel_requested or bool(cancel_requested)

    async def heartbeat_loop(self, db: Callable[..., Any]) -> None:
        interval = cfg.heartbeat_s()
        while not self.lost and not self.released:
            await asyncio.sleep(interval)
            if self.released:
                return
            try:
                cancel = await db(self.store.heartbeat, self.fence, lease_s=cfg.lease_s())
                self.renewed(cancel)
            except OwnershipLost:
                if not self.released:
                    logger.warning("launch_session_ownership_lost session=%s (heartbeat)", self.fence.session_id)
                self.lost = True
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — self-fence covers a dead DB
                logger.warning("launch_session_heartbeat_failed session=%s err=%s", self.fence.session_id, exc)


class StageContext:
    def __init__(self, runner: Any, lease: Lease, session: Dict[str, Any]) -> None:
        self.runner = runner
        self.store = runner.store
        self.fx = runner.fx
        self.lease = lease
        self.session = session
        self.memo: Dict[str, Any] = {}

    # ── identity ────────────────────────────────────────────────────────────
    @property
    def sid(self) -> str:
        return str(self.session["id"])

    @property
    def fence(self) -> Fence:
        return self.lease.fence

    @property
    def job_id(self) -> str:
        return str(self.session["job_id"])

    @property
    def job_ref(self) -> str:
        """The key the browser flow uses for save / launch: the JobDiva ref,
        else the job id."""
        return str(self.session.get("jobdiva_ref") or self.session["job_id"])

    @property
    def job_keys(self) -> List[str]:
        keys: List[str] = []
        for k in (self.session.get("job_id"), self.session.get("jobdiva_ref")):
            s = str(k or "").strip()
            if s and s not in keys:
                keys.append(s)
        return keys

    @property
    def input(self) -> Dict[str, Any]:
        return self.session.get("input") or {}

    @property
    def options(self) -> Dict[str, Any]:
        return self.input.get("options") or {}

    @property
    def ck(self) -> Dict[str, Any]:
        return self.session.get("checkpoint") or {}

    # ── plumbing ────────────────────────────────────────────────────────────
    async def db(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return await self.runner.db(fn, *args, **kwargs)

    async def boundary(self) -> None:
        """A unit boundary: raise if this claim must stop starting work."""
        self.lease.check()

    def should_stop(self) -> bool:
        """Sync form for run_launch's should_stop hook."""
        return self.lease.stop_reason() is not None

    async def assert_owner(self, *, allow_cancel: bool = False) -> None:
        """Before an externally visible unit: still ours with lease margin
        (renewed), and not cancelled. Raises — the caller must not send.
        ``allow_cancel``: the finalize path of a cancelled session (its
        JobDiva provisioning) still runs its units."""
        self.lease.check(allow_cancel=allow_cancel)
        try:
            cancel = await self.db(
                self.store.assert_owner, self.fence, lease_s=cfg.lease_s(), margin_s=cfg.assert_owner_margin_s(),
            )
        except OwnershipLost:
            self.lease.lost = True
            raise
        self.lease.renewed(cancel)
        self.lease.check(allow_cancel=allow_cancel)

    async def reload(self) -> Dict[str, Any]:
        """Re-read the session row; raises OwnershipLost if it is not ours."""
        row = await self.db(self.store.get_session, self.sid)
        if (
            row is None or row.get("owner") != self.fence.owner
            or int(row.get("claims") or 0) != self.fence.claims or row.get("status") != "running"
        ):
            self.lease.lost = True
            raise OwnershipLost(self.sid)
        self.session = row
        if row.get("cancel_requested"):
            self.lease.cancel_requested = True
        return row

    # ── fenced writes ───────────────────────────────────────────────────────
    async def update(
        self,
        *,
        values: Optional[Dict[str, Any]] = None,
        coalesce_now: Sequence[str] = (),
        set_now: Sequence[str] = (),
        checkpoint: Optional[Dict[str, Any]] = None,
        counts: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        row = await self.db(
            self.store.update_session, self.fence, values=values, coalesce_now=coalesce_now,
            set_now=set_now, checkpoint=checkpoint, counts=counts,
        )
        self.session = row
        if row.get("cancel_requested"):
            # Every fenced UPDATE returns the flag: act on a cancel at the next
            # boundary instead of the next heartbeat.
            self.lease.cancel_requested = True
        return row

    async def save_ck(self, key: str, value: Any, *, counts: Optional[Dict[str, Any]] = None) -> None:
        await self.update(checkpoint={key: value}, counts=counts)

    async def set_ck_path(self, path: Sequence[str], value: Any) -> None:
        await self.db(self.store.set_checkpoint_path, self.fence, list(path), value)
        node = self.session.setdefault("checkpoint", {})
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value

    async def set_states(self, items: Sequence[Dict[str, Any]], *, from_states: Optional[Sequence[str]] = None) -> List[str]:
        if not items:
            return []
        out: List[str] = []
        for i in range(0, len(items), 200):
            out += await self.db(self.store.set_states, self.fence, list(items[i:i + 200]), from_states=from_states)
        return out

    async def rows(self, states: Optional[Sequence[str]] = None, ids: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        return await self.db(self.store.candidate_rows, self.sid, states=states, ids=ids)

    async def event(self, kind: str, **fields: Any) -> None:
        """Best-effort progress event (fenced: a lost owner writes none)."""
        try:
            await self.db(self.store.append_event, self.sid, ev.payload(kind, **fields), self.fence)
        except Exception as exc:  # noqa: BLE001
            logger.debug("launch_session event failed: %s", exc)

    async def refresh_counts(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Recompute the per-state counts from the rows and merge ``extra``."""
        folded = await self.db(self.store.state_counts, self.sid)
        counts = {"states": folded["states"], "reasons": folded["reasons"]}
        if extra:
            counts.update(extra)
        await self.update(counts=counts)
        return counts
