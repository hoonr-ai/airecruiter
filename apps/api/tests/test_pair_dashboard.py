"""services/pair_dashboard.py: definitions behind the PAIR Dashboard.

(a) shared rules pinned to the reports they must agree with,
(b) attribution of JobDiva activity (direct / cross-sub / non-PAIR),
(c) the Overview, Funnel & Speed and Productivity computations on small
    hand-built populations,
(d) the SQL loaders on a real Postgres (skip without one; TEMP tables only).
"""
import datetime
import json
import os

import pytest

from routers import recruiter_analytics
from services import pair_dashboard as dash

UTC = datetime.timezone.utc
ET = dash.REPORT_TIMEZONE


def utc(*args):
    return datetime.datetime(*args, tzinfo=UTC)


def et_noon(day: datetime.date) -> datetime.datetime:
    """A PAIR (aware) timestamp on this Eastern day."""
    return datetime.datetime.combine(day, datetime.time(12), tzinfo=ET)


def jd(day: datetime.date, hour: int = 10) -> datetime.datetime:
    """A JobDiva (naive Eastern) timestamp on this day."""
    return datetime.datetime.combine(day, datetime.time(hour))


D = datetime.date


# ---------------------------------------------------------------------------
# (a) shared rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("parent,ref,job_id", [
    ("", "26-15314", "32308716"),
    ("26-15314", "26-15314-v2", "26-15314-v2"),
    (None, "26-15314-V3", "x"),
    ("", "", "32308716"),
    ("  ", None, "abc-v2"),
])
def test_family_key_is_recruiter_analytics_rule(parent, ref, job_id):
    assert dash.family_key(parent, ref, job_id) == recruiter_analytics._family_key(parent, ref, job_id)


@pytest.mark.parametrize("launched,stopped,archived,status,expected", [
    (False, False, False, "OPEN", "Unpublished"),
    (True, False, False, "OPEN", "Active"),
    (True, False, False, None, "Active"),
    (True, False, False, "On Hold", "Inactive"),
    (True, True, False, "OPEN", "Inactive"),
    (True, False, True, "OPEN", "Inactive"),
])
def test_pair_status_mirrors_the_jobs_dashboard(launched, stopped, archived, status, expected):
    assert dash.derive_pair_status(launched, stopped, archived, status) == expected


@pytest.mark.parametrize("source,bucket", [
    ("JobDiva-Applicants", "applied"),
    ("JobDiva-TalentSearch", "jobdiva"),
    ("JobDiva-JobAgent", "jobdiva"),
    ("JobDiva", "jobdiva"),
    ("LinkedIn-Unipile", "linkedin"),
    ("LinkedIn-Exa", "linkedin"),
    ("Dice", "other"),
    ("upload-resume", "other"),
    (None, "other"),
])
def test_source_buckets(source, bucket):
    assert dash.source_bucket(source) == bucket


def test_client_interview_type():
    assert dash.is_client_interview("Client Interview L1- External")
    assert dash.is_client_interview("")
    assert not dash.is_client_interview("Pyramid SME Tech Screening- Internal")


def test_period_previous_and_weeks():
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    assert period.days == 7
    assert period.previous == (D(2026, 9, 11), D(2026, 9, 17))
    assert len(period.weeks) == 12 and period.weeks[-1] == D(2026, 9, 21)  # Monday of the end's week
    assert all(w.weekday() == 0 for w in period.weeks)
    assert period.window_start == period.weeks[0]
    everything = dash.make_period(None, None, today=D(2026, 9, 24))
    assert everything.start is None and everything.previous is None and everything.window_start is None
    assert everything.contains(D(2020, 1, 1))


def test_et_date_reads_pair_times_in_eastern_and_jobdiva_times_as_is():
    assert dash.et_date(utc(2026, 9, 22, 2, 0)) == D(2026, 9, 21)  # 10pm ET the day before
    assert dash.et_date(datetime.datetime(2026, 9, 22, 2, 0)) == D(2026, 9, 22)


# ---------------------------------------------------------------------------
# Fixture world
# ---------------------------------------------------------------------------
#   Req A (team, 3 openings) launched Sep 1; req B (another team) launched Aug 1;
#   req C (team) cancelled; req D is a non-PAIR JobDiva job.

