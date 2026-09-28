"""PAIR Dashboard: the Overview, Funnel & Speed and Productivity tabs.

GET /api/v1/admin/dashboard/* (routers/pair_dashboard.py) is the only caller.
The page's Data Dictionary states the same definitions in words; keep the two
in step.

Populations
-----------
* A **PAIR requirement** ("req") is a JobDiva job added to PAIR: every
  monitored_jobs row of one version family (v1 plus its "-vN" clones, the same
  family rule as Recruiter Analytics) is ONE req.
* A **PAIR candidate** is a person PAIR launched on a req, keyed per req by
  `candidate_id` (the launched-candidate rule in services/launched_candidates:
  an interview id exists). Launch time is their first successful launch
  (engage_interview_audit), Pass / Fail follow the rank list
  (`engage_display_sql`) and the recruiter decision is the person's newest one
  (the feedback predicates in services/feedback_metrics). One small, known
  difference from the Launch Report: a launch whose audit row was lost (only
  `data.engage_interview_id` survives) has no launch time and is left out.
* **JobDiva activity** is the mirror in services/jobdiva_bi_sync.py: one
  record per submittal of a candidate to a job, carrying that submittal's
  interview and hire state. A record is matched to a PAIR candidate by the
  rank list's own rule, JobDiva CANDIDATEID in {candidate_id,
  data.jobdiva_candidate_id}.

Attribution of a JobDiva activity
---------------------------------
* **Direct**: on a PAIR req, by a candidate who passed PAIR on that same req,
  dated on or after the req's first PAIR launch.
* **Cross-sub**: by a candidate who passed PAIR on a DIFFERENT req within the
  90 days before the submittal. Credited to the req they passed on, so a
  team's cross-subs are the ones its own passes produced, wherever they landed.
* **Non-PAIR**: on a PAIR req (after its first launch) and neither of the above.

Dates
-----
Everything is activity-based: a metric counts the events that happened in the
period (a submittal in the period counts even if the req launched months ago).
Periods are US Eastern calendar days; "vs prev" compares with the same number
of days immediately before; trends are Monday-anchored weeks. PAIR timestamps
are converted from UTC; JobDiva's are Eastern wall clock already.
"""

import datetime
import logging
import os
import re
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from services.engage_status import engage_display_sql
from services.feedback_metrics import (
    HAS_DECISION_SQL,
    IS_EXTERNAL_SUBMIT_SQL,
    IS_INTERNAL_SUBMIT_SQL,
    IS_REJECT_SQL,
    IS_UNREACHABLE_SQL,
)

logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc
REPORT_TIMEZONE = ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/New_York"))
# engage_interview_audit / sourced_candidates timestamps are naive readings of
# the DB session zone (see routers/launch_report.REPORT_DB_TIMEZONE).
_DB_TIMEZONE = ZoneInfo(os.getenv("REPORT_DB_TIMEZONE", "UTC"))

MAX_RANGE_DAYS = 366
SERIES_WEEKS = 12
CROSS_SUBMISSION_WINDOW = datetime.timedelta(days=90)

# JobDiva statuses that take a req out of Net Openings. Upper-cased and
# whitespace-collapsed before comparing (the feeds say "On Hold" and "ON HOLD").
EXCLUDED_REQ_STATUSES = frozenset({"CANCELLED", "CANCELED", "ON HOLD", "DECLINED", "IGNORED"})
# A req counts as actively worked while its status is one of these.
ACTIVE_REQ_STATUSES = frozenset({"OPEN", ""})

PAIR_STATUSES = ("Active", "Inactive", "Unpublished")

SOURCE_BUCKETS = (
    ("applied", "Applied"),
    ("jobdiva", "JobDiva"),
    ("linkedin", "LinkedIn"),
    ("other", "Other"),
)

# The candidate-state read aggregates every launched person's blob. It is the
# heaviest statement here; everything else is small or indexed.
_PEOPLE_STATEMENT_TIMEOUT_MS = int(os.getenv("PAIR_DASHBOARD_PEOPLE_TIMEOUT_MS", "25000"))
_STATEMENT_TIMEOUT_MS = int(os.getenv("PAIR_DASHBOARD_STATEMENT_TIMEOUT_MS", "15000"))

_VERSION_SUFFIX_RE = re.compile(r"-v\d+$", re.IGNORECASE)
_ISO_PREFIX_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def normalize_status(value: Any) -> str:
    return " ".join(str(value or "").upper().split())


def family_key(parent_job_id: Any, jobdiva_id: Any, job_id: Any) -> str:
    """Same rule as routers.recruiter_analytics._family_key (pinned by a test)."""
    parent = str(parent_job_id or "").strip()
    if parent:
        return parent.lower()
    ref = str(jobdiva_id or "").strip() or str(job_id or "").strip()
    return _VERSION_SUFFIX_RE.sub("", ref).lower()


def derive_pair_status(launched: bool, stopped: bool, archived: bool, status: Any) -> str:
    """Mirrors routers/jobs.py and admin analytics: launched jobs are Active
    until outreach is stopped, the job is archived, or JobDiva stops saying OPEN."""
    if not launched:
        return "Unpublished"
    if stopped or archived or (normalize_status(status) or "OPEN") != "OPEN":
        return "Inactive"
    return "Active"


def source_bucket(source: Any) -> str:
    text = str(source or "").strip().lower()
    if "applicant" in text:
        return "applied"
    if text.startswith("jobdiva") or text == "vetteddb":
        return "jobdiva"
    if "linkedin" in text or "unipile" in text or "exa" in text:
        return "linkedin"
    return "other"


def et_date(value: Optional[datetime.datetime]) -> Optional[datetime.date]:
    """Calendar day in US Eastern. Naive values are JobDiva's Eastern wall
    clock; PAIR values are made timezone-aware by the loaders."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(REPORT_TIMEZONE).date()


def as_instant(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """An aware instant: naive JobDiva values are read as Eastern."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=REPORT_TIMEZONE)
    return value


def _db_instant(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, datetime.datetime):
        return None
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=_DB_TIMEZONE).astimezone(UTC)


def _parse_iso_instant(value: Any) -> Optional[datetime.datetime]:
    text = str(value or "").strip()
    if not text or not re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.datetime.fromisoformat(text[:19])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_DB_TIMEZONE)


def _parse_recruiter_emails(raw: Any) -> FrozenSet[str]:
    from routers._helpers import _parse_recruiter_emails as parse

    return frozenset(parse(raw))


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Period:
    """The selected range, the one before it, and the trend weeks."""

    start: Optional[datetime.date]  # None = all time
    end: datetime.date
    previous: Optional[Tuple[datetime.date, datetime.date]]
    weeks: Tuple[datetime.date, ...]  # Mondays, oldest first

    @property
    def days(self) -> Optional[int]:
        return None if self.start is None else (self.end - self.start).days + 1

    def contains(self, day: Optional[datetime.date]) -> bool:
        if day is None:
            return False
        return (self.start is None or day >= self.start) and day <= self.end

    def in_previous(self, day: Optional[datetime.date]) -> bool:
        return day is not None and self.previous is not None and self.previous[0] <= day <= self.previous[1]

    def week_index(self, day: Optional[datetime.date]) -> Optional[int]:
        if day is None or not self.weeks:
            return None
        monday = day - datetime.timedelta(days=day.weekday())
        index = (monday - self.weeks[0]).days // 7
        return index if 0 <= index < len(self.weeks) else None

    @property
    def window_start(self) -> Optional[datetime.date]:
        """Earliest day any number on the page needs (None = unbounded)."""
        if self.start is None:
            return None
        candidates = [self.start, self.weeks[0]]
        if self.previous:
            candidates.append(self.previous[0])
        return min(candidates)


def make_period(
    start: Optional[datetime.date],
    end: Optional[datetime.date],
    today: datetime.date,
    weeks: int = SERIES_WEEKS,
) -> Period:
    if start is None or end is None:
        end = today
        start = None
    last_monday = end - datetime.timedelta(days=end.weekday())
    week_starts = tuple(last_monday - datetime.timedelta(weeks=w) for w in range(weeks - 1, -1, -1))
    previous = None
    if start is not None:
        span = (end - start).days + 1
        previous = (start - datetime.timedelta(days=span), start - datetime.timedelta(days=1))
    return Period(start, end, previous, week_starts)


