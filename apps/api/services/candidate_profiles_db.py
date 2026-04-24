import json
import logging
import hashlib
from typing import Dict, Any, List, Optional
from datetime import datetime
import sqlalchemy
from sqlalchemy import text
from core.config import DATABASE_URL, SUPABASE_DB_URL

logger = logging.getLogger(__name__)


# v22: Module-level engine singleton. Pre-v22 `bulk_upsert_candidates`
# instantiated a fresh unpooled engine on every call, leaking connections
# under load.
_ENGINE: Optional[sqlalchemy.engine.Engine] = None


def _get_engine() -> sqlalchemy.engine.Engine:
    global _ENGINE
    if _ENGINE is None:
        url = DATABASE_URL or SUPABASE_DB_URL
        if not url:
            raise RuntimeError("DATABASE_URL not configured for candidate_profiles_db")
        _ENGINE = sqlalchemy.create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    return _ENGINE


class CandidateProfilesDB:
    def __init__(self):
        self.db_url = DATABASE_URL or SUPABASE_DB_URL

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
        if not candidates:
            return 0
            
        saved = 0
        engine = _get_engine()
        with engine.begin() as conn:
            for c in candidates:
                try:
                    cid = self._resolve_candidate_id(c, source)
                    
                    # Ensure candidate has a unified 'name' field
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
                    
                    # 1. UPSERT PROFILES
                    conn.execute(text("""
                        INSERT INTO candidate_profiles (
                            candidate_id, source, firstname, lastname, fullname, 
                            profile_title, work_email, phone, user_location
                        )
                        VALUES (
                            :cid, :src, :first, :last, :full, 
                            :title, :email, :phone, :loc
                        )
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
                    """), {
                        "cid": cid,
                        "src": source,
                        "first": first,
                        "last": last,
                        "full": name,
                        "title": c.get("job_title") or c.get("title") or c.get("headline"),
                        "email": email,
                        "phone": phone,
                        "loc": c.get("current_location") or c.get("location") or c.get("city")
                    })
                    
                    # 2. UPSERT JOB LINK (sourced_candidate_jobs)
                    if jobdiva_id:
                        conn.execute(text("""
                            INSERT INTO sourced_candidate_jobs (
                                jobdiva_id, candidate_id, source, resume_id
                            )
                            VALUES (:jid, :cid, :src, :rid)
                            ON CONFLICT (jobdiva_id, candidate_id, source) DO NOTHING
                        """), {
                            "jid": jobdiva_id,
                            "cid": cid,
                            "src": source,
                            "rid": c.get("resume_id")
                        })
                        
                    # 3. UPSERT SKILLS
                    skills_raw = c.get("structured_skills") or c.get("skills") or []
                    if skills_raw:
                        conn.execute(text("DELETE FROM candidate_skills WHERE candidate_id = :cid"), {"cid": cid})
                        for s in skills_raw:
                            s_name = s.get("skill") or s.get("name") if isinstance(s, dict) else s
                            if s_name:
                                conn.execute(text("""
                                    INSERT INTO candidate_skills (candidate_id, skill_raw, skill_mapped, skill_source)
                                    VALUES (:cid, :raw, :mapped, :src)
                                """), {
                                    "cid": cid,
                                    "raw": str(s_name)[:255],
                                    "mapped": str(s_name)[:255],
                                    "src": "reported" if source != "JobDiva" else "predicted"
                                })
                                
                    # 4. UPSERT EDUCATION
                    edu_raw = c.get("candidate_education") or c.get("education") or []
                    if edu_raw:
                        conn.execute(text("DELETE FROM candidate_education WHERE candidate_id = :cid"), {"cid": cid})
                        for idx, e in enumerate(edu_raw):
                            if isinstance(e, dict):
                                conn.execute(text("""
                                    INSERT INTO candidate_education (candidate_id, education_number, university_raw, degree_raw)
                                    VALUES (:cid, :idx, :uni, :deg)
                                """), {
                                    "cid": cid,
                                    "idx": idx+1,
                                    "uni": e.get("institution") or e.get("school"),
                                    "deg": e.get("degree") or e.get("field")
                                })
                                
                    # 5. UPSERT POSITIONS
                    exp_raw = c.get("company_experience") or c.get("experience") or []
                    if exp_raw:
                        conn.execute(text("DELETE FROM candidate_positions WHERE candidate_id = :cid"), {"cid": cid})
                        for idx, exp in enumerate(exp_raw):
                            if isinstance(exp, dict):
                                conn.execute(text("""
                                    INSERT INTO candidate_positions (candidate_id, position_number, company_raw, title_raw)
                                    VALUES (:cid, :idx, :comp, :title)
                                """), {
                                    "cid": cid,
                                    "idx": idx+1,
                                    "comp": exp.get("company"),
                                    "title": exp.get("title")
                                })
                                
                    saved += 1
                except Exception as e:
                    logger.error(f"[CandidateProfilesDB] Error upserting candidate {c.get('candidate_id')}: {e}")
            
        return saved
        
candidate_profiles_db = CandidateProfilesDB()
