"""The one launched-candidate population shared by Rankings and the launch report.

services/launched_candidates.py is the reason the Rankings header, its
"Candidates Launched" figure and the launch report row for a job can no longer
disagree: they all read this SQL and hand the rows to
launch_report.summarise_launched_candidates. These tests pin

  (a) the population rules (a person, not an interview; needs an interview id;
      a failed launch is not a launch; lifetime, not launch day),
  (b) the row shape the aggregation relies on,
  (c) the rank-list visibility fragment, byte-for-byte against the rank list's
      own query so "Sourced" cannot drift from "Showing N of M candidates",
  (d) the SQL itself against a real Postgres when one is reachable
      (LAUNCH_REPORT_TEST_DSN), with the schema of the two real tables.
"""
import inspect
import json
import os
import re

import pytest

from services import launched_candidates as lc


# ---------------------------------------------------------------------------
# (a)/(b) shape of the SQL and helpers — no DB needed
# ---------------------------------------------------------------------------
def test_key_pair_repeats_a_single_key_and_drops_blanks():
    assert lc.key_pair(["26-01234", "55"]) == ("26-01234", "55")
    assert lc.key_pair(["55"]) == ("55", "55")
    assert lc.key_pair(["", "  ", "55"]) == ("55", "55")
    with pytest.raises(ValueError):
        lc.key_pair(["", None])


def test_launched_sql_is_one_row_per_person_not_per_interview():
    sql = lc.LAUNCHED_CANDIDATES_SQL
    assert sql.count("DISTINCT ON (candidate_id)") == 2      # latest audit + latest JSONB per person
    assert "DISTINCT ON (interview_id)" not in sql
    # Newest row per person wins on both sides, like the rank list's latest_audit.
    assert sql.count("ORDER BY candidate_id, id DESC") == 2


def test_launched_sql_requires_an_interview_id_from_either_side():
    """Launched = an interview id in the audit log OR in the JSONB. A failed
    launch (engage_status='failed', interview id '') is neither."""
    sql = lc.LAUNCHED_CANDIDATES_SQL
    assert "COALESCE(NULLIF(interview_id, ''), '') <> ''" in sql
    assert "WHERE la.candidate_id IS NOT NULL OR sc.engage_interview_id IS NOT NULL" in sql
    assert "engage_status" not in sql.split("WHERE la.candidate_id IS NOT NULL")[1]


def test_launched_sql_prefers_the_jsonb_interview_id_like_the_rank_list_row():
    assert "COALESCE(sc.engage_interview_id, la.interview_id) AS interview_id" in lc.LAUNCHED_CANDIDATES_SQL


def test_launched_sql_takes_both_job_keys_and_no_date_filter():
    sql = lc.LAUNCHED_CANDIDATES_SQL
    assert sql.count("(jobdiva_id = %s OR jobdiva_id = %s)") == 2
    assert sql.count("%s") == 4
    assert "created_at" not in sql.split("FROM engage_interview_audit")[1].split("ORDER BY")[0]
    assert lc._launched_params(["26-01234", "55"]) == ("26-01234", "55", "26-01234", "55")


def test_count_sql_shares_the_population_cte_with_the_row_sql():
    """The count Rankings shows and the rows the buckets are built from must
    be the same query, not two queries that happen to agree today."""
    cte = lc._LAUNCHED_CTE_SQL
    assert lc.LAUNCHED_CANDIDATES_SQL.startswith(cte)
    assert lc.LAUNCHED_CANDIDATE_COUNT_SQL.startswith(cte)
    assert lc.LAUNCHED_CANDIDATE_COUNT_SQL.rstrip().endswith("SELECT COUNT(*) FROM launched")


def test_interview_id_of_falls_back_across_row_fields():
    assert lc.interview_id_of({"interview_id": " i1 "}) == "i1"
    assert lc.interview_id_of({"interview_id": None, "engage_interview_id": "i2"}) == "i2"
    assert lc.interview_id_of({"interview_id": "", "audit_interview_id": "i3"}) == "i3"
    assert lc.interview_id_of({}) is None


def test_engage_fields_cover_everything_the_payload_builder_reads():
    """candidate_outreach_payload copies these names off the row verbatim; a
    field read there but not selected here would silently be None."""
    from routers import launch_report as lr

    assert set(lr._CANDIDATE_PAYLOAD_FIELDS) <= set(lc._ENGAGE_FIELDS)
    assert "engage_candidate_score" in lc._ENGAGE_FIELDS
    for field in lc._ENGAGE_FIELDS:
        assert f"data->>'{field}' AS {field}" in lc.LAUNCHED_CANDIDATES_SQL
        assert f"sc.{field}" in lc.LAUNCHED_CANDIDATES_SQL


