"""Unit tests for the daily PAIR launch report (routers/launch_report.py).

Covers the pieces that are easy to get subtly wrong and expensive to notice:

  (a) status bucketing, including the unknown-value fallback — the vocabulary
      lives in pair-bot and can grow without pair knowing
  (b) duration maths, including the negative-span guard
  (c) Eastern day-boundary handling: a late-evening launch belongs to that
      Eastern day, not the next UTC one, across both EDT and EST
  (d) the two-key (jobdiva_id / job_id) merge, including the empty-string
      jobdiva_id case that would otherwise pool unrelated jobs
  (e) the percentage metric at each of its edges — zero launched, zero
      resolved, and partially resolved outreach

Real DB connections are blocked by conftest, so the SQL itself is exercised
through its generated text and its Python-side equivalents rather than a live
Postgres. The full statements were separately validated against a scratch
Postgres when written.
"""
import datetime

import pytest

from routers import launch_report as lr


# ---------------------------------------------------------------------------
# (a) status bucketing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pending", "pending"),
        ("PENDING", "pending"),
        ("  scheduled  ", "pending"),
        ("phase1", "in_progress"),
        ("phase3", "in_progress"),
        ("in_progress", "in_progress"),
        ("completed", "completed"),
        ("passed", "completed"),
        ("failed", "completed"),
        ("outreach_incomplete", "partial_complete"),
        ("expired", "partial_complete"),
        ("no_response", "partial_complete"),
    ],
)
def test_bucket_status_known_values(raw, expected):
    assert lr._bucket_status(raw) == expected


def test_bucket_status_missing_is_pending():
    # An interview pair-bot has not started reporting on has not been attempted.
    assert lr._bucket_status(None) == "pending"
    assert lr._bucket_status("") == "pending"
    assert lr._bucket_status("   ") == "pending"


def test_bucket_status_unknown_value_falls_back_and_warns(caplog):
    """An unrecognised status must not vanish silently.

    pair-bot owns this vocabulary; a new value showing up is how we learn the
    bucket sets need updating, so it has to be both counted and logged.
    """
    with caplog.at_level("WARNING"):
        assert lr._bucket_status("some_brand_new_state") == "partial_complete"
    assert "some_brand_new_state" in caplog.text


# ---------------------------------------------------------------------------
# (b) duration maths
# ---------------------------------------------------------------------------
def _utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


def test_minutes_between_basic():
    assert lr._minutes_between(_utc(2026, 8, 27, 14, 0), _utc(2026, 8, 27, 15, 30)) == 90.0


def test_minutes_between_negative_span_is_none():
    """A negative span means two services disagree about the clock.

    Reporting it as a duration would be worse than reporting nothing.
    """
    assert lr._minutes_between(_utc(2026, 8, 27, 15, 0), _utc(2026, 8, 27, 14, 0)) is None


def test_minutes_between_missing_end_is_none():
    assert lr._minutes_between(None, _utc(2026, 8, 27, 14, 0)) is None
    assert lr._minutes_between(_utc(2026, 8, 27, 14, 0), None) is None
    assert lr._minutes_between(None, None) is None


def test_parse_iso_tolerates_junk_and_z_suffix():
    assert lr._parse_iso("2026-08-27T14:00:00Z") == _utc(2026, 8, 27, 14, 0)
    # Naive strings are UTC — both writers emit datetime.now(timezone.utc).
    assert lr._parse_iso("2026-08-27T14:00:00") == _utc(2026, 8, 27, 14, 0)
    assert lr._parse_iso("not-a-date") is None
    assert lr._parse_iso("") is None
    assert lr._parse_iso(None) is None


# ---------------------------------------------------------------------------
# (c) Eastern day boundary
# ---------------------------------------------------------------------------
def test_late_evening_edt_launch_belongs_to_that_eastern_day():
    """22:02 EDT on Aug 27 is 02:02 UTC on Aug 28 — it must report as Aug 27."""
    stored = datetime.datetime(2026, 8, 28, 2, 2)  # naive, as read from Postgres
    rendered = lr._edt(lr._from_db(stored))
    assert rendered == "2026-08-27T22:02:00-04:00"
    assert rendered.startswith("2026-08-27")


