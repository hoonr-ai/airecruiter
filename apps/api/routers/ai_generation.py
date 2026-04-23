import os
import time
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import hashlib
from dataclasses import asdict
from datetime import datetime

from services.jobdiva import jobdiva_service
from services.job_skills_extractor import JobSkillsExtractor, ExtractedSkill
from services.job_skills_db import JobSkillsDB
from services.job_rubric_db import JobRubricDB
from services.screening_question_generator import generate_screening_questions
from openai import AsyncOpenAI
from core import (
    OPENAI_API_KEY,
    JOBDIVA_AI_JD_UDF_ID, JOBDIVA_JOB_NOTES_UDF_ID,
    OPENAI_MODEL
)

router = APIRouter()

# Initialize OpenAI client
client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


class JobDescriptionRequest(BaseModel):
    jobTitle: str = ""
    jobNotes: str = ""
    workAuthorization: str = ""
    jobDescription: str = ""

class JobDivaSyncRequest(BaseModel):
    jobId: str
    aiDescription: str
    jobNotes: str = ""
    workAuthorization: str = ""
    recruiterEmail: str = ""

@router.post("/sync-jobdiva")
async def sync_to_jobdiva(req: JobDivaSyncRequest):
    """
    Push AI JD (UDF #230) and Job Notes (UDF #231) to JobDiva,
    and persist both locally in PostgreSQL (monitored_jobs table).
    """
    udf_ai_jd  = JOBDIVA_AI_JD_UDF_ID
    udf_notes  = JOBDIVA_JOB_NOTES_UDF_ID

    def truncate(s: str) -> str:
        return s[:3900] if s else ""

    fields = [
        {"userfieldId": udf_ai_jd, "value": truncate(req.aiDescription)},
        {"userfieldId": udf_notes, "value": truncate(req.jobNotes)},
    ]

    jobdiva_ok = await jobdiva_service.update_job_user_fields(req.jobId, fields)
    local_ok   = jobdiva_service.monitor_job_locally(req.jobId, {
        "ai_description": req.aiDescription,
        "recruiter_notes": req.jobNotes,
        "work_authorization": req.workAuthorization,
        "recruiter_email":     req.recruiterEmail,
    })

    return {
        "jobdiva_updated": jobdiva_ok,
        "locally_tracked": local_ok,
        "message": "Sync complete" if (jobdiva_ok and local_ok) else
                   ("Local OK, JobDiva failed (check credentials)" if local_ok else "Both failed"),
    }

# Rate limiting variables (Less strict for OpenAI)
RATE_LIMIT = 20  # Maximum requests per minute
REQUEST_INTERVAL = 60 / RATE_LIMIT  # Time interval between requests in seconds
last_request_time = 0

