"""Platform sessions: the ping upsert, the stale-logout read, User-Agent
parsing, client IP, and the admin read's role scoping
(services/user_sessions.py, routers/user_sessions.py)."""
import datetime
import os
import uuid

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
from routers import user_sessions as router_mod
from services import user_sessions as us

UTC = datetime.timezone.utc
_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ua, expected", [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/129.0 Safari/537.36 Edg/129.0", ("desktop", "Windows", "Edge")),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/17.0 Safari/605.1.15", ("desktop", "macOS", "Safari")),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "CriOS/129.0 Mobile/15E148 Safari/604.1", ("mobile", "iOS", "Chrome")),
    ("Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/17.0 Mobile/15E148 Safari/604.1", ("tablet", "iOS", "Safari")),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/129.0 Mobile Safari/537.36", ("mobile", "Android", "Chrome")),
    # Android without "Mobile" is a tablet.
    ("Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/129.0 Safari/537.36", ("tablet", "Android", "Chrome")),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", ("desktop", "Linux", "Firefox")),
    ("Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
     ("desktop", "ChromeOS", "Chrome")),
    ("curl/8.4.0", ("desktop", "Other", "Other")),
    ("", ("unknown", "Other", "Other")),
])
def test_parse_user_agent(ua, expected):
    got = us.parse_user_agent(ua)
    assert (got["device_type"], got["os"], got["browser"]) == expected


def test_active_ms_is_clamped():
    assert us._clamp_ms(-5) == 0 and us._clamp_ms("x") == 0 and us._clamp_ms(None) == 0
    assert us._clamp_ms(10**12) == us.MAX_ACTIVE_MS_PER_PING


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def _client(role="recruiter", email="r@x.com", **identity):
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email=email, role=role, **identity)
    return TestClient(app)


