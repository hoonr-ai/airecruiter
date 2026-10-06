import hashlib
import hmac
import json
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from main import app
from routers.dnc_webhook import _verify_hmac_signature

client = TestClient(app)


def test_dnc_webhook_signature_verification(monkeypatch):
    secret = "test-secret-key-12345"
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", secret)

    payload = b'{"event": "candidate.outreach_suppressed"}'
    valid_sig = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    assert _verify_hmac_signature(payload, valid_sig) is True
    assert _verify_hmac_signature(payload, "sha256=invalid") is False
    assert _verify_hmac_signature(payload, None) is False


def test_dnc_webhook_endpoint_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", "strict-secret")
    monkeypatch.setenv("ENVIRONMENT", "production")

    resp = client.post(
        "/api/v1/webhooks/dnc",
        content=b'{"event": "candidate.outreach_suppressed"}',
        headers={"X-Hub-Signature-256": "sha256=badbadbad"},
    )
    assert resp.status_code == 401


@patch("routers.dnc_webhook.suppress_contact_locally")
@patch("routers.dnc_webhook.get_db_connection")
def test_dnc_webhook_endpoint_success(mock_get_db, mock_suppress, monkeypatch):
    secret = "my-secret-key"
    monkeypatch.setenv("PAIR_WEBHOOK_SECRET", secret)

    mock_suppress.return_value = {
        "dnc_phone_added": True,
        "candidates_stopped": 1,
        "locally_suppressed": True,
        "phone": "15551234567",
        "email": "jane.doe@example.com",
        "error": None,
    }

    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_cur.rowcount = 1
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_get_db.return_value = mock_conn

    payload_dict = {
        "event": "candidate.outreach_suppressed",
        "interview_id": 9038,
        "candidate": {
            "name": "Jane Doe",
            "email": "jane.doe@example.com",
            "phone": "+15551234567",
        },
        "source": "hoonr",
        "suppression": {
            "is_suppressed": True,
            "blocked_channels": ["email", "sms", "call"],
            "scope": "hoonr",
            "trigger": "intake_check",
            "reasons": {
                "jane.doe@example.com": "Candidate previously unsubscribed"
            },
        },
        "message": "Candidate is on the DNC / Opt-Out suppression list. Automated outreach is suppressed.",
        "timestamp": "2026-09-25T10:14:36.585942Z",
    }
    raw_bytes = json.dumps(payload_dict).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()

    resp = client.post(
        "/api/v1/webhooks/dnc",
        content=raw_bytes,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["interview_id"] == 9038
    assert data["persisted_locally"] is True
    assert mock_suppress.called