def make_world():
    a = dash.Req(key="26-00001", ref="26-00001", link_key="26-00001", job_ids=["101"], candidate_keys={"101", "26-00001"},
                 jobdiva_job_ids={"101"}, title="Analyst", customer="Acme", status="OPEN", priority="P1",
                 division="SI-1", recruiter_emails={"sarah@x.com"}, added_at=et_noon(D(2026, 9, 18)),
                 launch_attempted_at=et_noon(D(2026, 9, 1)), first_launch_at=et_noon(D(2026, 9, 1)),
                 issue_at=jd(D(2026, 8, 30)), openings=3)
    b = dash.Req(key="26-00002", ref="26-00002", link_key="26-00002", job_ids=["102"], candidate_keys={"102"},
                 jobdiva_job_ids={"102"}, title="Engineer", customer="Beta", status="OPEN",
                 added_at=et_noon(D(2026, 8, 1)), launch_attempted_at=et_noon(D(2026, 8, 1)),
                 first_launch_at=et_noon(D(2026, 8, 1)), issue_at=jd(D(2026, 7, 30)), openings=1)
    c = dash.Req(key="26-00003", ref="26-00003", link_key="26-00003", job_ids=["103"], candidate_keys={"103"},
                 jobdiva_job_ids={"103"}, title="PM", customer="Acme", status="CANCELLED",
                 added_at=et_noon(D(2026, 9, 20)), openings=2)
    people = [
        # passed on A, submitted directly on A, interviewed, started
        dash.Person(req=a.key, candidate_id="p1", name="Ann", jobdiva_ids={"9001"}, source="applied",
                    launched_at=et_noon(D(2026, 9, 2)), outcome="Pass", passed_at=et_noon(D(2026, 9, 3)),
                    decision="external", decision_at=et_noon(D(2026, 9, 19))),
        # passed on A, no feedback yet -> pending
        dash.Person(req=a.key, candidate_id="p2", name="Ben", jobdiva_ids={"9002"}, source="linkedin",
                    launched_at=et_noon(D(2026, 9, 19)), outcome="Pass", passed_at=et_noon(D(2026, 9, 20))),
        # failed on A, rejected by the recruiter
        dash.Person(req=a.key, candidate_id="p3", name="Cal", jobdiva_ids={"9003"}, source="jobdiva",
                    launched_at=et_noon(D(2026, 9, 19)), outcome="Fail",
                    decision="reject", decision_at=et_noon(D(2026, 9, 21)), reject_reason="Communication skills"),
        # passed on B (other team) -> their submittal on A is B's cross-sub
        dash.Person(req=b.key, candidate_id="p4", name="Dee", jobdiva_ids={"9004"}, source="jobdiva",
                    launched_at=et_noon(D(2026, 8, 2)), outcome="Pass", passed_at=et_noon(D(2026, 8, 20))),
        # in progress on A
        dash.Person(req=a.key, candidate_id="p5", name="Eve", source="other", launched_at=et_noon(D(2026, 9, 12)),
                    outcome="In Progress"),
    ]
    activities = [
        # direct: p1 on A
        dash.Activity("a1", "101", "26-00001", "9001", "777", False, jd(D(2026, 9, 19)), jd(D(2026, 9, 22)), jd(D(2026, 9, 23))),
        # cross (credited to B): p4, passed Aug 20, submitted on A Sep 20
        dash.Activity("a2", "101", "26-00001", "9004", "777", False, jd(D(2026, 9, 20)), None, None),
        # non-PAIR on A: someone PAIR never launched
        dash.Activity("a3", "101", "26-00001", "5555", "778", False, jd(D(2026, 9, 21)), None, jd(D(2026, 9, 24))),
        # internal submittal: never a submission
        dash.Activity("a4", "101", "26-00001", "5556", "778", True, jd(D(2026, 9, 21)), None, None),
        # before A's first launch: not attributed to A at all
        dash.Activity("a5", "101", "26-00001", "5557", "778", False, jd(D(2026, 8, 25)), None, None),
        # p1's cross-sub to a non-PAIR job D, within 90 days of the pass
        dash.Activity("a6", "999", "26-00999", "9001", "777", False, jd(D(2026, 9, 12)), None, None),
    ]
    return [a, b, c], people, activities


def test_attribution_rules():
    reqs, people, activities = make_world()
    attributor = dash.Attributor(reqs, people)
    by_id = {a.activity_id: attributor.attribute(a) for a in activities}
    assert by_id["a1"].direct and by_id["a1"].req == "26-00001"
    assert not by_id["a2"].direct and by_id["a2"].cross_reqs == ("26-00002",)
    assert not by_id["a3"].is_pair
    assert attributor.is_non_pair_on(by_id["a3"], {"26-00001"}, activities[2].anchor_day)
    assert not attributor.is_non_pair_on(by_id["a5"], {"26-00001"}, activities[4].anchor_day)  # pre-launch
    assert by_id["a6"].req is None and by_id["a6"].cross_reqs == ("26-00001",)
    # credit follows the scope: a2 is B's, a6 is A's
    assert dash.credited(by_id["a2"], {"26-00001"}) is None
    assert dash.credited(by_id["a2"], {"26-00002"}) == "cross"
    assert dash.credited(by_id["a6"], {"26-00001"}) == "cross"


def test_cross_sub_needs_a_pass_within_90_days_before_the_submittal():
    reqs, people, _ = make_world()
    attributor = dash.Attributor(reqs, people)
    too_late = dash.Activity("x", "101", "", "9004", "", False, jd(D(2026, 11, 19)), None, None)  # 91 days
    before_pass = dash.Activity("y", "101", "", "9004", "", False, jd(D(2026, 8, 19)), None, None)
    in_window = dash.Activity("z", "101", "", "9004", "", False, jd(D(2026, 11, 18)), None, None)  # 90 days
    assert attributor.attribute(too_late).cross_reqs == ()
    assert attributor.attribute(before_pass).cross_reqs == ()
    assert attributor.attribute(in_window).cross_reqs == ("26-00002",)


def test_interview_and_hire_records_are_judged_on_the_candidates_submittal():
    reqs, people, _ = make_world()
    attributor = dash.Attributor(reqs, people)
    # p4 passed Aug 20 (on B), was submitted to A on Sep 20 and starts Dec 1: the
    # start is 103 days after the pass, but its submittal was inside the window.
    start = dash.Activity("h", "101", "26-00001", "9004", "777", False, None, None, jd(D(2026, 12, 1)),
                          first_submittal=jd(D(2026, 9, 20)))
    assert attributor.attribute(start).cross_reqs == ("26-00002",)
    orphan = dash.Activity("h2", "101", "26-00001", "9004", "777", False, None, None, jd(D(2026, 12, 1)))
    assert attributor.attribute(orphan).cross_reqs == ()  # without the submittal it is judged on its own date
    # a start whose submittal predates A's first launch is not A's, even if the start is later
    early = dash.Activity("h3", "101", "26-00001", "5557", "778", False, None, None, jd(D(2026, 9, 15)),
                          first_submittal=jd(D(2026, 8, 25)))
    assert not attributor.is_non_pair_on(attributor.attribute(early), {"26-00001"}, early.anchor_day)