@router.post("/jobs/{job_id}/generate-description")
async def generate_job_description(job_id: str, req: JobDescriptionRequest, background_tasks: BackgroundTasks):
    global last_request_time
    current_time = time.time()
    time_since_last_request = current_time - last_request_time

    if time_since_last_request < REQUEST_INTERVAL:
        time.sleep(max(0, REQUEST_INTERVAL - time_since_last_request))

    last_request_time = time.time()

    print(f"DEBUG PAYLOAD: Job Notes Length = {len(req.jobNotes)}, JD Length = {len(req.jobDescription)}")

    prompt = (
        "You are an expert recruitment copywriter. Your task is to generate a premium, catchy, and concise job description ready for external publication on platforms like LinkedIn and job boards.\n\n"
        "STRICT EXTRACTION PRIORITY (You MUST extract concrete facts based on this hierarchy):\n"
        "1. HIGHEST PRIORITY - Job Notes & Work Authorization: Focus heavily on any specific requirements, hiring manager insights, or details mentioned here. Ensure you reflect the Work Authorization requirement clearly if provided.\n"
        "2. SECOND PRIORITY - Existing Job Description: Extensively mine this for concrete facts (e.g., years of experience, mandatory tools, key duties) if they are missing or sparse in the Job Notes. Do NOT summarize away specific numbers like '10 years of experience'.\n"
        "3. LAST PRIORITY - Job Title: Use this for general context and naming conventions.\n\n"
        f"Input Data:\n"
        f"Job Notes: {req.jobNotes}\n"
        f"Work Authorization: {req.workAuthorization}\n"
        f"Existing Job Description: {req.jobDescription}\n"
        f"Job Title: {req.jobTitle}\n\n"
        "STYLING & STRUCTURE INSTRUCTIONS:\n"
        "- Format headers by using **Bold Title Case** (e.g., **The Role**).\n"
        "- Format bullet points by starting the line with the • bullet (e.g., • Responsibility details).\n"
        "- DO NOT use Markdown headings (no #).\n"
        "- ACTIVELY use bolding (**bold**) and italics (*italic*) to emphasize important keywords (e.g., years of experience, specific tools).\n"
        "- MANDATORY BOLDING: You MUST bold the **Location** (e.g., **New York, NY**) whenever it appears in the main body.\n"
        "- ZIP CODE REMOVAL: Do NOT include zip codes or postal codes in any location mention. Always format locations as City, State only (e.g., Austin, TX — not Austin, TX 73301).\n"
        "- PAY RATE RULE: The Pay Rate MUST appear ONLY in the **Pay Rate Transparency** section. DO NOT mention the pay rate, salary, or compensation anywhere in the other sections (THE ROLE, WHAT YOU'LL DO, WHAT YOU BRING, WHY WORK WITH US). Bold the pay rate only inside the **Pay Rate Transparency** section.\n"
        "- PAY RATE FORMAT: When extracting the pay rate, you MUST preserve the EXACT range from the source. If a range is given (e.g., $62 - $62.80/hour or $60 - $80/hour), use the full range — do NOT reduce it to a single value. Only use a single value if the source explicitly provides just one fixed rate.\n"
        "- STRICT REMOVAL: You MUST NOT include the following internal fields in the final output, regardless of whether they appear in the Job Notes or the original Job Description: Bill Rate, Hiring Manager, Customer Name, and Option Ref No.\n"
        "- DO NOT use any emojis anywhere in the text.\n"
        "- START with a catchy 'Hook' or summary that highlights why someone should join.\n"
        "- INCLUDE sections in this EXACT order: **The Role**, **Pay Rate Transparency**, **What You'll Do**, **What You Bring**, and **Why Work With Us**.\n"
        "- SECTION CONTENT: Use the following for the Pay Rate Transparency section:\n\n"
        "**Pay Rate Transparency**\n"
        "Pay Range: [Extracted Pay Rate or XX-XX]/hour. Employee benefits include, but are not limited to, health insurance (medical, dental, vision), 401(k) plan, and paid sick leave (depending on work location).\n\n"
        "- MANDATORY FINAL SECTION: You MUST append the following exactly as written to the very end of the job description:\n\n"
        "**Equal Employment Opportunity**\n"
        "Pyramid Consulting, Inc. provides equal employment opportunities to all employees and applicants for employment and prohibits discrimination and harassment of any type without regard to race, colour, religion, age, sex, national origin, disability status, genetics, protected veteran status, sexual orientation, gender identity or expression, or any other characteristic protected by federal, state, or local laws.\n"
        "By applying to our jobs, you agree to receive calls, AI-generated calls, text messages, or emails from Pyramid Consulting, Inc. and its affiliates, and contracted partners. Frequency varies for text messages. Message and data rates may apply. Carriers are not liable for delayed or undelivered messages. You can reply STOP to cancel and HELP for help. You can access our privacy policy [here](https://pyramidci.com).\n\n"
        "- Use professional and engaging language. Avoid generic corporate speak.\n"
        "- Be concise but impactful. Focus on value propositions.\n"
        "- Ensure the final output is a unified, cohesive narrative that feels like it was written by a human expert.\n\n"
        "Return ONLY the final formatted job description text. No preamble or meta-commentary."
    )

    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set in environment variables.")

    description = None
    try:
        print(f"DEBUG: Attempting JD generation with OpenAI: {OPENAI_MODEL}")
        completion = await client.chat.completions.create(
            model=OPENAI_MODEL if OPENAI_MODEL else "gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert recruitment copywriter."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            timeout=45
        )
        description = completion.choices[0].message.content
        print("DEBUG: OpenAI JD generation successful.")
    except Exception as e:
        print(f"DEBUG: OpenAI JD generation failed: {e}")
        # No fallback to Gemini as requested

    if not description:
        # ULTIMATE FALLBACK: Expert Template
        print("DEBUG: Using Expert Template Fallback due to API issues.")
        description = (
            f"{req.jobTitle.upper()}\n\n"
            "**The Role**\n"
            f"We are looking for a talented {req.jobTitle} to join our growing team. "
            "In this role, you will be a key contributor to our mission, leveraging your expertise to solve complex challenges and drive innovation.\n\n"
            "**What You'll Do**\n"
            "• Collaborate with cross-functional teams to design and implement high-quality solutions.\n"
            "• Take ownership of key components and drive them from concept to production.\n"
            "• Mentor junior team members and contribute to a culture of technical excellence.\n"
            "• Stay ahead of industry trends and incorporate best practices into our development lifecycle.\n\n"
            "**What You Bring**\n"
            "• Proven experience in the relevant field with a strong track record of success.\n"
            "• Excellent analytical, problem-solving, and communication skills.\n"
            "• Ability to work collaboratively in a fast-paced environment.\n\n"
            "**Why Work With Us**\n"
            "• Impact: Your work will directly influence our product and millions of users.\n"
            "• Growth: We offer continuous learning opportunities and a clear career path.\n"
            "• Culture: Join a diverse, inclusive, and collaborative environment where your voice matters.\n\n"
        )

    if description and job_id and job_id != "new":
        ref_code = job_id
        numeric_job_id = job_id  # Default to whatever was passed in
        try:
            import psycopg2
            from core.config import DATABASE_URL
            with psycopg2.connect(DATABASE_URL) as conn:
                with conn.cursor() as cursor:
                    if "-" in str(job_id):
                        # job_id is a ref code (e.g. "26-06182") — look up the numeric ID
                        cursor.execute("SELECT job_id, jobdiva_id FROM monitored_jobs WHERE jobdiva_id = %s", (job_id,))
                        row = cursor.fetchone()
                        if row:
                            numeric_job_id = row[0]  # Use the real numeric job_id as PK
                            ref_code = row[1] or job_id
                        else:
                            ref_code = job_id
                    else:
                        # job_id is already numeric — look up the ref code for display
                        cursor.execute("SELECT jobdiva_id FROM monitored_jobs WHERE job_id = %s", (job_id,))
                        row = cursor.fetchone()
                        if row and row[0]:
                            ref_code = row[0]
        except Exception as e:
            print(f"DEBUG: Failed to fetch ref code: {e}")
            
        description = f"{description}\n\n**JobDiva ID**: {ref_code}"
        # Auto-persist using the NUMERIC job_id to avoid creating a duplicate row
        jobdiva_service.monitor_job_locally(numeric_job_id, {"ai_description": description})

    return {"description": description}

