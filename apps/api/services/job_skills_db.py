#!/usr/bin/env python3
"""
Job Skills Database Service
Saves extracted skills to database with Ronak's ontology integration
"""

import psycopg2
import psycopg2.extras
from typing import List, Dict
import uuid
from datetime import datetime
from core.config import DATABASE_URL

class JobSkillsDB:
    """Handles database operations for job skills integrated with Ronak's ontology"""
    
    def __init__(self, db_url: str = None):
        self.db_url = db_url or DATABASE_URL
    
    def save_job_skills(self, jobdiva_id: str, extracted_skills: List, analysis_metadata: Dict) -> Dict:
        """
        Saves extracted skills to job_skills table
        """
        with psycopg2.connect(self.db_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                
                # Clear existing skills for this job (if re-analyzing)
                cursor.execute("DELETE FROM job_skills WHERE jobdiva_id = %s", (jobdiva_id,))
                
                skills_saved = 0
                for skill in extracted_skills:
                    try:
                        cursor.execute("""
                            INSERT INTO job_skills (
                                jobdiva_id, skill_name, min_years, recent,
                                match_type, is_required, category
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s
                            )
                        """, (
                            jobdiva_id,
                            skill.normalized_name,
                            skill.min_years or 0,
                            False,
                            'Similar',
                            skill.importance == 'required',
                            getattr(skill, 'category', 'hard')
                        ))
                        skills_saved += 1
                    except Exception as e:
                        print(f"❌ Failed to save skill {skill.normalized_name}: {e}")
                
                conn.commit()
                
                print(f"✅ Saved {skills_saved} skills for job {jobdiva_id}")
                
                return {
                    'jobdiva_id': jobdiva_id,
                    'skills_saved': skills_saved,
                    'status': 'success'
                }
    
    def get_job_skills(self, jobdiva_id: str) -> List[Dict]:
        """Retrieve all skills for a job"""
        with psycopg2.connect(self.db_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT 
                        skill_name,
                        min_years,
                        recent,
                        match_type,
                        is_required,
                        category
                    FROM job_skills 
                    WHERE jobdiva_id = %s
                    ORDER BY skill_name
                """, (jobdiva_id,))
                return [dict(row) for row in cursor.fetchall()]
    
    def get_skills_summary(self, jobdiva_id: str) -> Dict:
        """Get summary of skills analysis for a job"""
        with psycopg2.connect(self.db_url) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                
                # Skills count by importance
                cursor.execute("""
                    SELECT 
                        importance_level,
                        COUNT(*) as count
                    FROM job_skills 
                    WHERE jobdiva_id = %s AND is_active = true
                    GROUP BY importance_level
                """, (jobdiva_id,))
                
                importance_counts = {row['importance_level']: row['count'] for row in cursor.fetchall()}
                
                # Total skills
                cursor.execute("""
                    SELECT COUNT(*) as total_skills
                    FROM job_skills 
                    WHERE jobdiva_id = %s AND is_active = true
                """, (jobdiva_id,))
                
                total_skills = cursor.fetchone()['total_skills']
                
                # Get extraction metadata
                cursor.execute("""
                    SELECT source_data 
                    FROM job_extraction_logs 
                    WHERE job_id = %s AND extraction_type = 'skills_extraction'
                    ORDER BY created_at DESC 
                    LIMIT 1
                """, (jobdiva_id,))
                
                metadata_row = cursor.fetchone()
                metadata = metadata_row['source_data'] if metadata_row else {}
                
                return {
                    'jobdiva_id': jobdiva_id,
                    'total_skills': total_skills,
                    'by_importance': importance_counts,
                    'analysis_metadata': metadata,
                    'has_skills': total_skills > 0
                }

# Example of complete integration flow
async def extract_and_save_job_skills(job_id: str, job_data: dict) -> dict:
    """
    Complete flow: Extract skills → Map to Ronak's ontology → Save to database
    """
    from services.job_skills_extractor import process_job_skills
    
    try:
        # Extract and map skills using Ronak's ontology
        analysis = await process_job_skills(job_id, job_data)
        
        # Save to database
        db_service = JobSkillsDB()
        save_result = db_service.save_job_skills(
            job_id=job_id,
            extracted_skills=analysis.extracted_skills,
            analysis_metadata=analysis.analysis_metadata
        )
        
        # Get summary
        summary = db_service.get_skills_summary(job_id)
        
        return {
            'status': 'success',
            'job_id': job_id,
            'skills_extracted': len(analysis.extracted_skills),
            'unmapped_skills': len(analysis.unmapped_skills),
            'skills_saved': save_result['skills_saved'],
            'summary': summary
        }
        
    except Exception as e:
        print(f"❌ Skills extraction failed for job {job_id}: {e}")
        return {
            'status': 'error',
            'job_id': job_id,
            'error': str(e)
        }