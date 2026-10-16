"""Recruiter warm handoff: the signed Pair-bot webhook, the join routes, and
the launch flag reaching Pair-bot.

Contract: Pair-bot docs/recruiter-handoff-pair-contract.md.
"""

import hashlib
import hmac
import json
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

import routers.engagement as eng
from core.auth import UserIdentity, get_current_user
from main import app
from services import recruiter_handoff as handoff

client = TestClient(app)
SECRET = "handoff-test-secret"


def _signed(payload: dict) -> tuple[bytes, dict]:
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _event(event="handoff_requested", **extra):
    base = {
        "event": event, "interview_id": 4242, "bulk_id": "b-1", "jobdiva_id": "26-29759",
        "candidate_name": "Ada Lovelace", "role_position": "Backend Engineer",
        "recruiter_email": "Rec@Example.com", "cc_recruiter_emails": ["lead@example.com"],
        "state": "requested", "expires_at": "2026-10-16T14:07:11+00:00", "join_url": None,
    }
    base.update(extra)
    return base


@pytest.fixture
def signed_env(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("ENVIRONMENT", "production")


# --- webhook route ------------------------------------------------------------

def test_webhook_rejects_a_bad_signature(signed_env):
    raw, headers = _signed(_event())
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers).status_code == 401


def test_webhook_records_a_valid_event(signed_env, monkeypatch):
    seen = []
    monkeypatch.setattr(handoff, "apply_event", lambda data: seen.append(data) or "recorded")
    raw, headers = _signed(_event())
    resp = client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers)
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "duplicate": False}
    assert seen[0]["interview_id"] == 4242 and seen[0]["event"] == "handoff_requested"


def test_webhook_acknowledges_a_retry_as_duplicate(signed_env, monkeypatch):
    monkeypatch.setattr(handoff, "apply_event", lambda data: "duplicate")
    raw, headers = _signed(_event())
    assert client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers).json()["duplicate"] is True


def test_webhook_ignores_unknown_events_and_needs_an_interview(signed_env, monkeypatch):
    monkeypatch.setattr(handoff, "apply_event", lambda data: pytest.fail("must not record"))
    raw, headers = _signed(_event(event="something_else"))
    assert client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers).json()["status"] == "ignored"
    raw, headers = _signed(_event(interview_id=None))
    assert client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers).status_code == 422


def test_webhook_storage_failure_is_a_5xx_so_pairbot_retries(signed_env, monkeypatch):
    def boom(data):
        raise RuntimeError("db down")

    monkeypatch.setattr(handoff, "apply_event", boom)
    raw, headers = _signed(_event())
    assert client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers).status_code == 500


# --- join routes act as the logged-in recruiter ----------------------------------

@pytest.fixture
def as_recruiter():
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email=" Rec@Example.com ", role="recruiter")
    yield
    app.dependency_overrides.pop(get_current_user, None)


def test_claim_sends_the_login_email_not_a_body_field(as_recruiter, monkeypatch):
    calls = []

    async def fake_claim(interview_id, email):
        calls.append((interview_id, email))
        return {"livekit_url": "wss://lk", "token": "jwt", "room": "interview-4242_active", "state": "claimed"}

    monkeypatch.setattr(handoff, "claim", fake_claim)
    resp = client.post("/api/v1/handoff/4242/claim", json={"recruiter_email": "spoof@evil.com"})
    assert resp.status_code == 200 and resp.json()["token"] == "jwt"
    assert calls == [(4242, "rec@example.com")]


@pytest.mark.parametrize("status", [403, 409, 410])
def test_pairbot_refusals_pass_through(as_recruiter, monkeypatch, status):
    async def refuse(*a):
        raise handoff.PairbotHandoffError(status, "nope")

    monkeypatch.setattr(handoff, "claim", refuse)
    resp = client.post("/api/v1/handoff/4242/claim")
    assert resp.status_code == status and resp.json()["detail"] == "nope"


def test_close_and_state(as_recruiter, monkeypatch):
    closed = []

    async def fake_close(interview_id, email, notes):
        closed.append((interview_id, email, notes))
        return {"interview_id": interview_id, "state": "closed"}

    async def fake_state(interview_id):
        return {"interview_id": interview_id, "state": "requested", "recruiter_email": "rec@example.com"}

    monkeypatch.setattr(handoff, "close", fake_close)
    monkeypatch.setattr(handoff, "state", fake_state)
    assert client.post("/api/v1/handoff/4242/close", json={"notes": "great call"}).json()["state"] == "closed"
    assert closed == [(4242, "rec@example.com", "great call")]
    state = client.get("/api/v1/handoff/4242").json()
    assert state["is_your_handoff"] is True


def test_join_routes_require_login():
    app.dependency_overrides.pop(get_current_user, None)
    assert client.post("/api/v1/handoff/4242/claim").status_code in (401, 403)


