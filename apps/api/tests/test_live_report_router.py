"""Unit tests for the Live Report proxy router (routers/live_report.py)."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import HTTPException

from core.auth import UserIdentity
from routers import live_report as lr


def test_get_live_report_launches_admin_sees_all():
    admin = UserIdentity(email="admin@example.com", role="admin")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"bulk_id": "b1", "state": "live", "jobdiva_ids": ["26-11111"]},
        {"bulk_id": "b2", "state": "live", "jobdiva_ids": ["26-22222"]},
    ]

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        res = asyncio.run(lr.get_live_report_launches(user=admin))
        assert len(res) == 2


def test_get_live_report_launches_recruiter_scoped_isolation():
    recruiter = UserIdentity(email="recruiter@example.com", role="recruiter")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"bulk_id": "b1", "state": "live", "jobdiva_ids": ["26-11111"]},
        {"bulk_id": "b2", "state": "live", "jobdiva_ids": ["26-22222"]},
    ]

    # Recruiter only has access to job 26-11111
    with patch("routers.live_report._get_user_accessible_jobdiva_ids", return_value={"26-11111"}):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res = asyncio.run(lr.get_live_report_launches(user=recruiter))
            assert len(res) == 1
            assert res[0]["bulk_id"] == "b1"


def test_get_live_report_snapshot_admin_sees_all_jobs():
    admin = UserIdentity(email="admin@example.com", role="admin")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "bulk_id": "b1",
        "jobs": [
            {"jobdiva_id": "26-11111", "title": "Job 1", "candidates": []},
            {"jobdiva_id": "26-22222", "title": "Job 2", "candidates": []},
        ],
    }

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        res = asyncio.run(lr.get_live_report_snapshot(bulk_id="b1", reveal=False, user=admin))
        assert len(res["jobs"]) == 2


def test_get_live_report_snapshot_recruiter_filters_unassigned_jobs():
    recruiter = UserIdentity(email="recruiter@example.com", role="recruiter")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "bulk_id": "b1",
        "jobs": [
            {"jobdiva_id": "26-11111", "title": "Job 1", "candidates": []},
            {"jobdiva_id": "26-22222", "title": "Job 2", "candidates": []},
        ],
    }

    # Recruiter only has access to 26-11111
    with patch("routers.live_report._get_user_accessible_jobdiva_ids", return_value={"26-11111"}):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            res = asyncio.run(lr.get_live_report_snapshot(bulk_id="b1", reveal=False, user=recruiter))
            assert len(res["jobs"]) == 1
            assert res["jobs"][0]["jobdiva_id"] == "26-11111"


def test_get_live_report_snapshot_recruiter_rejected_if_zero_jobs():
    recruiter = UserIdentity(email="recruiter@example.com", role="recruiter")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "bulk_id": "b1",
        "jobs": [
            {"jobdiva_id": "26-99999", "title": "Job 99", "candidates": []},
        ],
    }

    with patch("routers.live_report._get_user_accessible_jobdiva_ids", return_value={"26-11111"}):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(lr.get_live_report_snapshot(bulk_id="b1", reveal=False, user=recruiter))
            assert exc.value.status_code == 403


def test_get_live_report_snapshot_handles_not_found():
    admin = UserIdentity(email="admin@example.com", role="admin")
    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(lr.get_live_report_snapshot(bulk_id="missing", reveal=False, user=admin))
        assert exc.value.status_code == 404
