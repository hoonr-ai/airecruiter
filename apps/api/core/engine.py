from typing import List, Dict, Any, Literal, Optional
from pydantic import BaseModel, Field
from core.graph import ontology
from core.models import CandidateProfile, JobDescription, CandidateSkill, ComputedStats
from core.intelligence import TribunalVerdict
from services.tribunal import TribunalService
from services.location import LocationService
from core.utils import normalize_skill


# Global Service Instance
tribunal_service = TribunalService()
location_service = LocationService()


class AnalysisSection(BaseModel):
    title: str # "Education", "Experience"
    status: Literal["met", "not_met", "review_needed", "exceeds", "missing", "partial"]
    summary: str # "BS in CS matches requirement"
    details: List[str] = [] # ["Found Bachelor's degree...", "Major: Computer Science"]

class TraceItem(BaseModel):
    skill_slug: str
    status: Literal["matched", "missing", "partial"]
    score: float
    seniority_level: Optional[str] = "unknown" # junior, mid, senior
    sources: List[str] = [] # "experience", "skills_section"
    priority: Literal["required", "preferred"] = "required"

class MatchResult(BaseModel):
    score: float = Field(..., ge=0, le=100)
    analysis: List[AnalysisSection]
    technical_trace: List[TraceItem] # Renamed from detailed_trace
    detailed_trace: List[TraceItem] = [] # Backward compatibility
    tribunal_verdict: Optional[TribunalVerdict] = None 
    seniority_level: str = "unknown"
    sources: List[str] = []

