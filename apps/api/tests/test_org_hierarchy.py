"""Org hierarchy (services/org_hierarchy.py): the mapping-sheet importer and the
scope computation. Pure logic — no database; the SQL is covered by
tests/test_org_hierarchy_postgres.py.

The fixtures reproduce the quirks of the real "Recruiter Mapping" sheet:
  * Resource Managers list THEMSELVES as their Reporting Manager;
  * most managers have no row of their own (named only, no email);
  * the same manager is spelled with different case ("Divyanjali K" / "k");
  * Positions carries trailing spaces and non-management titles
    ("Step - Graduate", "Executive Resourcing");
  * a vertical's name contains a comma ("H,P&C + Engineering").
"""

import csv
import io

import pytest

from services import org_hierarchy as oh
from services.org_hierarchy import Member

HEADER = ["Email. ID", "Name", "Positions", "Reporting Manager", "Regional/Vertical Structure", "Regional/Vertical Head"]


def sheet(*rows, header=HEADER, delimiter=","):
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def tree(plan):
    """{name: (role, parent name, email)} — the shape every test asserts on."""
    by_key = {m.key: m for m in plan.members}
    return {
        m.name: (m.role, by_key[m.parent_key].name if m.parent_key else None, m.email)
        for m in plan.members
    }


def messages(plan, severity=None):
    return [i.message for i in plan.issues if severity is None or i.severity == severity]


# The shape of the real sheet, in miniature.
REAL_SHAPE = sheet(
    ["rita@x.com", "Rita Manager", "Resource Manager", "Rita Manager", "SI Region", "Hank Head"],
    ["ann@x.com", "Ann Recruiter", "Recruiter ", "Rita Manager", "SI Region", "Hank Head"],
    ["bob@x.com", "Bob Recruiter", "Recruiter", "Nina Nomail", "SI Region", "Hank Head"],
    ["cy@x.com", "Cy Graduate", "Step - Graduate", "nina nomail", "SI Region", "Hank Head"],
    ["eve@x.com", "Eve Exec", "Executive Resourcing", "Tom Telecom", "H,P&C + Engineering", "Tess Head"],
)


# ---------------------------------------------------------------------------
# Titles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title,role,recognised",
    [
        ("Recruiter", oh.RECRUITER, True),
        ("Recruiter ", oh.RECRUITER, True),
        ("Resource Manager", oh.RESOURCE_MANAGER, True),
        ("  resource   manager ", oh.RESOURCE_MANAGER, True),
        ("resource_manager", oh.RESOURCE_MANAGER, True),
        ("Recruiting Manager", oh.RESOURCE_MANAGER, True),
        ("Delivery Manager", oh.DELIVERY_MANAGER, True),
        ("Sr. Delivery Manager", oh.DELIVERY_MANAGER, True),
        ("Delivery Director", oh.DELIVERY_DIRECTOR, True),
        ("AVP", oh.AVP, True),
        ("Assistant Vice President", oh.AVP, True),
        # Not management levels: Recruiter, and flagged so an admin can correct a typo.
        ("Step - Graduate", oh.RECRUITER, False),
        ("Executive Resourcing", oh.RECRUITER, False),
        ("Sr. Executive Resourcing", oh.RECRUITER, False),
        ("", oh.RECRUITER, False),
        # Least privilege: a plain "Director" or "Manager" is NOT silently promoted.
        ("Director", oh.RECRUITER, False),
        ("Manager", oh.RECRUITER, False),
    ],
)
def test_level_for_title(title, role, recognised):
    assert oh.level_for_title(title) == (role, recognised)


def test_rank_and_labels_follow_the_five_levels():
    assert oh.LEVELS == ("recruiter", "resource_manager", "delivery_manager", "delivery_director", "avp")
    assert [oh.rank_of(level) for level in oh.LEVELS] == [1, 2, 3, 4, 5]
    assert oh.role_label("delivery_director") == "Delivery Director"
    assert oh.role_label("avp") == "AVP"
    # Unknown roles are the lowest level, never an accidental promotion.
    assert oh.rank_of("ceo") == 1 and oh.normalize_role("ceo") == "recruiter"
    assert oh.level_above("recruiter") == "resource_manager"
    assert oh.level_above("avp") is None


