"""Redis cache of Exa people-search results (Pass A and Dice), per query.

Pass A buys one Exa search per title query and nothing kept the answer, so
the sample run, the full run, relaxation attempts (attempts 1-2 and 3-4 send
byte-identical queries) and a resumed launch session bought the same
searches again: about 4 Exa runs per job on PROD (docs/exa-cost-optimization.md).
Each query's results are now kept for EXA_SEARCH_CACHE_TTL_S (default 12 h).

Key: ``exa:ppl:v1:{env}:{sha1(canonical json of q, category, type,
include_domains sorted, contents spec)}``. ``num_results`` is deliberately
NOT in the key: the value records the size asked (``n_req``), a request for
fewer is served from a larger entry sliced to it (Exa results are rank
ordered), a request for more misses. ``env`` keeps dev / QA / PROD apart on
a shared Redis (their ENCRYPTION_KEYs differ anyway). Bump ``_VERSION`` when
the stored shape changes; the contents spec is hashed into the key already.

Value: ``"{n_req}.{token}"``, token = Fernet(zlib(json{n_req, cached_at,
results: [{id, url, title, text, highlights}]})), the only result fields
the parsers read (exa_service._common_people_fields, search_candidates,
search_dice_candidates). Highlights are résumé text, so the token is
encrypted with a key derived from ENCRYPTION_KEY under this module's own
salt. The plain ``n_req`` prefix lets ``put`` keep its no-shrink guard
without reading and decrypting a ~100-300 KB entry.

Writes: never an empty result or a failure (the caller only puts a paid
answer), never a value over 512 KB, never over an entry that already holds
at least as many results. A thin answer (fewer than min(n_req, 10) rows)
lives 30 minutes, not the full TTL, so a blip cannot pin it for 12 h.

``get`` / ``put`` never raise. A Redis connection or timeout error opens this
module's own 30 s down window, never jobdiva_rate_limit's (a cache error
must not switch the JobDiva BI limiter to per-worker pacing); a
ResponseError (e.g. OOM) is a logged miss. An unreadable value (written
under another ENCRYPTION_KEY, or corrupt) is a miss and is deleted: left in
place, the no-shrink guard (which reads only the plain prefix) would refuse
every rewrite and the query would be bought on every search until it
expired. Encode / decode run on a dedicated 2-thread executor, not the
default one that the blocking Exa SDK calls queue on.

``state()``: ``on`` | ``off`` (EXA_SEARCH_CACHE_TTL_S=0) | ``off_config`` (no
REDIS_URL or no ENCRYPTION_KEY: a configuration fault, one WARNING per
process) | ``down``.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import config as _cfg
from core import newrelic as _nr
from services import jobdiva_rate_limit as _rl

log = logging.getLogger(__name__)

_VERSION = "v1"
_PREFIX = f"exa:ppl:{_VERSION}:"
# What the parsers read off a result; everything else is dropped.
FIELDS = ("id", "url", "title", "text", "highlights")
MAX_VALUE_BYTES = 512 * 1024
THIN_ROWS = 10
THIN_TTL_S = 30 * 60
DOWN_S = 30.0
_WARN_EVERY_S = 60.0

try:  # redis is a requirement; a broken install only disables the cache
    from redis import exceptions as _redis_exc

    _DOWN_ERRORS: Tuple[type, ...] = (
        _redis_exc.ConnectionError, _redis_exc.TimeoutError, OSError, asyncio.TimeoutError,
    )
    _RESPONSE_ERRORS: Tuple[type, ...] = (_redis_exc.ResponseError,)
except Exception:  # pragma: no cover
    _DOWN_ERRORS = (OSError, asyncio.TimeoutError)
    _RESPONSE_ERRORS = ()

try:
    from cryptography.fernet import InvalidToken as _InvalidToken
except Exception:  # pragma: no cover
    class _InvalidToken(Exception):  # type: ignore[no-redef]
        pass

# A value that cannot be decrypted / inflated / parsed (json and utf-8
# errors are ValueErrors).
_UNREADABLE = (_InvalidToken, zlib.error, ValueError, TypeError)

_down_until = 0.0
_off_logged = False
_last_warn_at: Optional[float] = None
_fernet: Optional[Any] = None
_fernet_failed = False
_executor: Optional[ThreadPoolExecutor] = None


def ttl_s() -> int:
    try:
        return max(0, int(getattr(_cfg, "EXA_SEARCH_CACHE_TTL_S", 12 * 3600) or 0))
    except (TypeError, ValueError):
        return 0


def _get_fernet() -> Optional[Any]:
    """Fernet keyed off ENCRYPTION_KEY with this cache's salt; None = no cache."""
    global _fernet, _fernet_failed
    if _fernet is not None or _fernet_failed:
        return _fernet
    try:
        from cryptography.fernet import Fernet

        key = getattr(_cfg, "ENCRYPTION_KEY", "") or ""
        if not key:
            raise ValueError("ENCRYPTION_KEY is empty")
        digest = hashlib.sha256(b"exa-search-cache:" + key.encode()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(digest))
    except Exception as exc:
        log.warning("Exa search cache: disabled, no encryption key: %s", exc)
        _fernet_failed = True
    return _fernet


