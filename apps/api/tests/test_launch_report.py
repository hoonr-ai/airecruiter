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
  (f) the row is the job's RANK LIST summarised: one unit per launched person
      (their latest interview), over the job's whole lifetime, classified by
      the same code the Rankings page uses — never per interview, never just
      the launch day

Real DB connections are blocked by conftest, so the SQL itself is exercised
through its generated text and its Python-side equivalents rather than a live
Postgres. The full statements were separately validated against a scratch
Postgres when written.
"""
import datetime
import asyncio

import pytest

from core.auth import UserIdentity
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
        assert lr._bucket_status("some_brand_new_state") == "pending"
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


def _launched(iid, cid=None, **fields):
    """A launched-candidate row as services/launched_candidates returns it.

    `interview_id` is the one the report asks pair-bot about; stored status
    lives in `audit_status` / `audit_response` and the `engage_*` JSONB fields.
    """
    return {"candidate_id": cid or f"c_{iid}", "interview_id": iid, **fields}


def _launched_from(candidates):
    """Turn sourced-shaped candidate dicts into launched rows (one interview each)."""
    return [
        {**c, "interview_id": c.get("engage_interview_id") or f"iv_{c['candidate_id']}"}
        for c in candidates
    ]


def test_percentage_counts_completed_and_partial():
    launched = [_launched(str(i)) for i in (1, 2, 3, 4)]
    by_iid = {
        "1": _outreach("completed"),
        "2": _outreach("outreach_incomplete"),
        "3": _outreach("pending"),
        "4": _outreach("phase2"),
    }
    row = lr._build_row(_job(4), launched, [], by_iid)
    assert (row["completed"], row["partial_complete"]) == (1, 1)
    assert row["percentage"] == 50.0


def test_percentage_is_none_when_nothing_launched():
    assert lr._build_row(_job(0), [], [], {})["percentage"] is None


def test_percentage_is_none_when_outreach_resolved_nothing():
    """A silent pair-bot must not render as 0% — that reads as a real result."""
    launched = [_launched("1"), _launched("2")]
    row = lr._build_row(_job(2), launched, [], {})
    assert row["percentage"] is None
    assert (row["outreach_detail_resolved"], row["outreach_detail_expected"]) == (0, 2)


def test_percentage_uses_full_launched_count_when_partially_resolved():
    """Numerator counts only answered candidates; denominator stays the full
    launched count, so a partial fetch reads low rather than inventing data.
    The row exposes resolved/expected so the UI can flag it.
    """
    launched = [_launched(str(i)) for i in (1, 2, 3, 4)]
    row = lr._build_row(_job(4), launched, [], {"1": _outreach("completed")})
    assert row["percentage"] == 25.0  # 1 of 4, not 1 of 1
    assert (row["outreach_detail_resolved"], row["outreach_detail_expected"]) == (1, 4)


# ---------------------------------------------------------------------------
# (f) the row is the rank list: people, latest interview, whole lifetime
# ---------------------------------------------------------------------------
def test_launched_is_the_number_of_launched_people_and_buckets_partition_it():
    """Launched == the rank list's "Candidates Launched"; the four status
    buckets are computed over exactly that set and always sum to it."""
    launched = [
        _launched("1", "c1", engage_status="sent", audit_status="Initiated"),      # just launched → Pending
        _launched("2", "c2", engage_status="in_progress"),
        _launched("3", "c3", engage_status="completed", engage_hard_filter_status="pass"),
        _launched("4", "c4"),                                                       # no status evidence at all
    ]
    row = lr._build_row(_job(4), launched, [], {"4": _outreach("outreach_incomplete")})
    assert row["total_candidates_launched"] == 4
    assert (row["pending"], row["in_progress"], row["completed"], row["partial_complete"]) == (1, 1, 1, 1)
    assert row["pending"] + row["in_progress"] + row["completed"] + row["partial_complete"] == 4
    assert (row["passed_candidates"], row["failed_candidates"]) == (1, 0)
    assert row["percentage"] == 50.0


def test_report_row_equals_the_rankings_header_for_the_same_job():
    """Both screens are summarise_launched_candidates over the same rows; the
    report row must expose exactly the header's numbers, key for key."""
    launched = [
        _launched("1", "c1", engage_status="in_progress", outreach_phase="phase1_6hr"),
        _launched("2", "c2", engage_status="passed", engage_score="91", outreach_phase="phase2"),
        _launched("3", "c3", engage_status="sent", audit_status="Initiated"),
    ]
    live = {"3": {"outreach_status": "failed", "candidate_score": 40, "outreach_phase": "phase3"}}
    header = lr.summarise_launched_candidates(launched, live)
    row = lr._build_row(_job(3), launched, [], live)
    assert row["total_candidates_launched"] == header["launched"] == 3
    for bucket in ("pending", "in_progress", "completed", "partial_complete"):
        assert row[bucket] == header["buckets"][bucket], bucket
    assert row["passed_candidates"] == header["buckets"]["passed"] == 1
    assert row["failed_candidates"] == header["buckets"]["failed"] == 1
    for phase in ("phase1", "phase2", "phase3", "phase4", "extra", "extra1", "extra2", "extra3"):
        assert row[phase] == header["phases"][phase], phase
    assert (row["phase2"], row["phase3"], row["phase4"]) == (1, 1, 1)