def test_est_launch_uses_the_winter_offset():
    """The same wall-clock hour in January is EST (-05:00), not EDT."""
    stored = datetime.datetime(2026, 1, 16, 2, 2)
    assert lr._edt(lr._from_db(stored)) == "2026-01-15T21:02:00-05:00"


@pytest.mark.parametrize(
    "stored,expected_date",
    [
        # Spring forward: 2026-03-08, EST -> EDT at 02:00 local.
        (datetime.datetime(2026, 3, 8, 4, 30), "2026-03-07"),   # 23:30 EST on the 7th
        (datetime.datetime(2026, 3, 8, 7, 30), "2026-03-08"),   # 03:30 EDT on the 8th
        # Fall back: 2026-11-01, EDT -> EST at 02:00 local.
        (datetime.datetime(2026, 11, 1, 3, 30), "2026-10-31"),  # 23:30 EDT on Oct 31
        (datetime.datetime(2026, 11, 1, 6, 30), "2026-11-01"),  # 01:30 EST on Nov 1
    ],
)
def test_day_boundary_across_dst_transitions(stored, expected_date):
    assert lr._edt(lr._from_db(stored)).startswith(expected_date)


def test_eastern_date_expr_converts_through_both_zones():
    """The SQL must interpret the naive column, then shift it to Eastern.

    Two placeholders in this order — a swap silently produces the wrong day,
    which is exactly the bug this expression exists to prevent.
    """
    sql = lr._eastern_date_expr("l.first_launch_at")
    assert sql.count("%s") == 2
    assert sql == "((l.first_launch_at AT TIME ZONE %s) AT TIME ZONE %s)::date"


def test_jobs_query_binds_params_in_statement_order():
    """Scope params precede the two timezones, which precede the date.

    psycopg2 binds positionally, so this ordering is load-bearing.
    """
    captured = {}

    class _Cur:
        description = []

        def execute(self, sql, params):
            captured["sql"], captured["params"] = sql, params

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    scope = {"job_ids": ["55", "56"], "sc_keys": ["55", "26-01234"]}
    lr._fetch_jobs_launched_on(_Conn(), datetime.date(2026, 8, 27), scope)

    assert captured["params"] == [
        ["55", "56"],
        lr.REPORT_DB_TIMEZONE,
        str(lr.REPORT_TIMEZONE),
        datetime.date(2026, 8, 27),
    ]
    # The empty-string guard must survive refactors: without it, an audit row
    # with jobdiva_id='' joins every job whose jobdiva_id is also ''.
    assert "NULLIF(a.jobdiva_id, '') IS NOT NULL" in captured["sql"]
    assert "NULLIF(mj.jobdiva_id, '')" in captured["sql"]


# ---------------------------------------------------------------------------
# (d) two-key merge
# ---------------------------------------------------------------------------
def test_keys_for_includes_both_key_variants():
    assert lr._keys_for({"job_id": 55, "jobdiva_id": "26-01234"}) == ["26-01234", "55"]


def test_keys_for_drops_blank_jobdiva_id():
    """A job with no JobDiva reference must not contribute '' as a lookup key.

    monitor_job_locally stores '' rather than NULL, and '' would match every
    other referenceless job's rows.
    """
    assert lr._keys_for({"job_id": 55, "jobdiva_id": ""}) == ["55"]
    assert lr._keys_for({"job_id": 55, "jobdiva_id": "   "}) == ["55"]
    assert lr._keys_for({"job_id": 55, "jobdiva_id": None}) == ["55"]


# ---------------------------------------------------------------------------
# (e) percentage
# ---------------------------------------------------------------------------
def _outreach(status, phase="phase1", comms=None):
    return {
        "outreach": {"outreach_status": status, "outreach_phase": phase},
        "communications": comms or [],
    }


