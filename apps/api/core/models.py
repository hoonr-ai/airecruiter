from typing import List, Optional, Literal, Dict
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# Fundamental Units
# -----------------------------------------------------------------------------

class EvidenceSource(BaseModel):
    section: Literal["experience", "education", "projects", "summary"] = Field(
        ..., description="Where in the document this was found"
    )
    verbatim: str = Field(..., description="The exact text snippet")
    inferred_dates: Optional[List[str]] = Field(
        None, description="ISO8601 dates inferred from context (e.g. ['2023-01', '2024-01'])"
    )
    context_level: Literal["practitioner", "expert", "learner", "unknown"] = Field(
        "unknown", description="Level of expertise inferred from verbs (e.g. 'Built' vs 'Learned')"
    )

class ComputedStats(BaseModel):
    months_experience: int = Field(0, description="Total months of experience with this skill")
    recency_decay: float = Field(1.0, ge=0.0, le=1.0, description="1.0 = Currently using, 0.0 = Ancient history")
    evidence_confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in the extraction (1.0 = Explicit mention)")

class CandidateSkill(BaseModel):
    skill_id: str = Field(..., description="Canonical slug")
    evidence_sources: List[EvidenceSource] = Field(default_factory=list)
    computed_stats: ComputedStats

# -----------------------------------------------------------------------------
# Advanced Candidate Contract
# -----------------------------------------------------------------------------

class CandidateMetadata(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    links: List[str] = []

class ComputedCandidateStats(BaseModel):
    total_yoe: float = 0.0
    avg_tenure_months: float = 0.0
    management_experience_years: float = 0.0

class TimelineSkill(BaseModel):
    skill_id: str
    context: Optional[str] = None

class TimelineEntry(BaseModel):
    company: Optional[str] = None
    title_raw: Optional[str] = None
    title_norm: Optional[str] = None
    start_date: Optional[str] = None # YYYY-MM
    end_date: Optional[str] = None # YYYY-MM or "Present"
    domain_tags: List[str] = []
    extracted_skills: List[TimelineSkill] = [] 

class EducationEntry(BaseModel):
    degree: Optional[str] = Field(None, description="Degree obtained (e.g. 'BS', 'Bachelor of Science', 'PhD').")
    field: Optional[str] = Field(None, description="Major or field of study (e.g. 'Computer Science', 'Economics').")
    institution: Optional[str] = Field(None, description="University or Institution name.")
    year: Optional[int] = Field(None, description="Year of graduation (e.g. 2023).")

class SkillProfileEntry(BaseModel):
    skill_slug: str # Added so we can put it in a list
    total_months: int = 0
    last_used: Optional[str] = None
    competency_level: Optional[str] = None
    sources: List[str] = []

class CandidateProfile(BaseModel):
    id: str
    candidate_metadata: CandidateMetadata
    computed_stats: ComputedCandidateStats
    timeline: List[TimelineEntry] = []
    skill_profile: List[SkillProfileEntry] = [] # Changed from Dict to List for strict extraction
    education: List[EducationEntry] = []
    competencies: List[str] = [] # List of soft skills/competencies extracted from resume
    # Legacy support (optional, but good for backward compat if needed temporarily)
    skills: List[CandidateSkill] = Field(default_factory=list)
    
    # Validation Flags
    is_valid: bool = Field(..., description="Is this a valid Resume/CV? Set False if it looks like a recipe, code block, or random text.")
    parsing_error: Optional[str] = None

# -----------------------------------------------------------------------------
# Advanced JD Contract
# -----------------------------------------------------------------------------

class JobMetadata(BaseModel):
    title: Optional[str] = None
    location: Optional[str] = None
    work_mode: Literal["remote", "onsite", "hybrid", "unknown"] = "unknown"
    clearance: Optional[str] = "None"

class GatingRules(BaseModel):
    visa_sponsorship: Optional[bool] = Field(None, description="Does the job offer visa sponsorship? True/False.")
    education_min: Optional[str] = Field(None, description="Minimum education degree required (e.g. 'Bachelors', 'Masters', 'PhD'). Extract explicitly.")
    education_strict: bool = Field(False, description="If True, reject candidates who do not meet the minimum education requirement.")
    security_clearance: Optional[str] = Field(None, description="Security clearance required (e.g. 'Top Secret', 'Confidential').")
    location_strict: bool = Field(False, description="If True, reject candidates who are not in the required location (unless open to relocation).")

class Requirement(BaseModel):
    req_id: str = Field(..., description="Unique ID for this requirement (e.g. 'req_1').")
    skill_id: Optional[str] = Field(None, description="Name of the hard technical skill (e.g. 'Python', 'React').")
    priority: Literal["must_have", "nice_to_have"] = Field(..., description="Classification: 'must_have' is default. 'nice_to_have' if text says 'preferred', 'plus', 'bonus', 'desired'.")
    level: Literal["junior", "mid", "senior"] = Field("mid", description="Required expertise level for this skill.")
    is_hard_filter: bool = Field(True, description="If True (and priority is must_have), missing this skill is dealbreaker.")
    min_years: int = Field(0, description="Minimum years of experience required.")
    context: Optional[str] = None
    logic: Optional[Literal["one_of"]] = None
    options: List[str] = [] # For clusters

class Competency(BaseModel):
    name: str = Field(..., description="Name of the competency (e.g. 'Problem Solving')")
    description: Optional[str] = None
    priority: Literal["must_have", "nice_to_have"] = "must_have"

class SenioritySignals(BaseModel):
    target_level: Optional[str] = None # e.g. "senior", "staff"
    keywords_found: List[str] = []

class JobDescription(BaseModel):
    """
    Structured representation of a Job following the Advanced Contract.
    """
    id: str
    job_metadata: JobMetadata
    gating_rules: GatingRules
    requirements: List[Requirement]
    competencies: List[Competency] = []
    seniority_signals: SenioritySignals
    
    # Validation Flags
    is_valid: bool = Field(..., description="Is this a valid Job Description? Set False if it looks like a recipe, resume, or random text.")
    parsing_error: Optional[str] = None