def test_report_asks_pair_bot_once_per_launched_person_over_the_whole_lifetime(monkeypatch):
    """No launch-day scoping and no per-interview fan-out: a candidate launched
    a week after the job's first launch is in the row, a re-launched candidate
    is one row on their latest interview, and pair-bot is asked about exactly
    those interviews."""
    captured = {}
    job = {**_job(0), "job_id": "55", "jobdiva_id": "26-01234",
           "first_launch_at": datetime.datetime(2026, 8, 28, 2, 2)}   # 2026-08-27 Eastern
    launched_by_job = {
        "55": [
            # launched on day one
            _launched("day-one", "c1", engage_status="pending", audit_status="Initiated",
                      audit_created_at=datetime.datetime(2026, 8, 28, 2, 10)),
            # launched a week later: still this job's candidate
            _launched("week-later", "c2", engage_status="in_progress",
                      audit_created_at=datetime.datetime(2026, 9, 4, 2, 10)),
            # re-launched: the population already collapsed them onto the newest interview
            _launched("second-try", "c3", engage_status="pending", audit_interview_id="second-try",
                      audit_status="Initiated"),
        ]
    }
    sourced_by_job = {"55": [{"candidate_id": cid, "created_at": datetime.datetime(2026, 8, 26, 12, 0)} for cid in ("c1", "c2", "c3", "never-launched")]}

    def _load_inputs(_start, _end, _scope):
        return [job], launched_by_job, sourced_by_job

    async def _fake_outreach(interview_ids):
        captured["ids"] = list(interview_ids)
        return {"day-one": _outreach("completed"), "week-later": _outreach("in_progress")}

    monkeypatch.setattr(lr, "_load_report_inputs", _load_inputs)
    monkeypatch.setattr(lr, "_fetch_all_outreach", _fake_outreach)

    response = asyncio.run(
        lr.get_launch_report(date=None, start_date="2026-08-27", end_date="2026-08-27",
                             team_id=None, user=_admin_user())
    )
    row = response["data"]["jobs"][0]
    assert captured["ids"] == ["day-one", "second-try", "week-later"]   # sorted, one per person
    assert row["total_candidates_launched"] == 3
    assert row["outreach_detail_expected"] == 3
    assert (row["pending"], row["in_progress"], row["completed"]) == (1, 1, 1)
    assert row["total_candidates_sourced"] == 4                           # lifetime, includes the unlaunched
    assert response["data"]["totals"]["candidates_launched"] == 3


def test_a_candidate_without_any_interview_id_is_not_launched():
    """A failed launch leaves engage_status='failed' and no interview id. The
    shared population excludes them at the SQL, and the row builder has no
    Python side-door that could count a JSONB-only terminal status."""
    from services.launched_candidates import LAUNCHED_CANDIDATES_SQL

    assert "WHERE la.candidate_id IS NOT NULL OR sc.engage_interview_id IS NOT NULL" in LAUNCHED_CANDIDATES_SQL
    assert not hasattr(lr, "apply_uncovered_pass_fail")
    assert not hasattr(lr, "_pass_fail_for_uncovered_candidates")
    # …and sourced-but-unlaunched rows only ever feed the sourcing columns.
    sourced = [{"candidate_id": "s1", "engage_status": "passed", "engage_updated_at": "2026-08-26T10:00:00Z"}]
    row = lr._build_row(_job(0), [], sourced, {})
    assert row["total_candidates_sourced"] == 1
    assert row["total_candidates_launched"] == 0
    assert row["passed_candidates"] == 0


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
    payloads = [_outreach("pending", phase=p) for p in (
        "phase1", "phase 1", "contact_check",
        "phase1_6hr", "phase 2",
        "phase2", "phase 3",
        "phase3", "phase 4",
        "phase1_extra", "phase1_6hr_extra", "phase2_extra"
    )]
    phases = lr._summarise_outreach(payloads, shift_phases=True)["phases"]
    # "phase1", "phase 1", "contact_check" -> phase1 (3)
    # "phase1_6hr", "phase 2" -> phase2 (2)
    # "phase2", "phase 3" -> phase3 (2)
    # "phase3", "phase 4" -> phase4 (2)
    # "phase1_extra" -> extra1 (1), "phase1_6hr_extra" -> extra2 (1), "phase2_extra" -> extra3 (1), total extra (3)
    assert phases["phase1"] == 3
    assert phases["phase2"] == 2
    assert phases["phase3"] == 2
    assert phases["phase4"] == 2
    assert phases["extra1"] == 1
    assert phases["extra2"] == 1
    assert phases["extra3"] == 1
    assert phases["extra"] == 3


def test_summarise_outreach_default_standard_phases():
    """Default summarise_outreach preserves standard unshifted phases for general consumers like jobs.py."""
    payloads = [
        _outreach("pending", phase="phase1"),
        _outreach("pending", phase="phase2"),
        _outreach("pending", phase="phase3"),
    ]
    summary = lr._summarise_outreach(payloads)
    assert summary["phases"]["phase1"] == 1
    assert summary["phases"]["phase2"] == 1
    assert summary["phases"]["phase3"] == 1


def test_summarise_outreach_passed_failed_sub_buckets():
    failed_payload = _outreach("failed")
    failed_payload["score"] = 50  # Candidate engaged and failed
    payloads = [
        _outreach("passed"),
        failed_payload,
        _outreach("completed"),
    ]
    summary = lr._summarise_outreach(payloads)
    assert summary["buckets"]["passed"] == 2
    assert summary["buckets"]["failed"] == 1
    assert summary["buckets"]["completed"] == 3


def test_summarise_outreach_unengaged_failed_buckets_as_pending():
    # Failed status without any score/response/phase should be bucketed as pending.
    # A candidate who got an outreach_phase is considered to have been contacted;
    # we use a flat payload with no phase to simulate a never-contacted scenario.
    payload = {"outreach": {"outreach_status": "failed"}, "communications": []}
    summary = lr._summarise_outreach([payload])
    assert summary["buckets"]["pending"] == 1
    assert summary["buckets"]["failed"] == 0