# ---------------------------------------------------------------------------
# (c) the rank list's visibility rules, shared verbatim
# ---------------------------------------------------------------------------
def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_sourced_visibility_fragment_is_the_rank_lists_own_where_clause():
    """The launch report's "Sourced" applies exactly the rows the rank list
    hides (Auto_ synthetic emails, phone-less jobdiva.local rows and their
    phone-duplicates). Pin the fragment to the rank list query's text so a
    change to either side fails here instead of drifting quietly."""
    from routers.candidates import get_job_candidates

    # The rank list writes the regex as '\\D' inside a plain triple-quoted
    # string; the evaluated fragment holds a single backslash. Compare values.
    rank_list_sql = _squash(inspect.getsource(get_job_candidates)).replace("\\\\", "\\")
    fragment = _squash(lc.sourced_visibility_sql("sc"))
    assert fragment in rank_list_sql, "rank list counts query no longer matches sourced_visibility_sql('sc')"
    assert lc.sourced_visibility_sql("sc").count("%s") == 2
    assert lc.SOURCED_CANDIDATES_SQL.count("%s") == 4


# ---------------------------------------------------------------------------
# (d) the statements really run — real Postgres, real table shapes
# ---------------------------------------------------------------------------
psycopg2 = pytest.importorskip("psycopg2")

_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

# Column-for-column the CREATE TABLE statements in routers/engagement.py
# (_ensure_audit_table) and services/sourced_candidates_storage.py.
_SCHEMA_SQL = """
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
    interview_id VARCHAR(255), candidate_name VARCHAR(255), candidate_email VARCHAR(255),
    payload JSONB, response JSONB, status VARCHAR(50) DEFAULT 'sent',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ON COMMIT DROP;
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    name TEXT, email TEXT, phone TEXT, headline TEXT, location TEXT, resume_id TEXT, resume_text TEXT,
    profile_url TEXT, image_url TEXT, data JSONB, status TEXT DEFAULT 'sourced',
    resume_match_percentage INT DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(jobdiva_id, candidate_id, source)
) ON COMMIT DROP;
"""

REF, NUM = "26-01234", "55"


@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        yield conn
    finally:
        conn.rollback()  # ON COMMIT DROP never fires; rollback discards everything
        conn.close()


def _sourced(conn, cid, *, key=REF, email=None, phone=None, data=None, created="2026-08-27 10:00:00"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, email, phone, data, created_at)"
            " VALUES (%s, %s, 'LinkedIn', %s, %s, %s, %s)",
            (key, cid, email, phone, json.dumps(data or {}), created),
        )


def _audit(conn, cid, iid, status="Initiated", *, key=REF, response=None, created="2026-08-27 12:00:00"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status, response, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (cid, key, iid, status, json.dumps(response) if response is not None else None, created),
        )


def _seed(conn):
    # launched once; audit + JSONB agree
    _sourced(conn, "c1", email="c1@x.com", phone="1112223333",
             data={"engage_interview_id": "i1", "engage_status": "in_progress", "outreach_phase": "phase2"})
    _audit(conn, "c1", "i1", "in_progress")
    # RE-LAUNCHED: two interviews; JSONB points at the newer one; older audit row completed
    _sourced(conn, "c2", email="c2@x.com", phone="2223334444",
             data={"engage_interview_id": "i2b", "engage_status": "pending"})
    _audit(conn, "c2", "i2a", "completed", created="2026-08-27 12:00:00")
    _audit(conn, "c2", "i2b", "Initiated", created="2026-09-03 12:00:00")   # a week later
    # launched, audit insert lost — JSONB interview id only
    _sourced(conn, "c3", email="c3@x.com", phone="3334445555",
             data={"engage_interview_id": "i3", "engage_status": "passed", "engage_score": "88"})
    # FAILED launch: audit row with no interview id, JSONB failed with '' id
    _sourced(conn, "c4", email="c4@x.com", phone="4445556666",
             data={"engage_interview_id": "", "engage_status": "failed"})
    _audit(conn, "c4", None, "failed")
    # sourced only
    _sourced(conn, "c5", email="c5@x.com", phone="5556667777")
    # stored under BOTH keys
    _sourced(conn, "c6", key=REF, email="c6@x.com", phone="6667778888",
             data={"engage_interview_id": "i6", "engage_status": "sent"}, created="2026-08-27 09:00:00")
    _sourced(conn, "c6", key=NUM, email="c6@x.com", phone="6667778888",
             data={"engage_interview_id": "i6", "engage_status": "sent"}, created="2026-08-27 09:30:00")
    _audit(conn, "c6", "i6", key=NUM)
    # audit row only, no sourced row
    _audit(conn, "c7", "i7")
    # synthetic JobDiva email: hidden from Sourced, still launched
    _sourced(conn, "c8", email="Auto_123@jobdiva.com", phone="",
             data={"engage_interview_id": "i8", "engage_status": "sent"})
    _audit(conn, "c8", "i8")
    # jobdiva.local without a phone → hidden
    _sourced(conn, "c9", email="pair-9990001111@no-email.jobdiva.local", phone="")
    # real candidate + jobdiva.local phone-duplicate of them → dup hidden
    _sourced(conn, "c10", email="c10@x.com", phone="(999) 000-2222",
             data={"feedback_type": "submit", "feedback_reason": "good", "feedback_at": "2026-08-28T10:00:00Z"})
    _sourced(conn, "c11", email="pair-9990002222@no-email.jobdiva.local", phone="")
    # jobdiva.local WITH a verified phone → visible
    _sourced(conn, "c12", email="pair-1230004444@no-email.jobdiva.local", phone="1230004444")
    # stored only under the numeric key
    _sourced(conn, "c13", key=NUM, email="c13@x.com", phone="1300000000",
             data={"engage_interview_id": "i13", "engage_status": "sent"})
    _audit(conn, "c13", "i13", key=NUM)
    # another job's rows must never leak in
    _sourced(conn, "z1", key="OTHER", email="z@x.com", phone="1", data={"engage_interview_id": "iz"})
    _audit(conn, "z1", "iz", key="OTHER")


