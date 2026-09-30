"""Short-lived Redis cache of JobDiva CandidatesDetail records, keyed by id.

Repeat searches, background hydration and single-id lookups ask for the same
candidates within minutes; each miss spends JobDiva BI quota (incident
2026-09-29, see fix.md). Records are cached for
``JOBDIVA_DETAIL_CACHE_TTL_S`` (default 20 min). Only hits are cached — a
missing record is always re-fetched. No Redis → no cache, never an error.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from services.jobdiva_rate_limit import _get_redis

log = logging.getLogger(__name__)

_PREFIX = "jobdiva:cand_detail:"
try:
    TTL_S = int((os.getenv("JOBDIVA_DETAIL_CACHE_TTL_S") or "").strip() or 20 * 60)
except ValueError:
    TTL_S = 20 * 60


async def get_many(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    client = _get_redis()
    if client is None or not ids or TTL_S <= 0:
        return {}
    try:
        raw = await client.mget([f"{_PREFIX}{cid}" for cid in ids])
    except Exception as exc:
        log.debug("jobdiva_detail_cache: mget failed: %s", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for cid, val in zip(ids, raw or []):
        if not val:
            continue
        try:
            rec = json.loads(val)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict):
            out[cid] = rec
    return out


async def set_many(records: Dict[str, Dict[str, Any]]) -> None:
    client = _get_redis()
    if client is None or not records or TTL_S <= 0:
        return
    try:
        pipe = client.pipeline(transaction=False)
        for cid, rec in records.items():
            pipe.set(f"{_PREFIX}{cid}", json.dumps(rec, default=str), ex=TTL_S)
        await pipe.execute()
    except Exception as exc:
        log.debug("jobdiva_detail_cache: set failed: %s", exc)