def test_phase_distribution_falls_back_to_status_when_phase_missing():
    payload = {"outreach": {"outreach_status": "phase2"}, "communications": []}
    summary = lr._summarise_outreach([payload], shift_phases=True)
    assert summary["phases"] == {"phase1": 0, "phase2": 0, "phase3": 1, "phase4": 0, "extra": 0, "extra1": 0, "extra2": 0, "extra3": 0}


def test_phase_distribution_does_not_promote_pending_status_to_phase1_when_phase_missing():
    payload = {"outreach": {"outreach_status": "queued"}, "communications": []}
    summary = lr._summarise_outreach([payload], shift_phases=True)
    assert summary["phases"] == {"phase1": 0, "phase2": 0, "phase3": 0, "phase4": 0, "extra": 0, "extra1": 0, "extra2": 0, "extra3": 0}


def test_phase_distribution_does_not_promote_contact_check_status_to_phase1_when_phase_missing():
    payload = {"outreach": {"outreach_status": "contact_check"}, "communications": []}
    summary = lr._summarise_outreach([payload], shift_phases=True)
    assert summary["phases"] == {"phase1": 0, "phase2": 0, "phase3": 0, "phase4": 0, "extra": 0, "extra1": 0, "extra2": 0, "extra3": 0}


def test_outstanding_feedback_never_goes_negative():
    """More feedback than completions (e.g. a candidate actioned before the
    webhook landed) must clamp at zero, not render as a negative backlog.
    """
    launched = [_launched("1", "c1")]
    candidates = [
        {"candidate_id": "c1", "created_at": None, "feedback_type": "Submit",
         "feedback_reason": "ok", "feedback_at": None, "engage_completed_at": None},
        {"candidate_id": "c2", "created_at": None, "feedback_type": "Reject",
         "feedback_reason": "no", "feedback_at": None, "engage_completed_at": None},
    ]
    row = lr._build_row(_job(1), launched, candidates, {"1": _outreach("completed")})
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
    row = lr._build_row(job, [_launched("1")], [], {"1": _outreach("completed")})
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
    assert lr._build_row(job, [], candidates, {})["time_to_source_minutes"] == 1440.0


def test_first_feedback_at_earliest_wins():
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        {"candidate_id": "c1", "feedback_at": "2026-08-27T10:00:00Z"},
        {"candidate_id": "c2", "feedback_at": "2026-08-26T10:00:00Z"},
        {"candidate_id": "c3"},  # no feedback
    ]
    row = lr._build_row(job, [], candidates, {})
    # EDT offset from UTC is -04:00, so 10:00 UTC = 06:00 EDT
    assert row["first_feedback_at"] == "2026-08-26T06:00:00-04:00"


def test_first_feedback_at_is_none_when_no_feedback():
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        {"candidate_id": "c1"},
        {"candidate_id": "c2"},
    ]
    assert lr._build_row(job, [], candidates, {})["first_feedback_at"] is None


def test_passed_and_failed_candidates_basic():
    """pass/passed/hired -> passed; fail/failed/rejected with engage_score -> failed."""
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        {"candidate_id": "c1", "engage_status": "passed", "engage_completed_at": "2026-08-27T10:00:00Z"},
        {"candidate_id": "c2", "engage_status": "pass", "engage_completed_at": "2026-08-26T10:00:00Z"},
        {"candidate_id": "c3", "engage_status": "hired", "engage_completed_at": "2026-08-28T10:00:00Z"},
        {"candidate_id": "c4", "engage_status": "failed", "engage_score": "72.5"},
        {"candidate_id": "c5", "engage_status": "fail", "engage_score": "60.0"},
        {"candidate_id": "c6", "engage_status": "rejected", "engage_score": "55.0"},
        {"candidate_id": "c7", "engage_status": "in_progress"},
    ]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["passed_candidates"] == 3  # passed, pass, hired
    assert row["failed_candidates"] == 3  # failed, fail, rejected — all have engage_score
    # 2026-08-26T10:00:00Z is 06:00 EDT — the earliest pass
    assert row["first_pass_at"] == "2026-08-26T06:00:00-04:00"


def test_failed_without_engage_score_is_not_counted():
    """failed/rejected with no engage_score = outreach provisioning failure, not interview fail."""
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        # No engage_score -> outreach failure, should not count as a real failed interview
        {"candidate_id": "c1", "engage_status": "failed"},
        {"candidate_id": "c2", "engage_status": "rejected"},
        {"candidate_id": "c3", "engage_status": "fail"},
        # Has engage_score -> real interview result
        {"candidate_id": "c4", "engage_status": "failed", "engage_score": "65.0"},
    ]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["failed_candidates"] == 1  # only c4 has engage_score
    assert row["passed_candidates"] == 0


def test_completed_status_with_hard_filter_pass_counts_as_passed():
    """completed + hf_status in ('', 'pass', 'passed', 'not_hard_filter') -> Pass."""
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        {"candidate_id": "c1", "engage_status": "completed",
         "engage_hard_filter_status": "not_hard_filter",
         "engage_completed_at": "2026-08-26T10:00:00Z"},
        {"candidate_id": "c2", "engage_status": "completed",
         "engage_hard_filter_status": "",
         "engage_completed_at": "2026-08-27T10:00:00Z"},
    ]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["passed_candidates"] == 2
    assert row["failed_candidates"] == 0
    assert row["first_pass_at"] == "2026-08-26T06:00:00-04:00"


