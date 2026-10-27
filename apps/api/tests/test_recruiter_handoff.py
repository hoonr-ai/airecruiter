"""Recruiter warm handoff: the signed Pair-bot webhook, the join routes, and
the launch flag reaching Pair-bot.
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
    resp = client.post("/api/v1/webhooks/pairbot/handoff", content=raw, headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "ignored"


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


@pytest.mark.parametrize("status", [403, 404, 409, 410])
def test_pairbot_refusals_pass_through(as_recruiter, monkeypatch, status):
    async def refuse(*a):
        raise handoff.PairbotHandoffError(status, "nope")

    monkeypatch.setattr(handoff, "claim", refuse)
    resp = client.post("/api/v1/handoff/4242/claim")
    assert resp.status_code == status and resp.json()["detail"] == "nope"


@pytest.mark.parametrize("status", [401, 422, 500, 503])
def test_other_pairbot_failures_become_502(as_recruiter, monkeypatch, status):
    """A Pair-bot 401 (our service key) must not reach the browser, which
    would read it as the recruiter's own login expiring."""
    async def fail(*a):
        raise handoff.PairbotHandoffError(status, "internal detail")

    monkeypatch.setattr(handoff, "claim", fail)
    resp = client.post("/api/v1/handoff/4242/claim")
    assert resp.status_code == 502 and "internal detail" not in resp.text


def _serve_state(monkeypatch, state):
    async def fake_state(interview_id):
        return dict(state, interview_id=interview_id)

    monkeypatch.setattr(handoff, "state", fake_state)
    return client.get("/api/v1/handoff/4242").json()


def test_state_hides_who_owns_someone_elses_handoff(as_recruiter, monkeypatch):
    body = _serve_state(monkeypatch, {"state": "claimed", "recruiter_email": "other@example.com",
                                      "claimed_by_email": "other@example.com", "expires_at": "x"})
    assert body["is_your_handoff"] is False
    assert "recruiter_email" not in body and "claimed_by_email" not in body
    assert body["state"] == "claimed"


def test_state_strips_every_sensitive_field_not_just_the_two_emails(as_recruiter, monkeypatch):
    """PR review (IDOR): interview ids are sequential, and only
    recruiter_email/claimed_by_email were ever stripped from a non-owner's
    response — close_reason, close_notes (free text up to 5000 chars),
    claimed_at, and expires_at all still rode along in full. A non-owner
    must get back nothing but interview_id/is_your_handoff/state; the join
    page (joinViewFor in apps/web/lib/handoff.ts) never reads anything else
    once is_your_handoff is false."""
    body = _serve_state(monkeypatch, {
        "state": "closed", "recruiter_email": "other@example.com", "claimed_by_email": "other@example.com",
        "claimed_at": "2026-10-27T10:00:00Z", "expires_at": "2026-10-27T10:05:00Z",
        "close_reason": "recruiter_closed", "close_notes": "candidate seemed uninterested, flagged for review",
    })
    assert body == {"interview_id": 4242, "is_your_handoff": False, "state": "closed"}


def test_state_returns_the_full_object_only_to_the_confirmed_owner(as_recruiter, monkeypatch):
    body = _serve_state(monkeypatch, {
        "state": "closed", "recruiter_email": "rec@example.com", "claimed_by_email": "rec@example.com",
        "claimed_at": "2026-10-27T10:00:00Z", "close_notes": "great call",
    })
    assert body["is_your_handoff"] is True
    assert body["recruiter_email"] == "rec@example.com"
    assert body["close_notes"] == "great call"


def test_state_without_a_recruiter_is_unknown_not_refused(as_recruiter, monkeypatch):
    assert _serve_state(monkeypatch, {"state": None})["is_your_handoff"] is None
    assert _serve_state(monkeypatch, {"state": "closed", "recruiter_email": None})["is_your_handoff"] is None


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


@pytest.mark.parametrize("route", ["context", "transcript", "recording"])
def test_copilot_reads_proxy_as_the_login(as_recruiter, monkeypatch, route):
    calls = []

    async def fake(interview_id, email):
        calls.append((interview_id, email))
        return {"ok": route}

    monkeypatch.setattr(handoff, route, fake)
    resp = client.get(f"/api/v1/handoff/4242/{route}?recruiter_email=spoof@evil.com")
    assert resp.status_code == 200 and resp.json() == {"ok": route}
    assert calls == [(4242, "rec@example.com")]


