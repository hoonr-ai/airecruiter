from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any
import asyncio
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from services.ai_service import ai_service
from models import (
    JobDescription, MatchResult, ParsedJobRequest, ParsedJobResponse,
    ChatRequest, ChatResponse, CandidateSearchRequest, CandidateMessageRequest, JobFetchRequest,
    CandidateAnalysisRequest, CandidateAnalysisResponse
)
from matcher import mock_match_candidates
from services.extractor import llm_extractor
from services.jobdiva import jobdiva_service
from services.unipile import unipile_service
from services.chat_service import chat_service
from routers import engagement

app = FastAPI(title="Hoonr.ai API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allow all origins for local dev
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/jobs/parse", response_model=ParsedJobResponse)
async def parse_job_description(request: ParsedJobRequest):
    """
    Parses raw text JD into structured format (skills, location, etc).
    """
    try:
        data = await llm_extractor.extract_from_jd(request.text)
        return ParsedJobResponse(
            title=data.title,
            summary=data.summary,
            hard_skills=data.hard_skills,
            soft_skills=data.soft_skills,
            experience_level=data.experience_level,
            location_type=data.location_type
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/candidates/search")
async def search_jobdiva_candidates(request: CandidateSearchRequest):
    """
    Searches JobDiva, Vetted Database, AND LinkedIn (via Unipile) for candidates.
    """
    # Determine effective location based on location_type
    effective_location = None if request.location_type.lower() == "remote" else request.location
    
    print(f"🔥 DEBUG: SEARCH REQUEST: {request.model_dump_json()}")
    print(f"🔥 DEBUG: SEARCH: location_type={request.location_type}, effective_location={effective_location}, sources={request.sources}, open_to_work={request.open_to_work}")
    
    # 1. Define Helper Wrapper
    async def safe_search(coro, name):
        try:
            print(f"🔍 Starting {name} search...")
            result = await asyncio.wait_for(coro, timeout=30.0)
            print(f"✅ {name} returned {len(result)} results")
            return result
        except asyncio.TimeoutError:
            print(f"⚠️ {name} Search Timed Out (>30s). Skipping.")
            return []
        except Exception as e:
            print(f"❌ {name} Search Failed: {e}")
            import traceback
            traceback.print_exc()
            return []

    # 2. Prepare Tasks
    tasks = []
    
    # Task A: JobDiva
    if "JobDiva" in request.sources:
        tasks.append(safe_search(
            jobdiva_service.search_candidates(request.skills, effective_location, request.page, request.limit), 
            "JobDiva"
        ))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # Task B: Vetted DB
    if "VettedDB" in request.sources:
        skill_names = []
        for s in request.skills:
             if isinstance(s, dict): skill_names.append(s.get("name"))
             elif hasattr(s, "name"): skill_names.append(s.name)
             else: skill_names.append(str(s))
        
        from services.vetted import vetted_service
        tasks.append(safe_search(
            vetted_service.search_candidates(skill_names, effective_location, request.page, request.limit),
            "VettedDB"
        ))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # Task C: LinkedIn (Unipile)
    if "LinkedIn" in request.sources:
        tasks.append(safe_search(
            unipile_service.search_candidates(
                request.skills, 
                effective_location, 
                request.open_to_work, 
                25 if request.limit > 25 else request.limit # Cap at 25 as requested
            ),
            "LinkedIn"
        ))
    else:
        tasks.append(asyncio.sleep(0, result=[]))

    # 3. Execute in Parallel
    results = await asyncio.gather(*tasks)
    
    jd_results = results[0] if isinstance(results[0], list) else []
    vet_results = results[1] if isinstance(results[1], list) else []
    li_results = results[2] if isinstance(results[2], list) else []
    
    print(f"✅ SEARCH COMPLETE: JobDiva={len(jd_results)}, Vetted={len(vet_results)}, LinkedIn={len(li_results)}")
    
    # 4. Combine
    if request.page == 1:
        # Prioritize Vetted, then LinkedIn, then JobDiva? Or Mix?
        # User implies LinkedIn is "extra".
        combined = vet_results + li_results + jd_results
    else:
        combined = jd_results # Pagination logic weak, assume others fit in page 1
        
    return combined

@app.post("/candidates/message")
async def message_candidate(request: CandidateMessageRequest):
    """
    Sends a message to a candidate via the specified source provider.
    Currently supports: LinkedIn (via Unipile).
    """
    if request.source == "LinkedIn":
        success = await unipile_service.send_message(request.candidate_provider_id, request.message)
        if success:
            return {"status": "success", "detail": "Message queued/sent via LinkedIn"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send LinkedIn message")
            
    elif request.source in ["JobDiva", "VettedDB", "Email"]:
        # Mock Email Send (Log it)
        print(f"📧 EMAIL OUTREACH: Sending email to candidate {request.candidate_provider_id}")
        print(f"📧 Subject: (Auto-generated)")
        print(f"📧 Body: {request.message}")
        return {"status": "success", "detail": f"Email simulation successful for {request.source}"}
        
    else:
        raise HTTPException(status_code=400, detail=f"Messaging not supported for source: {request.source}")

@app.post("/candidates/analyze", response_model=CandidateAnalysisResponse)
async def analyze_candidates(request: CandidateAnalysisRequest):
    """
    Batch analyzes candidates against JD using AI.
    """
    candidates_to_process = []
    
    # We need to ensure we have resume text for analysis.
    # If the client sent it, great. If not, we fetch it given the ID.
    for c in request.candidates: 
        c_text = c.get("resume_text")
        if not c_text:
             # Try fetch if missing
             try:
                # Determine Source to Route Correctly
                source = c.get("source", "JobDiva")
                if source == "VettedDB":
                    from services.vetted import vetted_service
                    c_text = await vetted_service.get_candidate_resume(c.get("id"))
                else:
                    # Default to JobDiva
                    c_text = await jobdiva_service.get_candidate_resume(c.get("id"))
                
                c["resume_text"] = c_text
             except Exception as e:
                # Log but continue, AI will just have less context
                print(f"Error fetching resume for {c.get('id')}: {e}")
                pass
        candidates_to_process.append(c)

    results = await ai_service.analyze_candidates_batch(
        candidates_to_process, 
        request.job_description,
        structured_jd=request.structured_jd
    )
    return {"results": results, "name": "", "email": "", "skills": [], "experience_years": 0} # Dummy fields to satisfy model if strict

@app.post("/jobs/fetch")
async def fetch_job_from_jobdiva(request: JobFetchRequest):
    """
    Fetches Full Job Details from JobDiva by ID.
    """
    job = await jobdiva_service.get_job_by_id(request.job_id)
    if not job:
         raise HTTPException(status_code=404, detail="Job not found in JobDiva")
    return job

@app.get("/candidates/{candidate_id}/resume")
async def get_candidate_resume(candidate_id: str):
    """
    Fetches the resume text for a candidate.
    Waterfall: LinkedIn (via Unipile + AI), JobDiva, then Vetted API.
    """
    # 1. LinkedIn (Unipile)
    if candidate_id.startswith("unipile_"):
        real_id = candidate_id.replace("unipile_", "")
        print(f"🔍 Fetching LinkedIn Profile for {real_id}...")
        profile = await unipile_service.get_candidate_profile(real_id)
        
        if profile:
            print(f"✅ Profile found. Generating Resume with AI...")
            resume_text = await ai_service.generate_resume_from_profile(profile)
            return {"resume_text": resume_text}
        else:
            raise HTTPException(status_code=404, detail="LinkedIn Profile not found or accessible")

    try:
        resume_text = await jobdiva_service.get_candidate_resume(candidate_id)
        
        # Check if JobDiva returned error string (it doesn't raise Exception)
        if not resume_text or "Resume content unavailable" in resume_text:
             raise Exception("JobDiva Resume Not Found")
             
        return {"resume_text": resume_text}
    except Exception:
        # If JobDiva fails (404), try Vetted DB
        try:
            from services.vetted import vetted_service
            resume_text = await vetted_service.get_candidate_resume(candidate_id)
            if resume_text:
                return {"resume_text": resume_text}
            raise HTTPException(status_code=404, detail="Resume not found in any source")
        except Exception:
            raise HTTPException(status_code=404, detail="Resume not found")

@app.post("/chat", response_model=ChatResponse)
async def chat_with_aria(request: ChatRequest):
    response = await chat_service.get_response(request.message, request.history)
    return {"response": response}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