class _Tally:
    """Weighted event counts for the period, the previous period and each week."""

    def __init__(self, period: Period):
        self.period = period
        self.value = 0.0
        self.previous = 0.0
        self.weekly = [0.0] * len(period.weeks)

    def add(self, day: Optional[datetime.date], weight: float = 1.0) -> None:
        if day is None:
            return
        if self.period.contains(day):
            self.value += weight
        if self.period.in_previous(day):
            self.previous += weight
        index = self.period.week_index(day)
        if index is not None:
            self.weekly[index] += weight


def count_metric(tally: _Tally, **extra: Any) -> Dict[str, Any]:
    return {
        "kind": "count",
        "value": _num(tally.value),
        "previous": _num(tally.previous) if tally.period.previous else None,
        "weekly": [_num(v) for v in tally.weekly],
        **extra,
    }


def ratio_metric(numerator: _Tally, denominator: _Tally, **extra: Any) -> Dict[str, Any]:
    def div(a: float, b: float) -> Optional[float]:
        return round(a / b, 4) if b else None

    return {
        "kind": "ratio",
        "value": div(numerator.value, denominator.value),
        "previous": div(numerator.previous, denominator.previous) if numerator.period.previous else None,
        "weekly": [div(a, b) for a, b in zip(numerator.weekly, denominator.weekly)],
        "numerator": _num(numerator.value),
        "denominator": _num(denominator.value),
        **extra,
    }


def _num(value: float) -> float:
    return int(value) if float(value).is_integer() else round(value, 2)


def trim_to_coverage(metric: Dict[str, Any], period: Period, covered_from: Optional[datetime.date]) -> Dict[str, Any]:
    """Blank what the JobDiva mirror cannot know yet.

    Days before `covered_from` (the oldest day the backfill has reached) are
    not zero, they are unknown: a week that starts before it (wholly or partly
    uncovered) shows no value, a previous period that starts before it has
    nothing to compare against, and a period that starts before it is flagged
    partial.
    """
    if covered_from is None:
        return metric
    weekly = metric.get("weekly") or []
    metric["weekly"] = [None if week < covered_from else value for week, value in zip(period.weeks, weekly)]
    for key in list(metric.get("breakdown") or {}):
        if key.startswith("weekly_") and isinstance(metric["breakdown"][key], list):
            metric["breakdown"][key] = [
                None if week < covered_from else value for week, value in zip(period.weeks, metric["breakdown"][key])
            ]
    if period.previous and period.previous[0] < covered_from:
        metric["previous"] = None
        for key in list(metric.get("breakdown") or {}):
            if key.startswith("previous_"):
                metric["breakdown"][key] = None
    if period.start is None or period.start < covered_from:
        metric["partial_from"] = covered_from.isoformat()
    if period.end < covered_from:
        # Nothing of the period is covered (the backfill has not reached it).
        metric["value"] = None
        for key in ("numerator", "denominator"):
            metric.pop(key, None)
    return metric


def _unavailable_metric(kind: str, period: Period, reason: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "value": None,
        "previous": None,
        "weekly": [None] * len(period.weeks),
        "unavailable": reason,
    }


# ---------------------------------------------------------------------------
# Domain records
# ---------------------------------------------------------------------------

@dataclass
class Req:
    """One PAIR requirement: a JobDiva job and all of its PAIR versions."""

    key: str
    ref: str
    link_key: str
    job_ids: List[str] = field(default_factory=list)
    candidate_keys: Set[str] = field(default_factory=set)
    jobdiva_job_ids: Set[str] = field(default_factory=set)
    # candidate key -> the rank-list URL key of that version (its JobDiva ref,
    # else its job_id), so a person links to the version they were launched on.
    links: Dict[str, str] = field(default_factory=dict)
    title: str = ""
    customer: str = ""
    status: str = ""
    priority: str = ""
    division: str = ""
    recruiter_emails: Set[str] = field(default_factory=set)
    launched_by: str = ""
    added_at: Optional[datetime.datetime] = None
    launch_attempted_at: Optional[datetime.datetime] = None
    first_launch_at: Optional[datetime.datetime] = None
    issue_at: Optional[datetime.datetime] = None
    openings: int = 1
    stopped: bool = False
    archived: bool = False

    @property
    def launched(self) -> bool:
        return self.first_launch_at is not None or self.launch_attempted_at is not None

    @property
    def pair_status(self) -> str:
        return derive_pair_status(self.launched, self.stopped, self.archived, self.status)

    @property
    def counts_as_opening(self) -> bool:
        return normalize_status(self.status) not in EXCLUDED_REQ_STATUSES


@dataclass
class Person:
    """One launched candidate on one req."""

    req: str
    candidate_id: str
    name: str = ""
    link_key: str = ""
    source: str = "other"
    jobdiva_ids: Set[str] = field(default_factory=set)
    launched_at: Optional[datetime.datetime] = None
    outcome: str = "Pending"
    passed_at: Optional[datetime.datetime] = None
    decision: Optional[str] = None
    decision_at: Optional[datetime.datetime] = None
    reject_reason: Optional[str] = None


@dataclass(frozen=True)
class Activity:
    """One JobDiva activity record (jobdiva_activities).

    JobDiva keeps a submittal, each client interview and a hire as SEPARATE
    records of the same (job, candidate): interview and hire records carry no
    submittal date (all 1,213 interviews and 269 hires of a 35-day sample).
    `first_submittal` is that candidate's first submittal to the job, from any
    record, so an interview or a start is attributed the way its submittal was.
    """

    activity_id: str
    job_id: str
    ref: str
    candidate_id: str
    user_id: str
    is_internal: bool
    submittal_date: Optional[datetime.datetime]
    interview_date: Optional[datetime.datetime]  # client interviews only
    start_date: Optional[datetime.datetime]  # hires only
    first_submittal: Optional[datetime.datetime] = None

    @property
    def is_external_submittal(self) -> bool:
        return not self.is_internal and self.submittal_date is not None

    @property
    def anchor_day(self) -> Optional[datetime.date]:
        """The day the record's attribution is judged on: the candidate's first
        submittal to the job, else the record's own first event."""
        for value in (self.first_submittal, self.submittal_date, self.interview_date, self.start_date):
            if value is not None:
                return et_date(value)
        return None


@dataclass(frozen=True)
class PyramidReq:
    """One JobDiva job of the all-reqs mirror (jobdiva_jobs)."""

    job_id: str
    ref: str
    issue_at: Optional[datetime.datetime]
    status: str
    openings: int
    status_changed_at: Optional[datetime.datetime] = None
    updated_at: Optional[datetime.datetime] = None


def is_client_interview(interview_type: Any) -> bool:
    """JobDiva interview types end in "- External" / "- Internal"; untyped
    legacy interviews count as client interviews."""
    return not str(interview_type or "").strip().lower().endswith("internal")


# ---------------------------------------------------------------------------
# Attribution (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attribution:
    req: Optional[str]  # the PAIR req the activity happened on, if any
    direct: bool
    cross_reqs: Tuple[str, ...]  # PAIR reqs whose pass produced it (cross-subs)

    @property
    def is_pair(self) -> bool:
        return self.direct or bool(self.cross_reqs)


class Attributor:
    def __init__(self, reqs: Iterable[Req], people: Iterable[Person]):
        self.req_by_job: Dict[str, str] = {}
        self.req_by_ref: Dict[str, str] = {}
        self.first_launch_day: Dict[str, Optional[datetime.date]] = {}
        for req in reqs:
            for job_id in req.jobdiva_job_ids:
                self.req_by_job.setdefault(job_id, req.key)
            self.req_by_ref.setdefault(req.key, req.key)
            self.first_launch_day[req.key] = et_date(req.first_launch_at or req.launch_attempted_at)
        # JobDiva candidate id -> [(req, pass day)] for every PAIR pass.
        self.passes: Dict[str, List[Tuple[str, Optional[datetime.date]]]] = {}
        for person in people:
            if person.outcome != "Pass":
                continue
            day = et_date(person.passed_at)
            for jd_id in person.jobdiva_ids:
                self.passes.setdefault(jd_id, []).append((person.req, day))

    def req_of(self, activity: Activity) -> Optional[str]:
        req = self.req_by_job.get(activity.job_id)
        if req is None and activity.ref:
            req = self.req_by_ref.get(_VERSION_SUFFIX_RE.sub("", activity.ref).lower())
        return req

    def after_launch(self, req: Optional[str], day: Optional[datetime.date]) -> bool:
        if req is None:
            return False
        launched = self.first_launch_day.get(req)
        if launched is None:
            return False
        return day is None or day >= launched

    def attribute(self, activity: Activity) -> Attribution:
        req = self.req_of(activity)
        day = activity.anchor_day
        passes = self.passes.get(activity.candidate_id, ())
        direct = bool(req) and any(r == req for r, _ in passes) and self.after_launch(req, day)
        cross: List[str] = []
        if not direct and day is not None:
            for passed_req, pass_day in passes:
                if passed_req == req or pass_day is None:
                    continue
                if pass_day <= day <= pass_day + CROSS_SUBMISSION_WINDOW:
                    if passed_req not in cross:
                        cross.append(passed_req)
        return Attribution(req, direct, tuple(sorted(cross)))

    def is_non_pair_on(self, attribution: Attribution, scoped: Set[str], day: Optional[datetime.date]) -> bool:
        return (
            attribution.req in scoped
            and not attribution.is_pair
            and self.after_launch(attribution.req, day)
        )


