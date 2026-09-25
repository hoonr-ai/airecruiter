"""Admin endpoint that repairs the JobDiva profiles PAIR created blank.

POST /engage/jobdiva-blank-profile-backfill: admin-only, dry run by default,
bounded per call, and each settled profile is stamped so repeated calls walk
the list (failures are not stamped -- the next call retries them).
"""
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.engagement as eng
import services.jobdiva_profile_backfill as bf
from core.auth import UserIdentity, get_current_user

ROWS = [
    {"candidate_id": "exa_linkedin.com/in/ada", "source": "LinkedIn-Exa", "data": {"jobdiva_candidate_id": "111"}},
    {"candidate_id": "exa_linkedin.com/in/grace", "source": "LinkedIn-Exa", "data": {"jobdiva_candidate_id": "222"}},
]


class _Cursor:
    def __init__(self, log):
        self._log = log

    def execute(self, sql, params=None):
        self._log.append({"sql": " ".join(sql.split()), "params": params})

    def fetchall(self):
        return ROWS

    def close(self):
        pass


class _Conn:
    def __init__(self, log):
        self._log = log

    def cursor(self, **_kw):
        return _Cursor(self._log)

    def commit(self):
        self._log.append({"commit": True})

    def close(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    log: List[Dict[str, Any]] = []
    calls: List[Dict[str, Any]] = []
    outcomes = {"111": "repaired", "222": "failed_resumes_not_readable"}

    async def _backfill(row, *, apply=False, service=None):
        jd = row["data"]["jobdiva_candidate_id"]
        calls.append({"jobdiva_id": jd, "apply": apply})
        status = outcomes[jd] if apply else "planned"
        return {"jobdiva_candidate_id": jd, "apply": apply, "status": status,
                "email_held_by_another_profile": jd == "111"}

    async def _job_ids(job):
        return 31920032, "26-1"

    monkeypatch.setattr(eng, "_get_db_connection", lambda: _Conn(log))
    monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", _job_ids)
    monkeypatch.setattr(bf, "backfill_blank_profile", _backfill)
    monkeypatch.setattr(eng, "_utc_now_iso", lambda: "2026-09-25T12:00:00+00:00")
    return {"log": log, "calls": calls}


def _client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(eng.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="who@pyramidci.com", role=role)
    return TestClient(app)


def _stamps(log):
    return [e for e in log if "sql" in e and e["sql"].startswith("UPDATE sourced_candidates")]


def test_non_admin_is_refused(harness):
    res = _client("recruiter").post("/engage/jobdiva-blank-profile-backfill", json={})
    assert res.status_code == 403
    assert harness["calls"] == []


def test_dry_run_is_the_default_and_writes_nothing(harness):
    res = _client("admin").post("/engage/jobdiva-blank-profile-backfill", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dry_run"] is True and body["statuses"] == {"planned": 2}
    assert [c["apply"] for c in harness["calls"]] == [False, False]
    assert _stamps(harness["log"]) == []


def test_apply_stamps_settled_profiles_and_leaves_failures_for_a_retry(harness):
    res = _client("admin").post(
        "/engage/jobdiva-blank-profile-backfill", json={"dry_run": False, "limit": 500, "job_id": "26-1"},
    )
    body = res.json()
    assert body["processed"] == 2
    assert body["statuses"] == {"repaired": 1, "failed_resumes_not_readable": 1}
    assert body["likely_duplicates"] == ["111"]

    select = next(e for e in harness["log"] if "sql" in e and e["sql"].startswith("SELECT"))
    assert select["params"]["limit"] == 50  # bounded
    assert select["params"]["job_ids"] == ["26-1", "31920032"]
    assert select["params"]["include_checked"] is False  # batches walk the list

    (stamp,) = _stamps(harness["log"])
    assert stamp["params"]["jobdiva_id"] == "111"
    assert '"jobdiva_backfill_status": "repaired"' in stamp["params"]["delta"]


def test_named_profiles_are_processed_even_when_already_checked(harness):
    _client("admin").post("/engage/jobdiva-blank-profile-backfill", json={"jobdiva_ids": ["111"]})
    select = next(e for e in harness["log"] if "sql" in e and e["sql"].startswith("SELECT"))
    assert select["params"]["jobdiva_ids"] == ["111"]
    assert select["params"]["include_checked"] is True