def test_direct_matches_by_ref_when_the_job_id_differs():
    reqs, people, _ = make_world()
    attributor = dash.Attributor(reqs, people)
    by_ref = dash.Activity("r", "424242", "26-00001-v2", "9001", "", False, jd(D(2026, 9, 19)), None, None)
    assert attributor.attribute(by_ref).direct


# ---------------------------------------------------------------------------
# (c) Overview
# ---------------------------------------------------------------------------

def overview(team=("26-00001", "26-00003"), activities_available=True, pyramid=()):
    reqs, people, activities = make_world()
    scoped = [r for r in reqs if r.key in team]
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    return dash.compute_overview(
        period=period,
        reqs=scoped,
        people=[p for p in people if p.req in {r.key for r in scoped}],
        activities=activities,
        attributor=dash.Attributor(reqs, people),
        pyramid_reqs=list(pyramid),
        activities_available=activities_available,
    )


def test_overview_counts_are_activity_based():
    m = overview()
    # A added Sep 18 with 3 openings; C was cancelled so it is not a net opening.
    assert m["net_openings"]["value"] == 3 and m["net_openings"]["breakdown"]["reqs"] == 1
    # launched in the period: p2, p3 (p1 Sep 2 and p5 Sep 12 are earlier)
    assert m["candidates_launched"]["value"] == 2
    assert m["candidates_launched"]["previous"] == 1  # p5 on Sep 12
    assert m["candidates_passed"]["value"] == 1  # p2
    # submissions: a1 direct; a6 (Sep 12) is before the period; a2 belongs to B
    subs = m["pair_submissions"]
    assert subs["value"] == 1 and subs["breakdown"]["direct"] == 1 and subs["breakdown"]["cross"] == 0
    assert subs["breakdown"]["previous_cross"] == 1  # a6 on Sep 12
    assert subs["breakdown"]["recorded_in_pair"] == 1  # p1's PAIR Submit on Sep 19
    assert m["pair_interviews"]["value"] == 1 and m["pair_starts"]["value"] == 1
    # non-PAIR on A: a3 (a2 is PAIR — B's cross-sub; a4 internal; a5 pre-launch)
    assert m["non_pair_submissions"]["value"] == 1
    assert m["non_pair_starts"]["value"] == 1
    assert m["pair_share"]["value"] == 0.5  # 1 / (1 + 1)
    assert m["fill_ratio"]["value"] == round(1 / 3, 4)
    assert m["submit_to_start"]["value"] == 1.0
    assert m["candidates_launched"]["weekly"][-1] == 0  # the week of Sep 21 had no launches
    assert m["candidates_launched"]["weekly"][-2] == 2


def test_overview_other_team_sees_its_cross_sub():
    m = overview(team=("26-00002",))
    assert m["pair_submissions"]["value"] == 1
    assert m["pair_submissions"]["breakdown"]["cross"] == 1
    assert m["non_pair_submissions"]["value"] == 0  # B has no activity of its own


def test_overview_marks_jobdiva_metrics_unavailable_instead_of_zero():
    m = overview(activities_available=False)
    assert m["pair_submissions"]["value"] is None and "unavailable" in m["pair_submissions"]
    assert m["pair_submissions"]["breakdown"]["recorded_in_pair"] == 1
    assert m["candidates_launched"]["value"] == 2  # PAIR's own numbers still show


def test_jobdiva_numbers_before_the_mirror_covers_them_are_unknown_not_zero():
    reqs, people, activities = make_world()
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    m = dash.compute_overview(
        period=period, reqs=reqs[:1], people=people, activities=activities,
        attributor=dash.Attributor(reqs, people), pyramid_reqs=[], activities_available=True,
        activities_from=D(2026, 9, 14), jobs_from=D(2026, 9, 1),
    )
    subs = m["pair_submissions"]
    assert subs["value"] == 1 and subs["previous"] is None  # Sep 11-17 starts before Sep 14
    assert subs["breakdown"]["previous_cross"] is None
    assert subs["weekly"][:-2] == [None] * 10 and subs["weekly"][-2:] == [1, 0]  # a1 (Sep 19); weeks from Sep 14 are covered
    partly = dash.trim_to_coverage({"weekly": [5] * 12}, period, D(2026, 9, 16))
    assert partly["weekly"][-2:] == [None, 5]  # the week of Sep 14 is only partly covered
    assert subs["breakdown"]["weekly_direct"][-3] is None
    assert "partial_from" not in subs  # the period itself is covered
    assert "partial_from" not in m["pair_volume"]  # requirements are covered from Sep 1
    assert m["candidates_launched"]["previous"] == 1  # PAIR's own numbers are never trimmed
    everything = dash.compute_overview(
        period=dash.make_period(None, None, today=D(2026, 9, 24)), reqs=reqs[:1], people=people,
        activities=activities, attributor=dash.Attributor(reqs, people), pyramid_reqs=[],
        activities_available=True, activities_from=D(2026, 9, 14),
    )
    assert everything["pair_submissions"]["partial_from"] == "2026-09-14"