def test_completed_status_with_hard_filter_fail_counts_as_failed():
    """completed + hf_status not in pass group -> Fail."""
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        {"candidate_id": "c1", "engage_status": "completed",
         "engage_hard_filter_status": "hard_filter"},
    ]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["passed_candidates"] == 0
    assert row["failed_candidates"] == 1
    assert row["first_pass_at"] is None


def test_first_pass_at_is_none_when_no_passes():
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        # Has engage_score so it IS a real fail, but not a pass
        {"candidate_id": "c1", "engage_status": "failed", "engage_score": "50.0",
         "engage_completed_at": "2026-08-27T10:00:00Z"},
        {"candidate_id": "c2", "engage_status": "in_progress"},
    ]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["passed_candidates"] == 0
    assert row["first_pass_at"] is None


def test_first_pass_at_falls_back_to_engage_updated_at():
    job = {**_job(0), "job_created_at_text": "2026-08-25 12:00:00"}
    candidates = [
        {"candidate_id": "c1", "engage_status": "passed",
         "engage_updated_at": "2026-08-26T10:00:00Z"},
    ]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["passed_candidates"] == 1
    assert row["first_pass_at"] == "2026-08-26T06:00:00-04:00"


def test_live_completed_status_counts_as_passed_even_if_jsonb_is_stale():
    """Rankings merge live pair-bot `completed` into Pass; launch report must too.

    Stale sourced_candidates.engage_status=in_progress used to leave Passed=0
    and First Pass Completed At blank even when time_to_first_pass was set.
    """
    job = {
        **_job(1),
        "first_launch_at": datetime.datetime(2026, 8, 26, 10, 0, tzinfo=datetime.timezone.utc),
        "time_to_first_pass": 7.0,
    }
    candidates = [{
        "candidate_id": "c1",
        "engage_interview_id": "1",
        "engage_status": "in_progress",
        "engage_updated_at": "2026-08-26T10:07:00Z",
        "created_at": datetime.datetime(2026, 8, 26, 9, 0, tzinfo=datetime.timezone.utc),
    }]
    row = lr._build_row(job, _launched_from(candidates), candidates, {"1": _outreach("completed")})
    assert row["completed"] == 1
    assert row["passed_candidates"] == 1
    assert row["failed_candidates"] == 0
    assert row["first_pass_at"] == "2026-08-26T06:07:00-04:00"
    assert row["time_to_first_pass_minutes"] == 7.0


def test_pass_fail_counts_a_launch_whose_audit_row_was_lost():
    """A launch stamps the interview id into the JSONB as well as the audit
    log; if the audit insert was lost the person is still launched (the shared
    population picks them up off the JSONB) and their stored Pass counts."""
    job = {**_job(1), "job_created_at_text": "2026-08-25 12:00:00"}
    launched = [
        _launched("1", "c1", engage_status="in_progress", audit_status="Initiated"),
        # no audit side: audit_status / audit_response are None
        _launched("2", "c2", engage_status="passed", engage_updated_at="2026-08-26T10:00:00Z"),
    ]
    row = lr._build_row(job, launched, [], {"1": _outreach("completed")})
    assert row["total_candidates_launched"] == 2
    assert row["passed_candidates"] == 2
    assert row["failed_candidates"] == 0
    assert row["first_pass_at"] == "2026-08-26T06:00:00-04:00"


def test_qualified_status_is_completed_and_passed():
    payloads = [_outreach("qualified")]
    summary = lr._summarise_outreach(payloads)
    assert summary["buckets"]["completed"] == 1
    assert summary["buckets"]["passed"] == 1
    assert summary["buckets"]["pending"] == 0


def test_time_to_first_pass_falls_back_when_derived_stamp_is_before_launch():
    job = {
        **_job(1),
        "first_launch_at": datetime.datetime(2026, 8, 26, 12, 0, tzinfo=datetime.timezone.utc),
        "time_to_first_pass": 7.0,
    }
    candidates = [{
        "candidate_id": "c1",
        "engage_status": "passed",
        "engage_updated_at": "2026-08-26T10:00:00Z",
    }]
    row = lr._build_row(job, _launched_from(candidates), candidates, {})
    assert row["first_pass_at"] == "2026-08-26T06:00:00-04:00"
    assert row["time_to_first_pass_minutes"] == 7.0



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
# Request validation
# ---------------------------------------------------------------------------
def _admin_user() -> UserIdentity:
    return UserIdentity(email="admin@example.com", role="admin")


def test_launch_report_rejects_ranges_over_the_server_cap_before_db(monkeypatch):
    called = False

    def _fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("range validation must run before DB reads")

    monkeypatch.setattr(lr, "_load_report_inputs", _fail_if_called)

    with pytest.raises(lr.HTTPException) as exc:
        asyncio.run(
            lr.get_launch_report(
                date=None,
                start_date="2026-01-01",
                end_date="2026-02-01",
                team_id=None,
                user=_admin_user(),
            )
        )

    assert exc.value.status_code == 400
    assert str(lr.MAX_LAUNCH_REPORT_RANGE_DAYS) in exc.value.detail
    assert called is False


