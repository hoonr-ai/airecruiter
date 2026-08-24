"""
Unit tests for recruiter analytics scoping and UserIdentity properties.
Verifies that:
  (a) UserIdentity correctly resolves is_recruiter, is_admin, and is_team_lead.
  (b) _load_recruiter_scope accurately extracts job_ids and sc_keys assigned to a recruiter's email.
  (c) Recruiter access pins scope to user.email.
"""
import sys
import types
from unittest.mock import MagicMock

# Stub DB drivers
for mod in ["pg8000", "pg8000.dbapi", "google", "google.cloud", "google.cloud.sql", "google.cloud.sql.connector"]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Attach dummy core.db module to core package so conftest.py monkeypatch succeeds
if "core.db" not in sys.modules:
    dummy_db = types.ModuleType("core.db")
    dummy_db.get_db_connection = MagicMock()
    sys.modules["core.db"] = dummy_db
    import core
    core.db = dummy_db

if "routers._helpers" not in sys.modules:
    dummy_helpers = types.ModuleType("routers._helpers")
    dummy_helpers.get_db_connection = MagicMock()
    sys.modules["routers._helpers"] = dummy_helpers

from core.auth import UserIdentity
from routers.admin_analytics import _parse_recruiter_emails, _load_recruiter_scope


def test_user_identity_roles():
    admin = UserIdentity(email="admin@hoonr.ai", role="admin")
    assert admin.is_admin is True
    assert admin.is_team_lead is False
    assert admin.is_recruiter is False

    lead = UserIdentity(email="lead@hoonr.ai", role="team_lead", team_id="team-1")
    assert lead.is_admin is False
    assert lead.is_team_lead is True
    assert lead.is_recruiter is False

    recruiter = UserIdentity(email="recruiter@hoonr.ai", role="recruiter")
    assert recruiter.is_admin is False
    assert recruiter.is_team_lead is False
    assert recruiter.is_recruiter is True


def test_parse_recruiter_emails():
    raw_json = '["Alice@Hoonr.ai", "bob@hoonr.ai"]'
    assert _parse_recruiter_emails(raw_json) == ["alice@hoonr.ai", "bob@hoonr.ai"]

    raw_single = "charlie@hoonr.ai"
    assert _parse_recruiter_emails(raw_single) == ["charlie@hoonr.ai"]

    raw_empty = ""
    assert _parse_recruiter_emails(raw_empty) == []


class DummyCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def execute(self, query, params=None):
        pass

    def fetchall(self):
        return self.rows


class DummyConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return DummyCursor(self.rows)


def test_load_recruiter_scope():
    dummy_jobs = [
        ("job-uuid-1", "DIVA-101", '["recruiter1@hoonr.ai", "recruiter2@hoonr.ai"]'),
        ("job-uuid-2", "DIVA-102", '["recruiter2@hoonr.ai"]'),
        ("job-uuid-3", "DIVA-103", '["recruiter3@hoonr.ai"]'),
    ]
    conn = DummyConnection(dummy_jobs)

    scope1 = _load_recruiter_scope(conn, "recruiter1@hoonr.ai")
    assert scope1["recruiter_email"] == "recruiter1@hoonr.ai"
    assert scope1["job_ids"] == ["job-uuid-1"]
    assert sorted(scope1["sc_keys"]) == ["DIVA-101", "job-uuid-1"]

    scope2 = _load_recruiter_scope(conn, "recruiter2@hoonr.ai")
    assert scope2["recruiter_email"] == "recruiter2@hoonr.ai"
    assert scope2["job_ids"] == ["job-uuid-1", "job-uuid-2"]
    assert sorted(scope2["sc_keys"]) == ["DIVA-101", "DIVA-102", "job-uuid-1", "job-uuid-2"]

    scope_none = _load_recruiter_scope(conn, "nobody@hoonr.ai")
    assert scope_none["job_ids"] == []
    assert scope_none["sc_keys"] == []
