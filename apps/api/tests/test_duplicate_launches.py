"""services/duplicate_launches.py: people launched under 2+ candidate_ids
for one job (the pre-person-gate duplicates), and its admin endpoint."""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import UserIdentity, get_current_user
from routers import admin_analytics as aa
from services import duplicate_launches as dl


def _row(job, cid, **kw):
    return {"job_key": job, "jobdiva_ref": f"26-{job}", "candidate_id": cid, "source": kw.pop("source", "LinkedIn"),
            "email": kw.pop("email", None), "phone": kw.pop("phone", None), "profile_url": kw.pop("profile_url", None),
            "jobdiva_candidate_id": kw.pop("jobdiva_candidate_id", None), "interview_id": "iv"}


def test_groups_a_person_across_ids_transitively_per_job():
    rows = [
        _row("1", "exa_jane", email="jane@x.com"),
        _row("1", "4471", source="JobDiva-Applicant", email="JANE@x.com", phone="4155550142"),
        _row("1", "unipile_j", phone="(415) 555-0142"),              # via 4471's phone
        _row("1", "bob", email="bob@x.com"),
        _row("2", "exa_jane", email="jane@x.com"),                   # other job: separate
        _row("2", "exa_jane", email="jane@x.com"),                   # same id under both keys: one person
    ]
    groups = dl.group_duplicates(rows)
    assert groups == [{
        "job_id": "1", "jobdiva_id": "26-1", "candidate_ids": ["4471", "exa_jane", "unipile_j"],
        "matched_by": ["email", "phone"], "extra_launches": 2,
    }]


def test_endpoint_is_admin_only(monkeypatch):
    app = FastAPI()
    app.include_router(aa.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="r@x.com", role="recruiter")
    assert TestClient(app).get("/api/v1/admin/duplicate-launches").status_code == 403


psycopg2 = pytest.importorskip("psycopg2")
_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


def test_pg_find_duplicate_launches_reads_both_job_keys():
    try:
        conn = psycopg2.connect(_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE monitored_jobs (job_id TEXT, jobdiva_id TEXT);
                CREATE TEMP TABLE sourced_candidates (id SERIAL, jobdiva_id TEXT, candidate_id TEXT, source TEXT,
                    email TEXT, phone TEXT, profile_url TEXT, data JSONB);
                INSERT INTO monitored_jobs VALUES ('55', '26-55');
                INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, email, data) VALUES
                  ('26-55', 'exa_jane', 'LinkedIn-Exa', 'jane@x.com', '{"engage_interview_id": "iv-1"}'),
                  ('55', '4471', 'JobDiva-Applicant', NULL, '{"engage_interview_id": "iv-2", "email": "jane@x.com"}'),
                  ('55', 'never', 'LinkedIn', 'jane@x.com', '{"engage_status": "failed"}');
            """)
        out = dl.find_duplicate_launches(conn)
        assert out["jobs_affected"] == 1 and out["people_affected"] == 1 and out["extra_launches"] == 1
        assert out["groups"][0]["candidate_ids"] == ["4471", "exa_jane"]
        assert out["groups"][0]["job_id"] == "55" and out["groups"][0]["jobdiva_id"] == "26-55"
    finally:
        conn.rollback()
        conn.close()
