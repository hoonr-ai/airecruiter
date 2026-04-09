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
        """Ensure the sourced_candidates table exists."""
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sourced_candidates (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT,
                headline TEXT,
                location TEXT,
                profile_url TEXT,
                image_url TEXT,
                data JSONB,
                status TEXT DEFAULT 'sourced',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id, candidate_id, source)
            )
        """))

    def save_candidates(self, job_id: str, candidates: List[SourcedCandidate]) -> int:
        """Save search results to the database."""
        if not self.db_url:
            return 0
        
        saved_count = 0
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                for c in candidates:
                    try:
                        conn.execute(text("""
                            INSERT INTO sourced_candidates 
                            (job_id, candidate_id, source, name, headline, location, profile_url, image_url, data, status)
                            VALUES (:job_id, :candidate_id, :source, :name, :headline, :location, :profile_url, :image_url, :data, :status)
                            ON CONFLICT (job_id, candidate_id, source) 
                            DO UPDATE SET 
                                name = EXCLUDED.name,
                                headline = EXCLUDED.headline,
                                location = EXCLUDED.location,
                                profile_url = EXCLUDED.profile_url,
                                image_url = EXCLUDED.image_url,
                                data = EXCLUDED.data
                        """), {
                            "job_id": job_id,
                            "candidate_id": c.candidate_id,
                            "source": c.source,
                            "name": c.name,
                            "headline": c.headline,
                            "location": c.location,
                            "profile_url": c.profile_url,
                            "image_url": c.image_url,
                            "data": json.dumps(c.data) if c.data else None,
                            "status": c.status
                        })
                        saved_count += 1
                    except Exception as e:
                        print(f"Error saving candidate {c.candidate_id}: {e}")
                
                conn.commit()
            return saved_count
        except Exception as e:
            print(f"Database error in save_candidates: {e}")
            return 0

    def get_candidates_for_job(self, job_id: str) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates for a specific job."""
        if not self.db_url:
            return []
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                # First, find both IDs for this job to ensure we catch all candidates
                # A job can be identified by its numeric job_id OR its jobdiva_id (ref code)
                alt_ids = [job_id]
                job_lookup = conn.execute(text("""
                    SELECT job_id, jobdiva_id FROM monitored_jobs 
                    WHERE job_id = :id OR jobdiva_id = :id
                """), {"id": job_id}).fetchone()
                
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
            print(f"Error retrieving candidates for job {job_id}: {e}")
            return []

    def get_all_candidates(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates across all jobs."""
        if not self.db_url:
            return []
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                # Join with monitored_jobs to get job title
                # Try matching by both numeric job_id and the jobdiva_id reference
                result = conn.execute(text("""
                    SELECT sc.*, mj.title as job_title 
                    FROM sourced_candidates sc
                    LEFT JOIN monitored_jobs mj ON (sc.job_id = mj.job_id OR sc.job_id = mj.jobdiva_id)
                    ORDER BY sc.created_at DESC
                    LIMIT :limit
                """), {"limit": limit})
                
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
            print(f"Error retrieving all candidates: {e}")
            return []

sourced_candidates_storage = SourcedCandidatesStorage()
