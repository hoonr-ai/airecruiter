
import json
import re
import time
import asyncio
import logging
from typing import List, Optional, Dict, Any
import sqlalchemy
from sqlalchemy import text
from core.config import (
    DATABASE_URL, SUPABASE_DB_URL,
    AZURE_OPENAI_API_KEY,
    OPENAI_API_KEY, OPENAI_MODEL
)
import httpx
from models import SourcedCandidate
from services.candidate_profiles_db import candidate_profiles_db

logger = logging.getLogger(__name__)


def _truncate_log_value(value: Any, limit: int = 80) -> str:
    text_value = str(value or "").strip()
    if len(text_value) <= limit:
        return text_value or "-"
    return text_value[: limit - 3] + "..."


def _clean_extracted_value(value: Any) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None

    invalid_markers = {
        "not provided",
        "n/a",
        "na",
        "unknown",
        "none",
        "null",
        "professional candidate",
        "available upon request",
    }
    if cleaned.lower() in invalid_markers:
        return None
        
    # Alphanumeric ID detection: Reject long strings (>15 chars) with no spaces that contain digits
    # These are almost always internal IDs or hashes from external scrapers.
    if len(cleaned) > 15 and " " not in cleaned:
        if any(c.isdigit() for c in cleaned):
            return None
        # Also reject if it has high entropy (mix of many upper/lower case letters with no spaces)
        if len(re.findall(r'[A-Z]', cleaned)) > 5 and len(re.findall(r'[a-z]', cleaned)) > 5:
            return None
            
    return cleaned


def _has_real_resume_text(resume_text: str) -> bool:
    cleaned = str(resume_text or "").strip()
    if not cleaned:
        return False

    placeholder_markers = [
        "resume content unavailable",
        "professional experience details available upon request",
        "experienced professional with a strong background",
        "contact information and detailed work history available upon request",
        "available upon request",
    ]
    lowered = cleaned.lower()
    return not any(marker in lowered for marker in placeholder_markers)


def _extract_resume_contact_details(resume_text: str) -> Dict[str, Any]:
    cleaned = str(resume_text or "")
    extracted: Dict[str, Any] = {"email": None, "phone": None, "urls": {}}
    if not cleaned:
        return extracted

    email_match = re.search(r"([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})", cleaned, re.IGNORECASE)
    if email_match:
        extracted["email"] = email_match.group(1).strip()

    phone_match = re.search(
        r"(\+?\d[\d\-\(\)\s]{7,}\d)",
        cleaned,
    )
    if phone_match:
        extracted["phone"] = re.sub(r"\s+", " ", phone_match.group(1)).strip()

    urls: Dict[str, str] = {}
    for raw_url in re.findall(r"(https?://[^\s\]\)<>]+|www\.[^\s\]\)<>]+|linkedin\.com/[^\s\]\)<>]+|github\.com/[^\s\]\)<>]+)", cleaned, re.IGNORECASE):
        normalized = raw_url.strip().rstrip(".,;")
        if normalized.lower().startswith("www."):
            normalized = f"https://{normalized}"
        elif not normalized.lower().startswith("http"):
            normalized = f"https://{normalized}"

        lowered = normalized.lower()
        if "linkedin.com/" in lowered:
            urls["linkedin"] = normalized
        elif "github.com/" in lowered:
            urls["github"] = normalized
        elif "mailto:" not in lowered:
            urls.setdefault("portfolio", normalized)

    extracted["urls"] = _normalize_candidate_urls(urls)
    return extracted


