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
