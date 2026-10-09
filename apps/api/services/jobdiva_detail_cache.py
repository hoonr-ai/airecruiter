"""Short-lived Redis cache of JobDiva CandidatesDetail records, keyed by id.

Repeat searches, background hydration and single-id lookups ask for the same
candidates within minutes; each miss spends JobDiva BI quota (incident
2026-09-29, docs/incidents/2026-09-29-jobdiva-429-nginx-503.md). Records are
cached for ``JOBDIVA_DETAIL_CACHE_TTL_S`` (default 24 h, 0 disables). Only
hits are cached — a missing record is always re-fetched. No Redis → no cache,
never an error.

The records carry candidate PII (email, phone, address), so values are
Fernet-encrypted with a key derived from ``ENCRYPTION_KEY`` and never sit in
Redis as plaintext. The full record is kept (not a field subset) because it
feeds the candidate normalizer, which reads dozens of keys.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional

from services import jobdiva_rate_limit as _rl

log = logging.getLogger(__name__)

_PREFIX = "jobdiva:cand_detail:"
try:
    TTL_S = int((os.getenv("JOBDIVA_DETAIL_CACHE_TTL_S") or "").strip() or 24 * 3600)
except ValueError:
    TTL_S = 24 * 3600

_fernet: Optional[Any] = None
_fernet_failed = False


def _get_fernet() -> Optional[Any]:
    """Fernet keyed off ENCRYPTION_KEY; None disables the cache (no plaintext fallback)."""
    global _fernet, _fernet_failed
    if _fernet is not None or _fernet_failed:
        return _fernet
    try:
        from cryptography.fernet import Fernet
        from core.config import ENCRYPTION_KEY
        if not ENCRYPTION_KEY:
            raise ValueError("ENCRYPTION_KEY is empty")
        digest = hashlib.sha256(b"jobdiva-detail-cache:" + ENCRYPTION_KEY.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    except Exception as exc:
        log.warning("jobdiva_detail_cache: disabled, no encryption key: %s", exc)
        _fernet_failed = True
    return _fernet


def _client_and_cipher():
    if TTL_S <= 0:
        return None, None
    fernet = _get_fernet()
    if fernet is None:
        return None, None
    return _rl._get_redis(), fernet


async def get_many(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    client, fernet = _client_and_cipher()
    if client is None or not ids:
        return {}
    try:
        raw = await client.mget([f"{_PREFIX}{cid}" for cid in ids])
    except Exception as exc:
        _rl._mark_redis_down(exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for cid, val in zip(ids, raw or []):
        if not val:
            continue
        try:
            rec = json.loads(fernet.decrypt(val.encode()))
        except Exception:
            continue  # wrong key / corrupt / pre-encryption value: treat as miss
        if isinstance(rec, dict):
            out[cid] = rec
    return out


async def set_many(records: Dict[str, Dict[str, Any]]) -> None:
    client, fernet = _client_and_cipher()
    if client is None or not records:
        return
    try:
        pipe = client.pipeline(transaction=False)
        for cid, rec in records.items():
            token = fernet.encrypt(json.dumps(rec, default=str).encode()).decode()
            pipe.set(f"{_PREFIX}{cid}", token, ex=TTL_S)
        await pipe.execute()
    except Exception as exc:
        _rl._mark_redis_down(exc)
