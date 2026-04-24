"""
Enhanced storage service for monitored_jobs table.
Stores all processed data in existing table with new fields.
"""

import json
import time
from typing import Optional, Dict, Any
import sqlalchemy
from sqlalchemy import text
from core.config import DATABASE_URL, SUPABASE_DB_URL
from models import ExtractedData, Skill


# v22: Module-level engine singleton. Pre-v22 each method called
# `sqlalchemy.create_engine(self.db_url)` with default pool settings, leaking
# connections on every invocation. Pool once, reuse forever; fail fast on a
# hung DB via `connect_timeout=5`.
_ENGINE: Optional[sqlalchemy.engine.Engine] = None


def _get_engine() -> sqlalchemy.engine.Engine:
    global _ENGINE
    if _ENGINE is None:
        url = DATABASE_URL or SUPABASE_DB_URL
        if not url:
            raise RuntimeError("DATABASE_URL not configured for monitored_jobs_storage")
        _ENGINE = sqlalchemy.create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 5},
        )
    return _ENGINE


class MonitoredJobsStorage:
    """Service to store all processed data in the updated monitored_jobs table."""

    def __init__(self):
        self.db_url = DATABASE_URL or SUPABASE_DB_URL
    
    def update_job_with_extracted_data(self, 
                                     job_id: str, 
                                     extracted_data: ExtractedData,
                                     processing_metadata: Dict = None) -> bool:
        """
        Update existing monitored_jobs record with all extracted/processed data.
        
        Args:
            job_id: Job ID in monitored_jobs table
            extracted_data: Complete ExtractedData from LLM
            processing_metadata: Processing info (model, time, tokens, etc.)
        
        Returns:
            bool: Success status
        """
        if not self.db_url:
            print("⚠️ No database connection available")
            return False
            
        try:
            engine = _get_engine()
            conn = engine.connect()
            
            # Prepare hard_skills as JSON
            hard_skills_json = [
                {
                    "name": skill.name,
                    "seniority": skill.seniority,
                    "priority": skill.priority,
                    "years_experience": skill.years_experience
                }
                for skill in extracted_data.hard_skills
            ]
            
            # Prepare extraction metadata
            extraction_meta = {
                "extraction_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": processing_metadata.get("model", "unknown") if processing_metadata else "unknown",
                "processing_time_ms": processing_metadata.get("processing_time_ms", 0) if processing_metadata else 0,
                "tokens_used": processing_metadata.get("tokens_used", 0) if processing_metadata else 0,
                "confidence": processing_metadata.get("confidence", 0.8) if processing_metadata else 0.8
            }
            
            bot_introduction = processing_metadata.get("bot_introduction") if processing_metadata else None

            # v22: DDL (ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS ...)
            # moved to lifespan `_ensure_monitored_jobs_schema` in routers/jobs.py.
            # Pre-v22 this ran on every extract, holding ACCESS EXCLUSIVE on
            # monitored_jobs and stalling concurrent readers of /jobs/monitored.

            # Update the monitored_jobs record with all processed data
            result = conn.execute(text("""
                UPDATE monitored_jobs SET
                    summary = :summary,
                    hard_skills = :hard_skills,
                    soft_skills = :soft_skills,
                    experience_level = :experience_level,
                    extraction_metadata = :extraction_metadata,
                    bot_introduction = COALESCE(:bot_introduction, bot_introduction),
                    sourcing_filters = COALESCE(:sourcing_filters, sourcing_filters),
                    updated_at = :updated_at
                WHERE job_id = :job_id
            """), {
                "summary": extracted_data.summary,
                "hard_skills": json.dumps(hard_skills_json),
                "soft_skills": json.dumps(extracted_data.soft_skills),
                "experience_level": extracted_data.experience_level,
                "extraction_metadata": json.dumps(extraction_meta),
                "bot_introduction": bot_introduction,
                "sourcing_filters": json.dumps(processing_metadata.get("sourcing_filters")) if processing_metadata and processing_metadata.get("sourcing_filters") else None,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "job_id": job_id
            })
            
            # Check if update affected any rows
            if result.rowcount == 0:
                print(f"⚠️ No job found with ID {job_id}")
                conn.close()
                return False
                
            conn.commit()
            conn.close()
            
            print(f"✅ Updated job {job_id} with all processed data")
            return True
            
        except Exception as e:
            print(f"❌ Error updating job with extracted data: {e}")
            return False
    
    def get_complete_job_data(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get complete job data including all processed fields.
        
        Args:
            job_id: Job identifier
            
        Returns:
            Complete job data dict or None if not found
        """
        if not self.db_url:
            return None
            
        try:
            engine = _get_engine()
            conn = engine.connect()
            
            result = conn.execute(text("SELECT * FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
            row = result.fetchone()
            
            if not row:
                return None
                
            # Get column names
            columns = result.keys()
            job_data = dict(zip(columns, row))
            
            # Parse JSON fields if they exist
            if job_data.get('hard_skills'):
                try:
                    job_data['hard_skills'] = json.loads(job_data['hard_skills'])
                except:
                    pass
                    
            if job_data.get('soft_skills'):
                try:
                    job_data['soft_skills'] = json.loads(job_data['soft_skills'])
                except:
                    pass
                    
            if job_data.get('extraction_metadata'):
                try:
                    job_data['extraction_metadata'] = json.loads(job_data['extraction_metadata'])
                except:
                    pass
            
            conn.close()
            return job_data
            
        except Exception as e:
            print(f"❌ Error retrieving job data: {e}")
            return None
    
    def get_jobs_with_processing_status(self) -> Dict[str, Any]:
        """Get summary of jobs and their processing status."""
        
        if not self.db_url:
            return {}
            
        try:
            engine = _get_engine()
            conn = engine.connect()
            
            # Count total jobs
            result = conn.execute(text("SELECT COUNT(*) FROM monitored_jobs"))
            total_jobs = result.fetchone()[0]
            
            # Count jobs with processed data
            result = conn.execute(text("SELECT COUNT(*) FROM monitored_jobs WHERE summary IS NOT NULL"))
            processed_jobs = result.fetchone()[0]
            
            # Count jobs by experience level
            result = conn.execute(text("""
                SELECT experience_level, COUNT(*) 
                FROM monitored_jobs 
                WHERE experience_level IS NOT NULL 
                GROUP BY experience_level
            """))
            experience_breakdown = dict(result.fetchall())
            
            # Count jobs by location type
            result = conn.execute(text("""
                SELECT location_type, COUNT(*) 
                FROM monitored_jobs 
                WHERE location_type IS NOT NULL 
                GROUP BY location_type
            """))
            location_breakdown = dict(result.fetchall())
            
            conn.close()
            
            return {
                "total_jobs": total_jobs,
                "processed_jobs": processed_jobs,
                "unprocessed_jobs": total_jobs - processed_jobs,
                "processing_rate": f"{(processed_jobs/total_jobs*100):.1f}%" if total_jobs > 0 else "0%",
                "experience_level_breakdown": experience_breakdown,
                "location_type_breakdown": location_breakdown
            }
            
        except Exception as e:
            print(f"❌ Error getting processing status: {e}")
            return {}

# Convenience function for easy integration
def store_extraction_in_monitored_jobs(job_id: str, extracted_data: ExtractedData, processing_info: Dict = None) -> bool:
    """
    Convenience function to store extraction results in monitored_jobs table.
    Use this in your extraction pipeline.
    """
    storage = MonitoredJobsStorage()
    return storage.update_job_with_extracted_data(job_id, extracted_data, processing_info)