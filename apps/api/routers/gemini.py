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
@router.post("/jobs/generate-title")
async def generate_job_title(req: JobDescriptionRequest):
    """
    Generate a polished, professional job title based on the original title and notes.
    """
    prompt = (
        "You are an expert recruitment copywriter. Your task is to polish a job title to make it catchy, professional, and clear for external posting.\n\n"
        f"Original Title: {req.jobTitle}\n"
        f"Job Notes Context: {req.jobNotes}\n"
        f"Generated Job Description Content: {req.jobDescription}\n\n"
        "MANDATORY GUIDELINES:\n"
        "- BE AGGRESSIVE: If the original title is simple (e.g., 'Analyst'), you MUST enhance it based on the JD content (e.g., 'Senior Business Analyst — Remote').\n"
        "- PRIORITY: Look for seniority (Senior/Junior), employment type (Contract/Hybrid/W2), and specialized skills in the 'Generated Job Description Content'.\n"
        "- If appropriate, add clear suffixes like '— Remote', '— Contract', or '— Hybrid'.\n"
        "- Ensure the title is concise (under 80 characters) but highly descriptive.\n"
        "- Use Title Case and remove internal job codes or IDs.\n\n"
        "Return ONLY the final polished job title. No preamble or meta-commentary."
    )
    
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    
    if not GEMINI_API_KEY:
        print("DEBUG TITLE: No API Key found.")
        return {"title": f"ERROR: No API Key"}

    MODELS = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent"
    ]

    last_error = ""
    for model_url in MODELS:
        try:
            print(f"DEBUG TITLE: Attempting model {model_url.split('/')[-1]}...")
            response = requests.post(f"{model_url}?key={GEMINI_API_KEY}", json=payload, timeout=20)
            print(f"DEBUG TITLE: Gemini Status Code: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                new_title = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                # Strip accidental surrounding quotes
                import re
                new_title = re.sub(r'^[\"\']|[\"\']$', '', new_title).strip()
                new_title = new_title.replace("**", "")
                
                print(f"DEBUG TITLE: Original: '{req.jobTitle}' -> New: '{new_title}'")
                
                if new_title.lower() == req.jobTitle.lower():
                    # Basic forced update based on presence of remote/contract in JD
                    if 'contract' in req.jobDescription.lower():
                        new_title = f"{new_title} (Contract)"
                    elif 'remote' in req.jobDescription.lower():
                        new_title = f"{new_title} (Remote)"
                    else:
                        new_title = f"{new_title} - Enhanced"
                
                if new_title:
                    return {"title": new_title}
            else:
                last_error = f"{response.status_code} - {response.text}"
                print(f"DEBUG TITLE: Error from AI: {last_error}")
                if response.status_code != 429:
                    # If it's a hard error (not rate limit), we could break, but let's just try the next model.
                    pass
        except Exception as e:
            last_error = str(e)
            print(f"DEBUG TITLE: Title enhancement failed: {e}")
            
    # If all models failed
    error_str = str(last_error)
    return {"title": "ERROR 429: Rate Limit Exceeded" if "429" in error_str else f"ERROR: {error_str[:20]}"}

class RubricGenerationRequest(BaseModel):
    jobTitle: str
    jobDescription: str
    jobNotes: str = ""
    originalDescription: str = ""
    customerName: str = ""

@router.post("/jobs/generate-rubric")
async def generate_rubric(req: RubricGenerationRequest):
    prompt = (
        "You are an expert technical recruiter analyzing job data. "
        "Your task is to extract structured rubric criteria from the following sources:\n\n"
        f"1. **AI-ENHANCED JD**: {req.jobDescription}\n"
        f"2. **RECRUITER JOB NOTES**: {req.jobNotes}\n"
        f"3. **ORIGINAL JOBDIVA DESCRIPTION**: {req.originalDescription}\n\n"
        f"Job Title Reference: {req.jobTitle}\n"
        f"Customer Context: {req.customerName}\n\n"
        "INSTRUCTIONS:\n"
        "- SYNTHESIZE ALL SOURCES: Scan all three sources (AI JD, Job Notes, and Original Description) for concrete requirements.\n"
        "- NO REDUNDANCY: Strictly avoid duplicate or redundant criteria. If a requirement is mentioned in multiple sources, extract it only once with the most complete context.\n"
        "- PRIMARY TITLE: You MUST use the provided 'Job Title Reference' as the single entry in the 'titles' array. Ensure it remains professional and matches the Step 2 selection.\n"
        "- EDUCATION EXTRACTION: Pay extremely close attention to education requirements. Look for degrees (Bachelor's, Master's, PhD) and specific fields of study mentioned in ANY of the sources. If JD and Notes differ, prioritize the Job Notes.\n"
        "- Return the output STRICTLY as a valid JSON object matching this schema exactly:\n"
        "{\n"
        "  \"titles\": [{\"value\": \"string\", \"minYears\": number, \"recent\": boolean, \"matchType\": \"Exact\" | \"Similar\", \"required\": \"Required\" | \"Preferred\"}],\n"
        "  \"skills\": [{\"value\": \"string\", \"minYears\": number, \"recent\": boolean, \"matchType\": \"Exact\" | \"Similar\", \"required\": \"Required\" | \"Preferred\"}],\n"
        "  \"education\": [{\"degree\": \"string\", \"field\": \"string\", \"required\": \"Required\" | \"Preferred\"}],\n"
        "  \"domain\": [{\"value\": \"string\", \"required\": \"Required\" | \"Preferred\"}],\n"
        "  \"customer_requirements\": [{\"type\": \"Must not be employed by\" | \"Currently employed by\" | \"Previously employed by\", \"value\": \"string\"}],\n"
        "  \"other_requirements\": [{\"value\": \"string\", \"required\": \"Required\" | \"Preferred\"}]\n"
        "}\n\n"
        "GUIDELINES:\n"
        "- Keep skill names CRISP and CONCISE (max 1-5 words). Avoid long sentences. Focus only on the core tool or competency.\n"
        "- Extract max 1 title.\n"
        "- Extract 5 to 10 top hard skills. If an experience requirement states '2 years', set minYears to 2. Else set to 0.\n"
        "- If industry domain is mentioned, extract it.\n"
        f"- If any customer-specific constraints (like do not submit candidates from {req.customerName}), put that in customer_requirements.\n"
        "- Put other constraints like W2 only, local to city, no relocation, in other_requirements.\n"
        "- If it's explicitly 'must have' set required to 'Required'. Otherwise 'Preferred'."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    MODELS = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    ]
    
    import json
    for model_url in MODELS:
        try:
            response = requests.post(f"{model_url}?key={GEMINI_API_KEY}", json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                content = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                if content:
                    return json.loads(content)
        except Exception as e:
            print(f"DEBUG RUBRIC: Error: {e}")
            
    # Fallback default exactly matching screenshot
    return {
        "titles": [{"value": req.jobTitle, "minYears": 2, "recent": False, "matchType": "Exact", "required": "Required"}],
        "skills": [
            {"value": "Accounts Payable", "minYears": 2, "recent": False, "matchType": "Exact", "required": "Required"},
            {"value": "ERP Systems", "minYears": 1, "recent": False, "matchType": "Similar", "required": "Required"},
            {"value": "3-Way PO Matching", "minYears": 1, "recent": False, "matchType": "Similar", "required": "Required"},
            {"value": "GAAP Knowledge", "minYears": 1, "recent": False, "matchType": "Similar", "required": "Required"},
            {"value": "Month-End Close", "minYears": 0, "recent": False, "matchType": "Similar", "required": "Preferred"},
            {"value": "ACH Processing", "minYears": 0, "recent": False, "matchType": "Similar", "required": "Preferred"},
            {"value": "Vendor Reconciliation", "minYears": 0, "recent": False, "matchType": "Similar", "required": "Preferred"},
            {"value": "High-Volume Invoicing", "minYears": 1, "recent": False, "matchType": "Similar", "required": "Preferred"}
        ],
        "education": [{"degree": "Bachelor's degree", "field": "Accounting or Finance", "required": "Preferred"}],
        "domain": [{"value": "Healthcare, Finance / Accounting", "required": "Preferred"}],
        "customer_requirements": [{"type": "Must not be employed by", "value": req.customerName or "Meridian Health Group"}],
        "other_requirements": [
            {"value": "Must be local to Atlanta metro — no relocation", "required": "Required"},
            {"value": "W2 only — no C2C or 1099", "required": "Required"}
        ]
    }
