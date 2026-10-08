"""The org hierarchy's SQL, run against a real Postgres.

The unit tests prove the policy with the database stubbed; this proves the
statements themselves — the schema, the atomic replace (a failed import must
leave the old tree, and everyone's access, exactly as it was), the membership
query behind every request's identity, and the report scope read.

Runs only when the embedded-Postgres package ``pgserver`` is importable
(``pip install pgserver``); it is not in requirements-dev, so CI skips it, like
the other real-Postgres suites.
"""

import json
import shutil

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg2 = pytest.importorskip("psycopg2")

import core.auth as auth  # noqa: E402
from routers import _helpers, recruiter_analytics as ra  # noqa: E402
from services import org_hierarchy as oh  # noqa: E402
from services import teams_db  # noqa: E402

SHEET = (
    "Email. ID,Name,Positions,Reporting Manager,Regional/Vertical Structure,Regional/Vertical Head\n"
    "dan@x.com,Dan Director,Delivery Director,,SI Region,\n"
    "dee@x.com,Dee Manager,Delivery Manager,Dan Director,SI Region,Dan Director\n"
    "rita@x.com,Rita RM,Resource Manager,Dee Manager,SI Region,Dan Director\n"
    "ann@x.com,Ann Recruiter,Recruiter,Rita RM,SI Region,Dan Director\n"
    "bob@x.com,Bob Recruiter,Recruiter ,rita@x.com,SI Region,Dan Director\n"
    "cat@x.com,Cat Recruiter,Step - Graduate,Rob Nomail,SI Region,Dan Director\n"
)


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    datadir = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(datadir))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()
        shutil.rmtree(datadir, ignore_errors=True)


class _Pooled:
    """Behaves like core.db._PooledConnection: `with` commits/rolls back, then
    hands the connection back (here: closes it)."""

    def __init__(self, uri):
        object.__setattr__(self, "_c", psycopg2.connect(uri))

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        setattr(self._c, name, value)

    def __enter__(self):
        return self._c.__enter__()

    def __exit__(self, exc_type, exc, tb):
        try:
            self._c.__exit__(exc_type, exc, tb)
        finally:
            self.close()

    def close(self):
        if not self._c.closed:
            self._c.close()