def test_pg_population_is_people_with_an_interview_id_over_the_jobs_lifetime(pg):
    _seed(pg)
    rows = {r["candidate_id"]: r for r in lc.fetch_launched_candidates(pg, [REF, NUM])}
    assert sorted(rows) == ["c1", "c13", "c2", "c3", "c6", "c7", "c8"]
    # re-launched c2 is ONE row, on the newest interview (a week after launch)
    assert rows["c2"]["interview_id"] == "i2b"
    assert rows["c2"]["audit_interview_id"] == "i2b"
    assert rows["c2"]["audit_status"] == "Initiated"
    # JSONB-only launch keeps its JSONB fields, has no audit side
    assert rows["c3"]["interview_id"] == "i3"
    assert rows["c3"]["audit_interview_id"] is None
    assert rows["c3"]["engage_score"] == "88"
    # audit-only launch has no JSONB side
    assert rows["c7"]["interview_id"] == "i7" and rows["c7"]["engage_status"] is None
    assert rows["c1"]["outreach_phase"] == "phase2"


def test_pg_count_matches_rows_and_respects_keys(pg):
    _seed(pg)
    assert lc.count_launched_candidates(pg, [REF, NUM]) == len(lc.fetch_launched_candidates(pg, [REF, NUM])) == 7
    assert lc.count_launched_candidates(pg, [REF]) == 6          # c13 lives only under NUM
    assert lc.count_launched_candidates(pg, ["NOPE"]) == 0


def test_pg_audit_response_comes_back_as_jsonb_dict(pg):
    _seed(pg)
    _audit(pg, "c1", "i1", "completed", response={"outreach_status": "completed", "candidate_score": 90})
    row = {r["candidate_id"]: r for r in lc.fetch_launched_candidates(pg, [REF, NUM])}["c1"]
    assert row["audit_status"] == "completed"
    assert isinstance(row["audit_response"], dict)


def test_pg_sourced_population_is_the_rank_lists_visible_rows(pg):
    _seed(pg)
    rows = {r["candidate_id"]: r for r in lc.fetch_sourced_candidates(pg, [REF, NUM])}
    # hidden: c8 (Auto_ email), c9 (jobdiva.local, no phone), c11 (phone-dup of c10), z1 (other job)
    assert sorted(rows) == ["c1", "c10", "c12", "c13", "c2", "c3", "c4", "c5", "c6"]
    # both-keys candidate: one row, earliest sourcing time
    assert str(rows["c6"]["created_at"]).startswith("2026-08-27 09:00:00")
    assert rows["c10"]["feedback_type"] == "submit"


def test_pg_report_row_and_rankings_header_agree_by_construction(pg):
    """The same rows feed summarise_launched_candidates for both screens, so
    the header buckets sum to Candidates Launched and equal the report row."""
    from routers import launch_report as lr

    _seed(pg)
    rows = lc.fetch_launched_candidates(pg, [REF, NUM])
    summary = lr.summarise_launched_candidates(rows, {})
    buckets = summary["buckets"]
    assert summary["launched"] == lc.count_launched_candidates(pg, [REF, NUM]) == 7
    assert buckets["pending"] + buckets["in_progress"] + buckets["completed"] + buckets["partial_complete"] == 7
    assert buckets["in_progress"] == 1       # c1 — the only one who has started
    assert buckets["completed"] == buckets["passed"] == 1   # c3, JSONB passed
    assert buckets["pending"] == 5           # launch-time `sent` / `Initiated`, exactly what the table shows