def test_pair_volume_denominator_is_all_reqs_plus_pair_reqs():
    pyramid = [
        dash.PyramidReq("101", "26-00001", jd(D(2026, 9, 20)), "OPEN", 3),  # PAIR req A, launched
        dash.PyramidReq("500", "26-00500", jd(D(2026, 9, 21)), "OPEN", 5),  # not in PAIR
        dash.PyramidReq("501", "26-00501", jd(D(2026, 9, 21)), "ON HOLD", 4),  # excluded
        dash.PyramidReq("502", "26-00502", jd(D(2026, 9, 1)), "OPEN", 9),  # outside the period
    ]
    m = overview(pyramid=pyramid)
    volume = m["pair_volume"]
    assert volume["numerator"] == 3 and volume["denominator"] == 8
    assert volume["value"] == round(3 / 8, 4)


def test_team_pair_volume_counts_another_teams_launched_req_as_pair():
    reqs, _, _ = make_world()
    a, b = reqs[0], reqs[1]
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    # The team (scope = A) is tagged on A and on B; B is another team's launched PAIR req.
    pyramid = [
        dash.PyramidReq("101", "26-00001", jd(D(2026, 9, 20)), "OPEN", 1),
        dash.PyramidReq("102", "26-00002", jd(D(2026, 9, 20)), "OPEN", 1),
    ]
    scoped_only = dash.pair_volume_metric(period, [a], pyramid)
    assert scoped_only["value"] == 0.5  # without every PAIR req, B looks non-PAIR
    everyone = dash.pair_volume_metric(period, [a], pyramid, all_reqs=[a, b])
    assert everyone["numerator"] == 2 and everyone["value"] == 1.0


def test_pair_volume_can_wait_for_the_team_tagging_sync():
    reqs, people, activities = make_world()
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    m = dash.compute_overview(period=period, reqs=reqs[:1], people=people, activities=activities,
                              attributor=dash.Attributor(reqs, people), pyramid_reqs=[],
                              activities_available=True, volume_unavailable="Waiting for tags.")
    assert m["pair_volume"]["value"] is None and m["pair_volume"]["unavailable"] == "Waiting for tags."


def test_a_period_wholly_before_the_coverage_has_no_value():
    period = dash.make_period(D(2026, 8, 1), D(2026, 8, 7), today=D(2026, 9, 24))
    metric = dash.trim_to_coverage({"kind": "count", "value": 0, "previous": 0, "weekly": [0] * 12},
                                   period, D(2026, 9, 1))
    assert metric["value"] is None and metric["previous"] is None and metric["partial_from"] == "2026-09-01"


def test_pair_volume_counts_a_pair_req_the_mirror_has_not_seen():
    reqs, _, _ = make_world()
    a = reqs[0]
    a.issue_at = jd(D(2026, 9, 20))
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    metric = dash.pair_volume_metric(period, [a], [])
    assert metric["numerator"] == 3 and metric["denominator"] == 3


def test_pair_volume_is_unavailable_before_the_first_sync():
    reqs, people, activities = make_world()
    period = dash.make_period(D(2026, 9, 18), D(2026, 9, 24), today=D(2026, 9, 24))
    m = dash.compute_overview(period=period, reqs=reqs[:1], people=people, activities=activities,
                              attributor=dash.Attributor(reqs, people), pyramid_reqs=None,
                              activities_available=True)
    assert m["pair_volume"]["value"] is None and "unavailable" in m["pair_volume"]


def test_all_time_has_no_previous_period():
    reqs, people, activities = make_world()
    period = dash.make_period(None, None, today=D(2026, 9, 24))
    m = dash.compute_overview(period=period, reqs=reqs[:1], people=people[:3] + people[4:], activities=activities,
                              attributor=dash.Attributor(reqs, people), pyramid_reqs=[],
                              activities_available=True)
    assert m["candidates_launched"]["value"] == 4 and m["candidates_launched"]["previous"] is None
    assert m["pair_submissions"]["breakdown"]["cross"] == 1  # a6


# ---------------------------------------------------------------------------
# (c) Funnel & Speed
# ---------------------------------------------------------------------------

def funnel(now=utc(2026, 9, 25, 16)):
    reqs, people, activities = make_world()
    a = reqs[0]
    return dash.compute_funnel(
        cohort=[a],
        people=[p for p in people if p.req == a.key],
        activities=activities,
        attributor=dash.Attributor(reqs, people),
        activities_available=True,
        now=now,
    )


def test_funnel_stages_count_people():
    stages = {s["key"]: s for s in funnel()["stages"]}
    assert stages["initiated"]["count"] == 4
    assert {s["key"]: s["count"] for s in stages["initiated"]["segments"]} == {
        "applied": 1, "jobdiva": 1, "linkedin": 1, "other": 1,
    }
    assert stages["completed"]["count"] == 3 and stages["passed"]["count"] == 2
    # p1: direct submission on A AND a cross-sub on D -> one person, counted direct
    sub = stages["pair_submission"]
    assert sub["count"] == 1 and {s["key"]: s["count"] for s in sub["segments"]} == {"direct": 1, "cross": 0}
    assert stages["pair_interview"]["count"] == 1 and stages["pair_start"]["count"] == 1
    assert stages["non_pair_submission"]["count"] == 1  # a3 (a2 is B's cross-sub, a5 pre-launch)
    assert stages["non_pair_start"]["count"] == 1
    assert stages["non_pair_interview"]["count"] == 0


