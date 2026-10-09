"""Job-status poll: batched BI JobsDetail instead of 2 JobDiva calls per job."""
import asyncio
from unittest.mock import patch

import httpx
import pytest

from services import jobdiva_rate_limit as rl
from services.jobdiva import JobDivaService


@pytest.fixture(autouse=True)
def _fast_limiter(monkeypatch):
    monkeypatch.setattr(rl, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(rl, "_local_tokens", None)
    monkeypatch.setattr(rl, "_local_cooldown_until", 0.0)
    monkeypatch.setattr(rl, "_local_fg_waiting_until", 0.0)
    monkeypatch.setattr(rl, "_local_lock", None)


class _Client:
    def __init__(self, calls, rows=None, status=200):
        self.calls, self.rows, self.status = calls, rows, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kw):
        self.calls.append((url, params))
        ids = params["jobIds"]
        data = [r for r in (self.rows or []) if int(r["ID"]) in ids]
        return httpx.Response(self.status, json={"data": data}, request=httpx.Request("GET", url))


def _svc(job_status_calls):
    svc = JobDivaService.__new__(JobDivaService)
    svc.api_url = "https://api.jobdiva.com"

    async def auth(force_refresh=False):
        return "tok"

    async def get_job_status(job_id):
        job_status_calls.append(job_id)
        return {"job_id": job_id, "status": "OPEN"}

    svc.authenticate = auth
    svc.get_job_status = get_job_status
    return svc


def _run(svc, ids, local=None, rows=None, status=200):
    calls = []
    with patch("services.jobdiva.httpx.AsyncClient", lambda *a, **k: _Client(calls, rows, status)):
        out = asyncio.run(svc.get_multiple_jobs_status(ids, local))
    return out, calls


def test_one_request_per_50_jobs():
    per_job = []
    ids = [str(1000 + i) for i in range(120)]
    rows = [{"ID": i, "JOBSTATUS": "OPEN", "TITLE": "Dev", "COMPANYNAME": "ACME CORP"} for i in ids]
    out, calls = _run(_svc(per_job), ids, rows=rows)
    assert len(calls) == 3  # 50 + 50 + 20
    assert all(c[0].endswith("/apiv2/bi/JobsDetail") for c in calls)
    assert per_job == []
    assert out[0] == {"job_id": "1000", "status": "OPEN", "customer_name": "Acme Corp",
                      "title": "Dev", "synced_at": out[0]["synced_at"]}


def test_missing_or_failed_jobs_are_not_found_so_status_is_kept():
    out, _ = _run(_svc([]), ["1", "2"], rows=[{"ID": 1, "JOBSTATUS": "Closed"}])
    assert [o["status"] for o in out] == ["Closed", "NOT_FOUND"]
    out, _ = _run(_svc([]), ["1"], rows=[{"ID": 1, "JOBSTATUS": "Closed"}], status=500)
    assert out[0]["status"] == "NOT_FOUND"


def test_customer_falls_back_to_local_value():
    out, _ = _run(_svc([]), ["7"], local={"7": {"customer_name": "Bank Of America", "title": "T"}},
                  rows=[{"ID": 7, "JOBSTATUS": "OPEN"}])
    assert out[0]["customer_name"] == "Bank Of America" and out[0]["title"] == "T"


def test_reference_ids_fall_back_to_per_job_lookup():
    per_job = []
    out, calls = _run(_svc(per_job), ["26-06182"])
    assert calls == [] and per_job == ["26-06182"]