async def calculate_match(candidate: CandidateProfile, jd: JobDescription) -> MatchResult:
    """
    Deterministic scoring engine.
    Now Async to support LLM Tribunal calls.
    
    Algorithm:
    1. Base Score = 0
    2. Required Skills: Worth 70% of total score.
    3. Preferred Skills: Worth 30% of total score.
    4. Exact string matching on normalized IDs.
    """
    
    # ---------------------------------------------------------
    # Helper: Combined Matching Logic (Graph + Fuzzy)
    # ---------------------------------------------------------
    
    # 1. Pre-process Candidate Skills with Alias Resolution
    # Map canonical_slug -> CandidateSkill object
    # If user has "ReactJS" (alias) and "React" (canonical), we merge or keep one.
    candidate_skills_map: Dict[str, CandidateSkill] = {}
    
    for s in candidate.skills:
        canonical = ontology.resolve_alias(s.skill_id)
        candidate_skills_map[canonical] = s

    # 2. Merge in skills from the "Skill Profile" (Header Section)
    # The LLM now prioritizes this list for explicit skills sections.
    if candidate.skill_profile:
        from core.models import ComputedStats, EvidenceSource
        for sp in candidate.skill_profile:
            canonical = ontology.resolve_alias(sp.skill_slug)
            
            # If not present, create a synthetic CandidateSkill entry so it counts for scoring
            if canonical not in candidate_skills_map:
                # Map for EvidenceSource Strict strictness
                level_map = {
                    "senior": "expert", "principal": "expert", "staff": "expert", "expert": "expert",
                    "mid": "practitioner", "intermediate": "practitioner", "practitioner": "practitioner",
                    "junior": "learner", "entry": "learner", "learner": "learner"
                }
                c_level = (sp.competency_level or "unknown").lower()
                valid_level = level_map.get(c_level, "unknown")
                
                synth_skill = CandidateSkill(
                    skill_id=canonical,
                    computed_stats=ComputedStats(
                        months_experience=sp.total_months,
                        recency_decay=1.0, # Assumed active if listed in core skills
                        evidence_confidence=1.0 
                    ),
                    evidence_sources=[
                        EvidenceSource(
                            section="summary" if "summary" in sp.sources else "experience", # fallback
                            verbatim=f"Listed in Skills Section: {sp.skill_slug}",
                            context_level=valid_level
                        )
                    ]
                )
                candidate_skills_map[canonical] = synth_skill
    
    # 3. Harvest skills from Timeline (Experience Section)
    if candidate.timeline:
        from core.models import ComputedStats, EvidenceSource
        for job in candidate.timeline:
            if not job.extracted_skills: continue
            
            for t_skill in job.extracted_skills:
                canonical = ontology.resolve_alias(t_skill.skill_id)
                
                # Create synthetic skill if missing
                if canonical not in candidate_skills_map:
                    # Infer level? Hard from timeline without text analysis, assume practitioner
                    # Calculate duration from job dates?
                    # For now, placeholder stats. Real system would propagate job duration.
                    synth_skill = CandidateSkill(
                        skill_id=canonical,
                        computed_stats=ComputedStats(
                            months_experience=12, # Placeholder, should calc from dates
                            recency_decay=1.0, 
                            evidence_confidence=0.9
                        ),
                        evidence_sources=[
                            EvidenceSource(
                                section="experience",
                                verbatim=t_skill.context or f"Used at {job.company}",
                                context_level="practitioner"
                            )
                        ]
                    )
                    candidate_skills_map[canonical] = synth_skill
    
    # DEBUG: Print Candidate Skills
    print(f"🧐 MATCH ENGINE: Candidate Skills keys: {list(candidate_skills_map.keys())}")

    # Advanced JD Schema Support
    # Filter requirements by priority
    req_ids = []
    pref_ids = []
    
    for req in jd.requirements:
        if not req.skill_id: continue # Skip if no skill_id yet (maybe purely text req)
        
        # Resolve Request Alias too
        norm_id = ontology.resolve_alias(req.skill_id)
        
        if req.priority == "must_have":
            req_ids.append(norm_id)
        else:
            pref_ids.append(norm_id)
            
    print(f"🧐 MATCH ENGINE: Requirements: {req_ids} + {pref_ids}")
    
    # Trackers
    matched_req = []
    missing_req = []
    matched_pref = []
    
    # Scoring Config (Updated)
    # We moved weights to Composite area, but `get_best_match_score` logic needs to stay
    
    # ---------------------------------------------------------
    # Helper: Combined Matching Logic (Graph + Fuzzy)
    # ---------------------------------------------------------
    
    def get_best_match_score(req_slug: str, cand_skills_map: Dict[str, Any]) -> float:
        """
        Returns a score 0.0 - 1.0 based on Graph + Fuzzy Logic.
        """
        # 1. Exact Match (Fastest)
        if req_slug in cand_skills_map:
            return 1.0
            
        cand_slugs = set(cand_skills_map.keys())
        
        # 2. Graph Lookup (Semantic: Parent/Child/Alternative)
        # Returns 1.0 for Implied, 0.5 for Alternative
        graph_score = ontology.get_related_scores(req_slug, cand_slugs)
        if graph_score >= 1.0:
            return graph_score # Perfect semantic match (e.g. React implies JS)
            
        # 2.5 Handle Underscore Mismatch (e.g. "data_analysis" vs "dataanalysis")
        req_slug_joined = req_slug.replace('_', '')
        if req_slug_joined in cand_slugs:
            return 1.0
            
        # 3. Fuzzy String Match (Fallback for "Java" vs "Java Programming")
        req_tokens = set(req_slug.split('_'))
        for cand_slug in cand_slugs:
            # Substring check
            if cand_slug in req_slug or req_slug in cand_slug:
                return max(graph_score, 1.0) # Text match overrides alternative score
            
            # Token overlap
            cand_tokens = set(cand_slug.split('_'))
            common = req_tokens.intersection(cand_tokens)
            for w in common:
                if len(w) > 3: 
                     return max(graph_score, 0.9) # High confidence text match
                     
        return graph_score

    # Helpers for Trace Enrichment
    # Index skill profile for fast lookup of metadata (seniority, etc.)
    skill_profile_map = {s.skill_slug: s for s in candidate.skill_profile} if candidate.skill_profile else {}

    def create_trace_item(skill_slug: str, score: float, priority: Literal["required", "preferred"]) -> TraceItem:
        status = "missing"
        if score >= 1.0: status = "matched"
        elif score > 0: status = "partial"
        
        # Look up metadata
        level = "unknown"
        sources = []
        
        # Try to find in skills profile (best source)
        if skill_slug in skill_profile_map:
            sp = skill_profile_map[skill_slug]
            level = sp.competency_level or "unknown"
            sources = sp.sources
            
            # Auto-infer level if missing but YOE is present
            if level == "unknown" and sp.total_months > 0:
                if sp.total_months >= 60: level = "senior"
                elif sp.total_months >= 24: level = "mid"
                else: level = "junior"
                
        # If not in profile, check global map (populated from Timeline/Lists)
        elif skill_slug in candidate_skills_map:
             sk = candidate_skills_map[skill_slug]
             
             # Extract sources from EvidenceSource objects
             if sk.evidence_sources:
                 sources = [es.section for es in sk.evidence_sources]
                 # Infer level from context
                 levels = [es.context_level for es in sk.evidence_sources if es.context_level != "unknown"]
                 if levels:
                     level = levels[0] # Take first valid level
                 else:
                     level = "listed"
             else:
                 level = "listed" 
        
        return TraceItem(
            skill_slug=skill_slug,
            status=status,
            score=score,
            seniority_level=level,
            sources=list(set(sources)), # Dedup
            priority=priority
        )

    # ---------------------------------------------------------
    # Analysis Logic
    # ---------------------------------------------------------
    def analyze_education(cand: CandidateProfile, job: JobDescription) -> AnalysisSection:
        req = job.gating_rules.education_min
        details = []
        
        # 1. Harvest Candidate Degrees
        detected_degrees = []
        for edu in cand.education:
            d_str = f"{edu.degree or 'Degree'} in {edu.field or 'Unknown'}"
            detected_degrees.append(d_str)
            
        details.append(f"Detected: {', '.join(detected_degrees) if detected_degrees else 'None'}")
        
        if not req:
            return AnalysisSection(
                title="Education",
                status="met",
                summary="No specific education requirements.",
                details=details
            )
            
        # 2. Check Requirement (Simple Logic)
        normalized_req = req.lower()
        met = False
        
        # Keyword matching for simple gating
        keywords = {
            "bachelor": ["bachelor", "bs", "b.s.", "ba", "b.a."],
            "master": ["master", "ms", "m.s.", "ma", "m.a.", "mba"],
            "phd": ["phd", "doctorate"]
        }
        
        target_keywords = []
        for level, keys in keywords.items():
            if level in normalized_req:
                target_keywords = keys
                break
        
        if not target_keywords:
            target_keywords = [normalized_req] # Fallback to raw string
            
        for edu in cand.education:
            deg = (edu.degree or "").lower()
            if any(k in deg for k in target_keywords):
                met = True
                break
                
        if met:
            return AnalysisSection(
                title="Education",
                status="met",
                summary=f"Matches requirement: {req}",
                details=details
            )
        else:
             return AnalysisSection(
                title="Education",
                status="review_needed", # We don't hard reject, just flag
                summary=f"Requirement: {req}",
                details=details + ["⚠️ No exact degree match found automatically."]
            )

    def analyze_experience(cand: CandidateProfile, job: JobDescription) -> AnalysisSection:
        # Currently just summarizing stats
        total_yoe = cand.computed_stats.total_yoe
        
        details = [
            f"Total Experience: {total_yoe} years",
            f"Average Tenure: {cand.computed_stats.avg_tenure_months:.1f} months"
        ]
        
        # Check Seniority Signals from JD
        target_level = job.seniority_signals.target_level
        
        status = "met"
        summary = "Experience level appears aligned."
        
        if target_level == "senior" and total_yoe < 4:
            status = "not_met"
            summary = "Senior role typically requires 4+ years."
        elif target_level == "staff" and total_yoe < 8:
            status = "review_needed"
            summary = "Staff role typically requires 8+ years."
            
        return AnalysisSection(
            title="Experience",
            status=status,
            summary=summary,
            details=details
        )

    def analyze_competencies(cand: CandidateProfile, job: JobDescription) -> AnalysisSection:
        required_competencies = job.competencies
        if not required_competencies:
            return AnalysisSection(
                title="Competencies",
                status="met",
                summary="No specific soft skills or competencies listed.",
                details=[]
            )

        # Build a set of candidate competencies (lowercase for matching)
        # 1. Explicitly extracted competencies
        cand_competencies = set(c.lower() for c in cand.competencies)
        
        # 2. Check skill profile for things that look like soft skills (heuristic)
        # Often "Communication", "Teamwork" might land in skills if not filtered
        if cand.skill_profile:
            for sp in cand.skill_profile:
                cand_competencies.add(sp.skill_slug.replace("_", " ").lower())

        found = []
        missing = []
        
        for comp in required_competencies:
            c_name = comp.name.lower()
            # Simple substring/keyword match
            # "Problem Solving" matches "Strong problem solving skills"
            match = False
            
            # Check against set
            if any(c_name in cc or cc in c_name for cc in cand_competencies):
                match = True
            
            if match:
                found.append(f"✅ {comp.name}")
            else:
                missing.append(f"❌ {comp.name}")

        status = "met"
        summary = f"Found {len(found)} out of {len(required_competencies)} key competencies."
        
        if len(missing) > 0:
            status = "review_needed"
            # If many missing, maybe 'not_met'? 
            # Soft skills are fuzzy, so 'review_needed' is safer than 'not_met'
        
        return AnalysisSection(
            title="Competencies",
            status=status,
            summary=summary,
            details=found + missing
        )

    # ---------------------------------------------------------
    # 3. Composite Scoring Implementation
    # ---------------------------------------------------------
    
    # Track critical failures (Hard Filters)
    critical_failures = []

    # Track scores for each component
    score_education = 0.0
    score_experience = 0.0
    score_competencies = 0.0
    score_required = 0.0
    score_preferred = 0.0
    
    # Constants
    WEIGHT_EDU = 10.0
    WEIGHT_EXP = 10.0
    WEIGHT_COMP = 10.0
    WEIGHT_REQ = 50.0
    WEIGHT_PREF = 20.0
    
    # A. Education Score (10)
    edu_analysis = analyze_education(candidate, jd)
    
    # STRICT CHECK: Education
    if jd.gating_rules.education_strict and edu_analysis.status != "met" and edu_analysis.status != "exceeds":
        # Downgrade "review_needed" to "not_met" if strict
        edu_analysis.status = "not_met"
        edu_analysis.summary += " (Strict Filter Failed)"
        critical_failures.append("Education Requirement")

    if edu_analysis.status == "met" or edu_analysis.status == "exceeds":
        score_education = WEIGHT_EDU
    elif edu_analysis.status == "review_needed":
        score_education = WEIGHT_EDU * 0.5
    else:
        score_education = 0.0
        
    # B. Experience Score (10)
    exp_analysis = analyze_experience(candidate, jd)
    if exp_analysis.status == "met" or exp_analysis.status == "exceeds":
        score_experience = WEIGHT_EXP
    elif exp_analysis.status == "review_needed":
        score_experience = WEIGHT_EXP * 0.5
    else:
        score_education = 0.0

    # C. Competencies Score (10)
    comp_analysis = analyze_competencies(candidate, jd)
    # Count verified vs total
    # The summary string is "Found X out of Y..." so we can parse or re-calculate
    # Let's re-calculate ratio for scoring precision
    if jd.competencies:
        # Simple ratio
        found_count = len([d for d in comp_analysis.details if "✅" in d])
        ratio = found_count / len(jd.competencies)
        score_competencies = WEIGHT_COMP * ratio
    else:
        score_competencies = WEIGHT_COMP # Full points if no soft skills required
        
    # Helper: Seniority Match Multiplier
    def get_seniority_multiplier(req_seniority: Optional[str], cand_seniority: Optional[str]) -> float:
        if not req_seniority: return 1.0 
        
        req_norm = req_seniority.lower()
        cand_norm = (cand_seniority or "unknown").lower()
        
        # Hierarchy values
        levels = {"junior": 1, "mid": 2, "senior": 3, "staff": 4, "principal": 5}
        
        r_val = levels.get(req_norm, 1)
        
        c_val = 1 
        if cand_norm in levels:
            c_val = levels[cand_norm]
        elif cand_norm in ["detected", "extracted", "unknown"]:
            c_val = 1 
        
        if c_val >= r_val:
            return 1.0 
        else:
            diff = r_val - c_val
            if diff == 1: return 0.8
            if diff >= 2: return 0.5
            return 0.5
    
    # 4. Requirements & Preferred IDs
    req_objs = [r for r in jd.requirements if r.priority == "must_have" and r.skill_id]
    pref_ids = [r.skill_id for r in jd.requirements if r.priority == "nice_to_have" and r.skill_id]
    
    # D. Required Skills (50)
    req_trace_List = []
    
    if req_objs:
        weight_per_req = WEIGHT_REQ / len(req_objs)
        for req in req_objs:
            rid = ontology.resolve_alias(req.skill_id)
            
            # DEBUG: Log skill matching attempt
            print(f"🔍 TRIBUNAL: Checking skill '{req.skill_id}' (resolved to '{rid}') against candidate skills: {list(candidate_skills_map.keys())[:5]}...")
            
            score = get_best_match_score(rid, candidate_skills_map)
            
            print(f"🔍 TRIBUNAL: Skill '{req.skill_id}' -> '{rid}' scored {score}")
            
            item = create_trace_item(rid, score, "required")
            req_trace_List.append(item)
            
            if score > 0:
                matched_req.append(rid)
                
                # USE PER-SKILL LEVEL from JD Requirement
                req_level = req.level or jd.seniority_signals.target_level
                seniority_mult = get_seniority_multiplier(req_level, item.seniority_level)
                
                final_skill_score = score * seniority_mult
                score_required += (weight_per_req * final_skill_score)
            else:
                missing_req.append(rid)
                # STRICT CHECK: Hard Filter
                if req.is_hard_filter:
                     print(f"❌ TRIBUNAL: HARD FILTER FAILED for '{req.skill_id}' (resolved: '{rid}')")
                     critical_failures.append(f"Missing Hard Skill: {req.skill_id}")

    else:
        score_required = WEIGHT_REQ

    # E. Preferred Skills (20)
    pref_trace_List = []
    
    if pref_ids:
        weight_per_pref = WEIGHT_PREF / len(pref_ids)
        for pid in pref_ids:
            score = get_best_match_score(pid, candidate_skills_map)
            item = create_trace_item(pid, score, "preferred")
            pref_trace_List.append(item)
            
            if score > 0:
                matched_pref.append(pid)
                seniority_mult = get_seniority_multiplier(jd.seniority_signals.target_level, item.seniority_level)
                final_skill_score = score * seniority_mult
                score_preferred += (weight_per_pref * final_skill_score)
            else:
                missing_req.append(pid) 
    else:
        if req_ids: 
             score_preferred = WEIGHT_PREF
        else:
             score_preferred = WEIGHT_PREF

    # Total
    total_score = round(score_education + score_experience + score_competencies + score_required + score_preferred, 2)
    
    # CRITICAL PENALTY
    if critical_failures:
        print(f"🛑 Critical Failures Detected: {critical_failures}")
        # Cap score at 40 if critical failures exist
        total_score = min(total_score, 40.0)
    
    total_score = min(100.0, total_score)
    
    analysis_results = [x for x in [edu_analysis, exp_analysis, comp_analysis] if x is not None]

    # ---------------------------------------------------------
    # 5. Location Analysis (Async)
    # ---------------------------------------------------------
    # Only if Job Location is relevant
    cand_loc = candidate.candidate_metadata.location
    job_loc = jd.job_metadata.location
    mode = jd.job_metadata.work_mode
    
    if mode != "remote":
        loc_verdict = await location_service.check_proximity(cand_loc, job_loc, mode)
        
        loc_status = "met"
        if not loc_verdict.is_within_range:
            loc_status = "not_met" if mode == "onsite" else "review_needed"
            
        analysis_results.append(AnalysisSection(
            title="Location",
            status=loc_status,
            summary=f"Distance: {loc_verdict.distance_estimate}",
            details=[
                f"Candidate: {cand_loc or 'Unknown'}",
                f"Job: {job_loc or 'Unknown'} ({mode.title()})",
                f"Analysis: {loc_verdict.reason}"
            ]
        ))
    else:
         analysis_results.append(AnalysisSection(
            title="Location",
            status="met",
            summary="Remote Role",
            details=["Job is Remote. Check time zones if necessary."]
        ))

    # NEW: Narrative Intelligence (Tribunal)
    # Run Tribunal if score is decent OR if we have critical failures (to explain why)
    tribunal_verdict = None
    
    # If Critical Failures exist, we can generate a specific "Rejection" verdict instantly without costly LLM
    should_run_tribunal = total_score >= 40.0 

    if should_run_tribunal and not critical_failures:
        try:
            tribunal_verdict = await tribunal_service.evaluate_narrative(
                resume_text="", 
                candidate=candidate,
                jd=jd
            )
                
        except Exception as e:
            print(f"⚠️ Tribunal Service skipped: {e}")
            
    # Fallback: If critical failures (regardless of score), create a "Rejection" verdict so UI shows the modal.
    if not tribunal_verdict and (critical_failures or total_score < 40):
         from core.intelligence import RiskSignal, CareerTrajectory, StrengthSignal
         
         reason = f"Candidate failed {len(critical_failures)} critical requirements: {', '.join(critical_failures)}" if critical_failures else "Match Score below analysis threshold."
         
         tribunal_verdict = TribunalVerdict(
             skeptic_summary=f"Tribunal Analysis Skipped: {reason}",
             advocate_summary="Analysis was halted due to hard filter violations. No advocacy case could be constructed.",
             trajectory_analysis=CareerTrajectory(direction="stagnating", reasoning="Profile rejected by pre-screen filters."),
             consensus_strengths=[],
             consensus_flags=[
                 RiskSignal(type="missing_requirements", severity="critical", evidence_snippet=cf) 
                 for cf in critical_failures
             ],
             narrative_tag="analysis_failed"
         )

    # Return Flat Result
    # Combine traces
    full_trace = req_trace_List + pref_trace_List
    all_analysis = analysis_results

    return MatchResult(
        score=total_score,
        analysis=all_analysis,
        technical_trace=full_trace,
        detailed_trace=full_trace, # Backward compatibility
        tribunal_verdict=tribunal_verdict,
        seniority_level=candidate.skill_profile[0].competency_level if candidate.skill_profile else "unknown",
        sources=[]
    )
