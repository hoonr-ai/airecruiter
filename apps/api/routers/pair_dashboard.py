"""PAIR Dashboard API: Overview, Funnel & Speed, Productivity.

  GET /api/v1/admin/dashboard/options       filter values + JobDiva sync coverage
  GET /api/v1/admin/dashboard/overview      activity-based KPIs for a period
  GET /api/v1/admin/dashboard/funnel        cohort funnel, speed, rejections, pending feedback
  GET /api/v1/admin/dashboard/productivity  weekly per-recruiter averages for a team (RM)

Definitions live in services/pair_dashboard.py; JobDiva facts come from the
mirror services/jobdiva_bi_sync.py keeps.

Access is the other admin reports' rule (routers.recruiter_analytics
._resolve_scope_team_id): admins see everything or one team (?team_id), team
leads (Recruiting Managers) are pinned to their own team, everyone else is 403.
"""

import asyncio
import datetime
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from core.auth import UserIdentity, get_current_user
from routers._helpers import _load_team_scope, get_db_connection
from routers.recruiter_analytics import _resolve_scope_team_id
from services import pair_dashboard as dash

router = APIRouter(prefix="/api/v1", tags=["PAIR Dashboard"])
logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Whole responses, per worker. ?refresh=true skips it.
_RESPONSE_CACHE = dash.TtlCache(ttl_seconds=60.0, max_entries=128)
# The req list and every launched person's state, shared by all tabs, scopes
# and filters: the one read that scans candidate blobs.
_CONTEXT_CACHE = dash.TtlCache(ttl_seconds=120.0, max_entries=2)


def clear_dashboard_caches() -> None:
    _RESPONSE_CACHE.clear()
    _CONTEXT_CACHE.clear()


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------

def _parse_day(raw: Optional[str], name: str) -> Optional[datetime.date]:
    text = (raw or "").strip()
    if not text:
        return None
    if not _DATE_RE.match(text):
        raise HTTPException(status_code=400, detail=f"Invalid {name}: expected YYYY-MM-DD.")
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}: expected YYYY-MM-DD.")


def _parse_range(
    start_raw: Optional[str], end_raw: Optional[str], *, names=("start_date", "end_date")
) -> Optional[Tuple[datetime.date, datetime.date]]:
    start = _parse_day(start_raw, names[0])
    end = _parse_day(end_raw, names[1])
    if start is None and end is None:
        return None
    if start is None or end is None:
        raise HTTPException(status_code=400, detail=f"Both {names[0]} and {names[1]} are required for a range.")
    if start > end:
        raise HTTPException(status_code=400, detail=f"{names[0]} must not be after {names[1]}.")
    if (end - start).days + 1 > dash.MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Date range cannot exceed {dash.MAX_RANGE_DAYS} days.")
    return start, end


def _filters(job, priority, client, jd_status, pair_status, vertical) -> dash.DashboardFilters:
    def clean(value: Optional[str]) -> Optional[str]:
        text = (value or "").strip()
        return text[:200] or None

    return dash.DashboardFilters(
        job=clean(job),
        priority=clean(priority),
        client=clean(client),
        jd_status=clean(jd_status),
        pair_status=clean(pair_status),
        vertical=clean(vertical),
    )


def _today() -> datetime.date:
    return datetime.datetime.now(dash.REPORT_TIMEZONE).date()


def _iso(day: Optional[datetime.date]) -> Optional[str]:
    return day.isoformat() if day else None


# ---------------------------------------------------------------------------
# Shared context
# ---------------------------------------------------------------------------

def _load_context(conn, refresh: bool) -> Tuple[List[dash.Req], List[dash.Person]]:
    def compute():
        reqs = dash.load_reqs(conn)
        people = dash.load_people(conn, reqs)
        first = dash.first_launch_by_req(people)
        for req in reqs:
            req.first_launch_at = first.get(req.key)
        return reqs, people

    return _CONTEXT_CACHE.get_or_compute("all", compute, refresh=refresh)


