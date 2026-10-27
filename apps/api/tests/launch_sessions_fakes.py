"""Fakes for the launch-session tests.

``MemoryStore`` is an in-memory twin of ``services.launch_sessions.store.PgStore``
— the same methods, the same conditional / fenced semantics, a controllable
clock — and the store contract suite (test_launch_sessions_store.py) runs the
SAME assertions against both, so the runner tests below it exercise real
store semantics without a database.

``FakeWorld`` stands in for everything outside the session tables:
sourced_candidates / engage_interview_audit rows, a fake Pairbot (interviews,
bulks), the search stream, contact lookups, /candidates/save, and the D1 launch
core (session claim, send claim, bulk_id_for, batches) — with D1's contracts.

``CrashPlan`` raises ``Crash`` (a BaseException: no handler in the runner
catches it — like a SIGKILL) at the n-th instrumented unit: after a store write
committed, after a Pairbot POST was accepted, after an answer was received …
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import hashlib
import itertools
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException

from services.launch_sessions import schema as S
from services.launch_sessions.errors import CancelRequested, OwnershipLost
from services.launch_sessions.store import (
    Claim,
    Fence,
    _CREATE_COLS,
    _SETTABLE,
    _TIMESTAMP_COLS,
    _fold_state_counts,
    finite_score,
    fold_engage_rows,
    jsonable,
)

UTC = dt.timezone.utc


class Crash(BaseException):
    """A simulated process death."""


class CrashPlan:
    def __init__(self, at: Optional[int] = None, only: Optional[Sequence[str]] = None) -> None:
        self.at = at
        self.only = tuple(only or ())
        self.n = 0
        self.log: List[str] = []
        self.fired: Optional[str] = None
        self._lock = threading.Lock()

    def check(self, name: str) -> None:
        if self.only and not name.startswith(self.only):
            return
        with self._lock:
            self.n += 1
            self.log.append(name)
            hit = self.at is not None and self.n == self.at and self.fired is None
            if hit:
                self.fired = name
        if hit:
            raise Crash(name)


def _deep(v: Any) -> Any:
    return copy.deepcopy(v)


_SESSION_DEFAULTS = {
    "jobdiva_ref": None, "requested_by": None, "parent_session_id": None, "trigger": "button",
    "idempotency_key": None, "input_hash": None, "status": "queued", "stage": "created",
    "checkpoint": {}, "counts": {}, "error": None, "attempt": 0, "claims": 0, "owner": None,
    "lease_until": None, "heartbeat_at": None, "not_before": None, "cancel_requested": False,
    "cancel_reason": None, "launch_id": None, "side_effects_fired_at": None, "event_seq": 0,
    "cand_rev": 0, "recheck_at": None, "job_added_at": None, "stage_started_at": None,
    "sourcing_started_at": None, "sourcing_done_at": None, "selection_done_at": None,
    "enrich_done_at": None, "review_started_at": None, "review_done_at": None, "save_started_at": None,
    "save_done_at": None, "launch_started_at": None, "first_interview_created_at": None,
    "last_batch_done_at": None, "first_dial_at": None, "completed_at": None, "human_wait_ms": 0,
}


class MemoryStore:
    """In-memory PgStore (see module docstring)."""

    def __init__(self, crash: Optional[CrashPlan] = None) -> None:
        self.lock = threading.RLock()
        self.t = dt.datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
        self._seq = itertools.count()
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.cands: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self.events: Dict[str, List[Dict[str, Any]]] = {}
        self.stream: Dict[str, Dict[Tuple[int, int], List[Any]]] = {}
        self.jobs: Dict[str, Dict[str, Any]] = {}
        # (job key, candidate_id) -> {"source", "data"}
        self.sourced: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.audit: List[Dict[str, Any]] = []
        self.crash = crash or CrashPlan()

    # ── clock / test helpers ───────────────────────────────────────────────
    def now(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.t += dt.timedelta(seconds=seconds)

    def expire_lease(self, sid: str) -> None:
        with self.lock:
            self.sessions[sid]["lease_until"] = self.t - dt.timedelta(seconds=1)

    def set_column(self, sid: str, column: str, value: Any) -> None:
        with self.lock:
            self.sessions[sid][column] = value

    def _check(self, name: str) -> None:
        self.crash.check(f"store.{name}")

    def _fence(self, fence: Fence, *, bump_rev: bool = False) -> Dict[str, Any]:
        s = self.sessions.get(fence.session_id)
        if (
            s is None or s["owner"] != fence.owner or s["claims"] != fence.claims or s["status"] != "running"
        ):
            raise OwnershipLost(fence.session_id)
        s["updated_at"] = self.t
        if bump_rev:
            s["cand_rev"] += 1
        return s

    # ── reads ──────────────────────────────────────────────────────────────
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return _deep(self.sessions.get(str(session_id)))

    def find_by_key(self, job_id: str, key: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for s in self.sessions.values():
                if s["job_id"] == job_id and s["idempotency_key"] == key:
                    return _deep(s)
            return None

    def active_session(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            act = [s for s in self.sessions.values() if s["job_id"] == job_id and S.holds_job(s["status"], s["stage"])]
            return _deep(max(act, key=lambda s: s["created_at"])) if act else None

    def _claimable(self, s) -> bool:
        return s["status"] == "queued" or (
            s["status"] == "running" and (s["lease_until"] is None or s["lease_until"] < self.t))

    @staticmethod
    def _claim_order(s):
        return (not s["cancel_requested"], s.get("parent_session_id") is None, s["created_at"])

    def queue_position(self, session_id):
        with self.lock:
            me = self.sessions.get(str(session_id))
            if me is None or not self._claimable(me):
                return None
            phase = S.phase(me["mode"], me["stage"])
            return sum(
                1 for s in self.sessions.values()
                if s["id"] != me["id"] and self._claimable(s)
                and (s["not_before"] is None or s["not_before"] <= self.t or s["cancel_requested"])
                and S.phase(s["mode"], s["stage"]) == phase
                and self._claim_order(s) < self._claim_order(me)
            )

    def list_sessions(self, job_id, *, active_only=False, needs_contact_only=False, limit=5):
        with self.lock:
            out = [s for s in self.sessions.values() if s["job_id"] == job_id]
            if active_only:
                out = [s for s in out if s["status"] in S.ACTIVE_STATUSES]
            if needs_contact_only:
                out = [s for s in out if s["status"] in S.TERMINAL_STATUSES and any(
                    c["state"] == S.ROW_NEEDS_CONTACT for c in self.cands.get(s["id"], {}).values())]
            out.sort(key=lambda s: s["created_at"], reverse=True)
            return _deep(out[:limit])

    def list_user_sessions(self, email, *, recent_hours=24, limit=10):
        email = (email or "").strip().lower()
        if not email:
            return []
        with self.lock:
            cutoff = self.t - dt.timedelta(hours=recent_hours)
            out = [dict(s, job_title=(self.jobs.get(s["job_id"]) or {}).get("title"))
                   for s in self.sessions.values()
                   if s.get("requested_by") == email and s["status"] != S.STATUS_COLLECTING
                   and (s["status"] in S.ACTIVE_STATUSES
                        or (s.get("completed_at") or s.get("updated_at") or s["created_at"]) > cutoff)]
            out.sort(key=lambda s: s["created_at"], reverse=True)
            out.sort(key=lambda s: s["status"] in S.ACTIVE_STATUSES, reverse=True)
            return _deep(out[:limit])

    def children(self, session_id):
        with self.lock:
            out = [s for s in self.sessions.values() if s.get("parent_session_id") == session_id]
            out.sort(key=lambda s: s["created_at"])
            return [{"id": s["id"], "trigger": s["trigger"], "status": s["status"], "stage": s["stage"],
                     "created_at": s["created_at"]} for s in out]

    def events_after(self, session_id, after_seq=0, limit=200):
        with self.lock:
            return _deep([e for e in self.events.get(session_id, []) if e["seq"] > after_seq][:limit])

    def events_tail(self, session_id, n=20):
        with self.lock:
            return _deep(self.events.get(session_id, [])[-n:])

    def _sorted_cands(self, sid):
        return sorted(self.cands.get(sid, {}).values(), key=lambda c: (c["ord"], c["candidate_id"]))

    def candidate_rows(self, session_id, *, states=None, ids=None):
        with self.lock:
            rows = self._sorted_cands(session_id)
            if states is not None:
                rows = [c for c in rows if c["state"] in states]
            if ids is not None:
                wanted = {str(i) for i in ids}
                rows = [c for c in rows if c["candidate_id"] in wanted]
            return _deep(rows)

    def candidate_states(self, session_id):
        with self.lock:
            return [{"candidate_id": c["candidate_id"], "state": c["state"], "reason": c["reason"]}
                    for c in self._sorted_cands(session_id)]

    def candidate_ids(self, session_id, states=None):
        return [c["candidate_id"] for c in self.candidate_rows(session_id, states=states)]

    def candidates_page(self, session_id, *, states=None, limit=200, offset=0, after_rev=None, after_id=None,
                        updated_after=None, light=True, ids=None):
        with self.lock:
            rows = self._sorted_cands(session_id)
            if states:
                rows = [c for c in rows if c["state"] in states]
            if ids is not None:
                wanted = {str(i) for i in ids}
                rows = [c for c in rows if c["candidate_id"] in wanted]
            if after_rev is not None:
                rows = [c for c in rows if (c["rev"], c["candidate_id"]) > (after_rev, str(after_id or ""))]
                rows.sort(key=lambda c: (c["rev"], c["candidate_id"]))
            if updated_after is not None:
                rows = [c for c in rows if c["updated_at"] > updated_after]
            total = len(rows)
            page = rows[:limit] if after_rev is not None else rows[offset:offset + limit]
            out = _deep(page)
            if light:
                for c in out:
                    for k in ("resume_text", "resumeText", "linkedin_profile"):
                        c["row"].pop(k, None)
            return out, total

    def state_counts(self, session_id):
        with self.lock:
            agg: Dict[Tuple[str, str], int] = {}
            for c in self.cands.get(session_id, {}).values():
                kind = (c.get("reason") or "").split(":", 1)[0]
                agg[(c["state"], kind)] = agg.get((c["state"], kind), 0) + 1
            return _fold_state_counts({"state": k[0], "kind": k[1], "n": n} for k, n in agg.items())

    def stream_load(self, session_id):
        with self.lock:
            out: Dict[int, List[Any]] = {}
            for (attempt, chunk), evs in sorted(self.stream.get(session_id, {}).items()):
                out.setdefault(attempt, []).extend(_deep(evs))
            return out

    # ── job data ───────────────────────────────────────────────────────────
    def resolve_job(self, job_key):
        with self.lock:
            key = str(job_key or "").strip()
            for j in self.jobs.values():
                if str(j["job_id"]) == key or j.get("jobdiva_id") == key:
                    return _deep(j)
            return None

    def job_first_launch_at(self, job_key):
        job = self.resolve_job(job_key)
        return job.get("first_launch_at") if job else None

    def _audit_iid(self, cid, keys) -> str:
        hits = [a for a in self.audit if a["candidate_id"] == cid and a["jobdiva_id"] in keys and a.get("interview_id")]
        return hits[-1]["interview_id"] if hits else ""

    def job_launch_state(self, job_keys):
        with self.lock:
            keys = [k for k in job_keys if k]
            out = []
            seen = set()
            for (key, cid), r in self.sourced.items():
                if key not in keys:
                    continue
                d = r.get("data") or {}
                seen.add(cid)
                out.append({
                    "candidate_id": cid, "source": r.get("source"),
                    "engage_status": d.get("engage_status") or "", "interview_id": d.get("engage_interview_id") or "",
                    "jobdiva_candidate_id": d.get("jobdiva_candidate_id") or "",
                    "engage_session_id": d.get("engage_session_id") or "",
                    "block_reason": d.get("engage_block_reason") or "",
                    "audit_launched": bool(self._audit_iid(cid, keys)),
                })
            for a in self.audit:
                if a["jobdiva_id"] in keys and a.get("interview_id") and a["candidate_id"] not in seen:
                    seen.add(a["candidate_id"])
                    out.append({"candidate_id": a["candidate_id"], "source": None, "engage_status": "",
                                "interview_id": "", "jobdiva_candidate_id": "", "engage_session_id": "",
                                "block_reason": "", "audit_launched": True})
            return out

    def engage_rows(self, job_keys, candidate_ids):
        with self.lock:
            keys = [k for k in job_keys if k]
            ids = {str(c) for c in candidate_ids}
            rows = []
            for (key, cid), r in self.sourced.items():
                if key in keys and cid in ids:
                    d = r.get("data") or {}
                    rows.append({
                        "candidate_id": cid, "engage_status": (d.get("engage_status") or "").lower(),
                        "interview_id": d.get("engage_interview_id") or "",
                        "engage_session_id": d.get("engage_session_id") or "",
                        "engage_bulk_id": d.get("engage_bulk_id") or "",
                        "block_reason": d.get("engage_block_reason") or "",
                        "audit_interview_id": self._audit_iid(cid, keys) or None,
                    })
            return fold_engage_rows(rows)

    def _session_rows(self, keys, ids, sid, statuses):
        for (key, cid), r in self.sourced.items():
            d = r.setdefault("data", {})
            if key in keys and cid in ids and d.get("engage_session_id") == str(sid) \
                    and d.get("engage_status") in statuses and not d.get("engage_interview_id"):
                yield cid, d

    def requeue_engage_rows(self, job_keys, candidate_ids, session_id):
        with self.lock:
            out = set()
            for cid, d in list(self._session_rows(job_keys, set(candidate_ids), session_id,
                                                  ("processing", "sending", "queued"))):
                d["engage_status"] = "queued"
                out.add(cid)
            self._check("requeue_engage_rows")
            return sorted(out)

    def park_posted_rows(self, job_keys, bulk_by_id, session_id):
        with self.lock:
            out = set()
            for cid, d in list(self._session_rows(job_keys, set(bulk_by_id), session_id, ("queued",))):
                d["engage_status"] = "processing"
                if bulk_by_id.get(cid):
                    d["engage_bulk_id"] = bulk_by_id[cid]
                out.add(cid)
            self._check("park_posted_rows")
            return sorted(out)

    def fail_engage_rows(self, job_keys, candidate_ids, session_id):
        with self.lock:
            out = set()
            for cid, d in list(self._session_rows(job_keys, set(candidate_ids), session_id,
                                                  ("processing", "sending", "queued"))):
                d.pop("engage_session_id", None)
                d.pop("engage_status_before_claim", None)
                d["engage_status"] = "failed"
                out.add(cid)
            self._check("fail_engage_rows")
            return sorted(out)

    # ── creation / browser transitions ─────────────────────────────────────
    def _conflicts(self, fields) -> bool:
        for s in self.sessions.values():
            if s["id"] == fields["id"]:
                return True
            if fields.get("idempotency_key") and s["job_id"] == fields["job_id"] \
                    and s["idempotency_key"] == fields["idempotency_key"]:
                return True
            if s["job_id"] == fields["job_id"] and S.holds_job(s["status"], s["stage"]) \
                    and S.holds_job(fields.get("status", "queued"), fields.get("stage", "created")):
                return True
        return False

    def _insert_rows(self, sid, rows, rev, upsert=True):
        bucket = self.cands.setdefault(sid, {})
        for i, r in enumerate(rows):
            cid = str(r["candidate_id"])
            old = bucket.get(cid)
            rec = {
                "candidate_id": cid, "ord": int(r.get("ord") if r.get("ord") is not None else i),
                "source": r.get("source"), "row": jsonable(r.get("row") or {}),
                "match_score": finite_score(r.get("match_score")), "state": r.get("state") or "pooled",
                "reason": r.get("reason"), "contact": _deep(r.get("contact")), "failure_stage": None,
                "batch_index": None, "interview_id": None, "error": None, "rev": rev, "updated_at": self.t,
            }
            if old is not None:
                if not upsert:
                    continue
                for k in ("failure_stage", "batch_index", "interview_id", "error"):
                    rec[k] = old[k]
                if rec["contact"] is None:
                    rec["contact"] = old["contact"]
            bucket[cid] = rec

    def create_session_in(self, fields, rows, event):
        if self._conflicts(fields):
            return None
        row = dict(_SESSION_DEFAULTS)
        row.update({c: _deep(fields.get(c)) for c in _CREATE_COLS if c in fields})
        for c in ("input", "checkpoint", "counts"):
            row[c] = jsonable(row.get(c) or {})
        row.update({
            "created_at": self.t + dt.timedelta(microseconds=next(self._seq)), "updated_at": self.t,
            "clicked_at": fields.get("clicked_at") or self.t, "human_wait_ms": int(fields.get("human_wait_ms") or 0),
            "cand_rev": 1 if rows else 0, "event_seq": 1 if event else 0,
        })
        self.sessions[row["id"]] = row
        self.cands.setdefault(row["id"], {})
        self.events.setdefault(row["id"], [])
        if rows:
            self._insert_rows(row["id"], rows, 1)
        if event:
            self.events[row["id"]].append({"seq": 1, "payload": jsonable(event), "created_at": self.t})
        return row

    def create_session(self, fields, candidates=(), event=None):
        with self.lock:
            row = self.create_session_in(fields, candidates, event)
            self._check("create_session")
            return _deep(row)

    def add_candidates(self, session_id, rows, *, max_total):
        with self.lock:
            s = self.sessions.get(session_id)
            if s is None or s["status"] != "collecting":
                return {"ok": False, "status": s["status"] if s else None, "total": 0, "too_many": False}
            bucket = self.cands.setdefault(session_id, {})
            new = {str(r["candidate_id"]) for r in rows}
            total = len(bucket) + len(new - set(bucket))
            if total > max_total:
                return {"ok": False, "status": "collecting", "total": total, "too_many": True}
            s["cand_rev"] += 1
            s["updated_at"] = self.t
            start = max([c["ord"] for c in bucket.values()], default=-1) + 1
            self._insert_rows(session_id, [dict(r, ord=start + i) for i, r in enumerate(rows)], s["cand_rev"])
            return {"ok": True, "status": "collecting", "total": total, "too_many": False}

    def start_session(self, session_id):
        with self.lock:
            s = self.sessions.get(session_id)
            if s is None:
                return None, "not_found"
            if s["status"] != "collecting":
                return None, s["status"]
            if not self.cands.get(session_id):
                return None, "empty"
            s["status"] = "queued"
            s["updated_at"] = self.t
            return _deep(s), None

    def cancel_session(self, session_id, reason):
        with self.lock:
            s = self.sessions.get(session_id)
            if s is None:
                return "not_active", None
            if s["status"] in ("collecting", "queued", "awaiting_input"):
                s.update(status="cancelled", completed_at=self.t, updated_at=self.t, error=reason,
                         cancel_reason=reason, owner=None, lease_until=None)
                return "cancelled", _deep(s)
            if s["status"] == "running":
                s["cancel_requested"] = True
                s["cancel_reason"] = s["cancel_reason"] or reason
                s["updated_at"] = self.t
                s["not_before"] = None
                return "requested", _deep(s)
            return "not_active", _deep(s)

    def confirm_review(self, session_id, final_contacts, review):
        with self.lock:
            s = self.sessions.get(session_id)
            if s is None or s["status"] != "awaiting_input":
                return None
            started = s["review_started_at"] or self.t
            s.update(status="queued", review_done_at=self.t, not_before=None, updated_at=self.t)
            s["human_wait_ms"] += max(0, int((self.t - started).total_seconds() * 1000))
            s["checkpoint"] = {**s["checkpoint"], "review": jsonable(review)}
            s["cand_rev"] += 1
            rev = s["cand_rev"]
            self._apply_states(session_id, [
                {"candidate_id": cid, "contact": {"final": vals, "confirmed": True}}
                for cid, vals in final_contacts.items()
            ], rev, from_states=[S.ROW_SELECTED])
            for c in self.cands.get(session_id, {}).values():
                if c["state"] == S.ROW_SELECTED and c["candidate_id"] not in final_contacts:
                    c.update(state=S.ROW_SKIPPED, reason="review_removed", rev=rev, updated_at=self.t)
            return _deep(s)

    def create_child(self, fields, rows, *, parent_id, handed_off, skipped, event=None, parent_event=None):
        with self.lock:
            child = self.create_session_in(fields, rows, event)
            if child is None:
                return None
            parent = self.sessions[parent_id]
            parent["cand_rev"] += 1
            items = [{"candidate_id": c, "state": S.ROW_HANDED_OFF, "reason": f"child:{child['id']}"} for c in handed_off]
            items += [{"candidate_id": c, "state": S.ROW_SKIPPED, "reason": "skipped_by_recruiter"} for c in skipped]
            self._apply_states(parent_id, items, parent["cand_rev"])
            if parent_event:
                self._append(parent_id, parent_event)
            return _deep(child)

    def _append(self, sid, payload):
        s = self.sessions[sid]
        s["event_seq"] += 1
        self.events.setdefault(sid, []).append({"seq": s["event_seq"], "payload": jsonable(payload), "created_at": self.t})
        return s["event_seq"]

    def append_event(self, session_id, payload, fence=None):
        try:
            with self.lock:
                s = self.sessions.get(session_id)
                if s is None:
                    return None
                if fence is not None and (s["owner"] != fence.owner or s["claims"] != fence.claims
                                          or s["status"] != "running"):
                    return None
                return self._append(session_id, payload)
        except Exception:  # noqa: BLE001
            return None

    # ── claim / lease ──────────────────────────────────────────────────────
    def claim(self, owner, limit, *, global_cap, lease_s, session_id=None, search_cap=None):
        if limit <= 0:
            return []
        with self.lock:
            now = self.t

            def take(pred, n):
                if n <= 0:
                    return []
                pick = [
                    s for s in sorted(self.sessions.values(), key=self._claim_order)
                    if self._claimable(s) and (session_id is None or s["id"] == session_id) and pred(s)
                ][:n]
                got = []
                for s in pick:
                    s.update(status="running", owner=owner, lease_until=now + dt.timedelta(seconds=lease_s),
                             heartbeat_at=now, not_before=None, updated_at=now)
                    s["claims"] += 1
                    got.append(Claim(s["id"], s["claims"], s["stage"]))
                return got

            out = take(lambda s: s["cancel_requested"] and (s["not_before"] is None or s["not_before"] <= now), limit)
            left = limit - len(out)
            if left > 0:
                live = [s for s in self.sessions.values()
                        if s["status"] == "running" and s["lease_until"] is not None and s["lease_until"] > now]
                n_phase = {p: sum(1 for s in live if S.phase(s["mode"], s["stage"]) == p)
                           for p in (S.PHASE_SEARCH, S.PHASE_LAUNCH)}

                def waited(s):
                    return not s["cancel_requested"] and (s["not_before"] is None or s["not_before"] <= now)

                if search_cap is None:
                    out += take(waited, min(left, global_cap - len(live)))
                else:
                    for name, free in ((S.PHASE_LAUNCH, global_cap - n_phase[S.PHASE_LAUNCH]),
                                       (S.PHASE_SEARCH, search_cap - n_phase[S.PHASE_SEARCH]),
                                       (S.PHASE_WRAPUP, left)):
                        got = take(lambda s, name=name: waited(s) and S.phase(s["mode"], s["stage"]) == name,
                                   min(left, free))
                        out += got
                        left -= len(got)
                        if left <= 0:
                            break
            self._check("claim")
            return out

    def heartbeat(self, fence, *, lease_s):
        with self.lock:
            s = self._fence(fence)
            s["lease_until"] = self.t + dt.timedelta(seconds=lease_s)
            s["heartbeat_at"] = self.t
            return bool(s["cancel_requested"])

    def assert_owner(self, fence, *, lease_s, margin_s):
        with self.lock:
            s = self.sessions.get(fence.session_id)
            if (s is None or s["owner"] != fence.owner or s["claims"] != fence.claims or s["status"] != "running"
                    or s["lease_until"] is None or s["lease_until"] <= self.t + dt.timedelta(seconds=margin_s)):
                raise OwnershipLost(fence.session_id)
            s["lease_until"] = self.t + dt.timedelta(seconds=lease_s)
            s["heartbeat_at"] = self.t
            self._check("assert_owner")
            return bool(s["cancel_requested"])

    def release(self, fence, *, not_before_s=None, error=None, count_failure=False, cancel_skips_wait=True):
        with self.lock:
            s = self.sessions.get(fence.session_id)
            if s is None or s["owner"] != fence.owner or s["claims"] != fence.claims or s["status"] != "running":
                return None
            # now() of a transaction that committed BEFORE the next claim's.
            s.update(owner=None, lease_until=self.t - dt.timedelta(microseconds=1), heartbeat_at=self.t,
                     updated_at=self.t,
                     not_before=None if not_before_s is None or (cancel_skips_wait and s["cancel_requested"])
                     else self.t + dt.timedelta(seconds=not_before_s))
            s["attempt"] += 1 if count_failure else 0
            if error is not None:
                s["error"] = error
            return s["attempt"]

    # ── fenced writes ──────────────────────────────────────────────────────
    def update_session(self, fence, *, values=None, coalesce_now=(), set_now=(), checkpoint=None, counts=None):
        for col in values or {}:
            if col not in _SETTABLE:
                raise ValueError(f"column {col} is not settable")
        for col in list(coalesce_now) + list(set_now):
            if col not in _TIMESTAMP_COLS:
                raise ValueError(f"column {col} is not a timestamp")
        with self.lock:
            s = self._fence(fence)
            for k, v in (values or {}).items():
                s[k] = jsonable(v) if k == "counts" else v
            for c in coalesce_now:
                if s.get(c) is None:
                    s[c] = self.t
            for c in set_now:
                s[c] = self.t
            if checkpoint:
                s["checkpoint"] = {**s["checkpoint"], **jsonable(checkpoint)}
            if counts:
                s["counts"] = {**s["counts"], **jsonable(counts)}
            self._check("update_session")
            return _deep(s)

    def set_checkpoint_path(self, fence, path, value):
        with self.lock:
            s = self._fence(fence)
            node = s["checkpoint"]
            for k in path[:-1]:
                if not isinstance(node.get(k), dict):
                    return  # jsonb_set does not create missing parents
                node = node[k]
            node[path[-1]] = jsonable(value)
            self._check("set_checkpoint_path")

    def to_awaiting_input(self, fence, checkpoint=None):
        with self.lock:
            s = self._fence(fence)
            if s["cancel_requested"]:
                raise CancelRequested(fence.session_id)
            s.update(status="awaiting_input", owner=None, lease_until=None)
            s["review_started_at"] = s["review_started_at"] or self.t
            s["checkpoint"] = {**s["checkpoint"], **jsonable(checkpoint or {})}
            self._check("to_awaiting_input")
            return _deep(s)

    def finish(self, fence, *, status, error, counts, recheck_in_s=None):
        with self.lock:
            s = self._fence(fence)
            s.update(status=status, error=error, completed_at=self.t, lease_until=None,
                     recheck_at=None if recheck_in_s is None else self.t + dt.timedelta(seconds=recheck_in_s))
            s["counts"] = {**s["counts"], **jsonable(counts or {})}
            if status == "completed":
                s["stage"] = "done"
            self._check("finish")
            return _deep(s)

    def claim_side_effects(self, fence):
        with self.lock:
            s = self._fence(fence)
            if s["side_effects_fired_at"] is not None:
                return False
            s["side_effects_fired_at"] = self.t
            self._check("claim_side_effects")
            return True

    def reset_side_effects(self, session_id):
        with self.lock:
            self.sessions[session_id]["side_effects_fired_at"] = None

    def upsert_rows(self, fence, rows, *, chunk=100):
        for i in range(0, len(rows), max(1, chunk)):
            with self.lock:
                s = self._fence(fence, bump_rev=True)
                self._insert_rows(fence.session_id, rows[i:i + chunk], s["cand_rev"])
                self._check("upsert_rows")
        return len(rows)

    def _apply_states(self, sid, items, rev, from_states=None):
        out = []
        bucket = self.cands.get(sid, {})
        for it in items:
            c = bucket.get(str(it["candidate_id"]))
            if c is None or (from_states is not None and c["state"] not in from_states):
                continue
            if it.get("state") is not None:
                c["state"] = it["state"]
                c["reason"] = it.get("reason")
            if it.get("contact") is not None:
                c["contact"] = {**(c["contact"] or {}), **jsonable(it["contact"])}
            for k in ("failure_stage", "batch_index", "interview_id"):
                if it.get(k) is not None:
                    c[k] = it[k]
            if it.get("error") is not None:
                c["error"] = str(it["error"])[:500]
            if it.get("row") is not None:
                c["row"] = jsonable(it["row"])
            c["rev"] = rev
            c["updated_at"] = self.t
            out.append(c["candidate_id"])
        return out

    def set_states(self, fence, items, *, session_id=None, from_states=None):
        if not items:
            return []
        with self.lock:
            if fence is not None:
                sid = fence.session_id
                rev = self._fence(fence, bump_rev=True)["cand_rev"]
            else:
                sid = str(session_id)
                s = self.sessions.get(sid)
                if s is None:
                    return []
                s["cand_rev"] += 1
                rev = s["cand_rev"]
            out = self._apply_states(sid, items, rev, from_states)
            self._check("set_states")
            return out

    def stream_append(self, fence, attempt, chunk, events):
        with self.lock:
            self._fence(fence)
            self.stream.setdefault(fence.session_id, {})[(attempt, chunk)] = jsonable(list(events))
            self._check("stream_append")

    def s1_flush(self, fence, *, attempt, chunk, events, rows, checkpoint_s1, counts):
        with self.lock:
            s = self._fence(fence, bump_rev=bool(rows))
            if events:
                self.stream.setdefault(fence.session_id, {})[(attempt, chunk)] = jsonable(list(events))
            if rows:
                self._insert_rows(fence.session_id, rows, s["cand_rev"])
            s["checkpoint"] = {**s["checkpoint"], "s1": jsonable(checkpoint_s1)}
            s["counts"] = {**s["counts"], **jsonable(counts)}
            self._check("s1_flush")
            return bool(s["cancel_requested"])

    def stream_delete(self, fence, *, from_attempt=None):
        with self.lock:
            self._fence(fence)
            bucket = self.stream.get(fence.session_id, {})
            for key in list(bucket):
                if from_attempt is None or key[0] >= from_attempt:
                    del bucket[key]
            self._check("stream_delete")

    # ── housekeeping ───────────────────────────────────────────────────────
    def housekeeping(self, *, collecting_ttl_min, review_ttl_h, retention_days, recheck_h, queued_max_s=None):
        with self.lock:
            now = self.t
            out = {"skipped": False, "collecting_expired": [], "review_expired": [], "queue_expired": []}
            for s in self.sessions.values():
                if s["status"] == "collecting" and s["updated_at"] < now - dt.timedelta(minutes=collecting_ttl_min):
                    s.update(status="cancelled", completed_at=now, updated_at=now, error="upload abandoned",
                             cancel_reason="upload abandoned")
                    out["collecting_expired"].append(s["id"])
                started = s["review_started_at"] or s["updated_at"]
                if s["status"] == "awaiting_input" and started < now - dt.timedelta(hours=review_ttl_h):
                    s["human_wait_ms"] += max(0, int((now - started).total_seconds() * 1000))
                    s.update(status="cancelled", completed_at=now, updated_at=now, error="review expired",
                             cancel_reason="review expired")
                    out["review_expired"].append(s["id"])
            for sid in out["collecting_expired"] + out["review_expired"]:
                self._append(sid, {"type": "cancelled", "reason": "expired"})
            if queued_max_s is not None and int(queued_max_s) > 0:
                for s in self.sessions.values():
                    if s["status"] == "queued" and s["claims"] == 0 and s["owner"] is None \
                            and s["updated_at"] < now - dt.timedelta(seconds=int(queued_max_s)):
                        s.update(status="cancelled", completed_at=now, updated_at=now,
                                 error="queue expired: no worker started it in time", cancel_reason="queue_expired")
                        out["queue_expired"].append(s["id"])
                for sid in out["queue_expired"]:
                    self._append(sid, {"type": "cancelled", "reason": "queue_expired"})
            deleted = 0
            for s in self.sessions.values():
                if s["recheck_at"] is not None and s["completed_at"] and s["completed_at"] < now - dt.timedelta(hours=recheck_h):
                    s["recheck_at"] = None
                if s["status"] in S.TERMINAL_STATUSES and s["completed_at"] \
                        and s["completed_at"] < now - dt.timedelta(days=retention_days) and s["recheck_at"] is None:
                    deleted += len(self.cands.get(s["id"], {}))
                    self.cands[s["id"]] = {}
                if s["status"] in S.TERMINAL_STATUSES or s["selection_done_at"] is not None:
                    self.stream.pop(s["id"], None)
            out["rows_deleted"] = deleted
            return out

    def claim_rechecks(self, *, limit, every_s):
        with self.lock:
            due = sorted(
                (s for s in self.sessions.values()
                 if s["recheck_at"] is not None and s["recheck_at"] <= self.t and s["status"] in S.TERMINAL_STATUSES),
                key=lambda s: s["recheck_at"],
            )[:limit]
            for s in due:
                s["recheck_at"] = self.t + dt.timedelta(seconds=every_s)
            return _deep(due)

    def finish_recheck(self, session_id, *, counts, done):
        with self.lock:
            s = self.sessions[session_id]
            s["counts"] = {**s["counts"], **jsonable(counts)}
            if done:
                s["recheck_at"] = None


# ─────────────────────────────────────────────────────────────────────────────
# The world outside the session tables
# ─────────────────────────────────────────────────────────────────────────────

def cand(cid: str, score: Any = 80, *, source: str = "LinkedIn-Exa", **fields: Any) -> Dict[str, Any]:
    """A search row with a usable contact by default."""
    row = {
        "candidate_id": cid, "name": f"Person {cid}", "source": source, "match_score": score,
        "email": f"{cid}@mail.test",
        "phone": f"+1512{int(hashlib.md5(cid.encode()).hexdigest(), 16) % 10 ** 7:07d}",
        "title": "Engineer", "location": "Austin, TX",
    }
    row.update(fields)
    return row


class FakePairbot:
    """Interviews per (job, candidate); a POST creates them unless scripted."""

    def __init__(self) -> None:
        self.interviews: Dict[str, str] = {}  # candidate_id -> interview_id
        self.posts: List[Tuple[str, List[str]]] = []  # (bulk_id, ids)
        self.post_count: Dict[str, int] = {}
        self.creating: Set[str] = set()  # bulk ids still creating
        self.down = False
        self._n = itertools.count(1)

    def create(self, ids: Sequence[str]) -> Dict[str, str]:
        out = {}
        for cid in ids:
            self.interviews.setdefault(cid, f"iv-{cid}-{next(self._n)}")
            out[cid] = self.interviews[cid]
        return out


class FakeWorld:
    """Effects (services.launch_sessions.effects.Effects) over MemoryStore's
    sourced rows and a FakePairbot, with D1's claim / send / reconcile rules."""

    def __init__(self, store: MemoryStore, crash: Optional[CrashPlan] = None) -> None:
        self.store = store
        self.crash = crash or store.crash
        self.pairbot = FakePairbot()
        # search: attempt label -> list of events
        self.search_script: Dict[str, List[Dict[str, Any]]] = {}
        self.search_calls: List[Tuple[str, bool]] = []
        self.search_deferred_contact: List[bool] = []
        self.search_raise_after: Dict[str, int] = {}
        # enrichment: candidate_id -> endpoint answer
        self.enrich_answers: Dict[str, Any] = {}
        self.enrich_drop: Set[str] = set()  # lookups that never answer
        self.enrich_calls: List[str] = []
        self.dnc: Set[str] = set()
        # save
        self.save_calls: List[List[str]] = []
        self.save_script: List[Any] = []  # per call: None | Exception | dict(dnc=…, nc=…, unsaved=…)
        self.saved_rows: Dict[str, Dict[str, Any]] = {}
        # launch
        self.pairbot_mode: Dict[str, str] = {}  # candidate_id -> ok | reject | timeout | processing | crash_after_post
        self.excluded: Dict[str, str] = {}  # candidate_id -> reason (contact gate)
        self.outreach_stop_at_batch: Optional[int] = None
        self.launch_calls: List[Dict[str, Any]] = []
        self.reconcile_calls: List[Dict[str, Any]] = []
        self.provision_calls: List[List[str]] = []
        self.provisioning_on = True
        self.side_effects: List[Dict[str, Any]] = []
        self.side_effects_ok = True
        self.clicked: List[Tuple[str, Any]] = []
        self.first_launch_stamps: List[str] = []
        self.timing_events: List[Dict[str, Any]] = []
        self.registered_runs: List[str] = []
        self.email_gate_error: Optional[HTTPException] = None

    def _check(self, name: str) -> None:
        self.crash.check(f"fx.{name}")

    # ── sourcing ───────────────────────────────────────────────────────────
    def build_search_request(self, payload):
        return dict(payload or {})

    async def search_events(self, request, *, schedule_cross_submissions, defer_paid_contact=False):
        label = str(request.get("boolean_string") or "")
        self.search_calls.append((label, bool(schedule_cross_submissions)))
        self.search_deferred_contact.append(bool(defer_paid_contact))
        raise_after = self.search_raise_after.get(label)
        for n, event in enumerate(self.search_script.get(label, [])):
            if raise_after is not None and n == raise_after:
                self.search_raise_after.pop(label, None)
                raise RuntimeError(f"search {label} died")
            await asyncio.sleep(0)
            self._check("search_event")
            yield copy.deepcopy(event)

    # ── contacts ───────────────────────────────────────────────────────────
    async def enrich(self, items, *, job_id, concurrency):
        for i, it in enumerate(items):
            await asyncio.sleep(0)
            self.enrich_calls.append(it["candidate_id"])
            if it["candidate_id"] in self.enrich_drop:
                continue
            result = copy.deepcopy(self.enrich_answers.get(it["candidate_id"], {"status": "success"}))
            self._check("enrich_answer")
            yield {"index": i, "candidate_id": it["candidate_id"], "result": result}

    def dnc_phones(self):
        return set(self.dnc)

    # ── save ───────────────────────────────────────────────────────────────
    def resolve_user(self, email):
        return SimpleNamespace(email=email, role="recruiter", is_admin=False)

    async def recruiter_email_gate(self, ref):
        if self.email_gate_error is not None:
            raise self.email_gate_error

    async def save(self, ref, rows, user):
        ids = [str(r["candidate_id"]) for r in rows]
        self.save_calls.append(ids)
        script = self.save_script.pop(0) if self.save_script else None
        if isinstance(script, BaseException):
            raise script
        script = script or {}
        dnc = set(script.get("dnc") or ())
        nc = set(script.get("nc") or ())
        unsaved = set(script.get("unsaved") or ())
        saved = []
        with self.store.lock:
            for r in rows:
                cid = str(r["candidate_id"])
                if cid in dnc or cid in nc or cid in unsaved:
                    continue
                key = (ref, cid)
                existing = self.store.sourced.get(key)
                data = dict((existing or {}).get("data") or {})
                self.store.sourced[key] = {"source": r.get("source"), "data": data}
                self.saved_rows[cid] = copy.deepcopy(r)
                saved.append(cid)
        self._check("save")
        return {
            "saved_ids": saved,
            "dnc_skipped": [{"candidate_id": c} for c in ids if c in dnc],
            "no_contact_skipped": [{"candidate_id": c} for c in ids if c in nc],
        }

    # ── launch (D1 contracts) ──────────────────────────────────────────────
    def launch_request(self, **fields):
        return SimpleNamespace(**fields)

    def register_launch_run(self, launch_id, job_id, *, requested_by, session_id):
        self.registered_runs.append(launch_id)

    def _rows(self, keys, cid):
        return [(k, r.setdefault("data", {})) for (k, c), r in self.store.sourced.items() if c == cid and k in keys]

    def _audit_iid(self, keys, cid):
        return self.store._audit_iid(cid, keys)

    async def settle_parked_legacy_rows(self):
        self.legacy_settles = getattr(self, "legacy_settles", 0) + 1
        return {"checked": 0}

    def claim_for_session(self, job_keys, ids, session_id, duplicates=None):
        keys = [k for k in job_keys if k]
        dup_of = dict(getattr(self, "person_duplicates", None) or {})
        if duplicates is not None:
            duplicates.update({c: v for c, v in dup_of.items() if c in {str(i) for i in ids}})
        ids = [i for i in ids if str(i) not in dup_of]
        out = []
        with self.store.lock:
            for cid in dict.fromkeys(str(i) for i in ids):
                rows = self._rows(keys, cid)
                if not rows or self._audit_iid(keys, cid):
                    continue
                ok = all(
                    not d.get("engage_interview_id") and not d.get("engage_block_reason") and (
                        (d.get("engage_status") or "") in ("", "failed")
                        or (d.get("engage_status") == "queued" and d.get("engage_session_id") == session_id))
                    for _, d in rows
                )
                if not ok:
                    continue
                for _, d in rows:
                    if d.get("engage_status") != "queued":
                        d["engage_status_before_claim"] = d.get("engage_status") or ""
                    d["engage_status"] = "queued"
                    d["engage_session_id"] = session_id
                out.append(cid)
        self._check("claim_for_session")
        return out

    def release_claims(self, session_id, job_keys, ids):
        n = 0
        with self.store.lock:
            for (k, cid), r in self.store.sourced.items():
                d = r.setdefault("data", {})
                if k in job_keys and d.get("engage_status") == "queued" and d.get("engage_session_id") == session_id \
                        and (ids is None or cid in ids):
                    d["engage_status"] = d.pop("engage_status_before_claim", "") or ""
                    d.pop("engage_session_id", None)
                    n += 1
        return n

    def _stamp(self, keys, ids, **values):
        with self.store.lock:
            for cid in ids:
                for _, d in self._rows(keys, cid):
                    d.update(values)

    def _record_sent(self, keys, found: Dict[str, str]):
        with self.store.lock:
            for cid, iid in found.items():
                for _, d in self._rows(keys, cid):
                    if not d.get("engage_interview_id"):
                        d.update(engage_status="sent", engage_interview_id=iid)
                if not self._audit_iid(keys, cid):
                    self.store.audit.append({"candidate_id": cid, "jobdiva_id": keys[-1], "interview_id": iid})

    async def run_launch(self, request, *, launch_id=None, run_id=None, side_effects_guard=None,
                         fire_side_effects=True, should_stop=None, provision_inline=True,
                         claimed_session_id=None, bulk_id_for=None, recruiter_email=None):
        self.launch_calls.append({"ids": list(request.candidate_ids), "launch_id": launch_id,
                                  "fire_side_effects": fire_side_effects, "provision_inline": provision_inline,
                                  "recruiter_email": recruiter_email})
        sid = claimed_session_id
        ref = request.job_id
        keys = []
        with self.store.lock:
            for j in self.store.jobs.values():
                if ref in (j["job_id"], j.get("jobdiva_id")):
                    keys = [j["job_id"], j.get("jobdiva_id") or j["job_id"]]
        ids = list(request.candidate_ids)
        size = int(request.batch_size or 75)
        batches = [ids[i:i + size] for i in range(0, len(ids), size)]
        yield {"type": "start", "_internal": {"batches": batches}}
        not_run: List[str] = []
        aborted = False
        for idx, batch in enumerate(batches):
            if aborted or (should_stop is not None and should_stop()):
                not_run.extend(batch)
                continue
            if self.outreach_stop_at_batch == idx:
                aborted = True
                yield {"type": "error", "status_code": 409, "message": "Job activity has been stopped.",
                       "candidate_ids": batch}
                continue
            excluded = [c for c in batch if c in self.excluded]
            sendable = [c for c in batch if c not in self.excluded]
            # send claim: our queued rows → sending
            claimed, skipped = [], []
            with self.store.lock:
                for cid in sendable:
                    rows = self._rows(keys, cid)
                    if rows and all(d.get("engage_status") == "queued" and d.get("engage_session_id") == sid
                                    and not d.get("engage_interview_id") for _, d in rows):
                        for _, d in rows:
                            d["engage_status"] = "sending"
                        claimed.append(cid)
                    else:
                        skipped.append(cid)
            base = {"type": "batch", "index": idx}
            internal = {"candidate_ids": claimed, "sent_ids": [], "no_interview_ids": [], "failed_ids": [],
                        "skipped_ids": skipped, "excluded_ids": excluded, "bulk_id": None}
            if not claimed:
                yield {**base, "status": "completed", "_internal": {**internal, "status": "completed"}}
                continue
            try:
                bulk_id = await bulk_id_for(idx, claimed)
            except Exception as exc:  # noqa: BLE001 — D1: nothing posted, rows back to queued
                self._stamp(keys, claimed, engage_status="queued")
                yield {**base, "status": "failed", "error": str(exc),
                       "_internal": {**internal, "status": "failed", "failed_ids": claimed}}
                continue
            modes = {c: self.pairbot_mode.get(c, "ok") for c in claimed}
            if any(m == "timeout" for m in modes.values()):
                # No answer: D1 releases the session's rows to queued; Pairbot
                # may or may not have the bulk — here it DID accept it.
                self.pairbot.posts.append((bulk_id, list(claimed)))
                for c in claimed:
                    self.pairbot.post_count[c] = self.pairbot.post_count.get(c, 0) + 1
                self.pairbot.create([c for c in claimed if modes[c] == "timeout"])
                self._stamp(keys, claimed, engage_status="queued")
                yield {**base, "status": "failed", "error": "timeout",
                       "_internal": {**internal, "status": "failed", "failed_ids": claimed}}
                continue
            self.pairbot.posts.append((bulk_id, list(claimed)))
            for c in claimed:
                self.pairbot.post_count[c] = self.pairbot.post_count.get(c, 0) + 1
            if any(m == "crash_after_post" for m in modes.values()):
                self.pairbot.create(claimed)
                for c in claimed:
                    self.pairbot_mode.pop(c, None)
                raise Crash("after pairbot post")
            ok = [c for c in claimed if modes[c] == "ok"]
            rejected = [c for c in claimed if modes[c] == "reject"]
            processing = [c for c in claimed if modes[c] == "processing"]
            found = self.pairbot.create(ok)
            self._check("posted")  # Pairbot has them; PAIR has not recorded anything yet
            self._record_sent(keys, found)
            self._stamp(keys, rejected, engage_status="failed")
            if processing:
                self.pairbot.creating.add(bulk_id)
                self._stamp(keys, processing, engage_status="processing", engage_bulk_id=bulk_id)
            self._check("batch_done")
            status = "failed" if processing else "completed"
            yield {**base, "status": status, "sent": len(ok),
                   "_internal": {**internal, "status": status, "sent_ids": ok, "no_interview_ids": rejected,
                                 "failed_ids": processing, "bulk_id": bulk_id if not processing else None}}
        yield {"type": "done", "excluded_candidates": [
            {"candidate_id": c, "name": "", "reason": self.excluded[c]} for c in ids if c in self.excluded],
            "_internal": {"not_run_ids": not_run, "stopped": bool(not_run) and not aborted}}

    async def reconcile(self, job_keys, ids, *, session_id, own_bulk_ids):
        self.reconcile_calls.append({"ids": list(ids), "own": list(own_bulk_ids)})
        self._check("reconcile")
        result = {"sent": {}, "unrecorded": {}, "duplicate_of": {}, "missing": [], "bulk_in_progress": False,
                  "in_progress_bulk_id": None, "own_bulks_creating": [], "error": None}
        if self.pairbot.down:
            result["error"] = "by-job ConnectError"
            return result
        keys = [k for k in job_keys if k]
        found = {c: self.pairbot.interviews[c] for c in ids if c in self.pairbot.interviews}
        self._record_sent(keys, found)
        result["sent"] = found
        creating = set(self.pairbot.creating)
        result["bulk_in_progress"] = bool(creating)
        result["own_bulks_creating"] = sorted(creating & set(own_bulk_ids))
        safe = not creating or (bool(own_bulk_ids) and not (creating & set(own_bulk_ids))
                                and not (creating - set(own_bulk_ids)))
        if safe:
            result["missing"] = [c for c in ids if c not in found]
        return result

    # ── provisioning / side effects / timings ─────────────────────────────
    def provisioning_enabled(self):
        return self.provisioning_on

    async def provision(self, job_id, ids):
        self.provision_calls.append(list(ids))
        self._check("provision")
        return {"requested": len(ids), "claimed": len(ids), "success": len(ids)}

    def fire_side_effects(self, job_id, **kwargs):
        self.side_effects.append({"job_id": job_id, **kwargs})
        return self.side_effects_ok

    def stamp_launch_clicked(self, job_id, at):
        self.clicked.append((job_id, at))

    def stamp_first_launch(self, job_key):
        self.first_launch_stamps.append(job_key)

    def record_timing_event(self, row, *, job_first_launch_at):
        self.timing_events.append({"session_id": row["id"], "status": row["status"]})


