import logging
import hashlib
from typing import Dict, Any, List, Optional
from datetime import datetime

import psycopg2.extras

from core.db import get_db_connection

logger = logging.getLogger(__name__)


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
        """One transaction per candidate. Child-table writes (skills,
        education, positions) used to be DELETE-then-loop-INSERT — for a
        candidate with 10 skills / 4 educations / 5 positions that was ~19
        round-trips, multiplied by every candidate in an auto-sync batch.
        Replaced with `execute_values` for one round-trip per child table.
        """
        if not candidates:
            return 0

        saved = 0
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
                    email = email[0] if email else None
                phone = c.get("phone")
                if isinstance(phone, list):
                    phone = phone[0] if phone else None

                title = c.get("job_title") or c.get("title") or c.get("headline")
                location = c.get("current_location") or c.get("location") or c.get("city")

                skill_source = "reported" if source != "JobDiva" else "predicted"
                skills_raw = c.get("structured_skills") or c.get("skills") or []
                edu_raw = c.get("candidate_education") or c.get("education") or []
                exp_raw = c.get("company_experience") or c.get("experience") or []

                with get_db_connection() as conn:
                    with conn.cursor() as cur:
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
