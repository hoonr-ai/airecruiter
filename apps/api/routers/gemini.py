import os
import time
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import requests
from time import sleep

router = APIRouter()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

print(f"DEBUG: GEMINI_API_KEY loaded: {'Set' if GEMINI_API_KEY else 'NOT SET'}")

class JobDescriptionRequest(BaseModel):
    jobTitle: str
    jobNotes: str
    workAuthorization: str = ""
    jobDescription: str

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
        "- Format headers by using ALL CAPS text (e.g., THE ROLE).\n"
        "- Format bullet points by starting the line with the • bullet (e.g., • Responsibility details).\n"
        "- DO NOT use Markdown headings (no #).\n"
        "- ACTIVELY use bolding (**bold**) and italics (*italic*) to emphasize important keywords (e.g., years of experience, specific tools).\n"
        "- DO NOT use any emojis anywhere in the text.\n"
        "- START with a catchy 'Hook' or summary that highlights why someone should join.\n"
        "- INCLUDE sections for: THE ROLE, WHAT YOU'LL DO, WHAT YOU BRING, and WHY WORK WITH US.\n"
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

    return {"description": description}