def test_launch_report_accepts_range_at_the_server_cap(monkeypatch):
    captured = {}

    def _load_inputs(start_date, end_date, scope_team_id):
        captured["dates"] = (start_date, end_date)
        captured["scope_team_id"] = scope_team_id
        return [], {}, {}

    async def _no_outreach(_interview_ids):
        return {}

    monkeypatch.setattr(lr, "_load_report_inputs", _load_inputs)
    monkeypatch.setattr(lr, "_fetch_all_outreach", _no_outreach)

    end_date = datetime.date(2026, 2, 1)
    start_date = end_date - datetime.timedelta(days=lr.MAX_LAUNCH_REPORT_RANGE_DAYS - 1)
    response = asyncio.run(
        lr.get_launch_report(
            date=None,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            team_id=None,
            user=_admin_user(),
        )
    )

    assert response["status"] == "success"
    assert captured["dates"] == (start_date, end_date)
    assert captured["scope_team_id"] is None


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
    row = lr._build_row(job, [_launched("1")], [], {"1": _outreach("completed")})
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
  id SERIAL PRIMARY KEY,
  candidate_id VARCHAR(255), jobdiva_id VARCHAR(255), interview_id VARCHAR(255),
  status VARCHAR(50), response JSONB,
  created_at TIMESTAMP) ON COMMIT DROP;
CREATE TEMP TABLE sourced_candidates (
  id SERIAL PRIMARY KEY, jobdiva_id TEXT, candidate_id TEXT, email TEXT, phone TEXT,
  data JSONB, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP) ON COMMIT DROP;

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

INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, status, created_at) VALUES
  ('c1','26-06182',   '901','Initiated','2026-08-28 01:00:00'),   -- keyed by ref
  ('c2','26-06182',   '902','Initiated','2026-08-28 01:05:00'),
  ('c3','26-06182-v2','903','Initiated','2026-08-28 02:30:00'),   -- v2's own launch
  ('g1','60',         '960','Initiated','2026-08-28 02:10:00'),   -- keyed by job_id
  ('g2','',           '961','Initiated','2026-08-28 02:20:00');   -- blank key: matches nothing
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
    from services.launched_candidates import count_launched_candidates

    jobs = lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 27), None)
    by_id = {j["job_id"]: j for j in jobs}
    assert {"26-06182", "26-06182-v2"} <= set(by_id)
    assert count_launched_candidates(pg_conn, lr._keys_for(by_id["26-06182"])) == 2
    assert count_launched_candidates(pg_conn, lr._keys_for(by_id["26-06182-v2"])) == 1


def test_query_does_not_pool_referenceless_jobs(pg_conn):
    """An audit row with jobdiva_id='' must attach to no job at all.

    Without the NULLIF guard it would join every job whose jobdiva_id is also
    '', silently merging Ghost A and Ghost B.
    """
    from services.launched_candidates import count_launched_candidates

    jobs = lr._fetch_jobs_launched_on(pg_conn, datetime.date(2026, 8, 27), None)
    by_id = {j["job_id"]: j for j in jobs}
    assert "60" in by_id
    assert count_launched_candidates(pg_conn, lr._keys_for(by_id["60"])) == 1   # only its own job_id-keyed row
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


def test_summarise_outreach_flat_payload_with_outreach_channel():
    """Flat PairBot webhook payload must properly resolve status, phase and channel."""
    flat_payload = {
        "interview_id": "10144",
        "jobdiva_id": "26-26878",
        "status": "completed",
        "phase": "Phase 3",
        "outreach_phase": "phase3",
        "channel": "Web",
        "outreach_channel": "web",
        "completed_at": "2026-09-02T14:18:42.509867",
    }
    summary = lr._summarise_outreach([flat_payload], shift_phases=True)
    assert summary["buckets"]["completed"] == 1
    assert summary["phases"]["phase4"] == 1
    assert summary["channels"]["web"] == 1


def test_summarise_outreach_mixed_nested_flat_payload():
    """Mixed nested outreach status and flat phase/channel must resolve symmetrically."""
    mixed_payload = {
        "outreach": {"outreach_status": "completed"},
        "outreach_phase": "phase3",
        "outreach_channel": "web",
    }
    summary = lr._summarise_outreach([mixed_payload], shift_phases=True)
    assert summary["buckets"]["completed"] == 1
    assert summary["phases"]["phase4"] == 1
    assert summary["channels"]["web"] == 1


def test_build_row_uses_3_layer_database_fallback_when_pairbot_api_missing_keys():
    """When PairBot live API is missing channel/phase or empty, candidate DB fallback populates the report."""
    job = _job(1)
    launched = [
        _launched(
            "10144", "cand_1",
            engage_status="completed",
            outreach_phase="phase3",
            outreach_channel="web",
            first_completed_at="2026-09-02T14:18:42.509867",
            audit_status="Initiated", audit_response=None,
        )
    ]
    # PairBot live HTTP API returns partial response without channel/phase
    outreach_by_interview = {"10144": {"outreach_status": "completed"}}

    row = lr._build_row(job, launched, [], outreach_by_interview)

    assert row["completed"] == 1
    assert row["web"] == 1
    assert row["phase4"] == 1


def test_launched_row_status_is_the_rank_list_rows_status():
    """Per launched person the report merges exactly what the rank list merges
    for that table row — JSONB fields, latest audit row, live answer — so a
    live in_progress lifts a stored pending, and a live pending never
    downgrades a stored in_progress."""
    rows = [
        _launched("lifted", "c1", engage_status="pending", audit_status="Initiated", outreach_phase="phase1_6hr"),
        _launched("held", "c2", engage_status="in_progress", audit_status="in_progress", outreach_phase="phase3"),
    ]
    live = {
        "lifted": {"outreach_status": "in_progress", "outreach_phase": "phase1_6hr"},
        "held": {"outreach_status": "pending", "outreach_phase": "phase3"},
    }
    summary = lr.summarise_launched_candidates(rows, live)
    assert summary["buckets"]["in_progress"] == 2
    assert summary["buckets"]["pending"] == 0
    assert (summary["phases"]["phase2"], summary["phases"]["phase4"]) == (1, 1)
    assert summary["launched"] == summary["resolved"] == 2


