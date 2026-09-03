"""Do-not-contact endpoints (routers/outreach_optout.py).

The behaviours pinned here are the ones the 2026-09-02 incident turned on:

- pair-bot's ``message`` reaches the recruiter verbatim (it carries the
  "across N interviews" count and the cross-tenant enforcement note).
- Both email and phone go upstream whenever both are known, because pair-bot
  stores them as separate identities.
- A local DNC suppression is written alongside every successful stop, so a
  re-import does not re-launch the candidate.
- pair-bot being unreachable still writes the local suppression, but reports
  failure; a 4xx writes nothing.
- opt-in fails closed: if pair-bot refuses, the local suppression stands.

No DB and no network — the pair-bot client and the dnc_storage writers are
monkeypatched at the router's own namespace.
"""
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
from services.opt_out import PairBotOptOutError, normalize_channels
import routers.outreach_optout as mod


PAIRBOT_OK = {
    "success": True,
    "message": "Outreach stopped. Cancelled 6 pending job(s) across 2 interview(s).",
    "data": {
        "suppressed": [
            {"contact_type": "email", "contact_value": "ahmay02@gmail.com"},
            {"contact_type": "phone", "contact_value": "+15105908688"},
        ],
        "channels": ["call", "email", "sms"],
        "scope": "curate",
        "enforced_globally": ["call", "sms"],
        "cancelled": 6,
        "interview_ids": [7, 9],
    },
}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(mod.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(
        email="recruiter@pyramidci.com", role="recruiter"
    )
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    """Capture every upstream/local write instead of performing it."""
    recorded: Dict[str, List[Any]] = {
        "opt_out": [], "opt_in": [], "status": [],
        "suppress": [], "release": [], "audit": [],
    }

    async def fake_opt_out(**kwargs):
        recorded["opt_out"].append(kwargs)
        return PAIRBOT_OK

    async def fake_opt_in(**kwargs):
        recorded["opt_in"].append(kwargs)
        return {"success": True, "message": "Suppression lifted."}

    async def fake_status(**kwargs):
        recorded["status"].append(kwargs)
        return {"success": True, "message": "not suppressed", "data": {}}

    def fake_suppress(**kwargs):
        recorded["suppress"].append(kwargs)
        return {
            "dnc_phone_added": True,
            "candidates_stopped": 2,
            "locally_suppressed": True,
            "error": None,
        }

    def fake_release(**kwargs):
        recorded["release"].append(kwargs)
        return {"dnc_phone_removed": True, "candidates_released": 2, "error": None}

    def fake_audit(**kwargs):
        recorded["audit"].append(kwargs)

    def fake_local_status(**kwargs):
        return {"dnc_listed": False, "stopped_rows": 0, "error": None}

    monkeypatch.setattr(mod, "pairbot_opt_out", fake_opt_out)
    monkeypatch.setattr(mod, "pairbot_opt_in", fake_opt_in)
    monkeypatch.setattr(mod, "pairbot_opt_out_status", fake_status)
    monkeypatch.setattr(mod, "suppress_contact_locally", fake_suppress)
    monkeypatch.setattr(mod, "release_contact_locally", fake_release)
    monkeypatch.setattr(mod, "record_opt_out_audit", fake_audit)
    monkeypatch.setattr(mod, "local_suppression_status", fake_local_status)
    return recorded


# ---------------------------------------------------------------------------
# opt-out, happy path
# ---------------------------------------------------------------------------
def test_opt_out_passes_message_through_verbatim(client, calls):
    res = client.post(
        "/api/v1/outreach/opt-out",
        json={"email": "AhMay02@Gmail.com ", "phone": "(510) 590-8688",
              "reason": "Candidate replied STOP to the recruiter"},
    )
    assert res.status_code == 200
    body = res.json()
    # The "across 2 interview(s)" count is the part recruiters need; it must
    # not be recomposed locally.
    assert body["message"] == PAIRBOT_OK["message"]
    assert body["data"]["cancelled"] == 6
    assert body["data"]["interview_ids"] == [7, 9]
    assert body["data"]["enforced_globally"] == ["call", "sms"]


def test_opt_out_sends_both_identities_upstream(client, calls):
    client.post(
        "/api/v1/outreach/opt-out",
        json={"email": "ahmay02@gmail.com", "phone": "5105908688"},
    )
    sent = calls["opt_out"][0]
    # A suppression recorded against only the address will not match a Twilio
    # STOP that later arrives carrying only the number.
    assert sent["email"] == "ahmay02@gmail.com"
    assert sent["phone"] == "5105908688"


def test_opt_out_omits_channels_and_scope_by_default(client, calls):
    client.post("/api/v1/outreach/opt-out", json={"email": "a@b.com"})
    sent = calls["opt_out"][0]
    # Omitted, not spelled out: pair-bot defaults channels to all three and
    # scope to "curate". Sending our own guesses would freeze those defaults.
    assert sent["channels"] is None
    assert sent["scope"] is None


def test_opt_out_stamps_actor_into_upstream_reason(client, calls):
    client.post(
        "/api/v1/outreach/opt-out",
        json={"email": "a@b.com", "reason": "Asked on the phone"},
    )
    reason = calls["opt_out"][0]["reason"]
    assert "Asked on the phone" in reason
    assert "recruiter@pyramidci.com" in reason


def test_opt_out_writes_local_dnc(client, calls):
    res = client.post(
        "/api/v1/outreach/opt-out",
        json={"email": "a@b.com", "phone": "5105908688", "reason": "stop"},
    )
    assert res.status_code == 200
    assert len(calls["suppress"]) == 1
    assert calls["suppress"][0]["phone"] == "5105908688"
    assert calls["suppress"][0]["email"] == "a@b.com"
    assert res.json()["local"]["candidates_stopped"] == 2
    assert calls["audit"][0]["pairbot_ok"] is True


def test_opt_out_accepts_interview_id_alone(client, calls):
    res = client.post("/api/v1/outreach/opt-out", json={"interview_id": 7})
    assert res.status_code == 200
    assert calls["opt_out"][0]["interview_id"] == 7


def test_opt_out_forwards_explicit_scope_and_channels(client, calls):
    res = client.post(
        "/api/v1/outreach/opt-out",
        json={"email": "a@b.com", "scope": "global", "channels": ["SMS", "call", "sms"]},
    )
    assert res.status_code == 200
    sent = calls["opt_out"][0]
    assert sent["scope"] == "global"
    assert sent["channels"] == ["sms", "call"]


# ---------------------------------------------------------------------------
# opt-out, validation and failure
# ---------------------------------------------------------------------------
def test_opt_out_without_identifier_is_422(client, calls):
    res = client.post("/api/v1/outreach/opt-out", json={"reason": "stop"})
    assert res.status_code == 422
    assert not calls["opt_out"]
    assert not calls["suppress"]


def test_opt_out_unknown_channel_is_422(client, calls):
    res = client.post(
        "/api/v1/outreach/opt-out",
        json={"email": "a@b.com", "channels": ["carrier-pigeon"]},
    )
    assert res.status_code == 422
    assert "carrier-pigeon" in res.json()["detail"]
    # A dropped channel is a channel that keeps sending — nothing partial ran.
    assert not calls["opt_out"]
    assert not calls["suppress"]


def test_opt_out_client_error_writes_nothing_locally(client, calls, monkeypatch):
    async def bad_request(**kwargs):
        raise PairBotOptOutError("interview 7 does not exist", status_code=404)

    monkeypatch.setattr(mod, "pairbot_opt_out", bad_request)
    res = client.post("/api/v1/outreach/opt-out", json={"interview_id": 7})
    assert res.status_code == 404
    # Nothing was suppressed upstream, so nothing is suppressed here either.
    assert not calls["suppress"]
    assert calls["audit"][0]["pairbot_ok"] is False


def test_opt_out_unreachable_pairbot_still_suppresses_locally(client, calls, monkeypatch):
    async def unreachable(**kwargs):
        raise PairBotOptOutError("connection refused")

    monkeypatch.setattr(mod, "pairbot_opt_out", unreachable)
    res = client.post(
        "/api/v1/outreach/opt-out", json={"email": "a@b.com", "phone": "5105908688"}
    )
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "retry" in detail.lower()
    # pair cannot cancel the queued sends, but it can refuse to add more — and
    # the recruiter is told exactly that rather than a bare failure.
    # The default fake reports a successful local write, so the claim is earned.
    assert len(calls["suppress"]) == 1
    assert "no new outreach will be launched" in detail


def test_unreachable_pairbot_does_not_claim_a_suppression_it_did_not_make(
    client, calls, monkeypatch
):
    """The interview_id-only path has nothing to write locally: the local DNC
    list is keyed on contact details. Claiming "marked do-not-contact in pair"
    here would read as a partial success and invite the recruiter to treat a
    candidate who is still being called as handled."""

    async def unreachable(**kwargs):
        raise PairBotOptOutError("connection refused")

    monkeypatch.setattr(mod, "pairbot_opt_out", unreachable)
    monkeypatch.setattr(
        mod, "suppress_contact_locally",
        lambda **kw: {
            "dnc_phone_added": False,
            "candidates_stopped": 0,
            "locally_suppressed": False,
            "error": "no normalizable phone or email",
        },
    )
    res = client.post("/api/v1/outreach/opt-out", json={"interview_id": 7})
    assert res.status_code == 502
    detail = res.json()["detail"]
    assert "still being contacted" in detail
    assert "marked do-not-contact" not in detail


def test_unreachable_pairbot_reports_failed_local_write_honestly(
    client, calls, monkeypatch
):
    async def unreachable(**kwargs):
        raise PairBotOptOutError("connection refused")

    monkeypatch.setattr(mod, "pairbot_opt_out", unreachable)
    monkeypatch.setattr(
        mod, "suppress_contact_locally",
        lambda **kw: {
            "dnc_phone_added": False,
            "candidates_stopped": 0,
            "locally_suppressed": False,
            "error": "db down",
        },
    )
    res = client.post(
        "/api/v1/outreach/opt-out", json={"email": "a@b.com", "phone": "5105908688"}
    )
    assert res.status_code == 502
    assert "marked do-not-contact" not in res.json()["detail"]


def test_opt_in_unreachable_pairbot_keeps_the_shared_status_mapping(
    client, calls, monkeypatch
):
    """opt-in routes through the same _pairbot_http_error helper as opt-out, so
    a 403 collapses to 401 on both."""

    async def forbidden(**kwargs):
        raise PairBotOptOutError("missing api key", status_code=403)

    monkeypatch.setattr(mod, "pairbot_opt_in", forbidden)
    res = client.post("/api/v1/outreach/opt-in", json={"email": "a@b.com"})
    assert res.status_code == 401
    assert not calls["release"]


def test_email_is_lowercased_before_going_upstream(client, calls):
    client.post("/api/v1/outreach/opt-out", json={"email": "  AhMay02@Gmail.COM "})
    # pair-bot normalizes case itself, but sending the canonical form means the
    # two sides agree regardless of what the recruiter's row happened to hold.
    assert calls["opt_out"][0]["email"] == "ahmay02@gmail.com"


def test_unreachable_pairbot_credits_an_already_standing_local_suppression(
    client, calls, monkeypatch
):
    """A second click, or a candidate already on the imported DNC list, changes
    no rows. "Nothing was suppressed" would be wrong — they are suppressed
    here; it is only pair-bot's queued sends that are unaccounted for."""

    async def unreachable(**kwargs):
        raise PairBotOptOutError("connection refused")

    monkeypatch.setattr(mod, "pairbot_opt_out", unreachable)
    monkeypatch.setattr(
        mod, "suppress_contact_locally",
        lambda **kw: {
            "dnc_phone_added": False,
            "candidates_stopped": 0,
            "locally_suppressed": True,
            "error": None,
        },
    )
    res = client.post("/api/v1/outreach/opt-out", json={"phone": "5105908688"})
    assert res.status_code == 502
    assert "marked do-not-contact in pair" in res.json()["detail"]


def test_opt_out_bad_api_key_surfaces_as_401(client, calls, monkeypatch):
    async def forbidden(**kwargs):
        raise PairBotOptOutError("missing api key", status_code=403)

    monkeypatch.setattr(mod, "pairbot_opt_out", forbidden)
    res = client.post("/api/v1/outreach/opt-out", json={"email": "a@b.com"})
    assert res.status_code == 401
    assert not calls["suppress"]


def test_opt_out_survives_local_write_failure(client, calls, monkeypatch):
    def broken(**kwargs):
        return {
            "dnc_phone_added": False,
            "candidates_stopped": 0,
            "locally_suppressed": False,
            "error": "db down",
        }

    monkeypatch.setattr(mod, "suppress_contact_locally", broken)
    res = client.post("/api/v1/outreach/opt-out", json={"email": "a@b.com"})
    # Outreach IS stopped upstream; bookkeeping trouble must not read as
    # "still calling".
    assert res.status_code == 200
    assert res.json()["success"] is True
    assert res.json()["local"]["error"] == "db down"


# ---------------------------------------------------------------------------
# candidate_id resolution
# ---------------------------------------------------------------------------
def test_candidate_id_resolves_contact_details(client, calls, monkeypatch):
    monkeypatch.setattr(
        mod, "_resolve_contact",
        lambda cid: {"email": "found@x.com", "phone": "5105908688", "name": "A"},
    )
    res = client.post("/api/v1/outreach/opt-out", json={"candidate_id": "c-1"})
    assert res.status_code == 200
    assert calls["opt_out"][0]["email"] == "found@x.com"
    assert calls["opt_out"][0]["phone"] == "5105908688"


def test_caller_supplied_contact_wins_over_lookup(client, calls, monkeypatch):
    monkeypatch.setattr(
        mod, "_resolve_contact",
        lambda cid: {"email": "stale@x.com", "phone": "5105908688", "name": "A"},
    )
    client.post(
        "/api/v1/outreach/opt-out",
        json={"candidate_id": "c-1", "email": "typed@x.com"},
    )
    sent = calls["opt_out"][0]
    assert sent["email"] == "typed@x.com"
    # …and the lookup still fills in the half the caller did not have.
    assert sent["phone"] == "5105908688"


def test_candidate_id_with_no_contact_on_file_is_422(client, calls, monkeypatch):
    monkeypatch.setattr(
        mod, "_resolve_contact",
        lambda cid: {"email": None, "phone": None, "name": None},
    )
    res = client.post("/api/v1/outreach/opt-out", json={"candidate_id": "c-1"})
    assert res.status_code == 422
    assert not calls["opt_out"]


# ---------------------------------------------------------------------------
# opt-in
# ---------------------------------------------------------------------------
def test_opt_in_releases_after_pairbot_accepts(client, calls):
    res = client.post(
        "/api/v1/outreach/opt-in",
        json={"email": "a@b.com", "phone": "5105908688", "reason": "Candidate called back"},
    )
    assert res.status_code == 200
    assert len(calls["release"]) == 1
    # Omitted scope: pair-bot clears every scope, which is what the candidate
    # themselves asking to be contacted again means.
    assert calls["opt_in"][0]["scope"] is None


def test_opt_in_never_sends_interview_id(client, calls):
    client.post("/api/v1/outreach/opt-in", json={"email": "a@b.com"})
    assert "interview_id" not in calls["opt_in"][0]


def test_opt_in_fails_closed_when_pairbot_unreachable(client, calls, monkeypatch):
    async def unreachable(**kwargs):
        raise PairBotOptOutError("timeout")

    monkeypatch.setattr(mod, "pairbot_opt_in", unreachable)
    res = client.post("/api/v1/outreach/opt-in", json={"email": "a@b.com"})
    assert res.status_code == 502
    assert "still suppressed" in res.json()["detail"]
    # Failing closed: nobody gets contacted while the two sides disagree.
    assert not calls["release"]


def test_opt_in_without_identifier_is_422(client, calls):
    res = client.post("/api/v1/outreach/opt-in", json={"reason": "why not"})
    assert res.status_code == 422
    assert not calls["opt_in"]


def test_opt_in_flags_retained_imported_dnc(client, calls, monkeypatch):
    monkeypatch.setattr(
        mod, "release_contact_locally",
        lambda **kw: {
            "dnc_phone_removed": False,
            "dnc_phone_retained_other_source": True,
            "candidates_released": 0,
            "error": None,
        },
    )
    res = client.post("/api/v1/outreach/opt-in", json={"phone": "5105908688"})
    assert res.status_code == 200
    # A clean "lifted" here would read as "we can call them now" — the Zoom
    # DNC list still says otherwise.
    assert "Do-Not-Contact list" in res.json()["message"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_reports_both_sides(client, calls):
    res = client.get("/api/v1/outreach/opt-out", params={"email": "a@b.com"})
    assert res.status_code == 200
    body = res.json()
    assert "pairbot" in body and "local" in body


def test_status_degrades_to_200_when_pairbot_down(client, calls, monkeypatch):
    async def unreachable(**kwargs):
        raise PairBotOptOutError("connection refused")

    monkeypatch.setattr(mod, "pairbot_opt_out_status", unreachable)
    res = client.get("/api/v1/outreach/opt-out", params={"email": "a@b.com"})
    # The local half of the answer is still worth rendering.
    assert res.status_code == 200
    assert res.json()["pairbot"]["error"]
    assert "local" in res.json()


def test_status_without_identifier_is_422(client, calls):
    assert client.get("/api/v1/outreach/opt-out").status_code == 422


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
def test_every_route_is_authenticated():
    """No global auth middleware exists in this app — each route carries its
    own Depends(get_current_user), so a new unguarded route is a silent hole."""
    import ast
    from pathlib import Path

    src = (Path(mod.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    routes = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorated = any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and isinstance(d.func.value, ast.Name)
            and d.func.value.id == "router"
            for d in node.decorator_list
        )
        if not decorated:
            continue
        routes += 1
        body = ast.get_source_segment(src, node) or ""
        signature = body[: body.find("):")]
        assert "get_current_user" in signature, f"{node.name} has no auth guard"
    assert routes == 3


# ---------------------------------------------------------------------------
# channel normalization
# ---------------------------------------------------------------------------
def test_normalize_channels_omits_when_empty():
    # None means "send no channels field", so pair-bot's all-three default wins.
    assert normalize_channels(None) is None
    assert normalize_channels([]) is None
    assert normalize_channels(["", "  "]) is None


def test_normalize_channels_lowercases_and_dedupes():
    assert normalize_channels([" Email ", "EMAIL", "call"]) == ["email", "call"]


def test_normalize_channels_rejects_unknown():
    with pytest.raises(ValueError):
        normalize_channels(["email", "fax"])
