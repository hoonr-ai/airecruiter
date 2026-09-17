"""The twin repair against a real Postgres: nothing a twin knows is lost.

Runs only when the embedded-Postgres package ``pgserver`` is importable
(``pip install pgserver``); it is not in requirements-dev, so CI skips this
module and the SQL-shape assertions in test_applicant_origin_audit_endpoint.py
stand in for it there.

The reviewer's scenario: one origin row (LinkedIn-Exa, stamped with the JobDiva
profile Launch PAIR minted) and TWO JobDiva-labelled twins for the same person on
the same job -- the applicant sync's JobDiva-Applicants row and a re-launch's
JobDiva-TalentSearch row -- each holding different engage bookkeeping. With an
``UPDATE ... FROM`` join Postgres would apply SET from one arbitrary twin and the
DELETE would then erase the other's state for good. The correlated fold must
keep every key, prefer the most recently updated twin where both carry one, never
overwrite what the origin already has, leave other jobs alone, and be idempotent.
"""
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pgserver = pytest.importorskip("pgserver")
psycopg2 = pytest.importorskip("psycopg2")

from core.auth import UserIdentity, get_current_user  # noqa: E402
import routers.engagement as eng  # noqa: E402

JOB = "26-1"
OTHER_JOB = "26-9"
PROFILE = "462058065251"
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    datadir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(datadir))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
        shutil.rmtree(datadir, ignore_errors=True)


@pytest.fixture
def db(pg_uri, monkeypatch):
    conn = psycopg2.connect(pg_uri)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS sourced_candidates")
        cur.execute(
            """
            CREATE TABLE sourced_candidates (
                id SERIAL PRIMARY KEY,
                jobdiva_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT,
                email TEXT,
                phone TEXT,
                data JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (jobdiva_id, candidate_id, source)
            )
            """
        )
    conn.autocommit = False

    # The endpoints borrow a connection per request and close it; hand out fresh ones.
    def _connect():
        c = psycopg2.connect(pg_uri)
        return c

    async def _job_ids(job):
        return (31920032, JOB) if job == JOB else (None, job)

    monkeypatch.setattr(eng, "_get_db_connection", _connect)
    monkeypatch.setattr(eng, "_resolve_provisioning_job_ids", _job_ids)
    yield conn
    conn.close()


def _insert(conn, *, job, candidate_id, source, data, updated_at, name="Ada Lovelace", email=None):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, name, email, data, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (job, candidate_id, source, name, email, json.dumps(data), updated_at, updated_at),
        )
    conn.commit()


def _rows(conn, job):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT candidate_id, source, data FROM sourced_candidates WHERE jobdiva_id = %s ORDER BY candidate_id",
            (job,),
        )
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(eng.router)
    app.dependency_overrides[get_current_user] = lambda: UserIdentity(email="admin@pyramidci.com", role="admin")
    return TestClient(app)


def _seed(conn):
    # Origin: the Exa row Launch PAIR provisioned. It already knows engage_status.
    _insert(conn, job=JOB, candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa",
            email="ada@work.example.com", updated_at=T0,
            data={"jobdiva_candidate_id": PROFILE, "jobdiva_application_origin": "pair",
                  "match_score": 84, "engage_status": "sent"})
    # Twin A: the sync's re-import (older). Knows the interview id and a score.
    _insert(conn, job=JOB, candidate_id=PROFILE, source="JobDiva-Applicants",
            email="ada@home.example.com", updated_at=T0 + timedelta(hours=1),
            data={"jobdiva_candidate_id": PROFILE, "engage_status": "completed",
                  "engage_interview_id": "iv-A", "engage_score": 80})
    # Twin B: a re-launch's copy (newer). Knows a different interview id and the hard filter.
    _insert(conn, job=JOB, candidate_id=PROFILE, source="JobDiva-TalentSearch",
            updated_at=T0 + timedelta(hours=2),
            data={"jobdiva_candidate_id": PROFILE, "engage_interview_id": "iv-B",
                  "engage_hard_filter_status": "passed"})
    # A genuine organic applicant on the same job: not a twin of anyone.
    _insert(conn, job=JOB, candidate_id="555", source="JobDiva-Applicants", name="Bob Byte",
            email="bob@example.com", updated_at=T0, data={"jobdiva_application_origin": "organic"})
    # An origin without twins: must be left untouched.
    _insert(conn, job=JOB, candidate_id="exa_linkedin.com/in/grace", source="LinkedIn-Exa",
            name="Grace Hopper", updated_at=T0, data={"jobdiva_candidate_id": "999", "engage_status": "sent"})
    # The same person's pair on ANOTHER job: out of scope for a job-scoped repair.
    _insert(conn, job=OTHER_JOB, candidate_id="exa_linkedin.com/in/ada", source="LinkedIn-Exa",
            updated_at=T0, data={"jobdiva_candidate_id": PROFILE})
    _insert(conn, job=OTHER_JOB, candidate_id=PROFILE, source="JobDiva-Applicants",
            updated_at=T0, data={"engage_interview_id": "iv-other"})


