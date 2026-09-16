"""Admin audit + repair for JobDiva-labelled twins of provisioned origin rows.

A twin is `(job, <JobDiva profile id>, 'JobDiva-*')` next to an origin row
`(job, 'exa_...', 'LinkedIn-Exa')` whose data.jobdiva_candidate_id is that same
profile id -- the applicant sync's (or a re-launch's) re-import of a person
Launch PAIR had provisioned. The repair folds the twin's engage state into the
origin row and deletes the twin; the origin keeps its `source`.
"""
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
import routers.engagement as eng


class _Cursor:
    def __init__(self, store, dict_rows: bool):
        self._store = store
        self._dict_rows = dict_rows
        self.rowcount = 4

    def execute(self, sql, params=None):
        self._store.append({"sql": " ".join(sql.split()), "params": params})

    def fetchone(self):
        return {"n": 2} if self._dict_rows else (2,)

    def fetchall(self):
        return [
            {"job_id": "26-1", "origin_candidate_id": "exa_linkedin.com/in/ada", "origin_source": "LinkedIn-Exa",
             "origin_application_origin": "pair", "origin_engage_status": "sent",
             "jobdiva_candidate_id": "462058065251", "twin_source": "JobDiva-Applicants",
             "twin_engage_status": None, "name": "Ada Lovelace", "twin_created_at": None},
        ]

    def close(self):
        pass


class _Conn:
    def __init__(self, store):
        self._store = store
        self.committed = False

    def cursor(self, **kw):
        return _Cursor(self._store, dict_rows="cursor_factory" in kw)

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
    return {"store": store, "conns": conns}


def _client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(eng.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="who@pyramidci.com", role=role)
    return TestClient(app)


def test_audit_lists_twin_pairs_for_admin(sql_log):
    res = _client("admin").get("/engage/applicant-origin-audit")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2 and body["returned"] == 1
    row = body["rows"][0]
    assert row["origin_source"] == "LinkedIn-Exa"
    assert row["twin_source"] == "JobDiva-Applicants"
    assert row["jobdiva_candidate_id"] == "462058065251"
    sql = sql_log["store"][-1]["sql"]
    # The pairing rule: same job, JobDiva-labelled row whose id is the origin's stamp.
    assert "o.data->>'jobdiva_candidate_id' = t.candidate_id" in sql
    assert "lower(o.source) NOT LIKE 'jobdiva%%'" in sql
    assert "lower(t.source) LIKE 'jobdiva%%'" in sql
    assert "ANY(%s)" not in sql


def test_audit_scopes_to_a_job_in_every_id_form(sql_log):
    res = _client("admin").get("/engage/applicant-origin-audit", params={"job_id": "26-1", "limit": 7})
    assert res.status_code == 200
    last = sql_log["store"][-1]
    assert "t.jobdiva_id = ANY(%s)" in last["sql"]
    assert last["params"][0] == ["26-1", "31920032"]
    assert last["params"][1] == 7


def test_audit_and_repair_are_admin_only(sql_log):
    assert _client("recruiter").get("/engage/applicant-origin-audit").status_code == 403
    assert _client("team_lead").post("/engage/applicant-origin-audit/repair", json={}).status_code == 403
    assert sql_log["store"] == []


def test_repair_defaults_to_dry_run_and_only_counts(sql_log):
    res = _client("admin").post("/engage/applicant-origin-audit/repair", json={"job_id": "26-1"})
    assert res.status_code == 200, res.text
    assert res.json() == {"success": True, "job_id": "26-1", "dry_run": True, "would_merge": 2}
    sqls = [s["sql"] for s in sql_log["store"]]
    assert all(s.startswith("SELECT") for s in sqls), sqls
    assert sql_log["conns"][-1].committed is False


def test_repair_folds_engage_state_into_origin_then_deletes_twin(sql_log):
    res = _client("admin").post(
        "/engage/applicant-origin-audit/repair", json={"job_id": "26-1", "dry_run": False}
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"success": True, "job_id": "26-1", "dry_run": False, "merged": 4, "deleted": 4}
    update, delete = sql_log["store"][-2], sql_log["store"][-1]
    assert update["sql"].startswith("UPDATE sourced_candidates AS o")
    # Only gaps are filled: the origin's own engage bookkeeping wins.
    assert "AND (o.data -> e.k) IS NULL" in update["sql"]
    assert "'jobdiva_twin_merged_at'" in update["sql"]
    assert "engage_status" in update["params"] and "engage_interview_id" in update["params"]
    assert update["params"][-1] == ["26-1", "31920032"]
    # The origin's `source` is never part of the SET list.
    set_clause = update["sql"].split(" FROM sourced_candidates AS t")[0]
    assert "SET data =" in set_clause and "source =" not in set_clause
    assert delete["sql"].startswith("DELETE FROM sourced_candidates AS t")
    assert "lower(t.source) LIKE 'jobdiva%%'" in delete["sql"]
    assert delete["params"] == [["26-1", "31920032"]]
    assert sql_log["conns"][-1].committed is True


def test_both_routes_require_authentication():
    for route in eng.router.routes:
        if "applicant-origin-audit" in getattr(route, "path", ""):
            names = {d.call.__name__ for d in route.dependant.dependencies}
            assert "get_current_user" in names, route.path