def credited(attribution: Attribution, scoped: Set[str]) -> Optional[str]:
    """"direct" / "cross" when the activity is a PAIR outcome of a scoped req."""
    if attribution.direct and attribution.req in scoped:
        return "direct"
    if any(r in scoped for r in attribution.cross_reqs):
        return "cross"
    return None


# ---------------------------------------------------------------------------
# Overview (pure)
# ---------------------------------------------------------------------------

def compute_overview(
    *,
    period: Period,
    reqs: Sequence[Req],
    people: Sequence[Person],
    activities: Sequence[Activity],
    attributor: Attributor,
    pyramid_reqs: Optional[Sequence[PyramidReq]],
    activities_available: bool,
    activities_from: Optional[datetime.date] = None,
    jobs_from: Optional[datetime.date] = None,
    all_reqs: Optional[Sequence[Req]] = None,
    volume_unavailable: Optional[str] = None,
) -> Dict[str, Any]:
    """`reqs` are the scoped PAIR reqs; `all_reqs` every PAIR req (a req in the
    scope's JobDiva total that another team launched is still launched with
    PAIR). `activities_from` / `jobs_from`: the oldest day the JobDiva mirror
    covers for activity and for requirements (see trim_to_coverage)."""
    scoped = {r.key for r in reqs}
    tally = lambda: _Tally(period)  # noqa: E731

    net_openings, net_reqs = tally(), tally()
    for req in reqs:
        if req.counts_as_opening:
            day = et_date(req.added_at)
            net_openings.add(day, req.openings)
            net_reqs.add(day)

    launched, passed, recorded_submits = tally(), tally(), tally()
    for person in people:
        if person.req not in scoped:
            continue
        launched.add(et_date(person.launched_at))
        if person.outcome == "Pass":
            passed.add(et_date(person.passed_at))
        if person.decision == "external":
            recorded_submits.add(et_date(person.decision_at))

    subs = {"direct": tally(), "cross": tally()}
    interviews = {"direct": tally(), "cross": tally()}
    starts = {"direct": tally(), "cross": tally()}
    non_pair_subs, non_pair_starts = tally(), tally()
    for activity in activities:
        attribution = attributor.attribute(activity)
        credit = credited(attribution, scoped)
        events = (
            (subs, non_pair_subs, activity.submittal_date if activity.is_external_submittal else None),
            (interviews, None, activity.interview_date),
            (starts, non_pair_starts, activity.start_date),
        )
        for pair_tallies, non_pair_tally, when in events:
            if when is None:
                continue
            day = et_date(when)
            if credit:
                pair_tallies[credit].add(day)
            elif non_pair_tally is not None and attributor.is_non_pair_on(attribution, scoped, activity.anchor_day):
                non_pair_tally.add(day)

    def total(pair: Dict[str, _Tally]) -> _Tally:
        combined = tally()
        combined.value = pair["direct"].value + pair["cross"].value
        combined.previous = pair["direct"].previous + pair["cross"].previous
        combined.weekly = [a + b for a, b in zip(pair["direct"].weekly, pair["cross"].weekly)]
        return combined

    def split(pair: Dict[str, _Tally]) -> Dict[str, Any]:
        return {
            "direct": _num(pair["direct"].value),
            "cross": _num(pair["cross"].value),
            "previous_direct": _num(pair["direct"].previous) if period.previous else None,
            "previous_cross": _num(pair["cross"].previous) if period.previous else None,
            "weekly_direct": [_num(v) for v in pair["direct"].weekly],
            "weekly_cross": [_num(v) for v in pair["cross"].weekly],
        }

    pair_subs, pair_interviews, pair_starts = total(subs), total(interviews), total(starts)
    all_subs = tally()
    all_subs.value = pair_subs.value + non_pair_subs.value
    all_subs.previous = pair_subs.previous + non_pair_subs.previous
    all_subs.weekly = [a + b for a, b in zip(pair_subs.weekly, non_pair_subs.weekly)]

    metrics: Dict[str, Any] = {
        "net_openings": count_metric(net_openings, breakdown={"reqs": _num(net_reqs.value)}),
        "candidates_launched": count_metric(launched),
        "candidates_passed": count_metric(passed),
    }
    if volume_unavailable or pyramid_reqs is None:
        metrics["pair_volume"] = _unavailable_metric(
            "ratio", period, volume_unavailable or "Waiting for the JobDiva requirements sync (all Pyramid reqs)."
        )
    else:
        metrics["pair_volume"] = trim_to_coverage(
            pair_volume_metric(period, reqs, pyramid_reqs, all_reqs), period, jobs_from
        )

    if not activities_available:
        reason = "Waiting for the JobDiva activity sync (submittals, interviews, starts)."
        for key, kind in (
            ("pair_submissions", "count"),
            ("pair_interviews", "count"),
            ("pair_starts", "count"),
            ("fill_ratio", "ratio"),
            ("submit_to_start", "ratio"),
            ("non_pair_submissions", "count"),
            ("non_pair_starts", "count"),
            ("pair_share", "ratio"),
        ):
            metrics[key] = _unavailable_metric(kind, period, reason)
        metrics["pair_submissions"]["breakdown"] = {"recorded_in_pair": _num(recorded_submits.value)}
        return metrics

    metrics.update({
        "pair_submissions": count_metric(
            pair_subs,
            breakdown={**split(subs), "recorded_in_pair": _num(recorded_submits.value)},
        ),
        "pair_interviews": count_metric(pair_interviews, breakdown=split(interviews)),
        "pair_starts": count_metric(pair_starts, breakdown=split(starts)),
        "fill_ratio": ratio_metric(pair_starts, net_openings),
        "submit_to_start": ratio_metric(pair_starts, pair_subs),
        "non_pair_submissions": count_metric(non_pair_subs),
        "non_pair_starts": count_metric(non_pair_starts),
        "pair_share": ratio_metric(pair_subs, all_subs),
    })
    for key in ("pair_submissions", "pair_interviews", "pair_starts", "submit_to_start",
                "non_pair_submissions", "non_pair_starts", "pair_share"):
        trim_to_coverage(metrics[key], period, activities_from)
    # Fill ratio needs both: PAIR's own openings and JobDiva's starts.
    trim_to_coverage(metrics["fill_ratio"], period, activities_from)
    return metrics


def pair_volume_metric(
    period: Period,
    reqs: Sequence[Req],
    pyramid_reqs: Sequence[PyramidReq],
    all_reqs: Optional[Sequence[Req]] = None,
) -> Dict[str, Any]:
    """Of the openings Pyramid received (JobDiva issue date) in the period, the
    share on reqs that were launched with PAIR.

    The denominator is the all-reqs mirror in scope PLUS every scoped PAIR req,
    so a PAIR req the mirror has not caught yet can never push the ratio past
    100%. A mirror req is matched against EVERY PAIR req (`all_reqs`): a req a
    team is tagged on but another team launched is still launched with PAIR.
    """
    by_job: Dict[str, Req] = {}
    by_ref: Dict[str, Req] = {}
    for req in list(all_reqs or ()) + list(reqs):
        for job_id in req.jobdiva_job_ids:
            by_job[job_id] = req
        by_ref[req.key] = req

    numerator, denominator = _Tally(period), _Tally(period)
    seen: Set[str] = set()
    for job in pyramid_reqs:
        req = by_job.get(job.job_id) or (by_ref.get(job.ref.lower()) if job.ref else None)
        if req is not None:
            seen.add(req.key)
        if normalize_status(job.status) in EXCLUDED_REQ_STATUSES:
            continue
        day = et_date(job.issue_at)
        denominator.add(day, job.openings)
        if req is not None and req.launched:
            numerator.add(day, job.openings)
    for req in reqs:
        if req.key in seen or not req.counts_as_opening:
            continue
        day = et_date(req.issue_at or req.added_at)
        denominator.add(day, req.openings)
        if req.launched:
            numerator.add(day, req.openings)
    return ratio_metric(numerator, denominator)