def _warn(msg: str, *args: Any) -> None:
    """WARNING at most once a minute (a broken cache must not flood the log)."""
    global _last_warn_at
    now = time.monotonic()
    if _last_warn_at is None or now - _last_warn_at >= _WARN_EVERY_S:
        _last_warn_at = now
        log.warning(msg, *args)


def _mark_down(exc: BaseException) -> None:
    global _down_until
    now = time.monotonic()
    if now >= _down_until:
        log.warning("Exa search cache: Redis unavailable, skipped for %.0fs: %s", DOWN_S, exc)
    _down_until = now + DOWN_S


def _open() -> Tuple[str, Optional[Any], Optional[Any]]:
    """(state, client, cipher); client and cipher are None unless ``on``."""
    global _off_logged
    if ttl_s() <= 0:
        return "off", None, None
    if not getattr(_cfg, "REDIS_URL", "") or _get_fernet() is None:
        if not _off_logged:
            _off_logged = True
            log.warning(
                "Exa search cache: OFF (REDIS_URL or ENCRYPTION_KEY missing): every "
                "Exa people search is bought again"
            )
        return "off_config", None, None
    if time.monotonic() < _down_until:
        return "down", None, None
    client = _rl._get_redis()
    if client is None:
        return "down", None, None
    return "on", client, _fernet


def state() -> str:
    """on | off | off_config | down (see the module docstring)."""
    try:
        return _open()[0]
    except Exception:
        return "down"


def _env() -> str:
    try:
        return _nr.environment() or "local"
    except Exception:
        return "local"