# ---------------------------------------------------------------------------
# The real sheet's shape
# ---------------------------------------------------------------------------

def test_real_sheet_shape_builds_the_expected_tree():
    plan = oh.plan_import(REAL_SHAPE)
    t = tree(plan)

    # A Resource Manager who lists themselves as their own manager reports to the head.
    assert t["Rita Manager"] == ("resource_manager", "Hank Head", "rita@x.com")
    assert t["Ann Recruiter"] == ("recruiter", "Rita Manager", "ann@x.com")
    # A manager with no row of their own is kept, without an email, one level above their reports.
    assert t["Nina Nomail"] == ("resource_manager", "Hank Head", None)
    assert t["Bob Recruiter"][1] == "Nina Nomail"
    # ...and a differently-cased spelling is the SAME person, not a second manager.
    assert t["Cy Graduate"][1] == "Nina Nomail"
    assert sum(1 for m in plan.members if oh.name_key(m.name) == "nina nomail") == 1
    # Heads the sheet only names are Delivery Directors by default, with nobody above them.
    assert t["Hank Head"] == ("delivery_director", None, None)
    assert t["Tess Head"] == ("delivery_director", None, None)
    assert t["Tom Telecom"] == ("resource_manager", "Tess Head", None)

    assert not plan.blocking
    assert plan.rows == 5
    assert len(plan.members) == 5 + 4  # 5 rows + Nina, Hank, Tom, Tess


def test_real_sheet_summary_lists_who_needs_an_email_and_who_is_on_top():
    summary = oh.plan_import(REAL_SHAPE).summary()

    assert summary["rows"] == 5 and summary["people"] == 9
    assert summary["with_email"] == 5 and summary["without_email"] == 4
    # Four recruiters (Ann, Bob, Cy, and Eve whose "Executive Resourcing" is not a management level).
    assert summary["by_role"] == {
        "recruiter": 4, "resource_manager": 3, "delivery_manager": 0, "delivery_director": 2, "avp": 0,
    }
    # Biggest gaps first: Hank sits over 5 people, Nina over 2.
    needs = {n["name"]: n for n in summary["needs_email"]}
    assert set(needs) == {"Nina Nomail", "Hank Head", "Tom Telecom", "Tess Head"}
    assert summary["needs_email"][0]["name"] == "Hank Head"
    assert needs["Nina Nomail"]["direct_reports"] == 2 and needs["Nina Nomail"]["named_only"] is True
    assert needs["Hank Head"]["total_reports"] == 5
    # Nobody above the heads: that is where the AVP level is still to be filled in.
    assert [t["name"] for t in summary["top_level"]] == ["Hank Head", "Tess Head"]
    assert summary["titles_treated_as_recruiter"] == [
        {"title": "Executive Resourcing", "count": 1},
        {"title": "Step - Graduate", "count": 1},
    ]
    assert any("treated as Recruiters" in m for m in messages(oh.plan_import(REAL_SHAPE), "info"))


def test_named_only_people_inherit_the_vertical_of_the_rows_that_name_them():
    plan = oh.plan_import(REAL_SHAPE)
    by_name = {m.name: m for m in plan.members}
    assert by_name["Nina Nomail"].vertical == "SI Region"
    assert by_name["Hank Head"].vertical == "SI Region"
    # The comma inside the vertical survives CSV quoting.
    assert by_name["Eve Exec"].vertical == "H,P&C + Engineering"
    assert by_name["Tom Telecom"].vertical == "H,P&C + Engineering"


def test_emails_are_lowercased_and_titles_keep_their_original_text():
    plan = oh.plan_import(sheet(["Ann.R@Pyramidci.com", "Ann", "Step - Graduate  ", "", "", ""]))
    (member,) = plan.members
    assert member.email == "ann.r@pyramidci.com"
    assert member.title == "Step - Graduate"
    assert member.role == "recruiter"


# ---------------------------------------------------------------------------
# All five levels
# ---------------------------------------------------------------------------

