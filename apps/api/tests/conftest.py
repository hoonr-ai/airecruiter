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
