"""Org hierarchy: who reports to whom, and so whose jobs and analytics each person sees.

Levels, lowest to highest:

    Recruiter → Resource Manager → Delivery Manager → Delivery Director → AVP

A person sees their own jobs plus those of everyone beneath them, at any depth,
and — from Resource Manager up — gets the admin analytics pages scoped to that
same population (core/auth.py: get_user_scope_emails / resolve_report_scope).
Nobody sees sideways or upwards.

The tree comes from the HR mapping sheet (``plan_import``). Each import
REPLACES the previous tree in one transaction, so the sheet stays the single
source of truth and there is nothing to reconcile by hand.

  - ``org_members``           one row per person. ``email`` is NULL until known:
                              the sheet names most managers but gives only the
                              recruiters' emails, and an email-less manager still
                              belongs in the tree (it keeps their reports grouped)
                              — they simply cannot sign in with a scope yet.
  - ``org_hierarchy_imports`` audit trail of who replaced the tree, and when.

Why not ``teams``? Teams are flat — one level, one team per person. This is a
tree with typed levels. The two coexist: a person's scope is the UNION of both
(core/auth.py), so introducing the hierarchy never takes access away.

Safety rules baked in here:
  * A manager must sit strictly above their report (rank), so cycles cannot exist.
  * A title this module does not recognise becomes Recruiter — least privilege.
    A typo can hide someone's team; it can never hand out visibility.
  * An email that does not parse is treated as "no email", never guessed.
"""

import asyncio
import csv
import io
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from core.db import get_db_connection

logger = logging.getLogger(__name__)

RECRUITER = "recruiter"
RESOURCE_MANAGER = "resource_manager"
DELIVERY_MANAGER = "delivery_manager"
DELIVERY_DIRECTOR = "delivery_director"
AVP = "avp"

# Lowest → highest. The rank of a level is its 1-based position here.
LEVELS: Tuple[str, ...] = (RECRUITER, RESOURCE_MANAGER, DELIVERY_MANAGER, DELIVERY_DIRECTOR, AVP)
LEVEL_RANK: Dict[str, int] = {level: i for i, level in enumerate(LEVELS, start=1)}
LEVEL_LABELS: Dict[str, str] = {
    RECRUITER: "Recruiter",
    RESOURCE_MANAGER: "Resource Manager",
    DELIVERY_MANAGER: "Delivery Manager",
    DELIVERY_DIRECTOR: "Delivery Director",
    AVP: "AVP",
}

# A "Reporting Manager" named in the sheet with no row of their own manages
# recruiters, so they are a Resource Manager. A "Regional/Vertical Head" with no
# row is the open question: the sheet does not say which level a head is, so it
# is an import option (the admin picks), defaulting to Delivery Director.
DEFAULT_MANAGER_ROLE = RESOURCE_MANAGER
DEFAULT_HEAD_ROLE = DELIVERY_DIRECTOR

# Scope keys: `org:<member id>` is "that person and everyone under them". The
# report endpoints join several keys with "+" (see routers/_helpers._load_team_scope).
ORG_SCOPE_PREFIX = "org:"

# Serialises concurrent imports across the API's 8 workers.
_IMPORT_LOCK_KEY = 7_204_117

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def rank_of(role: Optional[str]) -> int:
    """Rank of a level; an unknown role is the lowest (least privilege)."""
    return LEVEL_RANK.get((role or "").strip().lower(), 1)


def normalize_role(role: Optional[str]) -> str:
    clean = (role or "").strip().lower()
    return clean if clean in LEVEL_RANK else RECRUITER


def role_label(role: Optional[str]) -> str:
    return LEVEL_LABELS[normalize_role(role)]


def level_above(role: str) -> Optional[str]:
    index = LEVELS.index(normalize_role(role))
    return LEVELS[index + 1] if index + 1 < len(LEVELS) else None