def _team_scope(conn, scope_team_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not scope_team_id:
        return None
    try:
        return _load_team_scope(conn, scope_team_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def _scope_payload(scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if scope is None:
        return None
    return {"team_id": scope["team_id"], "team_name": scope["team_name"], "member_count": len(scope["emails"])}


def _scoped_reqs(reqs, filters, scope) -> List[dash.Req]:
    team_job_ids = set(scope["job_ids"]) if scope is not None else None
    return dash.filter_reqs(reqs, filters, team_job_ids)


def _passed_jobdiva_ids(people, scoped_keys) -> List[str]:
    ids = set()
    for person in people:
        if person.req in scoped_keys and person.outcome == "Pass":
            ids.update(person.jobdiva_ids)
    return sorted(ids)


def _mirror_state(conn) -> Dict[str, Any]:
    return dash.jobdiva_coverage(conn)


def _coverage_day(value: Any) -> Optional[datetime.date]:
    """The first WHOLE day the mirror covers (its backfill stops mid-day)."""
    if not value:
        return None
    try:
        moment = datetime.datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return None
    day = moment.date()
    return day if moment.time() == datetime.time.min else day + datetime.timedelta(days=1)


def _team_user_ids(conn, scope: Optional[Dict[str, Any]]) -> Tuple[Optional[List[str]], List[str]]:
    """(JobDiva user ids of the team's emails, emails JobDiva doesn't know).
    (None, []) when unscoped."""
    if scope is None:
        return None, []
    mapping = dash.team_jobdiva_users(conn, scope["emails"])
    missing = sorted(e for e in scope["emails"] if e not in mapping)
    return sorted(set(mapping.values())), missing


# ---------------------------------------------------------------------------
# Computations (run in a thread)
# ---------------------------------------------------------------------------

def _options_sync(scope_team_id: Optional[str], refresh: bool, teams: List[Dict[str, Any]]) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        scope = _team_scope(conn, scope_team_id)
        reqs, _ = _load_context(conn, refresh)
        scoped = _scoped_reqs(reqs, dash.DashboardFilters(), scope)
        options = dash.filter_options(scoped, dash.load_divisions(conn) if scope is None else ())
        return {
            **options,
            "teams": teams,
            "team_scope": _scope_payload(scope),
            "jobdiva": _mirror_state(conn),
        }
    finally:
        conn.close()


def _overview_sync(scope_team_id, date_range, filters, refresh) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        scope = _team_scope(conn, scope_team_id)
        reqs_all, people_all = _load_context(conn, refresh)
        reqs = _scoped_reqs(reqs_all, filters, scope)
        scoped_keys = {r.key for r in reqs}
        period = dash.make_period(*(date_range or (None, None)), today=_today())
        mirror = _mirror_state(conn)
        attributor = dash.Attributor(reqs_all, people_all)

        activities: List[dash.Activity] = []
        if mirror["activities_available"] and reqs:
            activities = dash.load_activities(
                conn,
                lo=period.window_start,
                hi=period.end,
                job_ids=sorted({j for r in reqs for j in r.jobdiva_job_ids}),
                refs=sorted(scoped_keys),
                candidate_ids=_passed_jobdiva_ids(people_all, scoped_keys),
            )

        pyramid = None
        volume_unavailable = None
        warnings: List[str] = []
        if not mirror["jobs_available"]:
            volume_unavailable = "Waiting for the JobDiva requirements sync (all Pyramid reqs)."
        elif filters.job or filters.pair_status:
            # Filtering to one job / a PAIR status narrows the denominator to
            # the PAIR reqs themselves (non-PAIR reqs have neither).
            pyramid = []
        elif scope is not None and not (mirror["users_available"] and mirror["job_users_ready"]):
            # A team's total is the reqs its people are tagged on in JobDiva;
            # until those tags are read it would be just its PAIR reqs (~100%).
            volume_unavailable = "Waiting for the JobDiva sync of who is tagged on each requirement."
        else:
            team_user_ids, missing = _team_user_ids(conn, scope)
            if missing:
                warnings.append(
                    f"{len(missing)} of {len(scope['emails'])} team emails are not in the JobDiva user "
                    "directory, so reqs only they are tagged on are not counted in PAIR Volume."
                )
            pyramid = dash.load_pyramid_reqs(
                conn,
                lo=period.window_start,
                hi=period.end,
                filters=filters,
                team_user_ids=team_user_ids,
                team_job_ids=sorted({j for r in reqs for j in r.jobdiva_job_ids}),
            )

        metrics = dash.compute_overview(
            period=period,
            reqs=reqs,
            all_reqs=reqs_all,
            people=[p for p in people_all if p.req in scoped_keys],
            activities=activities,
            attributor=attributor,
            pyramid_reqs=pyramid,
            volume_unavailable=volume_unavailable,
            activities_available=mirror["activities_available"],
            activities_from=_coverage_day(mirror.get("activities_from")),
            jobs_from=_coverage_day(mirror.get("jobs_from")),
        )
        return {
            "range": {"start": _iso(period.start), "end": _iso(period.end)} if period.start else None,
            "previous_range": (
                {"start": _iso(period.previous[0]), "end": _iso(period.previous[1])} if period.previous else None
            ),
            "weeks": [w.isoformat() for w in period.weeks],
            "metrics": metrics,
            "reqs_in_scope": len(reqs),
            "team_scope": _scope_payload(scope),
            "jobdiva": mirror,
            "warnings": warnings,
        }
    finally:
        conn.close()


def _funnel_sync(scope_team_id, posted_range, filters, refresh) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        scope = _team_scope(conn, scope_team_id)
        reqs_all, people_all = _load_context(conn, refresh)
        today = _today()
        posted_from, posted_to = posted_range or (datetime.date(today.year, 1, 1), today)
        cohort = []
        for req in _scoped_reqs(reqs_all, filters, scope):
            if not req.launched:
                continue
            posted = dash.et_date(req.issue_at) or dash.et_date(req.added_at)
            if posted is not None and posted_from <= posted <= posted_to:
                cohort.append(req)
        cohort_keys = {r.key for r in cohort}
        mirror = _mirror_state(conn)
        activities: List[dash.Activity] = []
        if mirror["activities_available"] and cohort:
            activities = dash.load_activities(
                conn,
                lo=None,
                hi=today + datetime.timedelta(days=366),  # future-dated interviews / starts too
                job_ids=sorted({j for r in cohort for j in r.jobdiva_job_ids}),
                refs=sorted(cohort_keys),
                candidate_ids=_passed_jobdiva_ids(people_all, cohort_keys),
            )
        funnel = dash.compute_funnel(
            cohort=cohort,
            people=[p for p in people_all if p.req in cohort_keys],
            activities=activities,
            attributor=dash.Attributor(reqs_all, people_all),
            activities_available=mirror["activities_available"],
            now=datetime.datetime.now(dash.UTC),
        )
        funnel["cohort"].update({"posted_from": _iso(posted_from), "posted_to": _iso(posted_to)})
        return {**funnel, "team_scope": _scope_payload(scope), "jobdiva": mirror}
    finally:
        conn.close()


def _productivity_sync(scope_team_id, date_range, filters, refresh, teams) -> Dict[str, Any]:
    conn = get_db_connection()
    try:
        scope = _team_scope(conn, scope_team_id)
        reqs_all, people_all = _load_context(conn, refresh)
        period = dash.make_period(*(date_range or (None, None)), today=_today())
        mirror = _mirror_state(conn)

        chosen = [t for t in teams if scope is None or t["id"] == scope["team_id"]]
        recruiters, everyone = set(), set()
        for team in chosen:
            leads = {e.strip().lower() for e in team.get("lead_emails") or [] if e}
            members = {e.strip().lower() for e in team.get("member_emails") or [] if e}
            # Recruiters are the team's members; the RM (lead) is tagged on the
            # team's reqs but is not one of the people the work is averaged over.
            recruiters |= members or leads
            everyone |= leads | members
        base = {
            "team_scope": _scope_payload(scope),
            "teams_configured": len(teams),
            "jobdiva": mirror,
            "range": {"start": _iso(period.start), "end": _iso(period.end)} if period.start else None,
        }
        if not everyone:
            return {**base, "unavailable": "No Teams are set up yet. Add each Recruiting Manager and their "
                                           "recruiters on the Teams page to see productivity."}
        if not (mirror["jobs_available"] and mirror["activities_available"] and mirror["users_available"]):
            return {**base, "unavailable": "Waiting for the JobDiva sync (requirements, submittals and the "
                                           "user directory that maps team emails to JobDiva users)."}

        mapping = dash.team_jobdiva_users(conn, everyone)
        recruiter_ids = sorted({mapping[e] for e in recruiters if e in mapping})
        tag_ids = sorted(set(mapping.values()))
        # The team's own PAIR reqs count as assigned even where JobDiva has not
        # tagged them: the same recruiter-assignment rule as every team scope
        # (routers._helpers._load_team_scope), over all the chosen teams.
        if scope is not None:
            team_reqs = _scoped_reqs(reqs_all, dash.DashboardFilters(), scope)
        else:
            team_reqs = [r for r in reqs_all if r.recruiter_emails & everyone]
        team_pair_jobs = sorted({j for r in team_reqs for j in r.jobdiva_job_ids})
        coverage_start = _coverage_day(mirror.get("activities_from"))
        job_filters = dash.DashboardFilters(priority=filters.priority, client=filters.client, vertical=filters.vertical)
        assigned = dash.load_pyramid_reqs(
            conn,
            lo=period.window_start or coverage_start,
            hi=period.end,
            filters=job_filters,
            team_user_ids=tag_ids,
            team_job_ids=team_pair_jobs,
            active_during=True,
        )
        activities = dash.load_activities(
            conn, lo=period.window_start or coverage_start, hi=period.end, user_ids=recruiter_ids,
            job_filters=job_filters,
        ) if recruiter_ids else []
        result = dash.compute_productivity(
            period=period,
            recruiter_count=len(recruiters),
            assigned_reqs=assigned,
            activities=activities,
            attributor=dash.Attributor(reqs_all, people_all),
            coverage_start=coverage_start,
            reqs_unavailable=(
                None if mirror["job_users_ready"]
                else "Waiting for the JobDiva sync of who is tagged on each requirement."
            ),
        )
        unmapped = sorted(e for e in everyone if e not in mapping)
        return {**base, **result, "unmapped_emails": unmapped}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def _teams_for(user: UserIdentity, scope_team_id: Optional[str]) -> List[Dict[str, Any]]:
    from services import teams_db

    try:
        teams = await asyncio.to_thread(teams_db.list_teams)
    except Exception as exc:  # noqa: BLE001 - teams are optional context
        logger.warning(f"PAIR dashboard: team list unavailable: {exc}")
        return []
    if not user.is_admin:
        teams = [t for t in teams if t.get("id") == scope_team_id]
    return [
        {
            "id": t.get("id"),
            "name": t.get("name"),
            "lead_emails": [e.lower() for e in t.get("lead_emails") or []],
            "member_emails": [e.lower() for e in t.get("member_emails") or []],
        }
        for t in teams
    ]


async def _run(cache_key, compute, refresh: bool):
    if not refresh:
        hit = _RESPONSE_CACHE.get(cache_key)
        if hit is not None:
            return {"status": "success", "data": hit}
    try:
        data = await asyncio.to_thread(compute)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"PAIR dashboard {cache_key[0]} failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Dashboard unavailable: {exc}")
    _RESPONSE_CACHE.put(cache_key, data)
    return {"status": "success", "data": data}


@router.get("/admin/dashboard/options")
async def get_dashboard_options(
    team_id: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
):
    scope_team_id = _resolve_scope_team_id(user, team_id)
    teams = await _teams_for(user, scope_team_id)
    return await _run(
        ("options", scope_team_id, user.is_admin),
        lambda: _options_sync(scope_team_id, refresh, teams),
        refresh,
    )


@router.get("/admin/dashboard/overview")
async def get_dashboard_overview(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    team_id: Optional[str] = Query(default=None),
    job: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    client: Optional[str] = Query(default=None),
    jd_status: Optional[str] = Query(default=None),
    pair_status: Optional[str] = Query(default=None),
    vertical: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
):
    scope_team_id = _resolve_scope_team_id(user, team_id)
    date_range = _parse_range(start_date, end_date)
    filters = _filters(job, priority, client, jd_status, pair_status, vertical)
    return await _run(
        ("overview", scope_team_id, date_range, filters.key()),
        lambda: _overview_sync(scope_team_id, date_range, filters, refresh),
        refresh,
    )


@router.get("/admin/dashboard/funnel")
async def get_dashboard_funnel(
    posted_from: Optional[str] = Query(default=None),
    posted_to: Optional[str] = Query(default=None),
    team_id: Optional[str] = Query(default=None),
    job: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    client: Optional[str] = Query(default=None),
    jd_status: Optional[str] = Query(default=None),
    pair_status: Optional[str] = Query(default=None),
    vertical: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
):
    scope_team_id = _resolve_scope_team_id(user, team_id)
    posted_range = _parse_range(posted_from, posted_to, names=("posted_from", "posted_to"))
    filters = _filters(job, priority, client, jd_status, pair_status, vertical)
    return await _run(
        ("funnel", scope_team_id, posted_range, filters.key()),
        lambda: _funnel_sync(scope_team_id, posted_range, filters, refresh),
        refresh,
    )


@router.get("/admin/dashboard/productivity")
async def get_dashboard_productivity(
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    team_id: Optional[str] = Query(default=None),
    priority: Optional[str] = Query(default=None),
    client: Optional[str] = Query(default=None),
    vertical: Optional[str] = Query(default=None),
    refresh: bool = Query(default=False),
    user: UserIdentity = Depends(get_current_user),
):
    scope_team_id = _resolve_scope_team_id(user, team_id)
    date_range = _parse_range(start_date, end_date)
    filters = _filters(None, priority, client, None, None, vertical)
    teams = await _teams_for(user, scope_team_id)
    return await _run(
        ("productivity", scope_team_id, date_range, filters.key()),
        lambda: _productivity_sync(scope_team_id, date_range, filters, refresh, teams),
        refresh,
    )
