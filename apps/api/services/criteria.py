import logging
import uuid
import os
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import sqlalchemy
from sqlalchemy import text
from openai import OpenAI
from dotenv import load_dotenv

from models import JobCriterion, JobCriteriaResponse
from services.usage_logger import usage_logger
from services.jobdiva import jobdiva_service

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

logger = logging.getLogger(__name__)

class CriteriaService:
    def __init__(self):
        self.db_url = os.getenv("DATABASE_URL")
        if self.db_url and self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://")
        
        self.engine = None
        if self.db_url:
            self.engine = sqlalchemy.create_engine(self.db_url)

    def get_job_criteria(self, job_id: str) -> List[JobCriterion]:
        """Fetch existing criteria from DB."""
        if not self.engine: return []
        try:
            with self.engine.connect() as conn:
                res = conn.execute(
                    text("SELECT * FROM job_criteria WHERE job_id = :job_id ORDER BY priority_score DESC, created_at ASC"),
                    {"job_id": job_id}
                )
                rows = res.fetchall()
                return [
                    JobCriterion(
                        id=str(row._mapping["id"]),
                        name=row._mapping["name"],
                        skill_id=row._mapping.get("skill_id"),
                        priority_score=row._mapping.get("priority_score", 5),
                        weight=row._mapping["weight"],
                        is_required=row._mapping["is_required"],
                        is_ai_generated=row._mapping["is_ai_generated"],
                        category=row._mapping.get("category", "Hard Filter")
                    ) for row in rows
                ]
        except Exception as e:
            logger.error(f"Error fetching criteria: {e}")
            return []

    async def generate_and_save_criteria(self, job_id: str) -> List[JobCriterion]:
        """
        The core step: 
        1. Fetch Job from JobDiva (includes AI JD if exists).
        2. Use LLM to extract crisp criteria.
        3. Save to job_criteria table.
        """
        # 1. Fetch Job from JobDiva (includes AI JD if exists, and raw JD)
        job = await jobdiva_service.get_job_by_id(job_id)
        if not job: return []

        ai_description = job.get("ai_description")
        if not ai_description:
            logger.warning(f"No AI description found for job {job_id}, falling back to raw description")
            ai_description = job.get("description")
            if not ai_description:
                return []

        # 2. Minimal Parser (LLM Call to get strings like in the image)
        try:
            prompt = f"""
            Task: Extract EXACTLY 8 crisp and compact hiring criteria from the Job Description below.
            
            Rules:
            1. Keep names brief (e.g., 'Java', 'React', 'Problem Solving') - avoid long sentences.
            2. Assign a 'priority_score' from 1-10 (10=Highest/Critical, 1=Minor).
            3. Assign a 'skill_id' following Ronak's normalization (SKL_<DOMAIN>_<SKILL>), e.g., SKL_BACKEND_JAVA.
            4. Identify if it is 'mandatory' (true for required, false for preferred).
            
            Format: Return a JSON object with a key 'criteria' containing a list of objects.
            Each object: {{ "name": "...", "priority_score": 10, "skill_id": "SKL_...", "mandatory": true }}
            
            Job Description:
            {ai_description}
            
            Return ONLY the valid JSON object with exactly 8 items.
            """
            
            model = "gpt-4o-mini"
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={ "type": "json_object" }
            )
            
            # Log Usage
            usage_logger.log_usage(
                service="criteria_generator",
                model=model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                job_id=job_id
            )
            
            content = response.choices[0].message.content
            data = json.loads(content)
            
            # Robust extraction of the list
            criteria_list = []
            if isinstance(data, list):
                criteria_list = data
            elif isinstance(data, dict):
                # Try common keys or just take the first list found
                criteria_list = data.get("criteria") or data.get("results")
                if not isinstance(criteria_list, list):
                    for val in data.values():
                        if isinstance(val, list):
                            criteria_list = val
                            break
            
            if not isinstance(criteria_list, list):
                logger.error(f"LLM returned non-list data: {data}")
                return []

            # Step 4: Weight Normalization (Ronak's logic)
            total_importance = sum(int(c.get("importance", 3)) for c in criteria_list if isinstance(c, dict))
            if total_importance == 0: total_importance = 1

            # 3. Save to DB
            new_criteria = []
            with self.engine.connect() as conn:
                # Clear existing for this job
                conn.execute(text("DELETE FROM job_criteria WHERE job_id = :job_id"), {"job_id": job_id})
                
                for item in criteria_list:
                    if not isinstance(item, dict): continue
                    
                    name = item.get("name", "")
                    priority_score = int(item.get("priority_score", 5))
                    skill_id = item.get("skill_id", "")
                    is_required = bool(item.get("mandatory", False))
                    
                    # Calculate weight internally for compatibility (0.0 to 1.0)
                    weight = round(priority_score / 10.0, 2)
                    
                    c_id = str(uuid.uuid4())
                    conn.execute(text("""
                        INSERT INTO job_criteria (id, job_id, name, skill_id, priority_score, weight, is_required, is_ai_generated, category)
                        VALUES (:id, :job_id, :name, :skill_id, :priority_score, :weight, :is_required, true, :cat)
                    """), {
                        "id": c_id, 
                        "job_id": job_id, 
                        "name": name, 
                        "skill_id": skill_id,
                        "priority_score": priority_score,
                        "weight": weight,
                        "is_required": is_required,
                        "cat": "Hard Filter"
                    })
                    
                    new_criteria.append(JobCriterion(
                        id=c_id,
                        name=name,
                        skill_id=skill_id,
                        priority_score=priority_score,
                        weight=weight,
                        is_required=is_required,
                        is_ai_generated=True
                    ))
                conn.commit()
            
            # Final sort for immediate UI consistency
            new_criteria.sort(key=lambda x: x.priority_score, reverse=True)
            return new_criteria
            
        except Exception as e:
            logger.error(f"Error generating criteria: {e}")
            return []

    def save_criteria(self, job_id: str, criteria_list: List[Dict[str, Any]]) -> bool:
        """Manually save/overwrite criteria for a job."""
        if not self.engine: return False
        try:
            with self.engine.connect() as conn:
                # Clear existing for this job
                conn.execute(text("DELETE FROM job_criteria WHERE job_id = :job_id"), {"job_id": job_id})
                
                for item in criteria_list:
                    c_id = item.get("id") or str(uuid.uuid4())
                    name = item.get("name", "")
                    skill_id = item.get("skill_id", "")
                    priority_score = int(item.get("priority_score", 5))
                    weight = float(item.get("weight", priority_score / 10.0))
                    is_required = bool(item.get("is_required", False))
                    is_ai_generated = bool(item.get("is_ai_generated", False))
                    category = item.get("category", "Hard Filter")
                    
                    conn.execute(text("""
                        INSERT INTO job_criteria (id, job_id, name, skill_id, priority_score, weight, is_required, is_ai_generated, category)
                        VALUES (:id, :job_id, :name, :skill_id, :priority_score, :weight, :is_required, :ai, :cat)
                    """), {
                        "id": c_id, 
                        "job_id": job_id, 
                        "name": name, 
                        "skill_id": skill_id,
                        "priority_score": priority_score,
                        "weight": weight,
                        "is_required": is_required,
                        "ai": is_ai_generated,
                        "cat": category
                    })
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error saving criteria: {e}")
            return False

criteria_service = CriteriaService()