def test_live_outreach_block_cannot_demote_a_stored_in_progress():
    """QA, 2026-09-18: header/report said Pending 6 / In Progress 0 while the
    table showed one In Progress. pair-bot's live body is
    {outreach: {outreach_status, outreach_phase}, communications} and its
    outreach_status stays `pending` while reminders are scheduled, even after
    the interview started. The summariser used to re-apply that block over the
    merged status and wipe the stored `in_progress`."""
    row = _launched("i1", "c1", engage_status="in_progress",
                    audit_status="in_progress", audit_response={"status": "in_progress"})
    live = {"outreach": {"outreach_status": "pending", "outreach_phase": "phase2"}, "communications": []}
    summary = lr.summarise_launched_candidates([row], {"i1": live})
    assert summary["buckets"]["in_progress"] == 1
    assert summary["buckets"]["pending"] == 0
    assert summary["phases"]["phase3"] == 1          # the live phase still counts


def test_unrecognised_live_interview_status_cannot_demote_a_stored_in_progress():
    row = _launched("i1", "c1", engage_status="in_progress", audit_status="in_progress")
    live = {"interview_status": "active", "outreach": {"outreach_status": "pending"}, "communications": []}
    summary = lr.summarise_launched_candidates([row], {"i1": live})
    assert summary["buckets"]["in_progress"] == 1


def test_live_interview_status_lifts_a_launch_time_sent():
    """The mirror-image gap: before the webhook lands only pair-bot knows the
    interview started. Header, report and table all say In Progress."""
    row = _launched("i1", "c1", engage_status="sent", audit_status="Initiated")
    live = {"interview_status": "in_progress", "outreach": {"outreach_status": "pending"}, "communications": []}
    summary = lr.summarise_launched_candidates([row], {"i1": live})
    assert summary["buckets"]["in_progress"] == 1
    assert summary["buckets"]["pending"] == 0


def test_table_and_buckets_classify_one_candidate_identically():
    """candidates.py stamps format_engage_status(select_engage_status(merged))
    on each table row; the buckets must agree for every payload shape."""
    import inspect
    from routers.candidates import get_job_candidates
    from services.engage_status import format_engage_status, select_engage_status

    src = inspect.getsource(get_job_candidates)
    assert "outreach_status = select_engage_status(merged)" in src
    assert 'merged.get("outreach_status") or merged.get("status")' not in src

    started = dict(engage_status="in_progress", audit_status="in_progress", audit_response={"status": "in_progress"})
    stamped = dict(engage_status="sent", audit_status="Initiated", audit_response={"status": "pending"})
    shapes = [
        (started, {"outreach": {"outreach_status": "pending"}, "communications": []}),
        (started, {"interview_status": "in_progress", "outreach": {"outreach_status": "pending"}}),
        (started, {"interview_status": "active", "outreach": {"outreach_status": "pending"}}),
        (started, None),
        (stamped, {"interview_status": "in_progress", "outreach": {"outreach_status": "pending"}}),
        (stamped, {"outreach": {"outreach_status": "in_progress"}}),
        (stamped, {"outreach": {"outreach_status": "completed"}, "hard_filter_status": "failed"}),
        (stamped, None),
    ]
    to_bucket = {"Pending": "pending", "In Progress": "in_progress", "Pass": "completed", "Fail": "completed"}
    for fields, live in shapes:
        row = _launched("i1", "c1", **fields)
        merged = lr.candidate_outreach_payload(row, live)
        table_label = format_engage_status(select_engage_status(merged), lr.score_from_payload(merged), lr.hf_display_from_payload(merged))
        summary = lr.summarise_launched_candidates([row], {"i1": live} if live is not None else {})
        header_bucket = next(k for k in ("pending", "in_progress", "completed", "partial_complete") if summary["buckets"][k])
        assert header_bucket == to_bucket[table_label], (fields, live, table_label, header_bucket)


def test_launched_row_with_no_status_evidence_is_pending_and_unresolved():
    summary = lr.summarise_launched_candidates([_launched("1", "c1")], {})
    assert summary["buckets"]["pending"] == 1
    assert (summary["launched"], summary["resolved"]) == (1, 0)


def test_candidate_score_fallback_makes_a_stored_fail_count_like_the_rank_list():
    """The rank list reads engage_candidate_score when engage_score is unset;
    a Fail without any score is an outreach miss, with one it is a Fail."""
    rows = [
        _launched("1", "c1", engage_status="failed"),                                   # no score → Pending
        _launched("2", "c2", engage_status="failed", engage_candidate_score="42"),      # → Fail
    ]
    summary = lr.summarise_launched_candidates(rows, {})
    assert (summary["buckets"]["pending"], summary["buckets"]["failed"]) == (1, 1)


# ---------------------------------------------------------------------------
# outreach status fetching
# ---------------------------------------------------------------------------
import asyncio

class MockResponse:
    def __init__(self, json_data):
        self._json_data = json_data
    def json(self):
        return self._json_data
    def raise_for_status(self):
        pass

class MockClient:
    def __init__(self, json_data):
        self.json_data = json_data
    async def get(self, url):
        return MockResponse(self.json_data)

def test_fetch_outreach_status_unwraps_apiresponse_envelope():
    async def _test():
        client = MockClient({"success": True, "data": {"outreach_status": "pass", "outreach_phase": "phase2"}})
        semaphore = asyncio.Semaphore(1)
        deadline = asyncio.get_running_loop().time() + 60.0
        
        result = await lr._fetch_outreach_status(client, semaphore, deadline, "123")
        assert result == {"outreach_status": "pass", "outreach_phase": "phase2"}
    asyncio.run(_test())