@pytest.mark.parametrize("route", ["context", "transcript", "recording"])
def test_copilot_reads_map_errors_like_claim(as_recruiter, monkeypatch, route):
    async def not_deployed(*a):
        raise handoff.PairbotHandoffError(404, "Not Found")

    async def broken(*a):
        raise handoff.PairbotHandoffError(401, "service key")

    monkeypatch.setattr(handoff, route, not_deployed)
    assert client.get(f"/api/v1/handoff/4242/{route}").status_code == 404
    monkeypatch.setattr(handoff, route, broken)
    assert client.get(f"/api/v1/handoff/4242/{route}").status_code == 502


def _owned_by(monkeypatch, owner):
    async def fake_state(interview_id):
        return {"interview_id": interview_id, "state": "requested", "recruiter_email": owner}

    monkeypatch.setattr(handoff, "state", fake_state)


def test_resume_is_served_to_the_owning_recruiter(as_recruiter, monkeypatch):
    _owned_by(monkeypatch, "rec@example.com")
    monkeypatch.setattr(handoff, "resume_for_interview_sync",
                        lambda iid: {"candidate_name": "Ada", "resume_text": "Analytical engines"})
    body = client.get("/api/v1/handoff/4242/resume").json()
    assert body == {"interview_id": 4242, "candidate_name": "Ada", "resume_text": "Analytical engines"}


@pytest.mark.parametrize("owner", ["other@example.com", None])
def test_resume_is_refused_to_anyone_else(as_recruiter, monkeypatch, owner):
    _owned_by(monkeypatch, owner)
    monkeypatch.setattr(handoff, "resume_for_interview_sync",
                        lambda iid: pytest.fail("must not read the resume"))
    assert client.get("/api/v1/handoff/4242/resume").status_code == 403


def test_resume_404_when_pair_never_launched_it(as_recruiter, monkeypatch):
    _owned_by(monkeypatch, "rec@example.com")
    monkeypatch.setattr(handoff, "resume_for_interview_sync", lambda iid: None)
    assert client.get("/api/v1/handoff/4242/resume").status_code == 404


# --- post-handoff review: job access, read as the launch recruiter ---------------

@pytest.fixture
def review_env(as_recruiter, monkeypatch):
    import routers._helpers as helpers
    import routers.engagement.recordings as recordings

    env = {"audit": {"jobdiva_id": "26-1"}, "access": [], "deny": False, "reads": []}
    monkeypatch.setattr(recordings, "recording_audit_row", lambda iid: env["audit"])

    def verify(job_id, user):
        env["access"].append((job_id, user.email))
        if env["deny"]:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="no")

    monkeypatch.setattr(helpers, "verify_job_access_by_id", verify)

    async def fake_state(interview_id):
        return {"interview_id": interview_id, "state": "closed", "recruiter_email": "owner@example.com",
                "claimed_by_email": "owner@example.com", "close_reason": "recruiter_closed"}

    async def fake_transcript(interview_id, email):
        env["reads"].append(("transcript", email))
        return {"lines": [{"role": "candidate", "text": "hi", "seq": 1}], "state": "closed",
                "suggestions": [{"id": "s1", "t": 3, "items": [{"question": "Q?", "why": "W"}]}]}

    async def fake_recording(interview_id, email):
        env["reads"].append(("recording", email))
        raise handoff.PairbotHandoffError(404, "no recording")

    monkeypatch.setattr(handoff, "state", fake_state)
    monkeypatch.setattr(handoff, "transcript", fake_transcript)
    monkeypatch.setattr(handoff, "recording", fake_recording)
    return env


def test_review_lets_a_teammate_with_job_access_read_as_the_launch_recruiter(review_env):
    body = client.get("/api/v1/handoff/4242/review").json()
    assert review_env["access"] == [("26-1", " Rec@Example.com ")]
    assert sorted(review_env["reads"]) == [("recording", "owner@example.com"), ("transcript", "owner@example.com")]
    assert body["state"] == "closed" and body["claimed_by_email"] == "owner@example.com"
    assert body["transcript"]["suggestions"][0]["id"] == "s1"
    assert body["recording"] is None  # 404 = none
    assert body["transcript_error"] is False and body["recording_error"] is False


