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
        assert len(res["launches"]) == 2
        assert res["retention_days"] == 14


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
            assert len(res["launches"]) == 1
            assert res["launches"][0]["bulk_id"] == "b1"
            assert res["retention_days"] == 14


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


def test_get_live_report_health_reports_unhealthy_on_network_error():
    admin = UserIdentity(email="admin@example.com", role="admin")
    import httpx

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=httpx.RequestError("down")):
        res = asyncio.run(lr.get_live_report_health(user=admin))
        assert res["healthy"] is False
        assert "unavailable" in res["error"]


def test_invalid_bulk_id_rejected():
    admin = UserIdentity(email="admin@example.com", role="admin")
    mock_request = MagicMock()

    with pytest.raises(HTTPException) as exc1:
        asyncio.run(lr.get_live_report_snapshot(bulk_id="../../etc/passwd", reveal=False, user=admin))
    assert exc1.value.status_code == 400

    with pytest.raises(HTTPException) as exc2:
        asyncio.run(lr.stream_live_report(bulk_id="bad id with spaces!", request=mock_request, user=admin))
    assert exc2.value.status_code == 400


def test_stream_live_report_recruiter_prefetch_failure_raises_503():
    recruiter = UserIdentity(email="recruiter@example.com", role="recruiter")
    mock_request = MagicMock()

    # Recruiter has accessible job
    with patch("routers.live_report._get_user_accessible_jobdiva_ids", return_value={"26-11111"}):
        mock_resp = MagicMock()
        mock_resp.status_code = 500  # Upstream prefetch fails

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(lr.stream_live_report(bulk_id="b1", request=mock_request, user=recruiter))
            assert exc.value.status_code == 503
            assert "security context" in exc.value.detail


def test_stream_live_report_recruiter_filters_unassigned_events():
    recruiter = UserIdentity(email="recruiter@example.com", role="recruiter")
    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)

    # 1. Mock snapshot prefetch returning one job with interview_id 100
    snap_resp = MagicMock()
    snap_resp.status_code = 200
    snap_resp.json.return_value = {
        "jobs": [
            {"jobdiva_id": "26-11111", "candidates": [{"interview_id": 100}]},
            {"jobdiva_id": "26-99999", "candidates": [{"interview_id": 200}]},
        ]
    }

    # 2. Mock upstream stream yielding two events (one for id 100, one for id 200)
    async def mock_aiter_lines():
        yield ': ping'
        yield 'data: {"interview_id": 100, "type": "outreach_attempted"}'
        yield 'data: {"interview_id": 200, "type": "outreach_attempted"}'

    upstream_mock = MagicMock()
    upstream_mock.status_code = 200
    upstream_mock.aiter_lines = mock_aiter_lines

    class MockStreamContext:
        async def __aenter__(self):
            return upstream_mock
        async def __aexit__(self, *args):
            pass

    with patch("routers.live_report._get_user_accessible_jobdiva_ids", return_value={"26-11111"}):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=snap_resp):
            with patch("httpx.AsyncClient.stream", return_value=MockStreamContext()):
                resp = asyncio.run(lr.stream_live_report(bulk_id="b1", request=mock_request, user=recruiter))

                # Collect streamed body chunks
                async def read_stream():
                    chunks = []
                    async for chunk in resp.body_iterator:
                        chunks.append(chunk.decode("utf-8"))
                    return "".join(chunks)

                stream_content = asyncio.run(read_stream())
                assert ": ping" in stream_content
                assert "100" in stream_content
                assert "200" not in stream_content  # 200 should be filtered out!
