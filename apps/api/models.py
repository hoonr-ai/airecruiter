from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict

class Skill(BaseModel):
    value: str
    seniority: str = "Mid"      # Junior, Mid, Senior
    priority: str = "Must Have" # Must Have, Flexible
    years_experience: Optional[int] = None

class TitleCriterion(BaseModel):
    value: str
    match_type: str = "must"  # must, can, exclude
    years: int = 0
    recent: bool = False
    similar_terms: List[str] = []

class SkillCriterion(BaseModel):
    value: str
    match_type: str = "must"  # must, can, exclude
    years: int = 0
    recent: bool = False
    similar_terms: List[str] = []

class LocationCriterion(BaseModel):
    value: str
    radius: str = "25"  # miles radius

class ResumeMatchFilter(BaseModel):
    category: str
    value: str
    active: bool = True

class GroundedTitle(BaseModel):
    value: str
    min_years: int = 0
    recent: bool = False
    match_type: str = "Similar"
    required: str = "Preferred"

class ExtractedData(BaseModel):
    title: str
    summary: str
    hard_skills: List[Skill]
    soft_skills: List[str]
    experience_level: str
    location_type: str = "Onsite" # Remote, Hybrid, Onsite
    grounded_titles: List[GroundedTitle] = []

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

class SourcedCandidate(BaseModel):
    candidate_id: str
    source: str = "JobDiva"  # JobDiva, LinkedIn, VettedDB, etc.
    name: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    profile_url: Optional[str] = None
    image_url: Optional[str] = None
    data: Optional[Dict[str, Any]] = None  # Additional metadata
    status: str = "sourced"  # sourced, contacted, responded, etc.
    
    # Enhanced JobDiva integration fields
    jobdiva_candidate_id: Optional[str] = None
    jobdiva_resume_id: Optional[str] = None
    resume_text: Optional[str] = None
    candidate_type: str = "talent_search"  # job_applicant or talent_search

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
    job_id: Optional[str] = None
    location: Optional[str] = None
    # Enhanced filtering criteria — single source of truth for titles/skills.
    # Legacy flat `titles: List[TitleCriterion]` and `skills: List[Skill]`
    # fields were removed (2026-04) in favor of `title_criteria` /
    # `skill_criteria`. Callers should build the rich criterion shape.
    title_criteria: List[TitleCriterion] = []
    skill_criteria: List[SkillCriterion] = []
    locations: List[LocationCriterion] = []
    keywords: List[str] = []  # General keywords
    companies: List[str] = []  # Target companies
    resume_match_filters: List[ResumeMatchFilter] = []
    location_type: str = "Unspecified"
    sources: List[str] = ["JobDiva"]
    open_to_work: bool = True
    boolean_string: str = ""
    page: int = 1
    limit: int = 100

class JobFetchRequest(BaseModel):
    job_id: str

class CandidateMessageRequest(BaseModel):
    candidate_provider_id: str
    message: str
    source: str = "LinkedIn"

class CandidateSaveRecord(BaseModel):
    candidate_id: str
    name: str = "Unknown Candidate"
    email: Optional[str] = None
    phone: Optional[str] = None
    headline: Optional[str] = None
    location: Optional[str] = None
    profile_url: Optional[str] = None
    image_url: Optional[str] = None
    resume_id: Optional[str] = None
    resume_text: Optional[str] = None
    skills: Any = []
    experience_years: Any = 0
    source: str = "JobDiva"
    match_score: Any = 0.0
    is_selected: bool = False
    # Additional enrichment fields sent by frontend
    education: Optional[Any] = None
    certifications: Optional[Any] = None
    company_experience: Optional[Any] = None
    urls: Optional[Any] = None
    enhanced_info: Optional[Any] = None

class CandidatesSaveRequest(BaseModel):
    jobdiva_id: str
    candidates: List[CandidateSaveRecord]

class JobSyncFiltersRequest(BaseModel):
    sourcing_filters: Optional[Dict[str, Any]] = None
    resume_match_filters: Optional[List[Dict[str, Any]]] = None

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
    customer_name: Optional[str] = None
    
    # Step 1 fields
    title: Optional[str] = None
    recruiter_notes: Optional[str] = None
    work_authorization: Optional[str] = None
    
    # Step 2 fields
    enhanced_title: Optional[str] = None
    ai_description: Optional[str] = None
    selected_employment_types: List[str] = []
    recruiter_emails: List[str] = []
    screening_level: str = "L1.5"
    selected_job_boards: List[str] = []
    
    # Metadata (persisted for Step 1 UI consistency)
    city: Optional[str] = None
    state: Optional[str] = None
    priority: Optional[str] = None
    program_duration: Optional[str] = None
    max_allowed_submittals: Optional[int] = None
    pay_rate: Optional[str] = None
    openings: Optional[int] = None
    start_date: Optional[str] = None
    posted_date: Optional[str] = None
    status: Optional[str] = None
    bot_introduction: Optional[str] = None
    rubric: Optional[Dict[str, Any]] = None
    
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
    sourcing_filters: Optional[Dict[str, Any]] = None
    resume_match_filters: Optional[List[Dict[str, Any]]] = None

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


# External (non-JobDiva) job flow models
class ExternalJobCreateRequest(BaseModel):
    title: str
    description: str = ""
    customer_name: str = ""
    recruiter_notes: str = ""


class ManualCandidateRequest(BaseModel):
    name: str
    email: Optional[str] = ""
    phone: Optional[str] = ""
    resume_text: str
