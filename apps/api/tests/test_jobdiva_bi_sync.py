"""services/jobdiva_bi_sync.py: the JobDiva BI mirror behind the PAIR Dashboard.

(a) normalisation of each feed's records (field names probed live 2026-09-24),
(b) the backfill / incremental window arithmetic,
(c) storage and whole sync cycles on a real Postgres with a fake JobDiva.

The Postgres tests skip when no server is reachable (set LAUNCH_REPORT_TEST_DSN,
e.g. to a pgserver URI or a local dev database). They use TEMP tables only,
which shadow any real table of the same name for the test's own session.
"""
import asyncio
import datetime
import os

import pytest

from services import jobdiva_bi_sync as sync


# ---------------------------------------------------------------------------
# (a) normalisation
# ---------------------------------------------------------------------------

ISSUED_JOB = {
    "JOBID": 32308716, "JOBDIVANO": "26-15314", "TITLE": "Cash Application Analyst ",
    "COMPANYID": 11, "COMPANYNAME": "Acme Corp", "DIVISIONID": 505047,
    "DIVISIONNAME": "Pharma                 ", "JOBSTATUS": "Open", "OPENINGS": "3", "FILLS": "0",
    "ISSUEDATE": "2026-09-17T19:30:48", "PRIMARYRECRUITERID": 777, "PRIMARYSALESID": 888,
    "POSITIONTYPE": "", "CONTACTFIRSTNAME": "Pat", "CONTACTLASTNAME": "Client",
}


def test_issued_job_normalises_with_trimmed_text_and_upper_status():
    job = sync.normalize_job(ISSUED_JOB)
    assert job["job_id"] == "32308716"
    assert job["jobdiva_ref"] == "26-15314"
    assert job["title"] == "Cash Application Analyst"
    assert job["division_name"] == "Pharma"
    assert job["job_status"] == "OPEN"
    assert job["openings"] == 3 and job["fills"] == 0
    assert job["issue_date"] == datetime.datetime(2026, 9, 17, 19, 30, 48)
    assert job["position_type"] is None  # blank -> NULL so COALESCE keeps a known value
    assert job["priority"] is None  # IssuedJobsList has no PRIORITY
    assert set(job) == set(sync.JOB_COLUMNS)


def test_jobs_detail_uses_id_and_dateissued():
    job = sync.normalize_job({
        "ID": "31783195", "JOBDIVANO": "26-02576", "JOBTITLE": "DevOps", "JOBSTATUS": "CLOSED",
        "PRIORITY": "P3", "DATEISSUED": "2026-02-01T08:00:00", "DATESTATUSUPDATED": "2026-06-25T09:02:34",
        "DIVISIONNAME": "Fidelity               ",
    })
    assert job["job_id"] == "31783195"
    assert job["title"] == "DevOps"
    assert job["issue_date"] == datetime.datetime(2026, 2, 1, 8, 0)
    assert job["status_updated_at"] == datetime.datetime(2026, 6, 25, 9, 2, 34)
    assert job["priority"] == "P3"
    assert job["division_name"] == "Fidelity"


def test_job_without_an_id_is_dropped():
    assert sync.normalize_job({"JOBDIVANO": "26-1"}) is None


ACTIVITY = {
    "ACTIVITYID": 9001, "JOBID": 32308716, "JOBREFERENCENUMBER": "26-15314", "CANDIDATEID": 19122444778939,
    "CANDIDATEFIRSTNAME": "Private", "CANDIDATELASTNAME": "Person", "CANDIDATEEMAIL": "private@example.com",
    "USERID": 777, "SUBMITTALFLAG": "1", "INTERNALSUBMITTALFLAG": "0", "SUBMITTALDATE": "2026-09-17T19:09:00",
    "INTERVIEWFLAG": "1", "INTERVIEWDATE": "2026-09-20T10:00:00", "INTERVIEW_TYPE": "Client Interview L1- External",
    "HIREFLAG": "0", "STARTDATE": "", "START_STATUS": "", "ACTIVITYDATE": "2026-09-18T11:00:00",
}


def test_activity_keeps_ids_and_state_and_drops_candidate_pii():
    activity = sync.normalize_activity(ACTIVITY)
    assert set(activity) == set(sync.ACTIVITY_COLUMNS)
    assert activity["candidate_id"] == "19122444778939"
    assert activity["is_internal"] is False and activity["is_submittal"] is True
    assert activity["interview_flag"] is True
    assert activity["interview_date"] == datetime.datetime(2026, 9, 20, 10, 0)
    assert activity["hire_flag"] is False and activity["start_date"] is None
    assert activity["activity_date"] == datetime.datetime(2026, 9, 18, 11, 0)
    stored = " ".join(str(v) for v in activity.values())
    assert "Private" not in stored and "private@example.com" not in stored