def _job(total_launched):
    return {
        "job_id": 55,
        "jobdiva_id": "26-01234",
        "title": "Data Engineer",
        "enhanced_title": None,
        "customer_name": "Acme",
        "recruiter_emails": '["r@x.com"]',
        "posted_date": "Aug 25, 2026",
        "time_to_first_pass": None,
        "job_created_at_text": "2026-08-25 12:00:00",
        "first_launch_at": datetime.datetime(2026, 8, 28, 2, 2),
        "total_launched": total_launched,
    }


def test_percentage_counts_completed_and_partial():
    audit = [{"interview_id": str(i)} for i in (1, 2, 3, 4)]
    by_iid = {
        "1": _outreach("completed"),
        "2": _outreach("outreach_incomplete"),
        "3": _outreach("pending"),
        "4": _outreach("phase2"),
    }
    row = lr._build_row(_job(4), [], audit, by_iid)
    assert (row["completed"], row["partial_complete"]) == (1, 1)
    assert row["percentage"] == 50.0


def test_percentage_is_none_when_nothing_launched():
    assert lr._build_row(_job(0), [], [], {})["percentage"] is None


def test_percentage_is_none_when_outreach_resolved_nothing():
    """A silent pair-bot must not render as 0% — that reads as a real result."""
    audit = [{"interview_id": "1"}, {"interview_id": "2"}]
    row = lr._build_row(_job(2), [], audit, {})
    assert row["percentage"] is None
    assert (row["outreach_detail_resolved"], row["outreach_detail_expected"]) == (0, 2)


def test_percentage_uses_full_launched_count_when_partially_resolved():
    """Numerator counts only answered interviews; denominator stays the full
    launched count, so a partial fetch reads low rather than inventing data.
    The row exposes resolved/expected so the UI can flag it.
    """
    audit = [{"interview_id": str(i)} for i in (1, 2, 3, 4)]
    row = lr._build_row(_job(4), [], audit, {"1": _outreach("completed")})
    assert row["percentage"] == 25.0  # 1 of 4, not 1 of 1
    assert (row["outreach_detail_resolved"], row["outreach_detail_expected"]) == (1, 4)


# ---------------------------------------------------------------------------
# outreach aggregation
# ---------------------------------------------------------------------------
def test_channel_counts_are_per_candidate_not_per_message():
    """"SMS: 1" means one candidate was SMS'd, however many times."""
    payload = _outreach(
        "completed",
        comms=[
            {"channel": "sms", "sent_at": "2026-08-27T14:00:00Z", "response_at": None},
            {"channel": "sms", "sent_at": "2026-08-27T16:00:00Z", "response_at": None},
            {"channel": "email", "sent_at": "2026-08-27T14:00:00Z", "response_at": None},
        ],
    )
    summary = lr._summarise_outreach([payload])
    # 'email' is the Web column — pair-bot has no 'web' outreach channel.
    assert summary["channels"] == {"call": 0, "sms": 1, "web": 1}


def test_channel_counts_accept_source_fallback_keys():
    payload = _outreach(
        "completed",
        comms=[
            {"source": "phone", "sent_at": "2026-08-27T14:00:00Z", "response_at": None},
            {"communication_source": "text", "sent_at": "2026-08-27T14:05:00Z", "response_at": None},
        ],
    )
    summary = lr._summarise_outreach([payload])
    assert summary["channels"] == {"call": 1, "sms": 1, "web": 0}


def test_channel_counts_fall_back_to_outreach_level_source_when_comms_empty():
    payload = {
        "outreach": {"outreach_status": "completed", "outreach_phase": "phase1", "source": "email"},
        "communications": [],
    }
    summary = lr._summarise_outreach([payload])
    assert summary["channels"] == {"call": 0, "sms": 0, "web": 1}


def test_response_times_use_first_contact_and_first_reply():
    payloads = [
        _outreach("completed", comms=[
            {"channel": "email", "sent_at": "2026-08-27T14:00:00Z", "response_at": "2026-08-27T14:20:00Z"},
        ]),
        _outreach("completed", comms=[
            {"channel": "call", "sent_at": "2026-08-27T14:00:00Z", "response_at": "2026-08-27T15:00:00Z"},
        ]),
        _outreach("pending", comms=[]),
    ]
    summary = lr._summarise_outreach(payloads)
    assert summary["time_to_first_response_minutes"] == 20.0   # fastest responder
    assert summary["overall_response_time_minutes"] == 40.0    # mean of 20 and 60
    assert summary["responded_count"] == 2