def _higher(a: str, b: str) -> str:
    return a if rank_of(a) >= rank_of(b) else b


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _ensure_schema() -> None:
    """Idempotent DDL, called once at startup from main.py lifespan."""
    conn = get_db_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS org_members (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    email TEXT,
                    role TEXT NOT NULL,
                    reports_to_id INTEGER,
                    vertical TEXT,
                    title TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # One person per email (a NULL email is "not known yet" and may repeat).
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_org_members_email_lower "
                "ON org_members (LOWER(email)) WHERE email IS NOT NULL"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_org_members_reports_to ON org_members (reports_to_id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS org_hierarchy_imports (
                    id SERIAL PRIMARY KEY,
                    imported_by TEXT,
                    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    summary TEXT
                )
                """
            )
        logger.info("org hierarchy schema ready")
    finally:
        conn.close()


async def init_org_hierarchy_schema() -> None:
    """Async wrapper — main.py lifespan awaits this with a timeout."""
    await asyncio.to_thread(_ensure_schema)


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    id: int
    name: str
    email: Optional[str]
    role: str
    reports_to_id: Optional[int] = None
    vertical: str = ""
    title: str = ""


def _row_to_member(row) -> Member:
    return Member(
        id=int(row[0]),
        name=row[1] or "",
        email=(row[2] or "").strip().lower() or None,
        role=normalize_role(row[3]),
        reports_to_id=int(row[4]) if row[4] is not None else None,
        vertical=row[5] or "",
        title=row[6] or "",
    )


def list_members() -> List[Member]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, role, reports_to_id, vertical, title FROM org_members ORDER BY id"
            )
            return [_row_to_member(r) for r in cur.fetchall()]


def get_membership(email: str) -> Optional[Dict[str, Any]]:
    """The caller's own node, for identity resolution (one cheap query per request).

    Returns {"id", "name", "role", "has_reports"} or None.
    """
    clean = (email or "").strip().lower()
    if not clean:
        return None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.name, m.role,
                       EXISTS (SELECT 1 FROM org_members c WHERE c.reports_to_id = m.id)
                FROM org_members m
                WHERE LOWER(m.email) = %s
                LIMIT 1
                """,
                (clean,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": int(row[0]),
        "name": row[1] or "",
        "role": normalize_role(row[2]),
        "has_reports": bool(row[3]),
    }


def children_index(members: Iterable[Member]) -> Dict[int, List[Member]]:
    index: Dict[int, List[Member]] = defaultdict(list)
    for member in members:
        if member.reports_to_id is not None:
            index[member.reports_to_id].append(member)
    return index


def descendants(members: List[Member], root_id: int) -> List[Member]:
    """Everyone beneath ``root_id`` at any depth (the root excluded).

    Cycle-safe by construction (a visited set), so a corrupt row can never make
    a request spin — ranks already forbid cycles on write.
    """
    index = children_index(members)
    seen: Set[int] = {root_id}
    out: List[Member] = []
    stack = list(index.get(root_id, []))
    while stack:
        member = stack.pop()
        if member.id in seen:
            continue
        seen.add(member.id)
        out.append(member)
        stack.extend(index.get(member.id, []))
    return out


def compute_scope(members: List[Member], root_id: int) -> Optional[Dict[str, Any]]:
    """A person's organisation: themself plus everyone under them.

    ``emails`` is every known email in it; ``lead_emails`` are the people who
    manage others (any level above Recruiter) and ``member_emails`` the
    recruiters, which is the split the Dashboard's Productivity tab averages over.
    """
    root = next((m for m in members if m.id == root_id), None)
    if root is None:
        return None
    people = [root] + descendants(members, root_id)
    emails = sorted({m.email for m in people if m.email})
    leads = sorted({m.email for m in people if m.email and m.role != RECRUITER})
    recruiters = sorted({m.email for m in people if m.email and m.role == RECRUITER})
    return {
        "root": root,
        "people": people,
        "emails": emails,
        "lead_emails": leads,
        "member_emails": recruiters,
    }


def get_scope(member_id: int) -> Optional[Dict[str, Any]]:
    return compute_scope(list_members(), int(member_id))


def scope_emails(member_id: int) -> Set[str]:
    """Every known email in the member's organisation (themself included).
    Empty when the member no longer exists."""
    scope = get_scope(member_id)
    return set(scope["emails"]) if scope else set()


def parse_org_part(part: str) -> Optional[int]:
    """`org:12` → 12; anything else (a legacy team id, garbage) → None."""
    text = (part or "").strip()
    if not text.lower().startswith(ORG_SCOPE_PREFIX):
        return None
    try:
        return int(text[len(ORG_SCOPE_PREFIX):])
    except ValueError:
        return None


def load_scope_part(part: str) -> Dict[str, Any]:
    """Resolve one ``org:<id>`` scope part to {name, lead_emails, member_emails}.

    Raises LookupError for an unknown or malformed part, which the report
    endpoints translate to 404 — never a zeroed dashboard.
    """
    member_id = parse_org_part(part)
    if member_id is None:
        raise LookupError(f"Scope '{part}' not found.")
    scope = get_scope(member_id)
    if scope is None:
        raise LookupError(f"Scope '{part}' not found.")
    root: Member = scope["root"]
    return {
        "name": f"{root.name} ({role_label(root.role)})",
        "lead_emails": scope["lead_emails"],
        "member_emails": scope["member_emails"],
    }


def build_overview(members: List[Member]) -> Dict[str, Any]:
    """The whole tree as a flat list plus per-person report counts (admin page)."""
    index = children_index(members)

    def total_under(member_id: int) -> int:
        seen: Set[int] = {member_id}
        stack = [c.id for c in index.get(member_id, [])]
        count = 0
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            count += 1
            stack.extend(c.id for c in index.get(current, []))
        return count

    rows = [
        {
            "id": m.id,
            "name": m.name,
            "email": m.email,
            "role": m.role,
            "role_label": role_label(m.role),
            "reports_to_id": m.reports_to_id,
            "vertical": m.vertical,
            "title": m.title,
            "direct_reports": len(index.get(m.id, [])),
            "total_reports": total_under(m.id),
        }
        for m in members
    ]
    by_role = Counter(m.role for m in members)
    return {
        "members": rows,
        "levels": [{"role": level, "label": LEVEL_LABELS[level], "rank": LEVEL_RANK[level]} for level in LEVELS],
        "counts": {
            "total": len(members),
            "with_email": sum(1 for m in members if m.email),
            "without_email": sum(1 for m in members if not m.email),
            "by_role": {level: by_role.get(level, 0) for level in LEVELS},
        },
    }


# ---------------------------------------------------------------------------
# Importing the mapping sheet
# ---------------------------------------------------------------------------
#
# The sheet is one row per person. Recognised columns (header spelling and
# punctuation are ignored, so "Email. ID" and "email id" both work):
#
#   Email. ID | Name | Positions | Reporting Manager | Regional/Vertical Structure | Regional/Vertical Head
#
# How a row becomes a place in the tree:
#   * Positions → level. Only an exact level name (or a close synonym) is
#     trusted; anything else — "Step - Graduate", "Executive Resourcing" — is a
#     Recruiter.
#   * Reporting Manager → the person's manager, matched by EMAIL if the cell
#     holds one, otherwise by NAME (case and spacing ignored). A manager with no
#     row of their own is kept as an email-less person so their reports stay
#     grouped. A manager equal to the person themself means "none stated" (the
#     sheet writes a Resource Manager as their own manager).
#   * Regional/Vertical Head → the fallback manager, used when no manager is
#     stated, and the manager of any Reporting Manager the sheet has no row for.
#   * To add Delivery Managers / Directors / AVPs, add a row for each with that
#     Positions value, and point their reports' Reporting Manager at them.

_ALIASES: Dict[str, Set[str]] = {
    "email": {"email", "emailid", "emailaddress", "workemail", "officialemail", "mail"},
    "name": {"name", "fullname", "employeename", "employee", "personname"},
    "title": {"positions", "position", "role", "designation", "jobtitle", "title", "level"},
    "manager": {
        "reportingmanager", "reportsto", "reportingto", "manager", "manageremail",
        "reportingmanageremail", "supervisor",
    },
    "vertical": {
        "regionalverticalstructure", "verticalstructure", "vertical", "region",
        "regionalvertical", "structure",
    },
    "head": {"regionalverticalhead", "verticalhead", "regionalhead", "head", "regionhead"},
}

_TITLE_RULES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (AVP, re.compile(r"\bavp\b|\bassistant vice president\b|\bvice president\b")),
    (DELIVERY_DIRECTOR, re.compile(r"\bdelivery director\b")),
    (DELIVERY_MANAGER, re.compile(r"\bdelivery manager\b")),
    (
        RESOURCE_MANAGER,
        re.compile(r"\bresource manager\b|\bresourcing manager\b|\brecruiting manager\b|\brecruitment manager\b"),
    ),
)


def level_for_title(title: Optional[str]) -> Tuple[str, bool]:
    """(level, recognised) for a Positions cell.

    Unrecognised → (Recruiter, False): least privilege, and the caller surfaces
    the title so an admin can correct the spelling if it was meant to be higher.
    """
    text = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    if not text:
        return RECRUITER, False
    for role, pattern in _TITLE_RULES:
        if pattern.search(text):
            return role, True
    return RECRUITER, bool(re.search(r"\brecruiter\b", text))


def name_key(name: Optional[str]) -> str:
    """Matching key for a person's name: case and spacing ignored."""
    return re.sub(r"\s+", " ", (name or "")).strip().lower()


def _clean_name(name: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (name or "")).strip()


def _clean_email(raw: Optional[str]) -> str:
    text = (raw or "").strip().lower()
    return text if _EMAIL_RE.match(text) else ""


def _name_from_email(email: str) -> str:
    local = email.split("@", 1)[0]
    words = re.split(r"[._\-\d]+", local)
    return " ".join(w.capitalize() for w in words if w) or email


@dataclass
class ImportIssue:
    severity: str  # 'error' blocks the import; 'warning' / 'info' do not
    message: str
    row: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "row": self.row, "message": self.message}


