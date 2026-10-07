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
    for lim in (vendor_limiter.OPENAI, vendor_limiter.EXA):
        monkeypatch.setattr(lim, "_local_next", 0.0)
        monkeypatch.setattr(lim, "_local_cooldown_until", 0.0)


@pytest.fixture(autouse=True)
def _no_real_amplitude_events(monkeypatch):
    """core.logging ships every WARNING/ERROR to Amplitude through httpx when
    AMPLITUDE_API_KEY is set (it is, in apps/api/.env). From the suite that
    sent real events and, in tests that stub httpx, showed up as extra
    requests in their call counts."""
    monkeypatch.setattr("core.amplitude.AMPLITUDE_API_KEY", "", raising=False)


@pytest.fixture(autouse=True)
def _no_real_kipplo_calls(monkeypatch):
    """Kipplo bills per hit and core.config loads apps/api/.env, so a key there
    must never reach a real lookup from the suite. Kipplo tests set a fake key
    and stub httpx themselves."""
    monkeypatch.delenv("KIPPLO_API_KEY", raising=False)


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