def add_job(store: MemoryStore, job_id: str = "1001", ref: str = "26-001", **extra: Any) -> Dict[str, Any]:
    job = {"job_id": job_id, "jobdiva_id": ref, "recruiter_emails": '["r@x.com"]', "outreach_stopped_at": None,
           "pair_added_at": None, "first_launch_at": None, "customer_name": "Acme", "customer": None}
    job.update(extra)
    store.jobs[job_id] = job
    return job


def new_session_fields(job: Dict[str, Any], *, mode: str = "full_search", status: str = "queued",
                       inp: Optional[Dict[str, Any]] = None, key: Optional[str] = None,
                       requested_by: str = "r@x.com", **extra: Any) -> Dict[str, Any]:
    base_input = {"mode": mode, "search_attempts": [], "options": {"notify_recruiters": True,
                  "is_initial_launch": True, "send_job_posting_email": None}, "app_base_url": "https://pairqa.test",
                  "require_review": False, "skip_enrichment": False}
    base_input.update(inp or {})
    fields = {
        "id": str(uuid.uuid4()), "job_id": job["job_id"], "jobdiva_ref": job.get("jobdiva_id"),
        "requested_by": requested_by, "parent_session_id": None, "trigger": "button", "mode": mode,
        "idempotency_key": key or str(uuid.uuid4()), "input_hash": "h", "status": status, "stage": "created",
        "input": base_input, "checkpoint": {}, "counts": {}, "job_added_at": None,
    }
    fields.update(extra)
    return fields


def candidate_event(row: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "candidate", "data": row}


def patch_event(cid: str, patch: Dict[str, Any], stage: Optional[str] = "scored") -> Dict[str, Any]:
    ev = {"type": "candidate_detail", "candidate_id": cid, "patch": patch}
    if stage is not None:
        ev["stage"] = stage
    return ev
