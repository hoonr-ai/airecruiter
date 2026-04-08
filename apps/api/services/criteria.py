import logging
import uuid
import os
import sys
import json
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import sqlalchemy
from sqlalchemy import text
from openai import OpenAI
from core.config import OPENAI_API_KEY, DATABASE_URL

# Add Ronak's JD Parser to path dynamically
# Project root is 2 levels up from apps/api/services/
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
jd_parser_src = os.path.join(project_root, "JD_Parser_Ronak", "src")

if os.path.exists(jd_parser_src):
    sys.path.append(jd_parser_src)
    try:
        from jd_parser.jdparser.jd_parser import parse_jd
    except ImportError:
        logging.warning("⚠️ JD_Parser_Ronak found but could not import parse_jd.")
        parse_jd = None
else:
    logging.warning(f"⚠️ JD_Parser_Ronak not found at {jd_parser_src}. Criteria parsing may be limited.")
    parse_jd = None

from models import JobCriterion, JobCriteriaResponse
from services.jobdiva import jobdiva_service

client = OpenAI(api_key=OPENAI_API_KEY)

logger = logging.getLogger(__name__)

def parse_toon_to_criteria(toon_output: str) -> List[Dict[str, Any]]:
    """
    Parse TOON format output from Ronak's JD Parser into criteria format.
    Extracts skills from REQUIRED_SKILLS and OPTIONAL_SKILLS sections.
    """
    criteria_list = []
    
    # Split into lines and clean up
    lines = [line.strip() for line in toon_output.split('\n') if line.strip()]
    
    current_section = None
    current_skill = {}
    
    for line in lines:
        # Detect sections
        if line.startswith("SECTION"):
            if "REQUIRED_SKILLS" in line:
                current_section = "required"
            elif "OPTIONAL_SKILLS" in line:
                current_section = "optional"
            else:
                current_section = None
            continue
            
        if current_section is None:
            continue
            
        # Start of new skill block
        if line == "SKILL":
            # Save previous skill if exists
            if current_skill:
                criteria_list.append(current_skill)
            current_skill = {"section": current_section}
            continue
        
        # Parse skill attributes
        if current_skill:
            if line.startswith("SKILL_ID"):
                current_skill["skill_id"] = line.replace("SKILL_ID", "").strip()
            elif line.startswith("CANONICAL_NAME"):
                current_skill["name"] = line.replace("CANONICAL_NAME", "").strip()
            elif line.startswith("MINIMUM_YEARS"):
                try:
                    current_skill["min_years"] = int(line.replace("MINIMUM_YEARS", "").strip())
                except:
                    current_skill["min_years"] = 0
            elif line.startswith("IMPORTANCE_SCORE"):
                try:
                    current_skill["importance_score"] = int(line.replace("IMPORTANCE_SCORE", "").strip())
                except:
                    current_skill["importance_score"] = 3
            elif line.startswith("MANDATORY"):
                mandatory = line.replace("MANDATORY", "").strip().lower()
                current_skill["is_required"] = mandatory == "true"
    
    # Add the last skill
    if current_skill:
        criteria_list.append(current_skill)
    
    # Convert to our criteria format
    formatted_criteria = []
    for skill in criteria_list:
        if "name" in skill and skill["name"]:
            # Map importance score (1-5) to priority score (1-10)
            importance = skill.get("importance_score", 3)
            priority_score = min(10, max(1, importance * 2))
            
            # Determine if required based on section and MANDATORY flag
            is_required = (skill.get("section") == "required" or 
                          skill.get("is_required", False))
            
            formatted_criteria.append({
                "name": skill["name"],
                "skill_id": skill.get("skill_id", ""),
                "priority_score": priority_score,
                "weight": round(priority_score / 10.0, 2),
                "is_required": is_required,
                "is_ai_generated": True,
                "category": "Hard Filter",
                "min_years": skill.get("min_years", 0)
            })
    
    return formatted_criteria

def extract_customer_requirements(job_data: dict) -> List[Dict[str, Any]]:
    """
    Extract customer-specific requirements from job data.
    Only extracts from AI JD, job notes, or JobDiva reference - strict source control.
    """
    customer_criteria = []
    
    # Get text sources - STRICT extraction only from these sources
    ai_description = job_data.get("ai_description", "")
    recruiter_notes = job_data.get("recruiter_notes") or job_data.get("recruiter_remarks") or job_data.get("job_notes") or ""
    jobdiva_description = job_data.get("description", "")
    
    # Combine all source text
    combined_text = f"{ai_description}\n{recruiter_notes}\n{jobdiva_description}".lower()
    
    # Common customer requirement patterns - only extract what's explicitly mentioned
    requirement_patterns = {
        "citizenship": r"(us citizen|citizenship|green card|work authorization|eligible to work)",
        "clearance": r"(security clearance|secret clearance|top secret|clearance required)",
        "location": r"(onsite|on-site|local|remote|hybrid|must be located)",
        "employment": r"(w2|1099|corp to corp|c2c|contract|permanent|full-time|part-time)",
        "travel": r"(travel required|no travel|up to \d+% travel|willing to travel)",
        "background": r"(background check|drug test|drug screening)",
        "education": r"(degree required|bachelor|master|phd|certification required)",
        "experience_level": r"(senior level|junior|mid-level|entry level)",
    }
    
    for req_type, pattern in requirement_patterns.items():
        matches = re.findall(pattern, combined_text, re.IGNORECASE)
        if matches:
            # Take the first match and create a crisp requirement
            requirement = matches[0]
            customer_criteria.append({
                "name": requirement.title(),
                "skill_id": f"CUST_{req_type.upper()}",
                "priority_score": 8,  # High priority for customer requirements
                "weight": 0.8,
                "is_required": True,
                "is_ai_generated": True,
                "category": "Customer Requirement"
            })
    
    return customer_criteria

