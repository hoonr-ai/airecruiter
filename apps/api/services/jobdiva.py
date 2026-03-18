import logging
import re
import time
import os
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from html import unescape
import sqlalchemy
from sqlalchemy import text

# TEMPORARY DEBUG LOGGER
debug_log_path = "/tmp/debug_sync.log"
def debug_log(msg):
    try:
        with open(debug_log_path, "a") as f:
            f.write(f"[{time.ctime()}] {msg}\n")
    except:
        pass

logger = logging.getLogger(__name__)

def readable_ist_now() -> str:
    """Returns current IST time in readable format: 2026-02-24 16:25:59 IST"""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")

def get_field(data: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
    """
    Safely extract a value from a dictionary by checking multiple potential keys
    case-insensitively and ignoring non-alphanumeric characters.
    """
    if not isinstance(data, dict):
        return default
      
    def normalize(s):
        return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())
      
    normalized_data = {normalize(k): v for k, v in data.items()}
  
    for key in keys:
        norm_key = normalize(key)
        if norm_key in normalized_data:
            return normalized_data[norm_key]
          
    return default

def format_job_description(raw_desc: str) -> str:
    """
    Format raw job description with minimal changes - keep exact text, just clean HTML.
    """
    if not raw_desc or not raw_desc.strip():
        return "No job description available."
  
    desc = unescape(raw_desc)
    desc = re.sub(r'<br\s*/?>', '\n', desc)
    desc = re.sub(r'<p>', '\n', desc)
    desc = re.sub(r'</p>', '\n', desc)
    desc = re.sub(r'<div[^>]*>', '\n', desc)
    desc = re.sub(r'</div>', '\n', desc)
    desc = re.sub(r'<[^>]*>', '', desc)
  
    desc = re.sub(r'\n\s*\n\s*\n+', '\n\n', desc)
    desc = re.sub(r'[ \t]+', ' ', desc)
    desc = desc.strip()
  
    return desc