def test_a_full_five_level_chain_resolves_by_name_and_by_email():
    plan = oh.plan_import(sheet(
        ["vera@x.com", "Vera AVP", "AVP", "", "", ""],
        ["dan@x.com", "Dan Director", "Delivery Director", "Vera AVP", "", ""],
        ["dee@x.com", "Dee Manager", "Delivery Manager", "dan@x.com", "", ""],  # by email
        ["ravi@x.com", "Ravi RM", "Resource Manager", "Dee Manager", "", ""],
        ["rey@x.com", "Rey Recruiter", "Recruiter", "Ravi RM", "", ""],
    ))
    assert tree(plan) == {
        "Vera AVP": ("avp", None, "vera@x.com"),
        "Dan Director": ("delivery_director", "Vera AVP", "dan@x.com"),
        "Dee Manager": ("delivery_manager", "Dan Director", "dee@x.com"),
        "Ravi RM": ("resource_manager", "Dee Manager", "ravi@x.com"),
        "Rey Recruiter": ("recruiter", "Ravi RM", "rey@x.com"),
    }
    assert plan.issues == []


def test_levels_may_be_skipped():
    # A Resource Manager straight under a Delivery Director (no Delivery Manager).
    plan = oh.plan_import(sheet(
        ["dan@x.com", "Dan Director", "Delivery Director", "", "", ""],
        ["ravi@x.com", "Ravi RM", "Resource Manager", "Dan Director", "", ""],
    ))
    assert tree(plan)["Ravi RM"][1] == "Dan Director"
    assert plan.issues == []


def test_an_unknown_manager_is_placed_one_level_above_whoever_names_them():
    plan = oh.plan_import(sheet(
        ["ravi@x.com", "Ravi RM", "Resource Manager", "Gus Unknown", "", ""],
        ["dan@x.com", "Dan Director", "Delivery Director", "Zed Unknown", "", ""],
    ))
    t = tree(plan)
    # Above a Resource Manager is a Delivery Manager; above a Director, an AVP.
    assert t["Gus Unknown"] == ("delivery_manager", None, None)
    assert t["Zed Unknown"] == ("avp", None, None)
    assert t["Ravi RM"][1] == "Gus Unknown" and t["Dan Director"][1] == "Zed Unknown"


@pytest.mark.parametrize(
    "head_role,expected",
    [("delivery_director", "delivery_director"), ("avp", "avp"), ("delivery_manager", "delivery_manager")],
)
def test_head_role_decides_what_an_unlisted_regional_head_is(head_role, expected):
    plan = oh.plan_import(
        sheet(["a@x.com", "Ann", "Recruiter", "", "SI", "Hal Head"]), head_role=head_role,
    )
    assert tree(plan)["Hal Head"][0] == expected
    assert tree(plan)["Ann"][1] == "Hal Head"


def test_the_head_is_the_fallback_when_no_manager_is_stated():
    plan = oh.plan_import(sheet(["a@x.com", "Ann", "Recruiter", "", "SI", "Hal Head"]))
    assert tree(plan)["Ann"] == ("recruiter", "Hal Head", "a@x.com")


def test_a_manager_wins_over_the_head_when_both_are_stated():
    plan = oh.plan_import(sheet(
        ["m@x.com", "Mia Manager", "Resource Manager", "", "SI", "Hal Head"],
        ["a@x.com", "Ann", "Recruiter", "Mia Manager", "SI", "Hal Head"],
    ))
    assert tree(plan)["Ann"][1] == "Mia Manager"
    assert tree(plan)["Mia Manager"][1] == "Hal Head"


# ---------------------------------------------------------------------------
# Bad data: reported, never guessed
# ---------------------------------------------------------------------------

def test_a_manager_who_is_not_a_level_above_is_ignored_with_a_warning():
    plan = oh.plan_import(sheet(
        ["pat@x.com", "Pat Peer", "Recruiter", "", "SI", ""],
        ["ann@x.com", "Ann", "Recruiter", "Pat Peer", "SI", "Hal Head"],
    ))
    # Pat is a Recruiter, so cannot manage Ann: fall back to the head.
    assert tree(plan)["Ann"][1] == "Hal Head"
    assert any("not a level above" in m for m in messages(plan, "warning"))