@dataclass
class PlannedMember:
    key: str
    name: str
    email: Optional[str]
    role: str
    title: str = ""
    vertical: str = ""
    parent_key: Optional[str] = None
    # False → a manager/head the sheet names but gives no row for.
    from_sheet: bool = True
    line: Optional[int] = None


@dataclass
class ImportPlan:
    members: List[PlannedMember] = field(default_factory=list)
    issues: List[ImportIssue] = field(default_factory=list)
    rows: int = 0
    unrecognised_titles: Counter = field(default_factory=Counter)

    @property
    def blocking(self) -> bool:
        return any(i.severity == "error" for i in self.issues) or not self.members

    def summary(self) -> Dict[str, Any]:
        kids: Dict[str, List[str]] = defaultdict(list)
        for m in self.members:
            if m.parent_key:
                kids[m.parent_key].append(m.key)

        def total_under(key: str) -> int:
            seen = {key}
            stack = list(kids.get(key, []))
            count = 0
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                count += 1
                stack.extend(kids.get(current, []))
            return count

        by_role = Counter(m.role for m in self.members)
        needs_email = sorted(
            (
                {
                    "name": m.name,
                    "role": m.role,
                    "role_label": role_label(m.role),
                    "vertical": m.vertical,
                    "direct_reports": len(kids.get(m.key, [])),
                    "total_reports": total_under(m.key),
                    "named_only": not m.from_sheet,
                }
                for m in self.members
                if not m.email
            ),
            key=lambda row: (-row["total_reports"], row["name"].lower()),
        )
        top_level = [
            {"name": m.name, "email": m.email, "role": m.role, "role_label": role_label(m.role),
             "vertical": m.vertical, "total_reports": total_under(m.key)}
            for m in self.members
            if m.parent_key is None and m.key in kids
        ]
        top_level.sort(key=lambda row: (-rank_of(row["role"]), -row["total_reports"], row["name"].lower()))
        return {
            "rows": self.rows,
            "people": len(self.members),
            "with_email": sum(1 for m in self.members if m.email),
            "without_email": len(needs_email),
            "by_role": {level: by_role.get(level, 0) for level in LEVELS},
            "needs_email": needs_email,
            "top_level": top_level,
            "titles_treated_as_recruiter": [
                {"title": title, "count": count}
                for title, count in sorted(self.unrecognised_titles.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary(),
            "issues": [i.to_dict() for i in self.issues],
            "blocking": self.blocking,
        }


@dataclass
class _Node:
    planned: PlannedMember
    mgr: Optional["_Node"] = None
    head: Optional["_Node"] = None
    verticals: Counter = field(default_factory=Counter)
    # For a named-only manager: which heads their reports' rows name.
    heads_of_reports: Counter = field(default_factory=Counter)

    @property
    def role(self) -> str:
        return self.planned.role


def _read_table(text: str) -> Tuple[List[str], List[Tuple[int, List[str]]]]:
    """(header, [(sheet row number, cells)]) — comma, tab or semicolon separated."""
    body = (text or "").lstrip("﻿")
    first_line = next((ln for ln in body.splitlines() if ln.strip()), "")
    delimiter = max((",", "\t", ";"), key=first_line.count)
    rows = list(csv.reader(io.StringIO(body), delimiter=delimiter))
    numbered = [(i, r) for i, r in enumerate(rows, start=1) if any(c.strip() for c in r)]
    if not numbered:
        return [], []
    return numbered[0][1], numbered[1:]


def _resolve_columns(header: List[str]) -> Dict[str, int]:
    columns: Dict[str, int] = {}
    for index, raw in enumerate(header):
        norm = re.sub(r"[^a-z0-9]", "", (raw or "").lower())
        for field_name, aliases in _ALIASES.items():
            if norm in aliases and field_name not in columns:
                columns[field_name] = index
                break
    return columns


def plan_import(
    text: str,
    *,
    head_role: str = DEFAULT_HEAD_ROLE,
    manager_role: str = DEFAULT_MANAGER_ROLE,
) -> ImportPlan:
    """Turn the sheet (CSV/TSV text) into a tree, without touching the database.

    Pure, so the whole mapping policy is unit-tested and the admin can preview
    it before anything is replaced.
    """
    head_role = normalize_role(head_role)
    manager_role = normalize_role(manager_role)
    plan = ImportPlan()
    header, body = _read_table(text)
    columns = _resolve_columns(header)
    if not header or ("name" not in columns and "email" not in columns):
        plan.issues.append(ImportIssue(
            "error",
            "Could not find a Name or Email column. Expected headers like: "
            "Email. ID, Name, Positions, Reporting Manager, Regional/Vertical Structure, Regional/Vertical Head.",
        ))
        return plan

    def cell(cells: List[str], field_name: str) -> str:
        index = columns.get(field_name)
        return cells[index].strip() if index is not None and index < len(cells) else ""

    nodes: List[_Node] = []
    by_email: Dict[str, _Node] = {}
    by_name: Dict[str, List[_Node]] = defaultdict(list)
    sequence = 0

    def add_node(name: str, email: str, role: str, *, title: str = "", vertical: str = "",
                 from_sheet: bool = True, line: Optional[int] = None) -> _Node:
        nonlocal sequence
        sequence += 1
        planned = PlannedMember(
            key=f"e:{email}" if email else f"n:{sequence}",
            name=name,
            email=email or None,
            role=role,
            title=title,
            vertical=vertical,
            from_sheet=from_sheet,
            line=line,
        )
        node = _Node(planned=planned)
        nodes.append(node)
        if email:
            by_email[email] = node
        by_name[name_key(name)].append(node)
        return node

    # Pass 1 — one node per row.
    rows: List[Tuple[int, _Node, str, str]] = []  # (line, node, manager ref, head ref)
    for line, cells in body:
        plan.rows += 1
        raw_email = cell(cells, "email")
        email = _clean_email(raw_email)
        if raw_email and not email:
            plan.issues.append(ImportIssue(
                "warning", f"'{raw_email}' is not a valid email, so {cell(cells, 'name') or 'this person'} "
                           "was added without one.", line))
        name = _clean_name(cell(cells, "name")) or (_name_from_email(email) if email else "")
        if not name:
            plan.issues.append(ImportIssue("warning", "Row has neither a name nor an email; skipped.", line))
            continue
        if email and email in by_email:
            plan.issues.append(ImportIssue(
                "warning",
                f"{email} already appears on row {by_email[email].planned.line}; this row was skipped.", line))
            continue
        title = cell(cells, "title")
        role, recognised = level_for_title(title)
        if not recognised and title.strip():
            plan.unrecognised_titles[re.sub(r"\s+", " ", title.strip())] += 1
        vertical = cell(cells, "vertical")
        node = add_node(name, email, role, title=re.sub(r"\s+", " ", title.strip()),
                        vertical=vertical, line=line)
        if vertical:
            node.verticals[vertical] += 1
        rows.append((line, node, cell(cells, "manager"), cell(cells, "head")))

    ambiguous_reported: Set[str] = set()

    def resolve(ref: str, referrer: _Node, line: int, *, default_role: str) -> Optional[_Node]:
        """Find (or create, email-less) the person a manager/head cell names."""
        if not ref.strip():
            return None
        wanted = _clean_email(ref) if "@" in ref else ""
        if "@" in ref and not wanted:
            plan.issues.append(ImportIssue("warning", f"'{ref}' is not a valid email; ignored.", line))
            return None
        placeholder_role = _higher(default_role, level_above(referrer.role) or default_role)
        if wanted:
            target = by_email.get(wanted)
            if target is None:
                target = add_node(_name_from_email(wanted), wanted, placeholder_role, from_sheet=False)
        else:
            matches = by_name.get(name_key(ref), [])
            if len(matches) > 1:
                if name_key(ref) not in ambiguous_reported:
                    ambiguous_reported.add(name_key(ref))
                    plan.issues.append(ImportIssue(
                        "warning",
                        f"'{_clean_name(ref)}' matches {len(matches)} different people, so nobody was linked to "
                        "them by name. Put that manager's email in the Reporting Manager column instead.", line))
                return None
            target = matches[0] if matches else add_node(
                _clean_name(ref), "", placeholder_role, from_sheet=False)
        if target is referrer:
            return None
        if not target.planned.from_sheet:
            # Named-only people get the highest level any reference implies.
            target.planned.role = _higher(target.planned.role, placeholder_role)
        return target

    # Pass 2 — resolve every row's manager and head.
    for line, node, manager_ref, head_ref in rows:
        node.mgr = resolve(manager_ref, node, line, default_role=manager_role)
        node.head = resolve(head_ref, node, line, default_role=head_role)
        # A named-only person has no row of their own, so they borrow the
        # vertical of the rows that name them.
        for named in (node.mgr, node.head):
            if named is not None and not named.planned.from_sheet and node.planned.vertical:
                named.verticals[node.planned.vertical] += 1
        if node.mgr is not None and not node.mgr.planned.from_sheet and node.head is not None:
            node.mgr.heads_of_reports[node.head.planned.key] += 1

    # Named-only people have no row to state their own manager, so they take
    # the head their reports most often name.
    by_key = {n.planned.key: n for n in nodes}
    for node in nodes:
        if not node.planned.from_sheet and not node.planned.vertical and node.verticals:
            node.planned.vertical = node.verticals.most_common(1)[0][0]

    # Pass 3 — pick each person's parent. The manager must sit strictly above
    # them; otherwise fall back to the head, and say so.
    for node in nodes:
        if node.planned.from_sheet:
            candidates = [("Reporting Manager", node.mgr), ("Regional/Vertical Head", node.head)]
        else:
            top_head = node.heads_of_reports.most_common(1)
            head_node = by_key.get(top_head[0][0]) if top_head else None
            if head_node is not None and len(node.heads_of_reports) > 1:
                plan.issues.append(ImportIssue(
                    "warning",
                    f"{node.planned.name}'s reports name more than one Regional/Vertical Head "
                    f"({len(node.heads_of_reports)}); {head_node.planned.name} was used."))
            candidates = [("Regional/Vertical Head", head_node)]
        for label, candidate in candidates:
            if candidate is None or candidate is node:
                continue
            if rank_of(candidate.role) > rank_of(node.role):
                node.planned.parent_key = candidate.planned.key
                break
            plan.issues.append(ImportIssue(
                "warning",
                f"{node.planned.name} ({role_label(node.role)}) lists {candidate.planned.name} "
                f"({role_label(candidate.role)}) as {label}, but that is not a level above — "
                "that link was ignored.", node.planned.line))

    if plan.unrecognised_titles:
        total = sum(plan.unrecognised_titles.values())
        plan.issues.append(ImportIssue(
            "info",
            f"{total} people have a Positions value that is not a management level and were treated as "
            "Recruiters. If any should be a Resource Manager, Delivery Manager, Delivery Director or AVP, "
            "write that level in the Positions column."))
    plan.members = [n.planned for n in nodes]
    return plan


EXPORT_HEADER: Tuple[str, ...] = (
    "Email. ID",
    "Name",
    "Positions",
    "Reporting Manager",
    "Regional/Vertical Structure",
    "Regional/Vertical Head",
)


def members_from_plan(plan: ImportPlan) -> List[Member]:
    """A plan as Members with synthetic ids — what apply_plan would store."""
    ids = {m.key: i for i, m in enumerate(plan.members, start=1)}
    return [
        Member(
            id=ids[m.key],
            name=m.name,
            email=m.email,
            role=m.role,
            reports_to_id=ids.get(m.parent_key) if m.parent_key else None,
            vertical=m.vertical,
            title=m.title,
        )
        for m in plan.members
    ]


def export_csv(members: List[Member]) -> str:
    """The tree as a sheet in the importer's own format.

    Round-trips: importing this file rebuilds the same tree. Every person gets
    a row — including managers the original sheet only named, with a blank
    email to fill in — and their manager is written as the manager's EMAIL when
    known (unambiguous), otherwise their name.
    """
    by_id = {m.id: m for m in members}

    def manager_ref(member: Member) -> str:
        boss = by_id.get(member.reports_to_id) if member.reports_to_id is not None else None
        return (boss.email or boss.name) if boss else ""

    def head_name(member: Member) -> str:
        # Informational: the importer only uses this column when no manager is stated.
        seen = {member.id}
        boss = by_id.get(member.reports_to_id) if member.reports_to_id is not None else None
        while boss is not None and boss.id not in seen:
            if rank_of(boss.role) >= LEVEL_RANK[DELIVERY_DIRECTOR]:
                return boss.name
            seen.add(boss.id)
            boss = by_id.get(boss.reports_to_id) if boss.reports_to_id is not None else None
        return ""

    def position(member: Member) -> str:
        if member.role != RECRUITER:
            return LEVEL_LABELS[member.role]
        return member.title or LEVEL_LABELS[RECRUITER]

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPORT_HEADER)
    for member in sorted(members, key=lambda m: (-rank_of(m.role), (m.vertical or "").lower(), m.name.lower())):
        writer.writerow([
            member.email or "",
            member.name,
            position(member),
            manager_ref(member),
            member.vertical or "",
            head_name(member),
        ])
    return out.getvalue()


# ---------------------------------------------------------------------------
# Applying a plan
# ---------------------------------------------------------------------------

def current_emails() -> Set[str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT LOWER(email) FROM org_members WHERE email IS NOT NULL")
            return {r[0] for r in cur.fetchall() if r[0]}


def apply_plan(plan: ImportPlan, imported_by: str = "") -> Dict[str, Any]:
    """Replace the whole tree with the plan, atomically.

    Either every row lands or none does — a failed import leaves the previous
    tree (and everyone's access) exactly as it was.
    """
    if plan.blocking:
        raise ValueError("The import has errors; fix them and try again.")
    # Parents first: a manager's rank is strictly higher, so rank-descending
    # order guarantees every parent id exists before its reports are inserted.
    ordered = sorted(plan.members, key=lambda m: (-rank_of(m.role), m.name.lower(), m.key))
    ids: Dict[str, int] = {}
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_IMPORT_LOCK_KEY,))
            cur.execute("DELETE FROM org_members")
            for member in ordered:
                cur.execute(
                    """
                    INSERT INTO org_members (name, email, role, reports_to_id, vertical, title)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        member.name,
                        member.email,
                        member.role,
                        ids.get(member.parent_key) if member.parent_key else None,
                        member.vertical or None,
                        member.title or None,
                    ),
                )
                ids[member.key] = int(cur.fetchone()[0])
            summary = plan.summary()
            cur.execute(
                "INSERT INTO org_hierarchy_imports (imported_by, summary) VALUES (%s, %s)",
                (
                    (imported_by or "").strip().lower() or None,
                    json.dumps({
                        "rows": summary["rows"],
                        "people": summary["people"],
                        "with_email": summary["with_email"],
                        "without_email": summary["without_email"],
                        "by_role": summary["by_role"],
                    }),
                ),
            )
    logger.info(
        "org hierarchy replaced by %s: %d people (%d without email)",
        imported_by or "?", len(ordered), sum(1 for m in ordered if not m.email),
    )
    return {"people": len(ordered)}


def last_import() -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT imported_by, imported_at, summary FROM org_hierarchy_imports ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "imported_by": row[0] or "",
        "imported_at": row[1].isoformat() if hasattr(row[1], "isoformat") else row[1],
    }
