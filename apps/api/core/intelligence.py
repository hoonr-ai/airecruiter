from pydantic import BaseModel, Field
from typing import List, Literal, Optional

# ------------------------------------------------------------------
# Narrative Intelligence Schemas (The "Tribunal" Interface)
# ------------------------------------------------------------------

class CareerTrajectory(BaseModel):
    direction: Literal["accelerating", "stable", "stagnating", "erratic", "pivoting"]
    reasoning: str = Field(..., description="1-sentence explanation of the trajectory.")

class RiskSignal(BaseModel):
    type: Literal["short_tenure", "gap_unexplained", "downgrade_in_role", "tech_stack_stale", "location_mismatch", "missing_requirements"]
    severity: Literal["low", "medium", "high", "critical"]
    evidence_snippet: str

class StrengthSignal(BaseModel):
    type: Literal["rapid_promotion", "elite_education", "modern_stack_mastery", "leadership_growth"]
    evidence_snippet: str

class TribunalVerdict(BaseModel):
    # The output of the "Skeptic vs Advocate" debate
    skeptic_summary: str
    advocate_summary: str
    consensus_flags: List[RiskSignal]
    consensus_strengths: List[StrengthSignal]
    trajectory_analysis: CareerTrajectory
    
    # We do NOT return a score. We return a "Tag".
    narrative_tag: Literal["top_tier_potential", "solid_performer", "high_risk", "mismatch", "analysis_failed"]
