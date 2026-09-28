"""Kipplo contact lookup by LinkedIn URL: the FIRST provider in every contact chain.

User 2026-09-28: "Kipplo can be now the first source", and the chains go
cheapest to most expensive. At $0.011-0.0135 a credit on the paid plans
(~1.1-1.4c per email, ~5.5-6.8c per mobile, misses free) Kipplo was ranked
cheapest, ahead of ZoomInfo/Apollo credits; Exa (~2c email + 7c phone + agent
compute) stays last. So it sits before ZoomInfo, Apollo and Exa in both chains
(services/contact_enrichment.enrich_contact_for_sourcing and
routers/candidates._enrich_candidate_contact_impl), right after the free
person-level contact cache, and is asked only for the fields a candidate still
lacks. The spend policy (contact_lookup_block_reason) is applied by the callers.

API (probed 2026-09-28 with our key; one "integration" = one saved query):

    POST {KIPPLO_EXECUTE_URL}          header X-API-Key (Bearer is rejected)
    {"params": {"filters": [{"field": "linkedin_url", "operator": "Equals", "value": <url>}],
                "requested_optional_fields": ["business_emails", "secondary_emails", "cell_numbers"]}}

  Answer: an Elasticsearch hit list. 0 or 1 hit per profile; each ``_source`` has
  ``linkedinurl`` ("linkedin.com/in/<slug>"), ``businessemails`` [{email, status}],
  ``secondaryemails`` [{email}] and ``cellnumbers`` [{phone, phone_cleaned, type}].
  ``phone`` is formatted with its country code ("+1 469-555-0100"), ``phone_cleaned``
  is the same digits without "+".

Billing: a miss is free; each field GROUP it finds is charged: 1 credit for business
emails, 1 for secondary emails, 5 for cell numbers (``pricing_breakdown``). So we
request only the groups for the fields the candidate lacks.

Failures, all fail-open (the chain moves on to the next provider):
  * out of credits: HTTP 200 with ``success: false``, ``error_code:
    INSUFFICIENT_CREDITS``. Trips a cool-down (logged at ERROR), like Apollo's.
  * bad key: 401 ``API_KEY_INVALID``; same cool-down.
  * rate limit: 429 ``RATE_LIMIT_EXCEEDED`` with ``detail.rate_limits`` per window.
    Our key's limits were 1/second, 10/minute, 10,000/day and 100,000/month, set
    per API key in the Kipplo portal. The API runs 8 uvicorn workers, so calls are
    paced by a limiter shared through Postgres (``kipplo_rate_calls``), with an
    in-process limiter as the fallback when the DB is unavailable. The limits count
    requests, so lookups waiting at the same time share one request (up to
    KIPPLO_BATCH_SIZE profiles; see "Lookup" below).

Accuracy check (2026-09-28, 34 JobDiva applicants with candidate-provided contact):
every record was for exactly the LinkedIn profile asked for; 4 of 9 cell numbers
equalled the candidate's own number, the other 5 were different numbers; contact
found for 11 of 34.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import weakref
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from services.contact_cache import linkedin_slug

logger = logging.getLogger(__name__)

DEFAULT_EXECUTE_URL = (
    "https://api.kipplo.com/api/v1/customer/integrations/"
    "eb0bdfee-7ed9-4603-bb5d-a2c5869073cf/execute"
)

CONTACT_FIELDS = ("email", "phone")
# Optional field groups bought for each contact field we lack.
FIELD_GROUPS = {
    "email": ("business_emails", "secondary_emails"),
    "phone": ("cell_numbers",),
}
_BAD_EMAIL_STATUSES = {"invalid", "bounced", "undeliverable", "do_not_mail", "spamtrap", "abuse"}

UNAVAILABLE_COOLDOWN_S = 600.0


# Clock and sleep, indirected so tests can run the limiter on a fake clock.
def _clock() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Configuration (read at call time, like services/apollo_phone.py)
# ---------------------------------------------------------------------------

def _truthy(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def api_key() -> str:
    return (os.getenv("KIPPLO_API_KEY") or "").strip()


def enabled() -> bool:
    """KIPPLO_CONTACT_ENRICH_ENABLED (default on) and a key configured."""
    return _truthy("KIPPLO_CONTACT_ENRICH_ENABLED", "true") and bool(api_key())


def supported_fields() -> Tuple[str, ...]:
    """Contact fields Kipplo is asked for (KIPPLO_CONTACT_FIELDS, default "phone").

    Phone only since 2026-09-28 (user: "we only need personal phone number,
    this is returning work info"): its email groups are mostly business
    (work) addresses, while its cell numbers are the personal mobiles the
    other providers rarely have. Emails come from the rest of the chain."""
    raw = os.getenv("KIPPLO_CONTACT_FIELDS", "phone")
    return tuple(f for f in CONTACT_FIELDS if f in {p.strip().lower() for p in raw.split(",")})


def execute_url() -> str:
    return (os.getenv("KIPPLO_EXECUTE_URL") or DEFAULT_EXECUTE_URL).strip()


def timeout_s() -> float:
    return max(1.0, _float_env("KIPPLO_TIMEOUT_S", 15.0))


def rate_per_minute() -> int:
    """Keep in step with the key's per-minute limit in the Kipplo portal."""
    return max(1, int(_float_env("KIPPLO_RATE_LIMIT_PER_MINUTE", 10)))


def min_interval_s() -> float:
    """Spacing between calls, from the key's per-second limit."""
    return 1.0 / max(0.01, _float_env("KIPPLO_RATE_LIMIT_PER_SECOND", 1.0))


def max_wait_s() -> float:
    """How long one lookup may wait for a rate-limit slot before the chain moves
    on without Kipplo."""
    return max(0.0, _float_env("KIPPLO_MAX_WAIT_S", 20.0))


# ---------------------------------------------------------------------------
# Out-of-credits / bad-key cool-down, and 429 back-off (per worker)
# ---------------------------------------------------------------------------

_unavailable_until = 0.0
_unavailable_reason = ""
_rate_blocked_until = 0.0


def unavailable() -> str:
    """Why Kipplo is being skipped right now ("" when it may be called)."""
    return _unavailable_reason if _clock() < _unavailable_until else ""


def _trip_unavailable(reason: str, fix: str, seconds: float = UNAVAILABLE_COOLDOWN_S) -> None:
    global _unavailable_until, _unavailable_reason
    _unavailable_until = _clock() + seconds
    _unavailable_reason = reason
    logger.error(
        "Kipplo %s: skipping Kipplo contact lookups for %ds (the chain goes on to "
        "ZoomInfo/Apollo/Exa); %s",
        reason, int(seconds), fix,
    )


def _note_rate_limited(payload: Any) -> None:
    """Back off after a 429, by the window Kipplo says is exhausted."""
    global _rate_blocked_until
    detail = payload.get("detail") if isinstance(payload, dict) else None
    limits = detail.get("rate_limits") if isinstance(detail, dict) else None
    exhausted = set()
    if isinstance(limits, dict):
        for window, info in limits.items():
            if isinstance(info, dict) and info.get("remaining") == 0:
                exhausted.add(str(window))
    if exhausted & {"day", "month"}:
        _trip_unavailable(
            f"{'/'.join(sorted(exhausted & {'day', 'month'}))} rate limit reached",
            "raise the key's limits in the Kipplo portal", 3600.0,
        )
        return
    backoff = 15.0 if "minute" in exhausted or not exhausted else 1.0
    _rate_blocked_until = max(_rate_blocked_until, _clock() + backoff)
    logger.info("Kipplo rate limited (%s): backing off %.0fs", ",".join(sorted(exhausted)) or "?", backoff)


# ---------------------------------------------------------------------------
# Rate limiter shared by the workers (Postgres), in-process fallback
# ---------------------------------------------------------------------------

CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS kipplo_rate_calls (
        called_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
    )
"""

_WINDOW_SQL = """
    SELECT count(*),
           EXTRACT(EPOCH FROM clock_timestamp() - max(called_at)),
           EXTRACT(EPOCH FROM min(called_at) + interval '60 seconds' - clock_timestamp())
    FROM kipplo_rate_calls
    WHERE called_at > clock_timestamp() - interval '60 seconds'
"""

_table_ready = False
_local_calls: Deque[float] = deque()
_db_limiter_warned = False


def _connect():
    # Looked up at call time: tests block real connections at core.db.
    from core import db as _db
    return _db.get_db_connection()


def _slot_wait(in_window: int, since_last: Any, oldest_frees_in: Any) -> float:
    """Seconds until the next call is allowed (<= 0: now)."""
    wait = 0.0
    if since_last is not None and float(since_last) < min_interval_s():
        wait = min_interval_s() - float(since_last)
    if in_window >= rate_per_minute() and oldest_frees_in is not None:
        wait = max(wait, float(oldest_frees_in))
    return wait


def _claim_slot_db() -> float:
    """Claim a call slot across all workers; 0.0 when claimed, else seconds to wait."""
    global _table_ready
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if not _table_ready:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('kipplo_rate_calls_ddl'))")
                cur.execute(CREATE_SQL)
                conn.commit()
                _table_ready = True
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('kipplo_rate_calls'))")
            cur.execute("DELETE FROM kipplo_rate_calls WHERE called_at < clock_timestamp() - interval '5 minutes'")
            cur.execute(_WINDOW_SQL)
            in_window, since_last, oldest_frees_in = cur.fetchone()
            wait = _slot_wait(int(in_window or 0), since_last, oldest_frees_in)
            if wait <= 0:
                cur.execute("INSERT INTO kipplo_rate_calls DEFAULT VALUES")
        conn.commit()
    finally:
        conn.close()
    return max(0.0, wait)


def _claim_slot_local() -> float:
    now = _clock()
    while _local_calls and now - _local_calls[0] >= 60.0:
        _local_calls.popleft()
    wait = _slot_wait(
        len(_local_calls),
        (now - _local_calls[-1]) if _local_calls else None,
        (60.0 - (now - _local_calls[0])) if _local_calls else None,
    )
    if wait <= 0:
        _local_calls.append(now)
    return max(0.0, wait)


async def _claim_slot() -> float:
    global _db_limiter_warned
    try:
        return await asyncio.to_thread(_claim_slot_db)
    except Exception as e:
        if not _db_limiter_warned:
            logger.warning("Kipplo shared rate limiter unavailable (%s); pacing this worker only", e)
            _db_limiter_warned = True
        return _claim_slot_local()


async def _acquire_slot(deadline: float) -> bool:
    """Wait (until ``deadline``, monotonic) for a call slot."""
    while True:
        blocked = _rate_blocked_until - _clock()
        if blocked > 0:
            if _clock() + blocked > deadline:
                return False
            await _sleep(blocked)
            continue
        wait = await _claim_slot()
        if wait <= 0:
            return True
        if _clock() + wait > deadline:
            return False
        await _sleep(wait + random.uniform(0.0, 0.25))


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _normalise_phone(raw: Any) -> str:
    raw = str(raw or "").strip()
    plus = "+" if raw.startswith("+") else ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"{plus}{digits}" if digits else ""


def _emails(items: Any) -> List[str]:
    out: List[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()
        if "@" not in email or status in _BAD_EMAIL_STATUSES or email in out:
            continue
        out.append(email)
    return out


_CELL_TYPES = {"cellphone", "cell", "mobile", "mobile_phone", "cell_phone"}


def _phones(items: Any) -> List[str]:
    """Personal mobiles only: a number typed as anything but a cell phone
    (work, landline, ...) is skipped."""
    out: List[str] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("type") or "").strip().lower()
        if ptype and ptype not in _CELL_TYPES:
            continue
        phone = _normalise_phone(item.get("phone"))
        if not phone.startswith("+"):
            # phone_cleaned carries the country code without "+" (11+ digits).
            cleaned = _normalise_phone(item.get("phone_cleaned"))
            phone = f"+{cleaned}" if len(cleaned) >= 11 else (phone or cleaned)
        if sum(ch.isdigit() for ch in phone) < 7 or phone in out:
            continue
        out.append(phone)
    return out


def extract_contact_fields(payload: Any, slug: str = "") -> Dict[str, Any]:
    """Kipplo answer -> the canonical contact shape the chains merge
    (``extract_apollo_contact_fields`` / ``extract_exa_contact_fields``).

    Only a hit for the profile we asked for is used: with ``slug`` given, a hit
    whose ``linkedinurl`` is another profile is ignored. Business emails become
    ``workEmail`` and secondary emails ``personalEmail``; cell numbers are mobiles.
    """
    result = payload.get("result") if isinstance(payload, dict) else None
    hits = ((result or {}).get("hits") or {}).get("hits") if isinstance(result, dict) else None
    source: Dict[str, Any] = {}
    for hit in hits if isinstance(hits, list) else []:
        src = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(src, dict):
            continue
        if slug and linkedin_slug(src.get("linkedinurl")) != slug:
            logger.warning(
                "Kipplo returned a different LinkedIn profile for %s: ignoring it", slug,
            )
            continue
        source = src
        break
    return _contact_from_source(source)


def _contact_from_source(source: Dict[str, Any]) -> Dict[str, Any]:
    """One Kipplo profile record (a hit's ``_source``) -> the contact shape."""
    business = _emails(source.get("businessemails"))
    secondary = [e for e in _emails(source.get("secondaryemails")) if e not in business]
    phones = _phones(source.get("cellnumbers"))
    return {
        "mobilePhone": phones[0] if phones else "",
        "workPhone": "",
        "workEmail": business[0] if business else "",
        "personalEmail": secondary[0] if secondary else "",
        "phoneCandidates": phones,
    }


# ---------------------------------------------------------------------------
# Lookup: one request carries up to KIPPLO_BATCH_SIZE profiles
# ---------------------------------------------------------------------------
#
# The key's limits count REQUESTS, and the `Equals` filter accepts a list of
# URLs (probed 2026-09-28: a list passes Kipplo's filter validation, which runs
# before its credit check). So lookups that are waiting at the same time go out
# as one request, grouped by the fields they need (nobody is billed for a group
# they did not ask for). A batch forms while the previous one is in flight or
# waiting for a rate-limit slot, i.e. exactly when the limit is the bottleneck.
#
# What a list RETURNS could not be checked live (the account had no credits),
# so a worker does not trust it until proven: after a batch it re-asks, alone,
# one profile the batch came back without. If that single lookup knows the
# profile, list lookups are switched off for the worker (ERROR logged) and the
# rest are looked up one at a time; if not, lists are trusted from then on. A
# rejected list filter, a cut-off answer (more matches than hits) or hits for
# profiles nobody asked about fall back the same way.

RATE_LIMITED = {"ok": False, "message": "Kipplo rate limited", "rate_limited": True}


def batch_size() -> int:
    """Profiles per request (KIPPLO_BATCH_SIZE, default 10; 1 = no batching)."""
    return max(1, int(_float_env("KIPPLO_BATCH_SIZE", 10)))


def batch_window_s() -> float:
    """How long the first lookup of a burst waits for others to join."""
    return max(0.0, _float_env("KIPPLO_BATCH_WINDOW_S", 0.5))


_batch_off_reason = ""   # list lookups proved unusable in this worker
_batch_verified = False  # a single lookup confirmed a batch's miss
_batch_cap = 0           # learned from a cut-off answer (0 = none)


def _batching_off(reason: str) -> None:
    global _batch_off_reason
    if not _batch_off_reason:
        _batch_off_reason = reason
        logger.error(
            "Kipplo list lookups switched off for this worker: %s; looking profiles up one at a time",
            reason,
        )


def _effective_batch_size() -> int:
    if _batch_off_reason:
        return 1
    return min(batch_size(), _batch_cap) if _batch_cap else batch_size()


def request_body(slugs: List[str], fields: Tuple[str, ...]) -> Dict[str, Any]:
    urls = [f"https://www.linkedin.com/in/{quote(s, safe='-_.~')}" for s in slugs]
    return {
        "params": {
            "filters": [{
                "field": "linkedin_url",
                "operator": "Equals",
                "value": urls[0] if len(urls) == 1 else urls,
            }],
            "requested_optional_fields": [g for f in fields for g in FIELD_GROUPS[f]],
        }
    }


def _credits(data: Dict[str, Any]) -> float:
    try:
        return float(data.get("credits_charged") or 0)
    except (TypeError, ValueError):
        return 0.0


def _answered(data: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], int, int]:
    """The profile records in an answer by slug, how many hits came back and
    how many matched in total."""
    result = data.get("result") if isinstance(data, dict) else None
    hits_obj = result.get("hits") if isinstance(result, dict) else None
    hits = hits_obj.get("hits") if isinstance(hits_obj, dict) else None
    hits = [h for h in hits if isinstance(h, dict)] if isinstance(hits, list) else []
    sources: Dict[str, Dict[str, Any]] = {}
    for hit in hits:
        src = hit.get("_source")
        slug = linkedin_slug(src.get("linkedinurl")) if isinstance(src, dict) else ""
        if slug:
            sources.setdefault(slug, src)
    total = (hits_obj.get("total") or {}).get("value") if isinstance(hits_obj, dict) else None
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = len(hits)
    return sources, len(hits), total


async def _post(body: Dict[str, Any], deadline: float, label: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """One request, paced by the shared limiter (a 429 is retried within the
    wait budget). Returns ``(answer, {})`` or ``(None, failure result)``."""
    headers = {"X-API-Key": api_key(), "Content-Type": "application/json", "Accept": "application/json"}
    res = None
    try:
        async with httpx.AsyncClient(timeout=timeout_s()) as client:
            for _attempt in range(3):
                if not await _acquire_slot(deadline):
                    logger.info("Kipplo: no rate-limit slot within %.0fs for %s", max_wait_s(), label)
                    return None, dict(RATE_LIMITED)
                res = await client.post(execute_url(), headers=headers, json=body)
                if res.status_code != 429:
                    break
                try:
                    _note_rate_limited(res.json())
                except ValueError:
                    _note_rate_limited({})
                if unavailable():
                    return None, {"ok": False, "message": f"Kipplo unavailable: {unavailable()}", "rate_limited": True}
            else:
                return None, dict(RATE_LIMITED)
    except Exception as e:
        logger.warning("Kipplo request failed for %s: %s", label, e)
        return None, {"ok": False, "message": f"Kipplo request failed: {e}"}

    try:
        data = res.json()
    except ValueError:
        data = {}
    detail = data.get("detail") if isinstance(data, dict) else None
    error_code = str(
        (data.get("error_code") if isinstance(data, dict) else "")
        or (detail.get("error_code") if isinstance(detail, dict) else "")
        or ""
    ).upper()
    if res.status_code == 401 or error_code.startswith("API_KEY"):
        _trip_unavailable(f"rejected the API key ({error_code or res.status_code})", "fix KIPPLO_API_KEY")
        return None, {"ok": False, "message": "Kipplo API key rejected"}
    if "CREDIT" in error_code or res.status_code == 402:
        _trip_unavailable("is out of credits", "top up the Kipplo account")
        return None, {"ok": False, "message": "Kipplo out of credits"}
    if res.status_code >= 400 or not isinstance(data, dict) or data.get("success") is False:
        error = str(data.get("error") or "") if isinstance(data, dict) else ""
        logger.warning("Kipplo error for %s: HTTP %s %s", label, res.status_code, (error or res.text)[:200])
        failure: Dict[str, Any] = {"ok": False, "message": f"Kipplo error ({error_code or res.status_code})"}
        if error.lower().startswith("invalid filters"):
            failure["invalid_filters"] = True
        return None, failure
    return data, {}


async def _lookup_many(slugs: List[str], fields: Tuple[str, ...], deadline: float, label: str) -> Dict[str, Dict[str, Any]]:
    """Look ``slugs`` up, one request per batch. A result per slug: ``ok`` means
    Kipplo answered for that profile (possibly with nothing); ``known`` whether
    it has the profile at all."""
    global _batch_verified, _batch_cap
    size = _effective_batch_size()
    if len(slugs) > size:
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(slugs), size):
            out.update(await _lookup_many(slugs[i:i + size], fields, deadline, label))
        return out

    data, failure = await _post(request_body(slugs, fields), deadline, label)
    if failure:
        if len(slugs) > 1 and failure.get("invalid_filters"):
            _batching_off(f"Kipplo rejected a list filter ({failure['message']})")
            return await _lookup_many(slugs, fields, deadline, label)
        return {s: failure for s in slugs}
    sources, n_hits, total = _answered(data)
    credits = _credits(data)

    if len(slugs) == 1:
        slug = slugs[0]
        extracted = extract_contact_fields(data, slug)
        found = [k for k in ("workEmail", "personalEmail", "mobilePhone") if extracted.get(k)]
        logger.info(
            "Kipplo for %s: asked %s, found %s (%g credits)",
            label, ",".join(fields), ",".join(found) or "nothing", credits,
        )
        return {slug: {"ok": True, "fields": extracted, "credits": credits, "known": slug in sources}}

    asked = set(slugs)
    foreign = [s for s in sources if s not in asked]
    if foreign:
        _batching_off(f"a list lookup returned {len(foreign)} profile(s) nobody asked for")
    out = {
        s: {"ok": True, "fields": _contact_from_source(sources[s]), "known": True}
        for s in slugs if s in sources
    }
    missing = [s for s in slugs if s not in out]
    if missing and total > n_hits:
        # More matches than hits: the answer was cut off, so the profiles
        # without a hit are unknown, not misses.
        _batch_cap = max(1, n_hits)
        logger.info("Kipplo list answer cut off (%d of %d): batches capped at %d", n_hits, total, _batch_cap)
        out.update(await _lookup_many(missing, fields, deadline, label))
        missing = []
    elif missing and not _batch_verified and not _batch_off_reason:
        probe = missing.pop(0)
        single = (await _lookup_many([probe], fields, deadline, label))[probe]
        out[probe] = single
        if single.get("ok") and single.get("known"):
            _batching_off("a single lookup found a profile its list lookup missed")
            out.update(await _lookup_many(missing, fields, deadline, label))
            missing = []
        elif single.get("ok"):
            _batch_verified = True
    for s in missing:
        out[s] = {"ok": True, "fields": _contact_from_source({}), "known": False}
    logger.info(
        "Kipplo batch of %d (%s): %d known, %d with contact (%g credits)",
        len(slugs), ",".join(fields), sum(1 for r in out.values() if r.get("known")),
        sum(1 for r in out.values() if any((r.get("fields") or {}).get(k) for k in ("workEmail", "personalEmail", "mobilePhone"))),
        credits,
    )
    return out


class _Pending:
    __slots__ = ("slug", "fields", "label", "deadline", "since", "future")


class _Batcher:
    """Per event loop: queues lookups by requested fields, sends them in batches."""

    def __init__(self) -> None:
        self.queues: Dict[Tuple[str, ...], List[_Pending]] = {}
        self.runner: Optional[asyncio.Task] = None

    def submit(self, slug: str, fields: Tuple[str, ...], label: str, deadline: float) -> "asyncio.Future":
        loop = asyncio.get_running_loop()
        p = _Pending()
        p.slug, p.fields, p.label, p.deadline, p.since = slug, fields, label, deadline, _clock()
        p.future = loop.create_future()
        self.queues.setdefault(fields, []).append(p)
        if self.runner is None or self.runner.done():
            self.runner = loop.create_task(self._run())
        return p.future

    async def _run(self) -> None:
        try:
            await _sleep(batch_window_s())
            while True:
                live = [(k, q) for k, q in self.queues.items() if q]
                if not live:
                    return
                fields, queue = min(live, key=lambda kq: kq[1][0].since)
                now = _clock()
                for p in [p for p in queue if p.deadline <= now]:
                    queue.remove(p)
                    _settle(p, dict(RATE_LIMITED))
                if not queue:
                    continue
                size = _effective_batch_size()
                slugs: List[str] = []
                for p in queue:
                    if p.slug not in slugs and len(slugs) < size:
                        slugs.append(p.slug)
                batch = [p for p in queue if p.slug in slugs]
                queue[:] = [p for p in queue if p.slug not in slugs]
                label = batch[0].label if len(slugs) == 1 else f"a batch of {len(slugs)}"
                try:
                    results = await _lookup_many(slugs, fields, min(p.deadline for p in batch), label)
                except Exception as e:
                    logger.warning("Kipplo batch failed: %s", e)
                    results = {}
                for p in batch:
                    _settle(p, results.get(p.slug) or {"ok": False, "message": "Kipplo: no answer"})
        finally:
            # Never leave a caller waiting (runner cancelled or crashed).
            for queue in self.queues.values():
                while queue:
                    _settle(queue.pop(0), {"ok": False, "message": "Kipplo: no answer"})


def _settle(p: _Pending, result: Dict[str, Any]) -> None:
    if not p.future.done():
        p.future.set_result(result)


_batchers: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Batcher]" = weakref.WeakKeyDictionary()