def cache_key(
    query: str,
    *,
    category: str,
    search_type: str,
    include_domains: Optional[Sequence[str]] = None,
    contents: Optional[Dict[str, Any]] = None,
) -> str:
    """Key for one people-search query; num_results is not part of it."""
    canon = json.dumps(
        {
            "q": query,
            "category": category,
            "type": search_type,
            "include_domains": sorted(str(d) for d in (include_domains or ())),
            "contents": contents or {},
        },
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return f"{_PREFIX}{_env()}:{hashlib.sha1(canon.encode()).hexdigest()}"


def _row(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return {k: result[k] for k in FIELDS if k in result}
    return {k: getattr(result, k) for k in FIELDS if hasattr(result, k)}


def _encode(fernet: Any, n_req: int, rows: List[Dict[str, Any]]) -> str:
    raw = json.dumps(
        {"n_req": int(n_req), "cached_at": int(time.time()), "results": rows},
        separators=(",", ":"), default=str,
    ).encode()
    return f"{int(n_req)}.{fernet.encrypt(zlib.compress(raw, 6)).decode()}"


def _decode(fernet: Any, value: str) -> Optional[Dict[str, Any]]:
    _head, _, token = value.partition(".")
    entry = json.loads(zlib.decompress(fernet.decrypt(token.encode())))
    return entry if isinstance(entry, dict) else None


def _head_n(value: Any) -> Optional[int]:
    """``n_req`` from a value's plain prefix (None when absent / unreadable)."""
    if isinstance(value, bytes):
        value = value.decode(errors="ignore")
    head, sep, _ = str(value or "").partition(".")
    if not sep:
        return None
    try:
        return int(head)
    except ValueError:
        return None


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="exa-search-cache")
    return _executor


async def _off_loop(fn: Any, *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(_pool(), fn, *args)


async def _discard(client: Any, key: str, why: BaseException) -> None:
    """Delete an entry this worker cannot read, so the next paid answer can
    replace it (put's no-shrink guard would refuse it otherwise). Another
    worker may have rewritten it in between: dropping that costs one search.
    Never raises."""
    _warn(
        "Exa search cache: unreadable entry dropped (ENCRYPTION_KEY changed, or corrupt): %s",
        type(why).__name__,
    )
    try:
        await client.unlink(key)
    except _DOWN_ERRORS as exc:
        _mark_down(exc)
    except Exception:  # noqa: BLE001 — best effort; it expires anyway
        pass


async def get(key: str, n: int) -> Optional[List[SimpleNamespace]]:
    """Up to ``n`` cached results (rebuilt as attribute objects, in Exa's
    order) when an entry asked for at least ``n``; else None. An unreadable
    entry is deleted (a miss). Never raises."""
    try:
        _state, client, fernet = _open()
        if client is None:
            return None
        try:
            value = await client.get(key)
        except _RESPONSE_ERRORS as exc:
            _warn("Exa search cache: get refused by Redis (a miss): %s", exc)
            return None
        except _DOWN_ERRORS as exc:
            _mark_down(exc)
            return None
        if not value:
            return None
        try:
            if isinstance(value, bytes):
                value = value.decode()
            entry = await _off_loop(_decode, fernet, value)
            if not isinstance(entry, dict):
                raise ValueError("not an entry")
            held = int(entry.get("n_req") or 0)
            rows = [r for r in (entry.get("results") or []) if isinstance(r, dict)]
            if not rows:
                raise ValueError("no results")  # put never stores an empty answer
        except _UNREADABLE as exc:
            await _discard(client, key, exc)
            return None
        if held < int(n):
            return None
        return [SimpleNamespace(**{k: r[k] for k in FIELDS if k in r}) for r in rows[: int(n)]]
    except Exception as exc:  # noqa: BLE001
        _warn("Exa search cache: get failed (a miss): %r", exc)
        return None


async def put(key: str, results: Sequence[Any], n_req: int) -> bool:
    """Store a paid answer for ``n_req`` results. True when written. Never raises."""
    try:
        rows = [_row(r) for r in (results or ())]
        if not rows:
            return False
        _state, client, fernet = _open()
        if client is None:
            return False
        try:
            # Never replace an entry that already holds at least as many.
            held = _head_n(await client.getrange(key, 0, 15))
            if held is not None and held >= int(n_req):
                return False
            value = await _off_loop(_encode, fernet, n_req, rows)
            if len(value) > MAX_VALUE_BYTES:
                log.info("Exa search cache: %d-byte answer not cached (cap %d)", len(value), MAX_VALUE_BYTES)
                return False
            ttl = ttl_s()
            if len(rows) < min(int(n_req), THIN_ROWS):
                ttl = min(ttl, THIN_TTL_S)
            await client.set(key, value, ex=ttl)
            return True
        except _RESPONSE_ERRORS as exc:
            _warn("Exa search cache: put refused by Redis (not cached): %s", exc)
            return False
        except _DOWN_ERRORS as exc:
            _mark_down(exc)
            return False
    except Exception as exc:  # noqa: BLE001
        _warn("Exa search cache: put failed (not cached): %r", exc)
        return False


def reset_for_tests() -> None:
    global _down_until, _off_logged, _last_warn_at, _fernet, _fernet_failed
    _down_until = 0.0
    _off_logged = False
    _last_warn_at = None
    _fernet = None
    _fernet_failed = False