def test_phase_distribution_normalizes_phase_variants():
    payloads = [_outreach("pending", phase=p) for p in ("phase1", "phase 1", "phase_2", "3", "contact_check")]
    assert lr._summarise_outreach(payloads)["phases"] == {"phase1": 3, "phase2": 1, "phase3": 1}


def test_phase_distribution_falls_back_to_status_when_phase_missing():
    payload = {"outreach": {"outreach_status": "phase2"}, "communications": []}
    summary = lr._summarise_outreach([payload])
    assert summary["phases"] == {"phase1": 0, "phase2": 1, "phase3": 0}


def test_outstanding_feedback_never_goes_negative():
    """More feedback than completions (e.g. a candidate actioned before the
    webhook landed) must clamp at zero, not render as a negative backlog.
    """
    audit = [{"interview_id": "1"}]
    candidates = [
        {"candidate_id": "c1", "created_at": None, "feedback_type": "Submit",
         "feedback_reason": "ok", "feedback_at": None, "engage_completed_at": None},
        {"candidate_id": "c2", "created_at": None, "feedback_type": "Reject",
         "feedback_reason": "no", "feedback_at": None, "engage_completed_at": None},
    ]
    row = lr._build_row(_job(1), candidates, audit, {"1": _outreach("completed")})
    assert row["completed"] == 1
    assert row["outstanding_feedback"] == 0