@router.post("/jobs/generate-title")
async def generate_job_title(req: JobDescriptionRequest):
    """
    Generate a polished, professional job title based on the original title and notes.
    """
    prompt = (
        "You are an expert recruitment copywriter. Your task is to polish a job title to make it professional, clear, and focused for external posting.\n\n"
        f"Original Title: {req.jobTitle}\n"
        f"Job Notes Context: {req.jobNotes}\n"
        f"Generated Job Description Content: {req.jobDescription}\n\n"
        "MANDATORY GUIDELINES:\n"
        "- FOCUS ONLY ON THE TITLE: Enhancement should only include functional title and seniority (Senior/Junior). Do NOT include employment type (Contract/W2/Full-Time).\n"
        "- NO LOCATION DETAILS: Do NOT include city, state, or zip code in the title (e.g., avoid '— Atlanta, GA').\n"
        "- NO EXTRA DETAILS: Do NOT include internal codes, project names, or company names.\n"
        "- PRIORITY: Look for seniority and core specialized skills in the 'Generated Job Description Content'.\n"
        "- Ensure the title is concise (under 60 characters).\n"
        "- Use Title Case and remove all internal identifiers.\n\n"
        "Return ONLY the final polished job title. No preamble or meta-commentary."
    )
    
    if not OPENAI_API_KEY:
        print("DEBUG TITLE: No OpenAI API Key found.")
        return {"title": f"ERROR: No API Key"}

    try:
        print(f"DEBUG TITLE: Attempting title enhancement with OpenAI: {OPENAI_MODEL}")
        completion = await client.chat.completions.create(
            model=OPENAI_MODEL if OPENAI_MODEL else "gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert recruitment copywriter."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            timeout=20
        )
        new_title = completion.choices[0].message.content.strip()
        
        # Strip accidental surrounding quotes
        import re
        new_title = re.sub(r'^[\"\']|[\"\']$', '', new_title).strip()
        new_title = new_title.replace("**", "")
        
        print(f"DEBUG TITLE: Original: '{req.jobTitle}' -> New: '{new_title}'")
        
        if new_title:
            return {"title": new_title}
            
    except Exception as e:
        print(f"DEBUG TITLE: OpenAI title enhancement failed: {e}")
        # No fallback to Gemini as requested

    return {"title": f"ERROR: AI enhancement failed"}


