import json
from openai import AsyncOpenAI
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from services.usage_logger import usage_logger
from core.config import OPENAI_API_KEY
from services.taxonomy_service import extract_grounded_rubric

from models import ExtractedData, Skill, GroundedTitle

class LLMExtractor:
    def __init__(self):
        self.client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

    async def extract_from_jd(self, text: str) -> ExtractedData:
        """
        AI-powered Job Description Extraction with Deterministic Taxonomy Grounding.
        
        Workflow:
        1. Contextual Extraction via GPT-4o-mini (Summary, Titles, Initial Skills).
        2. Deterministic Grounding (Pass 1: Fuzzy, Pass 2: LLM Refinement) against 50k+ master records.
        3. Standardized Output for further pipeline steps.
        """
        if not self.client:
            return ExtractedData(title="Error", summary="API Key not configured", hard_skills=[], soft_skills=[], experience_level="Unknown")

        try:
            # Step 1: Base Extraction
            response = await self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Extract structured job information. Ensure summary is professional and concise."},
                    {"role": "user", "content": text}
                ],
                response_format=ExtractedData,
            )

            # Log usage
            usage_logger.log_usage(
                service="jd_extractor",
                model="gpt-4o-mini",
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens
            )

            extracted = response.choices[0].message.parsed
            
            # Step 2: Integrated Deterministic Grounding (Hybrid Discovery -> Fuzzy Match)
            try:
                # Use raw text for 100% recall against taxonomy
                grounded = await extract_grounded_rubric(
                    job_text=text,
                    job_title=extracted.title,
                    client=self.client,
                    max_skills=12,
                    max_titles=5
                )
                
                # Replace AI-guessed skills with 100% grounded technical skills
                if grounded.get("hard_skills"):
                    extracted.hard_skills = [
                        Skill(
                            value=s["value"], 
                            seniority=s.get("seniority", "Mid"), 
                            priority=s.get("required", "Must Have"),
                            years_experience=s.get("minYears", 3)
                        )
                        for s in grounded["hard_skills"]
                    ]
                    
                # Populate grounded alternative titles (Roles)
                extracted.grounded_titles = [
                    GroundedTitle(
                        value=t["value"],
                        min_years=t.get("minYears", 0),
                        recent=t.get("recent", False),
                        match_type=t.get("matchType", "Similar"),
                        required=t.get("required", "Preferred")
                    )
                    for t in grounded.get("extra_titles", [])
                ]
                
            except Exception as tax_err:
                print(f"⚠️ Taxonomy grounding failed in Step 2: {tax_err}")

            return extracted
            
        except Exception as e:
            print(f"Extraction Error: {e}")
            return ExtractedData(
                title="Error", 
                summary="Error extracting", 
                hard_skills=[], 
                soft_skills=[], 
                experience_level="Unknown",
                location_type="Onsite"
            )

llm_extractor = LLMExtractor()