def test_a_duplicate_email_keeps_the_first_row_and_says_so():
    plan = oh.plan_import(sheet(
        ["ann@x.com", "Ann One", "Recruiter", "", "", ""],
        ["ANN@x.com", "Ann Two", "Recruiter", "", "", ""],
    ))
    assert [m.name for m in plan.members] == ["Ann One"]
    assert any("already appears on row 2" in m for m in messages(plan, "warning"))


def test_an_invalid_email_is_dropped_but_the_person_stays_in_the_tree():
    plan = oh.plan_import(sheet(["not-an-email", "Ann", "Recruiter", "", "", ""]))
    assert tree(plan)["Ann"] == ("recruiter", None, None)
    assert any("not a valid email" in m for m in messages(plan, "warning"))


def test_a_row_with_neither_name_nor_email_is_skipped_and_a_blank_line_is_ignored():
    plan = oh.plan_import("\n".join([",".join(HEADER), ",,Recruiter,,,", "", "ann@x.com,Ann,Recruiter,,,"]))
    assert [m.name for m in plan.members] == ["Ann"]
    assert any("neither a name nor an email" in m for m in messages(plan, "warning"))


def test_a_name_with_no_name_column_value_is_derived_from_the_email():
    plan = oh.plan_import(sheet(["aakash.singh@x.com", "", "Recruiter", "", "", ""]))
    assert plan.members[0].name == "Aakash Singh"


def test_an_ambiguous_manager_name_links_nobody_until_an_email_is_used():
    rows = [
        ["s1@x.com", "Sam Lee", "Resource Manager", "", "A", "Hal Head"],
        ["s2@x.com", "Sam Lee", "Resource Manager", "", "B", "Hal Head"],
    ]
    ambiguous = oh.plan_import(sheet(*rows, ["ann@x.com", "Ann", "Recruiter", "Sam Lee", "A", "Hal Head"]))
    assert tree(ambiguous)["Ann"][1] == "Hal Head"  # not guessed
    assert any("matches 2 different people" in m for m in messages(ambiguous, "warning"))

    precise = oh.plan_import(sheet(*rows, ["ann@x.com", "Ann", "Recruiter", "s2@x.com", "B", "Hal Head"]))
    by_key = {m.key: m for m in precise.members}
    ann = next(m for m in precise.members if m.name == "Ann")
    assert by_key[ann.parent_key].email == "s2@x.com"
    assert not any("matches" in m for m in messages(precise))


def test_a_manager_email_with_no_row_is_kept_with_that_email():
    plan = oh.plan_import(sheet(["ann@x.com", "Ann", "Recruiter", "boss.person@x.com", "", ""]))
    boss = next(m for m in plan.members if m.email == "boss.person@x.com")
    assert boss.name == "Boss Person" and boss.role == "resource_manager" and boss.from_sheet is False


def test_reports_in_two_verticals_are_flagged_for_a_named_only_manager():
    plan = oh.plan_import(sheet(
        ["a@x.com", "Ann", "Recruiter", "Nina Nomail", "A", "Head One"],
        ["b@x.com", "Bob", "Recruiter", "Nina Nomail", "B", "Head Two"],
    ))
    assert any("more than one Regional/Vertical Head" in m for m in messages(plan, "warning"))
    # Nobody is dropped: both still sit under Nina.
    assert tree(plan)["Ann"][1] == tree(plan)["Bob"][1] == "Nina Nomail"


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def test_tab_separated_text_pasted_from_excel_is_read():
    plan = oh.plan_import(sheet(["a@x.com", "Ann", "Recruiter", "", "SI", ""], delimiter="\t"))
    assert tree(plan) == {"Ann": ("recruiter", None, "a@x.com")}


def test_a_byte_order_mark_and_odd_header_spelling_are_tolerated():
    text = "﻿" + sheet(
        ["a@x.com", "Ann", "Recruiter", "", "", ""],
        header=["email id", "NAME", "position", "reports to", "vertical", "Head"],
    )
    assert tree(oh.plan_import(text)) == {"Ann": ("recruiter", None, "a@x.com")}


def test_columns_can_be_in_any_order_and_extra_columns_are_ignored():
    text = sheet(
        ["Ann", "ignored", "Recruiter", "a@x.com"],
        header=["Name", "Phone", "Positions", "Email. ID"],
    )
    assert tree(oh.plan_import(text)) == {"Ann": ("recruiter", None, "a@x.com")}