# ---------------------------------------------------------------------------
# Funnel & Speed (pure)
# ---------------------------------------------------------------------------

def _stats(values: Iterable[float]) -> Dict[str, Any]:
    cleaned = sorted(v for v in values if v is not None)
    if not cleaned:
        return {"median_hours": None, "mean_hours": None, "jobs": 0}
    return {
        "median_hours": round(statistics.median(cleaned), 1),
        "mean_hours": round(sum(cleaned) / len(cleaned), 1),
        "jobs": len(cleaned),
    }


def _hours_between(start: Optional[datetime.datetime], end: Optional[datetime.datetime]) -> Optional[float]:
    """Hours from start to end, both made aware. Up to a day backwards is
    clock skew (JobDiva dates some records at midnight) and reads as 0; more
    than that, or over a year, is a data problem and is left out."""
    a, b = as_instant(start), as_instant(end)
    if a is None or b is None:
        return None
    hours = (b - a).total_seconds() / 3600.0
    if hours < -24 or hours > 365 * 24:
        return None
    return max(hours, 0.0)


def compute_funnel(
    *,
    cohort: Sequence[Req],
    people: Sequence[Person],
    activities: Sequence[Activity],
    attributor: Attributor,
    activities_available: bool,
    now: datetime.datetime,
    pending_limit: int = 500,
) -> Dict[str, Any]:
    scoped = {r.key for r in cohort}
    reqs_by_key = {r.key: r for r in cohort}
    cohort_people = [p for p in people if p.req in scoped]

    initiated: Dict[str, int] = {key: 0 for key, _ in SOURCE_BUCKETS}
    completed = passed_count = 0
    for person in cohort_people:
        initiated[person.source if person.source in initiated else "other"] += 1
        if person.outcome in ("Pass", "Fail"):
            completed += 1
        if person.outcome == "Pass":
            passed_count += 1

    # PAIR people reaching each JobDiva stage (a person counts once per stage;
    # direct wins over cross), and non-PAIR JobDiva candidates on cohort reqs.
    pair_stage: Dict[str, Dict[Tuple[str, str], str]] = {"submission": {}, "interview": {}, "start": {}}
    non_pair_stage: Dict[str, Set[Tuple[str, str]]] = {"submission": set(), "interview": set(), "start": set()}
    first_submit: Dict[str, datetime.datetime] = {}
    people_by_jd: Dict[Tuple[str, str], Person] = {}
    for person in cohort_people:
        if person.outcome == "Pass":
            for jd_id in person.jobdiva_ids:
                people_by_jd[(person.req, jd_id)] = person

    for activity in activities:
        attribution = attributor.attribute(activity)
        stages = (
            ("submission", activity.submittal_date if activity.is_external_submittal else None),
            ("interview", activity.interview_date),
            ("start", activity.start_date),
        )
        if attribution.direct and attribution.req in scoped:
            credits = [(attribution.req, "direct")]
        else:
            credits = [(r, "cross") for r in attribution.cross_reqs if r in scoped]
        for stage, when in stages:
            if when is None:
                continue
            for req_key, kind in credits:
                person = people_by_jd.get((req_key, activity.candidate_id))
                person_key = (req_key, person.candidate_id if person else activity.candidate_id)
                if pair_stage[stage].get(person_key) != "direct":
                    pair_stage[stage][person_key] = kind
                if stage == "submission" and kind == "direct":
                    earliest = first_submit.get(req_key)
                    instant = as_instant(when)
                    if earliest is None or instant < earliest:
                        first_submit[req_key] = instant
            if not credits and attributor.is_non_pair_on(attribution, scoped, activity.anchor_day):
                non_pair_stage[stage].add((attribution.req, activity.candidate_id))

    # A PAIR-recorded external Submit is also a first submit (JobDiva may lag).
    for person in cohort_people:
        if person.decision == "external" and person.decision_at is not None:
            earliest = first_submit.get(person.req)
            if earliest is None or person.decision_at < earliest:
                first_submit[person.req] = person.decision_at

    def pair_stage_entry(stage: str, label: str, direct_label: str, cross_label: str) -> Dict[str, Any]:
        kinds = list(pair_stage[stage].values())
        entry = {
            "key": f"pair_{stage}",
            "label": label,
            "count": len(kinds),
            "segments": [
                {"key": "direct", "label": direct_label, "count": kinds.count("direct")},
                {"key": "cross", "label": cross_label, "count": kinds.count("cross")},
            ],
        }
        if not activities_available:
            entry["unavailable"] = True
        return entry

    stages: List[Dict[str, Any]] = [
        {
            "key": "initiated",
            "label": "Engage Initiated",
            "count": len(cohort_people),
            "segments": [{"key": key, "label": label, "count": initiated[key]} for key, label in SOURCE_BUCKETS],
        },
        {"key": "completed", "label": "Engage Completed", "count": completed},
        {"key": "passed", "label": "Engage Passed", "count": passed_count},
        pair_stage_entry("submission", "PAIR – Submission", "Direct Submission", "Cross-Sub"),
        pair_stage_entry("interview", "PAIR – Interview", "Direct Interview", "Cross-Sub Interview"),
        pair_stage_entry("start", "PAIR – Start", "Direct Start", "Cross-Sub Start"),
    ]
    for stage, label in (("submission", "Non-PAIR – Submission"), ("interview", "Non-PAIR – Interview"), ("start", "Non-PAIR – Start")):
        entry = {"key": f"non_pair_{stage}", "label": label, "count": len(non_pair_stage[stage]), "group": "non_pair"}
        if not activities_available:
            entry["unavailable"] = True
        stages.append(entry)

    # Speed to Pipeline: per req, then median / mean across the cohort.
    first_pass: Dict[str, datetime.datetime] = {}
    for person in cohort_people:
        if person.outcome == "Pass" and person.passed_at is not None:
            if person.req not in first_pass or person.passed_at < first_pass[person.req]:
                first_pass[person.req] = person.passed_at
    to_launch, to_pass, submit_from_launch, submit_from_posted = [], [], [], []
    for req in cohort:
        launch = req.first_launch_at or req.launch_attempted_at
        to_launch.append(_hours_between(req.issue_at, launch))
        to_pass.append(_hours_between(launch, first_pass.get(req.key)))
        submit_from_launch.append(_hours_between(launch, first_submit.get(req.key)))
        submit_from_posted.append(_hours_between(req.issue_at, first_submit.get(req.key)))
    speed = [
        {"key": "time_to_launch", "label": "Time to PAIR Launch", "from": "from job posted", **_stats(to_launch)},
        {"key": "time_to_first_pass", "label": "Time to First Pass", "from": "from PAIR launch", **_stats(to_pass)},
        {"key": "time_to_first_submit", "label": "Time to First Submit", "from": "from PAIR launch", **_stats(submit_from_launch)},
        {"key": "time_to_first_submit_posted", "label": "Time to First Submit", "from": "from job posted", **_stats(submit_from_posted)},
    ]

    # Rejection reasons: the recruiter's Reject reason on PAIR candidates.
    reasons: Dict[str, int] = {}
    for person in cohort_people:
        if person.decision == "reject":
            reason = (person.reject_reason or "").strip() or "No reason given"
            reasons[reason] = reasons.get(reason, 0) + 1
    rejected = sum(reasons.values())

    # Pending feedback: passed, and the recruiter has recorded no decision.
    # The clock starts at the pass.
    pending = []
    given = 0
    for person in cohort_people:
        if person.outcome != "Pass":
            continue
        if person.decision is not None:
            given += 1
            continue
        req = reqs_by_key.get(person.req)
        since = person.passed_at or person.launched_at
        days = None
        if since is not None:
            days = max(0, int((now - since).total_seconds() // 86400))
        pending.append({
            "candidate_id": person.candidate_id,
            "name": person.name or "Unnamed candidate",
            "job_key": person.link_key or (req.link_key if req else person.req),
            "job_ref": req.ref if req else person.req,
            "job_title": req.title if req else "",
            "recruiters": sorted(_recruiters_of(req)) if req else [],
            "passed_at": since.isoformat() if since else None,
            "days_pending": days,
        })
    pending.sort(key=lambda row: (-(row["days_pending"] or 0), row["name"]))

    return {
        "cohort": {"reqs": len(cohort)},
        "stages": stages,
        "speed": speed,
        "rejection_reasons": {
            "total": rejected,
            "reasons": [
                {"reason": reason, "count": count}
                for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        },
        "pending_feedback": {
            "pending": len(pending),
            "passed": passed_count,
            "given": given,
            "candidates": pending[:pending_limit],
            "truncated": len(pending) > pending_limit,
        },
    }


def _recruiters_of(req: Req) -> Set[str]:
    if req.launched_by:
        return {req.launched_by}
    return set(req.recruiter_emails)


# ---------------------------------------------------------------------------
# Productivity (pure)
# ---------------------------------------------------------------------------

def req_active_through(job: PyramidReq) -> Optional[datetime.date]:
    """Last day the req counted as worked: None while it is still open,
    otherwise the day its status changed (or, before JobsDetail has filled
    that in, its last update / issue day)."""
    if normalize_status(job.status) in ACTIVE_REQ_STATUSES:
        return None
    changed = job.status_changed_at or job.updated_at or job.issue_at
    return et_date(changed)


def compute_productivity(
    *,
    period: Period,
    recruiter_count: int,
    assigned_reqs: Sequence[PyramidReq],
    activities: Sequence[Activity],
    attributor: Attributor,
    coverage_start: Optional[datetime.date] = None,
    reqs_unavailable: Optional[str] = None,
) -> Dict[str, Any]:
    """Weekly averages per recruiter (the team's members), for the team's reqs
    (any team email tagged on the req in JobDiva) and the submittals / starts
    its members made. `reqs_unavailable` blanks Reqs Assigned while the
    job-user tags are still being read.

    All time means the window the JobDiva mirror covers (`coverage_start`),
    capped at a year: a weekly average over a decade of reqs says nothing.
    """
    weeks = list(period.weeks)

    def week_bounds(monday: datetime.date) -> Tuple[datetime.date, datetime.date]:
        return monday, monday + datetime.timedelta(days=6)

    def active_in(job: PyramidReq, lo: datetime.date, hi: datetime.date) -> bool:
        issued = et_date(job.issue_at)
        if issued is None or issued > hi:
            return False
        through = req_active_through(job)
        return through is None or through >= lo

    def mondays_between(lo: datetime.date, hi: datetime.date) -> List[datetime.date]:
        first = lo - datetime.timedelta(days=lo.weekday())
        out = []
        cursor = first
        while cursor <= hi:
            out.append(cursor)
            cursor += datetime.timedelta(days=7)
        return out

    def avg_active(lo: datetime.date, hi: datetime.date) -> Optional[float]:
        mondays = mondays_between(lo, hi)
        if not mondays or recruiter_count <= 0:
            return None
        totals = []
        for monday in mondays:
            a, b = week_bounds(monday)
            totals.append(sum(1 for job in assigned_reqs if active_in(job, a, b)))
        return round(sum(totals) / len(mondays) / recruiter_count, 2)

    start = period.start
    if start is None:
        floor = period.end - datetime.timedelta(days=364)
        start = max(coverage_start or floor, floor)
    span_weeks = max(((period.end - start).days + 1) / 7.0, 1 / 7.0)

    subs = {"pair": _Tally(period), "non_pair": _Tally(period)}
    starts = {"pair": _Tally(period), "non_pair": _Tally(period)}
    in_range_subs = {"pair": 0, "non_pair": 0}
    in_range_starts = {"pair": 0, "non_pair": 0}
    for activity in activities:
        kind = "pair" if attributor.attribute(activity).is_pair else "non_pair"
        if activity.is_external_submittal:
            day = et_date(activity.submittal_date)
            subs[kind].add(day)
            if start <= (day or datetime.date.min) <= period.end:
                in_range_subs[kind] += 1
        if activity.start_date is not None:
            day = et_date(activity.start_date)
            starts[kind].add(day)
            if start <= (day or datetime.date.min) <= period.end:
                in_range_starts[kind] += 1

    def per_recruiter_week(count: float, weeks_: float) -> Optional[float]:
        if recruiter_count <= 0 or weeks_ <= 0:
            return None
        return round(count / recruiter_count / weeks_, 2)

    def rate_metric(tallies: Dict[str, _Tally], in_range: Dict[str, int]) -> Dict[str, Any]:
        prev_weeks = (period.days / 7.0) if period.previous else None
        pair_prev = tallies["pair"].previous
        non_prev = tallies["non_pair"].previous
        return {
            "kind": "average",
            "value": per_recruiter_week(in_range["pair"] + in_range["non_pair"], span_weeks),
            "previous": per_recruiter_week(pair_prev + non_prev, prev_weeks) if prev_weeks else None,
            "weekly": [
                per_recruiter_week(a + b, 1.0) for a, b in zip(tallies["pair"].weekly, tallies["non_pair"].weekly)
            ],
            "breakdown": {
                "pair": per_recruiter_week(in_range["pair"], span_weeks),
                "non_pair": per_recruiter_week(in_range["non_pair"], span_weeks),
                "pair_total": in_range["pair"],
                "non_pair_total": in_range["non_pair"],
            },
        }

    reqs_value = avg_active(start, period.end)
    reqs_previous = avg_active(*period.previous) if period.previous else None
    reqs_weekly = [avg_active(*week_bounds(monday)) for monday in weeks]
    metrics = {
        "reqs_assigned": {
            "kind": "average",
            "value": reqs_value,
            "previous": reqs_previous,
            "weekly": reqs_weekly,
            "breakdown": {"reqs": sum(1 for j in assigned_reqs if active_in(j, start, period.end))},
        },
        "client_subs": rate_metric(subs, in_range_subs),
        "starts": rate_metric(starts, in_range_starts),
    }
    for metric in metrics.values():
        trim_to_coverage(metric, period, coverage_start)
    if reqs_unavailable:
        metrics["reqs_assigned"] = _unavailable_metric("average", period, reqs_unavailable)
    return {
        "recruiters": recruiter_count,
        "weeks": [w.isoformat() for w in weeks],
        "covered_from": coverage_start.isoformat() if coverage_start else None,
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Loaders (SQL)
# ---------------------------------------------------------------------------

def _set_timeout(cur, ms: int) -> None:
    cur.execute(f"SET LOCAL statement_timeout = '{int(ms)}ms'")


def table_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
        return bool(cur.fetchone()[0])


def load_reqs(conn) -> List[Req]:
    """Every PAIR req (all versions folded), newest first, with the JobDiva
    job's own status / priority / division / issue date where the mirror has
    it (the monitored_jobs copy otherwise)."""
    from routers._helpers import _ts, _ts_utc, optional_monitored_jobs_columns

    has_mirror = table_exists(conn, "jobdiva_jobs")
    mirror_cols = (
        "jj.job_id, jj.job_status, jj.priority, jj.division_name, jj.openings, jj.issue_date, jj.company_name"
        if has_mirror
        else "NULL, NULL, NULL, NULL, NULL, NULL::timestamp, NULL"
    )
    mirror_join = (
        """
        LEFT JOIN LATERAL (
            SELECT j.job_id, j.job_status, j.priority, j.division_name, j.openings, j.issue_date, j.company_name
            FROM jobdiva_jobs j
            WHERE j.job_id = mj.job_id::text
               OR LOWER(j.jobdiva_ref) = LOWER(REGEXP_REPLACE(COALESCE(NULLIF(TRIM(mj.parent_job_id::text), ''),
                                                  NULLIF(TRIM(mj.jobdiva_id::text), ''), ''), '-v[0-9]+$', '', 'i'))
            ORDER BY (j.job_id = mj.job_id::text) DESC
            LIMIT 1
        ) jj ON TRUE
        """
        if has_mirror
        else ""
    )
    with conn.cursor() as cur:
        _set_timeout(cur, _STATEMENT_TIMEOUT_MS)
        optional = optional_monitored_jobs_columns(cur, "mj.")
        cur.execute(f"""
            SELECT mj.job_id::text, mj.jobdiva_id::text, mj.parent_job_id::text,
                   COALESCE(NULLIF(TRIM(mj.enhanced_title), ''), NULLIF(TRIM(mj.title), ''), 'Untitled'),
                   COALESCE(NULLIF(TRIM(mj.customer_name), ''), ''),
                   mj.status, mj.priority, mj.openings::text, mj.posted_date,
                   {_ts_utc('mj.created_at')},
                   ({_ts('mj.pair_launched_at')}) AT TIME ZONE 'UTC',
                   mj.outreach_stopped_at IS NOT NULL,
                   COALESCE(mj.is_archived, FALSE),
                   mj.recruiter_emails,
                   {optional['pair_launched_by']},
                   {mirror_cols}
            FROM monitored_jobs mj
            {mirror_join}
        """)
        rows = cur.fetchall()
    return build_reqs(rows)


def build_reqs(rows: Iterable[Sequence[Any]]) -> List[Req]:
    from routers._helpers import _parse_posted_date

    reqs: Dict[str, Req] = {}
    newest_added: Dict[str, datetime.datetime] = {}
    for row in rows:
        (job_id, jobdiva_id, parent, title, customer, status, priority, openings, posted,
         created_at, launched_at, stopped, archived, recruiter_emails, launched_by,
         jd_job_id, jd_status, jd_priority, jd_division, jd_openings, jd_issue, jd_company) = row
        key = family_key(parent, jobdiva_id, job_id)
        if not key:
            continue
        req = reqs.get(key)
        if req is None:
            req = reqs[key] = Req(key=key, ref=key.upper(), link_key=str(jobdiva_id or job_id or key))
        req.job_ids.append(str(job_id))
        version_link = str(jobdiva_id or job_id or key).strip()
        for candidate_key in (job_id, jobdiva_id):
            if candidate_key and str(candidate_key).strip():
                req.candidate_keys.add(str(candidate_key).strip())
                req.links[str(candidate_key).strip()] = version_link
        if str(job_id or "").isdigit():
            req.jobdiva_job_ids.add(str(job_id))
        if jd_job_id and str(jd_job_id).isdigit():
            req.jobdiva_job_ids.add(str(jd_job_id))
        added = created_at if isinstance(created_at, datetime.datetime) else None
        if added is not None:
            added = added if added.tzinfo else added.replace(tzinfo=UTC)
            if req.added_at is None or added < req.added_at:
                req.added_at = added
        launched = launched_at if isinstance(launched_at, datetime.datetime) else None
        if launched is not None:
            launched = launched if launched.tzinfo else launched.replace(tzinfo=UTC)
            if req.launch_attempted_at is None or launched < req.launch_attempted_at:
                req.launch_attempted_at = launched
        # The newest version describes the req (title, recruiters, stop state).
        stamp = added or datetime.datetime.min.replace(tzinfo=UTC)
        if key not in newest_added or stamp >= newest_added[key]:
            newest_added[key] = stamp
            req.link_key = str(jobdiva_id or job_id or key)
            req.title = title or req.title
            req.customer = jd_company or customer or req.customer
            req.status = normalize_status(jd_status or status)
            req.priority = str(jd_priority or priority or "").strip()
            req.division = str(jd_division or "").strip()
            req.recruiter_emails = set(_parse_recruiter_emails(recruiter_emails))
            req.launched_by = str(launched_by or "").strip().lower()
            req.stopped = bool(stopped)
            req.archived = bool(archived)
            req.openings = _positive_int(jd_openings) or _positive_int(openings) or 1
        issue = jd_issue if isinstance(jd_issue, datetime.datetime) else None
        if issue is None:
            posted_day = _parse_posted_date(posted) if isinstance(posted, str) else None
            if posted_day is not None:
                issue = datetime.datetime.combine(posted_day, datetime.time.min)
        if issue is not None and (req.issue_at is None or issue < req.issue_at):
            req.issue_at = issue  # naive Eastern, like the mirror
    ordered = sorted(reqs.values(), key=lambda r: r.added_at or datetime.datetime.min.replace(tzinfo=UTC), reverse=True)
    return ordered


# The candidate blob is read ONCE per row: jsonb_to_record pulls just the keys
# the shared rules need into a small, untoasted copy named `data`, and the rank
# list's own SQL (engage_display_sql, the feedback predicates) runs on that copy
# unchanged. A dozen `sc.data->>` calls on the stored blob would detoast it a
# dozen times, per launched person. tests/test_pair_dashboard.py fails if either
# rule starts reading a key this list does not carry.
SLIM_KEYS: Tuple[str, ...] = (
    "engage_status",
    "engage_score",
    "engage_hard_filter_status",
    "hard_filter_status",
    "first_completed_at",
    "engage_completed_at",
    "engage_updated_at",
    "jobdiva_candidate_id",
    "feedback_type",
    "submission_type",
    "feedback_at",
    "feedback_reason",
    "engage_interview_id",
)

_SLIM_LATERAL = (
    "CROSS JOIN LATERAL (SELECT to_jsonb(r) AS data FROM jsonb_to_record("
    "CASE WHEN jsonb_typeof(sc.data) = 'object' THEN sc.data ELSE '{}'::jsonb END"
    ") AS r(" + ", ".join(f"{key} text" for key in SLIM_KEYS) + ")) slim"
)

_DISPLAY_SQL = engage_display_sql("slim.data")


def _decision_sql(alias: str = "slim.") -> str:
    p = lambda fragment: fragment.format(alias=alias)  # noqa: E731
    return (
        "CASE"
        f" WHEN {p(IS_INTERNAL_SUBMIT_SQL)} THEN 'internal'"
        f" WHEN {p(IS_EXTERNAL_SUBMIT_SQL)} THEN 'external'"
        f" WHEN {p(IS_REJECT_SQL)} THEN 'reject'"
        f" WHEN {p(IS_UNREACHABLE_SQL)} THEN 'unreachable'"
        f" WHEN {p(HAS_DECISION_SQL)} THEN 'other'"
        " END"
    )


_LAUNCHES_SQL = """
    SELECT jobdiva_id, candidate_id, MIN(created_at)
    FROM engage_interview_audit
    WHERE jobdiva_id = ANY(%s)
      AND COALESCE(NULLIF(interview_id, ''), '') <> ''
      AND COALESCE(NULLIF(candidate_id, ''), '') <> ''
    GROUP BY jobdiva_id, candidate_id
"""

_PEOPLE_ROWS_SQL = f"""
    SELECT l.key, sc.id, sc.jobdiva_id, sc.candidate_id, sc.source, sc.name,
           {_DISPLAY_SQL},
           slim.data->>'first_completed_at',
           slim.data->>'engage_completed_at',
           slim.data->>'engage_updated_at',
           NULLIF(TRIM(COALESCE(slim.data->>'jobdiva_candidate_id', '')), ''),
           {_decision_sql()},
           slim.data->>'feedback_at',
           slim.data->>'feedback_reason',
           NULLIF(TRIM(COALESCE(slim.data->>'engage_interview_id', '')), '')
    FROM unnest(%s::text[], %s::text[]) AS l(key, candidate_id)
    JOIN sourced_candidates sc ON sc.jobdiva_id = l.key AND sc.candidate_id = l.candidate_id
    {_SLIM_LATERAL}
"""


def load_people(conn, reqs: Sequence[Req]) -> List[Person]:
    """Every launched person on the given reqs, collapsed across both job keys
    and every PAIR version of the req."""
    key_to_req: Dict[str, str] = {}
    links: Dict[str, str] = {}
    for req in reqs:
        for candidate_key in req.candidate_keys:
            key_to_req.setdefault(candidate_key, req.key)
            links.setdefault(candidate_key, req.links.get(candidate_key, req.link_key))
    if not key_to_req:
        return []
    with conn.cursor() as cur:
        _set_timeout(cur, _PEOPLE_STATEMENT_TIMEOUT_MS)
        cur.execute(_LAUNCHES_SQL, (list(key_to_req),))
        launch_rows = cur.fetchall()
        first_launch: Dict[Tuple[str, str], datetime.datetime] = {}
        for key, candidate_id, created_at in launch_rows:
            req = key_to_req.get(str(key))
            instant = _db_instant(created_at)
            if req is None or instant is None:
                continue
            slot = (req, str(candidate_id))
            if slot not in first_launch or instant < first_launch[slot]:
                first_launch[slot] = instant
        # Every stored row of each launched person, under any key of the req.
        keys_by_req: Dict[str, List[str]] = {}
        for candidate_key, req in key_to_req.items():
            keys_by_req.setdefault(req, []).append(candidate_key)
        pair_keys: List[str] = []
        pair_ids: List[str] = []
        for (req, candidate_id) in first_launch:
            for candidate_key in keys_by_req.get(req, ()):
                pair_keys.append(candidate_key)
                pair_ids.append(candidate_id)
        rows = []
        if pair_keys:
            cur.execute(_PEOPLE_ROWS_SQL, (pair_keys, pair_ids))
            rows = cur.fetchall()
    return build_people(first_launch, rows, key_to_req, links)


_OUTCOME_RANK = {"Pass": 3, "Fail": 2, "In Progress": 1, "Pending": 0}


def build_people(
    first_launch: Dict[Tuple[str, str], datetime.datetime],
    rows: Iterable[Sequence[Any]],
    key_to_req: Dict[str, str],
    links: Optional[Dict[str, str]] = None,
) -> List[Person]:
    people: Dict[Tuple[str, str], Person] = {}
    newest_row: Dict[Tuple[str, str], int] = {}
    decision_rank: Dict[Tuple[str, str], Tuple[datetime.datetime, int]] = {}
    for (req, candidate_id), launched_at in first_launch.items():
        people[(req, candidate_id)] = Person(req=req, candidate_id=candidate_id, launched_at=launched_at)
    for row in rows:
        (key, row_id, stored_key, candidate_id, source, name, display, first_completed,
         completed, updated, jd_candidate_id, decision, feedback_at, feedback_reason, interview_id) = row
        req = key_to_req.get(str(key))
        person = people.get((req, str(candidate_id))) if req else None
        if person is None:
            continue
        slot = (person.req, person.candidate_id)
        if str(candidate_id).strip():
            person.jobdiva_ids.add(str(candidate_id).strip())
        if jd_candidate_id:
            person.jobdiva_ids.add(str(jd_candidate_id).strip())
        if row_id is not None and (slot not in newest_row or int(row_id) > newest_row[slot]):
            newest_row[slot] = int(row_id)
            person.name = str(name or "").strip() or person.name
            person.source = source_bucket(source)
            person.link_key = (links or {}).get(str(stored_key or ""), str(stored_key or ""))
        display = str(display or "Pending")
        if _OUTCOME_RANK.get(display, 0) > _OUTCOME_RANK.get(person.outcome, 0):
            person.outcome = display
        if display == "Pass":
            passed = (
                _parse_iso_instant(first_completed)
                or _parse_iso_instant(completed)
                or _parse_iso_instant(updated)
            )
            if passed is not None and (person.passed_at is None or passed < person.passed_at):
                person.passed_at = passed
        if decision:
            at = _parse_iso_instant(feedback_at)
            rank = (at or datetime.datetime.min.replace(tzinfo=UTC), int(row_id or 0))
            if slot not in decision_rank or rank > decision_rank[slot]:
                decision_rank[slot] = rank
                person.decision = str(decision)
                person.decision_at = at
                person.reject_reason = str(feedback_reason or "").strip() or None
    for person in people.values():
        if person.outcome == "Pass" and person.passed_at is None:
            person.passed_at = person.launched_at
    return list(people.values())


def first_launch_by_req(people: Iterable[Person]) -> Dict[str, datetime.datetime]:
    out: Dict[str, datetime.datetime] = {}
    for person in people:
        if person.launched_at is not None and (person.req not in out or person.launched_at < out[person.req]):
            out[person.req] = person.launched_at
    return out


def _window_sql(column: str, lo: Optional[datetime.date], hi: datetime.date) -> Tuple[str, List[Any]]:
    upper = datetime.datetime.combine(hi + datetime.timedelta(days=1), datetime.time.min)
    if lo is None:
        return f"({column} IS NOT NULL AND {column} < %s)", [upper]
    lower = datetime.datetime.combine(lo, datetime.time.min)
    return f"({column} >= %s AND {column} < %s)", [lower, upper]


def _job_attribute_sql(filters: "DashboardFilters", column: str = "") -> Tuple[List[str], List[Any]]:
    """Vertical / client / priority / JD status clauses on jobdiva_jobs columns."""
    prefix = f"{column}." if column else ""
    clauses: List[str] = []
    params: List[Any] = []
    if filters.vertical:
        clauses.append(f"LOWER(TRIM(COALESCE({prefix}division_name, ''))) = %s")
        params.append(filters.vertical.strip().lower())
    if filters.client:
        clauses.append(f"LOWER(TRIM(COALESCE({prefix}company_name, ''))) = %s")
        params.append(filters.client.strip().lower())
    if filters.priority:
        clauses.append(f"LOWER(TRIM(COALESCE({prefix}priority, ''))) = %s")
        params.append(filters.priority.strip().lower())
    if filters.jd_status:
        clauses.append(f"UPPER(TRIM(COALESCE({prefix}job_status, ''))) = %s")
        params.append(normalize_status(filters.jd_status))
    return clauses, params


def load_activities(
    conn,
    *,
    lo: Optional[datetime.date],
    hi: datetime.date,
    job_ids: Sequence[str] = (),
    refs: Sequence[str] = (),
    candidate_ids: Sequence[str] = (),
    user_ids: Sequence[str] = (),
    job_filters: Optional["DashboardFilters"] = None,
) -> List[Activity]:
    """Activities with a submittal, client interview or start in [lo, hi] that
    touch the given jobs / refs / candidates / users. job_filters further
    restricts them to jobs whose mirror row matches (Productivity's vertical /
    client / priority)."""
    selectors = []
    params: List[Any] = []
    if job_ids:
        selectors.append("job_id = ANY(%s)")
        params.append(list(job_ids))
    if refs:
        selectors.append("LOWER(REGEXP_REPLACE(COALESCE(jobdiva_ref, ''), '-v[0-9]+$', '', 'i')) = ANY(%s)")
        params.append(list(refs))
    if candidate_ids:
        selectors.append("candidate_id = ANY(%s)")
        params.append(list(candidate_ids))
    if user_ids:
        selectors.append("user_id = ANY(%s)")
        params.append(list(user_ids))
    if not selectors:
        return []
    dated = []
    for column in ("submittal_date", "interview_date", "start_date"):
        clause, values = _window_sql(column, lo, hi)
        dated.append(clause)
        params.extend(values)
    job_clause = ""
    if job_filters is not None:
        clauses, values = _job_attribute_sql(job_filters, "j")
        if clauses:
            job_clause = f" AND job_id IN (SELECT j.job_id FROM jobdiva_jobs j WHERE {' AND '.join(clauses)})"
            params.extend(values)
    with conn.cursor() as cur:
        _set_timeout(cur, _STATEMENT_TIMEOUT_MS)
        cur.execute(f"""
            SELECT a.activity_id, COALESCE(a.job_id, ''), COALESCE(a.jobdiva_ref, ''), COALESCE(a.candidate_id, ''),
                   COALESCE(a.user_id, ''), a.is_internal, a.submittal_date,
                   a.interview_flag, a.interview_date, a.interview_type, a.hire_flag, a.start_date,
                   (SELECT MIN(s.submittal_date) FROM jobdiva_activities s
                     WHERE s.candidate_id = a.candidate_id AND s.job_id = a.job_id
                       AND s.submittal_date IS NOT NULL) AS first_submittal
            FROM (SELECT * FROM jobdiva_activities
                  WHERE ({' OR '.join(selectors)}) AND ({' OR '.join(dated)}){job_clause}) a
        """, params)
        rows = cur.fetchall()
    return [activity_from_row(r) for r in rows]


def activity_from_row(row: Sequence[Any]) -> Activity:
    (activity_id, job_id, ref, candidate_id, user_id, is_internal, submittal_date,
     interview_flag, interview_date, interview_type, hire_flag, start_date, first_submittal) = row
    return Activity(
        activity_id=str(activity_id),
        job_id=str(job_id or ""),
        ref=str(ref or ""),
        candidate_id=str(candidate_id or ""),
        user_id=str(user_id or ""),
        is_internal=bool(is_internal),
        submittal_date=submittal_date,
        interview_date=interview_date if interview_flag and is_client_interview(interview_type) else None,
        start_date=start_date if hire_flag else None,
        first_submittal=first_submittal,
    )


def load_pyramid_reqs(
    conn,
    *,
    lo: Optional[datetime.date],
    hi: datetime.date,
    filters: "DashboardFilters",
    team_user_ids: Optional[Sequence[str]],
    team_job_ids: Sequence[str] = (),
    active_during: bool = False,
) -> List[PyramidReq]:
    """All-reqs mirror rows, either issued in [lo, hi] (PAIR Volume) or, with
    active_during, issued by hi and not closed before lo (Productivity)."""
    where = []
    params: List[Any] = []
    if active_during:
        clause, values = _window_sql("issue_date", None, hi)
        where.append(clause)
        params.extend(values)
        if lo is not None:
            where.append(
                "(UPPER(TRIM(COALESCE(job_status, ''))) IN ('OPEN', '') "
                "OR COALESCE(status_updated_at, jobdiva_updated_at, issue_date) >= %s)"
            )
            params.append(datetime.datetime.combine(lo, datetime.time.min))
    else:
        clause, values = _window_sql("issue_date", lo, hi)
        where.append(clause)
        params.extend(values)
    clauses, values = _job_attribute_sql(filters)
    where.extend(clauses)
    params.extend(values)
    if team_user_ids is not None:
        where.append(
            "(job_id IN (SELECT ju.job_id FROM jobdiva_job_users ju WHERE ju.user_id = ANY(%s)) OR job_id = ANY(%s))"
        )
        params.extend([list(team_user_ids), list(team_job_ids)])
    with conn.cursor() as cur:
        _set_timeout(cur, _STATEMENT_TIMEOUT_MS)
        cur.execute(f"""
            SELECT job_id, COALESCE(jobdiva_ref, ''), issue_date, COALESCE(job_status, ''), openings,
                   status_updated_at, jobdiva_updated_at
            FROM jobdiva_jobs
            WHERE {' AND '.join(where)}
        """, params)
        rows = cur.fetchall()
    return [
        PyramidReq(
            job_id=str(r[0]),
            ref=str(r[1] or ""),
            issue_at=r[2],
            status=normalize_status(r[3]),
            openings=_positive_int(r[4]) or 1,
            status_changed_at=r[5],
            updated_at=r[6],
        )
        for r in rows
    ]


def team_jobdiva_users(conn, emails: Iterable[str]) -> Dict[str, str]:
    """{email: JobDiva USERID} for the team emails the JobDiva directory knows."""
    wanted = sorted({e.strip().lower() for e in emails if e and e.strip()})
    if not wanted or not table_exists(conn, "jobdiva_users"):
        return {}
    with conn.cursor() as cur:
        _set_timeout(cur, _STATEMENT_TIMEOUT_MS)
        cur.execute(
            "SELECT LOWER(email), user_id FROM jobdiva_users WHERE LOWER(email) = ANY(%s) "
            "ORDER BY is_active DESC NULLS LAST",
            (wanted,),
        )
        out: Dict[str, str] = {}
        for email, user_id in cur.fetchall():
            out.setdefault(str(email), str(user_id))
    return out


# Team-scoped numbers that need to know who is tagged on each req (PAIR Volume
# for a team, Reqs Assigned) wait until this share of open reqs has been read.
JOB_USERS_READY_SHARE = 0.95


def jobdiva_coverage(conn) -> Dict[str, Any]:
    """What the JobDiva mirror holds, per feed, for the page's notes and for
    deciding which numbers are real.

    Each feed answers for itself: the jobs feed succeeding says nothing about
    activity, so an activity feed that has not run yet (deferred by a jobs
    backfill that spent the cycle's budget, or failing) makes the activity
    numbers unavailable instead of showing zeros.
    """
    empty = {
        "available": False,
        "activities_available": False,
        "jobs_available": False,
        "users_available": False,
        "job_users_ready": False,
    }
    if not table_exists(conn, "jobdiva_bi_sync_state"):
        return empty
    from services.jobdiva_bi_sync import sync_status

    status = sync_status(conn)

    def iso(value):
        return value.isoformat() if hasattr(value, "isoformat") else value

    def succeeded(feed: str, table: str) -> bool:
        return bool((status.get(feed) or {}).get("last_success_at")) and table_exists(conn, table)

    activities = status.get("activities") or {}
    jobs = status.get("jobs") or {}
    job_users_ready = False
    if succeeded("jobs", "jobdiva_jobs"):
        with conn.cursor() as cur:
            _set_timeout(cur, _STATEMENT_TIMEOUT_MS)
            cur.execute("""
                SELECT COUNT(*), COUNT(*) FILTER (WHERE users_synced_at IS NOT NULL)
                FROM jobdiva_jobs
                WHERE job_id ~ '^[0-9]+$' AND COALESCE(job_status, 'OPEN') IN ('OPEN', 'ON HOLD')
            """)
            open_reqs, read = cur.fetchone()
        job_users_ready = bool(open_reqs) and read / open_reqs >= JOB_USERS_READY_SHARE
    return {
        "available": any(s.get("last_success_at") for s in status.values()),
        "activities_available": succeeded("activities", "jobdiva_activities"),
        "activities_from": iso(activities.get("backfill_until")),
        "activities_complete": bool(activities.get("backfill_done")),
        "jobs_available": succeeded("jobs", "jobdiva_jobs"),
        "jobs_from": iso(jobs.get("backfill_until")),
        "jobs_complete": bool(jobs.get("backfill_done")),
        "users_available": succeeded("users", "jobdiva_users"),
        "job_users_ready": job_users_ready,
        "last_synced_at": iso(max(
            (s.get("last_success_at") for s in status.values() if s.get("last_success_at")),
            default=None,
        )),
        "last_error": next((s.get("last_error") for s in status.values() if s.get("last_error")), None),
    }


# ---------------------------------------------------------------------------
# Filters and scope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DashboardFilters:
    job: Optional[str] = None
    priority: Optional[str] = None
    client: Optional[str] = None
    jd_status: Optional[str] = None
    pair_status: Optional[str] = None
    vertical: Optional[str] = None

    def key(self) -> Tuple[Optional[str], ...]:
        return (self.job, self.priority, self.client, self.jd_status, self.pair_status, self.vertical)


def filter_reqs(
    reqs: Iterable[Req],
    filters: DashboardFilters,
    team_job_ids: Optional[Set[str]] = None,
) -> List[Req]:
    """Scoped reqs. team_job_ids is the team scope (routers._helpers
    _load_team_scope job_ids, by recruiter assignment); None = everyone."""
    def lower(value: Optional[str]) -> str:
        return (value or "").strip().lower()

    out = []
    for req in reqs:
        if team_job_ids is not None and not (set(req.job_ids) & team_job_ids):
            continue
        if filters.job and filters.job not in req.job_ids and lower(filters.job) != req.key:
            continue
        if filters.priority and lower(filters.priority) != lower(req.priority):
            continue
        if filters.client and lower(filters.client) != lower(req.customer):
            continue
        if filters.jd_status and normalize_status(filters.jd_status) != normalize_status(req.status):
            continue
        if filters.pair_status and lower(filters.pair_status) != lower(req.pair_status):
            continue
        if filters.vertical and lower(filters.vertical) != lower(req.division):
            continue
        out.append(req)
    return out


def filter_options(reqs: Sequence[Req], divisions: Iterable[str] = ()) -> Dict[str, Any]:
    def distinct(values: Iterable[str]) -> List[str]:
        seen: Dict[str, str] = {}
        for value in values:
            text = (value or "").strip()
            if text:
                seen.setdefault(text.lower(), text)
        return sorted(seen.values(), key=str.lower)

    return {
        "jobs": [
            {"job_id": r.job_ids[0] if r.job_ids else r.key, "ref": r.ref, "title": r.title, "customer": r.customer}
            for r in reqs[:2000]
        ],
        "priorities": distinct(r.priority for r in reqs),
        "clients": distinct(r.customer for r in reqs),
        "jd_statuses": distinct(r.status.title() for r in reqs if r.status),
        "pair_statuses": list(PAIR_STATUSES),
        "verticals": distinct(list(divisions) + [r.division for r in reqs]),
    }


def load_divisions(conn) -> List[str]:
    if not table_exists(conn, "jobdiva_jobs"):
        return []
    with conn.cursor() as cur:
        _set_timeout(cur, _STATEMENT_TIMEOUT_MS)
        cur.execute(
            "SELECT DISTINCT TRIM(division_name) FROM jobdiva_jobs "
            "WHERE NULLIF(TRIM(division_name), '') IS NOT NULL AND issue_date >= NOW() - INTERVAL '400 days'"
        )
        return [str(r[0]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Per-worker caches
# ---------------------------------------------------------------------------

class TtlCache:
    """A small per-process TTL cache. With 8 uvicorn workers a burst of page
    opens costs at most one computation per worker per key per TTL."""

    def __init__(self, ttl_seconds: float, max_entries: int = 64):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._data: Dict[Any, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            expires, value = hit
            if time.monotonic() >= expires:
                self._data.pop(key, None)
                return None
            return value

    def put(self, key: Any, value: Any, ttl: Optional[float] = None) -> None:
        with self._lock:
            now = time.monotonic()
            for stale in [k for k, (exp, _) in self._data.items() if now >= exp]:
                del self._data[stale]
            while len(self._data) >= self.max_entries:
                del self._data[min(self._data, key=lambda k: self._data[k][0])]
            self._data[key] = (now + (ttl if ttl is not None else self.ttl), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def get_or_compute(self, key: Any, compute: Callable[[], Any], refresh: bool = False) -> Any:
        if not refresh:
            hit = self.get(key)
            if hit is not None:
                return hit
        value = compute()
        self.put(key, value)
        return value
