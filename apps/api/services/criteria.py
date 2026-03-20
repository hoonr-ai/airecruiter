import logging
import uuid
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import sqlalchemy
from sqlalchemy import text
from models import JobCriterion, JobCriteriaResponse

# Minimal Ronak-style parsing logic 
# (Gradually integrated as requested)
import json
from openai import OpenAI
from dotenv import load_dotenv
from services.usage_logger import usage_logger

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
                    text("SELECT * FROM job_criteria WHERE job_id = :job_id"),
                    {"job_id": job_id}
                )
                rows = res.fetchall()
                return [
                    JobCriterion(
                        id=str(row._mapping["id"]),
                        name=row._mapping["name"],
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
        The core Step 3 logic: 
        1. Fetch AI JD from monitored_jobs.
        2. Use LLM to extract specific criteria strings.
        3. Save to job_criteria table.
        """
        if not self.engine: return []
        
        # 1. Fetch AI JD
        ai_description = ""
        try:
            with self.engine.connect() as conn:
                res = conn.execute(
                    text("SELECT ai_description FROM monitored_jobs WHERE job_id = :job_id"),
                    {"job_id": job_id}
                )
                row = res.fetchone()
                if row:
                    ai_description = row._mapping["ai_description"]
        except Exception as e:
            logger.error(f"Error fetching AI JD: {e}")
            return []

        if not ai_description:
            logger.warning(f"No AI description found for job {job_id}")
            return []

        # 2. Minimal Parser (LLM Call to get strings like in the image)
        try:
            prompt = f"""
            Task: Extract EXACTLY 8 specific, high-quality hiring criteria from the Job Description below.
            Format: Return a JSON object with a key 'criteria' containing a list of objects.
            Each object must have:
            - 'name': The requirement string (e.g., 'Experience with...')
            - 'importance': An integer from 1-5 (5=critical, 1=minor)
            - 'mandatory': Boolean (true if required, false if preferred)
            
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
                    importance = int(item.get("importance", 3))
                    is_required = bool(item.get("mandatory", False))
                    weight = round(importance / total_importance, 2)
                    
                    c_id = str(uuid.uuid4())
                    conn.execute(text("""
                        INSERT INTO job_criteria (id, job_id, name, weight, is_required, is_ai_generated, category)
                        VALUES (:id, :job_id, :name, :weight, :is_required, true, :cat)
                    """), {
                        "id": c_id, 
                        "job_id": job_id, 
                        "name": name, 
                        "weight": weight,
                        "is_required": is_required,
                        "cat": "Hard Filter"
                    })
                    
                    new_criteria.append(JobCriterion(
                        id=c_id,
                        name=name,
                        weight=weight,
                        is_required=is_required,
                        is_ai_generated=True
                    ))
                conn.commit()
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
                    weight = float(item.get("weight", 1.0))
                    is_required = bool(item.get("is_required", False))
                    is_ai_generated = bool(item.get("is_ai_generated", False))
                    category = item.get("category", "Hard Filter")
                    
                    conn.execute(text("""
                        INSERT INTO job_criteria (id, job_id, name, weight, is_required, is_ai_generated, category)
                        VALUES (:id, :job_id, :name, :weight, :is_required, :ai, :cat)
                    """), {
                        "id": c_id, 
                        "job_id": job_id, 
                        "name": name, 
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
