from pydantic import BaseModel, Field
from typing import List, Optional

class JobDescription(BaseModel):
    title: str
    content: str
    required_skills: List[str] = []
    min_experience_years: int = 0

class CandidateProfile(BaseModel):
    id: str
    name: str
    email: str
    skills: List[str]
    experience_years: int
    resume_text: str = ""

class MatchResult(BaseModel):
    candidate: CandidateProfile
    match_percentage: int
    missing_skills: List[str]
    explainability: List[str]