def test_activity_interview_date_falls_back_to_schedule_date():
    activity = sync.normalize_activity({"ACTIVITYID": "1", "INTERVIEWFLAG": "1",
                                        "INTERVIEWSCHEDULEDATE": "2026-09-01T09:00:00"})
    assert activity["interview_date"] == datetime.datetime(2026, 9, 1, 9, 0)


def test_user_email_is_lowercased_and_flags_parsed():
    user = sync.normalize_user({
        "USERID": 777, "EMAIL": " Sarah.Jones@PyramidCI.com ", "FIRSTNAME": "Sarah", "ACTIVEFLAG": "1",
        "RECRUITERFLAG": "1", "RECRUITINGMANAGERFLAG": "0", "TEAMLEADERFLAG": "1", "DIVISION": "SI-1 ",
    })
    assert user["email"] == "sarah.jones@pyramidci.com"
    assert user["is_active"] and user["is_recruiter"] and user["is_team_leader"]
    assert not user["is_recruiting_manager"]
    assert user["division"] == "SI-1"


def test_job_user_roles_are_collected():
    row = sync.normalize_job_user({
        "JOBID": 5, "USERID": 7, "PRIMARYRECRUITER": "1", "RECRUITER": "true", "Secondary Sales": "Y",
        "SALES": "0", "DATELASTASSIGNED": "2026-09-01T10:00:00",
    })
    assert row["roles"] == "primary_recruiter,recruiter,secondary_sales"
    assert sync.normalize_job_user({"JOBID": 5}) is None


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-17T19:30:48", datetime.datetime(2026, 9, 17, 19, 30, 48)),
    ("2026-09-17 19:30:48.123", datetime.datetime(2026, 9, 17, 19, 30, 48)),
    ("09/17/2026 19:30:48", datetime.datetime(2026, 9, 17, 19, 30, 48)),
    ("09/17/2026", datetime.datetime(2026, 9, 17)),
    ("", None), (None, None), ("not a date", None),
])
def test_timestamps(raw, expected):
    assert sync._timestamp(raw) == expected


# ---------------------------------------------------------------------------
# (b) windows
# ---------------------------------------------------------------------------

T0 = datetime.datetime(2026, 9, 24, 9, 0)


def test_backfill_windows_walk_newest_first_and_cover_exactly():
    windows = sync.backfill_windows(T0, T0 - datetime.timedelta(days=16), datetime.timedelta(days=7))
    assert windows[0] == (T0 - datetime.timedelta(days=7), T0)
    assert windows[-1] == (T0 - datetime.timedelta(days=16), T0 - datetime.timedelta(days=14))
    for (lo, hi), (lo_next, hi_next) in zip(windows, windows[1:]):
        assert hi_next == lo  # contiguous, no gap, no overlap
    assert sync.backfill_windows(T0, T0, datetime.timedelta(days=7)) == []


def test_forward_windows_are_contiguous_and_capped():
    windows = sync.forward_windows(T0 - datetime.timedelta(days=2, hours=3), T0, datetime.timedelta(days=1))
    assert len(windows) == 3
    assert windows[0][0] == T0 - datetime.timedelta(days=2, hours=3)
    assert windows[-1][1] == T0
    assert all(hi - lo <= datetime.timedelta(days=1) for lo, hi in windows)


def test_jobdiva_datetime_format_is_what_the_bi_api_takes():
    assert sync.format_jobdiva_datetime(datetime.datetime(2026, 9, 3, 7, 5, 9)) == "09/03/2026 07:05:09"


def test_batches():
    assert sync.batched(list(range(5)), 2) == [[0, 1], [2, 3], [4]]


# ---------------------------------------------------------------------------
# (c) real Postgres
# ---------------------------------------------------------------------------
psycopg2 = pytest.importorskip("psycopg2")

_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")


def _temp(statement: str) -> str:
    return statement.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1)