class RubricGenerationRequest(BaseModel):
    jobId: str = ""      # Numeric PK for database linking
    jobdivaId: str = ""  # Alphanumeric Ref Code for rubric tables
    jobTitle: str
    enhancedJobTitle: str = ""  # Set when recruiter clicked Enhance; may differ from jobTitle
    jobDescription: str
    jobNotes: str = ""
    originalDescription: str = ""
    customerName: str = ""
    requiredDegree: str = ""
    jobCity: str = ""
    jobState: str = ""
    locationType: str = ""

@router.post("/jobs/generate-rubric")
async def generate_rubric(req: RubricGenerationRequest):
    try:
        import os
        import logging
        
        logger = logging.getLogger(__name__)
        
        # Use provided job_id or create temporary one
        job_id = req.jobId if req.jobId else f"temp_{hashlib.md5(req.jobTitle.encode()).hexdigest()[:8]}"
        
        # Initialize our rubric extractor
        extractor = JobSkillsExtractor(OPENAI_API_KEY)
        
        # Run full rubric extraction
        logger.info(f"🧠 Extracting full rubric strictly from job {job_id} text...")
        rubric_obj = await extractor.extract_full_rubric(
            job_id=job_id,
            job_title=req.jobTitle,
            enhanced_job_title=req.enhancedJobTitle,
            jobdiva_description=req.originalDescription,
            ai_description=req.jobDescription,
            recruiter_notes=req.jobNotes,
            customer_name=req.customerName,
            job_location=f"{req.jobCity}, {req.jobState}".strip(", "),
            location_type=req.locationType
        )
        
        # If JobDiva has a structured degree, and AI found nothing, use it
        if not rubric_obj.education and req.requiredDegree:
             # Basic mapping from JobDiva strings if needed, 
             # but we can just add it as a fact for the AI if we pass it in the prompt
             pass
        
        # Convert dataclass to dict for JSON response
        rubric = asdict(rubric_obj) # Moved this line up to ensure 'rubric' is defined for legacy saving
        
        # Save all rubric components to structured database if we have a job context
        if req.jobId and not req.jobId.startswith("temp_"):
            try:
                # We need the correct jobdiva_id (ref code) for cross-referencing
                # Use provided jobdivaId or try to resolve from background service
                ref_id = req.jobdivaId
                
                if not ref_id or "-" not in str(ref_id):
                    logger.info(f"🔍 Resolving ref_id for numeric jobId: {req.jobId}")
                    job_context = await jobdiva_service.get_job_by_id(req.jobId)
                    if job_context and job_context.get('jobdiva_id'):
                        ref_id = job_context.get('jobdiva_id')
                        logger.info(f"✅ Resolved to {ref_id}")
                    else:
                        ref_id = req.jobId # Fallback if resolution fails
                
                logger.info(f"💾 Persisting full rubric for ref_id: {ref_id}")
                rubric_db = JobRubricDB()
                rubric_db.save_full_rubric(
                    jobdiva_id=ref_id,
                    rubric_obj=rubric_obj,
                    recruiter_notes=req.jobNotes
                )
                
                # Legacy compatibility: also save skills to old table if needed
                # (Optional, but helps keep existing dashboards working)
                extracted_skills = []
                
                # Hard skills
                for s in rubric.get('skills', []):
                    extracted_skills.append(ExtractedSkill(
                        original_text=s.get('value', ''),
                        normalized_name=s.get('value', ''),
                        skill_id=None,
                        importance=s.get('required', 'preferred').lower(),
                        min_years=s.get('minYears', 0),
                        confidence=1.0,
                        category="hard"
                    ))
                
                # Soft skills
                for s in rubric.get('soft_skills', []):
                    extracted_skills.append(ExtractedSkill(
                        original_text=s.get('value', ''),
                        normalized_name=s.get('value', ''),
                        skill_id=None,
                        importance=s.get('required', 'preferred').lower(),
                        min_years=0,
                        confidence=1.0,
                        category="soft"
                    ))
                
                db_service = JobSkillsDB()
                db_service.save_job_skills(
                    jobdiva_id=ref_id,
                    extracted_skills=extracted_skills,
                    analysis_metadata={"source": "Step 3 Generation"}
                )
                logger.info(f"💾 Saved skills to legacy table for jobid: {ref_id}")
            except Exception as e:
                logger.error(f"❌ Failed to persist rubric: {e}")
        
        logger.info(f"✅ Full rubric extracted: {len(rubric['skills'])} skills, {len(rubric['titles'])} titles")
        return rubric
        
    except Exception as e:
        import traceback
        logger.error(f"🚨 Error in full rubric extraction: {e}")
        logger.debug(traceback.format_exc())
        
        # Fallback to minimal rubric if extraction fails
        return {
            "titles": [{"value": req.jobTitle, "minYears": 2, "recent": False, "matchType": "Exact", "required": "Required"}],
            "skills": [],
            "education": [{"degree": "Bachelor's degree", "field": "Relevant field", "required": "Preferred"}],
            "domain": [],
            "customer_requirements": [],
            "other_requirements": []
        }