# ---------------------------------------------------------------------------
# PAIR Published / PAIR Launch / Turn Around Time
# ---------------------------------------------------------------------------
def test_pair_published_reads_the_job_arrival_time():
    """PAIR Published is when the job was brought into pair (the
    monitored_jobs row's birth), not monitored_jobs.pair_launched_at.
    """
    row = lr._build_row(
        {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"},
        [], [], {},
    )
    assert row["pair_published_at"] == "2026-08-25T08:00:00-04:00"  # 12:00 UTC -> 08:00 EDT


def test_pair_published_honours_the_ist_suffix():
    """readable_ist_now() rows are India wall-clock, not DB-zone.

    Reading "12:00:00 IST" as UTC would put PAIR Published 5.5h late.
    """
    row = lr._build_row(
        {**_job(0), "job_created_at_text": "2026-08-25 12:00:00 IST"},
        [], [], {},
    )
    # 12:00 IST == 06:30 UTC == 02:30 EDT
    assert row["pair_published_at"] == "2026-08-25T02:30:00-04:00"


def test_pair_published_survives_a_garbage_value():
    row = lr._build_row({**_job(0), "job_created_at_text": "not a timestamp"}, [], [], {})
    assert row["pair_published_at"] is None
    assert row["turn_around_time_minutes"] is None


def test_turn_around_time_is_launch_minus_published():
    job = {
        **_job(1),
        "job_created_at_text": "2026-08-27 12:00:00",          # 12:00 UTC
        "first_launch_at": datetime.datetime(2026, 8, 27, 14, 30),  # 14:30 UTC
    }
    row = lr._build_row(job, [], [{"interview_id": "1"}], {"1": _outreach("completed")})
    assert row["turn_around_time_minutes"] == 150.0


def test_turn_around_time_is_distinct_from_time_to_launch():
    """Time to Launch spans JobDiva posting -> launch; Turn Around Time spans
    only the stretch pair owns. They must not collapse into one number.
    """
    job = {
        **_job(1),
        "posted_date": "Aug 25, 2026",
        "job_created_at_text": "2026-08-27 12:00:00",
        "first_launch_at": datetime.datetime(2026, 8, 27, 14, 30),
    }
    row = lr._build_row(job, [], [], {})
    assert row["turn_around_time_minutes"] == 150.0
    # Aug 25 00:00 EDT -> Aug 27 10:30 EDT
    assert row["time_to_launch_minutes"] == 3510.0
    assert row["time_to_launch_minutes"] != row["turn_around_time_minutes"]


def test_time_to_launch_is_none_without_a_jobdiva_date():
    job = {**_job(1), "posted_date": "", "job_created_at_text": "2026-08-27 12:00:00"}
    assert lr._build_row(job, [], [], {})["time_to_launch_minutes"] is None


def test_time_to_source_runs_from_pair_published():
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [{
        "candidate_id": "c1",
        "created_at": datetime.datetime(2026, 8, 26, 12, 0),
        "feedback_type": None, "feedback_reason": None,
        "feedback_at": None, "engage_completed_at": None,
    }]
    assert lr._build_row(job, candidates, [], {})["time_to_source_minutes"] == 1440.0


# ---------------------------------------------------------------------------
# Job versions
# ---------------------------------------------------------------------------
def test_each_job_version_is_its_own_report_row():
    """"Edit Job Setup" clones a job into a versioned monitored_jobs row.

    Grouping by mj.job_id is what keeps v1 and v2 as separate report rows; a
    GROUP BY on jobdiva_id or parent_job_id would merge their launches.
    """
    captured = {}

    class _Cur:
        description = []

        def execute(self, sql, params):
            captured["sql"] = sql

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    lr._fetch_jobs_launched_on(_Conn(), datetime.date(2026, 8, 27), None)
    assert "GROUP BY mj.job_id" in captured["sql"]
    assert "parent_job_id" not in captured["sql"]


def test_version_keys_do_not_overlap_between_versions():
    """v2 carries its own ref, so neither of its keys can match v1's rows."""
    v1 = lr._keys_for({"job_id": "26-06182", "jobdiva_id": "26-06182"})
    v2 = lr._keys_for({"job_id": "26-06182-v2", "jobdiva_id": "26-06182-v2"})
    assert set(v1).isdisjoint(v2)


def test_version_is_surfaced_and_defaults_to_one():
    assert lr._build_row({**_job(0), "version": 2}, [], [], {})["version"] == 2
    assert lr._build_row({**_job(0), "version": None}, [], [], {})["version"] == 1
    assert lr._build_row(_job(0), [], [], {})["version"] == 1


# ---------------------------------------------------------------------------
# Every emitted timestamp is Eastern
# ---------------------------------------------------------------------------
def test_all_emitted_timestamps_carry_an_eastern_offset():
    """No field may leak a UTC/naive timestamp to the UI or the CSV."""
    job = {
        **_job(1),
        "job_created_at_text": "2026-08-27 12:00:00",
        "first_launch_at": datetime.datetime(2026, 8, 28, 2, 2),
    }
    row = lr._build_row(job, [], [{"interview_id": "1"}], {"1": _outreach("completed")})
    for field in ("pair_published_at", "pair_launch_at"):
        assert row[field].endswith(("-04:00", "-05:00")), (field, row[field])


def test_ist_and_now_written_rows_both_land_in_eastern():
    """The two shapes this column is written in must agree on the timezone.

    JobDiva import / manual create stamp readable_ist_now(); the version clone
    stamps NOW(). Both must render as Eastern.
    """
    ist = lr._build_row({**_job(0), "job_created_at_text": "2026-08-20 17:30:00 IST"}, [], [], {})
    now = lr._build_row({**_job(0), "job_created_at_text": "2026-08-27 18:00:00"}, [], [], {})
    assert ist["pair_published_at"] == "2026-08-20T08:00:00-04:00"   # 17:30 IST -> 08:00 EDT
    assert now["pair_published_at"] == "2026-08-27T14:00:00-04:00"   # 18:00 UTC -> 14:00 EDT


# ---------------------------------------------------------------------------
# Integration: the query really is executed, not just string-matched
# ---------------------------------------------------------------------------
# The substring assertions above pin the SQL's shape; these run it against a
# real Postgres so a change that keeps the same GROUP BY text but breaks the
# actual grouping or join is still caught. Everything happens in TEMP tables
# inside a transaction that is always rolled back, so no real schema is
# touched. Skipped when no Postgres is reachable (set LAUNCH_REPORT_TEST_DSN
# to point at one).
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_FIXTURE_SQL = """
CREATE TEMP TABLE monitored_jobs (
  job_id TEXT, jobdiva_id TEXT, title TEXT, enhanced_title TEXT, customer_name TEXT,
  recruiter_emails TEXT, posted_date TEXT, time_to_first_pass DOUBLE PRECISION,
  parent_job_id TEXT, version INT, created_at TEXT) ON COMMIT DROP;
CREATE TEMP TABLE engage_interview_audit (
  candidate_id VARCHAR(255), jobdiva_id VARCHAR(255), interview_id VARCHAR(255),
  created_at TIMESTAMP) ON COMMIT DROP;

INSERT INTO monitored_jobs VALUES
  -- v1 arrived via JobDiva import (readable_ist_now string)
  ('26-06182','26-06182','Data Engineer',NULL,'Acme','["r@x.com"]','Aug 20, 2026',NULL,NULL,1,
   '2026-08-20 17:30:00 IST'),
  -- v2 cloned by Edit Job Setup (NOW(), plain timestamp)
  ('26-06182-v2','26-06182-v2','Data Engineer',NULL,'Acme','["r@x.com"]','Aug 20, 2026',NULL,
   '26-06182',2,'2026-08-27 18:00:00'),
  -- two referenceless jobs: jobdiva_id is '' rather than NULL
  ('60','','Ghost A',NULL,'Beta','[]','',NULL,NULL,1,'2026-08-27 09:00:00'),
  ('61','','Ghost B',NULL,'Beta','[]','',NULL,NULL,1,'2026-08-27 09:00:00');

INSERT INTO engage_interview_audit VALUES
  ('c1','26-06182',   '901','2026-08-28 01:00:00'),   -- keyed by ref
  ('c2','26-06182',   '902','2026-08-28 01:05:00'),
  ('c3','26-06182-v2','903','2026-08-28 02:30:00'),   -- v2's own launch
  ('g1','60',         '960','2026-08-28 02:10:00'),   -- keyed by job_id
  ('g2','',           '961','2026-08-28 02:20:00');   -- blank key: matches nothing
"""


@pytest.fixture()
def pg_conn():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(_FIXTURE_SQL)
        yield conn
    finally:
        conn.rollback()  # ON COMMIT DROP never fires; rollback discards everything
        conn.close()


def test_query_returns_one_row_per_job_version(pg_conn):
    """v1 and v2 must each get their own row with their own launch count."""
    jobs = lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 27), None)
    by_id = {j["job_id"]: j for j in jobs}
    assert by_id["26-06182"]["total_launched"] == 2
    assert by_id["26-06182-v2"]["total_launched"] == 1


