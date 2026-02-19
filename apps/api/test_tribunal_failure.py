import asyncio
import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

from core.models import CandidateProfile, JobDescription, Requirement, JobMetadata, GatingRules, CandidateMetadata, ComputedCandidateStats, SenioritySignals
from core.engine import calculate_match
from services.ai_service import ai_service

async def test_failure():
    print("--- Starting Hard Failure Test ---")
    
    # 1. Create JD with Hard Constraint
    jd = JobDescription(
        id="jd_123",
        job_metadata=JobMetadata(title="Java Developer", location="NY", work_mode="onsite"),
        requirements=[
            Requirement(req_id="r1", skill_id="Java", priority="must_have", is_hard_filter=True),
            Requirement(req_id="r2", skill_id="Python", priority="nice_to_have", is_hard_filter=False)
        ],
        gating_rules=GatingRules(),
        seniority_signals=SenioritySignals(),
        competencies=[],
        is_valid=True
    )
    
    # 2. Create Candidate (Missing Java)
    # Only has Python
    cand = CandidateProfile(
        id="c_fail",
        candidate_metadata=CandidateMetadata(name="Fail Candidate", location="NY"),
        computed_stats=ComputedCandidateStats(),
        skills=[], # Will add Python via manual construction if needed, or just rely on extraction/graph
        skill_profile=[],
        timeline=[],
        is_valid=True
    )
    from core.models import CandidateSkill, ComputedStats
    cand.skills = [
        CandidateSkill(
            skill_id="Python", 
            computed_stats=ComputedStats(
                months_experience=24,
                recency_decay=1.0,
                evidence_confidence=0.9
            )
        )
    ]
    
    # 3. Run Matching directly first
    try:
        print("\n--- Running calculate_match (Direct) ---")
        result = await calculate_match(cand, jd)
        print(f"Direct Score: {result.score}")
        print(f"Direct Verdict: {result.tribunal_verdict.narrative_tag if result.tribunal_verdict else 'None'}")
        print(f"Critical Failures Logic Triggered: {result.score <= 40}")
    except Exception as e:
        print(f"❌ Direct Match Failed: {e}")
        import traceback
        traceback.print_exc()

    # 4. Run via AI Service (Batch) to check serialization/filtering
    print("\n--- Running AI Service Batch ---")
    
    # Mock candidate dict
    c_dict = {
        "id": "c_fail",
        "firstName": "Fail",
        "lastName": "User", 
        "resume_text": "I know Python.",
        "city": "NY",
        "state": "NY"
    }
    
    # We must patch _extract_candidate and _extract_jd to return our mocks to avoid LLM calls
    # Or safely use the 'structured_jd' and allow LLM to parse simple candidate?
    # To be fast, we'll monkeypatch extractors.
    
    original_extract_cand = ai_service._extract_candidate
    ai_service._extract_candidate = lambda a,b: asyncio.Future()
    # ai_service._extract_candidate = mock_extract_cand # Removed incorrect line
    
    # Actually just mocking the method result
    async def mock_extract(text, cid):
        return cand
        
    ai_service._extract_candidate = mock_extract
    
    # Run
    # Provide structured JD to skip JD extraction
    structured_jd = {
        "title": "Java Dev",
        "hard_skills": [{"name": "Java", "priority": "Must Have"}, {"name": "Python", "priority": "Flexible"}],
        "location": "NY"
    }
    
    results = await ai_service.analyze_candidates_batch([c_dict], "", structured_jd=structured_jd)
    
    print(f"\nBatch Results Count: {len(results)}")
    if results:
        r = results[0]
        print(f"Result Status: {r.get('tribunal_status')}")
        print(f"Result Score: {r.get('score')}")
        print(f"Result ID: {r.get('candidate_id')}")
    else:
        print("❌ result list is empty!")

if __name__ == "__main__":
    asyncio.run(test_failure())
