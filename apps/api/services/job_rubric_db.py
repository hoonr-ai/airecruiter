import asyncio
import json
import logging
import psycopg2
import psycopg2.extras
from typing import List, Dict, Optional
from dataclasses import asdict
from core.config import DATABASE_URL
from core.db import get_db_connection

logger = logging.getLogger(__name__)


def _ensure_rubric_schema_sync() -> None:
    """Provision schema for rubric tables. Idempotent.

    1. `source` columns — recruiter-added vs AI-extracted rubric items need
       to round-trip provenance so the chip in Step 3 ("Recruiter" /
       "PAIR") survives save+reload. `job_titles.source` already
       existed pre-change; `job_skills.source` and `job_education.source`
       are added here as nullable TEXT so legacy rows are read as
       PAIR by the reader's COALESCE.

       The non-recruiter sentinel is written as 'PAIR' post-rebrand; rows
       written before it still hold 'Hoonr-Curate'. No migration is needed
       because the only consumer (sourceLabel in the wizard) tests for
       'recruiter' and treats every other value as PAIR.

    2. Unique indexes on (jobdiva_id, LOWER(skill_name)) and
       (jobdiva_id, LOWER(title)) — defense-in-depth against duplicate
       rubric items per job. The Step 5 wizard now dedupes on add, but the
       DB constraint catches any path that bypasses the in-memory
       seen_skills set in save_full_rubric.
    """
    with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE job_skills ADD COLUMN IF NOT EXISTS source TEXT")
            cur.execute("ALTER TABLE job_education ADD COLUMN IF NOT EXISTS source TEXT")

            # One-time dedup so the unique index below can be created without
            # failing. The save path already DELETEs-then-INSERTs with an
            # in-memory seen_skills set, so accumulated dupes only exist from
            # historical writes or non-rubric writers. Keep the row with the
            # highest ctid (latest physical insert).
            cur.execute("""
                DELETE FROM job_skills s1
                USING job_skills s2
                WHERE s1.ctid < s2.ctid
                  AND s1.jobdiva_id = s2.jobdiva_id
                  AND LOWER(s1.skill_name) = LOWER(s2.skill_name)
            """)
            cur.execute("""
                DELETE FROM job_titles t1
                USING job_titles t2
                WHERE t1.ctid < t2.ctid
                  AND t1.jobdiva_id = t2.jobdiva_id
                  AND LOWER(t1.title) = LOWER(t2.title)
            """)
            # Defense-in-depth against duplicate rubric items per job. LOWER()
            # matches the case-insensitive frontend dedup and the backend's
            # seen_skills.upper() check in save_full_rubric.
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS job_skills_unique_per_job
                ON job_skills (jobdiva_id, LOWER(skill_name))
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS job_titles_unique_per_job
                ON job_titles (jobdiva_id, LOWER(title))
            """)
        conn.commit()


async def init_rubric_schema() -> None:
    """Async wrapper so main.py startup can await the sync alter."""
    await asyncio.to_thread(_ensure_rubric_schema_sync)


def _normalize_title(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "").strip() if ch.isalnum())


def _parse_customer_requirement(requirement: str) -> Dict[str, str]:
    requirement = (requirement or "").strip()
    known_prefixes = [
        "Must not be employed by",
        "Currently employed by",
        "Previously employed by",
    ]

    for prefix in known_prefixes:
        marker = f"{prefix}:"
        if requirement.startswith(marker):
            return {
                "type": prefix,
                "value": requirement[len(marker):].strip(),
            }

    return {
        "type": "Must not be employed by",
        "value": requirement,
    }

class JobRubricDB:
    """Handles structured persistent storage for all components of a job rubric."""
    
    def __init__(self, db_url: str = None):
        import logging
        self.db_url = db_url or DATABASE_URL
        self.logger = logging.getLogger(__name__)

    def save_full_rubric(self, jobdiva_id: str, rubric_obj: any, recruiter_notes: str = None, bot_introduction: str = None) -> bool:
        """
        Saves all rubric sections to their respective tables.
        Uses jobdiva_id (ref code) as the primary cross-reference key.
        """
        if not jobdiva_id:
            return False

        # Convert dataclass to dict if necessary
        if hasattr(rubric_obj, '__dataclass_fields__'):
            rubric = asdict(rubric_obj)
        else:
            rubric = rubric_obj

        try:
            # Route through the per-worker pool instead of opening a fresh
            # socket per save. The raw psycopg2.connect pattern was the
            # explicit follow-up from bf857e5: it bypasses _POOL_MAX, consumes
            # Postgres-side max_connections, and never .close()s the socket on
            # __exit__ (raw psycopg2 commits but does not close).
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT title FROM monitored_jobs WHERE jobdiva_id = %s OR job_id = %s LIMIT 1",
                        (jobdiva_id, jobdiva_id)
                    )
                    monitored_job = cur.fetchone()
                    primary_job_title = _normalize_title(monitored_job[0]) if monitored_job and monitored_job[0] else ""

                    # 1. Clear existing rubric data for this job
                    cur.execute("DELETE FROM job_skills WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_education WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_titles WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_customer_requirements WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_other_requirements WHERE jobdiva_id = %s", (jobdiva_id,))

                    # 2. Save Skills (Hard and Soft)
                    all_skills = []
                    # We merge lists but FILTER OUT anything that was re-routed to education
                    raw_skills = (rubric.get('skills', []) or []) + (rubric.get('hard_skills', []) or [])
                    seen_skills = set()
                    
                    for s in raw_skills:
                        if s.get('category') == 'certification':
                            continue
                        val = s.get('value', '').upper()
                        if val in seen_skills: continue
                        seen_skills.add(val)
                        s['category'] = s.get('category', 'hard')
                        all_skills.append(s)

                    for s in rubric.get('soft_skills', []):
                        val = s.get('value', '').upper()
                        if val in seen_skills: continue
                        seen_skills.add(val)
                        s['category'] = 'soft'
                        all_skills.append(s)

                    for s in all_skills:
                        cur.execute("""
                            INSERT INTO job_skills (jobdiva_id, skill_name, min_years, recent, match_type, is_required, category, similar_skills, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s)
                        """, (
                            jobdiva_id,
                            s.get('value', ''),
                            s.get('minYears', 0),
                            bool(s.get('recent', False)),
                            s.get('matchType', 'Similar'),
                            (s.get('importance', s.get('required', 'Required')) == 'Required'),
                            s.get('category', 'hard'),
                            s.get('similar_skills', []), # psycopg2 handles list as postgres ARRAY
                            s.get('source') or 'PAIR',
                        ))

                    # 3. Save Titles / Experience
                    for t in rubric.get('titles', []):
                        normalized_title = _normalize_title(t.get('value', ''))
                        is_direct_title = bool(primary_job_title and normalized_title == primary_job_title)

                        cur.execute("""
                            INSERT INTO job_titles (jobdiva_id, title, min_years, recent, match_type, is_required, similar_titles, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s::text[], %s)
                        """, (
                            jobdiva_id,
                            t.get('value', ''),
                            t.get('minYears', 0),
                            bool(t.get('recent', False)),
                            'Similar',
                            is_direct_title or (t.get('required', 'Required') == 'Required'),
                            t.get('similar_titles', []), # postgres ARRAY
                            t.get('source', 'PAIR')
                        ))

                    # 4. Save Education & Certs
                    for e in rubric.get('education', []):
                        cur.execute("""
                            INSERT INTO job_education (jobdiva_id, degree, field, is_required, source)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (
                            jobdiva_id,
                            e.get('degree', ''),
                            e.get('field', ''),
                            e.get('required', 'Required') == 'Required',
                            e.get('source') or 'PAIR',
                        ))

                    # 5. Save Customer Requirements
                    for cr in rubric.get('customer_requirements', []):
                        val = cr.get('value', '')
                        if cr.get('type'):
                            val = f"{cr['type']}: {val}"
                        cur.execute("""
                            INSERT INTO job_customer_requirements (jobdiva_id, requirement, is_required)
                            VALUES (%s, %s, %s)
                        """, (
                            jobdiva_id,
                            val,
                            True # Defaulting to true as UI doesn't always show a toggle here
                        ))

                    # 6. Save Other Requirements
                    for orq in rubric.get('other_requirements', []):
                        cur.execute("""
                            INSERT INTO job_other_requirements (jobdiva_id, requirement, is_required)
                            VALUES (%s, %s, %s)
                        """, (
                            jobdiva_id,
                            orq.get('value', ''),
                            orq.get('required', 'Required') == 'Required'
                        ))

                    # 7. Update monitored_jobs with domains and recruiter_notes
                    domains = [d.get('value') for d in rubric.get('domain', []) if d.get('value')]
                    
                    cur.execute("""
                        UPDATE monitored_jobs 
                        SET domains = %s,
                            recruiter_notes = COALESCE(%s, recruiter_notes),
                            bot_introduction = COALESCE(%s, bot_introduction)
                        WHERE jobdiva_id = %s OR job_id = %s
                    """, (
                        json.dumps(domains),
                        recruiter_notes,
                        bot_introduction,
                        jobdiva_id,
                        jobdiva_id # Fallback if jobdiva_id is actually the job_id
                    ))
                    
                    # 8. Save Screen Questions. Sync the table whenever the
                    # caller passed a list — including an empty one — so that
                    # recruiter deletions on Step 4 don't leak into the next
                    # interview payload. Only skip when the key is absent
                    # (partial save from a step that doesn't touch questions).
                    if 'screen_questions' in rubric and rubric.get('screen_questions') is not None:
                        self._save_screen_questions_internal(cur, jobdiva_id, rubric.get('screen_questions') or [])

                conn.commit()
                return True
        except Exception as e:
            print(f"❌ Failed to save rubric for {jobdiva_id}: {e}")
            return False

    def get_full_rubric(self, jobdiva_id: str) -> Optional[Dict]:
        """Retrieves the complete rubric from separate tables."""
        if not jobdiva_id:
            return None

        try:
            # Route through the per-worker pool (see save_full_rubric note).
            with get_db_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Single round trip: monitored_jobs lookup + jsonb_agg of
                    # all five child tables. Pre-fix this issued six sequential
                    # queries (one per child table + monitored_jobs) which
                    # dominated job-detail and ranking latency on every load.
                    cur.execute(
                        """
                        WITH job AS (
                            SELECT jobdiva_id, job_id, domains, bot_introduction
                            FROM monitored_jobs
                            WHERE (jobdiva_id = %s OR job_id = %s)
                            LIMIT 1
                        ),
                        keys AS (
                            SELECT DISTINCT id FROM (VALUES (%s), ((SELECT jobdiva_id FROM job)), ((SELECT job_id FROM job))) AS t(id) WHERE id IS NOT NULL AND id != ''
                        )
                        SELECT
                            (SELECT domains FROM job) AS domains,
                            (SELECT bot_introduction FROM job) AS bot_introduction,
                            COALESCE((SELECT jsonb_agg(to_jsonb(s)) FROM job_skills s WHERE jobdiva_id IN (SELECT id FROM keys)), '[]'::jsonb) AS skills_rows,
                            COALESCE((SELECT jsonb_agg(to_jsonb(t)) FROM job_titles t WHERE jobdiva_id IN (SELECT id FROM keys)), '[]'::jsonb) AS title_rows,
                            COALESCE((SELECT jsonb_agg(to_jsonb(e)) FROM job_education e WHERE jobdiva_id IN (SELECT id FROM keys)), '[]'::jsonb) AS education_rows,
                            COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM job_customer_requirements c WHERE jobdiva_id IN (SELECT id FROM keys)), '[]'::jsonb) AS customer_rows,
                            COALESCE((SELECT jsonb_agg(to_jsonb(o)) FROM job_other_requirements o WHERE jobdiva_id IN (SELECT id FROM keys)), '[]'::jsonb) AS other_rows
                        """,
                        (jobdiva_id, jobdiva_id, jobdiva_id),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None

                    domains_list = row.get('domains') or []
                    domain_objs = [{"value": d, "required": "Required"} for d in domains_list]

                    skills = []
                    soft_skills = []
                    for r in (row.get('skills_rows') or []):
                        match_type = r.get('match_type')
                        skill_obj = {
                            "value": r.get('skill_name'),
                            "minYears": r.get('min_years'),
                            "recent": r.get('recent'),
                            "matchType": 'Similar' if not match_type or match_type.lower() == 'similar' else match_type,
                            "required": "Required" if r.get('is_required') else "Preferred",
                            "source": r.get('source') or 'PAIR',
                            "similar_skills": list(r.get('similar_skills')) if r.get('similar_skills') else []
                        }
                        if r.get('category') == 'soft':
                            soft_skills.append(skill_obj)
                        else:
                            skills.append(skill_obj)

                    titles = []
                    for r in (row.get('title_rows') or []):
                        match_type = r.get('match_type')
                        titles.append({
                            "value": r.get('title'),
                            "minYears": r.get('min_years'),
                            "recent": r.get('recent'),
                            "matchType": 'Similar' if not match_type or match_type.lower() == 'similar' else match_type,
                            "required": "Required" if r.get('is_required') else "Preferred",
                            "source": r.get('source') or 'PAIR',
                            "similar_titles": list(r.get('similar_titles')) if r.get('similar_titles') else []
                        })

                    education = [{
                        "degree": r.get('degree'),
                        "field": r.get('field'),
                        "required": "Required" if r.get('is_required') else "Preferred",
                        "source": r.get('source') or 'PAIR'
                    } for r in (row.get('education_rows') or [])]

                    customer_reqs = [
                        _parse_customer_requirement(r.get('requirement'))
                        for r in (row.get('customer_rows') or [])
                    ]

                    other_reqs = [{
                        "value": r.get('requirement'),
                        "required": "Required" if r.get('is_required') else "Preferred"
                    } for r in (row.get('other_rows') or [])]

                    return {
                        "titles": titles,
                        "skills": skills,
                        "soft_skills": soft_skills,
                        "education": education,
                        "domain": domain_objs,
                        "customer_requirements": customer_reqs,
                        "other_requirements": other_reqs,
                        "bot_introduction": row.get('bot_introduction'),
                        "screen_questions": self._get_screen_questions_internal(cur, jobdiva_id)
                    }
        except Exception as e:
            print(f"❌ Failed to fetch rubric for {jobdiva_id}: {e}")
            return None
    def _ensure_hard_filter_column(self, cur) -> None:
        """No-op placeholder.

        v30: moved `job_screen_questions.is_hard_filter` schema provisioning to
        startup bootstrap in `routers/jobs._ensure_monitored_jobs_schema` so
        request paths remain DDL-free.
        """
        return

    def _save_screen_questions_internal(self, cur, jobdiva_id: str, questions: List[Dict]):
        """Internal helper to save screen questions using an existing cursor."""
        self._ensure_hard_filter_column(cur)
        cur.execute("DELETE FROM job_screen_questions WHERE jobdiva_id = %s", (jobdiva_id,))
        for i, q in enumerate(questions):
            cur.execute("""
                INSERT INTO job_screen_questions (
                    jobdiva_id, question_text, pass_criteria, is_default, category, order_index, is_hard_filter
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                jobdiva_id,
                q.get('question_text', ''),
                q.get('pass_criteria', ''),
                bool(q.get('is_default', False)),
                q.get('category', 'other'),
                q.get('order_index', i),
                bool(q.get('is_hard_filter', False)),
            ))

    def _get_screen_questions_internal(self, cur, jobdiva_id: str) -> List[Dict]:
        """Internal helper to fetch screen questions using an existing cursor."""
        self._ensure_hard_filter_column(cur)
        cur.execute("""
            WITH job AS (
                SELECT jobdiva_id, job_id
                FROM monitored_jobs
                WHERE (jobdiva_id = %s OR job_id = %s)
                LIMIT 1
            ),
            keys AS (
                SELECT DISTINCT id FROM (VALUES (%s), ((SELECT jobdiva_id FROM job)), ((SELECT job_id FROM job))) AS t(id) WHERE id IS NOT NULL AND id != ''
            )
            SELECT question_text, pass_criteria, is_default, category, order_index,
                   COALESCE(is_hard_filter, FALSE) AS is_hard_filter
            FROM job_screen_questions
            WHERE jobdiva_id IN (SELECT id FROM keys)
            ORDER BY order_index
        """, (jobdiva_id, jobdiva_id, jobdiva_id))
        return [{
            "question_text": r['question_text'],
            "pass_criteria": r['pass_criteria'],
            "is_default": r['is_default'],
            "category": r['category'],
            "order_index": r['order_index'],
            "is_hard_filter": bool(r.get('is_hard_filter', False)),
        } for r in cur.fetchall()]
