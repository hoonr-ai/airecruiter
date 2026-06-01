"""ZoomInfo OAuth2 client_credentials token cache.

Background:
    ZoomInfo's old "paste a static bearer token into .env" pattern died a quiet
    death — the tokens are 1-hour Okta JWTs and we let one expire for 25+ days
    before noticing. This module replaces that with the OAuth2
    `client_credentials` flow so the API server mints its own fresh access
    tokens on demand.

How it works:
    1. First call (or after a 401 with `force_refresh=True`) POSTs
       ``grant_type=client_credentials`` to the ZoomInfo token endpoint (an
       Okta endpoint under the hood) with HTTP Basic auth on the
       client_id / client_secret pair.
    2. The response gives us an `access_token` (JWT) and `expires_in` (24h
       today). We cache the token in process memory along with its absolute
       expiry epoch.
    3. Subsequent callers reuse the cached token until it's within
       ``_REFRESH_BUFFER_SECONDS`` of expiring, at which point the next call
       trips a re-mint.
    4. ``asyncio.Lock`` serialises concurrent re-mints inside a single worker
       so the token endpoint sees one POST per worker per token-lifetime, not
       N (one per concurrent enrichment).

Why not refresh_token rotation:
    ZoomInfo's docs mention a `grant_type=refresh_token` flow, but
    ``client_credentials`` doesn't issue refresh tokens — it's already
    stateless server-to-server. The flow is functionally identical without
    the persistence headache.

Why not Postgres-backed cache (multi-worker concurrency):
    Multiple uvicorn workers each minting their own token concurrently is
    fine — ZoomInfo accepts as many ``client_credentials`` exchanges as we
    care to make. Each worker hangs onto its own in-memory token until it
    expires, then mints another. Tested empirically: minting twice in a row
    returns two different JWTs (different `jti`), both valid simultaneously.

Configuration:
    `ZOOMINFO_CLIENT_ID`, `ZOOMINFO_CLIENT_SECRET`,
    `ZOOMINFO_OAUTH_TOKEN_URL` (default points at the Okta endpoint that
    matches the JWT's `iss`), `ZOOMINFO_SCOPES` (default `api:data:contact`).

Failure mode:
    If credentials aren't configured (legacy deployments) the helper raises
    ``ZoomInfoAuthNotConfigured`` so callers can treat ZoomInfo as disabled
    and fall through to Apollo / no-op without crashing the request.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from core.config import (
    ZOOMINFO_CLIENT_ID,
    ZOOMINFO_CLIENT_SECRET,
    ZOOMINFO_OAUTH_TOKEN_URL,
    ZOOMINFO_SCOPES,
)

logger = logging.getLogger(__name__)

# Refresh a little before the token actually expires so a token we hand out
# can't die mid-request on a slow downstream call.
_REFRESH_BUFFER_SECONDS = 60

# Module-level cache. Workers each own a copy — see module docstring.
_cached_token: Optional[str] = None
_cached_exp: float = 0.0  # epoch seconds; 0 = no cache
_lock = asyncio.Lock()


class ZoomInfoAuthNotConfigured(RuntimeError):
    """Raised when CLIENT_ID/SECRET aren't set. Caller should disable ZI."""


class ZoomInfoAuthFailed(RuntimeError):
    """Raised when the token endpoint rejects our credentials (4xx) or
    returns a malformed body. Caller should log and disable ZI for this
    request; the next request will retry the mint."""


def _is_configured() -> bool:
    return bool(ZOOMINFO_CLIENT_ID and ZOOMINFO_CLIENT_SECRET and ZOOMINFO_OAUTH_TOKEN_URL)


def _cache_is_fresh(now: float) -> bool:
    return bool(_cached_token) and now < (_cached_exp - _REFRESH_BUFFER_SECONDS)


async def _mint_token() -> None:
    """POST client_credentials to the token endpoint and update the cache.

    Must be called inside ``_lock``. On success updates the two module
    globals; on failure raises (cache stays whatever it was).
    """
    global _cached_token, _cached_exp

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                ZOOMINFO_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "scope": ZOOMINFO_SCOPES,
                },
                auth=(ZOOMINFO_CLIENT_ID, ZOOMINFO_CLIENT_SECRET),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except httpx.HTTPError as exc:
            raise ZoomInfoAuthFailed(f"token mint request failed: {exc}") from exc

    if response.status_code != 200:
        raise ZoomInfoAuthFailed(
            f"token mint returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ZoomInfoAuthFailed(f"token mint returned non-JSON: {response.text[:200]}") from exc

    token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not token or not isinstance(expires_in, int):
        raise ZoomInfoAuthFailed(f"token mint missing access_token/expires_in: {payload}")

    _cached_token = token
    _cached_exp = time.time() + expires_in
    logger.info(
        "zoominfo_auth: minted new access token, expires_in=%ds, exp_at=%.0f",
        expires_in,
        _cached_exp,
    )


async def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid ZoomInfo access token, minting one if needed.

    Args:
        force_refresh: drop the cache and mint a fresh token even if the
            current one looks valid. Pass `True` from a 401 retry path —
            ZoomInfo can revoke server-side (e.g. when the admin rotates the
            secret or kills an old session).

    Raises:
        ZoomInfoAuthNotConfigured: credentials aren't in env. Caller should
            log and treat ZoomInfo as disabled for this request.
        ZoomInfoAuthFailed: the token endpoint rejected us or returned an
            unparseable body.
    """
    if not _is_configured():
        raise ZoomInfoAuthNotConfigured(
            "ZoomInfo OAuth not configured: set ZOOMINFO_CLIENT_ID + "
            "ZOOMINFO_CLIENT_SECRET (+ optional ZOOMINFO_OAUTH_TOKEN_URL)."
        )

    now = time.time()
    if not force_refresh and _cache_is_fresh(now):
        return _cached_token  # type: ignore[return-value]

    async with _lock:
        # Re-check after acquiring the lock — a sibling task may have already
        # refreshed while we were waiting. Without this, every concurrent
        # caller queues up and runs its own mint.
        now = time.time()
        if not force_refresh and _cache_is_fresh(now):
            return _cached_token  # type: ignore[return-value]
        await _mint_token()
        return _cached_token  # type: ignore[return-value]


async def invalidate_cache() -> None:
    """Drop the cached token. Test/admin use only."""
    global _cached_token, _cached_exp
    async with _lock:
        _cached_token = None
        _cached_exp = 0.0


def _peek_cache_for_tests() -> tuple[Optional[str], float]:
    """Internal — let tests inspect cache state without taking the lock."""
    return _cached_token, _cached_exp
