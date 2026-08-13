import logging
import hashlib
from typing import Dict, Any, List, Optional
from datetime import datetime

import psycopg2.extras

from core.db import get_db_connection
from services.location import sanitize_candidate_location

logger = logging.getLogger(__name__)


def _pick_valid_email(emails: List[Any]) -> Optional[str]:
    """From a list of emails prefer the first well-formed, non-placeholder one,
    falling back to the first value so we never drop a present contact entirely."""
    from services.jobdiva import _EMAIL_RE
    from utils.email_utils import is_placeholder_email

    cleaned = [str(e).strip() for e in emails if e and str(e).strip()]
    if not cleaned:
        return None
    well_formed = [e for e in cleaned if _EMAIL_RE.match(e.lower())]
    for e in well_formed:
        if not is_placeholder_email(e):
            return e
    return well_formed[0] if well_formed else cleaned[0]


# v23: Dropped the standalone SQLAlchemy engine (pool_size=5 + overflow=10)
# that used to live here. It was a second, fragmented pool — independent of
# the psycopg2 pool in core/db.py — that competed for the same Postgres
# `max_connections` budget without coordination. All writes now go through
# `get_db_connection()` so per-worker slot usage matches `_POOL_MAX=20`.


class CandidateProfilesDB:
    def __init__(self):
        # Kept for API compatibility; not used directly. Connection routing
        # now goes through core.db.get_db_connection().
        pass

    def _resolve_candidate_id(self, candidate: Dict[str, Any], source: str) -> str:
        cid = str(candidate.get("candidate_id") or candidate.get("id") or "")
        if cid and cid.lower() != "unknown" and cid.lower() != "none":
            return cid

        # Fallback for missing IDs
        identifier_str = f"{candidate.get('email', '')}|{candidate.get('phone', '')}|{candidate.get('name', '')}|{candidate.get('candidate_name', '')}"
        if identifier_str == "|||":
            return f"anon_{hashlib.md5(str(datetime.now().timestamp()).encode()).hexdigest()[:10]}"
        return f"{source.lower()}_{hashlib.md5(identifier_str.encode()).hexdigest()}"

    def upsert_candidate(self, jobdiva_id: str, candidate: Dict[str, Any], source: str = "JobDiva"):
        self.bulk_upsert_candidates(jobdiva_id, [candidate], source)

    def bulk_upsert_candidates(self, jobdiva_id: str, candidates: List[Dict[str, Any]], source: str = "JobDiva") -> int:
        """Upsert profile + child tables for a list of candidates, reusing
        ONE pool connection for the whole batch.

        Pre-fix this method opened a fresh `get_db_connection()` inside the
        per-candidate loop. With auto-sync feeding ~100 candidates per job
        and 8 workers contending on a 20-slot pool, those per-row borrows
        were the dominant source of pool churn — and a frequent trigger
        for the wizard's step-3 save hanging while waiting for a free
        connection slot.

        Now: one borrow per call, statement_timeout per candidate so a
        single misbehaving row can't stall the whole batch.

        Child-table writes (skills, education, positions) batch their own
        rows via `execute_values`, so each candidate still costs a small
        constant number of round-trips inside the shared connection.
        """
        if not candidates:
            return 0

        saved = 0
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Bound each candidate's child-table churn. Without this,
                # one unindexed lookup on a child table can stall the
                # batch.
                cur.execute("SET LOCAL lock_timeout = '2000ms'")
                cur.execute("SET LOCAL statement_timeout = '10000ms'")
                for c in candidates:
                    try:
                        cid = self._resolve_candidate_id(c, source)

                        # Normalize candidate fields
                        name = c.get("candidate_name") or c.get("name") or "Unknown"
                        parts = name.split(" ", 1)
                        first = parts[0]
                        last = parts[1] if len(parts) > 1 else ""

                        email = c.get("email")
                        if isinstance(email, list):
                            email = _pick_valid_email(email)
                        phone = c.get("phone")
                        if isinstance(phone, list):
                            phone = next(
                                (p for p in phone if p and any(ch.isdigit() for ch in str(p))),
                                (phone[0] if phone else None),
                            )

                        title = c.get("job_title") or c.get("title") or c.get("headline")
                        # Source-native location/city beats the LLM-parsed
                        # current_location, and work-arrangement strings
                        # ("Remote"/"Hybrid") are never a residence.
                        location = (
                            sanitize_candidate_location(c.get("location"))
                            or sanitize_candidate_location(c.get("city"))
                            or sanitize_candidate_location(c.get("current_location"))
                            or None
                        )

                        skill_source = "reported" if source != "JobDiva" else "predicted"
                        skills_raw = c.get("structured_skills") or c.get("skills") or []
                        edu_raw = c.get("candidate_education") or c.get("education") or []
                        exp_raw = c.get("company_experience") or c.get("experience") or []

                        # 1. UPSERT PROFILE
                        cur.execute(
                            """
                            INSERT INTO candidate_profiles (
                                candidate_id, source, firstname, lastname, fullname,
                                profile_title, work_email, phone, user_location
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (candidate_id) DO UPDATE SET
                                source = EXCLUDED.source,
                                firstname = COALESCE(EXCLUDED.firstname, candidate_profiles.firstname),
                                lastname = COALESCE(EXCLUDED.lastname, candidate_profiles.lastname),
                                fullname = COALESCE(EXCLUDED.fullname, candidate_profiles.fullname),
                                profile_title = COALESCE(EXCLUDED.profile_title, candidate_profiles.profile_title),
                                work_email = COALESCE(EXCLUDED.work_email, candidate_profiles.work_email),
                                phone = COALESCE(EXCLUDED.phone, candidate_profiles.phone),
                                user_location = COALESCE(EXCLUDED.user_location, candidate_profiles.user_location),
                                updated_at = CURRENT_TIMESTAMP
                            """,
                            (cid, source, first, last, name, title, email, phone, location),
                        )

                        # 2. UPSERT JOB LINK
                        if jobdiva_id:
                            cur.execute(
                                """
                                INSERT INTO sourced_candidate_jobs (
                                    jobdiva_id, candidate_id, source, resume_id
                                )
                                VALUES (%s, %s, %s, %s)
                                ON CONFLICT (jobdiva_id, candidate_id, source) DO NOTHING
                                """,
                                (jobdiva_id, cid, source, c.get("resume_id")),
                            )

                        # 3. SKILLS — DELETE + bulk INSERT via execute_values
                        if skills_raw:
                            cur.execute("DELETE FROM candidate_skills WHERE candidate_id = %s", (cid,))
                            skill_rows: List[tuple] = []
                            for s in skills_raw:
                                s_name = (
                                    s.get("skill") or s.get("name")
                                    if isinstance(s, dict)
                                    else s
                                )
                                if s_name:
                                    name_str = str(s_name)[:255]
                                    skill_rows.append((cid, name_str, name_str, skill_source))
                            if skill_rows:
                                psycopg2.extras.execute_values(
                                    cur,
                                    "INSERT INTO candidate_skills (candidate_id, skill_raw, skill_mapped, skill_source) VALUES %s",
                                    skill_rows,
                                )

                        # 4. EDUCATION
                        if edu_raw:
                            cur.execute("DELETE FROM candidate_education WHERE candidate_id = %s", (cid,))
                            edu_rows: List[tuple] = []
                            for idx, e in enumerate(edu_raw):
                                if isinstance(e, dict):
                                    edu_rows.append((
                                        cid,
                                        idx + 1,
                                        e.get("institution") or e.get("school"),
                                        e.get("degree") or e.get("field"),
                                    ))
                            if edu_rows:
                                psycopg2.extras.execute_values(
                                    cur,
                                    "INSERT INTO candidate_education (candidate_id, education_number, university_raw, degree_raw) VALUES %s",
                                    edu_rows,
                                )

                        # 5. POSITIONS
                        if exp_raw:
                            cur.execute("DELETE FROM candidate_positions WHERE candidate_id = %s", (cid,))
                            pos_rows: List[tuple] = []
                            for idx, exp in enumerate(exp_raw):
                                if isinstance(exp, dict):
                                    pos_rows.append((
                                        cid,
                                        idx + 1,
                                        exp.get("company"),
                                        exp.get("title"),
                                    ))
                            if pos_rows:
                                psycopg2.extras.execute_values(
                                    cur,
                                    "INSERT INTO candidate_positions (candidate_id, position_number, company_raw, title_raw) VALUES %s",
                                    pos_rows,
                                )

                        saved += 1
                    except Exception as e:
                        logger.error(f"[CandidateProfilesDB] Error upserting candidate {c.get('candidate_id')}: {e}")

        return saved


candidate_profiles_db = CandidateProfilesDB()