def test_funnel_speed_medians_in_hours():
    speed = {s["key"]: s for s in funnel()["speed"]}
    # posted Aug 30 10:00 ET -> first launch Sep 1 12:00 ET = 50h
    assert speed["time_to_launch"]["median_hours"] == 50.0 and speed["time_to_launch"]["jobs"] == 1
    # first launch Sep 1 12:00 -> first pass Sep 3 12:00 = 48h
    assert speed["time_to_first_pass"]["median_hours"] == 48.0
    # first submit: JobDiva direct Sep 19 10:00 ET (before the PAIR Submit at noon)
    assert speed["time_to_first_submit"]["median_hours"] == 18 * 24 - 2
    assert speed["time_to_first_submit_posted"]["median_hours"] == 20 * 24


def test_funnel_rejections_and_pending_feedback():
    result = funnel()
    assert result["rejection_reasons"] == {"total": 1, "reasons": [{"reason": "Communication skills", "count": 1}]}
    pending = result["pending_feedback"]
    assert pending["pending"] == 1 and pending["passed"] == 2 and pending["given"] == 1
    row = pending["candidates"][0]
    assert row["name"] == "Ben" and row["job_ref"] == "26-00001"
    assert row["recruiters"] == ["sarah@x.com"]
    assert row["days_pending"] == 5  # passed Sep 20 noon ET, now Sep 25 noon ET


def test_hours_between_tolerates_a_day_of_skew_only():
    assert dash._hours_between(utc(2026, 9, 2), utc(2026, 9, 1, 12)) == 0.0
    assert dash._hours_between(utc(2026, 9, 3), utc(2026, 9, 1)) is None
    assert dash._hours_between(None, utc(2026, 9, 1)) is None


# ---------------------------------------------------------------------------
# (c) Productivity
# ---------------------------------------------------------------------------

def test_productivity_weekly_averages_per_recruiter():
    reqs, people, activities = make_world()
    period = dash.make_period(D(2026, 9, 14), D(2026, 9, 27), today=D(2026, 9, 27))  # two whole weeks
    assigned = [
        dash.PyramidReq("101", "26-00001", jd(D(2026, 8, 30)), "OPEN", 3),
        dash.PyramidReq("600", "26-00600", jd(D(2026, 9, 1)), "CLOSED", 1, status_changed_at=jd(D(2026, 9, 16))),
        dash.PyramidReq("601", "26-00601", jd(D(2026, 9, 22)), "OPEN", 1),
    ]
    result = dash.compute_productivity(
        period=period, recruiter_count=2, assigned_reqs=assigned, activities=activities,
        attributor=dash.Attributor(reqs, people), coverage_start=D(2026, 9, 1),
    )
    m = result["metrics"]
    assert m["client_subs"]["previous"] is None  # Aug 31 - Sep 13 starts before the coverage
    assert result["covered_from"] == "2026-09-01"
    # week of Sep 14: 101 + 600 = 2; week of Sep 21: 101 + 601 = 2 -> 2 reqs / 2 recruiters
    assert m["reqs_assigned"]["value"] == 1.0
    assert m["reqs_assigned"]["weekly"][-2:] == [1.0, 1.0]
    # external submittals in range: a1, a2, a3 (a4 internal, a5 Aug 25 and a6 Sep 12 before the range)
    # over 2 weeks x 2 recruiters
    assert m["client_subs"]["value"] == 0.75
    assert m["client_subs"]["breakdown"]["pair_total"] == 2  # a1 direct, a2 cross
    assert m["client_subs"]["breakdown"]["non_pair_total"] == 1
    assert m["starts"]["breakdown"]["pair_total"] == 1 and m["starts"]["breakdown"]["non_pair_total"] == 1
    assert m["starts"]["value"] == 0.5


def test_productivity_reqs_assigned_waits_for_the_tagging_sync():
    reqs, people, activities = make_world()
    period = dash.make_period(D(2026, 9, 14), D(2026, 9, 27), today=D(2026, 9, 27))
    result = dash.compute_productivity(period=period, recruiter_count=2, assigned_reqs=[], activities=activities,
                                       attributor=dash.Attributor(reqs, people), reqs_unavailable="Waiting.")
    assert result["metrics"]["reqs_assigned"]["unavailable"] == "Waiting."
    assert result["metrics"]["client_subs"]["value"] == 0.75  # submittals do not need the tags


def test_productivity_without_recruiters_is_empty_not_a_division_error():
    period = dash.make_period(D(2026, 9, 14), D(2026, 9, 27), today=D(2026, 9, 27))
    result = dash.compute_productivity(period=period, recruiter_count=0, assigned_reqs=[], activities=[],
                                       attributor=dash.Attributor([], []))
    assert result["metrics"]["reqs_assigned"]["value"] is None
    assert result["metrics"]["client_subs"]["value"] is None


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_filter_reqs():
    reqs, _, _ = make_world()
    F = dash.DashboardFilters
    assert [r.key for r in dash.filter_reqs(reqs, F(), {"101", "103"})] == ["26-00001", "26-00003"]
    assert [r.key for r in dash.filter_reqs(reqs, F(client="acme"))] == ["26-00001", "26-00003"]
    assert [r.key for r in dash.filter_reqs(reqs, F(jd_status="Cancelled"))] == ["26-00003"]
    assert [r.key for r in dash.filter_reqs(reqs, F(pair_status="Unpublished"))] == ["26-00003"]
    assert [r.key for r in dash.filter_reqs(reqs, F(job="102"))] == ["26-00002"]
    assert [r.key for r in dash.filter_reqs(reqs, F(job="26-00002"))] == ["26-00002"]
    assert [r.key for r in dash.filter_reqs(reqs, F(vertical="si-1", priority="p1"))] == ["26-00001"]


