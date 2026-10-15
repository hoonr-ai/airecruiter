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
    # A test that reached an unreachable Redis must not leave the next one
    # pacing per-worker for 30 s.
    monkeypatch.setattr(jobdiva_rate_limit, "_redis_down_until", 0.0)
    monkeypatch.setattr(vendor_limiter, "_redis_down_until", 0.0)
    for lim in (vendor_limiter.OPENAI, vendor_limiter.EXA, vendor_limiter.UNIPILE):
        monkeypatch.setattr(lim, "_local_next", 0.0)
        monkeypatch.setattr(lim, "_local_cooldown_until", 0.0)
        # The Exa out-of-credits breaker a 402 test trips must not leak.
        monkeypatch.setattr(lim, "_local_no_credits_until", 0.0)
    # Scripted Unipile fakes make many calls per test; pacing them is just delay.
    monkeypatch.setattr(vendor_limiter.UNIPILE, "min_interval_s", 0.0)


@pytest.fixture(autouse=True)
def _no_real_kipplo_calls(monkeypatch):
    """Kipplo bills per hit and core.config loads apps/api/.env, so a key there
    must never reach a real lookup from the suite. Kipplo tests set a fake key
    and stub httpx themselves."""
    monkeypatch.delenv("KIPPLO_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_real_exa_calls_and_fresh_exa_state(monkeypatch):
    """Exa bills per call and core.config loads apps/api/.env, so a key there
    must never reach a real search or Agent run from the suite: the shared
    SDK client is dropped and the contact path's key blanked (tests that
    exercise them set fakes / a test key). The process-wide Exa telemetry
    state (metadata latch, ExaConfig once-per-hour, bound context) starts
    fresh in every test, and so does the Pass A search cache's (its down
    window, warning latches, in-flight searches)."""
    from services import contact_enrichment, exa_search_cache, exa_telemetry
    from services import exa_service as exa_service_module
    from services.exa_service import exa_service

    monkeypatch.setattr(exa_service, "exa", None)
    monkeypatch.setattr(contact_enrichment, "EXA_API_KEY", "")
    exa_telemetry.reset_for_tests()
    exa_search_cache.reset_for_tests()
    exa_service_module._inflight.clear()
    token = exa_telemetry._ctx.set(exa_telemetry._EMPTY)
    yield
    try:
        exa_telemetry._ctx.reset(token)
    except ValueError:
        pass
    exa_telemetry.reset_for_tests()


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


@pytest.fixture(autouse=True)
def _no_deep_skip_holdout(monkeypatch):
    """The Launch deep-email holdout (EXA_DEEP_SKIP_REACHABLE_PCT, 0 in code
    until it starts; a local .env may set it) would put test slugs in its
    skip arm, and its decision log is a table: off here, and its writes are
    no-ops. tests/test_contact_ladder.py turns it on."""
    from core import sourcing_config
    from services import contact_holdout

    async def _nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sourcing_config, "EXA_DEEP_SKIP_REACHABLE_PCT", 0)
    monkeypatch.setattr(contact_holdout, "record_decision", _nothing)
    monkeypatch.setattr(contact_holdout, "record_deep_outcome", _nothing)