def test_audit_lists_each_twin_pair(db):
    _seed(db)
    res = _client().get("/engage/applicant-origin-audit", params={"job_id": JOB})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 2
    assert {r["twin_source"] for r in body["rows"]} == {"JobDiva-Applicants", "JobDiva-TalentSearch"}
    assert {r["origin_candidate_id"] for r in body["rows"]} == {"exa_linkedin.com/in/ada"}


def test_repair_folds_both_twins_and_deletes_them_only_on_the_scoped_job(db):
    _seed(db)
    client = _client()

    dry = client.post("/engage/applicant-origin-audit/repair", json={"job_id": JOB})
    assert dry.json() == {"success": True, "job_id": JOB, "dry_run": True, "would_merge": 2}
    assert len(_rows(db, JOB)) == 5  # dry run changed nothing

    res = client.post("/engage/applicant-origin-audit/repair", json={"job_id": JOB, "dry_run": False})
    assert res.status_code == 200, res.text
    assert res.json() == {
        "success": True, "job_id": JOB, "dry_run": False, "origins_updated": 1, "twins_deleted": 2,
    }

    rows = _rows(db, JOB)
    # Both twins are gone; the origin, the organic applicant and the twin-less origin remain.
    assert set(rows) == {
        ("exa_linkedin.com/in/ada", "LinkedIn-Exa"),
        ("555", "JobDiva-Applicants"),
        ("exa_linkedin.com/in/grace", "LinkedIn-Exa"),
    }
    origin = rows[("exa_linkedin.com/in/ada", "LinkedIn-Exa")]
    # The origin's own bookkeeping wins over both twins.
    assert origin["engage_status"] == "sent"
    # A key both twins carry: the most recently updated twin (B) wins.
    assert origin["engage_interview_id"] == "iv-B"
    # Keys only one twin carries are kept, whichever twin it was.
    assert origin["engage_score"] == 80                     # only twin A had it
    assert origin["engage_hard_filter_status"] == "passed"  # only twin B had it
    # Origin identity and provenance are untouched.
    assert origin["jobdiva_candidate_id"] == PROFILE
    assert origin["jobdiva_application_origin"] == "pair"
    assert origin["match_score"] == 84
    assert sorted(origin["jobdiva_twin_sources"]) == ["JobDiva-Applicants", "JobDiva-TalentSearch"]
    assert origin["jobdiva_twin_count"] == 2
    assert origin["jobdiva_twin_merged_at"].endswith("Z")
    # Non-engage keys of a twin never leak into the origin.
    assert "jobdiva_twin_merged_at" not in rows[("exa_linkedin.com/in/grace", "LinkedIn-Exa")]
    assert rows[("exa_linkedin.com/in/grace", "LinkedIn-Exa")] == {"jobdiva_candidate_id": "999", "engage_status": "sent"}

    # The other job's pair is untouched by a job-scoped repair...
    other = _rows(db, OTHER_JOB)
    assert set(other) == {("exa_linkedin.com/in/ada", "LinkedIn-Exa"), (PROFILE, "JobDiva-Applicants")}

    # ...and a second run on the same job is a no-op.
    again = client.post("/engage/applicant-origin-audit/repair", json={"job_id": JOB, "dry_run": False})
    assert again.json()["origins_updated"] == 0 and again.json()["twins_deleted"] == 0
    assert _rows(db, JOB) == rows


def test_unscoped_repair_covers_every_job(db):
    _seed(db)
    res = _client().post("/engage/applicant-origin-audit/repair", json={"dry_run": False})
    assert res.json()["origins_updated"] == 2 and res.json()["twins_deleted"] == 3
    other = _rows(db, OTHER_JOB)
    assert set(other) == {("exa_linkedin.com/in/ada", "LinkedIn-Exa")}
    assert other[("exa_linkedin.com/in/ada", "LinkedIn-Exa")]["engage_interview_id"] == "iv-other"
