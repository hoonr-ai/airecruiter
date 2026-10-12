"""Launch-session knobs, read from the environment at call time.

Every value is read when it is used (not at import), so an operator can flip a
kill switch with a restart-free ``systemctl reload`` and tests can monkeypatch
``os.environ``. All of them are documented in apps/api/.env.example and
docs/launch-sessions.md.
"""

from __future__ import annotations

import os
from typing import Optional

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(float((os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


_PROD_NAMES = {"prod", "production", "prd", "live"}
_PROD_HOSTS = {"pair.pyramidci.com"}


def _prod_looking() -> bool:
    """The fallback when core.newrelic cannot be imported: the same signals
    resolve_environment reads (NEW_RELIC_ENVIRONMENT / APP_ENV / ENVIRONMENT /
    ENV, then the APP_BASE_URL host), any of them saying PROD."""
    for var in ("NEW_RELIC_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV"):
        if (os.getenv(var) or "").strip().lower() in _PROD_NAMES:
            return True
    try:
        from urllib.parse import urlparse

        host = (urlparse((os.getenv("APP_BASE_URL") or "").strip()).hostname or "").lower()
    except Exception:  # noqa: BLE001
        host = ""
    return host in _PROD_HOSTS


def _on_outside_prod() -> bool:
    """The default of the two launch-session switches: ON on QA and local,
    OFF on PROD (core.newrelic.resolve_environment: NEW_RELIC_ENVIRONMENT,
    APP_ENV / ENVIRONMENT / ENV, then the APP_BASE_URL host). If that cannot
    be resolved, ON only when nothing looks like PROD."""
    try:
        from core.newrelic import resolve_environment

        return resolve_environment() != "prod"
    except Exception:  # noqa: BLE001 — unsure → off only where it looks like PROD
        return not _prod_looking()


def server_launch_enabled() -> bool:
    """PAIR_SERVER_LAUNCH_ENABLED: the browser uses server launch sessions;
    false = the legacy browser-driven Source & Launch. Gates session CREATION
    only — sessions already created still run to the end, and the browser
    still reattaches to / watches them. Unset (or not a boolean) = ON on QA
    and local, OFF on PROD (``_on_outside_prod``); an explicit true / false
    always wins. deploy.yml passes the GitHub environment variable of the
    same name, which deploy-azure.sh writes into apps/api/.env when set."""
    return _bool("PAIR_SERVER_LAUNCH_ENABLED", _on_outside_prod())


def runner_enabled() -> bool:
    """PAIR_LAUNCH_RUNNER_ENABLED: false stops every worker from claiming
    sessions (they wait, queued — housekeeping cancels never-started ones
    after PAIR_LAUNCH_QUEUED_MAX_S — until it is back on), and turns creation
    off too (/config answers enabled=false, create answers 503 disabled).
    Same default and deploy wiring as PAIR_SERVER_LAUNCH_ENABLED: unset = ON
    on QA and local, OFF on PROD."""
    return _bool("PAIR_LAUNCH_RUNNER_ENABLED", _on_outside_prod())


def creation_enabled() -> bool:
    """May a new launch session be created? Only when sessions are on AND a
    runner will pick them up (a session created with the runner off would
    hold its job, queued, until it expired)."""
    return server_launch_enabled() and runner_enabled()


def poll_ms() -> int:
    return _int("PAIR_LAUNCH_POLL_MS", 3000, 500)


def hidden_poll_ms() -> int:
    return _int("PAIR_LAUNCH_HIDDEN_POLL_MS", 15000, 1000)


def lease_s() -> int:
    """How long a claim lasts without a heartbeat (SIGKILL recovery time)."""
    return _int("PAIR_LAUNCH_LEASE_S", 180, 30)


def heartbeat_s() -> int:
    return _int("PAIR_LAUNCH_HEARTBEAT_S", 30, 1)


def self_fence_margin_s() -> int:
    """A runner whose heartbeats have failed for (lease - this) seconds stops
    starting new units: another worker may claim the session soon."""
    return _int("PAIR_LAUNCH_SELF_FENCE_MARGIN_S", 20, 1)


def assert_owner_margin_s() -> int:
    """assert_owner() refuses when less than this is left on the lease."""
    return _int("PAIR_LAUNCH_ASSERT_MARGIN_S", 30, 0)


def tick_s() -> int:
    return _int("PAIR_LAUNCH_TICK_S", 10, 1)


def housekeeping_s() -> int:
    return _int("PAIR_LAUNCH_HOUSEKEEPING_S", 60, 5)


def max_attempts() -> int:
    """Failed stage runs (not shutdowns, not waits) before a session fails."""
    return _int("PAIR_LAUNCH_MAX_ATTEMPTS", 5, 1)


def access_denied_attempts() -> int:
    """403s tolerated (retried) before a session fails with 'access revoked'."""
    return _int("PAIR_LAUNCH_ACCESS_DENIED_ATTEMPTS", 3, 1)


def global_concurrency() -> int:
    """Sessions past their search (lookups, saves, Pairbot posts) running at
    once across the fleet."""
    return _int("PAIR_LAUNCH_GLOBAL_CONCURRENCY", 3, 1)


def search_concurrency() -> int:
    """Full-search sessions in their search phase (S1 search + S2 selection)
    running at once across the fleet. The browser ran every recruiter's
    search at once, so this is kept well above the launch cap; the
    per-worker cap still bounds a worker to one session."""
    return _int("PAIR_LAUNCH_SEARCH_CONCURRENCY", 8, 1)


def worker_concurrency() -> int:
    return _int("PAIR_LAUNCH_WORKER_CONCURRENCY", 1, 1)


def db_concurrency() -> int:
    """Runner DB calls in flight per worker (HTTP keeps the rest of the pool)."""
    return _int("PAIR_LAUNCH_DB_CONCURRENCY", 3, 1)


def enrich_concurrency() -> int:
    """Contact lookups in flight (the browser: 4 groups of 5)."""
    return _int("PAIR_LAUNCH_ENRICH_CONCURRENCY", 20, 1)


def save_batch_size() -> int:
    return _int("PAIR_LAUNCH_SAVE_BATCH", 25, 1)


def save_retries() -> int:
    return _int("PAIR_LAUNCH_SAVE_RETRIES", 3, 1)


def pairbot_batch_size() -> int:
    return _int("PAIR_LAUNCH_BATCH_SIZE", 75, 1)


def stream_flush_events() -> int:
    return _int("PAIR_LAUNCH_STREAM_FLUSH_EVENTS", 50, 1)


def stream_flush_s() -> float:
    return float(_int("PAIR_LAUNCH_STREAM_FLUSH_MS", 2000, 100)) / 1000.0


def repost_after_s() -> int:
    """A posted row Pairbot has no record of may be re-posted only this long
    after its POST (a POST still in flight would not show up yet)."""
    return _int("PAIR_LAUNCH_REPOST_AFTER_S", 600, 0)


def fresh_bulk_s() -> int:
    """A bulk posted less than this long ago is not passed as own_bulk_ids
    (Pairbot may not have registered it yet, so it would not read as creating)."""
    return _int("PAIR_LAUNCH_FRESH_BULK_S", 30, 0)


def settle_wait_s() -> int:
    """Back-off between reconcile checks while Pairbot is still creating."""
    return _int("PAIR_LAUNCH_SETTLE_WAIT_S", 45, 1)


def settle_max_s() -> int:
    """How long a launching session keeps waiting for ambiguous rows before
    it records them as 'unconfirmed' and moves on (housekeeping re-checks
    those for PAIR_LAUNCH_UNCONFIRMED_RECHECK_H more)."""
    return _int("PAIR_LAUNCH_SETTLE_MAX_S", 1800, 0)


def collecting_ttl_min() -> int:
    return _int("PAIR_LAUNCH_COLLECTING_TTL_MIN", 15, 1)


def queued_max_s() -> int:
    """A session still queued — never claimed by a worker — this long after
    it was queued is cancelled by housekeeping (cancel_reason 'queue_expired'):
    with the runner off (or the fleet stuck) it would hold its job and then
    launch a stale search hours later. 0 = never.

    Default: 30 min while the runner is off (nothing will claim it), 6 h while
    it is on — a busy queue (launch cap 3) can legitimately hold a session for
    a while and must not lose it."""
    default = 1800 if not runner_enabled() else 21600
    return _int("PAIR_LAUNCH_QUEUED_MAX_S", default, 0)


def review_ttl_h() -> int:
    return _int("PAIR_LAUNCH_REVIEW_TTL_H", 24, 1)


def unconfirmed_recheck_h() -> int:
    return _int("PAIR_LAUNCH_UNCONFIRMED_RECHECK_H", 24, 1)


def recheck_every_s() -> int:
    return _int("PAIR_LAUNCH_RECHECK_EVERY_S", 300, 10)


def retention_days() -> int:
    return _int("PAIR_LAUNCH_RETENTION_DAYS", 14, 1)


def provision_chunk() -> int:
    return _int("PAIR_LAUNCH_PROVISION_CHUNK", 25, 1)


def provision_tries() -> int:
    """Tries per provisioning chunk (a claim DB error) before the chunk is
    counted failed and the session moves on."""
    return _int("PAIR_LAUNCH_PROVISION_TRIES", 3, 1)


def max_session_candidates() -> int:
    return _int("PAIR_LAUNCH_MAX_CANDIDATES", 1000, 1)


def upload_chunk_max() -> int:
    return _int("PAIR_LAUNCH_UPLOAD_CHUNK", 50, 1)


def require_review_default(environment: Optional[str] = None) -> bool:
    """PAIR_LAUNCH_REQUIRE_REVIEW_DEFAULT = auto (default) | true | false.

    auto: true on QA (the QA Override review — no real candidate is called
    from QA without a human confirming the contacts), false elsewhere. A
    request that says require_review explicitly always wins."""
    raw = (os.getenv("PAIR_LAUNCH_REQUIRE_REVIEW_DEFAULT") or "auto").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    env = environment
    if env is None:
        try:
            from core.newrelic import resolve_environment

            env = resolve_environment()
        except Exception:  # noqa: BLE001 — unsure → not QA
            env = "local"
    return env == "qa"