def test_filter_options_are_distinct_and_sorted():
    reqs, _, _ = make_world()
    options = dash.filter_options(reqs, divisions=["Pharma", "si-1"])
    assert options["clients"] == ["Acme", "Beta"]
    assert options["jd_statuses"] == ["Cancelled", "Open"]
    assert options["verticals"] == ["Pharma", "si-1"]  # case-insensitive dedupe keeps the first spelling
    assert options["pair_statuses"] == ["Active", "Inactive", "Unpublished"]


# ---------------------------------------------------------------------------
# build_people / build_reqs (row shapes the SQL returns)
# ---------------------------------------------------------------------------

def test_build_people_collapses_rows_across_keys():
    first = {("26-00001", "c1"): utc(2026, 9, 1, 12)}
    key_to_req = {"101": "26-00001", "26-00001": "26-00001"}
    rows = [
        # (key, id, stored_key, candidate_id, source, name, display, first_completed, completed, updated,
        #  jd_candidate_id, decision, feedback_at, feedback_reason, interview_id)
        ("101", 1, "101", "c1", "LinkedIn-Unipile", "Old Name", "In Progress", None, None, None, None, None, None, None, "i1"),
        ("26-00001", 2, "26-00001", "c1", "LinkedIn-Unipile", "New Name", "Pass", "2026-09-02T10:00:00+00:00",
         "2026-09-03T10:00:00+00:00", None, "19122444778939", "reject", "2026-09-04T10:00:00+00:00", "Too far", "i1"),
        ("101", 1, "101", "c1", "LinkedIn-Unipile", "Old Name", "In Progress", None, None, None, None,
         "external", "2026-09-05T10:00:00+00:00", None, "i1"),
    ]
    [person] = dash.build_people(first, rows, key_to_req, links={"101": "26-00001", "26-00001": "26-00001"})
    assert person.outcome == "Pass"
    assert person.passed_at == utc(2026, 9, 2, 10)
    assert person.decision == "external"  # newest decision wins
    assert person.jobdiva_ids == {"c1", "19122444778939"}
    assert person.name == "New Name" and person.source == "linkedin"
    assert person.link_key == "26-00001"


def test_build_reqs_folds_versions_and_prefers_the_mirror():
    rows = [
        # job_id, jobdiva_id, parent, title, customer, status, priority, openings, posted, created_at,
        # launched_at, stopped, archived, recruiter_emails, launched_by,
        # jd_job_id, jd_status, jd_priority, jd_division, jd_openings, jd_issue, jd_company
        ("32308716", "26-15314", None, "Analyst", "Acme", "OPEN", "P3", "1", "Aug 30, 2026",
         utc(2026, 9, 1), utc(2026, 9, 2), False, False, '["a@x.com"]', "",
         "32308716", "ON HOLD", "P1", "SI-2", 4, datetime.datetime(2026, 8, 29, 9), "Acme Corp"),
        ("26-15314-v2", "26-15314-v2", "26-15314", "Analyst II", "Acme", "OPEN", "P3", "", "",
         utc(2026, 9, 10), None, True, False, '["b@x.com"]', "b@x.com",
         None, None, None, None, None, None, None),
    ]
    [req] = dash.build_reqs(rows)
    assert req.key == "26-15314" and sorted(req.job_ids) == ["26-15314-v2", "32308716"]
    assert req.candidate_keys == {"32308716", "26-15314", "26-15314-v2"}
    assert req.jobdiva_job_ids == {"32308716"}
    assert req.added_at == utc(2026, 9, 1) and req.launch_attempted_at == utc(2026, 9, 2)
    assert req.issue_at == datetime.datetime(2026, 8, 29, 9)  # mirror beats posted_date
    # the newest version describes the req
    assert req.title == "Analyst II" and req.stopped and req.recruiter_emails == {"b@x.com"}
    assert req.launched_by == "b@x.com"
    assert req.links == {"32308716": "26-15314", "26-15314": "26-15314", "26-15314-v2": "26-15314-v2"}


# ---------------------------------------------------------------------------
# (d) real Postgres
# ---------------------------------------------------------------------------
psycopg2 = pytest.importorskip("psycopg2")
_TEST_DSN = os.getenv("LAUNCH_REPORT_TEST_DSN", "dbname=postgres")

_SCHEMA = """
CREATE TEMP TABLE monitored_jobs (
    job_id TEXT, jobdiva_id TEXT, parent_job_id TEXT, title TEXT, enhanced_title TEXT, customer_name TEXT,
    status TEXT, priority TEXT, openings TEXT, posted_date TEXT, created_at TEXT, pair_launched_at TIMESTAMP,
    outreach_stopped_at TIMESTAMP, is_archived BOOLEAN, recruiter_emails TEXT, pair_launched_by TEXT
);
CREATE TEMP TABLE engage_interview_audit (
    id SERIAL PRIMARY KEY, candidate_id VARCHAR(255) NOT NULL, jobdiva_id VARCHAR(255),
    interview_id VARCHAR(255), status VARCHAR(50), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TEMP TABLE sourced_candidates (
    id SERIAL PRIMARY KEY, jobdiva_id TEXT NOT NULL, candidate_id TEXT NOT NULL, source TEXT NOT NULL,
    name TEXT, email TEXT, phone TEXT, data JSONB, status TEXT DEFAULT 'sourced',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(jobdiva_id, candidate_id, source)
);
"""