class ScreeningQuestionsRequest(BaseModel):
    """
    Request model for the new depth-aware screening-question generator
    (Step 4 of the New Job wizard). Replaces the frontend's boilerplate
    "Can you describe your experience with {skill}?" template with
    role + seniority-aware questions produced by an LLM.
    """
    jobTitle: str
    rubric: dict
    screeningLevel: str = "medium"      # light | medium | intensive
    customerName: str = ""
    workArrangement: str = "on-site"    # on-site | onsite | hybrid | remote
    address: str = ""
    totalYears: int = 0


@router.post("/jobs/{job_id}/screening-questions/generate")
async def generate_screening_questions_endpoint(job_id: str, req: ScreeningQuestionsRequest):
    """
    Generate role + seniority-aware screening questions for Step 4.

    Returns a list of question objects the frontend maps onto its
    `screenQuestions` state. Questions are NOT persisted here — the
    frontend drives persistence via the existing save-draft flow.

    Front-matter questions (intro / total-years / work-arrangement) are
    always included. Role-specific questions come from the LLM in
    numbers scaled by screening_level.
    """
    if not client:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not set")

    try:
        questions = await generate_screening_questions(
            openai_client=client,
            model=OPENAI_MODEL or "gpt-4o-mini",
            job_title=req.jobTitle or "",
            rubric=req.rubric or {},
            screening_level=req.screeningLevel,
            customer_name=req.customerName,
            work_arrangement=req.workArrangement,
            address=req.address,
            total_years=req.totalYears or 0,
        )
        return {"questions": questions}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(f"screening-questions generate failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/jobs/{job_id}/rubric")
async def get_job_rubric(job_id: str):
    """
    Retrieve the saved rubric for a job.
    """
    try:
        # First resolve the jobdiva_id (ref code) if only numeric ID is provided
        # The rubric tables use jobdiva_id (e.g. 26-06182)
        ref_id = job_id
        if "-" not in job_id:
            job_context = await jobdiva_service.get_job_by_id(job_id)
            if job_context:
                ref_id = job_context.get('jobdiva_id', job_id)
        
        rubric_db = JobRubricDB()
        rubric = rubric_db.get_full_rubric(ref_id)
        
        if not rubric:
            raise HTTPException(status_code=404, detail="Rubric not found for this job")
            
        return rubric
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error fetching rubric: {e}")
        raise HTTPException(status_code=500, detail=str(e))