def test_review_flags_a_pairbot_failure_apart_from_absence(review_env, monkeypatch):
    async def broken(interview_id, email):
        raise handoff.PairbotHandoffError(503, "down")

    monkeypatch.setattr(handoff, "transcript", broken)
    body = client.get("/api/v1/handoff/4242/review").json()
    assert body["transcript"] is None and body["transcript_error"] is True
    assert body["recording"] is None and body["recording_error"] is False


def test_review_refuses_without_job_access(review_env):
    review_env["deny"] = True
    assert client.get("/api/v1/handoff/4242/review").status_code == 403
    assert review_env["reads"] == []


def test_review_404s_interviews_pair_never_launched(review_env):
    review_env["audit"] = None
    assert client.get("/api/v1/handoff/4242/review").status_code == 404
    assert review_env["access"] == []


def test_review_without_a_handoff_reads_nothing_else(review_env, monkeypatch):
    async def no_handoff(interview_id):
        return {"interview_id": interview_id, "state": None, "recruiter_handoff": False}

    monkeypatch.setattr(handoff, "state", no_handoff)
    body = client.get("/api/v1/handoff/4242/review").json()
    assert body["state"] is None and body["transcript"] is None and review_env["reads"] == []


def test_join_routes_require_login(monkeypatch):
    # A developer's own apps/api/.env commonly sets DEV_USER_EMAIL for local
    # convenience (core/auth.py's get_current_user falls back to it); loaded
    # at app import time, it otherwise leaks into this test's "no auth at
    # all" case on that one machine and silently resolves a user instead of
    # raising 401 — this guard is what makes the test actually exercise "no
    # login" everywhere, not just in CI (which has no such file).
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    app.dependency_overrides.pop(get_current_user, None)
    assert client.post("/api/v1/handoff/4242/claim").status_code in (401, 403)


# --- Pair-bot base URL (PR review: silently defaulted to QA in prod) --------

@pytest.mark.parametrize("env,expected", [
    ("production", "https://pairbot.hoonr.ai"),
    ("prod", "https://pairbot.hoonr.ai"),
    ("staging", "https://pairbotqa.hoonr.ai"),
    ("qa", "https://pairbotqa.hoonr.ai"),
    ("dev", "https://pairbotqa.hoonr.ai"),
    ("", "https://pairbotqa.hoonr.ai"),
])
def test_base_url_default_follows_environment(monkeypatch, env, expected):
    monkeypatch.delenv("EXTERNAL_INTERVIEW_API_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", env)
    assert handoff._base_url() == expected