@pytest.fixture()
def pg():
    try:
        conn = psycopg2.connect(_TEST_DSN, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no Postgres reachable at {_TEST_DSN!r}: {exc}")
    from services import jobdiva_bi_sync as sync

    try:
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_SCHEMA)
            for statement in sync.SCHEMA_STATEMENTS:
                cur.execute(statement.replace("CREATE TABLE IF NOT EXISTS", "CREATE TEMP TABLE", 1))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _exec(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def _seed(conn):
    _exec(conn, """INSERT INTO monitored_jobs VALUES
        ('101', '26-00001', NULL, 'Analyst', NULL, 'Acme', 'OPEN', 'P1', '3', 'Aug 30, 2026',
         '2026-09-18 12:00:00', '2026-09-01 16:00:00', NULL, FALSE, '["sarah@x.com"]', 'sarah@x.com'),
        ('26-00001-v2', '26-00001-v2', '26-00001', 'Analyst v2', NULL, 'Acme', 'OPEN', 'P1', '3', '',
         '2026-09-20 12:00:00 IST', NULL, NULL, FALSE, '["sarah@x.com"]', NULL)""")
    _exec(conn, """INSERT INTO jobdiva_jobs (job_id, jobdiva_ref, job_status, division_name, openings, issue_date, company_name)
                   VALUES ('101', '26-00001', 'OPEN', 'SI-1', 3, '2026-08-30 10:00', 'Acme Corp'),
                          ('500', '26-00500', 'OPEN', 'SI-1', 5, '2026-09-21 10:00', 'Other Co')""")
    _exec(conn, "INSERT INTO jobdiva_users (user_id, email, is_active) VALUES ('777', 'sarah@x.com', TRUE)")
    _exec(conn, "INSERT INTO jobdiva_job_users (job_id, user_id) VALUES ('500', '777')")
    # p1: launched under the ref, stored under the numeric key, passed, submitted
    _exec(conn, "INSERT INTO engage_interview_audit (candidate_id, jobdiva_id, interview_id, created_at) VALUES "
                "('p1', '26-00001', 'i1', '2026-09-02 16:00'), ('p1', '26-00001', 'i1b', '2026-09-05 16:00'), "
                "('p2', '26-00001-v2', 'i2', '2026-09-19 16:00'), ('px', '26-00001', '', '2026-09-19 16:00')")
    _exec(conn, "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, name, data) VALUES (%s, %s, %s, %s, %s)",
          ("101", "p1", "JobDiva-Applicants", "Ann", json.dumps({
              "engage_status": "completed", "engage_hard_filter_status": "pass",
              "first_completed_at": "2026-09-03T16:00:00+00:00", "jobdiva_candidate_id": "9001",
              "feedback_type": "Submit", "submission_type": "external", "feedback_at": "2026-09-19T16:00:00+00:00",
          })))
    _exec(conn, "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, name, data) VALUES (%s, %s, %s, %s, %s)",
          ("26-00001-v2", "p2", "LinkedIn-Exa", "Ben", json.dumps({"engage_status": "failed", "engage_score": "12"})))
    _exec(conn, """INSERT INTO jobdiva_activities (activity_id, job_id, jobdiva_ref, candidate_id, user_id,
                       is_internal, submittal_date, interview_flag, interview_date, interview_type, hire_flag, start_date)
                   VALUES ('a1', '101', '26-00001', '9001', '777', FALSE, '2026-09-19 10:00', TRUE,
                           '2026-09-22 10:00', 'Client Interview L1- External', FALSE, NULL),
                          ('h1', '101', '26-00001', '9001', '777', FALSE, NULL, FALSE, NULL, NULL, TRUE, '2026-10-12 09:00'),
                          ('a2', '101', '26-00001', '5555', '778', FALSE, '2026-09-21 10:00', TRUE,
                           '2026-09-23 10:00', 'Pyramid SME Tech Screening- Internal', TRUE, '2026-10-05 09:00'),
                          ('a3', '999', '26-00999', '9001', '777', FALSE, '2025-01-01 10:00', FALSE, NULL, NULL, FALSE, NULL)""")


def test_pg_loaders_build_the_same_world_the_pure_tests_use(pg):
    _seed(pg)
    [req] = dash.load_reqs(pg)
    assert req.key == "26-00001" and req.candidate_keys == {"101", "26-00001", "26-00001-v2"}
    assert req.added_at == utc(2026, 9, 18, 12)  # first added with v1; the v2 (IST text) came later
    assert req.division == "SI-1" and req.customer == "Acme Corp" and req.openings == 3
    assert req.issue_at == datetime.datetime(2026, 8, 30, 10)
    assert req.launch_attempted_at == utc(2026, 9, 1, 16)

    people = {p.candidate_id: p for p in dash.load_people(pg, [req])}
    assert set(people) == {"p1", "p2"}  # px's launch failed (no interview id)
    p1 = people["p1"]
    assert p1.launched_at == utc(2026, 9, 2, 16)  # FIRST launch, not the re-launch
    assert p1.outcome == "Pass" and p1.passed_at == utc(2026, 9, 3, 16)
    assert p1.decision == "external" and p1.jobdiva_ids == {"p1", "9001"}
    assert p1.source == "applied" and p1.link_key == "26-00001"
    assert people["p2"].outcome == "Fail" and people["p2"].link_key == "26-00001-v2"

    activities = {a.activity_id: a for a in dash.load_activities(
        pg, lo=D(2026, 9, 1), hi=D(2026, 9, 30), job_ids=["101"], refs=["26-00001"], candidate_ids=["9001"],
    )}
    assert set(activities) == {"a1", "a2"}  # a3 is outside the window, h1 starts in October
    assert activities["a1"].first_submittal == datetime.datetime(2026, 9, 19, 10)
    october = {a.activity_id: a for a in dash.load_activities(pg, lo=D(2026, 10, 1), hi=D(2026, 10, 31), job_ids=["101"])}
    hire = october["h1"]
    assert hire.submittal_date is None
    assert hire.first_submittal == datetime.datetime(2026, 9, 19, 10)  # the separate submittal record
    assert activities["a1"].interview_date == datetime.datetime(2026, 9, 22, 10)
    assert activities["a2"].interview_date is None  # internal screen, not a client interview
    # The record carries its whole state; the tallies decide which dates fall in a period.
    assert activities["a2"].start_date == datetime.datetime(2026, 10, 5, 9)
    assert [a.activity_id for a in dash.load_activities(pg, lo=D(2026, 10, 1), hi=D(2026, 10, 31), user_ids=["778"])] == ["a2"]

    mapping = dash.team_jobdiva_users(pg, ["Sarah@x.com", "nobody@x.com"])
    assert mapping == {"sarah@x.com": "777"}
    pyramid = dash.load_pyramid_reqs(pg, lo=D(2026, 9, 18), hi=D(2026, 9, 24), filters=dash.DashboardFilters(),
                                     team_user_ids=["777"], team_job_ids=[])
    assert [j.job_id for j in pyramid] == ["500"]
    everyone = dash.load_pyramid_reqs(pg, lo=D(2026, 8, 1), hi=D(2026, 9, 24),
                                      filters=dash.DashboardFilters(vertical="si-1"), team_user_ids=None)
    assert sorted(j.job_id for j in everyone) == ["101", "500"]
    active = dash.load_pyramid_reqs(pg, lo=D(2026, 9, 22), hi=D(2026, 9, 28), filters=dash.DashboardFilters(),
                                    team_user_ids=None, active_during=True)
    assert sorted(j.job_id for j in active) == ["101", "500"]

    # Productivity's vertical / client filters apply to the activity too.
    si1 = dash.load_activities(pg, lo=D(2026, 9, 1), hi=D(2026, 9, 30), user_ids=["777", "778"],
                               job_filters=dash.DashboardFilters(vertical="si-1"))
    assert sorted(a.activity_id for a in si1) == ["a1", "a2"]
    other = dash.load_activities(pg, lo=D(2026, 9, 1), hi=D(2026, 9, 30), user_ids=["777", "778"],
                                 job_filters=dash.DashboardFilters(client="Other Co"))
    assert other == []


def test_pg_coverage_is_per_feed_and_waits_for_job_user_tags(pg):
    _exec(pg, "INSERT INTO jobdiva_bi_sync_state (feed, cursor_at, backfill_until, last_success_at) "
              "VALUES ('jobs', '2026-09-24 09:00', '2026-09-20 09:00', NOW())")
    _exec(pg, "INSERT INTO jobdiva_jobs (job_id, job_status, users_synced_at) VALUES "
              "('1', 'OPEN', NOW()), ('2', 'OPEN', NULL), ('3', 'CLOSED', NULL)")
    coverage = dash.jobdiva_coverage(pg)
    # The jobs feed ran; activity never did: its numbers must not read as zeros.
    assert coverage["jobs_available"] and not coverage["activities_available"]
    assert not coverage["users_available"]
    assert coverage["job_users_ready"] is False  # 1 of 2 open reqs tagged (closed ones don't count)
    _exec(pg, "UPDATE jobdiva_jobs SET users_synced_at = NOW() WHERE job_id = '2'")
    _exec(pg, "INSERT INTO jobdiva_bi_sync_state (feed, cursor_at, backfill_until, last_success_at) "
              "VALUES ('activities', '2026-09-24 09:00', '2026-09-24 09:00', NOW()), ('users', '2026-09-24 09:00', NULL, NOW())")
    coverage = dash.jobdiva_coverage(pg)
    assert coverage["activities_available"] and coverage["users_available"] and coverage["job_users_ready"]


def test_slim_copy_carries_every_key_the_shared_rules_read():
    import re

    from services import feedback_metrics
    from services.engage_status import engage_display_sql

    sql = engage_display_sql("x.data") + " ".join(
        getattr(feedback_metrics, name).format(alias="x.")
        for name in ("HAS_DECISION_SQL", "IS_SUBMIT_SQL", "IS_INTERNAL_SUBMIT_SQL", "IS_EXTERNAL_SUBMIT_SQL",
                     "IS_REJECT_SQL", "IS_UNREACHABLE_SQL")
    )
    read = set(re.findall(r"->>'([a-z_]+)'", sql))
    assert read and read <= set(dash.SLIM_KEYS), read - set(dash.SLIM_KEYS)
    assert set(re.findall(r"->>'([a-z_]+)'", dash._PEOPLE_ROWS_SQL)) <= set(dash.SLIM_KEYS)


def test_pg_display_sql_runs_against_every_stored_status(pg):
    _seed(pg)
    # Non-object and NULL blobs must not abort the statement.
    _exec(pg, "INSERT INTO sourced_candidates (jobdiva_id, candidate_id, source, data) VALUES "
              "('26-00001', 'p8', 'LinkedIn', 'null'::jsonb), ('26-00001', 'p9', 'LinkedIn', NULL)")
    with pg.cursor() as cur:
        cur.execute(f"SELECT candidate_id, {dash._DISPLAY_SQL}, {dash._decision_sql()} "
                    f"FROM sourced_candidates sc {dash._SLIM_LATERAL} ORDER BY 1")
        assert cur.fetchall() == [("p1", "Pass", "external"), ("p2", "Fail", None),
                                  ("p8", "Pending", None), ("p9", "Pending", None)]
