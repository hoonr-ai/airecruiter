"""GET /api/health: DB down -> 503, Redis trouble -> degraded 200."""
import asyncio
import json

import main


class _Conn:
    def cursor(self):
        return self

    def execute(self, sql):
        pass

    def fetchone(self):
        return (1,)

    def close(self):
        pass


class _Redis:
    def __init__(self, ok=True):
        self.ok = ok

    async def ping(self):
        if not self.ok:
            raise ConnectionError("down")
        return True


def _call():
    resp = asyncio.run(main.api_health())
    return resp.status_code, json.loads(resp.body)


class _FakeNotificationsRouter:
    def __init__(self, schema_ok):
        self._schema_ok = schema_ok

    def notifications_schema_status(self):
        return self._schema_ok


def test_all_ok(monkeypatch):
    monkeypatch.setattr(main, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: _Redis())
    monkeypatch.setattr(main, "notifications_router", _FakeNotificationsRouter(True))
    assert _call() == (
        200,
        {"status": "ok", "checks": {"db": "ok", "redis": "ok", "notifications_schema": "ok"}},
    )


def test_redis_down_is_degraded_not_failing(monkeypatch):
    monkeypatch.setattr(main, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: _Redis(ok=False))
    monkeypatch.setattr(main, "notifications_router", _FakeNotificationsRouter(True))
    code, body = _call()
    assert code == 200 and body["status"] == "degraded"


def test_db_down_is_503(monkeypatch):
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(main, "get_db_connection", boom)
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: None)
    monkeypatch.setattr(main, "notifications_router", _FakeNotificationsRouter(True))
    code, body = _call()
    assert code == 503 and body["status"] == "down" and body["checks"]["redis"] == "unavailable"


def test_notifications_schema_failure_is_degraded_not_failing(monkeypatch):
    """PR #821 review: a failed notifications schema init must be visible on
    this endpoint instead of only a log line nobody watches."""
    monkeypatch.setattr(main, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: _Redis())
    monkeypatch.setattr(main, "notifications_router", _FakeNotificationsRouter(False))
    code, body = _call()
    assert code == 200 and body["status"] == "degraded" and body["checks"]["notifications_schema"] == "error"


def test_notifications_schema_pending_before_init_has_run(monkeypatch):
    monkeypatch.setattr(main, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: _Redis())
    monkeypatch.setattr(main, "notifications_router", _FakeNotificationsRouter(None))
    code, body = _call()
    assert code == 200 and body["status"] == "ok" and body["checks"]["notifications_schema"] == "pending"


def test_health_route_is_registered():
    assert any(getattr(r, "path", None) == "/api/health" for r in main.app.routes)