def test_pairbot_client_maps_http_errors(monkeypatch):
    import asyncio

    import httpx

    def handler(request):
        assert request.headers.get("content-type") == "application/json"
        return httpx.Response(410, json={"detail": "The candidate is no longer waiting"})

    real = httpx.AsyncClient
    monkeypatch.setattr(handoff.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(handoff.PairbotHandoffError) as exc:
        asyncio.run(handoff.claim(4242, "rec@example.com"))
    assert exc.value.status == 410 and "no longer waiting" in exc.value.detail


# --- the launch flag reaches Pair-bot ----------------------------------------------

def test_launch_models_accept_and_bound_the_handoff_fields():
    from pydantic import ValidationError

    req = eng.LaunchRequest(job_id="J1", candidate_ids=["c"], recruiter_handoff=True, handoff_timeout_sec=300)
    assert req.recruiter_handoff is True and req.handoff_timeout_sec == 300
    assert eng.LaunchRequest(job_id="J1", candidate_ids=["c"]).recruiter_handoff is False
    with pytest.raises(ValidationError):
        eng.LaunchRequest(job_id="J1", candidate_ids=["c"], handoff_timeout_sec=30)
    from services.launch_sessions.models import LaunchOptions

    assert LaunchOptions(recruiter_handoff=True).model_dump()["recruiter_handoff"] is True


def test_payload_flag_is_gated_on_a_recruiter_email():
    """Mirrors the block in _send_bulk_interview_core: only the launch's
    recruiter can join, so the flag goes out only alongside their email."""
    import inspect

    src = inspect.getsource(eng._send_bulk_interview_core)
    gate = src.index("if request.recruiter_handoff and recruiter_email:")
    assert src.index('payload_obj["recruiter_handoff"] = True') > gate
    assert src.index('payload_obj["recruiter_email"] = recruiter_email') < gate


# --- webhook processing against a real Postgres (opt-in) ------------------------------

_DSN = os.getenv("HANDOFF_TEST_DSN")

_NOTIFICATIONS_DDL = """
CREATE TABLE notifications (
    id BIGSERIAL PRIMARY KEY, recipient_email TEXT NOT NULL, type TEXT NOT NULL,
    job_id TEXT, jobdiva_id TEXT NOT NULL, candidate_id TEXT, title TEXT NOT NULL, body TEXT,
    score NUMERIC, metadata JSONB DEFAULT '{}'::jsonb, read_at TIMESTAMPTZ, cleared_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@pytest.fixture
def pg(monkeypatch):
    if not _DSN:
        pytest.skip("HANDOFF_TEST_DSN not set")
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        admin = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres at {_DSN!r}: {exc}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS notifications, recruiter_handoff_events")
        cur.execute(_NOTIFICATIONS_DDL)

    @contextmanager
    def connect():
        conn = psycopg2.connect(_DSN)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(handoff, "get_db_connection", connect)
    handoff.init_recruiter_handoff_schema_sync()
    yield admin
    with admin.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS notifications, recruiter_handoff_events")
    admin.close()


def _notifications(admin):
    with admin.cursor() as cur:
        cur.execute("SELECT recipient_email, type, jobdiva_id, metadata, read_at FROM notifications ORDER BY id")
        return cur.fetchall()


def test_requested_notifies_only_the_joining_recruiter_once(pg):
    assert handoff.apply_event(_event()) == "recorded"
    assert handoff.apply_event(_event()) == "duplicate"
    rows = _notifications(pg)
    assert len(rows) == 1
    recipient, ntype, jobdiva_id, metadata, read_at = rows[0]
    assert recipient == "rec@example.com" and ntype == handoff.NOTIFY_REQUESTED
    assert jobdiva_id == "26-29759" and read_at is None
    assert metadata["interview_id"] == 4242 and metadata["join_path"] == "/handoff/4242"


def test_a_later_event_retires_the_join_now_notification(pg):
    handoff.apply_event(_event())
    handoff.apply_event(_event("handoff_claimed", state="claimed"))
    (_, _, _, _, read_at), = _notifications(pg)
    assert read_at is not None


def test_out_of_order_requested_after_closed_raises_no_join_now(pg):
    handoff.apply_event(_event("handoff_closed", state="closed"))
    handoff.apply_event(_event())
    assert _notifications(pg) == []


def test_missed_notifies_the_recruiter_and_the_cc_list(pg):
    handoff.apply_event(_event())
    handoff.apply_event(_event("handoff_missed", state="missed"))
    rows = _notifications(pg)
    missed = sorted(r[0] for r in rows if r[1] == handoff.NOTIFY_MISSED)
    assert missed == ["lead@example.com", "rec@example.com"]
    requested = [r for r in rows if r[1] == handoff.NOTIFY_REQUESTED]
    assert requested[0][4] is not None  # the join-now was retired