class _KeepOpen:
    """Hands the sync a connection whose close() is a no-op, so the test can
    inspect the temp tables afterwards."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            for statement in sync.SCHEMA_STATEMENTS:
                cur.execute(_temp(statement))
            cur.execute("CREATE TEMP TABLE monitored_jobs (job_id TEXT, jobdiva_id TEXT)")
        conn.commit()
        yield conn
    finally:
        conn.rollback()
        conn.close()  # temp tables go with the session


def _rows(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_pg_job_feeds_merge_without_blanking_each_other(pg):
    sync.store_jobs(pg, [ISSUED_JOB])
    # NewUpdatedJobRecords: has priority + status change, no names, blank openings.
    sync.store_jobs(pg, [{
        "JOBID": 32308716, "JOBSTATUS": "CANCELLED", "PRIORITY": "P1", "OPENINGS": "",
        "DATESTATUSUPDATED": "2026-09-22T10:00:00", "DATEUPDATED": "2026-09-22T10:00:00",
    }], mark_detailed=True, mark_users_stale=True)
    row = _rows(pg, "SELECT job_status, priority, openings, company_name, division_name, detail_synced_at "
                    "FROM jobdiva_jobs WHERE job_id = '32308716'")[0]
    assert row[:5] == ("CANCELLED", "P1", 3, "Acme Corp", "Pharma")
    assert row[5] is not None


def test_pg_activities_overwrite_with_current_state(pg):
    sync.store_activities(pg, [{**ACTIVITY, "HIREFLAG": "1", "STARTDATE": "2026-10-01T09:00:00"}])
    sync.store_activities(pg, [{**ACTIVITY, "HIREFLAG": "0", "STARTDATE": ""}])  # start cancelled
    hire, start = _rows(pg, "SELECT hire_flag, start_date FROM jobdiva_activities")[0]
    assert hire is False and start is None


def test_pg_job_users_are_replaced_per_job(pg):
    sync.replace_job_users(pg, ["5", "6"], [
        {"JOBID": 5, "USERID": 1, "RECRUITER": "1"}, {"JOBID": 5, "USERID": 2}, {"JOBID": 99, "USERID": 3},
        {"JOBID": 6, "USERID": 4},
    ])
    sync.replace_job_users(pg, ["5", "6"], [{"JOBID": 5, "USERID": 2}])
    # 5 is replaced; 6 was not in the answer and keeps its tags (an answer never erases).
    assert _rows(pg, "SELECT job_id, user_id FROM jobdiva_job_users ORDER BY 1, 2") == [("5", "2"), ("6", "4")]
    # Both requested jobs are stamped as read either way.
    assert _rows(pg, "SELECT job_id FROM jobdiva_jobs WHERE users_synced_at IS NOT NULL ORDER BY 1") == [("5",), ("6",)]


class _Answers:
    """A per-job endpoint that answers only for some job ids."""

    def __init__(self, known, key="ID"):
        self.known, self.key, self.calls = set(known), key, []

    async def __call__(self, path, params):
        self.calls.append(list(params["jobIds"]))
        return [{self.key: j, "JOBSTATUS": "OPEN"} for j in params["jobIds"] if j in self.known]


def test_an_empty_batch_is_re_asked_one_job_at_a_time(monkeypatch):
    monkeypatch.setattr(sync, "PAUSE_BETWEEN_CALLS_SECONDS", 0)
    budget = sync._Budget(60)
    fetch = _Answers(known=set())  # everything gone... but only after 3 singles do we believe the endpoint is down
    with pytest.raises(sync.JobDivaBIError):
        asyncio.run(sync._fetch_batch(fetch, "/apiv2/bi/JobsDetail", ["1", "2", "3", "4", "5"], budget))
    assert fetch.calls == [[1, 2, 3, 4, 5], [1], [2], [3]]
    fetch = _Answers(known={4})  # a real job among gone ones settles the whole batch
    rows, settled = asyncio.run(sync._fetch_batch(fetch, "/apiv2/bi/JobsDetail", ["1", "4", "5"], budget))
    assert [r["ID"] for r in rows] == [4] and settled == ["1", "4", "5"]
    fetch = _Answers(known={1})
    rows, settled = asyncio.run(sync._fetch_batch(fetch, "/apiv2/bi/JobsDetail", ["1", "2"], budget))
    assert fetch.calls == [[1, 2]] and settled == ["1", "2"]  # a normal answer needs no retry


def test_an_exhausted_budget_settles_only_the_jobs_it_asked(monkeypatch):
    monkeypatch.setattr(sync, "PAUSE_BETWEEN_CALLS_SECONDS", 0)
    budget = sync._Budget(0)
    rows, settled = asyncio.run(sync._fetch_batch(_Answers(known=set()), "/apiv2/bi/JobsDetail", ["1", "2"], budget))
    assert rows == [] and settled == []


class _Response:
    def __init__(self, status_code, payload):
        self.status_code, self._payload, self.text = status_code, payload, str(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, url, params=None, headers=None):
        return _Response(200, self.payload)


@pytest.mark.parametrize("payload,expected", [
    ({"data": [{"ID": 1}]}, [{"ID": 1}]),
    ({"data": {"ID": 1}}, [{"ID": 1}]),  # a single record unwrapped
    ({"data": None}, []),
    ([{"ID": 2}], [{"ID": 2}]),
])
def test_http_fetch_payload_shapes(monkeypatch, payload, expected):
    from services.jobdiva import jobdiva_service

    async def token(force_refresh=False):
        return "t"

    monkeypatch.setattr(jobdiva_service, "authenticate", token)
    assert asyncio.run(sync._make_http_fetch(_Client(payload))("/apiv2/bi/JobsDetail", {})) == expected


def test_http_fetch_rejects_an_error_shaped_as_a_200(monkeypatch):
    from services.jobdiva import jobdiva_service

    async def token(force_refresh=False):
        return "t"

    monkeypatch.setattr(jobdiva_service, "authenticate", token)
    with pytest.raises(sync.JobDivaBIError):
        asyncio.run(sync._make_http_fetch(_Client({"message": "Invalid token"}))("/apiv2/bi/JobsDetail", {}))


class FakeJobDiva:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def __call__(self, path, params):
        self.calls.append((path, params))
        name = path.rsplit("/", 1)[-1]
        if name in self.fail:
            raise sync.JobDivaBIError(f"{name} is down")
        if name == "NewUpdatedUserRecords":
            return [{"USERID": 777, "EMAIL": "Sarah@Pyramidci.com", "ACTIVEFLAG": "1"}]
        if name == "IssuedJobsList":
            return [ISSUED_JOB]
        if name == "NewUpdatedJobRecords":
            # Real rows carry division and client too (probed), just no recruiter names.
            return [{"JOBID": 1111, "JOBDIVANO": "26-00001", "JOBSTATUS": "OPEN", "PRIORITY": "P2",
                     "ISSUEDATE": "2026-09-23T08:00:00", "DIVISIONNAME": "SI-1", "COMPANYNAME": "Acme"}]
        if name == "SubmittalInterviewHireActivitiesList":
            return [ACTIVITY]
        if name == "OpenJobsList":
            return [{"JOBID": 2222, "JOBDIVANO": "25-00002", "JOBSTATUS": "OPEN", "PRIORITY": "P1",
                     "ISSUEDATE": "2025-01-15T08:00:00", "DIVISIONNAME": "SI-3"}]
        if name == "JobsDetail":
            return [{"ID": j, "JOBSTATUS": "OPEN", "PRIORITY": "P3"} for j in params["jobIds"]]
        if name == "JobsUsersDetail":
            return [{"JOBID": j, "USERID": 777, "PRIMARYRECRUITER": "1"} for j in params["jobIds"]]
        raise AssertionError(f"unexpected call {path}")

    def names(self):
        return [p.rsplit("/", 1)[-1] for p, _ in self.calls]


def _cycle(pg, fake, now, monkeypatch, **kwargs):
    monkeypatch.setattr(sync, "PAUSE_BETWEEN_CALLS_SECONDS", 0)
    return asyncio.run(sync.run_sync_cycle(
        conn_factory=lambda: _KeepOpen(pg), fetch=fake, budget_seconds=60, now=now, **kwargs,
    ))


def test_pg_first_cycle_backfills_newest_first_then_resumes_incrementally(pg, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_DAYS", 10)
    fake = FakeJobDiva()
    summary = _cycle(pg, fake, T0, monkeypatch)
    assert not any(str(v).startswith("failed") for v in summary.values()), summary

    issued = [params for path, params in fake.calls if path.endswith("IssuedJobsList")]
    assert [p["toDate"] for p in issued] == ["09/24/2026 09:00:00", "09/17/2026 09:00:00"]  # newest first
    assert issued[-1]["fromDate"] == "09/14/2026 09:00:00"  # stops at the 10-day target
    state = {r[0]: r[1:] for r in _rows(pg, "SELECT feed, cursor_at, backfill_done FROM jobdiva_bi_sync_state")}
    assert state["jobs"] == (T0, True) and state["activities"] == (T0, True)

    assert _rows(pg, "SELECT COUNT(*) FROM jobdiva_activities")[0][0] == 1
    assert _rows(pg, "SELECT email FROM jobdiva_users")[0][0] == "sarah@pyramidci.com"
    # A long-running open req issued before the backfill window still arrives.
    assert _rows(pg, "SELECT priority, division_name FROM jobdiva_jobs WHERE job_id = '2222'") == [("P1", "SI-3")]
    # Every job got its detail and its users read.
    assert _rows(pg, "SELECT COUNT(*) FROM jobdiva_jobs WHERE detail_synced_at IS NULL")[0][0] == 0
    assert _rows(pg, "SELECT COUNT(*) FROM jobdiva_job_users")[0][0] == 3

    later = T0 + datetime.timedelta(hours=2)
    fake2 = FakeJobDiva()
    _cycle(pg, fake2, later, monkeypatch)
    assert "IssuedJobsList" not in fake2.names()  # backfill is done
    assert "NewUpdatedUserRecords" not in fake2.names()  # directory is < 24h old
    assert "OpenJobsList" not in fake2.names()  # open reqs are < 24h old
    incremental = [p for path, p in fake2.calls if path.endswith("SubmittalInterviewHireActivitiesList")]
    assert incremental == [{"fromDate": "09/24/2026 08:30:00", "toDate": "09/24/2026 11:00:00"}]  # 30 min overlap


def test_pg_details_are_fetched_for_jobs_only_activity_knows(pg, monkeypatch):
    sync.store_activities(pg, [{**ACTIVITY, "ACTIVITYID": "77", "JOBID": 31000001}])
    with pg.cursor() as cur:
        cur.execute("INSERT INTO monitored_jobs VALUES ('31000002', '26-9'), ('26-9-v2', '26-9-v2')")
    assert sorted(sync._jobs_needing_detail(pg, 50)) == ["31000001", "31000002"]  # never the non-numeric clone id


def test_pg_a_failing_feed_keeps_its_cursor_and_the_others_still_run(pg, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_DAYS", 7)
    fake = FakeJobDiva()
    fake.fail.add("SubmittalInterviewHireActivitiesList")
    summary = _cycle(pg, fake, T0, monkeypatch)
    assert str(summary["activities"]).startswith("failed")
    assert summary["jobs"] and summary["job_users"]
    cursor, done, error = _rows(
        pg, "SELECT cursor_at, backfill_done, last_error FROM jobdiva_bi_sync_state WHERE feed = 'activities'"
    )[0]
    assert cursor is None and done is False and "down" in error

    fake.fail.clear()
    _cycle(pg, fake, T0 + datetime.timedelta(minutes=30), monkeypatch)
    assert _rows(pg, "SELECT backfill_done FROM jobdiva_bi_sync_state WHERE feed = 'activities'")[0][0] is True


def test_pg_rate_limit_ends_the_cycle(pg, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_DAYS", 7)

    class Limited(FakeJobDiva):
        async def __call__(self, path, params):
            if path.endswith("NewUpdatedJobRecords"):
                self.calls.append((path, params))
                raise sync.JobDivaRateLimited("429")
            return await super().__call__(path, params)

    fake = Limited()
    summary = _cycle(pg, fake, T0, monkeypatch)
    assert str(summary["jobs"]).startswith("rate limited")
    assert "activities" not in summary
    assert "SubmittalInterviewHireActivitiesList" not in fake.names()


def test_pg_cycle_skips_when_another_worker_holds_the_lock(pg, monkeypatch):
    try:
        other = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(str(exc))
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (sync.SYNC_LOCK_KEY,))
        fake = FakeJobDiva()
        summary = _cycle(pg, fake, T0, monkeypatch)
        assert "skipped" in summary and fake.calls == []
    finally:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (sync.SYNC_LOCK_KEY,))
        other.close()


def test_pg_lock_is_released_after_a_cycle(pg, monkeypatch):
    monkeypatch.setattr(sync, "BACKFILL_DAYS", 7)
    _cycle(pg, FakeJobDiva(), T0, monkeypatch)
    other = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (sync.SYNC_LOCK_KEY,))
            assert cur.fetchone()[0] is True
            cur.execute("SELECT pg_advisory_unlock(%s)", (sync.SYNC_LOCK_KEY,))
    finally:
        other.close()
