"""Team management storage.

Teams group recruiters under one or more team leads:
  - `teams`         — one row per team (id is a uuid4 hex generated here;
                      no gen_random_uuid() dependency on the DB).
  - `team_members`  — one row per (team, email) with member_role
                      'lead' | 'member'.

Membership is exclusive: a person belongs to at most ONE team (as lead or
member), enforced by the unique index on LOWER(email). Cross-table links are
logical only (no FK), matching every other relationship in this schema.

Role interplay (see core/auth.py): ADMIN_EMAILS / user_roles admins stay
'admin' even when they lead a team; a non-admin lead resolves to 'team_lead'.
"""

import asyncio
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Union

from core.db import get_db_connection

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ensure_teams_schema() -> None:
    """Idempotent DDL, called once at startup from main.py lifespan."""
    conn = get_db_connection()
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_teams_name_lower ON teams (LOWER(name))"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS team_members (
                    id SERIAL PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    email TEXT NOT NULL,
                    member_role TEXT NOT NULL DEFAULT 'member',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # One team per person — the product decision, enforced at the DB
            # level so concurrent admin writes cannot race past the app check.
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_team_members_email_lower ON team_members (LOWER(email))"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_team_members_team ON team_members (team_id)"
            )
        logger.info("teams schema ready")
    finally:
        conn.close()


async def init_teams_schema() -> None:
    """Async wrapper — main.py lifespan awaits this with a timeout."""
    await asyncio.to_thread(_ensure_teams_schema)


def parse_email_list(raw: Union[str, List[str], None]) -> List[str]:
    """Normalize a comma/semicolon/newline-separated string (or list) of
    emails: lowercase, strip, dedupe preserving order. Raises ValueError on
    malformed entries so admins get a precise message instead of silently
    dropping a typo'd teammate."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,;\n]", raw)
    else:
        parts = []
        for item in raw:
            parts.extend(re.split(r"[,;\n]", str(item)))
    seen: List[str] = []
    bad: List[str] = []
    for p in parts:
        email = p.strip().lower()
        if not email:
            continue
        if not _EMAIL_RE.match(email):
            bad.append(p.strip())
            continue
        if email not in seen:
            seen.append(email)
    if bad:
        raise ValueError(f"Invalid email address(es): {', '.join(bad)}")
    return seen


def _normalize_membership(
    lead_emails: Union[str, List[str], None],
    member_emails: Union[str, List[str], None],
) -> List[Dict[str, str]]:
    """Returns [{email, member_role}] with leads winning duplicates."""
    leads = parse_email_list(lead_emails)
    members = parse_email_list(member_emails)
    if not leads:
        raise ValueError("A team needs at least one team lead email.")
    rows = [{"email": e, "member_role": "lead"} for e in leads]
    rows += [{"email": e, "member_role": "member"} for e in members if e not in leads]
    return rows


def _fetch_conflicts(cur, emails: List[str], exclude_team_id: Optional[str]) -> List[str]:
    """Emails already claimed by ANOTHER team ('one team per person')."""
    if not emails:
        return []
    if exclude_team_id:
        cur.execute(
            """
            SELECT tm.email, t.name FROM team_members tm
            JOIN teams t ON t.id = tm.team_id
            WHERE LOWER(tm.email) = ANY(%s) AND tm.team_id <> %s
            """,
            (emails, exclude_team_id),
        )
    else:
        cur.execute(
            """
            SELECT tm.email, t.name FROM team_members tm
            JOIN teams t ON t.id = tm.team_id
            WHERE LOWER(tm.email) = ANY(%s)
            """,
            (emails,),
        )
    return [f"{row[0]} (already in team '{row[1]}')" for row in cur.fetchall()]


def _team_row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "name": row[1],
        "created_by": row[2] or "",
        "created_at": row[3].isoformat() if hasattr(row[3], "isoformat") else row[3],
        "updated_at": row[4].isoformat() if hasattr(row[4], "isoformat") else row[4],
        "lead_emails": [],
        "member_emails": [],
    }


def _attach_members(cur, teams_by_id: Dict[str, Dict[str, Any]]) -> None:
    if not teams_by_id:
        return
    cur.execute(
        "SELECT team_id, email, member_role FROM team_members WHERE team_id = ANY(%s) ORDER BY id",
        (list(teams_by_id.keys()),),
    )
    for team_id, email, member_role in cur.fetchall():
        team = teams_by_id.get(team_id)
        if team is None:
            continue
        if str(member_role).strip().lower() == "lead":
            team["lead_emails"].append(email)
        else:
            team["member_emails"].append(email)


def list_teams() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, created_by, created_at, updated_at FROM teams ORDER BY LOWER(name)"
            )
            teams = [_team_row_to_dict(r) for r in cur.fetchall()]
            _attach_members(cur, {t["id"]: t for t in teams})
    return teams


def get_team(team_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, created_by, created_at, updated_at FROM teams WHERE id = %s",
                (str(team_id),),
            )
            row = cur.fetchone()
            if not row:
                return None
            team = _team_row_to_dict(row)
            _attach_members(cur, {team["id"]: team})
    return team


def get_team_for_email(email: str) -> Optional[Dict[str, Any]]:
    """Resolve the (single) team an email belongs to, or None.

    Returns {"team_id", "team_name", "member_role"} — used by auth to derive
    the 'team_lead' role and by job filtering to find a lead's team scope.
    """
    clean = (email or "").strip().lower()
    if not clean:
        return None
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tm.team_id, t.name, tm.member_role
                FROM team_members tm
                JOIN teams t ON t.id = tm.team_id
                WHERE LOWER(tm.email) = %s
                LIMIT 1
                """,
                (clean,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "team_id": row[0],
        "team_name": row[1],
        "member_role": str(row[2] or "member").strip().lower(),
    }


