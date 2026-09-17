"""Unit tests for the Live Report proxy router (routers/live_report.py)."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import HTTPException

from core.auth import UserIdentity
from routers import live_report as lr


def test_require_admin_or_team_lead_permits_admin():
    admin = UserIdentity(email="admin@example.com", role="admin")
    lr._require_admin_or_team_lead(admin)  # should not raise


def test_require_admin_or_team_lead_permits_team_lead():
    lead = UserIdentity(email="lead@example.com", role="team_lead", team_id="t1")
    lr._require_admin_or_team_lead(lead)  # should not raise


def test_require_admin_or_team_lead_rejects_recruiter():
    recruiter = UserIdentity(email="recruiter@example.com", role="recruiter")
    with pytest.raises(HTTPException) as exc:
        lr._require_admin_or_team_lead(recruiter)
    assert exc.value.status_code == 403


def test_get_live_report_launches_proxies_upstream():
    admin = UserIdentity(email="admin@example.com", role="admin")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"launches": [{"bulk_id": "b1", "state": "live"}]}

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        res = asyncio.run(lr.get_live_report_launches(user=admin))
        assert res == {"launches": [{"bulk_id": "b1", "state": "live"}]}


def test_get_live_report_snapshot_handles_not_found():
    admin = UserIdentity(email="admin@example.com", role="admin")
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(lr.get_live_report_snapshot(bulk_id="missing", reveal=False, user=admin))
        assert exc.value.status_code == 404