@pytest.mark.parametrize("text", ["", "   \n", "Foo,Bar\n1,2\n"])
def test_a_file_without_a_name_or_email_column_is_a_blocking_error(text):
    plan = oh.plan_import(text)
    assert plan.blocking and not plan.members
    assert messages(plan, "error")


def test_a_header_only_file_has_nobody_to_import_and_is_blocking():
    plan = oh.plan_import(",".join(HEADER))
    assert plan.blocking and plan.members == []


# ---------------------------------------------------------------------------
# Strict ranks make cycles impossible
# ---------------------------------------------------------------------------

def test_two_people_naming_each_other_cannot_form_a_cycle():
    plan = oh.plan_import(sheet(
        ["a@x.com", "Ann", "Resource Manager", "Bob", "", ""],
        ["b@x.com", "Bob", "Resource Manager", "Ann", "", ""],
    ))
    t = tree(plan)
    assert t["Ann"][1] is None and t["Bob"][1] is None
    assert len(messages(plan, "warning")) == 2


def test_every_planned_parent_is_strictly_higher_than_its_report():
    plan = oh.plan_import(REAL_SHAPE)
    by_key = {m.key: m for m in plan.members}
    for member in plan.members:
        if member.parent_key:
            assert oh.rank_of(by_key[member.parent_key].role) > oh.rank_of(member.role)


# ---------------------------------------------------------------------------
# Export round-trips
# ---------------------------------------------------------------------------

def test_exporting_the_tree_and_importing_it_back_rebuilds_the_same_tree():
    first = oh.plan_import(REAL_SHAPE)
    exported = oh.export_csv(oh.members_from_plan(first))
    second = oh.plan_import(exported)

    assert tree(second) == tree(first)
    assert second.issues == [] or all(i.severity == "info" for i in second.issues)
    first_vertical = {m.name: m.vertical for m in first.members}
    assert {m.name: m.vertical for m in second.members} == first_vertical


def test_the_export_has_the_sheets_own_columns_and_blank_emails_to_fill_in():
    exported = oh.export_csv(oh.members_from_plan(oh.plan_import(REAL_SHAPE)))
    rows = list(csv.reader(io.StringIO(exported)))
    assert tuple(rows[0]) == oh.EXPORT_HEADER
    body = {r[1]: r for r in rows[1:]}
    # Named-only managers are rows now, email blank, ready to be filled.
    assert body["Nina Nomail"][0] == "" and body["Nina Nomail"][2] == "Resource Manager"
    # Someone whose manager has an email is pointed at that email (unambiguous).
    assert body["Ann Recruiter"][3] == "rita@x.com"
    # Where the manager has none, the name is used.
    assert body["Bob Recruiter"][3] == "Nina Nomail"
    # Recruiters keep their original title text.
    assert body["Cy Graduate"][2] == "Step - Graduate"
    # Heads come first.
    assert rows[1][2] == "Delivery Director"


def test_filling_in_an_email_in_the_export_gives_that_manager_a_scope():
    exported = oh.export_csv(oh.members_from_plan(oh.plan_import(REAL_SHAPE)))
    # The email is the first column, so a blank one makes the line start ",Nina Nomail,...".
    assert "\n,Nina Nomail," in exported
    filled = exported.replace("\n,Nina Nomail,", "\nnina@x.com,Nina Nomail,", 1)
    plan = oh.plan_import(filled)
    nina = next(m for m in plan.members if m.name == "Nina Nomail")
    assert nina.email == "nina@x.com"
    members = oh.members_from_plan(plan)
    nina_id = next(m.id for m in members if m.email == "nina@x.com")
    scope = oh.compute_scope(members, nina_id)
    assert scope["emails"] == ["bob@x.com", "cy@x.com", "nina@x.com"]


# ---------------------------------------------------------------------------
# Scope computation
# ---------------------------------------------------------------------------

def _m(id, name, role, parent=None, email="auto"):
    return Member(
        id=id, name=name, role=role, reports_to_id=parent,
        email=(f"{name.lower().replace(' ', '.')}@x.com" if email == "auto" else email),
    )


