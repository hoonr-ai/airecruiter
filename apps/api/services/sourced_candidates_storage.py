
import json
import time
import asyncio
import logging
from typing import List, Optional, Dict, Any
import sqlalchemy
from sqlalchemy import text
from core.config import (
    DATABASE_URL, SUPABASE_DB_URL,
    AZURE_AI_PROJECT_ENDPOINT, AZURE_AI_AGENT_NAME, AZURE_OPENAI_API_KEY,
    OPENAI_API_KEY, OPENAI_MODEL
)
import httpx
from models import SourcedCandidate

logger = logging.getLogger(__name__)

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


# Concurrency controls to prevent 429 Too Many Requests
# NOTE: Azure Agent calls now go through AzureAgentService which has its own semaphore
# This semaphore is kept for LLM calls only
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

async def extract_skills_with_azure(resume_text: str) -> Dict[str, Any]:
    """Call Azure Agent API to extract skills from resume text using AzureAgentService.
    
    This function now delegates to AzureAgentService which handles:
    - Global semaphore for max 1 concurrent call
    - Exponential backoff retry on 429 errors
    - Consistent error handling
    """
    logger.info(f"🤖 [Azure Agent] Starting skill extraction for resume ({len(resume_text)} chars)")
    logger.info(f"🤖 [Azure Agent] Using endpoint: {AZURE_AI_PROJECT_ENDPOINT}")
    logger.info(f"🤖 [Azure Agent] Using agent: {AZURE_AI_AGENT_NAME}")
    
    try:
        # Import and use AzureAgentService for consistent rate limiting
        from services.azure_agent_service import AzureAgentService
        
        agent = AzureAgentService(
            project_endpoint=AZURE_AI_PROJECT_ENDPOINT,
            api_key=AZURE_OPENAI_API_KEY,
            agent_name=AZURE_AI_AGENT_NAME,
        )
        
        # Truncate resume to 15K chars to speed up processing
        truncated_resume = resume_text[:15000]
        
        # Call AzureAgentService which handles rate limiting and retries
        result = await agent.extract_roles_and_skills(truncated_resume, max_retries=5)
        
        # Log the response summary
        skill_count = len(result.get("job_skills", []) or result.get("skills", []))
        role_count = len(result.get("job_roles", []) or result.get("roles", []))
        
        logger.info(f"📊 [Azure Agent] Response Summary:")
        logger.info(f"   - Skills extracted: {skill_count}")
        logger.info(f"   - Roles extracted: {role_count}")
        if skill_count > 0:
            skills_list = result.get("job_skills", []) or result.get("skills", [])
            skill_names = [s.get("skill_mapped", s.get("extracted_skill", "N/A")) for s in skills_list[:5]]
            logger.info(f"   - Top skills: {', '.join(skill_names)}")
        if role_count > 0:
            roles_list = result.get("job_roles", [])
            role_names = [r.get("ROLE_K17000", r.get("extracted_title", "N/A")) for r in roles_list[:3]]
            logger.info(f"   - Top roles: {', '.join(role_names)}")
        
        logger.info(f"✅ [Azure Agent] Successfully extracted skills and roles")
        return result
                    
    except Exception as e:
        logger.error(f"❌ [Azure Agent] Error extracting skills: {e}")
        return {"error": str(e), "job_skills": [], "job_roles": []}