def test_base_url_explicit_override_always_wins(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbot-staging.example.com/")
    assert handoff._base_url() == "https://pairbot-staging.example.com"


# --- notification field length cap (PR review: unvalidated payload fields) --

def test_apply_event_caps_candidate_name_and_role_in_notification_text(monkeypatch):
    inserted = []

    class _FakeCur:
        def execute(self, sql, params=None):
            inserted.append((sql, params))

        def fetchone(self):
            return (4242,) if "RETURNING" in inserted[-1][0] else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _FakeConn:
        def cursor(self):
            return _FakeCur()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(handoff, "get_db_connection", lambda: _FakeConn())
    huge_name = "A" * 10_000
    huge_role = "B" * 10_000
    handoff.apply_event(_event(candidate_name=huge_name, role_position=huge_role))
    # The notification insert's title/body params carry the capped text.
    notif_calls = [params for sql, params in inserted if "INSERT INTO notifications" in sql]
    assert notif_calls, "expected a notification insert"
    title, body = notif_calls[0][3], notif_calls[0][4]
    assert len(title) < len(huge_name)
    assert "A" * handoff._NOTIFICATION_FIELD_MAX_CHARS in title
    assert "A" * (handoff._NOTIFICATION_FIELD_MAX_CHARS + 1) not in title
    assert "B" * (handoff._NOTIFICATION_FIELD_MAX_CHARS + 1) not in body


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


@pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]"])
def test_pairbot_client_rejects_a_2xx_without_a_json_object(monkeypatch, body):
    import asyncio

    import httpx

    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    monkeypatch.setattr(handoff.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    with pytest.raises(handoff.PairbotHandoffError) as exc:
        asyncio.run(handoff.state(4242))
    assert exc.value.status == 502


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


def test_payload_flag_is_gated_on_a_recruiter_email(monkeypatch):
    """Only the launch's recruiter can join, so the flag goes out only
    alongside their email."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    req = eng.SendBulkInterviewRequest.model_construct(recruiter_handoff=True, handoff_timeout_sec=600)
    payload = {}
    eng._apply_handoff_fields(payload, req, "rec@example.com")
    assert payload == {"recruiter_handoff": True, "handoff_timeout_sec": 600}
    payload = {}
    eng._apply_handoff_fields(payload, req, None)
    assert payload == {}
    off = eng.SendBulkInterviewRequest.model_construct(recruiter_handoff=False, handoff_timeout_sec=None)
    payload = {}
    eng._apply_handoff_fields(payload, off, "rec@example.com")
    assert payload == {}


@pytest.mark.parametrize("env", ["production", "prod", " Production "])
def test_payload_flag_is_dropped_in_production(monkeypatch, caplog, env):
    """The UI hides handoff in prod; the API must not send it either."""
    monkeypatch.setenv("ENVIRONMENT", env)
    req = eng.SendBulkInterviewRequest.model_construct(recruiter_handoff=True, handoff_timeout_sec=600)
    payload = {}
    with caplog.at_level("WARNING"):
        eng._apply_handoff_fields(payload, req, "rec@example.com")
    assert payload == {}
    assert "production" in caplog.text


def test_payload_flag_is_sent_outside_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "qa")
    req = eng.SendBulkInterviewRequest.model_construct(recruiter_handoff=True, handoff_timeout_sec=None)
    payload = {}
    eng._apply_handoff_fields(payload, req, "rec@example.com")
    assert payload == {"recruiter_handoff": True}


def test_pairbot_reads_carry_the_recruiter_in_a_header_not_the_url(monkeypatch):
    import asyncio

    sent = []

    async def fake_call(method, path, body=None, headers=None):
        sent.append((method, path, headers))
        return {}

    monkeypatch.setattr(handoff, "_call", fake_call)
    for read in (handoff.context, handoff.transcript, handoff.recording):
        asyncio.run(read(4242, "rec@example.com"))
    assert [p for _, p, _ in sent] == [
        "/api/interviews/4242/handoff/context",
        "/api/interviews/4242/handoff/transcript",
        "/api/interviews/4242/handoff/recording",
    ]
    assert all(h == {"X-Recruiter-Email": "rec@example.com"} for _, _, h in sent)
    assert not any("recruiter_email" in p for _, p, _ in sent)


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
    # This fixture drops `notifications`: never against a real database.
    dbname = admin.get_dsn_parameters().get("dbname", "")
    if "test" not in dbname.lower():
        admin.close()
        pytest.fail(f"HANDOFF_TEST_DSN must point at a test database (got {dbname!r})")
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


def test_resume_lookup_maps_the_interview_to_the_launch_candidate(pg):
    with pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS engage_interview_audit, sourced_candidates")
        cur.execute("""CREATE TABLE engage_interview_audit (id SERIAL PRIMARY KEY, candidate_id TEXT,
                       jobdiva_id TEXT, interview_id TEXT, candidate_name TEXT)""")
        cur.execute("CREATE TABLE sourced_candidates (candidate_id TEXT, jobdiva_id TEXT, name TEXT, resume_text TEXT)")
        cur.execute("""INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, candidate_name)
                       VALUES ('c1', 'J1', '4242', 'Ada L')""")
        cur.execute("""INSERT INTO sourced_candidates VALUES ('c1', 'J0', 'Ada', 'other job resume'),
                       ('c1', 'J1', 'Ada Lovelace', 'launch job resume'), ('c2', 'J1', 'Bob', 'not hers')""")
    try:
        assert handoff.resume_for_interview_sync(4242) == {
            "candidate_name": "Ada Lovelace", "resume_text": "launch job resume"}
        assert handoff.resume_for_interview_sync(9999) is None
        # No resume on the launch's own job: never another job's.
        cur_conn = pg.cursor()
        cur_conn.execute("UPDATE sourced_candidates SET resume_text = NULL WHERE jobdiva_id = 'J1'")
        cur_conn.close()
        assert handoff.resume_for_interview_sync(4242) == {"candidate_name": "Ada Lovelace", "resume_text": None}
    finally:
        with pg.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS engage_interview_audit, sourced_candidates")
