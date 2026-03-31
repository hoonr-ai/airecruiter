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
    job_id: Optional[str] = None  # Optional job_id for saving to database

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
    skill_id: Optional[str] = None # e.g. "SKL_BACKEND_JAVA"
    priority_score: int = 5 # 1-10 human friendly score
    weight: float = 1.0 # Calculated internal weight
    is_required: bool = False # True = Required, False = Preferred
    is_ai_generated: bool = True
    category: Optional[str] = "Hard Filter"

class JobCriteriaResponse(BaseModel):
    job_id: str
    criteria: List[JobCriterion]

class JobCriteriaUpdate(BaseModel):
    criteria: List[JobCriterion]

# Job Draft Models for Multi-Step Workflow
class JobDraftData(BaseModel):
    """Data model for job draft updates"""
    job_id: str
    jobdiva_id: Optional[str] = None
    current_step: int = 1
    user_session: str = "default"
    
    # Step 1 fields
    title: Optional[str] = None
    recruiter_notes: Optional[str] = None
    work_authorization: Optional[str] = None
    
    # Step 2 fields
    enhanced_title: Optional[str] = None
    ai_description: Optional[str] = None
    selected_employment_types: List[str] = []
    recruiter_emails: List[str] = []
    pair_level: str = "L1.5"
    selected_job_boards: List[str] = []
    
    # Progress tracking
    step1_completed: bool = False
    step2_completed: bool = False
    step3_completed: bool = False
    
    # Save metadata
    is_auto_saved: bool = False
    draft_notes: Optional[str] = None
    rubric: Optional[Dict] = None
    bot_introduction: Optional[str] = None
    screen_questions: List[Dict[str, Any]] = []

class JobDraftRequirement(BaseModel):
    """Model for draft requirements"""
    requirement_type: str  # 'skills', 'education', 'domain', 'customer_requirements'
    value: str
    field: Optional[str] = None
    priority: str = "Required"  # 'Required', 'Preferred'
    min_years: int = 0
    is_user_added: bool = False
    display_order: int = 0

class JobDraftRequirements(BaseModel):
    """Collection of draft requirements"""
    draft_id: str
    requirements: List[JobDraftRequirement]

class JobDraftResponse(BaseModel):
    """Response model for getting a job draft"""
    draft_id: str
    job_id: str
    current_step: int
    workflow_status: str
    draft_data: JobDraftData
    requirements: List[JobDraftRequirement] = []
    created_at: str
    updated_at: str
    last_step_completed_at: Optional[str] = None

class JobPublishRequest(BaseModel):
    """Request to publish a draft to live data"""
    draft_id: str
    final_validation: bool = True
    publish_notes: Optional[str] = None

# Job field update models
class JobBasicInfoUpdate(BaseModel):
    """Request to update basic job information"""
    employment_type: Optional[str] = None
    recruiter_notes: Optional[str] = None
    work_authorization: Optional[str] = None
    recruiter_emails: Optional[List[str]] = None

# Legacy / Unused but kept for safety if referenced elsewhere temporarily
class JobDescription(BaseModel):
    title: str
    content: str
    required_skills: List[str] = []
    min_experience_years: int = 0

# =====================================================
# RONAK SKILLS INTEGRATION MODELS  
# =====================================================

class SkillsExtractionRequest(BaseModel):
    """Request to extract and map skills from job descriptions using Ronak's ontology"""
    job_id: str
    jobdiva_description: Optional[str] = None
    ai_description: Optional[str] = None  
    recruiter_notes: Optional[str] = None

class ExtractedSkillResponse(BaseModel):
    """Individual skill extracted from job descriptions"""
    skill_id: str          # Ronak's skill_nodes.slug 
    normalized_name: str   # Ronak's display name
    original_text: str     # Text context from job description
    importance: str        # 'required', 'preferred', 'nice-to-have'
    min_years: int        # Minimum experience required
    confidence: float     # AI confidence score (0.0-1.0)

class SkillsExtractionResponse(BaseModel):
    """Response from skills extraction and mapping process"""
    job_id: str
    extracted_skills: List[ExtractedSkillResponse]
    unmapped_skills: List[str]  # Skills AI found but couldn't map to Ronak's ontology
    analysis_metadata: Dict[str, Any]
    mapping_rate: float

class JobSkillsSummaryResponse(BaseModel):
    """Summary of skills stored for a job"""
    job_id: str
    total_skills: int
    by_importance: Dict[str, int]  # {"required": 5, "preferred": 3}
    analysis_metadata: Dict[str, Any]