def test_query_does_not_pool_referenceless_jobs(pg_conn):
    """An audit row with jobdiva_id='' must attach to no job at all.

    Without the NULLIF guard it would join every job whose jobdiva_id is also
    '', silently merging Ghost A and Ghost B.
    """
    jobs = lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 27), None)
    by_id = {j["job_id"]: j for j in jobs}
    assert by_id["60"]["total_launched"] == 1   # only its own job_id-keyed row
    assert "61" not in by_id                    # no launches, so absent entirely


def test_query_filters_on_the_eastern_calendar_day(pg_conn):
    """Every launch here is on Aug 28 in UTC but Aug 27 in Eastern."""
    assert lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 27), None)
    assert lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 28), None) == []


def test_query_rows_render_both_timestamp_shapes_in_eastern(pg_conn):
    jobs = lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 27), None)
    by_id = {j["job_id"]: j for j in jobs}
    v1 = lr._build_row(by_id["26-06182"], [], [], {})
    v2 = lr._build_row(by_id["26-06182-v2"], [], [], {})
    assert v1["pair_published_at"] == "2026-08-20T08:00:00-04:00"   # IST string
    assert v2["pair_published_at"] == "2026-08-27T14:00:00-04:00"   # NOW() timestamp
    assert v2["version"] == 2
