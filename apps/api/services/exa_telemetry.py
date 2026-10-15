"""Exa spend telemetry: one New Relic ``ExaCall`` event per Exa call.

Exa returns ``costDollars`` on every search, contents call and Agent run, and
nothing kept it: spend could not be split by surface, tier or environment,
Pass B's parse failures were paid for unseen, and whether ``/search`` bills
the contents of every result was a guess (docs/exa-cost-optimization.md).

Contract: telemetry is READ-ONLY. No code may branch on anything here, and
every function is a no-op that never raises when New Relic or
EXA_TELEMETRY_ENABLED is off.

``ExaCall`` attributes are ids, enums and numbers only (no names, emails,
phones, URLs, query or response text), filtered by
``core.newrelic.safe_attributes`` (which silently drops keys containing
name / url / text / contact / content / resume ..., hence ``cost_pages`` not
``cost_contents`` and ``rewatch`` not ``resumed``):

- ``surface``: pass_a | dice | deep_analyze | pass_b | contact
- ``endpoint``: search | contents | agent_run | agent_guard (a decision of
  the contact path's 400 guard, not a call)
- ``outcome``: completed | ok | timeout | error | rate_limited | no_slot |
  out_of_credits | breaker_open | create_error | create_rejected | poll_error |
  run_failed | run_cancelled | not_found | abandoned | metadata_rejected |
  sources_refused | rejected | ... (an Agent run's network failure is a
  create_error / poll_error with ``error_kind`` = the exception class)
- cost: ``cost_total``, ``cost_search``, ``cost_pages`` (contents),
  ``cost_compute``, ``cost_fee_e`` / ``cost_fee_p`` (per email / phone found),
  ``cost_fiber`` (Exa Connect data sources), ``acu``
- ``num_results`` (asked) / ``n_results`` (got), ``fields_asked`` /
  ``fields_found``, ``http_status``, ``error_kind``, ``latency_ms``,
  ``exa_run_id``, ``call_id`` (the run's metadata id), ``effort``, ``late``,
  ``cache`` (Pass A / Dice: ``miss`` = the search cache was asked first,
  ``off`` = it is unavailable; a cache hit is not billed and sends no ExaCall)
- who asked, from the request-scoped context below: ``trigger``, ``path``,
  ``attempt``, ``job_id``, ``launch_session_id``, ``tier``, ``arm``, ``defer``,
  ``role`` (a contact run's single-flight role: leader / passthrough /
  adopted; contact_enrichment.exa_step), plus pipeline_trace's ``run_id`` /
  ``job_id``.

Context. A ContextVar carries who asked, so no signature changes (tests fake
``_exa_contact_attempt`` with a fixed signature):

- ``scope(**kw)``: plain coroutines; resets on exit.
- ``bind_if_unbound(**kw)``: async generators (a reset there can run in
  another context when the generator is closed elsewhere); a bare set of the
  keys not already bound.

``ContactLookup`` (one per contact_enrichment.exa_step, per adopted orphan
run, and per on-demand final miss / confirmed re-buy) says what the person's
single-flight lease decided: ``outcome`` leader | follower | passthrough |
in_progress | lease_error | adopted | final_miss | confirm_rebuy
(``record_contact_lookup``). A follower's free polls of another lookup's run
send no ExaCall: the run's owner reports it. ``outcome`` ``lookup`` is one
per on-demand lookup (routers/candidates.py): ``existing_rows``,
``seed_from`` (request | row | -), the booleans ``seeded_email`` /
``has_name`` / ``has_company``, ``normal_asked`` / ``normal_outcome`` /
``deep_asked`` / ``deep_outcome``, and the Launch deep-email holdout's
``deep_skip_eligible`` / ``deep_skip_arm`` (skip | control) /
``deep_skip_pct`` / ``first_exposure``.

``ExaPeopleFanout`` (one per Pass A / Dice fan-out) counts cache hits,
misses and in-flight joins, the per-query size and what the merge kept
(``record_people_fanout``; see exa_service._people_search_fanout). One whose
caller left before the merge carries ``abandoned`` = true and the lookup
counts only (no rows, cost, ``starved`` or ``cf15_same``).

``ExaConfig`` (once per worker per hour, from the Step-5 search) shows the
Exa flags each worker actually runs with, so a hand-set ``.env`` override is
visible.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Dict, Iterator, Mapping, Optional

from core import config as _cfg
from core import newrelic as _nr
from core import pipeline_trace as _pt
from services.exa_errors import ExaNoSlotError, ExaOutOfCreditsError, http_status_of, out_of_credits_tag

logger = logging.getLogger(__name__)

EVENT = "ExaCall"
CONFIG_EVENT = "ExaConfig"
FANOUT_EVENT = "ExaPeopleFanout"
LOOKUP_EVENT = "ContactLookup"
CONFIG_EVERY_S = 3600.0

CONTEXT_KEYS = (
    "trigger", "path", "attempt", "job_id", "launch_session_id",
    "tier", "fields_asked", "arm", "defer", "role",
)
_METADATA_KEYS = ("tier", "path", "trigger", "job_id", "attempt", "arm")
# ContactLookup's boolean flags (record_contact_lookup).
LOOKUP_FLAGS = ("seeded_email", "has_name", "has_company")
_MAX_STR = 64

_EMPTY: Mapping[str, Any] = MappingProxyType({})
_ctx: ContextVar[Mapping[str, Any]] = ContextVar("exa_telemetry_ctx", default=_EMPTY)

# Process latch: Exa rejected run metadata once (see latch_metadata_off).
_metadata_latched = False
# monotonic time ExaConfig was last sent from this worker (None = never).
_config_sent_at: Optional[float] = None
_sdk_version_cache: Optional[str] = None


def enabled() -> bool:
    return bool(getattr(_cfg, "EXA_TELEMETRY_ENABLED", True))


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

def current() -> Dict[str, Any]:
    """A copy of the bound context."""
    return dict(_ctx.get())


def _merged(kw: Dict[str, Any], override: bool) -> Dict[str, Any]:
    cur = _ctx.get()
    new = dict(cur)
    for key, value in kw.items():
        if key not in CONTEXT_KEYS or value is None or value == "":
            continue
        if not override and key in cur:
            continue
        new[key] = value
    return new


@contextmanager
def scope(override: bool = True, **kw: Any) -> Iterator[None]:
    """Bind ``kw`` (CONTEXT_KEYS only; None / "" ignored) for the block.
    ``override=False`` only fills keys nobody bound yet (an outer caller,
    e.g. a launch session, knows better)."""
    token = None
    try:
        token = _ctx.set(_merged(kw, override))
    except Exception:
        pass
    try:
        yield
    finally:
        if token is not None:
            try:
                _ctx.reset(token)
            except ValueError:
                # Exited in another context (a generator closed elsewhere):
                # that context is not ours to touch.
                pass


def bind_if_unbound(**kw: Any) -> None:
    """Bare set of the keys not already bound (for async generators)."""
    try:
        new = _merged(kw, override=False)
        if new != dict(_ctx.get()):
            _ctx.set(new)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Cost parsing (defensive: SDK objects across exa_py versions, or raw JSON)
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _get(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, Mapping):
            if obj.get(name) is not None:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return None


def _sum_leaves(value: Any) -> Optional[float]:
    """A cost part is a number or a breakdown ({text, highlights, summary},
    {neural}, {fiber: ...}): the sum of its numbers."""
    number = _num(value)
    if number is not None:
        return number
    if not isinstance(value, Mapping):
        value = getattr(value, "__dict__", None)
        if not isinstance(value, dict):
            return None
    parts = [p for p in (_sum_leaves(v) for v in value.values()) if p is not None]
    return sum(parts) if parts else None


def _costs(cost: Any, fields: Dict[str, tuple]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if cost is None:
        return out
    for key, names in fields.items():
        value = _sum_leaves(_get(cost, *names))
        if value is not None:
            out[key] = round(value, 6)
    return out


def cost_from_search_response(resp: Any) -> Dict[str, float]:
    """``cost_total`` / ``cost_search`` / ``cost_pages`` of a search or
    contents response (SDK ``cost_dollars``); {} when absent."""
    try:
        return _costs(
            _get(resp, "cost_dollars", "costDollars"),
            {"cost_total": ("total",), "cost_search": ("search",), "cost_pages": ("contents",)},
        )
    except Exception:
        return {}


def cost_from_run_json(run: Any) -> Dict[str, float]:
    """Cost breakdown and agent compute units of an Agent run (raw run JSON or
    an SDK AgentRun); {} when absent. ``acu`` may be fractional (0.4 is what
    broke exa_py 2.13's int field)."""
    try:
        out = _costs(_get(run, "costDollars", "cost_dollars"), {
            "cost_total": ("total",),
            "cost_compute": ("agentCompute", "agent_compute"),
            "cost_search": ("search",),
            "cost_fee_e": ("emails",),
            "cost_fee_p": ("phoneNumbers", "phone_numbers"),
            "cost_fiber": ("dataSources", "data_sources"),
        })
        acu = _num(_get(_get(run, "usage"), "agentComputeUnits", "agent_compute_units"))
        if acu is not None:
            out["acu"] = acu
        return out
    except Exception:
        return {}


def classify_exc(exc: BaseException) -> Dict[str, Any]:
    """``outcome`` / ``http_status`` / ``error_kind`` for a failed call. A
    cancelled call (a Step-5 stream closed mid-search) is ``abandoned``, as on
    the contact path, not an Exa error."""
    if isinstance(exc, asyncio.CancelledError):
        return {"outcome": "abandoned"}
    if isinstance(exc, ExaOutOfCreditsError):
        if exc.skipped:
            return {"outcome": "breaker_open"}
        return {"outcome": "out_of_credits", "http_status": 402, "error_kind": exc.tag or None}
    if isinstance(exc, ExaNoSlotError):
        return {"outcome": "no_slot"}
    if isinstance(exc, asyncio.TimeoutError):
        return {"outcome": "timeout"}
    status = http_status_of(exc)
    if status == 429:
        return {"outcome": "rate_limited", "http_status": 429}
    tag = out_of_credits_tag(exc)
    if tag:
        return {"outcome": "out_of_credits", "http_status": 402, "error_kind": tag}
    return {"outcome": "error", "http_status": status, "error_kind": type(exc).__name__}


def latency_ms(started: float) -> int:
    """Milliseconds since a ``time.monotonic()`` stamp."""
    return int((time.monotonic() - started) * 1000)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def _send(event_type: str, attrs: Dict[str, Any]) -> None:
    """The one place events leave (a seam for tests)."""
    _nr.record_custom_event(event_type, attrs)


def _flat(value: Any) -> Any:
    if isinstance(value, (tuple, list, set, frozenset)):
        return ",".join(str(v) for v in value)
    return value


def _with_context(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """The bound context plus pipeline_trace's run / job, then ``attrs``
    (which win; None values dropped)."""
    payload: Dict[str, Any] = {}
    ctx = _ctx.get()
    for key in CONTEXT_KEYS:
        if ctx.get(key) is not None:
            payload[key] = ctx[key]
    if payload.get("job_id") is None:
        job = _pt.job_id.get()
        if job:
            payload["job_id"] = job
    run = _pt.run_id.get()
    if run:
        payload["run_id"] = run
    payload.update({k: v for k, v in attrs.items() if v is not None})
    return payload


def record_exa_call(surface: str, endpoint: str, outcome: str, **attrs: Any) -> None:
    """One ``ExaCall`` event (see the module docstring for the attributes).
    ``attrs`` beat the bound context; None values are dropped. Never raises."""
    if not enabled():
        return
    try:
        payload = _with_context(attrs)
        payload.update(surface=surface, endpoint=endpoint, outcome=outcome)
        _send(EVENT, _nr.safe_attributes({k: _flat(v) for k, v in payload.items()}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("ExaCall not recorded: %s", exc)


def record_people_fanout(surface: str, **attrs: Any) -> None:
    """One ``ExaPeopleFanout`` event per Pass A / Dice fan-out (counts and
    flags only, see exa_service._people_search_fanout), with the same who-
    asked context as ExaCall. Cache hits are counted here, never as ExaCall
    (they are not billed). Never raises."""
    if not enabled():
        return
    try:
        payload = _with_context(attrs)
        payload["surface"] = surface
        _send(FANOUT_EVENT, _nr.safe_attributes({k: _flat(v) for k, v in payload.items()}))
    except Exception as exc:  # noqa: BLE001
        logger.debug("ExaPeopleFanout not recorded: %s", exc)


def record_contact_lookup(outcome: str, *, flags: Optional[Mapping[str, Any]] = None, **attrs: Any) -> None:
    """One ``ContactLookup`` event: what a contact step's single-flight lease
    (or the final-miss rule) decided, or one on-demand lookup's summary (see
    the module docstring), with the who-asked context. Ids, enums and numbers
    only. ``flags``: booleans among LOOKUP_FLAGS, sent as booleans past the
    PII key filter (their names hold "email" / "name"; a bool carries
    nothing). Never raises."""
    if not enabled():
        return
    try:
        payload = _with_context(attrs)
        payload["outcome"] = outcome
        out = _nr.safe_attributes({k: _flat(v) for k, v in payload.items()})
        out.update({k: bool(v) for k, v in (flags or {}).items() if k in LOOKUP_FLAGS})
        _send(LOOKUP_EVENT, out)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ContactLookup not recorded: %s", exc)


def _sdk_version() -> str:
    global _sdk_version_cache
    if _sdk_version_cache is None:
        try:
            from importlib.metadata import version

            _sdk_version_cache = version("exa_py")
        except Exception:
            _sdk_version_cache = "unknown"
    return _sdk_version_cache


def record_config_once() -> None:
    """``ExaConfig`` from this worker, at most once per CONFIG_EVERY_S. It
    counts as sent only once New Relic is on and the record call returned,
    so a worker whose agent connects late still reports."""
    global _config_sent_at
    if not enabled():
        return
    now = time.monotonic()
    if _config_sent_at is not None and now - _config_sent_at < CONFIG_EVERY_S:
        return
    if not _nr.is_enabled():
        return
    try:
        from core import sourcing_config as _sc
        from core.vendor_limiter import EXA
        from services import exa_search_cache

        _send(CONFIG_EVENT, _nr.safe_attributes({
            "per_query_factor": float(getattr(_sc, "EXA_PEOPLE_PER_QUERY_FACTOR", 0.0) or 0.0),
            "search_cache_ttl_s": exa_search_cache.ttl_s(),
            "search_cache": exa_search_cache.state(),
            "pass_b": bool(getattr(_sc, "EXA_AGENT_ENABLED", False)),
            "deep_analysis": bool(_sc.exa_deep_analysis_enabled()),
            "metadata_on": run_metadata_enabled(),
            "sdk_version": _sdk_version(),
            "breaker_ttl_s": float(EXA.no_credits_ttl_s),
            "normal_effort": str(getattr(_cfg, "EXA_CONTACT_ENRICH_EFFORT", "") or ""),
            "deep_effort": str(getattr(_cfg, "EXA_CONTACT_DEEP_EFFORT", "") or ""),
            "deep_skip_pct": int(getattr(_sc, "EXA_DEEP_SKIP_REACHABLE_PCT", 0) or 0),
            "defer_paid": bool(getattr(_sc, "SOURCING_DEFER_PAID_CONTACT_ENABLED", True)),
            "worker_pid": os.getpid(),
        }))
        _config_sent_at = now
    except Exception as exc:  # noqa: BLE001
        logger.debug("ExaConfig not recorded: %s", exc)


# ---------------------------------------------------------------------------
# Agent run metadata
# ---------------------------------------------------------------------------

def run_metadata_enabled() -> bool:
    """EXA_RUN_METADATA_ENABLED, unless Exa rejected it in this process."""
    return bool(getattr(_cfg, "EXA_RUN_METADATA_ENABLED", True)) and not _metadata_latched


def agent_metadata(surface: str, **extra: Any) -> Dict[str, str]:
    """``metadata`` for an Agent run create: Exa types it Dict[str, str], so
    every value is a short string (None / "" dropped), plus a fresh
    ``call_id`` that joins the run list to its ExaCall events."""
    meta: Dict[str, str] = {}
    try:
        ctx = _ctx.get()
        values: Dict[str, Any] = {"env": _nr.environment(), "surface": surface}
        for key in _METADATA_KEYS:
            values[key] = extra[key] if key in extra else ctx.get(key)
        if values.get("job_id") in (None, ""):
            values["job_id"] = _pt.job_id.get()
        for key, value in values.items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            text = str(value).strip()[:_MAX_STR]
            if text:
                meta[key] = text
    except Exception:
        pass
    meta["call_id"] = uuid.uuid4().hex
    return meta


def with_fresh_call_id(body: Dict[str, Any]) -> Dict[str, Any]:
    """``body`` for a NEW run of the same lookup: a new ``call_id`` (one per
    billed run), else unchanged."""
    meta = body.get("metadata")
    if not isinstance(meta, dict):
        return body
    return {**body, "metadata": {**meta, "call_id": uuid.uuid4().hex}}


def latch_metadata_off(detail: str = "") -> bool:
    """Exa rejected run metadata: never send it again in this process. True
    the first time (logged at ERROR once per worker)."""
    global _metadata_latched
    if _metadata_latched:
        return False
    _metadata_latched = True
    logger.error(
        "Exa rejected run metadata (400): sending runs without it for the rest of "
        "this worker's life; set EXA_RUN_METADATA_ENABLED=false. Detail: %s",
        (detail or "")[:200],
    )
    return True


def reset_for_tests() -> None:
    global _metadata_latched, _config_sent_at
    _metadata_latched = False
    _config_sent_at = None