def _normalize_llm_skills(llm_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    formatted_skills: List[Dict[str, Any]] = []
    llm_skills = llm_result.get("skills", []) or llm_result.get("hard_skills", [])
    for skill in llm_skills:
        if isinstance(skill, dict) and skill.get("name"):
            formatted_skills.append({
                "skill": str(skill["name"]).strip(),
                "similar_skills": [],
            })
        elif isinstance(skill, str) and skill.strip():
            formatted_skills.append({
                "skill": skill.strip(),
                "similar_skills": [],
            })
    return formatted_skills


def _normalize_candidate_urls(urls: Any) -> Dict[str, str]:
    if not isinstance(urls, dict):
        return {}

    allowed_keys = {"linkedin", "github", "portfolio"}
    normalized_urls: Dict[str, str] = {}
    for key, value in urls.items():
        normalized_key = str(key or "").strip().lower()
        normalized_value = str(value or "").strip()
        if normalized_key in allowed_keys and normalized_value:
            normalized_urls[normalized_key] = normalized_value
    return normalized_urls


def _log_extraction_snapshot(candidate_id: str, enhanced_info: Dict[str, Any]) -> None:
    skills = enhanced_info.get("structured_skills", []) or []
    companies = enhanced_info.get("company_experience", []) or []
    education = enhanced_info.get("candidate_education", []) or []
    certifications = enhanced_info.get("candidate_certification", []) or []
    logger.info(
        "[ResumeExtract] candidate_id=%s status=%s name=%s title=%s years=%s location=%s skills=%s companies=%s education=%s certs=%s",
        candidate_id,
        enhanced_info.get("resume_extraction_status", "unknown"),
        _truncate_log_value(enhanced_info.get("candidate_name")),
        _truncate_log_value(enhanced_info.get("job_title")),
        _truncate_log_value(enhanced_info.get("years_of_experience")),
        _truncate_log_value(enhanced_info.get("current_location")),
        len(skills),
        len(companies),
        len(education),
        len(certifications),
    )

class SourcedCandidatesStorage:
    def __init__(self):
        self.db_url = DATABASE_URL or SUPABASE_DB_URL

    def _ensure_table(self, conn):
        """Ensure the sourced_candidates table exists with clean optimized schema."""
        # Create table with clean schema (no redundant columns)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sourced_candidates (
                id SERIAL PRIMARY KEY,
                jobdiva_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT,
                email TEXT,
                phone TEXT,
                headline TEXT,
                location TEXT,
                resume_id TEXT,
                resume_text TEXT,
                profile_url TEXT,
                image_url TEXT,
                data JSONB,
                status TEXT DEFAULT 'sourced',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(jobdiva_id, candidate_id, source)
            )
        """))
        
        # Migration: rename columns if old schema exists
        try:
            conn.execute(text("ALTER TABLE sourced_candidates RENAME COLUMN job_id TO jobdiva_id"))
        except Exception:
            pass  # Already renamed or doesn't exist
            
        try:
            conn.execute(text("ALTER TABLE sourced_candidates RENAME COLUMN jobdiva_resume_id TO resume_id"))
        except Exception:
            pass  # Already renamed or doesn't exist
            
        # Remove redundant columns
        try:
            conn.execute(text("ALTER TABLE sourced_candidates DROP COLUMN IF EXISTS jobdiva_candidate_id"))
            conn.execute(text("ALTER TABLE sourced_candidates DROP COLUMN IF EXISTS candidate_type"))
        except Exception:
            pass  # Already dropped or doesn't exist
            
        # Add missing columns
        try:
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS email TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS phone TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_id TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS resume_text TEXT"))
            conn.execute(text("ALTER TABLE sourced_candidates ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
            conn.commit()
        except Exception as e:
            pass  # Column might already exist

    def save_candidates(self, jobdiva_id: str, candidates: List[SourcedCandidate]) -> int:
        """Save search results to the database with enhanced JobDiva integration."""
        if not self.db_url:
            return 0
        
        saved_count = 0
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                for c in candidates:
                    try:
                        # Enhanced insert with clean schema
                        conn.execute(text("""
                            INSERT INTO sourced_candidates 
                            (jobdiva_id, candidate_id, source, name, email, phone, headline, location, profile_url, image_url, 
                             data, status, resume_id, resume_text, updated_at)
                            VALUES (:jobdiva_id, :candidate_id, :source, :name, :email, :phone, :headline, :location, 
                                    :profile_url, :image_url, :data, :status, :resume_id, :resume_text, CURRENT_TIMESTAMP)
                            ON CONFLICT (jobdiva_id, candidate_id, source) 
                            DO UPDATE SET 
                                name = EXCLUDED.name,
                                email = EXCLUDED.email,
                                phone = EXCLUDED.phone,
                                headline = EXCLUDED.headline,
                                location = EXCLUDED.location,
                                profile_url = EXCLUDED.profile_url,
                                image_url = EXCLUDED.image_url,
                                data = EXCLUDED.data,
                                resume_id = EXCLUDED.resume_id,
                                resume_text = EXCLUDED.resume_text,
                                updated_at = CURRENT_TIMESTAMP
                        """), {
                            "jobdiva_id": jobdiva_id,
                            "candidate_id": c.candidate_id,
                            "source": c.source,
                            "name": c.name,
                            "email": getattr(c, 'email', None),
                            "phone": getattr(c, 'phone', None),
                            "headline": c.headline,
                            "location": c.location,
                            "profile_url": c.profile_url,
                            "image_url": c.image_url,
                            "data": json.dumps(c.data) if c.data else None,
                            "status": c.status,
                            "resume_id": getattr(c, 'resume_id', None),
                            "resume_text": getattr(c, 'resume_text', None)
                        })
                        saved_count += 1
                    except Exception as e:
                        print(f"Error saving candidate {c.candidate_id}: {e}")
                        
                conn.commit()
        except Exception as e:
            print(f"Error saving candidates: {e}")
        
        return saved_count

    def save_enhanced_candidate(self, job_id: str, candidate_data: Dict[str, Any]) -> bool:
        """Save a single enhanced candidate with full JobDiva data integration using new schema."""
        if not self.db_url:
            return False
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                conn.execute(text("""
                    INSERT INTO sourced_candidates 
                    (jobdiva_id, candidate_id, source, name, email, phone, headline, location, profile_url, 
                     image_url, resume_id, resume_text, data, status, updated_at)
                    VALUES (:jobdiva_id, :candidate_id, :source, :name, :email, :phone, :headline, :location, 
                            :profile_url, :image_url, :resume_id, :resume_text, :data, :status, CURRENT_TIMESTAMP)
                    ON CONFLICT (jobdiva_id, candidate_id, source) 
                    DO UPDATE SET 
                        name = EXCLUDED.name,
                        email = EXCLUDED.email,
                        phone = EXCLUDED.phone,
                        headline = EXCLUDED.headline,
                        location = EXCLUDED.location,
                        profile_url = EXCLUDED.profile_url,
                        image_url = EXCLUDED.image_url,
                        resume_id = EXCLUDED.resume_id,
                        resume_text = EXCLUDED.resume_text,
                        data = EXCLUDED.data,
                        status = 'sourced',
                        updated_at = CURRENT_TIMESTAMP
                """), candidate_data)
                
                conn.commit()
                return True
        except Exception as e:
            print(f"Error saving enhanced candidate: {e}")
            return False

    def deduplicate_candidates(self, job_id: str) -> int:
        """Remove duplicates, prioritizing job applicants over talent search using new schema."""
        if not self.db_url:
            return 0
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                result = conn.execute(text("""
                    DELETE FROM sourced_candidates s1
                    WHERE s1.jobdiva_id = :job_id
                    AND EXISTS (
                        SELECT 1 FROM sourced_candidates s2
                        WHERE s2.jobdiva_id = s1.jobdiva_id
                        AND s2.candidate_id = s1.candidate_id
                        AND s2.source = 'JobDiva-Applicants'
                        AND s1.source = 'JobDiva-TalentSearch'
                        AND s1.id != s2.id
                    )
                """), {"job_id": job_id})
                
                conn.commit()
                return result.rowcount
        except Exception as e:
            print(f"Error deduplicating candidates: {e}")
            return 0

    def get_candidates_for_job(self, jobdiva_id: str) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates for a specific job."""
        if not self.db_url:
            return []
            
        try:
            engine = sqlalchemy.create_engine(self.db_url)
            with engine.connect() as conn:
                self._ensure_table(conn)
                
                # First, find both IDs for this job to ensure we catch all candidates
                # A job can be identified by its numeric job_id OR its jobdiva_id (ref code)
                alt_ids = [jobdiva_id]
                job_lookup = conn.execute(text("""
                    SELECT job_id, jobdiva_id FROM monitored_jobs 
                    WHERE job_id = :id OR jobdiva_id = :id
                """), {"id": jobdiva_id}).fetchone()
                
                if job_lookup:
                    alt_ids = [job_lookup[0], job_lookup[1]]
                
                result = conn.execute(text("""
                    SELECT * FROM sourced_candidates 
                    WHERE job_id IN :ids 
                    ORDER BY created_at DESC
                """), {"ids": tuple(alt_ids)})
                
                candidates = []
                for row in result:
                    c_dict = dict(zip(result.keys(), row))
                    if c_dict.get('data'):
                        try:
                            if isinstance(c_dict['data'], str):
                                c_dict['data'] = json.loads(c_dict['data'])
                        except:
                            pass
                    if c_dict.get('created_at'):
                        c_dict['created_at'] = str(c_dict['created_at'])
                    candidates.append(c_dict)
                return candidates
        except Exception as e:
            print(f"Error retrieving candidates for job {jobdiva_id}: {e}")
            return []

    def get_all_candidates(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Retrieve all sourced candidates across all jobs."""
        if not self.db_url:
            return []
            
        try:
            # Use fresh connection to avoid transaction issues
            import psycopg2
            import psycopg2.extras
            
            conn = psycopg2.connect(self.db_url)
            conn.autocommit = True  # Prevent transaction issues
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            
            # Join with monitored_jobs to get job title
            # Try matching by jobdiva_id with the monitored jobs
            cur.execute("""
                SELECT sc.*, mj.title as job_title 
                FROM sourced_candidates sc
                LEFT JOIN monitored_jobs mj ON (sc.jobdiva_id = mj.job_id OR sc.jobdiva_id = mj.jobdiva_id)
                ORDER BY sc.created_at DESC
                LIMIT %s
            """, (limit,))
            
            candidates = []
            for row in cur.fetchall():
                c_dict = dict(row)
                if c_dict.get('data'):
                    try:
                        if isinstance(c_dict['data'], str):
                            c_dict['data'] = json.loads(c_dict['data'])
                    except:
                        pass
                if c_dict.get('created_at'):
                    c_dict['created_at'] = str(c_dict['created_at'])
                candidates.append(c_dict)
                
            cur.close()
            conn.close()
            return candidates
        except Exception as e:
            print(f"Error retrieving all candidates: {e}")
            return []

sourced_candidates_storage = SourcedCandidatesStorage()


# Concurrency controls to prevent 429 Too Many Requests for LLM enrichment.
_llm_semaphore = asyncio.Semaphore(2)  # Reduced to 2 to avoid rate limits

async def crisp_resume_with_ai(resume_text: str, max_length: int = 7500) -> str:
    """
    Condense resume using AI to fit within token limits while preserving ALL important details.
    
    PRESERVED (no truncation):
    - All company names and job titles
    - All employment dates (start/end)
    - All education (degree, institution, year)
    - All certifications (name, issuer, year)
    - All skills
    - Contact information (email, phone, LinkedIn)
    
    REMOVED (fluff reduction):
    - Long job descriptions
    - Generic soft skills ("team player", "hardworking")
    - Redundant phrases
    - Formatting noise
    - Buzzwords
    
    Returns crisped resume under max_length characters.
    Original resume is NOT modified - used only for extraction.
    """
    # If resume is already short enough, return as-is
    if len(resume_text) <= max_length:
        logger.info(f"📄 [Resume Crisping] Resume already short ({len(resume_text)} chars), skipping crisping")
        return resume_text
    
    logger.info(f"📄 [Resume Crisping] Starting crisping for resume ({len(resume_text)} chars)")
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    
    prompt = (
        "Condense the following resume to under 7500 characters. "
        "You MUST preserve ALL of the following without exception:\n\n"
        "REQUIRED (do not remove or summarize):\n"
        "- Every single company name\n"
        "- Every single job title\n"
        "- Every employment date (start and end dates)\n"
        "- Every degree and institution\n"
        "- Every certification name, issuer, and year\n"
        "- All technical skills\n"
        "- Contact information (email, phone, LinkedIn URL)\n\n"
        "REMOVE (to save space):\n"
        "- Long job descriptions and responsibilities\n"
        "- Generic soft skills (team player, hardworking, etc.)\n"
        "- Redundant phrases\n"
        "- Formatting noise\n\n"
        "Format the output as a clean, scannable resume. "
        "Use bullet points for companies and education. "
        "Keep dates in 'MMM YYYY' format.\n\n"
        "Resume to condense:\n"
        "---BEGIN RESUME---\n"
        + resume_text +
        "\n---END RESUME---"
    )
    
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are a resume editor. Condense resumes by removing fluff while preserving all companies, titles, dates, education, and certifications. Never truncate important details."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 2000
    }
    
    async with _llm_semaphore:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    
                    if response.status_code == 429:
                        wait_time = 5 * (2 ** attempt)
                        logger.warning(f"⚠️ [Resume Crisping] 429 error, retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    response.raise_for_status()
                    result = response.json()
                    crisped = result["choices"][0]["message"]["content"]
                    
                    logger.info(f"✅ [Resume Crisping] Crisped resume from {len(resume_text)} to {len(crisped)} chars")
                    return crisped
                    
            except Exception as e:
                if attempt == 2:
                    logger.error(f"❌ [Resume Crisping] Failed after 3 attempts: {e}")
                    # Fall back to truncation if crisping fails
                    logger.warning(f"⚠️ [Resume Crisping] Falling back to simple truncation")
                    return resume_text[:max_length]
                
                wait_time = 5 * (2 ** attempt)
                logger.warning(f"⚠️ [Resume Crisping] Error on attempt {attempt + 1}: {e}, retrying...")
                await asyncio.sleep(wait_time)
    
    # Should not reach here, but just in case
    return resume_text[:max_length]

async def extract_enhanced_info_with_llm(resume_text: str) -> Dict[str, Any]:
    """Call OpenAI LLM to extract enhanced info from resume text."""
    logger.info("[LLM] resume_chars=%s model=%s starting extraction", len(resume_text), OPENAI_MODEL)
    
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        logger.error("❌ [LLM] OpenAI API key or model not configured")
        raise RuntimeError("OpenAI API key or model not configured.")
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    # Sanitize resume text to avoid JSON parsing issues while keeping enough of the
    # resume to capture lower-page sections like education and certifications.
    sanitized_resume = resume_text[:16000]
    # Remove null bytes and other control characters that could break JSON
    sanitized_resume = ''.join(char for char in sanitized_resume if ord(char) >= 32 or char in '\n\r\t')
    
    prompt = (
        "Extract the following information from the resume text and return ONLY valid JSON matching this exact structure (no summary field):\n"
        "{\n"
        '  "candidate_name": "Full Name",\n'
        '  "email": "email@example.com",\n'
        '  "phone": "+1-xxx-xxx-xxxx",\n'
        '  "job_title": "Most recent or current job title",\n'
        '  "years_of_experience": 5,\n'
        '  "current_location": "City, State",\n'
        '  "skills": [\n'
        '    {\n'
        '      "name": "Skill Name"\n'
        '    }\n'
        '  ],\n'
        '  "company_experience": [\n'
        '    {\n'
        '      "company": "Company Name",\n'
        '      "title": "Job Title",\n'
        '      "start_date": "Jan 2020",\n'
        '      "end_date": "Dec 2023 or Present"\n'
        '    }\n'
        '  ],\n'
        '  "candidate_education": [\n'
        '    {\n'
        '      "degree": "Degree Name",\n'
        '      "institution": "University Name",\n'
        '      "year": "2020"\n'
        '    }\n'
        '  ],\n'
        '  "candidate_certification": [\n'
        '    {\n'
        '      "name": "Certification Name",\n'
        '      "issuer": "Issuing Organization",\n'
        '      "year": "2021"\n'
        '    }\n'
        '  ],\n'
        '  "urls": {\n'
        '    "linkedin": "https://linkedin.com/in/...",\n'
        '    "github": "https://github.com/...",\n'
        '    "portfolio": "https://..."\n'
        '  }\n'
        "}\n\n"
        "Instructions:\n"
        "1. Fill every field in the JSON from the resume text whenever the resume contains that information.\n"
        "2. job_title must be the candidate's current or most recent title from the latest experience entry.\n"
        "3. years_of_experience must be a numeric total based on the resume timeline or explicit summary.\n"
        "4. Extract concrete professional skills into skills[].name. Include all meaningful technical and functional skills stated in the resume.\n"
        "5. Extract complete company_experience entries with company, title, start_date, end_date. List them in reverse chronological order.\n"
        "6. Extract all education entries present in the resume into candidate_education.\n"
        "7. Extract all certifications/licenses present in the resume into candidate_certification.\n"
        "8. Extract only LinkedIn, GitHub, and portfolio URLs into urls.\n"
        "9. Do not use placeholders like 'available upon request', 'not provided', 'n/a', or empty dummy values.\n"
        "10. Return ONLY the JSON object, no markdown, no explanations.\n"
        "11. Ensure all strings are properly escaped and JSON is valid.\n\n"
        "Resume Text (parse this to extract the information above):\n"
        "---BEGIN RESUME---\n"
        + sanitized_resume +
        "\n---END RESUME---"
    )
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": "You are an expert resume parser. Always return valid JSON only, no markdown formatting, no explanations."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": 1800
    }
    
    async with _llm_semaphore:
        for attempt in range(5):
            logger.info(f"🧠 [LLM] Attempt {attempt + 1}/5...")
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    logger.info(f"🧠 [LLM] Sending request to OpenAI API...")
                    response = await client.post(url, headers=headers, json=payload)
                    
                    if response.status_code == 429:
                        wait_time = 5 * (2 ** attempt)
                        logger.warning(f"⚠️ [LLM] 429 Too Many Requests, retrying in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    
                    response.raise_for_status()
                    result = response.json()
                    logger.info("[LLM] response received")
                    
                    # Parse the LLM's JSON output from the response
                    content = result["choices"][0]["message"]["content"]
                    logger.info("[LLM] response_chars=%s", len(content))
                    
                    try:
                        parsed = json.loads(content)
                        
                        logger.info(
                            "[LLM] parsed name=%s title=%s years=%s location=%s skills=%s companies=%s education=%s certs=%s urls=%s",
                            _truncate_log_value(parsed.get("candidate_name")),
                            _truncate_log_value(parsed.get("job_title")),
                            _truncate_log_value(parsed.get("years_of_experience")),
                            _truncate_log_value(parsed.get("current_location")),
                            len(parsed.get("skills", []) or []),
                            len(parsed.get("company_experience", []) or []),
                            len(parsed.get("candidate_education", []) or []),
                            len(parsed.get("candidate_certification", []) or []),
                            len([v for v in (parsed.get("urls", {}) or {}).values() if v]),
                        )
                        return parsed
                    except json.JSONDecodeError as json_err:
                        logger.warning(f"⚠️ [LLM] JSON parse error: {json_err}, returning raw content")
                        logger.debug(f"[LLM] Raw content: {content[:500]}...")
                        return {"raw": content}
            except Exception as e:
                if attempt == 4:
                    logger.error(f"❌ [LLM] Error on final attempt: {e}")
                    return {"error": str(e)}
                
                wait_time = 5 * (2 ** attempt)
                logger.warning(f"⚠️ [LLM] Error on attempt {attempt + 1}: {e}, retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)


async def process_jobdiva_candidate(candidate: Dict[str, Any]):
    candidate_id = candidate.get("candidate_id", "unknown")
    candidate_name = candidate.get("name", "Unknown")
    
    logger.info(f"🔄 [Candidate:{candidate_id}] Starting resume processing for {candidate_name}")
    
    # Get original resume text (this is what gets saved to database and shown in UI)
    original_resume_text = candidate.get("resume_text", "")
    if not _has_real_resume_text(original_resume_text):
        logger.warning(f"⚠️ [Candidate:{candidate_id}] No usable resume text found, skipping processing")
        return {"candidate_id": candidate_id, "raw": {}, "skipped": True}
    
    logger.info(f"📄 [Candidate:{candidate_id}] Original resume text length: {len(original_resume_text)} characters")
    resume_contact_fallbacks = _extract_resume_contact_details(original_resume_text)

    # Step 1: Crisp the resume first, then let the LLM extract every field from
    # that crisped version. This matches the earlier successful flow while
    # keeping extraction fully LLM-driven.
    logger.info(f"📄 [Candidate:{candidate_id}] Crisping resume for LLM extraction...")
    crisped_resume_text = await crisp_resume_with_ai(original_resume_text, max_length=12000)
    logger.info(f"📄 [Candidate:{candidate_id}] Using crisped resume ({len(crisped_resume_text)} chars) for LLM extraction")
        
    # Step 2: Run LLM-only extraction for all enhanced candidate fields.
    logger.info(f"🧠 [Candidate:{candidate_id}] Calling LLM for enhanced info extraction (using crisped resume)...")
    enhanced_info_task = asyncio.create_task(extract_enhanced_info_with_llm(crisped_resume_text))
    
    logger.info(f"⏳ [Candidate:{candidate_id}] Waiting for LLM response...")
    enhanced_info_result = await enhanced_info_task
    
    # Log LLM results
    if enhanced_info_result.get("error"):
        logger.error(f"❌ [Candidate:{candidate_id}] LLM error: {enhanced_info_result.get('error')}")
        formatted_skills = []
    else:
        formatted_skills = _normalize_llm_skills(enhanced_info_result)

    extracted_job_title = (
        enhanced_info_result.get("job_title")
        or enhanced_info_result.get("current_title")
        or ((enhanced_info_result.get("company_experience") or [{}])[0].get("title") if isinstance(enhanced_info_result.get("company_experience"), list) and (enhanced_info_result.get("company_experience") or []) else None)
        or candidate.get("title")
        or candidate.get("headline")
    )

    # Validate LLM-extracted name - fall back to JobDiva name if LLM returns "Not Provided" or empty
    llm_name = _clean_extracted_value(enhanced_info_result.get("candidate_name"))
    jobdiva_name = _clean_extracted_value(candidate.get("name"))
    
    # Use LLM name only if it's valid (not empty and not "Not Provided")
    if llm_name:
        final_name = llm_name
    elif jobdiva_name:
        final_name = jobdiva_name
    else:
        final_name = None
    
    logger.info(f"👤 [Candidate:{candidate_id}] Name selection: LLM='{llm_name}', JobDiva='{jobdiva_name}', Final='{final_name}'")
    
    enhanced_info = {
        # LLM-only extracted values for candidate enrichment
        "candidate_name": final_name,
        "email": _clean_extracted_value(enhanced_info_result.get("email")) or _clean_extracted_value(resume_contact_fallbacks.get("email")) or _clean_extracted_value(candidate.get("email")),
        "phone": _clean_extracted_value(enhanced_info_result.get("phone")) or _clean_extracted_value(resume_contact_fallbacks.get("phone")) or _clean_extracted_value(candidate.get("phone")),
        "job_title": _clean_extracted_value(extracted_job_title),
        "years_of_experience": enhanced_info_result.get("years_of_experience"),
        "current_location": _clean_extracted_value(enhanced_info_result.get("current_location")) or _clean_extracted_value(candidate.get("location")),
        
        # Structured data from LLM
        "company_experience": enhanced_info_result.get("company_experience", []),
        "candidate_education": enhanced_info_result.get("candidate_education", []),
        "candidate_certification": enhanced_info_result.get("candidate_certification", []),
        "urls": _normalize_candidate_urls({
            **(resume_contact_fallbacks.get("urls") or {}),
            **(enhanced_info_result.get("urls") or {}),
        }),
        "structured_skills": formatted_skills,
        
        # Metadata
        "source": candidate.get("source", "JobDiva"),
        "resume_extraction_status": "completed" if any([
            final_name,
            _clean_extracted_value(extracted_job_title),
            formatted_skills,
            enhanced_info_result.get("company_experience", []),
            enhanced_info_result.get("candidate_education", []),
            enhanced_info_result.get("candidate_certification", []),
            _clean_extracted_value(enhanced_info_result.get("phone")),
            _clean_extracted_value(enhanced_info_result.get("email")),
            _clean_extracted_value(enhanced_info_result.get("current_location")),
        ]) else "partial"
    }

    _log_extraction_snapshot(candidate_id, enhanced_info)

    # Save to candidate_enhanced_info (using ORIGINAL resume, not crisped)
    logger.info(f"💾 [Candidate:{candidate_id}] Saving to candidate_enhanced_info table...")
    try:
        save_candidate_enhanced_info(candidate["candidate_id"], enhanced_info, original_resume_text)
        logger.info(f"✅ [Candidate:{candidate_id}] Successfully saved to candidate_enhanced_info")
    except Exception as e:
        logger.error(f"❌ [Candidate:{candidate_id}] Failed to save to candidate_enhanced_info: {e}")

    logger.info("[ResumeExtract] candidate_id=%s persisted=%s source=%s", candidate_id, "yes", enhanced_info.get("source", "JobDiva"))
    
    # Return merged/enriched data for API response
    return {
        "candidate_id": candidate["candidate_id"],
        "name": enhanced_info.get("candidate_name"),
        "current_title": enhanced_info.get("job_title"),
        "location": enhanced_info.get("current_location"),
        "years_experience": enhanced_info.get("years_of_experience"),
        "skills": formatted_skills,
        "company_experience": enhanced_info.get("company_experience", []),
        "education": enhanced_info.get("candidate_education", []),
        "certifications": enhanced_info.get("candidate_certification", []),
        "urls": enhanced_info.get("urls", {}),
        "raw": enhanced_info
    }


async def process_linkedin_candidate(candidate: Dict[str, Any]):
    """Process LinkedIn candidate and save enhanced info to database."""
    candidate_id = candidate.get("candidate_id", candidate.get("id", "unknown"))
    candidate_name = candidate.get("name", "Unknown")
    
    logger.info(f"🔄 [LinkedIn Candidate:{candidate_id}] Starting profile processing for {candidate_name}")
    
    # Build a pseudo-resume from LinkedIn profile data for LLM extraction
    profile_summary = candidate.get("summary", "")
    headline = candidate.get("title", candidate.get("headline", ""))
    location = candidate.get("location", candidate.get("city", ""))
    company_exp = candidate.get("company_experience", [])
    education = candidate.get("candidate_education", [])
    certifications = candidate.get("candidate_certification", [])
    skills = candidate.get("skills", [])
    profile_url = candidate.get("profile_url", "")
    
    # Construct a text representation of the LinkedIn profile
    linkedin_profile_text = f"""
Name: {candidate_name}
Headline: {headline}
Location: {location}
Profile URL: {profile_url}

Summary:
{profile_summary}

Experience:
"""
    
    for exp in company_exp:
        linkedin_profile_text += f"- {exp.get('title', '')} at {exp.get('company', '')} ({exp.get('start_date', '')} to {exp.get('end_date', '')})\n"
    
    linkedin_profile_text += "\nEducation:\n"
    for edu in education:
        linkedin_profile_text += f"- {edu.get('degree', '')} from {edu.get('institution', '')} ({edu.get('year', '')})\n"
    
    linkedin_profile_text += "\nCertifications:\n"
    for cert in certifications:
        linkedin_profile_text += f"- {cert.get('name', '')} by {cert.get('issuer', '')} ({cert.get('year', '')})\n"
    
    linkedin_profile_text += "\nSkills:\n"
    for skill in skills:
        skill_name = skill.get("name", skill) if isinstance(skill, dict) else skill
        linkedin_profile_text += f"- {skill_name}\n"
    
    # Check if we have meaningful profile data
    if len(linkedin_profile_text.strip()) < 100:
        logger.warning(f"⚠️ [LinkedIn Candidate:{candidate_id}] Insufficient profile data, skipping LLM processing")
        return candidate
    
    logger.info(f"📄 [LinkedIn Candidate:{candidate_id}] Profile text length: {len(linkedin_profile_text)} characters")
    
    # Crisp the profile text for LLM extraction
    logger.info(f"📄 [LinkedIn Candidate:{candidate_id}] Crisping profile for LLM extraction...")
    crisped_profile_text = await crisp_resume_with_ai(linkedin_profile_text, max_length=12000)
    logger.info(f"📄 [LinkedIn Candidate:{candidate_id}] Using crisped profile ({len(crisped_profile_text)} chars) for LLM extraction")
    
    # Run LLM extraction
    logger.info(f"🧠 [LinkedIn Candidate:{candidate_id}] Calling LLM for enhanced info extraction...")
    enhanced_info_result = await extract_enhanced_info_with_llm(crisped_profile_text)
    
    # Log LLM results
    if enhanced_info_result.get("error"):
        logger.error(f"❌ [LinkedIn Candidate:{candidate_id}] LLM error: {enhanced_info_result.get('error')}")
        formatted_skills = []
    else:
        formatted_skills = _normalize_llm_skills(enhanced_info_result)
    
    # Extract job title with fallbacks
    extracted_job_title = (
        enhanced_info_result.get("job_title")
        or enhanced_info_result.get("current_title")
        or ((enhanced_info_result.get("company_experience") or [{}])[0].get("title") if isinstance(enhanced_info_result.get("company_experience"), list) and (enhanced_info_result.get("company_experience") or []) else None)
        or headline
    )
    
    # Validate LLM-extracted name
    llm_name = _clean_extracted_value(enhanced_info_result.get("candidate_name"))
    linkedin_name = _clean_extracted_value(candidate.get("name"))
    
    if llm_name:
        final_name = llm_name
    elif linkedin_name:
        final_name = linkedin_name
    else:
        final_name = None
    
    logger.info(f"👤 [LinkedIn Candidate:{candidate_id}] Name selection: LLM='{llm_name}', LinkedIn='{linkedin_name}', Final='{final_name}'")
    
    # Build enhanced info
    enhanced_info = {
        "candidate_name": final_name,
        "email": _clean_extracted_value(enhanced_info_result.get("email")) or _clean_extracted_value(candidate.get("email")),
        "phone": _clean_extracted_value(enhanced_info_result.get("phone")) or _clean_extracted_value(candidate.get("phone")),
        "job_title": _clean_extracted_value(extracted_job_title),
        "years_of_experience": enhanced_info_result.get("years_of_experience"),
        "current_location": _clean_extracted_value(enhanced_info_result.get("current_location")) or _clean_extracted_value(location),
        
        # Use LLM-extracted data with LinkedIn data as fallback
        "company_experience": enhanced_info_result.get("company_experience", []) or company_exp,
        "candidate_education": enhanced_info_result.get("candidate_education", []) or education,
        "candidate_certification": enhanced_info_result.get("candidate_certification", []) or certifications,
        "urls": _normalize_candidate_urls({
            "linkedin": profile_url,
            **(enhanced_info_result.get("urls") or {}),
        }),
        "structured_skills": formatted_skills or skills,
        
        # Metadata
        "source": "LinkedIn",
        "resume_extraction_status": "completed" if any([
            final_name,
            _clean_extracted_value(extracted_job_title),
            formatted_skills,
            enhanced_info_result.get("company_experience", []) or company_exp,
            enhanced_info_result.get("candidate_education", []) or education,
            _clean_extracted_value(enhanced_info_result.get("email")),
            _clean_extracted_value(enhanced_info_result.get("current_location")),
        ]) else "partial"
    }
    
    _log_extraction_snapshot(candidate_id, enhanced_info)
    
    # Save to candidate_enhanced_info table
    logger.info(f"💾 [LinkedIn Candidate:{candidate_id}] Saving to candidate_enhanced_info table...")
    try:
        save_candidate_enhanced_info(candidate_id, enhanced_info, linkedin_profile_text)
        logger.info(f"✅ [LinkedIn Candidate:{candidate_id}] Successfully saved to candidate_enhanced_info")
    except Exception as e:
        logger.error(f"❌ [LinkedIn Candidate:{candidate_id}] Failed to save to candidate_enhanced_info: {e}")
    
    logger.info("[LinkedInExtract] candidate_id=%s persisted=%s source=LinkedIn", candidate_id, "yes")
    
    # Return merged/enriched data for API response
    return {
        "candidate_id": candidate_id,
        "name": enhanced_info.get("candidate_name"),
        "current_title": enhanced_info.get("job_title"),
        "location": enhanced_info.get("current_location"),
        "years_experience": enhanced_info.get("years_of_experience"),
        "skills": formatted_skills,
        "company_experience": enhanced_info.get("company_experience", []),
        "education": enhanced_info.get("candidate_education", []),
        "certifications": enhanced_info.get("candidate_certification", []),
        "urls": enhanced_info.get("urls", {}),
        "raw": enhanced_info
    }
    
async def process_dice_candidate(candidate: Dict[str, Any]):
    """Process Dice candidate and save enhanced info to database."""
    candidate_id = candidate.get("candidate_id", candidate.get("id", "unknown"))
    candidate_name = candidate.get("name", "Unknown")
    
    logger.info(f"🔄 [Dice Candidate:{candidate_id}] Starting profile processing for {candidate_name}")
    
    # Build a pseudo-resume from Dice candidate data for LLM extraction
    headline = candidate.get("title", candidate.get("headline", ""))
    location = candidate.get("location", candidate.get("city", ""))
    # Dice candidates usually have resume_text already
    resume_text = candidate.get("resume_text", "")
    
    if not _has_real_resume_text(resume_text):
        # Construct summary from metadata if no resume
        summary = f"""
Name: {candidate_name}
Title: {headline}
Location: {location}
"""
        resume_text = summary
    
    # Check if we have meaningful data
    if len(resume_text.strip()) < 50:
        logger.warning(f"⚠️ [Dice Candidate:{candidate_id}] Insufficient profile data, skipping LLM processing")
        return candidate
    
    logger.info(f"📄 [Dice Candidate:{candidate_id}] Profile text length: {len(resume_text)} characters")
    
    # Crisp the profile text for LLM extraction
    logger.info(f"📄 [Dice Candidate:{candidate_id}] Crisping profile for LLM extraction...")
    crisped_text = await crisp_resume_with_ai(resume_text, max_length=12000)
    
    # Run LLM extraction
    logger.info(f"🧠 [Dice Candidate:{candidate_id}] Calling LLM for enhanced info extraction...")
    enhanced_info_result = await extract_enhanced_info_with_llm(crisped_text)
    
    # Log LLM results
    if enhanced_info_result.get("error"):
        logger.error(f"❌ [Dice Candidate:{candidate_id}] LLM error: {enhanced_info_result.get('error')}")
        formatted_skills = []
    else:
        formatted_skills = _normalize_llm_skills(enhanced_info_result)
    
    # Extract job title with fallbacks
    extracted_job_title = (
        enhanced_info_result.get("job_title")
        or enhanced_info_result.get("current_title")
        or headline
    )
    
    # Build enhanced info
    enhanced_info = {
        "candidate_name": _clean_extracted_value(enhanced_info_result.get("candidate_name")) or candidate_name,
        "email": _clean_extracted_value(enhanced_info_result.get("email")) or candidate.get("email"),
        "phone": _clean_extracted_value(enhanced_info_result.get("phone")) or candidate.get("phone"),
        "job_title": _clean_extracted_value(extracted_job_title),
        "years_of_experience": enhanced_info_result.get("years_of_experience"),
        "current_location": _clean_extracted_value(enhanced_info_result.get("current_location")) or location,
        
        "company_experience": enhanced_info_result.get("company_experience", []),
        "candidate_education": enhanced_info_result.get("candidate_education", []),
        "candidate_certification": enhanced_info_result.get("candidate_certification", []),
        "urls": _normalize_candidate_urls({
            **(enhanced_info_result.get("urls") or {}),
        }),
        "structured_skills": formatted_skills,
        
        # Metadata
        "source": "Dice",
        "resume_extraction_status": "completed" if any([
            formatted_skills,
            enhanced_info_result.get("company_experience", []),
            _clean_extracted_value(enhanced_info_result.get("email")),
        ]) else "partial"
    }
    
    _log_extraction_snapshot(candidate_id, enhanced_info)
    
    # Save to candidate_enhanced_info table
    try:
        save_candidate_enhanced_info(candidate_id, enhanced_info, resume_text)
    except Exception as e:
        logger.error(f"❌ [Dice Candidate:{candidate_id}] Failed to save: {e}")
    
    # Return merged data
    return {
        "candidate_id": candidate_id,
        "name": enhanced_info.get("candidate_name"),
        "current_title": enhanced_info.get("job_title"),
        "location": enhanced_info.get("current_location"),
        "years_experience": enhanced_info.get("years_of_experience"),
        "skills": formatted_skills,
        "company_experience": enhanced_info.get("company_experience", []),
        "education": enhanced_info.get("candidate_education", []),
        "certifications": enhanced_info.get("candidate_certification", []),
        "urls": enhanced_info.get("urls", {}),
        "raw": enhanced_info
    }

# --- Save Functions ---
def save_candidate_enhanced_info(candidate_id: str, enhanced_info: Dict[str, Any], resume_text: str):
    """Save or update candidate_enhanced_info table with enriched data."""
    try:
        engine = sqlalchemy.create_engine(DATABASE_URL)
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS candidate_enhanced_info (
                    id SERIAL PRIMARY KEY,
                    candidate_id TEXT NOT NULL UNIQUE,
                    candidate_name TEXT,
                    email TEXT,
                    phone TEXT,
                    job_title TEXT,
                    years_of_experience INT,
                    current_location TEXT,
                    key_skills JSONB DEFAULT '[]'::jsonb,
                    company_experience JSONB DEFAULT '[]'::jsonb,
                    candidate_education JSONB DEFAULT '[]'::jsonb,
                    candidate_certification JSONB DEFAULT '[]'::jsonb,
                    urls JSONB DEFAULT '{}'::jsonb,
                    resume_text TEXT,
                    resume_extraction_status TEXT DEFAULT 'pending',
                    source TEXT DEFAULT 'JobDiva',
                    extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP DEFAULT (CURRENT_TIMESTAMP + '30 days'::interval)
                )
            """))
            
            # Safe int parsing for years_experience
            raw_years = enhanced_info.get("years_of_experience")
            parsed_years = None
            if isinstance(raw_years, int) or isinstance(raw_years, float):
                parsed_years = int(raw_years)
            elif isinstance(raw_years, str):
                import re
                match = re.search(r'\d+', raw_years)
                if match:
                    parsed_years = int(match.group())
            
            # Extract data from enhanced_info
            company_exp = enhanced_info.get("company_experience", [])
            education = enhanced_info.get("candidate_education", [])
            certifications = enhanced_info.get("candidate_certification", [])
            urls = enhanced_info.get("urls", {})
            
            conn.execute(text("""
                INSERT INTO candidate_enhanced_info
                (candidate_id, candidate_name, email, phone, job_title, current_location, 
                 years_of_experience, key_skills, company_experience, candidate_education,
                 candidate_certification, urls, resume_text, 
                 resume_extraction_status, source, extracted_at)
                VALUES (:candidate_id, :candidate_name, :email, :phone, :job_title, :current_location, 
                        :years_of_experience, :key_skills, :company_experience, :candidate_education,
                        :candidate_certification, :urls, :resume_text,
                        :resume_extraction_status, :source, CURRENT_TIMESTAMP)
                ON CONFLICT (candidate_id) DO UPDATE SET
                    candidate_name = EXCLUDED.candidate_name,
                    email = EXCLUDED.email,
                    phone = EXCLUDED.phone,
                    job_title = EXCLUDED.job_title,
                    current_location = EXCLUDED.current_location,
                    years_of_experience = EXCLUDED.years_of_experience,
                    key_skills = EXCLUDED.key_skills,
                    company_experience = EXCLUDED.company_experience,
                    candidate_education = EXCLUDED.candidate_education,
                    candidate_certification = EXCLUDED.candidate_certification,
                    urls = EXCLUDED.urls,
                    resume_text = EXCLUDED.resume_text,
                    resume_extraction_status = EXCLUDED.resume_extraction_status,
                    source = EXCLUDED.source,
                    extracted_at = CURRENT_TIMESTAMP
            """), {
                "candidate_id": candidate_id,
                "candidate_name": enhanced_info.get("candidate_name"),
                "email": enhanced_info.get("email"),
                "phone": enhanced_info.get("phone"),
                "job_title": enhanced_info.get("job_title"),
                "current_location": enhanced_info.get("current_location"),
                "years_of_experience": parsed_years,
                "key_skills": json.dumps(enhanced_info.get("structured_skills", [])),
                "company_experience": json.dumps(company_exp),
                "candidate_education": json.dumps(education),
                "candidate_certification": json.dumps(certifications),
                "urls": json.dumps(urls),
                "resume_text": resume_text,
                "resume_extraction_status": enhanced_info.get("resume_extraction_status", "pending"),
                "source": enhanced_info.get("source", "JobDiva")
            })
            conn.commit()
            
            # --- Propagate strictly to the newly normalized relations ---
            try:
                candidate_profiles_db.upsert_candidate(
                    jobdiva_id="", # We may not have jobdiva_id locally here but it operates as intended
                    candidate=enhanced_info,
                    source=enhanced_info.get("source", "JobDiva")
                )
            except Exception as e_norm:
                logger.error(f"❌ Normalized persistence failed for {candidate_id}: {e_norm}")
    except Exception as e:
        print(f"Error saving candidate_enhanced_info for {candidate_id}: {e}")