@pytest.fixture
def db(pg_uri, monkeypatch):
    def connect():
        return _Pooled(pg_uri)

    for module in (oh, teams_db):
        monkeypatch.setattr(module, "get_db_connection", connect)
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)

    admin = psycopg2.connect(pg_uri)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(
            "DROP TABLE IF EXISTS org_members, org_hierarchy_imports, team_members, teams, "
            "user_roles, monitored_jobs"
        )
    oh._ensure_schema()
    teams_db._ensure_teams_schema()
    with admin.cursor() as cur:
        # Created by routers/jobs._ensure_monitored_jobs_schema in the real app.
        cur.execute(
            "CREATE TABLE user_roles (email TEXT PRIMARY KEY, role TEXT NOT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
    yield admin
    admin.close()


def apply(sheet=SHEET, by="admin@x.com"):
    plan = oh.plan_import(sheet)
    assert not plan.blocking, plan.issues
    oh.apply_plan(plan, by)
    return plan


def by_name():
    return {m.name: m for m in oh.list_members()}


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_the_schema_is_idempotent(db):
    oh._ensure_schema()
    oh._ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass('org_members') IS NOT NULL, to_regclass('org_hierarchy_imports') IS NOT NULL")
        assert cur.fetchone() == (True, True)


def test_one_person_per_email_regardless_of_case_but_blank_emails_may_repeat(db):
    with db.cursor() as cur:
        cur.execute("INSERT INTO org_members (name, email, role) VALUES ('A', 'a@x.com', 'recruiter')")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("INSERT INTO org_members (name, email, role) VALUES ('A2', 'A@X.COM', 'recruiter')")
    with db.cursor() as cur:
        cur.execute("INSERT INTO org_members (name, email, role) VALUES ('P1', NULL, 'resource_manager')")
        cur.execute("INSERT INTO org_members (name, email, role) VALUES ('P2', NULL, 'resource_manager')")
        cur.execute("SELECT COUNT(*) FROM org_members WHERE email IS NULL")
        assert cur.fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Applying a plan
# ---------------------------------------------------------------------------

def test_a_plan_is_stored_with_every_parent_link_intact(db):
    apply()
    members = by_name()
    assert len(members) == 7  # 6 rows + the unlisted manager "Rob Nomail"
    ids = {m.id: m for m in members.values()}

    def boss(name):
        parent = members[name].reports_to_id
        return ids[parent].name if parent is not None else None

    assert boss("Dan Director") is None
    assert boss("Dee Manager") == "Dan Director"
    assert boss("Rita RM") == "Dee Manager"
    assert boss("Ann Recruiter") == "Rita RM"
    assert boss("Bob Recruiter") == "Rita RM"  # named by email
    assert boss("Cat Recruiter") == "Rob Nomail"
    assert boss("Rob Nomail") == "Dan Director"
    assert members["Rob Nomail"].email is None and members["Rob Nomail"].role == "resource_manager"
    assert members["Cat Recruiter"].title == "Step - Graduate"
    assert members["Ann Recruiter"].vertical == "SI Region"


def test_applying_again_replaces_the_whole_tree(db):
    apply()
    first_ids = {m.id for m in oh.list_members()}
    apply(
        "Email. ID,Name,Positions,Reporting Manager,Regional/Vertical Structure,Regional/Vertical Head\n"
        "solo@x.com,Solo,Recruiter,,,\n"
    )
    members = oh.list_members()
    assert [m.name for m in members] == ["Solo"]
    assert {m.id for m in members}.isdisjoint(first_ids)


def test_a_failed_apply_leaves_the_previous_tree_untouched(db):
    apply()
    before = {(m.name, m.email, m.reports_to_id) for m in oh.list_members()}

    bad = oh.ImportPlan(members=[
        oh.PlannedMember(key="e:a", name="Dup One", email="dup@x.com", role="recruiter"),
        oh.PlannedMember(key="e:b", name="Dup Two", email="dup@x.com", role="recruiter"),  # unique violation
    ])
    with pytest.raises(psycopg2.errors.UniqueViolation):
        oh.apply_plan(bad, "admin@x.com")

    assert {(m.name, m.email, m.reports_to_id) for m in oh.list_members()} == before
    # ...and nothing was recorded as imported either.
    assert oh.last_import()["imported_by"] == "admin@x.com"


def test_a_blocking_plan_is_refused_before_anything_is_deleted(db):
    apply()
    with pytest.raises(ValueError):
        oh.apply_plan(oh.plan_import("Foo,Bar\n1,2"), "admin@x.com")
    assert len(oh.list_members()) == 7


def test_the_audit_trail_records_who_replaced_the_tree(db):
    assert oh.last_import() is None
    apply(by="Admin@X.com")
    last = oh.last_import()
    assert last["imported_by"] == "admin@x.com" and last["imported_at"]
    with db.cursor() as cur:
        cur.execute("SELECT summary FROM org_hierarchy_imports")
        summary = json.loads(cur.fetchone()[0])
    assert summary["people"] == 7 and summary["without_email"] == 1


def test_current_emails_lists_only_people_with_an_email(db):
    apply()
    assert oh.current_emails() == {
        "dan@x.com", "dee@x.com", "rita@x.com", "ann@x.com", "bob@x.com", "cat@x.com",
    }


# ---------------------------------------------------------------------------
# The identity query (runs on every request)
# ---------------------------------------------------------------------------

def test_membership_says_who_you_are_and_whether_anyone_reports_to_you(db):
    apply()
    rita = oh.get_membership("  Rita@X.com ")
    assert rita["name"] == "Rita RM" and rita["role"] == "resource_manager" and rita["has_reports"] is True
    ann = oh.get_membership("ann@x.com")
    assert ann["role"] == "recruiter" and ann["has_reports"] is False
    assert oh.get_membership("nobody@x.com") is None
    assert oh.get_membership("") is None


def test_a_person_in_the_tree_without_an_email_cannot_be_found_by_email(db):
    apply()
    assert oh.get_membership("rob.nomail@x.com") is None


# ---------------------------------------------------------------------------
# Scope through the real read
# ---------------------------------------------------------------------------

def test_scope_emails_walks_the_tree_at_any_depth(db):
    apply()
    ids = {m.email: m.id for m in oh.list_members() if m.email}
    assert oh.scope_emails(ids["dan@x.com"]) == {
        "dan@x.com", "dee@x.com", "rita@x.com", "ann@x.com", "bob@x.com", "cat@x.com",
    }
    assert oh.scope_emails(ids["dee@x.com"]) == {"dee@x.com", "rita@x.com", "ann@x.com", "bob@x.com"}
    assert oh.scope_emails(ids["rita@x.com"]) == {"rita@x.com", "ann@x.com", "bob@x.com"}
    assert oh.scope_emails(ids["ann@x.com"]) == {"ann@x.com"}
    assert oh.scope_emails(10_000) == set()


def test_an_email_less_manager_still_groups_their_reports(db):
    apply()
    rob = by_name()["Rob Nomail"]
    scope = oh.get_scope(rob.id)
    assert scope["emails"] == ["cat@x.com"]
    assert [m.name for m in scope["people"]][0] == "Rob Nomail"


def test_end_to_end_identity_scope_and_job_access_over_real_sql(db):
    apply()
    rita = auth.resolve_user_identity("rita@x.com")
    assert rita.role == "team_lead" and rita.org_role == "resource_manager" and rita.manages_org
    assert auth.get_user_scope_emails(rita) == {"rita@x.com", "ann@x.com", "bob@x.com"}

    auth.verify_job_access({"recruiter_emails": ["ann@x.com"]}, rita)
    with pytest.raises(auth.HTTPException):
        auth.verify_job_access({"recruiter_emails": ["cat@x.com"]}, rita)  # reports to Rob, not Rita

    dan = auth.resolve_user_identity("dan@x.com")
    auth.verify_job_access({"recruiter_emails": ["cat@x.com"]}, dan)
    assert auth.resolve_user_identity("ann@x.com").role == "recruiter"


def test_a_manager_who_leads_a_team_gets_the_union_over_real_sql(db):
    apply()
    teams_db.create_team("East", ["rita@x.com"], ["outsider@x.com"], "admin@x.com")
    rita = auth.resolve_user_identity("rita@x.com")
    assert rita.leads_team and rita.manages_org
    assert auth.get_user_scope_emails(rita) == {"rita@x.com", "ann@x.com", "bob@x.com", "outsider@x.com"}
    key = auth.resolve_report_scope(rita, None, "analytics")
    assert key.startswith("org:") and "+" in key


def test_a_manager_who_is_only_a_team_member_is_not_handed_the_roster(db):
    apply()
    teams_db.create_team("East", ["lead@x.com"], ["rita@x.com", "outsider@x.com"], "admin@x.com")
    rita = auth.resolve_user_identity("rita@x.com")
    assert rita.is_team_lead and rita.team_id and not rita.leads_team
    assert auth.get_user_scope_emails(rita) == {"rita@x.com", "ann@x.com", "bob@x.com"}


# ---------------------------------------------------------------------------
# Reports: scope key -> emails and jobs
# ---------------------------------------------------------------------------

def _jobs(db):
    with db.cursor() as cur:
        cur.execute("CREATE TABLE monitored_jobs (job_id TEXT, jobdiva_id TEXT, recruiter_emails TEXT)")
        for job_id, jd, emails in [
            ("j-ann", "JD-1", ["ann@x.com"]),
            ("j-bob", "JD-2", ["bob@x.com", "outsider@x.com"]),
            ("j-cat", "JD-3", ["cat@x.com"]),
            ("j-none", "JD-4", []),
        ]:
            cur.execute("INSERT INTO monitored_jobs VALUES (%s, %s, %s)", (job_id, jd, json.dumps(emails)))


def test_load_team_scope_over_real_tables(db):
    apply()
    _jobs(db)
    rita_id = by_name()["Rita RM"].id
    conn = psycopg2.connect(db.dsn)
    try:
        scope = _helpers._load_team_scope(conn, f"org:{rita_id}")
        assert scope["emails"] == ["ann@x.com", "bob@x.com", "rita@x.com"]
        assert scope["job_ids"] == ["j-ann", "j-bob"]
        assert scope["sc_keys"] == sorted({"j-ann", "j-bob", "JD-1", "JD-2"})
        assert scope["lead_emails"] == ["rita@x.com"] and scope["member_emails"] == ["ann@x.com", "bob@x.com"]

        dan_id = by_name()["Dan Director"].id
        assert set(_helpers._load_team_scope(conn, f"org:{dan_id}")["job_ids"]) == {"j-ann", "j-bob", "j-cat"}

        team = teams_db.create_team("East", ["rita@x.com"], ["outsider@x.com"], "admin@x.com")
        union = _helpers._load_team_scope(conn, f"org:{rita_id}+{team['id']}")
        assert "outsider@x.com" in union["emails"] and "j-bob" in union["job_ids"]

        with pytest.raises(LookupError):
            _helpers._load_team_scope(conn, "org:999999")
    finally:
        conn.close()


def test_the_recruiter_directory_reads_the_hierarchy_over_real_tables(db):
    apply()
    with db.cursor() as cur:
        cur.execute("INSERT INTO user_roles (email, role) VALUES ('boss@x.com', 'admin')")
    conn = psycopg2.connect(db.dsn)
    try:
        directory = ra._load_directory(conn)
    finally:
        conn.close()
    assert directory["team_by_email"]["ann@x.com"] == {"team_name": "Rita RM", "is_team_lead": False}
    assert directory["team_by_email"]["rita@x.com"] == {"team_name": "Dee Manager", "is_team_lead": True}
    assert directory["team_by_email"]["cat@x.com"]["team_name"] == "Rob Nomail"
    assert {"ann@x.com", "dan@x.com", "boss@x.com"} <= directory["accounts"]


def test_the_recruiter_directory_works_before_the_hierarchy_table_exists(db):
    with db.cursor() as cur:
        cur.execute("DROP TABLE org_members")
    teams_db.create_team("East", ["lead@x.com"], ["m@x.com"], "admin@x.com")
    conn = psycopg2.connect(db.dsn)
    try:
        directory = ra._load_directory(conn)
    finally:
        conn.close()
    assert directory["team_by_email"]["lead@x.com"] == {"team_name": "East", "is_team_lead": True}
    assert "ann@x.com" not in directory["accounts"]