def get_team_member_emails(team_id: str) -> List[str]:
    """All emails on a team — leads AND members — lowercased."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT LOWER(email) FROM team_members WHERE team_id = %s ORDER BY id",
                (str(team_id),),
            )
            return [r[0] for r in cur.fetchall()]


def create_team(
    name: str,
    lead_emails: Union[str, List[str], None],
    member_emails: Union[str, List[str], None],
    created_by: str = "",
) -> Dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Team name is required.")
    membership = _normalize_membership(lead_emails, member_emails)
    team_id = uuid.uuid4().hex

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM teams WHERE LOWER(name) = %s", (clean_name.lower(),))
            if cur.fetchone():
                raise ValueError(f"A team named '{clean_name}' already exists.")
            conflicts = _fetch_conflicts(cur, [m["email"] for m in membership], None)
            if conflicts:
                raise ValueError(
                    "Each person can only belong to one team: " + "; ".join(conflicts)
                )
            cur.execute(
                "INSERT INTO teams (id, name, created_by) VALUES (%s, %s, %s)",
                (team_id, clean_name, (created_by or "").strip().lower()),
            )
            for m in membership:
                cur.execute(
                    "INSERT INTO team_members (team_id, email, member_role) VALUES (%s, %s, %s)",
                    (team_id, m["email"], m["member_role"]),
                )
    created = get_team(team_id)
    logger.info(
        "team created: %s (%s) leads=%d members=%d",
        clean_name, team_id,
        len(created["lead_emails"]), len(created["member_emails"]),
    )
    return created


def update_team(
    team_id: str,
    name: str,
    lead_emails: Union[str, List[str], None],
    member_emails: Union[str, List[str], None],
) -> Dict[str, Any]:
    """Full replace of name + membership (the edit modal always sends both)."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Team name is required.")
    membership = _normalize_membership(lead_emails, member_emails)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM teams WHERE id = %s", (str(team_id),))
            if not cur.fetchone():
                raise LookupError(f"Team '{team_id}' not found.")
            cur.execute(
                "SELECT 1 FROM teams WHERE LOWER(name) = %s AND id <> %s",
                (clean_name.lower(), str(team_id)),
            )
            if cur.fetchone():
                raise ValueError(f"A team named '{clean_name}' already exists.")
            conflicts = _fetch_conflicts(cur, [m["email"] for m in membership], str(team_id))
            if conflicts:
                raise ValueError(
                    "Each person can only belong to one team: " + "; ".join(conflicts)
                )
            cur.execute(
                "UPDATE teams SET name = %s, updated_at = NOW() WHERE id = %s",
                (clean_name, str(team_id)),
            )
            cur.execute("DELETE FROM team_members WHERE team_id = %s", (str(team_id),))
            for m in membership:
                cur.execute(
                    "INSERT INTO team_members (team_id, email, member_role) VALUES (%s, %s, %s)",
                    (str(team_id), m["email"], m["member_role"]),
                )
    return get_team(str(team_id))


def delete_team(team_id: str) -> bool:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM team_members WHERE team_id = %s", (str(team_id),))
            cur.execute("DELETE FROM teams WHERE id = %s", (str(team_id),))
            deleted = cur.rowcount > 0
    if deleted:
        logger.info("team deleted: %s", team_id)
    return deleted