class CriteriaService:
    def __init__(self):
        self.db_url = DATABASE_URL
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
        Enhanced step 3: Use Ronak's JD Parser for STRICT skills extraction.
        1. Fetch Job from JobDiva (includes AI JD, job notes, JobDiva reference).
        2. Use Ronak's JD Parser to extract crisp criteria in TOON format.
        3. Extract customer requirements from job-specific sources only.
        4. Convert to criteria format and save to job_criteria table.
        """
        logger.info(f"🔄 Starting strict criteria extraction for job {job_id} using Ronak's JD Parser")
        
        # 1. Fetch Job from JobDiva (includes AI JD if exists, and raw JD)
        job = await jobdiva_service.get_job_by_id(job_id)
        if not job: 
            logger.error(f"Job {job_id} not found")
            return []

        # Prepare input for Ronak's JD Parser - STRICT source control
        ai_description = job.get("ai_description", "")
        jobdiva_description = job.get("description", "")  # JobDiva reference
        recruiter_notes = job.get("recruiter_notes") or job.get("recruiter_remarks") or job.get("job_notes") or ""
        
        # Create TOON_JOB format input for Ronak's parser
        toon_input = f"""
JOB_TITLE: {job.get("title", "")}
AI_DESCRIPTION: {ai_description}
RECRUITER_REMARKS: {recruiter_notes}
JOBDIVA_DESCRIPTION: {jobdiva_description}
JOB_DIVA_ID: {job_id}
        """.strip()
        
        if not any([ai_description, jobdiva_description, recruiter_notes]):
            logger.warning(f"No valid source data found for job {job_id}")
            return []

        try:
            logger.info(f"🧠 Calling Ronak's JD Parser for strict skills extraction...")
            
            # 2. Use Ronak's JD Parser for STRICT extraction
            toon_output = parse_jd(toon_input)
            logger.info(f"✅ Ronak's parser completed. Processing TOON output...")
            
            # Parse TOON format to criteria
            skills_criteria = parse_toon_to_criteria(toon_output)
            
            # 3. Extract customer requirements - STRICT extraction only
            customer_criteria = extract_customer_requirements(job)
            
            # Combine all criteria
            all_criteria = skills_criteria + customer_criteria
            
            # Ensure we have crisp names (no long sentences)
            for criterion in all_criteria:
                name = criterion["name"]
                if len(name) > 50 or "," in name or " and " in name:
                    # Split long names into crisp terms
                    parts = re.split(r'[,/&]|\s+and\s+', name)
                    criterion["name"] = parts[0].strip().title()
            
            # Limit to 12 criteria max, prioritize by importance
            all_criteria.sort(key=lambda x: x["priority_score"], reverse=True)
            all_criteria = all_criteria[:12]
            
            # 4. Save to database
            new_criteria = []
            if self.engine:
                with self.engine.connect() as conn:
                    # Clear existing for this job
                    conn.execute(text("DELETE FROM job_criteria WHERE job_id = :job_id"), {"job_id": job_id})
                    
                    for item in all_criteria:
                        c_id = str(uuid.uuid4())
                        conn.execute(text("""
                            INSERT INTO job_criteria (id, job_id, name, skill_id, priority_score, weight, is_required, is_ai_generated, category)
                            VALUES (:id, :job_id, :name, :skill_id, :priority_score, :weight, :is_required, :ai, :cat)
                        """), {
                            "id": c_id, 
                            "job_id": job_id, 
                            "name": item["name"], 
                            "skill_id": item["skill_id"],
                            "priority_score": item["priority_score"],
                            "weight": item["weight"],
                            "is_required": item["is_required"],
                            "ai": item["is_ai_generated"],
                            "cat": item["category"]
                        })
                        
                        new_criteria.append(JobCriterion(
                            id=c_id,
                            name=item["name"],
                            skill_id=item["skill_id"],
                            priority_score=item["priority_score"],
                            weight=item["weight"],
                            is_required=item["is_required"],
                            is_ai_generated=item["is_ai_generated"],
                            category=item["category"]
                        ))
                    conn.commit()
            
            # Sort for UI consistency
            new_criteria.sort(key=lambda x: x.priority_score, reverse=True)
            
            logger.info(f"✅ Extracted and saved {len(new_criteria)} crisp criteria using Ronak's parser")
            logger.info(f"📊 Skills: {len(skills_criteria)}, Customer Requirements: {len(customer_criteria)}")
            
            return new_criteria
            
        except Exception as e:
            logger.error(f"❌ Error in Ronak's strict extraction for job {job_id}: {e}")
            # Return empty list instead of fallback to maintain strict extraction
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
