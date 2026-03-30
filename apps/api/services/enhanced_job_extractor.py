import uuid
from typing import List, Optional
from openai import AsyncOpenAI
from pydantic import BaseModel
from core.config import OPENAI_API_KEY

from core.models import (
    JobDescription, 
    JobMetadata, 
    GatingRules, 
    Requirement, 
    Competency, 
    SenioritySignals
)
from services.job_storage import JobStorageService
from services.usage_logger import usage_logger

class EnhancedJobExtractor:
    """Enhanced job extractor that produces full JobDescription models."""
    
    def __init__(self):
        self.api_key = OPENAI_API_KEY
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        self.storage_service = JobStorageService()
    
    async def extract_and_store_job(self, raw_description: str, job_id: Optional[str] = None, source_job_id: Optional[str] = None) -> JobDescription:
        """
        Extract structured JobDescription from raw text and store it.
        
        Args:
            raw_description: Raw job posting text
            job_id: Optional job ID, will generate UUID if not provided
            source_job_id: Reference to original monitored_jobs entry
            
        Returns:
            Structured JobDescription model
        """
        if not job_id:
            job_id = str(uuid.uuid4())
            
        # Extract structured data
        job_description = await self._extract_job_description(raw_description, job_id)
        
        # Store in database
        stored_successfully = await self.storage_service.store_job_data(
            job_description, 
            source_job_id=source_job_id,
            raw_description=raw_description
        )
        
        if stored_successfully:
            print(f"✅ Job {job_id} extracted and stored successfully")
        else:
            print(f"⚠️ Job {job_id} extracted but storage failed")
            
        return job_description
    
    async def _extract_job_description(self, raw_description: str, job_id: str) -> JobDescription:
        """Extract structured JobDescription using LLM."""
        
        if not self.client:
            # Mock fallback for testing
            return self._create_mock_job_description(job_id)
        
        try:
            model = "gpt-4o"
            
            system_prompt = """You are an expert HR system that extracts structured job data.

Extract the following from job descriptions:

1. JOB METADATA:
   - title: Job title
   - location: Location (city, state or "Remote")
   - work_mode: "remote", "onsite", "hybrid", or "unknown"
   - clearance: Security clearance if mentioned, otherwise "None"

2. GATING RULES (Hard filters):
   - visa_sponsorship: true/false if mentioned
   - education_min: "Bachelors", "Masters", "PhD" if required
   - education_strict: true if education is strict requirement
   - security_clearance: specific clearance if required
   - location_strict: true if must be in specific location

3. REQUIREMENTS (Skills and experience):
   - req_id: unique ID (req_1, req_2, etc.)
   - skill_id: skill name (Python, React, etc.)
   - priority: "must_have" (default) or "nice_to_have" (if says preferred/plus/bonus)
   - level: "junior", "mid", "senior" based on context
   - is_hard_filter: true for must_have skills
   - min_years: minimum years if specified
   - context: any additional context

4. COMPETENCIES (Soft skills):
   - name: competency name
   - priority: "must_have" or "nice_to_have"

5. SENIORITY SIGNALS:
   - target_level: "junior", "mid", "senior", "staff", "principal" etc.
   - keywords_found: list of seniority-related keywords found

Return structured JSON following the JobDescription schema."""
            
            response = await self.client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Extract structured data from this job description:\n\n{raw_description}"}
                ],
                response_format=JobDescription,
            )
            
            # Log usage
            usage_logger.log_usage(
                service="enhanced_job_extractor",
                model=model,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens
            )
            
            job_description = response.choices[0].message.parsed
            job_description.id = job_id  # Ensure ID is set
            
            return job_description
            
        except Exception as e:
            print(f"❌ Error in job extraction: {e}")
            # Return a basic valid structure on error
            return JobDescription(
                id=job_id,
                job_metadata=JobMetadata(),
                gating_rules=GatingRules(),
                requirements=[],
                competencies=[],
                seniority_signals=SenioritySignals(),
                is_valid=False,
                parsing_error=str(e)
            )
    
    def _create_mock_job_description(self, job_id: str) -> JobDescription:
        """Create mock JobDescription for testing."""
        return JobDescription(
            id=job_id,
            job_metadata=JobMetadata(
                title="Mock Software Engineer",
                location="Remote",
                work_mode="remote",
                clearance="None"
            ),
            gating_rules=GatingRules(
                visa_sponsorship=False,
                education_min="Bachelors",
                education_strict=False,
                location_strict=False
            ),
            requirements=[
                Requirement(
                    req_id="req_1",
                    skill_id="Python",
                    priority="must_have",
                    level="mid",
                    min_years=3
                ),
                Requirement(
                    req_id="req_2", 
                    skill_id="React",
                    priority="nice_to_have",
                    level="mid",
                    min_years=2
                )
            ],
            competencies=[
                Competency(name="Communication", priority="must_have"),
                Competency(name="Problem Solving", priority="must_have")
            ],
            seniority_signals=SenioritySignals(
                target_level="mid",
                keywords_found=["3+ years", "experience"]
            ),
            is_valid=True,
            parsing_error=None
        )