def _batcher() -> _Batcher:
    loop = asyncio.get_running_loop()
    batcher = _batchers.get(loop)
    if batcher is None:
        batcher = _batchers[loop] = _Batcher()
    return batcher


async def enrich_by_linkedin(
    candidate_id: str,
    linkedin_url: str,
    fields: Tuple[str, ...] = CONTACT_FIELDS,
) -> Dict[str, Any]:
    """Look one person up by LinkedIn URL (batched with other lookups waiting at
    the same time). Pure async, no DB writes except the shared rate-limit row.

    ``fields`` is what the candidate still lacks (a subset of ``CONTACT_FIELDS``);
    only those field groups are requested, since Kipplo bills per group found.
    Returns ``{"ok": bool, "fields"|"message": ...}`` like
    ``apollo_enrich_by_linkedin``: ``ok`` means Kipplo answered (possibly with
    nothing). Never raises.
    """
    if not enabled():
        return {"ok": False, "message": "Kipplo not configured"}
    reason = unavailable()
    if reason:
        return {"ok": False, "message": f"Kipplo unavailable: {reason}"}
    wanted = tuple(f for f in supported_fields() if f in (fields or ()))
    if not wanted:
        return {"ok": False, "message": "no contact fields requested"}
    slug = linkedin_slug(linkedin_url)
    if not slug:
        return {"ok": False, "message": "not a LinkedIn profile URL"}

    deadline = _clock() + max_wait_s()
    try:
        if batch_size() <= 1:
            return (await _lookup_many([slug], wanted, deadline, candidate_id))[slug]
        future = _batcher().submit(slug, wanted, candidate_id, deadline)
        return await asyncio.wait_for(asyncio.shield(future), timeout=max_wait_s() + 4 * timeout_s() + 10)
    except Exception as e:
        logger.warning("Kipplo lookup failed for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"Kipplo lookup failed: {type(e).__name__}"}