def test_client_ip_trusts_nginx_not_the_client(monkeypatch):
    seen = []
    monkeypatch.setattr(router_mod, "_ping_sync", lambda **kw: seen.append(kw["ip"]) or True)
    c = _client()
    body = {"session_id": str(uuid.uuid4())}
    # nginx overwrites X-Real-IP; X-Forwarded-For is appended to, so its
    # first entry is whatever the client sent.
    c.post("/api/v1/sessions/ping", json=body, headers={"X-Real-IP": "203.0.113.7",
                                                         "X-Forwarded-For": "6.6.6.6, 203.0.113.7"})
    c.post("/api/v1/sessions/ping", json=body, headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.8"})
    assert seen == ["203.0.113.7", "203.0.113.8"]


def test_ping_reports_ended_and_degrades_to_error(monkeypatch):
    c = _client(email=" R@X.com ")
    calls = []
    monkeypatch.setattr(router_mod, "_ping_sync", lambda **kw: calls.append(kw) or False)
    sid = str(uuid.uuid4())
    assert c.post("/api/v1/sessions/ping", json={"session_id": sid, "end": "logout"}).json() == {"status": "ended"}
    assert calls[0]["email"] == "r@x.com" and calls[0]["end_reason"] == "logout"
    assert c.post("/api/v1/sessions/ping", json={"session_id": "not-a-uuid"}).status_code == 422
    assert c.post("/api/v1/sessions/ping", json={"session_id": sid, "end": "kicked"}).status_code == 422

    def boom(**kw):
        raise RuntimeError("pool exhausted")
    monkeypatch.setattr(router_mod, "_ping_sync", boom)
    assert c.post("/api/v1/sessions/ping", json={"session_id": sid}).json() == {"status": "error"}
    assert _client(email="service-account").post(
        "/api/v1/sessions/ping", json={"session_id": sid}).json() == {"status": "ignored"}


def test_admin_read_is_scoped(monkeypatch):
    seen = []
    monkeypatch.setattr(router_mod, "_fetch_sync", lambda *a: seen.append(a) or [])
    monkeypatch.setattr(router_mod, "get_user_scope_emails", lambda u: {"lead@x.com", "mia@x.com"})
    url = "/api/v1/admin/user-sessions?start_date=2026-10-01&end_date=2026-10-07"

    # Recruiters: no access.
    assert _client().get(url).status_code == 403

    # Admins: any team, any user.
    admin = _client(role="admin", email="a@x.com")
    assert admin.get(url + "&team_id=t1&user=Zed@X.com").status_code == 200
    assert seen[-1][:2] == ("t1", "zed@x.com")

    # Team leads: pinned to their own team whatever they ask for, and only their people.
    lead = _client(role="team_lead", email="lead@x.com", team_id="t9", team_member_role="lead")
    assert lead.get(url + "&team_id=t1").status_code == 200
    assert seen[-1][:2] == ("t9", None)
    assert lead.get(url + "&user=MIA@x.com").status_code == 200
    assert seen[-1][:2] == ("t9", "mia@x.com")
    n = len(seen)
    assert lead.get(url + "&user=outsider@x.com").status_code == 403
    assert len(seen) == n  # refused before any read

    assert admin.get("/api/v1/admin/user-sessions?start_date=2026-10-08&end_date=2026-10-01").status_code == 400


# ---------------------------------------------------------------------------
# SQL (needs a Postgres; skipped otherwise)
# ---------------------------------------------------------------------------
@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            for stmt in us.SCHEMA_STATEMENTS:
                # TEMP survives ping()'s commit and vanishes with the connection.
                cur.execute(stmt.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE IF NOT EXISTS"))
        conn.commit()
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _row(conn, sid):
    with conn.cursor() as cur:
        cur.execute("SELECT user_email, active_ms, ended_at, end_reason, os FROM user_sessions WHERE id = %s", (sid,))
        return cur.fetchone()


def _fetch(conn, **kw):
    now = datetime.datetime.now(UTC)
    return us.fetch_sessions(conn, start=now - datetime.timedelta(days=1), end=now + datetime.timedelta(days=1), **kw)


def test_pg_ping_is_an_idempotent_upsert_and_logout_is_final(pg):
    sid = str(uuid.uuid4())
    win = "Mozilla/5.0 (Windows NT 10.0) Chrome/129.0 Safari/537.36"
    assert us.ping(pg, session_id=sid, email="r@x.com", active_ms=1000, user_agent=win)  # login
    assert us.ping(pg, session_id=sid, email="r@x.com", active_ms=2000)  # repeat: same row, time adds up
    assert _row(pg, sid) == ("r@x.com", 3000, None, None, "Windows")

    assert us.ping(pg, session_id=sid, email="r@x.com", active_ms=500, end_reason="logout")
    _, ms, ended_at, reason, _ = _row(pg, sid)
    assert (ms, reason) == (3500, "logout") and ended_at is not None

    # A repeated logout (another tab, a retried keepalive) or a late ping
    # changes nothing and tells the browser to mint a new session.
    assert not us.ping(pg, session_id=sid, email="r@x.com", active_ms=700, end_reason="logout")
    assert not us.ping(pg, session_id=sid, email="r@x.com", active_ms=700)
    assert _row(pg, sid) == ("r@x.com", 3500, ended_at, "logout", "Windows")


def test_pg_another_users_session_id_is_rejected(pg):
    sid = str(uuid.uuid4())
    assert us.ping(pg, session_id=sid, email="alice@x.com", active_ms=1000)
    assert not us.ping(pg, session_id=sid, email="mallory@x.com", active_ms=99_000)
    assert not us.ping(pg, session_id=sid, email="mallory@x.com", active_ms=0, end_reason="logout")
    assert _row(pg, sid)[:4] == ("alice@x.com", 1000, None, None)


def test_pg_read_closes_stale_sessions_at_their_last_ping(pg):
    live, stale, out = (str(uuid.uuid4()) for _ in range(3))
    for sid, who in ((live, "a@x.com"), (stale, "b@x.com"), (out, "c@x.com")):
        assert us.ping(pg, session_id=sid, email=who, active_ms=90_000)
    assert us.ping(pg, session_id=out, email="c@x.com", active_ms=0, end_reason="logout")
    with pg.cursor() as cur:
        cur.execute("UPDATE user_sessions SET started_at = NOW() - interval '1 hour', "
                    "last_seen_at = NOW() - interval '20 minutes' WHERE id = %s", (stale,))
    pg.commit()

    rows = {r["id"]: r for r in _fetch(pg)}
    assert rows[live]["is_active"] and rows[live]["logout_at"] is None and rows[live]["end_reason"] is None
    assert not rows[stale]["is_active"] and rows[stale]["end_reason"] == "closed"
    assert rows[stale]["logout_at"] == rows[stale]["last_seen_at"]
    assert rows[stale]["duration_seconds"] == 40 * 60 and rows[stale]["active_seconds"] == 90
    assert not rows[out]["is_active"] and rows[out]["end_reason"] == "logout"

    assert [r["user_email"] for r in _fetch(pg, emails=["b@x.com"])] == ["b@x.com"]
    assert _fetch(pg, emails=[]) == []
