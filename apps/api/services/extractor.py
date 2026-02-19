import os
import json
from openai import AsyncOpenAI
from pydantic import BaseModel
from typing import List, Optional

from models import ExtractedData, Skill

class LLMExtractor:
    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=api_key) if api_key else None

    async def extract_from_jd(self, text: str) -> ExtractedData:
        """
        Uses OpenAI to parse text into structured skills.
        """
        from models import ExtractedData, Skill

        if not self.client:
             # MOCK FALLBACK
             return ExtractedData(
                 title="Mock Job Title",
                 summary="Mock Summary of the JD",
                 hard_skills=[
                     Skill(name="MockSkill1", seniority="Senior", priority="Must Have"),
                     Skill(name="MockSkill2", seniority="Mid", priority="Flexible")
                 ],
                 soft_skills=["Communication"],
                 experience_level="Senior",
                 location_type="Remote"
             )
        
        try:
            response = await self.client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": "You are an expert HR Tech extraction engine. Extract the Job Description details into the requested JSON structure. For HARD SKILLS, determine the required Seniority (Junior/Mid/Senior based on context) and Priority (Must Have vs Flexible/Preferred). Also determine Location Type (Remote, Hybrid, Onsite)."},
                    {"role": "user", "content": text},
                ],
                response_format=ExtractedData,
            )
            return response.choices[0].message.parsed
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