class JobDivaService:
    def __init__(self):
        self.api_url = os.getenv("JOBDIVA_API_URL", "https://api.jobdiva.com")
        self.client_id = os.getenv("JOBDIVA_CLIENT_ID", "mock-client")
        self.username = os.getenv("JOBDIVA_USERNAME", "mock-user")
        self.password = os.getenv("JOBDIVA_PASSWORD", "mock-pass")
        self.cached_token = None
        self.token_expiry = 0
        
        self.db_url = os.getenv("DATABASE_URL")
        if self.db_url and self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://")
        
        self.engine = None
        if self.db_url:
            try:
                self.engine = sqlalchemy.create_engine(self.db_url)
            except Exception as e:
                logger.error(f"Failed to create JobDiva DB engine: {e}")

    async def authenticate(self) -> str:
        """Authenticate with JobDiva and return JWT token."""
        if self.cached_token and time.time() < self.token_expiry:
            return self.cached_token
        
        if not self.client_id or self.client_id == "mock-client" or not self.username:
            logger.error(f"JobDiva Credentials not configured. client_id={self.client_id}, username={self.username}")
            return None

        auth_url = f"{self.api_url}/api/authenticate"
        params = {
            "clientid": self.client_id,
            "username": self.username,
            "password": self.password
        }

        try:
            async with httpx.AsyncClient() as client:
                debug_log(f"Authenticating to JobDiva: {self.username} at {auth_url}")
                response = await client.get(auth_url, params=params)
                
                if response.status_code != 200:
                    debug_log(f"JobDiva Auth Failed: {response.status_code} - {response.text}")
                    return None
                
                token = response.text.replace("\"", "").strip()
                if len(token) < 10:
                    logger.error(f"JobDiva Auth returned invalid token: {token}")
                    return None

                self.cached_token = token
                self.token_expiry = time.time() + (23 * 3600)
                debug_log("JobDiva Auth Successful")
                return token

        except Exception as e:
            logger.error(f"JobDiva Auth Exception: {repr(e)}")
            return None

    async def search_candidates(self, skills: List[Any], location: str, page: int = 1, limit: int = 100) -> List[Dict[str, Any]]:
        """Search for candidates based on skills and location."""
        from models import Skill
        token = await self.authenticate()
        if not token: return []

        jd_results = []
        must_haves = []
        flexible = []
        
        for s in skills:
            s_name = ""
            s_prio = "Must Have"
            s_years = 0
            
            if isinstance(s, dict):
                s_name = s.get("name", "")
                s_prio = s.get("priority", "Must Have")
                s_years = s.get("years_experience", 0)
            elif hasattr(s, "name"):
                s_name = s.name
                s_prio = s.priority
                s_years = getattr(s, "years_experience", 0)
            else:
                s_name = str(s)

            if not s_name: continue
            
            term = f'("{s_name.upper()}" recent over {s_years} years)' if int(s_years or 0) > 0 else f'"{s_name.upper()}"'
            if "must" in s_prio.lower(): must_haves.append(term)
            else: flexible.append(term)

        criteria_parts = []
        if must_haves: criteria_parts.append(f"(" + " AND ".join(must_haves) + ")")
        elif flexible: criteria_parts.append(f"(" + " OR ".join(flexible) + ")")
        if location: criteria_parts.append(f'"{location.strip()}"')
            
        search_value = " AND ".join(criteria_parts) if criteria_parts else "*"
        url = f"{self.api_url}/apiv2/jobdiva/TalentSearch"
        headers = {"Authorization": f"Bearer {token}"}
        payload = {"skills": [search_value], "pageNumber": page, "pageSize": limit}
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    candidates = data if isinstance(data, list) else data.get("candidates", [])
                    for c in candidates:
                        jd_results.append({
                            "id": str(get_field(c, ["id", "candidateId"])),
                            "firstName": get_field(c, ["firstName"]) or "Unknown",
                            "lastName": get_field(c, ["lastName"]) or "Candidate",
                            "email": get_field(c, ["email"]) or "",
                            "city": get_field(c, ["city"]) or "",
                            "state": get_field(c, ["state"]) or "",
                            "title": get_field(c, ["title"]) or "",
                            "source": "JobDiva",
                            "match_score": 0
                        })
        except Exception as e:
            logger.error(f"Candidate Search Error: {e}")

        return jd_results

    async def get_job_by_id(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a specific job by ID from JobDiva, including AI UDFs."""
        logger.info(f"Fetching Job ID: {job_id}")
        token = await self.authenticate()
        if not token: return None

        url = f"{self.api_url}/apiv2/jobdiva/SearchJob"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        is_ref = "-" in job_id
        if is_ref:
            payload = {"jobdivaref": job_id, "maxReturned": 1}
        else:
            safe_id = "".join(filter(str.isdigit, job_id))
            if not safe_id: return None
            payload = {"jobOrderId": int(safe_id), "maxReturned": 1}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code != 200: return None
                data = response.json()
                jobs = data if isinstance(data, list) else data.get("data", [])
                if not jobs: return None
                j = jobs[0]
            
                # Strict Matching: JobDiva sometimes returns arbitrary jobs for invalid inputs like '1'
                j_id = str(get_field(j, ["id", "jobId"]) or "")
                j_ref = str(get_field(j, ["reference #", "jobdivaref", "ref"]) or "")
                
                if is_ref:
                    if job_id.lower() != j_ref.lower():
                        logger.warning(f"Bogus JobDiva response: requested ref {job_id}, got ref {j_ref}")
                        return None
                else:
                    if safe_id != j_id:
                        logger.warning(f"Bogus JobDiva response: requested ID {safe_id}, got ID {j_id}")
                        return None
                
                u_fields = j.get("user fields", {}) or {}
                ai_description = None
                job_notes = None
                for k, v in u_fields.items():
                    if "AI Job Description" in k: ai_description = v
                    if "Job Notes" in k or k == "231": job_notes = v

                customer_name = str(get_field(j, ["customer", "company"]) or "").title() or "Unknown Customer"
                description = format_job_description(get_field(j, ["job description", "description"]) or "")

                # Restore full-length UDFs from local DB if JobDiva truncated them
                local_data = self.get_locally_monitored_job(job_id)
                if local_data:
                    local_ai = local_data.get("ai_description")
                    if local_ai and len(str(local_ai)) > len(str(ai_description or "")):
                        ai_description = local_ai
                        logger.info(f"Restored full ai_description from local DB for {job_id}")
                    
                    local_notes = local_data.get("job_notes")
                    if local_notes and len(str(local_notes)) > len(str(job_notes or "")):
                        job_notes = local_notes
                        logger.info(f"Restored full job_notes from local DB for {job_id}")

                return {
                    "id": get_field(j, ["id", "jobId"]),
                    "title": get_field(j, ["job title", "title"]),
                    "description": description,
                    "ai_description": ai_description,
                    "job_notes": job_notes,
                    "customer_name": customer_name,
                    "job_status": get_field(j, ["job status", "status"]) or "OPEN",
                    "city": get_field(j, ["city", "jobCity"]),
                    "state": get_field(j, ["state", "jobState"]),
                    "zip": get_field(j, ["zip", "postalCode"]),
                    "start_date": get_field(j, ["start date", "startDate", "available"]),
                    "location_type": get_field(j, ["location type", "location_type", "position type", "work type", "assignment type", "jobType"]),
                    "work_authorization": get_field(j, ["work authorization", "visa", "legal status"])
                }
        except Exception as e:
            logger.error(f"SearchJob Error: {e}")
            return None

    async def get_candidate_resume(self, candidate_id: str) -> Optional[str]:
        """Fetches resume text with cascading fallback."""
        token = await self.authenticate()
        if not token: return None
        headers = {"Authorization": f"Bearer {token}"}
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res_url = f"{self.api_url}/apiv2/bi/CandidateResumesDetail"
                resp = await client.get(res_url, params={"candidateId": candidate_id}, headers=headers)
                if resp.status_code == 200:
                    recs = resp.json()
                    if isinstance(recs, dict): recs = recs.get("data", [])
                    if recs:
                        rid = recs[0].get("RESUMEID")
                        det_url = f"{self.api_url}/apiv2/bi/ResumeDetail"
                        det_resp = await client.get(det_url, params={"resumeId": rid}, headers=headers)
                        if det_resp.status_code == 200:
                            data = det_resp.json()
                            if isinstance(data, dict): data = data.get("data", [{}])[0]
                            text = get_field(data, ["PLAINTEXT", "text"])
                            if text: return unescape(text)
        except Exception as e:
            logger.error(f"Resume Fetch Error: {e}")
        return "Resume content unavailable."

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Efficient job status check."""
        job = await self.get_job_by_id(job_id)
        if not job: return {"job_id": job_id, "status": "NOT_FOUND"}
        return {
            "job_id": job_id,
            "status": job.get("job_status", "OPEN"),
            "customer": job.get("customer_name", "Unknown"),
            "title": job.get("title", ""),
            "synced_at": readable_ist_now()
        }

    async def get_multiple_jobs_status(self, job_ids: List[str]) -> List[Dict[str, Any]]:
        """Batch fetch status for multiple jobs."""
        results = []
        for job_id in job_ids:
            results.append(await self.get_job_status(job_id))
        return results

    async def update_job_user_fields(self, job_id: str, fields: list) -> bool:
        """Update JobDiva UDFs."""
        token = await self.authenticate()
        if not token: return False
        
        internal_id = job_id
        if "-" in str(job_id):
            job_data = await self.get_job_by_id(job_id)
            if job_data: internal_id = job_data["id"]
            else: return False
            
        url = f"{self.api_url}/apiv2/jobdiva/updateJob"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        normalized_fields = [
            {"userfieldId": f.get("userfieldId"), "userfieldValue": f.get("userfieldValue") or f.get("value", "")}
            for f in fields
        ]
        payload = {"jobid": int(internal_id), "Userfields": normalized_fields}
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                debug_log(f"Pushing UDFs to {job_id}: {payload}")
                response = await client.post(url, json=payload, headers=headers)
                return response.status_code == 200
        except Exception: return False

    def monitor_job_locally(self, job_id: str, data: dict) -> bool:
        if not self.engine:
            logger.error("Database engine not initialized for monitoring")
            return False
            
        try:
            with self.engine.connect() as conn:
                # Check if job exists
                res = conn.execute(text("SELECT 1 FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
                exists = res.fetchone()
                
                if exists:
                    # Update
                    update_parts = []
                    params = {"job_id": job_id}
                    for k, v in data.items():
                        if k in ["status", "customer", "title", "recruiter_email", "work_authorization", "ai_description", "job_notes", "added_at"]:
                            update_parts.append(f"{k} = :{k}")
                            params[k] = v
                    
                    update_parts.append("last_updated = :last_updated")
                    params["last_updated"] = readable_ist_now()
                    
                    query = f"UPDATE monitored_jobs SET {', '.join(update_parts)} WHERE job_id = :job_id"
                    conn.execute(text(query), params)
                else:
                    # Insert
                    params = {
                        "job_id": job_id,
                        "status": data.get("status"),
                        "customer": data.get("customer"),
                        "title": data.get("title"),
                        "recruiter_email": data.get("recruiter_email"),
                        "work_authorization": data.get("work_authorization"),
                        "ai_description": data.get("ai_description"),
                        "job_notes": data.get("job_notes"),
                        "added_at": data.get("added_at") or readable_ist_now(),
                        "last_updated": readable_ist_now()
                    }
                    conn.execute(text("""
                        INSERT INTO monitored_jobs (job_id, status, customer, title, recruiter_email, work_authorization, ai_description, job_notes, added_at, last_updated)
                        VALUES (:job_id, :status, :customer, :title, :recruiter_email, :work_authorization, :ai_description, :job_notes, :added_at, :last_updated)
                    """), params)
                
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error monitoring job locally in DB: {e}")
            return False

    def get_locally_monitored_job(self, job_id: str) -> dict:
        if not self.engine:
             return {}
        try:
            with self.engine.connect() as conn:
                res = conn.execute(text("SELECT * FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
                row = res.fetchone()
                if row:
                    # Convert row to dict
                    return dict(row._mapping)
        except Exception as e:
            logger.error(f"Error fetching locally monitored job from DB: {e}")
        return {}
        
    def get_all_monitored_jobs(self) -> dict:
        if not self.engine:
            return {"jobs": {}}
        try:
            with self.engine.connect() as conn:
                res = conn.execute(text("SELECT * FROM monitored_jobs"))
                rows = res.fetchall()
                jobs = {}
                for row in rows:
                    j_dict = dict(row._mapping)
                    jid = j_dict.pop("job_id")
                    jobs[jid] = j_dict
                return {"jobs": jobs}
        except Exception as e:
            logger.error(f"Error fetching all monitored jobs from DB: {e}")
            return {"jobs": {}}

jobdiva_service = JobDivaService()
