"""Admin audit + repair endpoints for the JobDiva profile-identity invariant."""
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
import routers.engagement as eng


class _Cursor:
    def __init__(self, store):
        self._store = store
        self.rowcount = 3

    def execute(self, sql, params=None):
        self._store.append({"sql": " ".join(sql.split()), "params": params})

    def fetchone(self):
        return {"n": 2}

    def fetchall(self):
        return [
            {"job_id": "26-1", "real_profile_id": "462058065251", "duplicate_profile_id": "999000111",
             "source": "JobDiva-JobAgent", "name": "Ada Lovelace", "updated_at": None},
            {"job_id": "26-1", "real_profile_id": "462058065252", "duplicate_profile_id": "999000112",
             "source": "JobDiva-TalentSearch", "name": "Grace Hopper", "updated_at": None},
        ]

    def close(self):
        pass


class _Conn:
    def __init__(self, store):
        self._store = store
        self.committed = False

    def cursor(self, **_kw):
        return _Cursor(self._store)

    def commit(self):
        self.committed = True

    def close(self):
        pass


@pytest.fixture
def sql_log(monkeypatch):
    store: List[Dict[str, Any]] = []
    conns: List[_Conn] = []

    def _conn():
        c = _Conn(store)
        conns.append(c)
        return c

    async def _job_ids(job):
        return 31920032, "26-1"

    monkeypatch.setattr(eng, "_get_db_connection", _conn)
    monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", _job_ids)
    store_obj = {"store": store, "conns": conns}
    return store_obj


def _client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(eng.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="who@pyramidci.com", role=role)
    return TestClient(app)


def test_audit_lists_suspected_duplicates_for_admin(sql_log):
    res = _client("admin").get("/engage/jobdiva-profile-audit")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2 and body["returned"] == 2
    assert body["rows"][0]["real_profile_id"] == "462058065251"
    assert body["rows"][0]["duplicate_profile_id"] == "999000111"
    sql = sql_log["store"][-1]["sql"]
    assert "sc.data->>'jobdiva_candidate_id' <> sc.candidate_id" in sql
    assert "lower(sc.source) LIKE 'jobdiva%%'" in sql
    assert "ANY(%s)" not in sql  # no job filter requested


def test_audit_scopes_to_a_job_in_every_id_form(sql_log):
    res = _client("admin").get("/engage/jobdiva-profile-audit", params={"job_id": "26-1", "limit": 5})
    assert res.status_code == 200
    last = sql_log["store"][-1]
    assert "sc.jobdiva_id = ANY(%s)" in last["sql"]
    assert last["params"][0] == ["26-1", "31920032"]
    assert last["params"][1] == 5


def test_audit_is_admin_only(sql_log):
    assert _client("recruiter").get("/engage/jobdiva-profile-audit").status_code == 403
    assert _client("team_lead").post("/engage/jobdiva-profile-audit/repair", json={}).status_code == 403
    assert sql_log["store"] == []


def test_repair_restamps_own_id_and_commits(sql_log):
    res = _client("admin").post("/engage/jobdiva-profile-audit/repair", json={"job_id": "26-1"})
    assert res.status_code == 200, res.text
    assert res.json() == {"success": True, "job_id": "26-1", "repaired": 3}
    sql = sql_log["store"][-1]["sql"]
    assert "jsonb_build_object('jobdiva_candidate_id', sc.candidate_id)" in sql
    assert "IS DISTINCT FROM sc.candidate_id" in sql
    assert "sc.jobdiva_id = ANY(%s)" in sql
    assert sql_log["conns"][-1].committed is True


def test_both_routes_require_authentication():
    """Structural: the routes declare get_current_user (this router has no
    router-level auth, so a route without it would be public in PROD)."""
    for route in eng.router.routes:
        if "jobdiva-profile-audit" in getattr(route, "path", ""):
            names = {d.call.__name__ for d in route.dependant.dependencies}
            assert "get_current_user" in names, route.path