async def extract_enhanced_info_with_llm(resume_text: str) -> Dict[str, Any]:
    """Call OpenAI LLM to extract enhanced info from resume text."""
    logger.info(f"🧠 [LLM] Starting enhanced info extraction for resume ({len(resume_text)} chars)")
    logger.info(f"🧠 [LLM] Using model: {OPENAI_MODEL}")
    
    if not OPENAI_API_KEY or not OPENAI_MODEL:
        logger.error("❌ [LLM] OpenAI API key or model not configured")
        raise RuntimeError("OpenAI API key or model not configured.")
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }
    # Sanitize resume text to avoid JSON parsing issues
    # Remove control characters and limit length
    sanitized_resume = resume_text[:8000]
    # Remove null bytes and other control characters that could break JSON
    sanitized_resume = ''.join(char for char in sanitized_resume if ord(char) >= 32 or char in '\n\r\t')
    
    # NOTE: job_title is extracted by Azure Agent, not LLM
    prompt = (
        "Extract the following information from the resume text and return ONLY valid JSON matching this exact structure (no summary field):\n"
        "{\n"
        '  "candidate_name": "Full Name",\n'
        '  "email": "email@example.com",\n'
        '  "phone": "+1-xxx-xxx-xxxx",\n'
        '  "years_of_experience": 5,\n'
        '  "current_location": "City, State",\n'
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
        '    "portfolio": "https://..."\n'
        '  }\n'
        "}\n\n"
        "Instructions:\n"
        "1. Extract company experience with clear start/end dates (e.g., 'Jan 2020' to 'Dec 2023' or 'Present')\n"
        "2. List companies in reverse chronological order (most recent first)\n"
        "3. Include all certifications mentioned\n"
        "4. Extract LinkedIn/portfolio URLs if present\n"
        "5. Return ONLY the JSON object, no markdown, no explanations\n"
        "6. Ensure all strings are properly escaped and JSON is valid\n\n"
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
        "max_tokens": 800
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
                    logger.info(f"✅ [LLM] Received response from OpenAI API")
                    
                    # Parse the LLM's JSON output from the response
                    content = result["choices"][0]["message"]["content"]
                    logger.info(f"🧠 [LLM] Response content length: {len(content)} chars")
                    
                    try:
                        parsed = json.loads(content)
                        
                        # Log the LLM response summary matching table schema
                        # NOTE: job_title is NOT extracted by LLM - it comes from Azure Agent
                        logger.info(f"📊 [LLM] Response Summary:")
                        logger.info(f"   - Name: {parsed.get('candidate_name', 'N/A')}")
                        logger.info(f"   - Years of Experience: {parsed.get('years_of_experience', 'N/A')}")
                        logger.info(f"   - Location: {parsed.get('current_location', 'N/A')}")
                        logger.info(f"   - Email: {parsed.get('email', 'N/A')}")
                        logger.info(f"   - Phone: {parsed.get('phone', 'N/A')}")
                        
                        # Company experience
                        company_exp = parsed.get('company_experience', [])
                        if company_exp and isinstance(company_exp, list) and len(company_exp) > 0:
                            logger.info(f"   - Companies ({len(company_exp)}):")
                            for idx, exp in enumerate(company_exp[:5], 1):
                                company = exp.get('company', 'N/A')
                                title = exp.get('title', 'N/A')
                                start = exp.get('start_date', 'N/A')
                                end = exp.get('end_date', 'N/A')
                                logger.info(f"      {idx}. {company} | {title} | {start} - {end}")
                        
                        # Education
                        education = parsed.get('candidate_education', [])
                        if education and isinstance(education, list) and len(education) > 0:
                            logger.info(f"   - Education ({len(education)}):")
                            for idx, edu in enumerate(education[:3], 1):
                                degree = edu.get('degree', 'N/A')
                                inst = edu.get('institution', 'N/A')
                                year = edu.get('year', 'N/A')
                                logger.info(f"      {idx}. {degree} | {inst} | {year}")
                        else:
                            logger.info(f"   - Education: None found")
                        
                        # Certifications
                        certs = parsed.get('candidate_certification', [])
                        if certs and isinstance(certs, list) and len(certs) > 0:
                            logger.info(f"   - Certifications ({len(certs)}):")
                            for idx, cert in enumerate(certs[:5], 1):
                                name = cert.get('name', 'N/A')
                                issuer = cert.get('issuer', 'N/A')
                                year = cert.get('year', 'N/A')
                                logger.info(f"      {idx}. {name} | {issuer} | {year}")
                        else:
                            logger.info(f"   - Certifications: None found")
                        
                        # URLs
                        urls = parsed.get('urls', {})
                        if urls and isinstance(urls, dict):
                            url_list = [f"{k}: {v}" for k, v in urls.items() if v]
                            if url_list:
                                logger.info(f"   - URLs: {', '.join(url_list[:3])}")
                        
                        logger.info(f"✅ [LLM] Successfully parsed JSON response")
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
    if not original_resume_text:
        logger.warning(f"⚠️ [Candidate:{candidate_id}] No resume text found, skipping processing")
        return candidate
    
    logger.info(f"📄 [Candidate:{candidate_id}] Original resume text length: {len(original_resume_text)} characters")
    
    # Step 1: Crisp the resume for LLM extraction (preserves all important details, removes fluff)
    # This ensures complete extraction of all companies, education, certifications
    logger.info(f"📄 [Candidate:{candidate_id}] Crisping resume for extraction...")
    crisped_resume_text = await crisp_resume_with_ai(original_resume_text, max_length=7500)
    logger.info(f"📄 [Candidate:{candidate_id}] Using crisped resume ({len(crisped_resume_text)} chars) for extraction")
        
    # Step 2: Run extractions
    # - Azure Agent: Uses original resume (handles up to 15K)
    # - LLM: Uses crisped resume (complete data extraction, no truncation)
    logger.info(f"🤖 [Candidate:{candidate_id}] Calling Azure Agent for skill extraction...")
    skills_task = asyncio.create_task(extract_skills_with_azure(original_resume_text))
    
    logger.info(f"🧠 [Candidate:{candidate_id}] Calling LLM for enhanced info extraction (using crisped resume)...")
    enhanced_info_task = asyncio.create_task(extract_enhanced_info_with_llm(crisped_resume_text))
    
    logger.info(f"⏳ [Candidate:{candidate_id}] Waiting for Azure Agent and LLM responses...")
    skills_result, enhanced_info_result = await asyncio.gather(skills_task, enhanced_info_task)
    
    # Log Azure Agent results
    if skills_result.get("error"):
        logger.error(f"❌ [Candidate:{candidate_id}] Azure Agent error: {skills_result.get('error')}")
    else:
        raw_skills = skills_result.get("job_skills") or skills_result.get("skills") or []
        logger.info(f"✅ [Candidate:{candidate_id}] Azure Agent returned {len(raw_skills)} raw skills")

    # Log LLM results
    if enhanced_info_result.get("error"):
        logger.error(f"❌ [Candidate:{candidate_id}] LLM error: {enhanced_info_result.get('error')}")
    else:
        logger.info(f"✅ [Candidate:{candidate_id}] LLM returned enhanced info with keys: {list(enhanced_info_result.keys())}")

    # Format the skills specifically from Azure Agent
    raw_agent_skills = skills_result.get("job_skills") or skills_result.get("skills") or []
    formatted_skills = []
    
    logger.info(f"🔄 [Candidate:{candidate_id}] Formatting {len(raw_agent_skills)} raw skills...")
    
    # Try to use AzureAgentService formatting if available to get skill_mapped + similar_skills
    try:
        from services.azure_agent_service import AzureAgentService
        temp_agent = AzureAgentService(project_endpoint="", api_key="")
        rubric_skills = temp_agent.convert_to_rubric_skills(raw_agent_skills)
        
        for s in rubric_skills:
            formatted_skills.append({
                "skill": s.get("value"),
                "similar_skills": s.get("similar_skills", [])
            })
        logger.info(f"✅ [Candidate:{candidate_id}] Formatted {len(formatted_skills)} skills using AzureAgentService")
    except Exception as e:
        logger.warning(f"⚠️ [Candidate:{candidate_id}] Error formatting agent skills: {e}, using manual fallback")
        for s in raw_agent_skills:
            canonical = s.get("skill_mapped") or s.get("skill_k15000") or s.get("extracted_skill")
            if canonical:
                formatted_skills.append({
                    "skill": canonical,
                    "similar_skills": [v for k,v in s.items() if k.startswith("skill_k") and v != canonical]
                })
        logger.info(f"✅ [Candidate:{candidate_id}] Formatted {len(formatted_skills)} skills using manual fallback")

    # Extract job_title from Azure Agent's job_roles response
    # Use the first role's extracted_title as the primary job title
    job_roles = skills_result.get("job_roles", [])
    extracted_job_title = None
    if job_roles:
        first_role = job_roles[0]
        # Priority: extracted_title > ROLE_K17000 > first non-null ROLE_K*
        extracted_job_title = first_role.get("extracted_title")
        if not extracted_job_title:
            # Try to find the most specific role from hierarchy
            for col in ["ROLE_K17000", "ROLE_K10000", "ROLE_K5000", "ROLE_K1500", "ROLE_K1000", "ROLE_K500", "ROLE_K150", "ROLE_K50", "ROLE_K10"]:
                val = first_role.get(col)
                if val and str(val).upper() not in ["GUARDRAIL", "GUARDRAILS", "NULL", "NONE", "EMPTY"]:
                    extracted_job_title = val
                    break
        logger.info(f"👔 [Candidate:{candidate_id}] Extracted job title from Azure Agent: {extracted_job_title}")
    else:
        logger.warning(f"⚠️ [Candidate:{candidate_id}] No job_roles found in Azure Agent response")

    # Merge results - use LLM response for personal info, Azure Agent for job_title
    # Validate LLM-extracted name - fall back to JobDiva name if LLM returns "Not Provided" or empty
    llm_name = enhanced_info_result.get("candidate_name", "").strip()
    jobdiva_name = candidate.get("name", "").strip()
    
    # Use LLM name only if it's valid (not empty and not "Not Provided")
    if llm_name and llm_name.lower() not in ["not provided", "n/a", "unknown", ""]:
        final_name = llm_name
    elif jobdiva_name and jobdiva_name.lower() not in ["professional candidate", ""]:
        final_name = jobdiva_name
    else:
        final_name = "Professional Candidate"
    
    logger.info(f"👤 [Candidate:{candidate_id}] Name selection: LLM='{llm_name}', JobDiva='{jobdiva_name}', Final='{final_name}'")
    
    enhanced_info = {
        # Use LLM extracted values directly (except job_title which comes from Azure Agent)
        # Name: prefer LLM extraction, but fall back to JobDiva if LLM returns "Not Provided"
        "candidate_name": final_name,
        "email": enhanced_info_result.get("email") or candidate.get("email"),
        "phone": enhanced_info_result.get("phone") or candidate.get("phone"),
        "job_title": extracted_job_title,  # From Azure Agent, NOT LLM
        "years_of_experience": enhanced_info_result.get("years_of_experience"),
        "current_location": enhanced_info_result.get("current_location") or candidate.get("location"),
        
        # Structured data from LLM
        "company_experience": enhanced_info_result.get("company_experience", []),
        "candidate_education": enhanced_info_result.get("candidate_education", []),
        "candidate_certification": enhanced_info_result.get("candidate_certification", []),
        "urls": enhanced_info_result.get("urls", {}),
        
        # Skills and Roles from Azure Agent
        "structured_skills": formatted_skills,
        "raw_agent_roles": job_roles,
        "agent_raw": skills_result,
        
        # Metadata
        "source": candidate.get("source", "JobDiva"),
        "resume_extraction_status": "completed" if formatted_skills else "partial"
    }

    # Save to candidate_enhanced_info (using ORIGINAL resume, not crisped)
    logger.info(f"💾 [Candidate:{candidate_id}] Saving to candidate_enhanced_info table...")
    try:
        save_candidate_enhanced_info(candidate["candidate_id"], enhanced_info, original_resume_text)
        logger.info(f"✅ [Candidate:{candidate_id}] Successfully saved to candidate_enhanced_info")
    except Exception as e:
        logger.error(f"❌ [Candidate:{candidate_id}] Failed to save to candidate_enhanced_info: {e}")

    # Use enhanced info to populate sourced_candidates
    logger.info(f"💾 [Candidate:{candidate_id}] Updating sourced_candidates table...")
    try:
        save_sourced_candidate(candidate, enhanced_info)
        logger.info(f"✅ [Candidate:{candidate_id}] Successfully updated sourced_candidates")
    except Exception as e:
        logger.error(f"❌ [Candidate:{candidate_id}] Failed to update sourced_candidates: {e}")

    logger.info(f"✅ [Candidate:{candidate_id}] Resume processing completed. Extracted {len(formatted_skills)} skills")
    
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
            raw_years = enhanced_info.get("years_experience")
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
                "years_of_experience": enhanced_info.get("years_of_experience"),
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
    except Exception as e:
        print(f"Error saving candidate_enhanced_info for {candidate_id}: {e}")

def save_sourced_candidate(candidate: Dict[str, Any], enhanced_info: Dict[str, Any]):
    """Update sourced_candidates table with enriched info."""
    try:
        engine = sqlalchemy.create_engine(DATABASE_URL)
        with engine.connect() as conn:
            # Only update fields that are present in the table
            conn.execute(text("""
                UPDATE sourced_candidates SET
                    data = :data,
                    updated_at = CURRENT_TIMESTAMP
                WHERE candidate_id = :candidate_id
            """), {
                "data": json.dumps(enhanced_info),
                "candidate_id": candidate["candidate_id"]
            })
            conn.commit()
    except Exception as e:
        print(f"Error updating sourced_candidates for {candidate['candidate_id']}: {e}")

# Implement save_candidate_enhanced_info and save_sourced_candidate as needed
