import json
import psycopg2
import psycopg2.extras
from typing import List, Dict, Optional
from dataclasses import asdict
from core.config import DATABASE_URL

class JobRubricDB:
    """Handles structured persistent storage for all components of a job rubric."""
    
    def __init__(self, db_url: str = None):
        self.db_url = db_url or DATABASE_URL

    def save_full_rubric(self, jobdiva_id: str, rubric_obj: any, recruiter_notes: str = None) -> bool:
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
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor() as cur:
                    # 1. Clear existing rubric data for this job
                    cur.execute("DELETE FROM job_skills WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_education WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_titles WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_customer_requirements WHERE jobdiva_id = %s", (jobdiva_id,))
                    cur.execute("DELETE FROM job_other_requirements WHERE jobdiva_id = %s", (jobdiva_id,))

                    # 2. Save Skills
                    for s in rubric.get('skills', []):
                        cur.execute("""
                            INSERT INTO job_skills (jobdiva_id, skill_name, min_years, recent, match_type, is_required)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (
                            jobdiva_id,
                            s.get('value', ''),
                            s.get('minYears', 0),
                            bool(s.get('recent', False)),
                            s.get('matchType', 'Exact'),
                            s.get('required', 'Required') == 'Required'
                        ))

                    # 3. Save Titles / Experience
                    for t in rubric.get('titles', []):
                        cur.execute("""
                            INSERT INTO job_titles (jobdiva_id, title, min_years, recent, match_type, is_required, source)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """, (
                            jobdiva_id,
                            t.get('value', ''),
                            t.get('minYears', 0),
                            bool(t.get('recent', False)),
                            t.get('matchType', 'Exact'),
                            t.get('required', 'Required') == 'Required',
                            t.get('source', 'JobDiva')
                        ))

                    # 4. Save Education & Certs
                    for e in rubric.get('education', []):
                        cur.execute("""
                            INSERT INTO job_education (jobdiva_id, degree, field, is_required)
                            VALUES (%s, %s, %s, %s)
                        """, (
                            jobdiva_id,
                            e.get('degree', ''),
                            e.get('field', ''),
                            e.get('required', 'Required') == 'Required'
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
                            recruiter_notes = COALESCE(%s, recruiter_notes)
                        WHERE jobdiva_id = %s OR job_id = %s
                    """, (
                        json.dumps(domains),
                        recruiter_notes,
                        jobdiva_id,
                        jobdiva_id # Fallback if jobdiva_id is actually the job_id
                    ))

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
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Fetch domains from monitored_jobs
                    cur.execute("SELECT domains FROM monitored_jobs WHERE jobdiva_id = %s OR job_id = %s", (jobdiva_id, jobdiva_id))
                    job_row = cur.fetchone()
                    domains_list = job_row['domains'] if job_row and job_row['domains'] else []
                    domain_objs = [{"value": d, "required": "Required"} for d in domains_list]

                    # Fetch all sections
                    cur.execute("SELECT * FROM job_skills WHERE jobdiva_id = %s", (jobdiva_id,))
                    skills = [{
                        "value": r['skill_name'],
                        "minYears": r['min_years'],
                        "recent": r['recent'],
                        "matchType": r['match_type'],
                        "required": "Required" if r['is_required'] else "Preferred"
                    } for r in cur.fetchall()]

                    cur.execute("SELECT * FROM job_titles WHERE jobdiva_id = %s", (jobdiva_id,))
                    titles = [{
                        "value": r['title'],
                        "minYears": r['min_years'],
                        "recent": r['recent'],
                        "matchType": r['match_type'],
                        "required": "Required" if r['is_required'] else "Preferred",
                        "source": r.get('source', 'JobDiva')
                    } for r in cur.fetchall()]

                    cur.execute("SELECT * FROM job_education WHERE jobdiva_id = %s", (jobdiva_id,))
                    education = [{
                        "degree": r['degree'],
                        "field": r['field'],
                        "required": "Required" if r['is_required'] else "Preferred"
                    } for r in cur.fetchall()]

                    cur.execute("SELECT * FROM job_customer_requirements WHERE jobdiva_id = %s", (jobdiva_id,))
                    customer_reqs = [{
                        "value": r['requirement']
                    } for r in cur.fetchall()]

                    cur.execute("SELECT * FROM job_other_requirements WHERE jobdiva_id = %s", (jobdiva_id,))
                    other_reqs = [{
                        "value": r['requirement'],
                        "required": "Required" if r['is_required'] else "Preferred"
                    } for r in cur.fetchall()]

                    return {
                        "titles": titles,
                        "skills": skills,
                        "education": education,
                        "domain": domain_objs,
                        "customer_requirements": customer_reqs,
                        "other_requirements": other_reqs
                    }
        except Exception as e:
            print(f"❌ Failed to fetch rubric for {jobdiva_id}: {e}")
            return None
