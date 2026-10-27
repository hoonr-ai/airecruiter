"""Shared pytest bootstrap for apps/api.

Router imports pull in core.config, which requires env vars at import time via
get_env_or_fail (see core/config.py). This module is the single source of truth
for test env stubs — per-module duplicate blocks were removed in #483.
"""
import pytest

from tests.env_stubs import stub_required_env

stub_required_env()


@pytest.fixture(autouse=True)
def _block_real_db_connections(monkeypatch):
    """Fail fast if a test accidentally opens a real Postgres connection."""

    def _fail(*_args, **_kwargs):
        raise RuntimeError(
            "Tests must not open real database connections; mock get_db_connection."
        )

    monkeypatch.setattr("core.db.get_db_connection", _fail, raising=False)


@pytest.fixture(autouse=True)
def _block_real_redis(monkeypatch):
    """core.config loads apps/api/.env, whose REDIS_URL is a shared instance.
    The JobDiva / Apollo / vendor limiters must never touch it from the suite;
    tests that need Redis set REDIS_URL themselves (fake or a local test DB)."""
    from core import config as cfg
    from core import vendor_limiter
    from services import jobdiva_rate_limit

    monkeypatch.setattr(cfg, "REDIS_URL", "", raising=False)
    monkeypatch.setattr(jobdiva_rate_limit, "_redis_client", None)
    monkeypatch.setattr(vendor_limiter, "_redis_client", None)
    for lim in (vendor_limiter.OPENAI, vendor_limiter.EXA, vendor_limiter.UNIPILE):
        monkeypatch.setattr(lim, "_local_next", 0.0)
        monkeypatch.setattr(lim, "_local_cooldown_until", 0.0)
    # Scripted Unipile fakes make many calls per test; pacing them is just delay.
    monkeypatch.setattr(vendor_limiter.UNIPILE, "min_interval_s", 0.0)
    # Every JobDiva call is paced now (jobdiva_rate_limit.paced_send): start
    # each test with a full bucket and no cooldown left over from another.
    monkeypatch.setattr(jobdiva_rate_limit, "_local_tokens", None)
    monkeypatch.setattr(jobdiva_rate_limit, "_local_cooldown_until", 0.0)
    monkeypatch.setattr(jobdiva_rate_limit, "_local_fg_waiting_until", 0.0)


@pytest.fixture(autouse=True)
def _fresh_jobdiva_read_cache(monkeypatch):
    """services.jobdiva's read cache is per worker; a value cached by one test
    must not answer another's lookup. Off by default (TTL 0); the cache's own
    tests build their own."""
    from core.bounded_cache import AsyncTTLCache
    from services import jobdiva

    monkeypatch.setattr(jobdiva, "_read_cache", AsyncTTLCache(maxsize=10, ttl_s=0))


@pytest.fixture(autouse=True)
def _no_real_kipplo_calls(monkeypatch):
    """Kipplo bills per hit and core.config loads apps/api/.env, so a key there
    must never reach a real lookup from the suite. Kipplo tests set a fake key
    and stub httpx themselves."""
    monkeypatch.delenv("KIPPLO_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _jobdiva_provisioning_default_on(monkeypatch):
    """JOBDIVA_PROVISIONING_ENABLED is the local-run kill switch (a local
    end-to-end launch must never write JobDiva), so apps/api/.env may set it
    false. The suite tests the default (on); kill-switch tests set it."""
    monkeypatch.delenv("JOBDIVA_PROVISIONING_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def _contact_chain_with_every_provider(monkeypatch):
    """Most contact-chain tests cover the mechanics of every provider, so they
    run with all four listed and without the Exa deep search. Production asks
    Apollo and Exa only, deep search on (CONTACT_LOOKUP_PROVIDERS,
    EXA_CONTACT_DEEP_EFFORT); tests/test_contact_ladder.py covers that."""
    from core import sourcing_config
    from services import contact_enrichment

    monkeypatch.setattr(sourcing_config, "CONTACT_LOOKUP_PROVIDERS", ("kipplo", "zoominfo", "apollo", "exa"))
    monkeypatch.setattr(contact_enrichment, "EXA_CONTACT_DEEP_EFFORT", "off")
