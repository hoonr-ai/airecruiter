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


def test_all_ok(monkeypatch):
    monkeypatch.setattr(main, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: _Redis())
    assert _call() == (200, {"status": "ok", "checks": {"db": "ok", "redis": "ok"}})


def test_redis_down_is_degraded_not_failing(monkeypatch):
    monkeypatch.setattr(main, "get_db_connection", lambda: _Conn())
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: _Redis(ok=False))
    code, body = _call()
    assert code == 200 and body["status"] == "degraded"


def test_db_down_is_503(monkeypatch):
    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(main, "get_db_connection", boom)
    monkeypatch.setattr(main.jobdiva_rate_limit, "_get_redis", lambda: None)
    code, body = _call()
    assert code == 503 and body["status"] == "down" and body["checks"]["redis"] == "unavailable"


def test_health_route_is_registered():
    assert any(getattr(r, "path", None) == "/api/health" for r in main.app.routes)