def test_fetch_outreach_status_handles_legacy_unwrapped_payload():
    async def _test():
        client = MockClient({"outreach_status": "pass", "outreach_phase": "phase2"})
        semaphore = asyncio.Semaphore(1)
        deadline = asyncio.get_running_loop().time() + 60.0
        
        result = await lr._fetch_outreach_status(client, semaphore, deadline, "123")
        assert result == {"outreach_status": "pass", "outreach_phase": "phase2"}
    asyncio.run(_test())


# ---------------------------------------------------------------------------
# Status bucketing and fallback invariant tests (fix/launch-report-metrics)
# ---------------------------------------------------------------------------
def test_bucket_status_extended_mappings():
    assert lr._bucket_status("initiated") == "pending"
    assert lr._bucket_status("not_started") == "pending"
    assert lr._bucket_status("call_in_progress") == "in_progress"
    assert lr._bucket_status("phase4") == "in_progress"
    assert lr._bucket_status("outreach_failed") == "partial_complete"


def test_status_buckets_sum_to_total_launched_under_all_conditions():
    """Invariant: Pending + In Progress + Completed + Partial Complete == Total Launched."""
    job = _job(4)
    # Candidate 1: Completed from live API
    # Candidate 2: Call in progress from live API
    # Candidate 3: Unresolved from live API, has DB fallback 'initiated'
    # Candidate 4: Completely unresolved (live API None, DB None) -> defaults to 'pending'
    launched = [
        _launched("1", "c_1"),
        _launched("2", "c_2"),
        _launched("3", "c_3", engage_status="initiated"),
        _launched("4", "c_4"),
    ]
    live_outreach = {
        "1": {"outreach_status": "completed"},
        "2": {"outreach_status": "call_in_progress"},
    }
    row = lr._build_row(job, launched, [], live_outreach)
    assert row["total_candidates_launched"] == 4
    assert row["completed"] == 1
    assert row["in_progress"] == 1
    assert row["pending"] == 2  # c_3 (initiated) and c_4 (unresolved fallback)
    assert row["partial_complete"] == 0
    assert row["pending"] + row["in_progress"] + row["completed"] + row["partial_complete"] == 4
    assert row["percentage"] == 25.0  # 1 of 4 completed
    assert row["outreach_detail_resolved"] == 3  # 1, 2 (live) and 3 (DB) resolved


def test_stale_pending_audit_row_cannot_downgrade_a_stored_in_progress():
    """Pair Bot can write a later, older-looking `pending` audit event after
    the interview reached In Progress. The person's row keeps the furthest
    status because every layer is merged monotonically."""
    row = _launched(
        "inv_1", "c1",
        engage_status="In Progress",
        audit_status="pending", audit_response={"status": "pending"},
    )
    summary = lr.summarise_launched_candidates([row], {})
    assert summary["launched"] == 1
    assert summary["buckets"]["in_progress"] == 1
    assert summary["buckets"]["pending"] == 0


def test_audit_response_carrying_only_status_joins_the_merge():
    """Webhook write-backs store pair-bot's interview `status`, not
    `outreach_status`. It must still outrank a launch-time `sent` stamp —
    otherwise the table (and the header) would call this person Pending."""
    payload = lr.build_merged_outreach_payload(
        {"engage_status": "sent"},
        {"status": "in_progress", "outreach_channel": "sms"},
        "in_progress",
        None,
    )
    assert payload["outreach_status"] == "in_progress"
    assert payload["status"] == "in_progress"
    summary = lr.summarise_launched_candidates(
        [_launched("1", "c1", engage_status="sent", audit_status="in_progress",
                   audit_response={"status": "in_progress", "outreach_channel": "sms"})],
        {},
    )
    assert summary["buckets"]["in_progress"] == 1
    assert summary["channels"]["sms"] == 1


def test_launch_time_sent_stamp_is_pending_like_the_rank_list_table():
    """engage_status='sent' / audit 'Initiated' are written by the launch
    itself, before pair-bot has contacted anyone. The rank list's table shows
    them as Pending; the buckets must not call them In Progress."""
    summary = lr.summarise_launched_candidates(
        [_launched("1", "c1", engage_status="sent", audit_status="Initiated")], {}
    )
    assert summary["buckets"]["pending"] == 1
    assert summary["buckets"]["in_progress"] == 0
    # pair-bot's own `pending` cannot regress it either way
    summary = lr.summarise_launched_candidates(
        [_launched("1", "c1", engage_status="sent", audit_status="Initiated")],
        {"1": {"outreach_status": "pending", "outreach_phase": "phase2"}},
    )
    assert summary["buckets"]["pending"] == 1
    assert summary["phases"]["phase3"] == 1


def test_merge_outreach_payloads_monotonic_state_progression():
    """Higher progression status (e.g. completed) cannot be downgraded by lower status (e.g. initiated or pending)."""
    cand = {"outreach_status": "completed"}
    audit = {"outreach_status": "initiated", "status": "initiated"}
    live = {"outreach_status": "pending"}

    merged = lr.merge_outreach_payloads(cand, audit, live)
    assert merged["outreach_status"] == "completed"

    # Conversely, live completed status upgrades pending candidate
    cand2 = {"outreach_status": "pending"}
    live2 = {"outreach_status": "completed"}
    merged2 = lr.merge_outreach_payloads(cand2, {}, live2)
    assert merged2["outreach_status"] == "completed"


def test_eastern_date_expr_sql():
    """Explicit timezone conversion in SQL query matches project defaults."""
    expr = lr._eastern_date_expr("a.created_at")
    assert expr == "((a.created_at AT TIME ZONE %s) AT TIME ZONE %s)::date"


