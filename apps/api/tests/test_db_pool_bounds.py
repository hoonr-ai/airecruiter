"""DB connection bounds and the session-lock connection behind PgBouncer."""
import importlib

import pytest

from core import db


def test_pool_max_default_is_bounded():
    # 8 workers × (8 psycopg2 + 5 SQLAlchemy) = 104, well under max_connections=200.
    assert db._POOL_MAX <= 8


def test_statement_timeout_option_only_without_pooler():
    assert "statement_timeout" in db._connect_kwargs(via_pooler=False)["options"]
    assert "options" not in db._connect_kwargs(via_pooler=True)


def test_session_connection_uses_pool_without_direct_url(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(db, "DATABASE_URL_DIRECT", "")
    monkeypatch.setattr(db, "get_db_connection", lambda: sentinel)
    assert db.get_session_db_connection() is sentinel


def test_session_connection_goes_direct_when_configured(monkeypatch):
    seen = {}

    def fake_connect(dsn, **kw):
        seen["dsn"], seen["kw"] = dsn, kw
        return "direct-conn"

    monkeypatch.setattr(db, "DATABASE_URL_DIRECT", "postgresql://direct:5432/x")
    monkeypatch.setattr(db.psycopg2, "connect", fake_connect)
    assert db.get_session_db_connection() == "direct-conn"
    assert seen["dsn"] == "postgresql://direct:5432/x"
    assert "statement_timeout" in seen["kw"]["options"]


def test_advisory_lock_defaults_to_session_connection(monkeypatch):
    from core.advisory_lock import AdvisoryLock

    monkeypatch.setattr(db, "get_session_db_connection", lambda: "session-conn")
    assert AdvisoryLock(1, "t")._get_connection() == "session-conn"


def test_sqlalchemy_engines_are_small():
    import inspect
    from routers import candidate_processing
    from services import vetted

    for mod in (candidate_processing, vetted):
        src = inspect.getsource(mod)
        assert "pool_size=SQLA_POOL_SIZE" in src and "max_overflow=SQLA_MAX_OVERFLOW" in src, mod.__name__
