from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict

class Skill(BaseModel):
    name: str
    seniority: str = "Mid"      # Junior, Mid, Senior
    priority: str = "Must Have" # Must Have, Flexible
    years_experience: Optional[int] = None

class ExtractedData(BaseModel):
    title: str
    summary: str
    hard_skills: List[Skill]
    soft_skills: List[str]
    experience_level: str
    location_type: str = "Onsite" # Remote, Hybrid, Onsite

# Used for frontend response mainly
class ParsedJobResponse(ExtractedData):
    job_id: Optional[str] = None

class CandidateAnalysisRequest(BaseModel):
    job_description: str
    structured_jd: Optional[Dict[str, Any]] = None # For passing the JSON directly
    candidates: List[Dict[str, Any]]

class CandidateAnalysisResponse(BaseModel):
    results: List[Dict[str, Any]] # List of { candidate_id, score, reasoning }
    name: str
    email: str
    skills: List[str]
    experience_years: int
    resume_text: str = ""

class ParsedJobRequest(BaseModel):
    text: str

class CandidateProfile(BaseModel):
    id: str
    name: str
    email: str
    skills: List[str]
    experience_years: int
    resume_text: str = ""
    source: str = "JobDiva" # JobDiva, Vetted, etc.
    match_score: float = 0.0

class MatchResult(BaseModel):
    candidate: CandidateProfile
    match_percentage: int
    missing_skills: List[str]
    explainability: List[str]

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []

class ChatResponse(BaseModel):
    response: str
    
class CandidateSearchRequest(BaseModel):
    skills: List[Skill]
    location: Optional[str] = None
    location_type: str = "Unspecified"
    sources: List[str] = ["VettedDB", "JobDiva"]  # Filter by source: VettedDB, JobDiva, LinkedIn
    open_to_work: bool = False
    page: int = 1
    limit: int = 100

class JobFetchRequest(BaseModel):
    job_id: str

class CandidateMessageRequest(BaseModel):
    candidate_provider_id: str
    message: str
    source: str = "LinkedIn"

# Job Criteria Models (Iterative Step 1)
class JobCriterion(BaseModel):
    id: Optional[str] = None
    name: str # e.g. "2+ years of Accounts Payable experience"
    weight: float = 1.0
    is_required: bool = False # True = Required, False = Preferred
    is_ai_generated: bool = True
    category: Optional[str] = "Hard Filter"

class JobCriteriaResponse(BaseModel):
    job_id: str
    criteria: List[JobCriterion]

class JobCriteriaUpdate(BaseModel):
    criteria: List[JobCriterion]

# Legacy / Unused but kept for safety if referenced elsewhere temporarily
class JobDescription(BaseModel):
    title: str
    content: str
    required_skills: List[str] = []
    min_experience_years: int = 0