def test_sourced_counts_the_whole_rank_list_not_just_rows_before_first_launch():
    """Sourced is the rank list's candidate total. Capping it at the first
    launch timestamp made Launched exceed Sourced for a job that kept sourcing
    after launch — a row that cannot be read."""
    job = {**_job(0), "first_launch_at": datetime.datetime(2026, 8, 28, 2, 2)}
    rows = [
        {"candidate_id": "before", "created_at": datetime.datetime(2026, 8, 28, 2, 1)},
        {"candidate_id": "after", "created_at": datetime.datetime(2026, 8, 28, 2, 3)},
        {"candidate_id": "unknown", "created_at": None},
    ]
    row = lr._build_row(job, _launched_from(rows), rows, {})
    assert row["total_candidates_sourced"] == 3
    assert row["total_candidates_launched"] == 3
    assert not hasattr(lr, "_candidate_rows_as_of_first_launch")


def test_live_outreach_timestamps_are_exposed_for_the_report_row():
    """Live outreach timestamps from PairBot are exposed and parsed."""
    payload = {
        "outreach": {
            "outreach_status": "completed",
            "first_attempted_at": "2026-08-27T14:00:00Z",
            "first_completed_at": "2026-08-27T14:30:00Z",
        },
        "communications": [],
    }
    summary = lr._summarise_outreach([payload])
    assert summary["first_attempted_at"] == datetime.datetime(2026, 8, 27, 14, 0, tzinfo=datetime.timezone.utc)
    assert summary["first_completed_at"] == datetime.datetime(2026, 8, 27, 14, 30, tzinfo=datetime.timezone.utc)


def test_status_taxonomy_and_hierarchy_sync():
    """All status taxonomy sets must stay 100% synchronized with _STATUS_HIERARCHY."""
    all_known = lr._PENDING_STATUSES | lr._IN_PROGRESS_STATUSES | lr._COMPLETED_STATUSES | lr._PARTIAL_STATUSES
    assert set(lr._STATUS_HIERARCHY.keys()) == all_known
    for s in lr._COMPLETED_STATUSES:
        assert lr._STATUS_HIERARCHY[s] == 4
    for s in lr._PARTIAL_STATUSES:
        assert lr._STATUS_HIERARCHY[s] == 3
    for s in lr._IN_PROGRESS_STATUSES:
        assert lr._STATUS_HIERARCHY[s] == 2
    for s in lr._PENDING_STATUSES:
        assert lr._STATUS_HIERARCHY[s] == 1


def test_merge_outreach_payloads_unrecognised_status_not_swallowed():
    """An unrecognised status from a higher-priority layer must not be dropped in favor of a recognised one."""
    cand = {"outreach_status": "pending"}
    audit = {"outreach_status": "new_unmapped_audit_state"}
    # Layer 2 (audit) unmapped state overrides Layer 1 (cand) pending
    merged = lr.merge_outreach_payloads(cand, audit, None)
    assert merged["outreach_status"] == "new_unmapped_audit_state"

    # Layer 3 (live API) unmapped state overrides Layer 1 (cand) pending
    live = {"outreach_status": "brand_new_pairbot_state"}
    merged_live = lr.merge_outreach_payloads(cand, {}, live)
    assert merged_live["outreach_status"] == "brand_new_pairbot_state"

    # But recognised monotonic hierarchy still protects completed from being overwritten by pending
    cand_completed = {"outreach_status": "completed"}
    live_pending = {"outreach_status": "pending"}
    merged_hier = lr.merge_outreach_payloads(cand_completed, {}, live_pending)
    assert merged_hier["outreach_status"] == "completed"


def test_build_merged_outreach_payload_unrecognised_audit_status():
    """build_merged_outreach_payload retains unrecognised audit_status without dropping it."""
    cand_data = {"engage_status": "pending"}
    payload = lr.build_merged_outreach_payload(cand_data, None, "brand_new_state", None)
    assert payload["outreach_status"] == "brand_new_state"


def test_fetch_jobs_launched_on_sql_filters_true_first_launch():
    """_fetch_jobs_launched_on filters on l.first_launch_at in the outer query, not a.created_at in the CTE."""
    class FakeCursor:
        def __init__(self):
            self.description = [("job_id",), ("jobdiva_id",), ("first_launch_at",)]
            self.last_sql = ""
            self.last_params = []

        def execute(self, sql, params):
            self.last_sql = sql
            self.last_params = params

        def fetchall(self):
            return []

    class FakeConn:
        def __init__(self):
            self.cursor_obj = FakeCursor()

        def cursor(self):
            class Ctx:
                def __init__(self, c):
                    self.c = c
                def __enter__(self):
                    return self.c
                def __exit__(self, *args):
                    pass
            return Ctx(self.cursor_obj)

    fake_conn = FakeConn()
    start = datetime.date(2026, 9, 8)
    end = datetime.date(2026, 9, 9)
    lr._fetch_jobs_launched_on(fake_conn, start, None, end)

    sql = fake_conn.cursor_obj.last_sql
    # CTE must NOT filter a.created_at
    assert "AND {audit_date_filter}" not in sql
    assert "WHERE mj_cond" not in sql  # was replaced by actual mj_cond
    # CTE computes MIN(a.created_at) without date restrictions
    assert "MIN(a.created_at)                             AS first_launch_at" in sql
    # Unused total_launched removed from CTE and outer query
    assert "total_launched" not in sql
    # Outer query filters on l.first_launch_at
    assert "WHERE ((l.first_launch_at AT TIME ZONE %s) AT TIME ZONE %s)::date BETWEEN %s AND %s" in sql