def save_sourced_candidate(candidate: Dict[str, Any], enhanced_info: Dict[str, Any]):
    """Update sourced_candidates table with enriched LLM-only candidate info."""
    try:
        engine = sqlalchemy.create_engine(DATABASE_URL)
        with engine.connect() as conn:
            sourced_payload = {
                "candidate_name": enhanced_info.get("candidate_name") or candidate.get("name"),
                "email": enhanced_info.get("email") or candidate.get("email"),
                "phone": enhanced_info.get("phone") or candidate.get("phone"),
                "job_title": enhanced_info.get("job_title") or candidate.get("title") or candidate.get("headline"),
                "years_of_experience": enhanced_info.get("years_of_experience"),
                "current_location": enhanced_info.get("current_location") or candidate.get("location"),
                "skills": enhanced_info.get("structured_skills", []),
                "company_experience": enhanced_info.get("company_experience", []),
                "candidate_education": enhanced_info.get("candidate_education", []),
                "candidate_certification": enhanced_info.get("candidate_certification", []),
                "urls": enhanced_info.get("urls", {}),
                "resume_text": candidate.get("resume_text", ""),
                "resume_extraction_status": enhanced_info.get("resume_extraction_status", "pending"),
                "source": enhanced_info.get("source", candidate.get("source", "JobDiva"))
            }

            conn.execute(text("""
                UPDATE sourced_candidates SET
                    name = COALESCE(:name, name),
                    email = COALESCE(:email, email),
                    phone = COALESCE(:phone, phone),
                    headline = COALESCE(:headline, headline),
                    location = COALESCE(:location, location),
                    resume_text = COALESCE(:resume_text, resume_text),
                    data = :data,
                    updated_at = CURRENT_TIMESTAMP
                WHERE jobdiva_id = :jobdiva_id AND candidate_id = :candidate_id AND source = :source
            """), {
                "name": sourced_payload["candidate_name"],
                "email": sourced_payload["email"],
                "phone": sourced_payload["phone"],
                "headline": sourced_payload["job_title"],
                "location": sourced_payload["current_location"],
                "resume_text": sourced_payload["resume_text"],
                "data": json.dumps(sourced_payload),
                "jobdiva_id": candidate.get("jobdiva_id"),
                "candidate_id": candidate["candidate_id"],
                "source": candidate.get("source", "JobDiva")
            })
            conn.commit()
    except Exception as e:
        print(f"Error updating sourced_candidates for {candidate['candidate_id']}: {e}")

# Implement save_candidate_enhanced_info and save_sourced_candidate as needed
