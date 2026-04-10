import json
import time
from typing import List, Optional, Dict, Any
import sqlalchemy
from sqlalchemy import text
from core.config import DATABASE_URL, SUPABASE_DB_URL
from models import SourcedCandidate

class SourcedCandidatesStorage:
    def __init__(self):
        self.db_url = DATABASE_URL or SUPABASE_DB_URL

    def _ensure_table(self, conn):
        """Ensure the sourced_candidates table exists with clean optimized schema."""
        # Create table with clean schema (no redundant columns)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sourced_candidates (
                id SERIAL PRIMARY KEY,
                jobdiva_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT,
                email TEXT,
                phone TEXT,
                headline TEXT,
                location TEXT,
                resume_id TEXT,
                resume_text TEXT,
                profile_url TEXT,
                image_url TEXT,
                data JSONB,
                status TEXT DEFAULT 'sourced',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(jobdiva_id, candidate_id, source)
            )
        """))
        
        # Migration: rename columns if old schema exists
        try:
            conn.execute(text("ALTER TABLE sourced_candidates RENAME COLUMN job_id TO jobdiva_id"))
        except Exception:
            pass  # Already renamed or doesn't exist
            
        try:
            conn.execute(text("ALTER TABLE sourced_candidates RENAME COLUMN jobdiva_resume_id TO resume_id"))
        except Exception:
            pass  # Already renamed or doesn't exist
            
        # Remove redundant columns
        try:
            conn.execute(text("ALTER TABLE sourced_candidates DROP COLUMN IF EXISTS jobdiva_candidate_id"))
            conn.execute(text("ALTER TABLE sourced_candidates DROP COLUMN IF EXISTS candidate_type"))
        except Exception:
            pass  # Already dropped or doesn't exist
            
        # Add missing columns
        try:
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS email TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS phone TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_id TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_text TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
            conn.commit()
        except Exception as e:
            pass  # Column might already exist

    def save_candidates(self, jobdiva_id: str, candidates: List[SourcedCandidate]) -> int:
        """Save search results to the database with enhanced JobDiva integration."""
        if not self.db_url:
            return 0
        
        saved_count = 0
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                for c in candidates:
                    try:
                        # Enhanced insert with clean schema
                        conn.execute(text("""
                            INSERT INTO sourced_candidates 
                            (jobdiva_id, candidate_id, source, name, email, phone, headline, location, profile_url, image_url, 
                             data, status, resume_id, resume_text, updated_at)
                            VALUES (:jobdiva_id, :candidate_id, :source, :name, :email, :phone, :headline, :location, 
                                    :profile_url, :image_url, :data, :status, :resume_id, :resume_text, CURRENT_TIMESTAMP)
                            ON CONFLICT (jobdiva_id, candidate_id, source) 
                            DO UPDATE SET 
                                name = EXCLUDED.name,
                                email = EXCLUDED.email,
                                phone = EXCLUDED.phone,
                                headline = EXCLUDED.headline,
                                location = EXCLUDED.location,
                                profile_url = EXCLUDED.profile_url,
                                image_url = EXCLUDED.image_url,
                                data = EXCLUDED.data,
                                resume_id = EXCLUDED.resume_id,
                                resume_text = EXCLUDED.resume_text,
                                updated_at = CURRENT_TIMESTAMP
                        """), {
                            "jobdiva_id": jobdiva_id,
                            "candidate_id": c.candidate_id,
                            "source": c.source,
                            "name": c.name,
                            "email": getattr(c, 'email', None),
                            "phone": getattr(c, 'phone', None),
                            "headline": c.headline,
                            "location": c.location,
                            "profile_url": c.profile_url,
                            "image_url": c.image_url,
                            "data": json.dumps(c.data) if c.data else None,
                            "status": c.status,
                            "resume_id": getattr(c, 'resume_id', None),
                            "resume_text": getattr(c, 'resume_text', None)
                        })
                        saved_count += 1
                    except Exception as e:
                        print(f"Error saving candidate {c.candidate_id}: {e}")
                        
                conn.commit()
        except Exception as e:
            print(f"Error saving candidates: {e}")
        
        return saved_count

    def save_enhanced_candidate(self, job_id: str, candidate_data: Dict[str, Any]) -> bool:
        """Save a single enhanced candidate with full JobDiva data integration using new schema."""
        if not self.db_url:
            return False
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                conn.execute(text("""
                    INSERT INTO sourced_candidates 
                    (jobdiva_id, candidate_id, source, name, email, phone, headline, location, profile_url, 
                     image_url, resume_id, resume_text, data, status, updated_at)
                    VALUES (:jobdiva_id, :candidate_id, :source, :name, :email, :phone, :headline, :location, 
                            :profile_url, :image_url, :resume_id, :resume_text, :data, :status, CURRENT_TIMESTAMP)
                    ON CONFLICT (jobdiva_id, candidate_id, source) 
                    DO UPDATE SET 
                        name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        phone = EXCLUDED.phone,
                        headline = EXCLUDED.headline,
                        location = EXCLUDED.location,
                        profile_url = EXCLUDED.profile_url,
                        image_url = EXCLUDED.image_url,
                        resume_id = EXCLUDED.resume_id,
                        resume_text = EXCLUDED.resume_text,
                        data = EXCLUDED.data,
                        status = 'sourced',
                        updated_at = CURRENT_TIMESTAMP
                """), candidate_data)
                
                conn.commit()
                return True
        except Exception as e:
            print(f"Error saving enhanced candidate: {e}")
            return False

    def deduplicate_candidates(self, job_id: str) -> int:
        """Remove duplicates, prioritizing job applicants over talent search using new schema."""
        if not self.db_url:
            return 0
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                result = conn.execute(text("""
                    DELETE FROM sourced_candidates s1
                    WHERE s1.jobdiva_id = :job_id
                    AND EXISTS (
                        SELECT 1 FROM sourced_candidates s2
                        WHERE s2.jobdiva_id = s1.jobdiva_id
                        AND s2.candidate_id = s1.candidate_id
                        AND s2.source = 'JobDiva-Applicants'
                        AND s1.source = 'JobDiva-TalentSearch'
                        AND s1.id != s2.id
                    )
                """), {"job_id": job_id})
                
                conn.commit()
                return result.rowcount
        except Exception as e:
            print(f"Error deduplicating candidates: {e}")
            return 0

    def get_candidates_for_job(self, jobdiva_id: str) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates for a specific job."""
        if not self.db_url:
            return []
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                # First, find both IDs for this job to ensure we catch all candidates
                # A job can be identified by its numeric job_id OR its jobdiva_id (ref code)
                alt_ids = [jobdiva_id]
                job_lookup = conn.execute(text("""
                    SELECT job_id, jobdiva_id FROM monitored_jobs 
                    WHERE job_id = :id OR jobdiva_id = :id
                """), {"id": jobdiva_id}).fetchone()
                
                if job_lookup:
                    alt_ids = [job_lookup[0], job_lookup[1]]
                
                result = conn.execute(text("""
                    SELECT * FROM sourced_candidates 
                    WHERE job_id IN :ids 
                    ORDER BY created_at DESC
                """), {"ids": tuple(alt_ids)})
                
                candidates = []
                for row in result:
                    c_dict = dict(zip(result.keys(), row))
                    if c_dict.get('data'):
                        try:
                            if isinstance(c_dict['data'], str):
                                c_dict['data'] = json.loads(c_dict['data'])
                        except:
                            pass
                    if c_dict.get('created_at'):
                        c_dict['created_at'] = str(c_dict['created_at'])
                    candidates.append(c_dict)
                return candidates
        except Exception as e:
            print(f"Error retrieving candidates for job {jobdiva_id}: {e}")
            return []

    def get_all_candidates(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates across all jobs."""
        if not self.db_url:
            return []
            
        try:
            # Use fresh connection to avoid transaction issues
            import psycopg2
            import psycopg2.extras
            
            conn = psycopg2.connect(self.db_url)
            conn.autocommit = True  # Prevent transaction issues
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Join with monitored_jobs to get job title
            # Try matching by jobdiva_id with the monitored jobs
            cur.execute("""
                SELECT sc.*, mj.title as job_title 
                FROM sourced_candidates sc
                LEFT JOIN monitored_jobs mj ON (sc.jobdiva_id = mj.job_id OR sc.jobdiva_id = mj.jobdiva_id)
                ORDER BY sc.created_at DESC
                LIMIT %s
            """, (limit,))
            
            candidates = []
            for row in cur.fetchall():
                c_dict = dict(row)
                if c_dict.get('data'):
                    try:
                        if isinstance(c_dict['data'], str):
                            c_dict['data'] = json.loads(c_dict['data'])
                    except:
                        pass
                if c_dict.get('created_at'):
                    c_dict['created_at'] = str(c_dict['created_at'])
                candidates.append(c_dict)
                
            cur.close()
            conn.close()
            return candidates
        except Exception as e:
            print(f"Error retrieving all candidates: {e}")
            return []

sourced_candidates_storage = SourcedCandidatesStorage()
