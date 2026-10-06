"""Who can see what, once the org hierarchy exists.

The hierarchy is only worth anything if it is enforced where data is read, so
this pins the places that enforce it:

  * identity resolution (core.auth.resolve_user_identity) — who counts as a manager;
  * the ONE scope definition (get_user_scope_emails) the jobs list and
    verify_job_access use, and the report scope keys built from the same sources;
  * routers._helpers._load_team_scope, which turns a scope key into the emails
    and jobs a report covers;
  * the admin-only org-hierarchy routes and their preview-first import.

Every lookup in these tests is stubbed — no database.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import core.auth as auth
from core.auth import UserIdentity
from routers import _helpers
from routers import org_hierarchy as org_router
from services import org_hierarchy as oh
from services import teams_db
from services.org_hierarchy import Member


def _m(id, name, role, parent=None, email="auto"):
    return Member(
        id=id, name=name, role=role, reports_to_id=parent,
        email=(f"{name.lower().replace(' ', '.')}@x.com" if email == "auto" else email),
    )


#   avp(1) ─ dd(2) ─┬─ dm(3) ─ rm1(4) ─┬─ rec1(6)
#                   │                  └─ rec2(7)
#                   └─ rm2(5) ─ rec3(8)
ORG = [
    _m(1, "Vera Avp", "avp"),
    _m(2, "Dan Dd", "delivery_director", 1),
    _m(3, "Dee Dm", "delivery_manager", 2),
    _m(4, "Rita Rm", "resource_manager", 3),
    _m(5, "Rob Rm", "resource_manager", 2),
    _m(6, "Ann Rec", "recruiter", 4),
    _m(7, "Bob Rec", "recruiter", 4),
    _m(8, "Cat Rec", "recruiter", 5),
]
ANN, BOB, CAT = "ann.rec@x.com", "bob.rec@x.com", "cat.rec@x.com"
RITA, ROB, DAN, VERA = "rita.rm@x.com", "rob.rm@x.com", "dan.dd@x.com", "vera.avp@x.com"


@pytest.fixture(autouse=True)
def _org_in_memory(monkeypatch):
    """The org tree lives in memory; there are no teams unless a test adds one."""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.setattr(oh, "list_members", lambda: list(ORG))
    monkeypatch.setattr(teams_db, "get_team_for_email", lambda email: None)
    monkeypatch.setattr(teams_db, "get_team_member_emails", lambda team_id: [])


def _membership(email):
    member = next((m for m in ORG if m.email == (email or "").strip().lower()), None)
    if member is None:
        return None
    return {
        "id": member.id, "name": member.name, "role": member.role,
        "has_reports": any(c.reports_to_id == member.id for c in ORG),
    }


@pytest.fixture()
def org_lookup(monkeypatch):
    monkeypatch.setattr(oh, "get_membership", _membership)


def _identity(email, base_role="recruiter", monkeypatch=None):
    return auth.resolve_user_identity(email)


@pytest.fixture(autouse=True)
def _base_role(monkeypatch):
    """ADMIN_EMAILS / user_roles are not what is under test: everyone is a recruiter
    unless the test adds the email to `admins`."""
    admins = set()
    monkeypatch.setattr(auth, "get_user_role", lambda email: "admin" if (email or "").lower() in admins else "recruiter")
    return admins


# ---------------------------------------------------------------------------
# Identity: who counts as a manager
# ---------------------------------------------------------------------------

def test_a_person_with_reports_resolves_to_team_lead_with_their_level(org_lookup):
    user = auth.resolve_user_identity(RITA)
    assert user.role == "team_lead" and user.is_team_lead
    assert (user.org_member_id, user.org_role, user.org_manages_people) == (4, "resource_manager", True)
    assert user.manages_org


@pytest.mark.parametrize(
    "email,level",
    [(RITA, "resource_manager"), ("dee.dm@x.com", "delivery_manager"), (DAN, "delivery_director"), (VERA, "avp")],
)
def test_every_management_level_gets_the_manager_views(org_lookup, email, level):
    user = auth.resolve_user_identity(email)
    assert user.is_team_lead and user.org_role == level


def test_a_recruiter_with_nobody_beneath_stays_a_recruiter(org_lookup):
    user = auth.resolve_user_identity(ANN)
    assert user.role == "recruiter" and not user.is_team_lead
    assert user.org_member_id == 6 and user.org_manages_people is False and not user.manages_org


def test_someone_not_in_the_hierarchy_is_unchanged(org_lookup):
    user = auth.resolve_user_identity("stranger@x.com")
    assert user.role == "recruiter" and user.org_member_id is None and not user.manages_org


def test_an_admin_who_manages_people_stays_admin(org_lookup, _base_role):
    _base_role.add(DAN)
    user = auth.resolve_user_identity(DAN)
    assert user.role == "admin" and user.is_admin and not user.is_team_lead
    assert user.manages_org  # still labelled by level


def test_a_failed_hierarchy_lookup_grants_nothing(monkeypatch):
    def boom(_email):
        raise RuntimeError("org_members does not exist yet")

    monkeypatch.setattr(oh, "get_membership", boom)
    user = auth.resolve_user_identity(RITA)
    assert user.role == "recruiter" and user.org_member_id is None and not user.manages_org


def test_a_failed_hierarchy_lookup_keeps_a_legacy_team_lead_their_team(monkeypatch):
    monkeypatch.setattr(oh, "get_membership", lambda _e: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(
        teams_db, "get_team_for_email",
        lambda e: {"team_id": "t-1", "team_name": "East", "member_role": "lead"},
    )
    user = auth.resolve_user_identity(RITA)
    assert user.role == "team_lead" and user.leads_team and user.team_id == "t-1"


def test_a_legacy_team_lead_outside_the_hierarchy_is_still_a_team_lead(monkeypatch):
    monkeypatch.setattr(
        teams_db, "get_team_for_email",
        lambda e: {"team_id": "t-1", "team_name": "East", "member_role": "lead"},
    )
    user = auth.resolve_user_identity("legacy.lead@x.com")
    assert user.role == "team_lead" and user.leads_team and not user.manages_org


# ---------------------------------------------------------------------------
# leads_team: belonging to a team is not leading it
# ---------------------------------------------------------------------------

def test_a_manager_who_is_only_a_team_member_does_not_lead_that_team(org_lookup, monkeypatch):
    monkeypatch.setattr(
        teams_db, "get_team_for_email",
        lambda e: {"team_id": "t-9", "team_name": "Other", "member_role": "member"},
    )

    # The roster must be a real answer, not an exception: get_user_scope_emails
    # swallows lookup errors, so a raising stub could never show a leak.
    monkeypatch.setattr(teams_db, "get_team_member_emails", lambda team_id: ["teammate@x.com", RITA])

    user = auth.resolve_user_identity(RITA)
    assert user.is_team_lead and user.team_id == "t-9"
    assert user.leads_team is False
    assert auth.report_scope_parts(user) == ["org:4"]  # not the team
    scope = auth.get_user_scope_emails(user)
    assert "teammate@x.com" not in scope
    assert scope == {RITA, ANN, BOB}


def test_the_teams_list_is_not_handed_to_a_manager_who_is_only_a_team_member(monkeypatch):
    from routers import teams as teams_router

    monkeypatch.setattr(
        teams_db, "get_team",
        lambda tid: {"id": tid, "name": "Other", "lead_emails": ["boss@x.com"], "member_emails": ["m@x.com"]},
    )
    member_manager = UserIdentity(
        email=RITA, role="team_lead", team_id="t-9", team_member_role="member",
        org_member_id=4, org_role="resource_manager", org_manages_people=True,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(teams_router.list_teams(user=member_manager))
    assert exc.value.status_code == 403

    # The lead of that team still gets it.
    lead = UserIdentity(email="boss@x.com", role="team_lead", team_id="t-9", team_member_role="lead")
    res = asyncio.run(teams_router.list_teams(user=lead))
    assert [t["id"] for t in res["data"]["teams"]] == ["t-9"]


def test_leads_team_falls_back_to_the_old_rule_for_hand_built_identities():
    assert UserIdentity(email="a@x.com", role="team_lead", team_id="t").leads_team is True
    assert UserIdentity(email="a@x.com", role="recruiter", team_id="t").leads_team is False
    assert UserIdentity(email="a@x.com", role="team_lead").leads_team is False
    # An explicit member role always wins.
    assert UserIdentity(email="a@x.com", role="team_lead", team_id="t", team_member_role="member").leads_team is False
    assert UserIdentity(email="a@x.com", role="team_lead", team_id="t", team_member_role="lead").leads_team is True


# ---------------------------------------------------------------------------
# The one scope definition
# ---------------------------------------------------------------------------

def test_each_level_sees_itself_and_everyone_beneath(org_lookup):
    assert auth.get_user_scope_emails(auth.resolve_user_identity(VERA)) == {e.email for e in ORG}
    assert auth.get_user_scope_emails(auth.resolve_user_identity(DAN)) == {e.email for e in ORG} - {VERA}
    assert auth.get_user_scope_emails(auth.resolve_user_identity(RITA)) == {RITA, ANN, BOB}
    assert auth.get_user_scope_emails(auth.resolve_user_identity(ANN)) == {ANN}


def test_nobody_sees_sideways(org_lookup):
    rita = auth.get_user_scope_emails(auth.resolve_user_identity(RITA))
    assert ROB not in rita and CAT not in rita


def test_a_manager_who_also_leads_a_team_sees_the_union(org_lookup, monkeypatch):
    monkeypatch.setattr(
        teams_db, "get_team_for_email",
        lambda e: {"team_id": "t-1", "team_name": "East", "member_role": "lead"},
    )
    monkeypatch.setattr(teams_db, "get_team_member_emails", lambda team_id: [RITA, "teammate@x.com"])
    user = auth.resolve_user_identity(RITA)
    assert auth.get_user_scope_emails(user) == {RITA, ANN, BOB, "teammate@x.com"}
    assert auth.report_scope_parts(user) == ["org:4", "t-1"]


def test_if_the_hierarchy_cannot_be_read_the_scope_shrinks_to_self_and_team(org_lookup, monkeypatch):
    user = auth.resolve_user_identity(RITA)
    monkeypatch.setattr(oh, "scope_emails", lambda _id: (_ for _ in ()).throw(RuntimeError("db down")))
    assert auth.get_user_scope_emails(user) == {RITA}


# ---------------------------------------------------------------------------
# verify_job_access — the check behind every job-scoped endpoint
# ---------------------------------------------------------------------------

def _job(*emails):
    return {"recruiter_emails": list(emails)}


def test_a_manager_may_open_a_job_assigned_to_anyone_beneath_them(org_lookup):
    rita = auth.resolve_user_identity(RITA)
    auth.verify_job_access(_job(ANN), rita)
    auth.verify_job_access(_job(BOB, "outsider@x.com"), rita)  # shared with an outsider is still theirs
    dan = auth.resolve_user_identity(DAN)
    auth.verify_job_access(_job(ANN), dan)  # two levels down
    auth.verify_job_access(_job(CAT), dan)


def test_a_manager_may_not_open_a_sideways_or_upward_job(org_lookup):
    rita = auth.resolve_user_identity(RITA)
    for assigned in (CAT, ROB, DAN):
        with pytest.raises(HTTPException) as exc:
            auth.verify_job_access(_job(assigned), rita)
        assert exc.value.status_code == 403


def test_a_recruiter_still_only_opens_their_own_jobs(org_lookup):
    ann = auth.resolve_user_identity(ANN)
    auth.verify_job_access(_job(ANN), ann)
    with pytest.raises(HTTPException):
        auth.verify_job_access(_job(BOB), ann)


def test_unassigned_jobs_stay_open_to_claim(org_lookup):
    auth.verify_job_access(_job(), auth.resolve_user_identity(ANN))


def test_admins_still_bypass(org_lookup, _base_role):
    _base_role.add("boss@x.com")
    auth.verify_job_access(_job("anyone@x.com"), auth.resolve_user_identity("boss@x.com"))


# ---------------------------------------------------------------------------
# The jobs list
# ---------------------------------------------------------------------------

def test_the_jobs_list_for_a_manager_is_their_organisations_jobs(org_lookup):
    from routers.jobs import _filter_jobs_for_user

    payload = {
        "jobs": {
            "mine": {"recruiter_emails": [RITA]},
            "ann": {"recruiter_emails": [ANN]},
            "bob-shared": {"recruiter_emails": [BOB, "outsider@x.com"]},
            "sibling": {"recruiter_emails": [CAT]},
            "json-string": {"recruiter_emails": json.dumps([BOB])},
            "unassigned": {"recruiter_emails": []},
        },
        "total_count": 6,
    }
    shown = _filter_jobs_for_user(payload, auth.resolve_user_identity(RITA))
    assert set(shown["jobs"]) == {"mine", "ann", "bob-shared", "json-string"}
    assert shown["total_count"] == 4

    everything = _filter_jobs_for_user(payload, auth.resolve_user_identity(VERA))
    assert set(everything["jobs"]) == {"mine", "ann", "bob-shared", "sibling", "json-string"}


# ---------------------------------------------------------------------------
# resolve_report_scope — the key the admin reports are computed for
# ---------------------------------------------------------------------------

def test_admins_get_whatever_they_ask_for():
    admin = UserIdentity(email="a@x.com", role="admin")
    assert auth.resolve_report_scope(admin, None, "analytics") is None
    assert auth.resolve_report_scope(admin, "  t-1  ", "analytics") == "t-1"
    assert auth.resolve_report_scope(admin, "org:4", "analytics") == "org:4"
    assert auth.resolve_report_scope(admin, "   ", "analytics") is None


def test_a_manager_is_pinned_to_their_organisation_and_the_request_is_ignored(org_lookup):
    rita = auth.resolve_user_identity(RITA)
    assert auth.resolve_report_scope(rita, None, "analytics") == "org:4"
    # Asking for someone else's organisation, or an admin-only everything view, changes nothing.
    assert auth.resolve_report_scope(rita, "org:2", "analytics") == "org:4"
    assert auth.resolve_report_scope(rita, "org:1+t-other", "analytics") == "org:4"


def test_a_manager_who_also_leads_a_team_gets_both_in_one_key(org_lookup, monkeypatch):
    monkeypatch.setattr(
        teams_db, "get_team_for_email",
        lambda e: {"team_id": "t-1", "team_name": "East", "member_role": "lead"},
    )
    assert auth.resolve_report_scope(auth.resolve_user_identity(RITA), None, "analytics") == "org:4+t-1"


def test_a_legacy_team_lead_is_pinned_to_their_team_exactly_as_before():
    lead = UserIdentity(email="l@x.com", role="team_lead", team_id="t-1", team_name="East")
    assert auth.resolve_report_scope(lead, "someone-elses", "analytics") == "t-1"


@pytest.mark.parametrize(
    "user",
    [
        UserIdentity(email="r@x.com", role="recruiter"),
        UserIdentity(email="r@x.com", role="recruiter", team_id="t-1"),  # a member, not a lead
        UserIdentity(email="l@x.com", role="team_lead"),  # a lead of nothing
        UserIdentity(email="l@x.com", role="team_lead", team_id="t-1", team_member_role="member"),
        UserIdentity(email="a@x.com", role="recruiter", org_member_id=6, org_role="recruiter"),  # leaf
    ],
    ids=["recruiter", "team-member", "lead-of-nothing", "member-promoted-by-nothing", "leaf-recruiter"],
)
def test_everyone_else_is_refused_with_the_reports_own_wording(user):
    with pytest.raises(HTTPException) as exc:
        auth.resolve_report_scope(user, None, "the launch report")
    assert exc.value.status_code == 403
    assert exc.value.detail == "Access denied. Admin or team lead access required to view the launch report."


def test_the_three_reports_keep_their_exact_403_wording():
    nobody = UserIdentity(email="r@x.com", role="recruiter")
    from routers import recruiter_analytics as ra

    with pytest.raises(HTTPException) as exc:
        ra._resolve_scope_team_id(nobody, None)
    assert exc.value.detail == "Access denied. Admin or team lead access required to view recruiter analytics."


# ---------------------------------------------------------------------------
# _load_team_scope — a scope key into emails and jobs
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)


JOB_ROWS = [
    ("j-ann", "JD-1", json.dumps([ANN])),
    ("j-bob", "JD-2", json.dumps([BOB, "outsider@x.com"])),
    ("j-cat", "JD-3", json.dumps([CAT])),
    ("j-rita", None, json.dumps([RITA])),
    ("j-none", "JD-9", json.dumps([])),
]


def test_an_org_scope_covers_the_organisations_people_and_jobs():
    scope = _helpers._load_team_scope(_Conn(JOB_ROWS), "org:4")
    assert scope["team_id"] == "org:4"
    assert scope["team_name"] == "Rita Rm (Resource Manager)"
    assert scope["emails"] == [ANN, BOB, RITA]
    assert scope["lead_emails"] == [RITA] and scope["member_emails"] == [ANN, BOB]
    assert scope["job_ids"] == ["j-ann", "j-bob", "j-rita"]
    # Candidate tables are keyed by either the job uuid or the JobDiva ref.
    assert scope["sc_keys"] == sorted({"j-ann", "j-bob", "j-rita", "JD-1", "JD-2"})
    assert "j-cat" not in scope["job_ids"]


def test_a_higher_level_scope_includes_every_level_below():
    scope = _helpers._load_team_scope(_Conn(JOB_ROWS), "org:2")
    assert set(scope["job_ids"]) == {"j-ann", "j-bob", "j-cat", "j-rita"}


def test_a_union_scope_merges_an_organisation_with_a_team(monkeypatch):
    monkeypatch.setattr(
        teams_db, "get_team",
        lambda tid: {"id": "t-1", "name": "East", "lead_emails": [RITA], "member_emails": [CAT]} if tid == "t-1" else None,
    )
    scope = _helpers._load_team_scope(_Conn(JOB_ROWS), "org:4+t-1")
    assert scope["team_id"] == "org:4+t-1"
    assert scope["team_name"] == "Rita Rm (Resource Manager) + East"
    assert set(scope["emails"]) == {RITA, ANN, BOB, CAT}
    assert "j-cat" in scope["job_ids"]


def test_a_repeated_part_is_not_double_counted():
    one = _helpers._load_team_scope(_Conn(JOB_ROWS), "org:4")
    twice = _helpers._load_team_scope(_Conn(JOB_ROWS), "org:4+org:4")
    assert twice["emails"] == one["emails"] and twice["team_id"] == "org:4"


def test_a_plain_team_scope_is_unchanged(monkeypatch):
    monkeypatch.setattr(
        teams_db, "get_team",
        lambda tid: {"id": "t-1", "name": "East", "lead_emails": ["Lead@X.com "], "member_emails": [ANN]},
    )
    scope = _helpers._load_team_scope(_Conn(JOB_ROWS), "t-1")
    assert scope["team_id"] == "t-1" and scope["team_name"] == "East"
    assert scope["emails"] == sorted(["lead@x.com", ANN])
    assert scope["lead_emails"] == ["lead@x.com"] and scope["member_emails"] == [ANN]
    assert scope["job_ids"] == ["j-ann"]


@pytest.mark.parametrize("key", ["org:999", "org:abc", "org:", "", "+", "t-unknown", "org:4+t-unknown"])
def test_an_unknown_scope_is_a_lookup_error_never_an_empty_dashboard(monkeypatch, key):
    monkeypatch.setattr(teams_db, "get_team", lambda tid: None)
    with pytest.raises(LookupError):
        _helpers._load_team_scope(_Conn(JOB_ROWS), key)


def test_the_jobs_list_and_the_reports_describe_the_same_people(org_lookup):
    """The invariant behind the whole feature."""
    for email in (VERA, DAN, "dee.dm@x.com", RITA, ROB):
        user = auth.resolve_user_identity(email)
        key = auth.resolve_report_scope(user, None, "analytics")
        report = set(_helpers._load_team_scope(_Conn([]), key)["emails"])
        assert report == auth.get_user_scope_emails(user), email


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

ADMIN = UserIdentity(email="admin@x.com", role="admin")
MANAGER = UserIdentity(email=RITA, role="team_lead", org_member_id=4, org_role="resource_manager", org_manages_people=True)
RECRUITER = UserIdentity(email=ANN, role="recruiter", org_member_id=6, org_role="recruiter")

SHEET = (
    "Email. ID,Name,Positions,Reporting Manager,Regional/Vertical Structure,Regional/Vertical Head\n"
    "rita@x.com,Rita Manager,Resource Manager,Rita Manager,SI Region,Hank Head\n"
    "ann@x.com,Ann Recruiter,Recruiter,Rita Manager,SI Region,Hank Head\n"
)


def run(coro):
    return asyncio.run(coro)


def test_every_route_requires_a_signed_in_user():
    assert org_router.router.routes
    for route in org_router.router.routes:
        assert auth.get_current_user in [d.call for d in route.dependant.dependencies], route.path


def test_the_import_defaults_to_a_preview():
    payload = org_router.ImportPayload(csv="x")
    assert payload.dry_run is True
    assert payload.head_role == "delivery_director"


@pytest.mark.parametrize("user", [MANAGER, RECRUITER], ids=["manager", "recruiter"])
def test_only_admins_may_touch_the_hierarchy(monkeypatch, user):
    def explode(*_a, **_k):
        raise AssertionError("must be refused before any work")

    monkeypatch.setattr(oh, "list_members", explode)
    monkeypatch.setattr(oh, "plan_import", explode)
    monkeypatch.setattr(oh, "apply_plan", explode)

    for call in (
        lambda: org_router.get_org_hierarchy(user=user),
        lambda: org_router.import_org_hierarchy(org_router.ImportPayload(csv=SHEET, dry_run=False), user=user),
        lambda: org_router.export_org_hierarchy(user=user),
    ):
        with pytest.raises(HTTPException) as exc:
            run(call())
        assert exc.value.status_code == 403


@pytest.fixture()
def import_stubs(monkeypatch):
    applied = []
    monkeypatch.setattr(oh, "current_emails", lambda: {"old@x.com", "ann@x.com"})
    monkeypatch.setattr(org_router, "_jobs_coverage", lambda emails: {"people_with_email": len(emails), "assigned_to_a_job": 1})

    def fake_apply(plan, imported_by=""):
        applied.append((plan, imported_by))
        return {"people": len(plan.members)}

    monkeypatch.setattr(oh, "apply_plan", fake_apply)
    return applied


def test_a_preview_reports_the_plan_and_changes_nothing(import_stubs):
    res = run(org_router.import_org_hierarchy(org_router.ImportPayload(csv=SHEET), user=ADMIN))
    data = res["data"]
    assert res["status"] == "success"
    assert data["dry_run"] is True and data["applied"] is False
    assert import_stubs == []
    assert data["summary"]["rows"] == 2 and data["summary"]["people"] == 3  # + the unlisted head
    assert data["summary"]["needs_email"][0]["name"] == "Hank Head"
    assert data["blocking"] is False
    # What it would do to the tree that exists today.
    assert data["diff"]["emails_added"] == 1 and data["diff"]["emails_removed"] == 1
    assert data["diff"]["removed_sample"] == ["old@x.com"]
    assert data["coverage"] == {"people_with_email": 2, "assigned_to_a_job": 1}


def test_applying_replaces_the_tree_once_and_records_who(import_stubs):
    res = run(org_router.import_org_hierarchy(org_router.ImportPayload(csv=SHEET, dry_run=False), user=ADMIN))
    assert res["data"]["applied"] is True
    assert len(import_stubs) == 1
    plan, imported_by = import_stubs[0]
    assert imported_by == "admin@x.com" and len(plan.members) == 3


def test_a_blocking_error_refuses_to_apply_and_says_why(import_stubs):
    with pytest.raises(HTTPException) as exc:
        run(org_router.import_org_hierarchy(org_router.ImportPayload(csv="Foo,Bar\n1,2", dry_run=False), user=ADMIN))
    assert exc.value.status_code == 400
    assert exc.value.detail["blocking"] is True
    assert exc.value.detail["applied"] is False
    assert import_stubs == []


def test_a_blocking_preview_is_reported_not_raised(import_stubs):
    res = run(org_router.import_org_hierarchy(org_router.ImportPayload(csv="Foo,Bar\n1,2"), user=ADMIN))
    assert res["data"]["blocking"] is True and res["data"]["applied"] is False


@pytest.mark.parametrize("head_role", ["recruiter", "ceo", ""])
def test_a_head_cannot_be_a_recruiter_or_an_invented_level(import_stubs, head_role):
    with pytest.raises(HTTPException) as exc:
        run(org_router.import_org_hierarchy(org_router.ImportPayload(csv=SHEET, head_role=head_role), user=ADMIN))
    assert exc.value.status_code == 400


def test_the_head_role_option_reaches_the_importer(import_stubs):
    res = run(org_router.import_org_hierarchy(org_router.ImportPayload(csv=SHEET, head_role="avp"), user=ADMIN))
    assert res["data"]["summary"]["by_role"]["avp"] == 1


def test_a_failed_diff_does_not_fail_the_preview(monkeypatch, import_stubs):
    monkeypatch.setattr(oh, "current_emails", lambda: (_ for _ in ()).throw(RuntimeError("no table yet")))
    res = run(org_router.import_org_hierarchy(org_router.ImportPayload(csv=SHEET), user=ADMIN))
    assert res["data"]["diff"] is None and res["data"]["summary"]["people"] == 3


def test_the_tree_view_returns_members_counts_and_the_last_import(monkeypatch):
    monkeypatch.setattr(oh, "last_import", lambda: {"imported_by": "admin@x.com", "imported_at": "2026-10-06T00:00:00"})
    res = run(org_router.get_org_hierarchy(user=ADMIN))["data"]
    assert res["counts"]["total"] == 8 and res["counts"]["with_email"] == 8
    assert res["last_import"]["imported_by"] == "admin@x.com"
    assert {m["name"] for m in res["members"]} >= {"Vera Avp", "Ann Rec"}


def test_the_export_is_a_sheet_the_importer_reads_back(monkeypatch):
    res = run(org_router.export_org_hierarchy(user=ADMIN))["data"]
    assert res["filename"] == "org-hierarchy.csv"
    plan = oh.plan_import(res["csv"])
    assert len(plan.members) == len(ORG) and not plan.blocking
    by_name = {m.name: m for m in plan.members}
    assert by_name["Ann Rec"].role == "recruiter" and by_name["Vera Avp"].role == "avp"


# ---------------------------------------------------------------------------
# /me and the schema guard
# ---------------------------------------------------------------------------

def test_me_reports_the_level_and_whether_anyone_reports_to_you():
    me = auth.get_my_identity(user=MANAGER)
    assert me["org_role"] == "resource_manager" and me["org_role_label"] == "Resource Manager"
    assert me["manages_people"] is True and me["is_team_lead"] is True

    leaf = auth.get_my_identity(user=RECRUITER)
    assert leaf["org_role"] == "recruiter" and leaf["manages_people"] is False

    stranger = auth.get_my_identity(user=UserIdentity(email="s@x.com", role="recruiter"))
    assert stranger["org_role"] is None and stranger["org_role_label"] is None
    assert stranger["manages_people"] is False


# ---------------------------------------------------------------------------
# Productivity tab: an org scope is not a Teams-page team
# ---------------------------------------------------------------------------

@pytest.fixture()
def productivity(monkeypatch):
    from routers import pair_dashboard as pd
    from services import pair_dashboard as dash

    seen = {}
    monkeypatch.setattr(pd, "get_db_connection", lambda: MagicMock())
    monkeypatch.setattr(pd, "_load_context", lambda conn, refresh: ([], []))
    monkeypatch.setattr(pd, "_mirror_state", lambda conn: {
        "jobs_available": True, "activities_available": True, "users_available": True,
        "job_users_ready": True, "activities_from": None,
    })
    monkeypatch.setattr(dash, "team_jobdiva_users", lambda conn, emails: {e: f"u-{e}" for e in emails})
    monkeypatch.setattr(dash, "load_pyramid_reqs", lambda conn, **kw: [])
    monkeypatch.setattr(dash, "load_activities", lambda conn, **kw: [])

    def fake_compute(**kw):
        seen.update(kw)
        return {"weeks": []}

    monkeypatch.setattr(dash, "compute_productivity", fake_compute)
    return pd, dash, seen


def _scope(team_id, name, leads, members):
    return {
        "team_id": team_id, "team_name": name,
        "emails": sorted(set(leads) | set(members)),
        "lead_emails": sorted(leads), "member_emails": sorted(members),
        "job_ids": [], "sc_keys": [],
    }


def test_productivity_for_an_org_scope_averages_over_the_recruiters_beneath(monkeypatch, productivity):
    pd, dash, seen = productivity
    scope = _scope("org:2", "Dan Dd (Delivery Director)", [DAN, RITA, ROB], [ANN, BOB, CAT])
    monkeypatch.setattr(pd, "_team_scope", lambda conn, key: scope)

    out = pd._productivity_sync("org:2", None, dash.DashboardFilters(), False, [])

    assert "unavailable" not in out
    assert out["teams_configured"] == 1
    # Managers are tagged on the reqs but are not the people the work is averaged over.
    assert seen["recruiter_count"] == 3
    assert out["team_scope"]["team_id"] == "org:2"


def test_productivity_for_a_plain_team_still_uses_the_teams_page_team(monkeypatch, productivity):
    pd, dash, seen = productivity
    scope = _scope("t-1", "East", ["lead@x.com"], ["m1@x.com", "m2@x.com"])
    monkeypatch.setattr(pd, "_team_scope", lambda conn, key: scope)
    teams = [{"id": "t-1", "name": "East", "lead_emails": ["lead@x.com"], "member_emails": ["m1@x.com", "m2@x.com"]}]

    out = pd._productivity_sync("t-1", None, dash.DashboardFilters(), False, teams)

    assert out["teams_configured"] == 1 and seen["recruiter_count"] == 2


def test_productivity_with_nobody_in_the_scope_says_so_instead_of_dividing_by_zero(monkeypatch, productivity):
    pd, dash, seen = productivity
    monkeypatch.setattr(pd, "_team_scope", lambda conn, key: _scope("org:6", "Ann (Recruiter)", [], []))
    out = pd._productivity_sync("org:6", None, dash.DashboardFilters(), False, [])
    assert "unavailable" in out and seen == {}


def test_the_admin_all_teams_view_is_unchanged(monkeypatch, productivity):
    pd, dash, seen = productivity
    monkeypatch.setattr(pd, "_team_scope", lambda conn, key: None)
    teams = [
        {"id": "t-1", "name": "East", "lead_emails": ["l1@x.com"], "member_emails": ["a@x.com"]},
        {"id": "t-2", "name": "West", "lead_emails": ["l2@x.com"], "member_emails": ["b@x.com", "c@x.com"]},
    ]
    out = pd._productivity_sync(None, None, dash.DashboardFilters(), False, teams)
    assert out["teams_configured"] == 2 and seen["recruiter_count"] == 3


# ---------------------------------------------------------------------------
# Recruiter Analytics: who the "team" column names
# ---------------------------------------------------------------------------

def test_the_directory_names_each_persons_manager_and_counts_them_as_known_to_pair():
    from routers import recruiter_analytics as ra

    class Cur:
        def __init__(self):
            self.results = []

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def execute(self, sql, params=None):
            text = " ".join(sql.split())
            if "FROM team_members" in text:
                self.results = [("legacy@x.com", "East", "lead")]
            elif "FROM user_roles" in text:
                self.results = [("boss@x.com",)]
            elif "to_regclass" in text:
                self.results = [(True,)]
            elif "FROM org_members" in text:
                self.results = [("ann.rec@x.com", "Rita Rm", "recruiter"), ("rita.rm@x.com", "Dee Dm", "resource_manager"),
                                ("legacy@x.com", "Someone", "recruiter")]
            else:
                self.results = []

        def fetchall(self):
            return self.results

        def fetchone(self):
            return self.results[0]

    conn = SimpleNamespace(cursor=lambda: Cur())
    directory = ra._load_directory(conn)

    assert directory["team_by_email"]["ann.rec@x.com"] == {"team_name": "Rita Rm", "is_team_lead": False}
    assert directory["team_by_email"]["rita.rm@x.com"] == {"team_name": "Dee Dm", "is_team_lead": True}
    # A Teams-page team wins over the hierarchy.
    assert directory["team_by_email"]["legacy@x.com"] == {"team_name": "East", "is_team_lead": True}
    assert {"ann.rec@x.com", "rita.rm@x.com", "legacy@x.com", "boss@x.com"} <= directory["accounts"]


def test_the_directory_survives_an_org_table_that_does_not_exist_yet():
    from routers import recruiter_analytics as ra

    class Cur:
        results = []

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def execute(self, sql, params=None):
            text = " ".join(sql.split())
            if "FROM org_members" in text:
                raise AssertionError("must not query a table to_regclass says is absent")
            self.results = [(False,)] if "to_regclass" in text else (
                [("lead@x.com", "East", "lead")] if "FROM team_members" in text else []
            )

        def fetchall(self):
            return self.results

        def fetchone(self):
            return self.results[0]

    directory = ra._load_directory(SimpleNamespace(cursor=lambda: Cur()))
    assert directory["team_by_email"] == {"lead@x.com": {"team_name": "East", "is_team_lead": True}}
