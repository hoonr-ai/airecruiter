"""Redis cache of JobDiva JobAgentSearch results, plus the job-intake prewarm.

JobAgentSearch takes minutes for a full tranche and its ranking only changes
when the job's JobDiva search criteria change, so a result is reused for
``JOBAGENT_CACHE_TTL_S`` (default 12 h).

Key: ``jd:ja:{job_id}:{sha1(canonical criteria json)}``. The criteria hashed
are the call inputs that change the result besides the job (``require_resume``
today); ``resume_count`` is stored in the value instead, so a cached full
tranche also serves the smaller quick-first call (sliced to the requested
count — JobAgent results are rank-ordered). Edits to the job's criteria
delete every key for the job (``invalidate``).

Aliases: a job is known by its numeric JobDiva id and its ref code. The
intake prewarm records ``jd:ja:alias:{id} -> canonical`` for both forms, and
get/put/invalidate resolve through it, so either form reads, writes and
invalidates the same entries. Written keys are tracked in the per-job set
``jd:ja:keys:{canonical}`` so ``invalidate`` needs no SCAN (SCAN remains a
fallback for entries without an index).

Values carry candidate PII (resume text, email, phone), so they are
Fernet-encrypted with the same derived key as ``jobdiva_detail_cache``.
Empty / "Criteria Not Assigned" results are never cached. No Redis or no
encryption key → no cache, never an error.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Optional

from core import config as _cfg
from services import jobdiva_detail_cache as _dc
from services import jobdiva_rate_limit as _rl

log = logging.getLogger(__name__)

_PREFIX = "jd:ja:"


def _ttl_s() -> int:
    return int(getattr(_cfg, "JOBAGENT_CACHE_TTL_S", 12 * 3600) or 0)


def criteria_hash(criteria: Dict[str, Any]) -> str:
    canon = json.dumps(criteria or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canon.encode()).hexdigest()


def cache_key(job_id: Any, require_resume: bool = True) -> str:
    """Key for an already-canonical job id (see ``canonical_job_id``)."""
    return f"{_PREFIX}{job_id}:{criteria_hash({'require_resume': bool(require_resume)})}"


def _alias_key(job_id: Any) -> str:
    return f"{_PREFIX}alias:{job_id}"


def _keyset_key(canonical: Any) -> str:
    return f"{_PREFIX}keys:{canonical}"


async def register_aliases(canonical: Any, aliases: tuple = ()) -> None:
    """Record that each alias (numeric id / ref code) names ``canonical``, so
    get/put/invalidate via any form resolve to the one canonical key space."""
    if not canonical:
        return
    client = _rl._get_redis()
    if client is None:
        return
    others = [str(a) for a in aliases if a and str(a) != str(canonical)]
    if not others:
        return
    ttl = max(_ttl_s(), 1) * 2  # outlive the entries the mapping points at
    try:
        pipe = client.pipeline(transaction=False)
        pipe.set(_alias_key(canonical), str(canonical), ex=ttl)
        for a in others:
            pipe.set(_alias_key(a), str(canonical), ex=ttl)
        await pipe.execute()
    except Exception as exc:
        _rl._mark_redis_down(exc)


async def canonical_job_id(job_id: Any, client: Any = None) -> str:
    """Resolve ``job_id`` through the alias map; unknown ids are their own canonical."""
    jid = str(job_id)
    client = client if client is not None else _rl._get_redis()
    if client is None:
        return jid
    try:
        val = await client.get(_alias_key(jid))
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return jid
    if not val:
        return jid
    return val.decode() if isinstance(val, bytes) else str(val)


def _client_and_cipher():
    if _ttl_s() <= 0:
        return None, None
    fernet = _dc._get_fernet()
    if fernet is None:
        return None, None
    return _rl._get_redis(), fernet


async def _get_canonical(client, fernet, canon: str, resume_count: int, require_resume: bool):
    try:
        raw = await client.get(cache_key(canon, require_resume))
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return None
    if not raw:
        return None
    try:
        entry = json.loads(fernet.decrypt(raw.encode() if isinstance(raw, str) else raw))
    except Exception:
        return None
    if not isinstance(entry, dict) or int(entry.get("resume_count") or 0) < int(resume_count):
        return None
    result = dict(entry.get("result") or {})
    result["candidates"] = list(result.get("candidates") or [])[: int(resume_count)]
    result["cache_hit"] = True
    return result


async def get(job_id: Any, *, resume_count: int, require_resume: bool = True) -> Optional[Dict[str, Any]]:
    """Cached result with at least ``resume_count`` requested, sliced to it; else None."""
    if not job_id:
        return None
    client, fernet = _client_and_cipher()
    if client is None:
        return None
    canon = await canonical_job_id(job_id, client)
    return await _get_canonical(client, fernet, canon, resume_count, require_resume)


async def put(job_id: Any, result: Dict[str, Any], *, resume_count: int, require_resume: bool = True) -> None:
    if not job_id or not isinstance(result, dict):
        return
    if not result.get("candidates") or result.get("criteria_unconfigured"):
        return
    client, fernet = _client_and_cipher()
    if client is None:
        return
    canon = await canonical_job_id(job_id, client)
    key = cache_key(canon, require_resume)
    try:
        # Never replace a larger cached tranche with a smaller one.
        existing = await _get_canonical(client, fernet, canon, int(resume_count) + 1, require_resume)
        if existing is not None:
            return
        token = fernet.encrypt(json.dumps(
            {"resume_count": int(resume_count), "result": result}, default=str
        ).encode()).decode()
        ttl = _ttl_s()
        pipe = client.pipeline(transaction=False)
        pipe.set(key, token, ex=ttl)
        # Per-job key index so invalidate needs no SCAN.
        pipe.sadd(_keyset_key(canon), key)
        pipe.expire(_keyset_key(canon), ttl + 60)
        await pipe.execute()
    except Exception as exc:
        _rl._mark_redis_down(exc)


async def invalidate(job_id: Any) -> int:
    """Delete every cached JobAgent result for ``job_id`` (any alias form).
    Returns keys deleted."""
    if not job_id:
        return 0
    client = _rl._get_redis()
    if client is None:
        return 0
    try:
        canon = await canonical_job_id(job_id, client)
        ids = {canon, str(job_id)}
        keys: set = set()
        for jid in ids:
            members = await client.smembers(_keyset_key(jid))
            keys.update(m.decode() if isinstance(m, bytes) else str(m) for m in (members or ()))
        if not keys:
            # Fallback for entries written before the key index existed.
            for jid in ids:
                keys.update([
                    k.decode() if isinstance(k, bytes) else str(k)
                    async for k in client.scan_iter(match=f"{_PREFIX}{jid}:*", count=100)
                ])
        n = int(await client.delete(*keys)) if keys else 0
        await client.delete(*[_keyset_key(j) for j in ids])
        return n
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return 0


async def prewarm_job(job_id: Any, aliases: tuple = ()) -> None:
    """Job intake: fill the JobAgent cache and warm the applicant/detail path
    at background priority so the recruiter's first search is fast. Never raises."""
    if not job_id or not getattr(_cfg, "JOB_INTAKE_PREWARM_ENABLED", True):
        return
    try:
        from core import sourcing_config as _sc
        from services.jobdiva import jobdiva_service

        resume_count = int(getattr(_sc, "JOBAGENT_RESUME_COUNT", 150) or 150)
        with _rl.background_context():
            # Searches may key on the numeric id or the ref code: map every
            # alias to job_id so both forms share one cache entry.
            await register_aliases(job_id, aliases)
            if await get(job_id, resume_count=resume_count) is None:
                result = await jobdiva_service.search_via_job_agent(
                    job_id=str(job_id), resume_count=resume_count, require_resume=True,
                )
                await put(job_id, result, resume_count=resume_count)
            try:
                token = await jobdiva_service.authenticate()
                if token:
                    await jobdiva_service._get_all_job_applicants(str(job_id), 100, token)
            except Exception as exc:
                log.info("prewarm_job(%s): applicants warm skipped: %s", job_id, exc)
        log.info("prewarm_job(%s): done", job_id)
    except Exception as exc:
        log.warning("prewarm_job(%s) failed: %s", job_id, exc)
