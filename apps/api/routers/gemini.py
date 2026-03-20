import os
import time
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import requests
from time import sleep
from services.jobdiva import jobdiva_service
from services.usage_logger import usage_logger

router = APIRouter()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

print(f"DEBUG: GEMINI_API_KEY loaded: {'Set' if GEMINI_API_KEY else 'NOT SET'}")

class JobDescriptionRequest(BaseModel):
    jobTitle: str
    jobNotes: str
    workAuthorization: str = ""
    jobDescription: str

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
    udf_ai_jd  = int(os.getenv("JOBDIVA_AI_JD_UDF_ID", "230"))
    udf_notes  = int(os.getenv("JOBDIVA_JOB_NOTES_UDF_ID", "231"))

    def truncate(s: str) -> str:
        return s[:3900] if s else ""

    fields = [
        {"userfieldId": udf_ai_jd, "value": truncate(req.aiDescription)},
        {"userfieldId": udf_notes, "value": truncate(req.jobNotes)},
    ]

    jobdiva_ok = await jobdiva_service.update_job_user_fields(req.jobId, fields)
    local_ok   = jobdiva_service.monitor_job_locally(req.jobId, {
        "ai_description": req.aiDescription,
        "job_notes":      req.jobNotes,
        "work_authorization": req.workAuthorization,
        "recruiter_email":     req.recruiterEmail,
    })

    return {
        "jobdiva_updated": jobdiva_ok,
        "locally_tracked": local_ok,
        "message": "Sync complete" if (jobdiva_ok and local_ok) else
                   ("Local OK, JobDiva failed (check credentials)" if local_ok else "Both failed"),
    }

# Rate limiting variables
RATE_LIMIT = 5  # Maximum requests per minute
REQUEST_INTERVAL = 60 / RATE_LIMIT  # Time interval between requests in seconds
last_request_time = 0

@router.post("/jobs/{job_id}/generate-description")
async def generate_job_description(job_id: str, req: JobDescriptionRequest, background_tasks: BackgroundTasks):
    global last_request_time
    current_time = time.time()
    time_since_last_request = current_time - last_request_time

    if time_since_last_request < REQUEST_INTERVAL:
        sleep(REQUEST_INTERVAL - time_since_last_request)

    last_request_time = current_time

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
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY is not set in environment variables.")
    MODELS = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    ]

    description = None
    last_error = ""

    for model_url in MODELS:
        try:
            print(f"DEBUG: Attempting Gemini with model: {model_url.split('/')[-1].split(':')[0]}")
            response = requests.post(
                f"{model_url}?key={GEMINI_API_KEY}",
                json=payload,
                timeout=30
            )
            print(f"DEBUG: Gemini API Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                description = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                if description:
                    # Log Gemini Usage
                    try:
                        # Estimate tokens based on prompt length (very rough) if not in response
                        # Gemini often doesn't provide token usage in the direct generateContent JSON 
                        # unless requested or in specific formats.
                        # We will log it with estimated or 0 tokens if not found.
                        usage_metadata = data.get("usageMetadata", {})
                        p_tokens = usage_metadata.get("promptTokenCount", len(prompt) // 4)
                        c_tokens = usage_metadata.get("candidatesTokenCount", len(description) // 4)
                        
                        usage_logger.log_usage(
                            service="gemini_jd_generation",
                            model=model_url.split('/')[-1].split(':')[0],
                            prompt_tokens=p_tokens,
                            completion_tokens=c_tokens,
                            job_id=job_id
                        )
                    except Exception as log_err:
                        print(f"⚠️ Failed to log Gemini usage: {log_err}")
                    break
            else:
                last_error = response.text
                print(f"DEBUG: Model failed: {last_error}")
                if "RESOURCE_EXHAUSTED" not in last_error:
                    # If it's not a rate limit, maybe don't bother trying the next one? 
                    # Actually, let's just try all models.
                    pass
        except Exception as e:
            last_error = str(e)
            print(f"DEBUG: Exception during model request: {e}")

    if not description:
        # ULTIMATE FALLBACK: Expert Template
        print("DEBUG: Using Expert Template Fallback due to API issues.")
        description = (
            f"{req.jobTitle.upper()}\n\n"
            "THE ROLE\n"
            f"We are looking for a talented {req.jobTitle} to join our growing team. "
            "In this role, you will be a key contributor to our mission, leveraging your expertise to solve complex challenges and drive innovation.\n\n"
            "WHAT YOU'LL DO\n"
            "• Collaborate with cross-functional teams to design and implement high-quality solutions.\n"
            "• Take ownership of key components and drive them from concept to production.\n"
            "• Mentor junior team members and contribute to a culture of technical excellence.\n"
            "• Stay ahead of industry trends and incorporate best practices into our development lifecycle.\n\n"
            "WHAT YOU BRING\n"
            "• Proven experience in the relevant field with a strong track record of success.\n"
            "• Excellent analytical, problem-solving, and communication skills.\n"
            "• Ability to work collaboratively in a fast-paced environment.\n\n"
            "WHY WORK WITH US\n"
            "• Impact: Your work will directly influence our product and millions of users.\n"
            "• Growth: We offer continuous learning opportunities and a clear career path.\n"
            "• Culture: Join a diverse, inclusive, and collaborative environment where your voice matters.\n\n"
        )

    if description and job_id and job_id != "new":
        description = f"{description}\n\n**JobDiva ID**: {job_id}"
        # Auto-persist to local DB so it sticks during regeneration
        jobdiva_service.monitor_job_locally(job_id, {"ai_description": description})

    return {"description": description}

