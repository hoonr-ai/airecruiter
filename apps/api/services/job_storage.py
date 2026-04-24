"""
Storage service for structured job data from pre-fetch phase.
Handles saving and retrieving rich JobDescription data.
"""

import json
from typing import Optional, Dict, Any
from sqlalchemy import create_engine, text
from core.config import CLOUDSQL_CONNECTION_NAME
from core.db import getconn
from core.models import JobDescription, JobMetadata, GatingRules, Requirement, Competency, SenioritySignals

class JobStorageService:
    """Service for storing and retrieving structured job data."""
    
    def __init__(self):
        self.engine = None
        if CLOUDSQL_CONNECTION_NAME:
            self.engine = create_engine("postgresql+pg8000://", creator=getconn)
    
    async def store_job_data(self, job_description: JobDescription, source_job_id: Optional[str] = None, raw_description: Optional[str] = None) -> bool:
        """
        Store complete structured job data from extraction phase.
        
        Args:
            job_description: Structured JobDescription model
            source_job_id: Reference to original monitored_jobs entry
            raw_description: Original job posting text
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self.engine:
            print("⚠️ No database connection available")
            return False
            
        try:
            with self.engine.connect() as conn:
                # 1. Insert main job record
                conn.execute(text("""
                    INSERT INTO jobs (id, title, location, work_mode, clearance, source_job_id, raw_description, is_valid, parsing_error)
                    VALUES (:id, :title, :location, :work_mode, :clearance, :source_job_id, :raw_description, :is_valid, :parsing_error)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        location = EXCLUDED.location,
                        work_mode = EXCLUDED.work_mode,
                        clearance = EXCLUDED.clearance,
                        source_job_id = EXCLUDED.source_job_id,
                        raw_description = EXCLUDED.raw_description,
                        is_valid = EXCLUDED.is_valid,
                        parsing_error = EXCLUDED.parsing_error,
                        updated_at = CURRENT_TIMESTAMP
                """), {
                    "id": job_description.id,
                    "title": job_description.job_metadata.title,
                    "location": job_description.job_metadata.location,
                    "work_mode": job_description.job_metadata.work_mode,
                    "clearance": job_description.job_metadata.clearance,
                    "source_job_id": source_job_id,
                    "raw_description": raw_description,
                    "is_valid": job_description.is_valid,
                    "parsing_error": job_description.parsing_error
                })
                
                # 2. Insert gating rules
                await self._store_gating_rules(conn, job_description.id, job_description.gating_rules)
                
                # 3. Insert requirements
                await self._store_requirements(conn, job_description.id, job_description.requirements)
                
                # 4. Insert competencies
                await self._store_competencies(conn, job_description.id, job_description.competencies)
                
                # 5. Insert seniority signals
                await self._store_seniority_signals(conn, job_description.id, job_description.seniority_signals)
                
                conn.commit()
                print(f"✅ Successfully stored structured job data for {job_description.id}")
                return True
                
        except Exception as e:
            print(f"❌ Error storing job data: {e}")
            return False
    
    async def _store_gating_rules(self, conn, job_id: str, gating_rules: GatingRules):
        """Store gating rules data."""
        conn.execute(text("""
            INSERT INTO job_gating_rules (job_id, visa_sponsorship, education_min, education_strict, security_clearance, location_strict)
            VALUES (:job_id, :visa_sponsorship, :education_min, :education_strict, :security_clearance, :location_strict)
            ON CONFLICT (job_id) DO UPDATE SET
                visa_sponsorship = EXCLUDED.visa_sponsorship,
                education_min = EXCLUDED.education_min,
                education_strict = EXCLUDED.education_strict,
                security_clearance = EXCLUDED.security_clearance,
                location_strict = EXCLUDED.location_strict
        """), {
            "job_id": job_id,
            "visa_sponsorship": gating_rules.visa_sponsorship,
            "education_min": gating_rules.education_min,
            "education_strict": gating_rules.education_strict,
            "security_clearance": gating_rules.security_clearance,
            "location_strict": gating_rules.location_strict
        })
    
    async def _store_requirements(self, conn, job_id: str, requirements: list[Requirement]):
        """Store job requirements."""
        # Clear existing requirements for this job
        conn.execute(text("DELETE FROM job_requirements WHERE job_id = :job_id"), {"job_id": job_id})
        
        # Insert new requirements
        for req in requirements:
            conn.execute(text("""
                INSERT INTO job_requirements (job_id, req_id, skill_id, priority, level, is_hard_filter, min_years, context, logic, options)
                VALUES (:job_id, :req_id, :skill_id, :priority, :level, :is_hard_filter, :min_years, :context, :logic, :options)
            """), {
                "job_id": job_id,
                "req_id": req.req_id,
                "skill_id": req.skill_id,
                "priority": req.priority,
                "level": req.level,
                "is_hard_filter": req.is_hard_filter,
                "min_years": req.min_years,
                "context": req.context,
                "logic": req.logic,
                "options": json.dumps(req.options) if req.options else "[]"
            })
    
    async def _store_competencies(self, conn, job_id: str, competencies: list[Competency]):
        """Store job competencies."""
        # Clear existing competencies for this job
        conn.execute(text("DELETE FROM job_competencies WHERE job_id = :job_id"), {"job_id": job_id})
        
        # Insert new competencies
        for comp in competencies:
            conn.execute(text("""
                INSERT INTO job_competencies (job_id, name, description, priority)
                VALUES (:job_id, :name, :description, :priority)
            """), {
                "job_id": job_id,
                "name": comp.name,
                "description": comp.description,
                "priority": comp.priority
            })
    
    async def _store_seniority_signals(self, conn, job_id: str, seniority_signals: SenioritySignals): 
        """Store seniority signals."""
        conn.execute(text("""
            INSERT INTO job_seniority_signals (job_id, target_level, keywords_found)
            VALUES (:job_id, :target_level, :keywords_found)
            ON CONFLICT (job_id) DO UPDATE SET
                target_level = EXCLUDED.target_level,
                keywords_found = EXCLUDED.keywords_found
        """), {
            "job_id": job_id,
            "target_level": seniority_signals.target_level,
            "keywords_found": json.dumps(seniority_signals.keywords_found) if seniority_signals.keywords_found else "[]"
        })
    
    async def get_job_data(self, job_id: str) -> Optional[JobDescription]:
        """
        Retrieve complete structured job data.
        
        Args:
            job_id: Job identifier
            
        Returns:
            JobDescription model if found, None otherwise
        """
        if not self.engine:
            return None
            
        try:
            with self.engine.connect() as conn:
                # Get main job data
                job_result = conn.execute(text("""
                    SELECT id, title, location, work_mode, clearance, is_valid, parsing_error
                    FROM jobs WHERE id = :job_id
                """), {"job_id": job_id}).fetchone()
                
                if not job_result:
                    return None
                
                # Get gating rules
                gating_result = conn.execute(text("""
                    SELECT visa_sponsorship, education_min, education_strict, security_clearance, location_strict
                    FROM job_gating_rules WHERE job_id = :job_id
                """), {"job_id": job_id}).fetchone()
                
                # Get requirements
                requirements_result = conn.execute(text("""
                    SELECT req_id, skill_id, priority, level, is_hard_filter, min_years, context, logic, options
                    FROM job_requirements WHERE job_id = :job_id
                """), {"job_id": job_id}).fetchall()
                
                # Get competencies
                competencies_result = conn.execute(text("""
                    SELECT name, description, priority
                    FROM job_competencies WHERE job_id = :job_id
                """), {"job_id": job_id}).fetchall()
                
                # Get seniority signals
                seniority_result = conn.execute(text("""
                    SELECT target_level, keywords_found
                    FROM job_seniority_signals WHERE job_id = :job_id
                """), {"job_id": job_id}).fetchone()
                
                # Construct JobDescription object
                return self._build_job_description(job_result, gating_result, requirements_result, competencies_result, seniority_result)
                
        except Exception as e:
            print(f"❌ Error retrieving job data: {e}")
            return None
    
    def _build_job_description(self, job_row, gating_row, requirements_rows, competencies_rows, seniority_row) -> JobDescription:
        """Build JobDescription from database rows."""
        
        # Job metadata
        job_metadata = JobMetadata(
            title=job_row.title,
            location=job_row.location,
            work_mode=job_row.work_mode or "unknown",
            clearance=job_row.clearance
        )
        
        # Gating rules
        gating_rules = GatingRules()
        if gating_row:
            gating_rules = GatingRules(
                visa_sponsorship=gating_row.visa_sponsorship,
                education_min=gating_row.education_min,
                education_strict=gating_row.education_strict,
                security_clearance=gating_row.security_clearance,
                location_strict=gating_row.location_strict
            )
        
        # Requirements
        requirements = []
        for req_row in requirements_rows:
            requirements.append(Requirement(
                req_id=req_row.req_id,
                skill_id=req_row.skill_id,
                priority=req_row.priority,
                level=req_row.level,
                is_hard_filter=req_row.is_hard_filter,
                min_years=req_row.min_years,
                context=req_row.context,
                logic=req_row.logic,
                options=json.loads(req_row.options) if req_row.options else []
            ))
        
        # Competencies
        competencies = []
        for comp_row in competencies_rows:
            competencies.append(Competency(
                name=comp_row.name,
                description=comp_row.description,
                priority=comp_row.priority
            ))
        
        # Seniority signals
        seniority_signals = SenioritySignals()
        if seniority_row:
            seniority_signals = SenioritySignals(
                target_level=seniority_row.target_level,
                keywords_found=json.loads(seniority_row.keywords_found) if seniority_row.keywords_found else []
            )
        
        return JobDescription(
            id=job_row.id,
            job_metadata=job_metadata,
            gating_rules=gating_rules,
            requirements=requirements,
            competencies=competencies,
            seniority_signals=seniority_signals,
            is_valid=job_row.is_valid,
            parsing_error=job_row.parsing_error
        )