#   avp(1)
#    └─ dd(2)
#        ├─ dm(3)
#        │   └─ rm1(4)
#        │       ├─ rec1(6)
#        │       └─ rec2(7)
#        └─ rm2(5)
#            └─ rec3(8)
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


def test_a_person_sees_themself_and_everyone_beneath_at_any_depth():
    scope = oh.compute_scope(ORG, 2)  # the Delivery Director
    assert scope["emails"] == sorted([
        "dan.dd@x.com", "dee.dm@x.com", "rita.rm@x.com", "rob.rm@x.com",
        "ann.rec@x.com", "bob.rec@x.com", "cat.rec@x.com",
    ])
    # The AVP above is not visible downwards... and the AVP sees everyone.
    assert "vera.avp@x.com" not in scope["emails"]
    assert len(oh.compute_scope(ORG, 1)["emails"]) == 8


def test_nobody_sees_sideways_or_upwards():
    rita = oh.compute_scope(ORG, 4)["emails"]
    assert rita == ["ann.rec@x.com", "bob.rec@x.com", "rita.rm@x.com"]
    assert "rob.rm@x.com" not in rita and "cat.rec@x.com" not in rita  # the sibling RM's team
    assert "dee.dm@x.com" not in rita  # their own manager
    assert oh.compute_scope(ORG, 6)["emails"] == ["ann.rec@x.com"]  # a recruiter: just themself


def test_scope_splits_managers_from_recruiters_for_the_productivity_tab():
    scope = oh.compute_scope(ORG, 2)
    assert scope["lead_emails"] == ["dan.dd@x.com", "dee.dm@x.com", "rita.rm@x.com", "rob.rm@x.com"]
    assert scope["member_emails"] == ["ann.rec@x.com", "bob.rec@x.com", "cat.rec@x.com"]


def test_people_without_an_email_belong_to_the_tree_but_not_to_the_email_scope():
    members = [
        _m(1, "Boss", "delivery_director", email=None),
        _m(2, "Rita", "resource_manager", 1),
        _m(3, "Ann", "recruiter", 2),
    ]
    scope = oh.compute_scope(members, 1)
    assert scope["emails"] == ["ann@x.com", "rita@x.com"]
    assert len(scope["people"]) == 3
    assert {m.name for m in oh.descendants(members, 1)} == {"Rita", "Ann"}


def test_an_unknown_root_has_no_scope():
    assert oh.compute_scope(ORG, 999) is None


def test_a_corrupt_cycle_cannot_hang_a_request():
    corrupt = [_m(1, "A", "resource_manager", 2), _m(2, "B", "resource_manager", 1)]
    assert {m.id for m in oh.descendants(corrupt, 1)} == {2}
    assert set(oh.compute_scope(corrupt, 1)["emails"]) == {"a@x.com", "b@x.com"}


def test_duplicate_descendants_are_listed_once():
    members = [_m(1, "Boss", "avp"), _m(2, "Mid", "resource_manager", 1), _m(3, "Rec", "recruiter", 2)]
    assert [m.id for m in oh.descendants(members, 1)].count(3) == 1


def test_parse_org_part_only_accepts_the_org_prefix():
    assert oh.parse_org_part("org:12") == 12
    assert oh.parse_org_part(" ORG:7 ") == 7
    assert oh.parse_org_part("org:abc") is None
    assert oh.parse_org_part("9f3c0a") is None  # a Teams-page team id
    assert oh.parse_org_part("") is None


def test_build_overview_counts_reports_and_emails():
    overview = oh.build_overview(ORG + [_m(9, "Pending Pat", "recruiter", 5, email=None)])
    by_name = {m["name"]: m for m in overview["members"]}
    # Dan (id 2): Dee, Rita, Rob, Ann, Bob, Cat + the pending recruiter under Rob.
    assert by_name["Dan Dd"]["direct_reports"] == 2 and by_name["Dan Dd"]["total_reports"] == 7
    assert by_name["Vera Avp"]["total_reports"] == 8  # everyone else
    assert by_name["Ann Rec"]["direct_reports"] == 0
    assert overview["counts"]["total"] == 9
    assert overview["counts"]["without_email"] == 1
    assert overview["counts"]["by_role"]["recruiter"] == 4
    assert [lvl["role"] for lvl in overview["levels"]] == list(oh.LEVELS)
