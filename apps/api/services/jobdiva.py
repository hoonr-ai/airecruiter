import logging
import re
import time
import json
import httpx
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from html import unescape
import sqlalchemy
from sqlalchemy import text
from core import (
    JOBDIVA_API_URL, JOBDIVA_CLIENT_ID, JOBDIVA_USERNAME, 
    JOBDIVA_PASSWORD, DATABASE_URL, DEBUG_LOG_PATH
)

logger = logging.getLogger(__name__)

# LLM-only candidate enrichment is active for sourcing.

# TEMPORARY DEBUG LOGGER
def debug_log(msg):
    if not DEBUG_LOG_PATH:
        return
    try:
        with open(DEBUG_LOG_PATH, "a") as f:
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
    Enhanced to filter out unwanted values like "Direct Placement" from location fields.
    """
    if not isinstance(data, dict):
        return default
      
    def normalize(s):
        return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())
      
    normalized_data = {normalize(k): v for k, v in data.items()}
  
    for key in keys:
        norm_key = normalize(key)
        if norm_key in normalized_data:
            val = normalized_data[norm_key]
            # Handle JobDiva's nested date/time objects
            if isinstance(val, dict):
                for subkey in ["dateTime", "date", "value", "$"]:
                    if subkey in val:
                        val = val[subkey]
                        break
            
            # Filter out employment-related values from location fields
            if isinstance(val, str) and _is_location_key(key):
                val_lower = val.lower().strip()
                # Don't return employment types as location data
                employment_indicators = [
                    "direct placement", "contract", "full-time", "part-time", 
                    "w2", "1099", "c2c", "corp to corp", "open", "pending",
                    "temporary", "permanent", "temp to perm", "fulltime", "parttime",
                    "consultant", "consulting", "employee", "contractor"
                ]
                if any(indicator in val_lower for indicator in employment_indicators):
                    continue
            
            return val
          
    return default

def _is_location_key(key: str) -> bool:
    """Check if a key represents a location-related field"""
    location_keywords = [
        "city", "state", "zip", "location", "address", "province", 
        "postal", "worksite", "jobcity", "jobstate", "locationcity", 
        "locationstate", "worksitecity", "worksitestate"
    ]
    key_lower = key.lower()
    return any(keyword in key_lower for keyword in location_keywords)

def _clean_location_field(value: Any) -> str:
    """Clean location field values to remove employment type contamination"""
    if not value:
        return ""
    
    val_str = str(value).strip()
    if not val_str:
        return ""
    
    val_lower = val_str.lower()
    
    # Don't return employment-related values as location
    employment_indicators = [
        "direct placement", "contract", "full-time", "part-time", 
        "w2", "1099", "c2c", "corp to corp", "open", "pending",
        "temporary", "permanent", "temp to perm", "fulltime", "parttime",
        "consultant", "consulting", "employee", "contractor"
    ]
    
    if any(indicator in val_lower for indicator in employment_indicators):
        return ""
    
    return val_str

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

def extract_pay_rate_from_text(description: str) -> str:
    """Extract pay rate from job description text as fallback when structured fields are missing"""
    if not description:
        return ""
    
    # Patterns to match various pay rate formats
    pay_patterns = [
        # "Pay Range: $25 - $36/hour" or "Pay Rate: $25–$26 per hour"
        r'[Pp]ay\s+[Rr]ange[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*(?:per\s+)?hours?',
        r'[Pp]ay\s+[Rr]ate[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*(?:per\s+)?hours?',
        r'[Ss]alary[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*(?:per\s+)?hours?',
        r'[Cc]ompensation[^$]*\$?(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*[/]?\s*hours?',
        # "$25 - $36/hour" or "$50-$75 per hour"
        r'\$(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*/?\s*(?:per\s+)?hours?',
        r'\$(\d+(?:[\.,]\d+)?)\s*[-–—]\s*(\d+(?:[\.,]\d+)?)\s*(?:per\s+)?hours?',
        # "$25–$36/hr" 
        r'\$(\d+(?:[\.,]\d+)?)\s*[-–—]\s*\$?(\d+(?:[\.,]\d+)?)\s*/?\s*hrs?',
    ]
    
    for pattern in pay_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            if len(match.groups()) == 2:
                min_pay = match.group(1).replace(',', '')
                max_pay = match.group(2).replace(',', '')
                return f"${min_pay} - ${max_pay}/hour"
            elif len(match.groups()) == 1:
                return f"${match.group(1)}/hour"
    
    # Single rate patterns
    single_patterns = [
        r'[Pp]ay\s+[Rr]ate[:\s]*\$?(\d+(?:[\.,]\d+)?)\s*/?\s*(?:per\s+)?hrs?',
        r'\$(\d+(?:[\.,]\d+)?)\s*/\s*(?:per\s+)?hrs?',
        r'\$(\d+(?:[\.,]\d+)?)\s*/?\s*(?:per\s+)?hours?'
    ]
    
    for pattern in single_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return f"${match.group(1)}/hour"
    
    return ""

def get_fallback_posted_date() -> str:
    """Get a reasonable fallback posted date when no date info is available from JobDiva"""
    from datetime import datetime
    # Use today's date as fallback - jobs are typically posted recently
    return datetime.now().strftime("%b %d, %Y")

def extract_posted_date_from_text(description: str) -> str:
    """Extract posted date from job description text as fallback when structured fields are missing"""
    if not description:
        return ""
    
    # Look for various date patterns
    date_patterns = [
        r'[Pp]osted[:\s]*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})',
        r'[Dd]ate[:\s]*(\w+\s+\d{1,2},?\s+\d{4})',
        r'[Ii]ssued[:\s]*(\w+\s+\d{1,2},?\s+\d{4})',
        r'[Cc]reated[:\s]*(\w+\s+\d{1,2},?\s+\d{4})',
        r'Job\s+ID[:\s]*\d+.*?[Pp]osted[:\s]*(\w+\s+\d{1,2},?\s+\d{4})'
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, description, re.IGNORECASE)
        if match:
            return match.group(1)
    





def calculate_date_duration(start_date_str: str, end_date_str: str) -> str:
    """Calculate human-readable duration between two date strings of format '%b %d, %Y'."""
    if not start_date_str or not end_date_str: 
        return ""
    try:
        from datetime import datetime
        start_dt = datetime.strptime(start_date_str, "%b %d, %Y")
        end_dt = datetime.strptime(end_date_str, "%b %d, %Y")
        
        if end_dt < start_dt:
            return ""
            
        total_days = (end_dt - start_dt).days + 1 # Inclusive
        if total_days <= 0:
            return ""
            
        years = total_days // 365
        rem_days = total_days % 365
        
        months = int(rem_days / 30.436875)
        days_diff = round(rem_days - (months * 30.436875))
            
        parts = []
        if years > 0:
            parts.append(f"{years} year" if years == 1 else f"{years} years")
        if months > 0:
            parts.append(f"{months} month" if months == 1 else f"{months} months")
        if days_diff > 0:
            parts.append(f"{days_diff} day" if days_diff == 1 else f"{days_diff} days")
            
        return " ".join(parts) if parts else "0 days"
    except Exception:
        return ""

def normalize_jobdiva_date(date_val: Any) -> str:
    """
    Format JobDiva date/timestamp into a readable YYYY-MM-DD format.
    Handles numeric timestamps and ISO date strings.
    Fixed to handle 2-digit years correctly relative to current year.
    Added validation to skip invalid date formats.
    """
    if not date_val:
        return ""
    
    # Handle numeric timestamp (milliseconds)
    if isinstance(date_val, (int, float)) or (isinstance(date_val, str) and date_val.isdigit()):
        try:
            ts = int(date_val)
            if ts > 10**11: # Likely milliseconds
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%b %d, %Y")
        except:
            pass

    # Handle string date formats
    date_str = str(date_val).strip()
    if not date_str:
        return ""
    
    # VALIDATION: Skip obviously invalid date formats like "2024/25"
    # Check for invalid patterns that would cause parsing errors
    if re.match(r'^\d{4}/\d{2}$', date_str):  # Pattern like "2024/25"
        return ""  # Skip this invalid format
    
    # Handle 2-digit year patterns first with smart year interpretation
    current_year = datetime.now().year
    
    two_digit_patterns = [
        "%m/%d/%y %I:%M %p",  # "02/24/26 9:52 AM"
        "%m/%d/%y",           # "02/24/26"  
    ]
    
    for pattern in two_digit_patterns:
        try:
            dt = datetime.strptime(date_str, pattern)
            # Python's %y interprets 00-68 as 2000-2068, 69-99 as 1969-1999
            # For our use case in 2026, this is already correct for recent dates
            # No adjustment needed since 26 -> 2026 is correct
            return dt.strftime("%b %d, %Y")
        except:
            continue
    
    # JobDiva 4-digit year patterns
    four_digit_patterns = [
        "%m/%d/%Y",           # "02/24/2024"
        "%m/%d/%Y %I:%M %p",  # "02/24/2024 9:52 AM"
    ]
    
    for pattern in four_digit_patterns:
        try:
            dt = datetime.strptime(date_str, pattern)
            return dt.strftime("%b %d, %Y")
        except:
            continue
    
    # Standard ISO and other formats    
    standard_patterns = [
        "%Y-%m-%d %H:%M:%S", 
        "%Y-%m-%dT%H:%M:%S", 
        "%Y-%m-%d"
    ]
    
    for pattern in standard_patterns:
        try:
            # We first parse then format to "Mar 18, 2026"
            dt = datetime.strptime(date_str[:19].replace('T', ' '), "%Y-%m-%d %H:%M:%S" if ' ' in date_str[:19] else "%Y-%m-%d")
            return dt.strftime("%b %d, %Y")
        except:
            try:
                # Fallback for common formats
                dt = datetime.fromisoformat(date_str.split('.')[0])
                return dt.strftime("%b %d, %Y")
            except:
                continue
                
    # Return empty string (not original input) if all parsing fails
    # This allows fallback logic to work properly 
    return ""

def extract_multiple_recruiter_emails(data: Dict[str, Any]) -> List[str]:
    """
    Extract multiple recruiter email addresses from JobDiva API response.
    Looks for various field patterns that might contain recruiter emails.
    """
    emails = []
    
    # Common JobDiva fields that might contain recruiter emails
    email_fields = [
        "recruiterEmail", "recruiter_email", "recruiterEmails", "recruiter_emails",
        "ownerEmail", "owner_email", "assignedRecruiterEmail", "assigned_recruiter_email",
        "accountManagerEmail", "account_manager_email", "contactEmail", "contact_email",
        "primaryRecruiterEmail", "primary_recruiter_email", "salesRepEmail", "sales_rep_email"
    ]
    
    for field in email_fields:
        value = get_field(data, [field])
        if value:
            if isinstance(value, str):
                # Single email or comma-separated emails
                split_emails = [email.strip() for email in value.split(',') if email.strip()]
                emails.extend(split_emails)
            elif isinstance(value, list):
                # List of emails
                emails.extend([str(email).strip() for email in value if email])
    
    # Remove duplicates and invalid emails, maintain order
    seen = set()
    valid_emails = []
    for email in emails:
        email = email.strip().lower()
        if email and '@' in email and '.' in email and email not in seen:
            seen.add(email)
            valid_emails.append(email)
    
    return valid_emails

def normalize_employment_type(emp_type: str) -> str:
    """
    Normalize JobDiva employment types to standard application format.
    Maps various JobDiva values to: W2, 1099, C2C, Full-Time, Contract
    """
    if not emp_type:
        return ""
    
    emp_lower = emp_type.lower().strip()
    
    # Map direct placement to Full-Time as requested
    if "direct placement" in emp_lower or "direct" in emp_lower:
        return "Full-Time"
    
    # Map other common JobDiva employment types
    if "full" in emp_lower and "time" in emp_lower:
        return "Full-Time"
    if "part" in emp_lower and "time" in emp_lower:
        return "Part-Time"
    if "contract" in emp_lower:
        return "Contract"
    if "w2" in emp_lower or "w-2" in emp_lower:
        return "W2"
    if "1099" in emp_lower:
        return "1099"
    if "c2c" in emp_lower or "corp to corp" in emp_lower or "corp-to-corp" in emp_lower:
        return "C2C"
    if "temp" in emp_lower and ("to" in emp_lower or "perm" in emp_lower):
        return "Contract"
    if "permanent" in emp_lower or "perm" in emp_lower:
        return "Full-Time"
    
    # Return original if no mapping found
    return emp_type

class JobDivaService:
    def _extract_customer_from_description(self, description: str) -> str:
        """Last resort: try to extract customer name from the description text."""
        if not description: return None
        
        # Look for patterns like "Client: [Name]" or "Company: [Name]" or "Customer: [Name]"
        patterns = [
            r"(?i)client:\s*([^\n\r<]+)",
            r"(?i)company:\s*([^\n\r<]+)",
            r"(?i)customer:\s*([^\n\r<]+)",
            r"(?i)hiring company:\s*([^\n\r<]+)"
        ]
        
        for p in patterns:
            match = re.search(p, description[:1000]) # Only check first 1000 chars
            if match:
                name = match.group(1).strip()
                # Basic cleanup
                name = re.sub(r'<[^>]*>', '', name)
                if len(name) > 2 and len(name) < 100:
                    return name
        return None
    def get_local_job(self, job_id: str) -> Optional[dict]:
        if not self.engine:
            return None
        try:
            with self.engine.connect() as conn:
                res = conn.execute(text("SELECT * FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
                row = res.fetchone()
                if row:
                    # Map row mapping to dict
                    return dict(row._mapping)
        except Exception as e:
            logger.error(f"Error fetching local job {job_id}: {e}")
        return None

    def __init__(self):
        self.api_url = JOBDIVA_API_URL
        self.client_id = JOBDIVA_CLIENT_ID
        self.username = JOBDIVA_USERNAME
        self.password = JOBDIVA_PASSWORD
        self.cached_token = None
        self.token_expiry = 0
        
        self.db_url = DATABASE_URL
        self.engine = None
        if self.db_url:
            try:
                # v22: add pool sizing + pre_ping + connect_timeout. Pre-v22 a
                # slow DB connect hung uvicorn workers for TCP default ~2 min;
                # unpooled defaults also leaked connections under load.
                self.engine = sqlalchemy.create_engine(
                    self.db_url,
                    pool_size=5,
                    max_overflow=10,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    connect_args={"connect_timeout": 5},
                )
            except Exception as e:
                logger.error(f"Failed to create JobDiva DB engine: {e}")

    async def authenticate(self) -> str:
        """Authenticate with JobDiva and return JWT token."""
        if self.cached_token and time.time() < self.token_expiry:
            return self.cached_token
        
        if not self.client_id or not self.username:
            logger.error(f"JobDiva Credentials not configured properly.")
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

    async def search_candidates(
        self,
        skills: List[Any],
        location: str,
        page: int = 1,
        limit: int = 100,
        job_id: str = None,
        boolean_string: str = "",
        recent_days: Optional[int] = None,
        require_resume: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search for candidates.
        - If job_id provided: Search applicants to that specific job (with optional filtering)
        - If no job_id: Search the general Talent Pool based on skills and location

        New in April 2026:
        - `recent_days`: if set, JobDiva Talent Search is constrained to candidates
          whose LASTMODIFIED is within the last N days (embedded inside the
          boolean via jobdiva_boolean_translator).
        - `require_resume`: when True (default), Talent Search results without
          resume text/file are dropped before returning. Recruiters opted into
          "Include candidates without resumes" pass False.
        """
        token = await self.authenticate()
        if not token: return []

        # If job_id provided, search for applicants to that specific job (with filtering)
        if job_id:
            return await self._search_job_applicants(job_id, limit, token, skills, location)

        # Talent pool search
        logger.debug("Searching JobDiva general talent pool")
        return await self._search_talent_pool(
            skills, location, limit, token,
            boolean_string=boolean_string,
            recent_days=recent_days,
            require_resume=require_resume,
        )

    async def _search_job_applicants(self, job_id: str, limit: int, token: str, skills: List[Any] = None, location: str = "") -> List[Dict[str, Any]]:
        """
        Search for candidates who applied to a specific job.
        Location constraints removed - only skills filtering if needed.
        """
        # Always get all job applicants - location constraints removed
        logger.debug(f"Getting all JobDiva applicants for job_id={job_id}")
        return await self._get_all_job_applicants(job_id, limit, token)

    async def _get_all_job_applicants(self, job_id: str, limit: int, token: str) -> List[Dict[str, Any]]:
        """Get all candidates who applied to a specific job using JobDiva v2 API."""
        # Resolve numeric ID if it's a reference number
        safe_id = job_id
        if "-" in job_id:
            job_info = await self.get_job_by_id(job_id)
            if job_info:
                safe_id = str(get_field(job_info, ["id", "jobId"]))

        logger.debug(f"Getting JobDiva applicants for job_id={job_id}, safe_id={safe_id}")
        
        # Use only the working JobApplicantsDetail endpoint
        endpoint_url = f"{self.api_url}/apiv2/bi/JobApplicantsDetail?jobId={safe_id}"
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        jd_results = []
        
        try:
            logger.debug(f"Trying JobDiva applicants endpoint: {endpoint_url}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(endpoint_url, headers=headers)
                
                logger.debug(f"Job applicants API response: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    logger.debug(f"Raw applicants data type: {type(data)}, keys: {list(data.keys()) if isinstance(data, dict) else 'Not dict'}")
                    
                    # Handle JobApplicantsDetail response format: {'message': '', 'data': [candidates]}
                    applicants = []
                    if isinstance(data, dict) and "data" in data:
                        applicants = data["data"] or []
                    elif isinstance(data, list):
                        applicants = data
                    elif isinstance(data, dict):
                        # Fallback: try other possible keys
                        applicants = (data.get("applicants") or 
                                    data.get("candidates") or 
                                    data.get("results") or [])
                    
                    logger.debug(f"Extracted {len(applicants)} applicants from JobApplicantsDetail")
                    
                    for c in applicants:
                        # Use correct field names from JobApplicantsDetail response
                        first_name = get_field(c, ["FIRSTNAME", "firstName", "firstname"]) or "Unknown"
                        last_name = get_field(c, ["LASTNAME", "lastName", "lastname"]) or "Candidate"
                        full_name = f"{first_name} {last_name}".strip()
                        
                        # Extract candidate ID using correct field name
                        candidate_id = get_field(c, ["CANDIDATEID", "candidateId", "id", "ID", "canId"]) or "Unknown"
                        
                        # Simple default score 
                        match_score = self._calculate_match_score(c, [])
                        
                        # Extract candidate skills
                        candidate_skills = self._extract_candidate_skills(c)
                        
                        jd_results.append({
                            "candidate_id": str(candidate_id),  # Add this field for consistency
                            "id": str(candidate_id),
                            "name": full_name,
                            "first_name": first_name,  # Use underscore format
                            "last_name": last_name,    # Use underscore format
                            "firstName": first_name,
                            "lastName": last_name,
                            "email": get_field(c, ["EMAIL", "email", "emailAddress"]) or "",
                            "city": get_field(c, ["CITY", "city", "locationCity", "workCity"]) or "",
                            "state": get_field(c, ["STATE", "state", "locationState", "workState"]) or "",
                            "title": get_field(c, ["TITLE", "title", "candidateTitle", "jobTitle"]) or "",
                            "source": "JobDiva Applicants",
                            "match_score": match_score,
                            "skills": candidate_skills,
                            "experience_years": self._extract_experience_years(c),
                            "resume_text": self._extract_resume_text(c),
                            "resume_id": get_field(c, ["RESUMEID", "resumeId", "resume_id"]),
                            "received": get_field(c, ["RECEIVED", "received"]),
                            "available": get_field(c, ["AVAILABLE", "available"]),
                            "lastnote": get_field(c, ["LASTNOTE", "lastNote"]),
                            "phone": get_field(c, ["PHONE", "phone", "phoneNumber", "mobilePhone"]) or ""
                        })
                    
                    if jd_results:
                        logger.debug(f"Got {len(jd_results)} applicants from JobApplicantsDetail")
                else:
                    logger.warning(f"❌ Endpoint failed with status: {response.status_code}, response: {response.text[:200]}")
                    
        except Exception as e:
            logger.error(f"❌ Exception with endpoint {endpoint_url}: {e}")
        
        return jd_results

    async def _search_talent_pool(
        self,
        skills: List[Any],
        location: str,
        limit: int,
        token: str,
        boolean_string: str = "",
        recent_days: Optional[int] = None,
        require_resume: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search JobDiva Talent Search using the generated Boolean string.

        The raw `boolean_string` coming from the frontend is in human-readable
        form (`"Databricks" AND "5+ years"`). JobDiva expects the
        `OVER N YRS` dialect (`"DATABRICKS" OVER 5 YRS`) — so we translate
        through `jobdiva_boolean_translator.translate_for_jobdiva` right
        before building the payload. See
        `apps/api/services/jobdiva_boolean_translator.py` for the syntax
        rules we normalize.

        Also filters out profile-only candidates (no resume_text) unless the
        caller explicitly opts in via `require_resume=False`. These profiles
        are what triggered the "This candidate's resume is not available"
        warning in the UI and eroded trust in the match ranking.
        """
        from services.jobdiva_boolean_translator import (
            translate_for_jobdiva,
            extract_skill_years,
        )

        jd_results = []
        raw_search_value = (
            boolean_string.strip()
            if boolean_string
            else self._build_talent_boolean(skills, location)
        )

        # Pull { skill: years } hints from the passed `skills` payload so
        # the translator can attach OVER clauses even if the frontend
        # forgot to inline them.
        skill_years = extract_skill_years(
            skills if isinstance(skills, list) else []
        )

        translated_search_value = translate_for_jobdiva(
            raw_search_value,
            skill_years=skill_years,
            recent_days=recent_days,
        )

        url = f"{self.api_url}/apiv2/jobdiva/TalentSearch"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "searchValue": translated_search_value or raw_search_value,
            "maxReturned": limit,
            "startFrom": 0,
        }

        logger.debug(
            f"JobDiva Talent Search — raw: {raw_search_value!r} | "
            f"translated: {translated_search_value!r} | recent_days={recent_days}"
        )

        dropped_no_resume = 0
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code != 200:
                    logger.warning(f"JobDiva Talent Search failed: {response.status_code} - {response.text[:200]}")
                    return jd_results

                data = response.json()
                if isinstance(data, dict):
                    candidates = data.get("data") or data.get("candidates") or data.get("results") or []
                else:
                    candidates = data or []

                for c in candidates:
                    candidate_id = str(get_field(c, ["candidateId", "CANDIDATEID", "id", "ID"]) or "")
                    if not candidate_id:
                        continue

                    first_name = get_field(c, ["firstName", "firstname", "FIRSTNAME"]) or "Unknown"
                    last_name = get_field(c, ["lastName", "lastname", "LASTNAME"]) or "Candidate"
                    full_name = f"{first_name} {last_name}".strip()

                    resume_text = self._extract_resume_text(c)
                    resume_id = get_field(c, ["resumeId", "RESUMEID", "resume_id"])
                    has_resume = bool((resume_text or "").strip()) or bool(resume_id)

                    # Filter out profile-only candidates unless caller opts in.
                    # These trigger the "resume not available" warning downstream
                    # and hurt the recruiter's trust in the match ranking.
                    if require_resume and not has_resume:
                        dropped_no_resume += 1
                        continue

                    city = get_field(c, ["city", "locationCity", "CITY"]) or ""
                    state = get_field(c, ["state", "locationState", "STATE"]) or ""
                    location_str = ", ".join([p for p in [city, state] if p]).strip()

                    # Abstract: prefer an explicit summary/comments field if JobDiva
                    # returns one; fall back to the first ~200 chars of resume text
                    # so the Step-5 list can show something meaningful.
                    raw_abstract = (
                        get_field(c, ["summary", "SUMMARY", "abstract", "ABSTRACT", "comments", "COMMENTS", "notes", "NOTES"])
                        or ""
                    )
                    if not raw_abstract and resume_text:
                        raw_abstract = resume_text[:240].replace("\n", " ").strip()
                    if raw_abstract and len(raw_abstract) > 240:
                        raw_abstract = raw_abstract[:237].rstrip() + "..."

                    recent_availability = (
                        get_field(
                            c,
                            [
                                "recentAvailability",
                                "RECENTAVAILABILITY",
                                "recent_availability",
                                "RECENT_AVAILABILITY",
                                "recentAvailable",
                                "RECENTAVAILABLE",
                                "recent_status",
                                "RECENT_STATUS",
                            ],
                        )
                        or ""
                    )
                    availability_status = (
                        recent_availability
                        or get_field(c, ["available", "AVAILABLE", "availability", "AVAILABILITY", "status", "STATUS"])
                        or ""
                    )
                    profile_url = get_field(
                        c,
                        ["profileUrl", "PROFILEURL", "profile_url", "PROFILE_URL"],
                    )

                    jd_results.append({
                        "candidate_id": candidate_id,
                        "id": candidate_id,
                        "name": full_name,
                        "first_name": first_name,
                        "last_name": last_name,
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": get_field(c, ["email", "EMAIL"]) or "",
                        "city": city,
                        "state": state,
                        "location": location_str,
                        "title": get_field(c, ["title", "candidateTitle", "TITLE"]) or "",
                        "source": "JobDiva-TalentSearch",
                        "match_score": 75,
                        "skills": self._extract_candidate_skills(c),
                        "experience_years": self._extract_experience_years(c),
                        "resume_text": resume_text,
                        "resume_id": resume_id,
                        "received": get_field(c, ["received", "RECEIVED"]),
                        "recent_availability": recent_availability,
                        "available": availability_status,
                        "availability_status": availability_status,
                        "abstract": raw_abstract,
                        "profile_url": profile_url,
                        "lastnote": get_field(c, ["lastNote", "LASTNOTE"]),
                        "phone": get_field(c, ["phone", "phoneNumber", "PHONE"]) or "",
                    })

                if dropped_no_resume:
                    logger.info(
                        f"JobDiva Talent Search: dropped {dropped_no_resume} "
                        f"profile-only candidates (no resume). Toggle "
                        f"'Include candidates without resumes' on the UI to keep them."
                    )
                logger.debug(f"JobDiva Talent Search returned {len(jd_results)} candidates")
        except Exception as e:
            logger.error(f"Talent Search Error: {e}")

        return jd_results

    def _build_talent_boolean(self, skills: List[Any], location: str) -> str:
        terms = []
        excludes = []
        for skill in skills or []:
            name = skill.get("value") if isinstance(skill, dict) else str(skill)
            match_type = skill.get("match_type", "must") if isinstance(skill, dict) else "must"
            if not name:
                continue
            term = f'"{str(name).strip()}"'
            if match_type == "exclude":
                excludes.append(term)
            else:
                terms.append(term)
        if location and location.strip():
            terms.append(f'"{location.strip()}"')
        search_value = " AND ".join(terms) if terms else "*"
        if excludes:
            search_value = f"{search_value} NOT ({' OR '.join(excludes)})"
        return search_value

    def _calculate_match_score(self, candidate: Dict[str, Any], required_skills: List[Any] = None) -> int:
        """Calculate a real match score based on candidate skills vs job requirements."""
        if not required_skills:
            # Base score for candidates without specific requirements
            base_score = 65
            
            # Boost based on available data quality
            title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
            email = get_field(candidate, ["email", "EMAIL"]) or ""
            
            # Title quality scoring
            if any(word in title.lower() for word in ["senior", "lead", "principal", "architect"]):
                base_score += 10
            elif any(word in title.lower() for word in ["junior", "entry", "intern"]):
                base_score -= 5
                
            # Contact completeness
            if email and "@" in email:
                base_score += 5
                
            return min(base_score, 85)  # Cap at 85% without specific matching
        
        # Calculate actual skill matching
        candidate_skills = self._extract_candidate_skills(candidate)
        candidate_title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
        
        if not candidate_skills and not candidate_title:
            return 60  # Minimum score for candidates with no skill data
            
        matched_skills = 0
        total_required = len(required_skills)
        
        if total_required == 0:
            return 70  # Default when no requirements specified
            
        logger.info(f"🎯 Matching {len(candidate_skills)} candidate skills against {total_required} requirements")
        
        for req_skill in required_skills:
            skill_name = req_skill.get("value", "").lower() if isinstance(req_skill, dict) else str(req_skill).lower()
            
            # Check against candidate skills with improved matching
            skill_match = False
            for candidate_skill in candidate_skills:
                candidate_skill_lower = candidate_skill.lower()
                
                # Exact match
                if skill_name == candidate_skill_lower:
                    skill_match = True
                    break
                    
                # Partial match (either direction)
                elif (skill_name in candidate_skill_lower or 
                      candidate_skill_lower in skill_name):
                    skill_match = True
                    break
                    
                # Technology family matching (e.g., "react" matches "reactjs")
                elif self._are_similar_skills(skill_name, candidate_skill_lower):
                    skill_match = True
                    break
                    
            # Check against candidate title if no skill match
            if not skill_match and skill_name in candidate_title.lower():
                skill_match = True
                
            if skill_match:
                matched_skills += 1
                
        # Calculate base percentage 
        match_percentage = (matched_skills / total_required) * 100 if total_required > 0 else 70
        
        # Apply experience and seniority bonuses
        exp_years = self._extract_experience_years(candidate)
        if exp_years >= 10:
            match_percentage += 15  # Senior bonus
        elif exp_years >= 5:
            match_percentage += 10  # Mid-level bonus
        elif exp_years >= 2:
            match_percentage += 5   # Junior+ bonus
            
        # Skill depth bonus (more skills = better match potential)
        if len(candidate_skills) >= 8:
            match_percentage += 5
        elif len(candidate_skills) >= 5:
            match_percentage += 3
            
        final_score = max(45, min(95, int(match_percentage)))
        logger.info(f"📊 Final match score: {matched_skills}/{total_required} skills = {final_score}%")
        
        # Ensure reasonable bounds
        return final_score
    
    def _extract_candidate_skills(self, candidate: Dict[str, Any]) -> List[str]:
        """Extract skills from candidate data without using the Azure agent."""
        skills = []

        # Look for skills in various fields
        skill_fields = ["skills", "skillList", "technologies", "expertise", "summary"]
        for field in skill_fields:
            skill_data = get_field(candidate, [field])
            if skill_data:
                if isinstance(skill_data, str):
                    # Parse comma-separated or space-separated skills
                    potential_skills = [s.strip() for s in skill_data.replace(",", " ").split() if len(s.strip()) > 2]
                    skills.extend(potential_skills[:10])  # Limit to 10
                elif isinstance(skill_data, list):
                    skills.extend([str(s) for s in skill_data[:10]])
        
        # If no skills found from resume, try to infer basic skills from title and other fields
        if not skills:
            title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
            title_lower = title.lower()
            
            # Generate basic skills based on common job titles - conservative approach
            if "java" in title_lower and "developer" in title_lower:
                skills = ["Java", "Software Development"]
            elif "python" in title_lower:
                skills = ["Python", "Software Development"]  
            elif "react" in title_lower or "frontend" in title_lower:
                skills = ["JavaScript", "Frontend Development"]
            elif "data analyst" in title_lower or "data science" in title_lower:
                skills = ["Data Analysis", "SQL"]
            elif "qa" in title_lower or "test" in title_lower:
                skills = ["Testing", "Quality Assurance"]
            elif any(word in title_lower for word in ["accountant", "accounting", "payable", "receivable"]):
                skills = ["Accounting", "Financial Analysis"]
            else:
                # Very basic skills for unknown roles
                skills = ["Communication", "Problem Solving"]
        
        # Remove duplicates and limit - return empty list if no meaningful skills found  
        final_skills = list(set(skills))[:8]
        if len(final_skills) == 2 and set(final_skills) == {"Communication", "Problem Solving"}:
            return []  # Don't return generic skills - better to show empty
        
        return final_skills
    
    def _are_similar_skills(self, skill1: str, skill2: str) -> bool:
        """Check if two skills are similar (e.g., react vs reactjs, python vs python3)."""
        # Remove common suffixes/prefixes
        normalize = lambda s: re.sub(r'(\.js|js|\.py|py|\d+|[^\w])', '', s.lower())
        
        norm1, norm2 = normalize(skill1), normalize(skill2)
        
        # Check if normalized versions match
        if norm1 == norm2:
            return True
            
        # Check common technology aliases
        aliases = {
            'javascript': ['js', 'ecmascript'],
            'typescript': ['ts'],
            'python': ['py'],
            'react': ['reactjs'],
            'vue': ['vuejs'],
            'node': ['nodejs'],
            'sql': ['mysql', 'postgresql', 'postgres'],
            'aws': ['amazon web services'],
            'gcp': ['google cloud'],
            'azure': ['microsoft azure']
        }
        
        for base, alias_list in aliases.items():
            if ((norm1 == base and norm2 in alias_list) or 
                (norm2 == base and norm1 in alias_list)):
                return True
                
        return False

    def _extract_experience_years(self, candidate: Dict[str, Any]) -> int:
        """Extract years of experience from candidate data."""
        # Look for experience fields
        exp_fields = ["experience", "yearsExperience", "totalExperience", "workExperience", "experienceYears"]
        for field in exp_fields:
            exp_data = get_field(candidate, [field])
            if exp_data and isinstance(exp_data, (int, float)) and exp_data > 0:
                return int(exp_data)
        
        # Try to extract from text fields
        title = get_field(candidate, ["title", "candidateTitle", "TITLE"]) or ""
        resume_text = get_field(candidate, ["resume", "resumeText", "summary"]) or ""
        
        # Look for patterns like "5+ years", "10 years experience", etc.
        import re
        text_to_search = f"{title} {resume_text}".lower()
        
        # Pattern matching for experience
        patterns = [
            r'(\d+)\+?\s*years?\s*(?:of\s*)?(?:experience|exp)',
            r'(\d+)\+?\s*yrs?\s*(?:of\s*)?(?:experience|exp)',
            r'over\s*(\d+)\s*years?',
            r'more\s*than\s*(\d+)\s*years?'
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, text_to_search)
            if matches:
                try:
                    years = int(matches[0])
                    if 0 <= years <= 50:  # Reasonable bounds
                        return years
                except ValueError:
                    continue
        
        # Infer from title seniority (more conservative estimates)
        title_lower = title.lower()
        if "senior" in title_lower or "sr" in title_lower:
            return 7  # Senior typically means 5-10 years
        elif "lead" in title_lower or "principal" in title_lower:
            return 10  # Lead/Principal typically means 8-15 years
        elif "architect" in title_lower or "manager" in title_lower:
            return 12  # Architect/Manager typically means 10+ years
        elif "junior" in title_lower or "jr" in title_lower:
            return 2   # Junior typically means 1-3 years
        elif "entry" in title_lower or "intern" in title_lower:
            return 1   # Entry level
        else:
            return 4   # Default mid-level experience

    def _extract_resume_text(self, candidate: Dict[str, Any]) -> str:
        """Extract resume/summary text from candidate data. Returns empty string if no resume found."""
        # Look for resume text in various fields
        resume_fields = ["resume", "resumeText", "summary", "profile", "description", "bio", "overview"]
        
        for field in resume_fields:
            resume_data = get_field(candidate, [field])
            if resume_data and isinstance(resume_data, str) and len(resume_data.strip()) > 20:
                # Clean HTML tags and return formatted text
                clean_text = re.sub(r'<[^>]+>', '', resume_data)
                clean_text = re.sub(r'\s+', ' ', clean_text)  # Normalize whitespace
                return clean_text.strip()
        
        # Return empty string if no resume found - no fallback generation
        return ""

    async def get_candidate_details(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed candidate information using /apiv2/bi/CandidatesDetail endpoint."""
        token = await self.authenticate()
        if not token:
            logger.warning(f"JobDiva authentication failed for candidate {candidate_id}")
            return None
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        endpoint = f"{self.api_url}/apiv2/bi/CandidatesDetail"
        params = {"candidateIds": [candidate_id]}
        
        try:
            logger.debug(f"Fetching candidate details for {candidate_id}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(endpoint, params=params, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    logger.debug(f"Candidate details received for {candidate_id}")
                    
                    if isinstance(data, dict) and "data" in data:
                        candidates = data["data"]
                        if candidates and len(candidates) > 0:
                            return candidates[0] if isinstance(candidates, list) else candidates
                    
        except Exception as e:
            logger.debug(f"Error fetching candidate details for {candidate_id}: {e}")
        
        return None
    
    async def get_candidate_resumes(self, candidate_id: str) -> Optional[List[Dict[str, Any]]]:
        """Get all resumes for a candidate using /apiv2/bi/CandidatesResumesDetail endpoint."""
        token = await self.authenticate()
        if not token:
            logger.warning(f"JobDiva authentication failed for candidate {candidate_id}")
            return None
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        endpoint = f"{self.api_url}/apiv2/bi/CandidatesResumesDetail"
        params = {"candidateIds": [candidate_id]}
        
        try:
            logger.debug(f"Fetching candidate resumes for {candidate_id}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(endpoint, params=params, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    if isinstance(data, dict) and "data" in data:
                        resumes = data["data"]
                        if resumes:
                            resume_count = len(resumes) if isinstance(resumes, list) else 1
                            logger.debug(f"Found {resume_count} resume(s) for candidate {candidate_id}")
                            return resumes if isinstance(resumes, list) else [resumes]
                    
        except Exception as e:
            logger.debug(f"Error fetching candidate resumes for {candidate_id}: {e}")
        
        return None

    async def get_candidate_resume(
        self,
        candidate_id: str,
        resume_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch full candidate resume/details by ID using JobDiva API v2 endpoints."""
        logger.debug(f"Getting resume for candidate {candidate_id}")
        
        token = await self.authenticate()
        if not token:
            logger.warning(f"JobDiva authentication failed for candidate {candidate_id}")
            return None
        
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        # Try the working resume fetching logic with cascading fallback
        resume_text = ""
        selected_resume_id = resume_id
        candidate_info = {}
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Step 1: Try to get candidate details
                details_url = f"{self.api_url}/apiv2/bi/CandidatesDetail"
                details_resp = await client.get(details_url, params={"candidateIds": [candidate_id]}, headers=headers)
                
                if details_resp.status_code == 200:
                    details_data = details_resp.json()
                    if isinstance(details_data, dict) and "data" in details_data:
                        candidates = details_data["data"]
                        if candidates and len(candidates) > 0:
                            candidate_info = candidates[0] if isinstance(candidates, list) else candidates
                            logger.debug(f"Got candidate details for {candidate_id}")
                
                if resume_id:
                    # Applicant flow: JobDiva already told us the exact resume for
                    # this applicant. Fetch that resume text directly; do not
                    # re-select from the candidate's full resume history.
                    resume_text = await self._get_resume_text_by_id(str(resume_id), client, headers)
                else:
                    resume_result = await self._get_resume_detail_with_id(
                        candidate_id,
                        client,
                        headers,
                    )
                    resume_text = resume_result.get("resume_text", "")
                    selected_resume_id = resume_result.get("resume_id") or selected_resume_id
                                
        except Exception as e:
            logger.warning(f"Error fetching candidate resume for {candidate_id}: {e}")
        
        # If we didn't get candidate info, create basic info from candidate_id
        if not candidate_info:
            candidate_info = {
                "candidateId": candidate_id,
                "firstName": "Unknown",
                "lastName": "Candidate"
            }
        
        # Add resume text to candidate info
        candidate_info["resume_text"] = resume_text or "Resume content unavailable"
        candidate_info["resume_id"] = selected_resume_id
        candidate_info["resume_count"] = 1 if resume_text else 0
        
        return self._format_candidate_resume(candidate_info)
    
    def _format_candidate_resume(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Format candidate data for resume display."""
        # Extract basic info using multiple possible field names
        candidate_id = get_field(candidate, ["candidateId", "id", "ID", "CANDIDATEID"]) or ""
        first_name = get_field(candidate, ["firstName", "FIRSTNAME", "firstname"]) or ""
        last_name = get_field(candidate, ["lastName", "LASTNAME", "lastname"]) or ""
        full_name = f"{first_name} {last_name}".strip() or candidate.get("name", "") or "Professional Candidate"
        
        # Extract resume text - could be in different fields
        resume_text = get_field(candidate, ["resume_text", "resumeText", "RESUMETEXT", "text", "content"]) or ""
        
        # If no resume text found, try to extract from resume data structure
        if not resume_text:
            resume_text = self._extract_resume_text(candidate)
        
        return {
            "id": str(candidate_id),
            "name": full_name,
            "firstName": first_name,
            "lastName": last_name,
            "email": get_field(candidate, ["email", "EMAIL", "emailAddress"]) or "Available upon request",
            "phone": get_field(candidate, ["phone", "PHONE", "phoneNumber", "mobilePhone"]) or "Available upon request", 
            "title": get_field(candidate, ["title", "TITLE", "currentTitle", "jobTitle"]) or "",
            "location": get_field(candidate, ["location", "city", "CITY", "workCity"]) or "",
            "text": resume_text,  # Main resume text field
            "resume_text": resume_text,  # Backup field name
            "resume_id": get_field(candidate, ["resume_id", "resumeId", "RESUMEID"]),
            "skills": self._extract_candidate_skills(candidate),
            "experience": get_field(candidate, ["experience", "EXPERIENCE", "experienceYears"]) or "",
            "education": get_field(candidate, ["education", "EDUCATION"]) or "",
            "resume_count": candidate.get("resume_count", 1),
            "source": "JobDiva"
        }
    
    async def get_job_by_id(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a specific job by ID from JobDiva, including AI UDFs."""
        logger.info(f"Fetching Job ID: {job_id}")
        token = await self.authenticate()
        if not token: return None

        url = f"{self.api_url}/apiv2/jobdiva/SearchJob"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        # NEW: Prefer Reference Number search even if numeric ID was provided
        # (JobDiva SearchJob API is more reliable with ref numbers than legacy numeric IDs)
        is_ref = "-" in job_id
        search_id = job_id
        
        if not is_ref:
            local_job = self.get_locally_monitored_job(job_id)
            if local_job and local_job.get("jobdiva_id"):
                search_id = local_job.get("jobdiva_id")
                is_ref = True
                logger.info(f"🔄 ID-Resolution: Using Reference {search_id} instead of numeric ID {job_id} for better reliability")

        if is_ref:
            payload = {"jobdivaref": search_id, "maxReturned": 1}
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
                j_ref = str(get_field(j, ["reference #", "jobdivaref", "ref", "jobdivano"]) or "")
                
                if is_ref:
                    if search_id.lower() != j_ref.lower():
                        logger.warning(f"Bogus JobDiva response: requested ref {search_id}, got ref {j_ref}")
                        return None
                else:
                    if safe_id != j_id:
                        # NEW: Relaxed ID matching for Aliases (ID 31920032 vs 9165998)
                        # Check local DB for expected reference first
                        local_job = self.get_locally_monitored_job(job_id)
                        expected_ref = local_job.get("jobdiva_id")
                        
                        if expected_ref and str(expected_ref).lower() == j_ref.lower():
                            logger.info(f"✅ Aliased ID Accepted: requested ID {safe_id}, got ID {j_id} (Ref {j_ref} matches local DB)")
                        else:
                            logger.warning(f"Bogus JobDiva response: requested ID {safe_id}, got ID {j_id}. Ref '{j_ref}' did not match expected '{expected_ref}'")
                            return None
                        
                # ----------------------------------------------------
                # CRITICAL: JobDiva v2 SearchJob endpoint randomly drops fields like MAXALLOWEDSUBMITTALS
                # We supplement it here using the /apiv2/bi/JobDetail BI endpoint which retains them.
                # ----------------------------------------------------
                detail_url = f"{self.api_url}/apiv2/bi/JobDetail"
                detail_params = {"jobdivaref": j_ref} if j_ref else {"jobId": j_id}
                try:
                    det_resp = await client.get(detail_url, params=detail_params, headers=headers)
                    if det_resp.status_code == 200:
                        det_data = det_resp.json()
                        det_list = det_data.get("data", []) if isinstance(det_data, dict) else det_data
                        if det_list and len(det_list) > 0:
                            d = det_list[0]
                            max_sub = d.get("MAXALLOWEDSUBMITTALS")
                            if max_sub:
                                j["maxAllowedSubmittals"] = max_sub
                            
                            # Add rock-solid BI Customer/Company name extraction
                            # We search for every possible variation found across different JobDiva setups
                            bi_keys = [
                                "CUSTOMERNAME", "COMPANYNAME", "CUSTOMER", "COMPANY", 
                                "CLIENTNAME", "CLIENT_NAME", "CLIENT", "NAME", "COMPANY_FULL_NAME"
                            ]
                            for ckey in bi_keys:
                                if d.get(ckey):
                                    j["customer_bi"] = d.get(ckey)
                                    logger.info(f"Found customer '{j['customer_bi']}' in BI field '{ckey}'")
                                    break

                            # Add robust BI Date and Status Extraction
                            if d.get("JOBSTATUS"):
                                j["JOBSTATUS_BI"] = d.get("JOBSTATUS")
                            if d.get("DATEISSUED"):
                                j["DATEISSUED_BI"] = d.get("DATEISSUED")
                            if d.get("STARTDATE"):
                                j["STARTDATE_BI"] = d.get("STARTDATE")
                except Exception as e:
                    logger.warning(f"Failed to fetch JobDetail supplemental data: {e}")
                
                u_fields = j.get("user fields", {}) or {}
                ai_description = None
                job_notes = None
                salary_range_udf = None
                issued_date_udf = None
                for k, v in u_fields.items():
                    k_low = k.lower()
                    # if "ai job description" in k_low: ai_description = v
                    # if "job notes" in k_low or k == "231": job_notes = v
                    if "salary range" in k_low or "pay range" in k_low or "pay rate" in k_low: salary_range_udf = v
                    if "issued date" in k_low or "posted date" in k_low or "date issued" in k_low or k_low == "issued" or k_low == "posted": issued_date_udf = v

                # Resolution Priority:
                # 1. BI Metadata (Most reliable)
                # 2. Standard API fields (company, customer, etc.)
                # 3. Regex parsing of description (Last resort)
                # 4. Local DB Restore (handled below)
                
                raw_customer = j.get("customer_bi") or get_field(j, ["customer", "company", "client", "customerName", "companyName", "clientName", "client_name"])
                
                description = format_job_description(get_field(j, ["job description", "description"]) or "")
                
                if not raw_customer or raw_customer.lower() in ["unknown", "unknown customer", ""]:
                    # Try parsing the first 500 characters of the description for common patterns
                    raw_customer = self._extract_customer_from_description(description)
                    if raw_customer:
                        logger.info(f"Extracted customer '{raw_customer}' from description text")

                customer_name = str(raw_customer or "").title() or "Unknown Customer"

                # ONLY restore full-length UDFs from local DB if JobDiva version looks truncated
                # and is NOT empty (which would mean it was cleared in JobDiva)
                local_data = self.get_locally_monitored_job(job_id)
                if local_data:
                    local_ai = local_data.get("ai_description")
                    # If JobDiva AI Description is not empty, but local is longer, assume truncation 
                    if local_ai and ai_description and len(str(ai_description)) > 3000 and len(str(local_ai)) > len(str(ai_description)):
                        ai_description = local_ai
                        logger.info(f"Restored full ai_description from local DB for {job_id}")
                    
                    local_notes = local_data.get("recruiter_notes")
                    if local_notes and job_notes and len(str(job_notes)) > 1000 and len(str(local_notes)) > len(str(job_notes)):
                        job_notes = local_notes
                        logger.info(f"Restored full recruiter_notes from local DB for {job_id}")
                    
                    # NEW: Restore customer_name from local DB if currently Unknown or Empty
                    local_customer = local_data.get("customer_name")
                    if local_customer and str(local_customer).lower() != "unknown" and (not customer_name or str(customer_name).lower() == "unknown" or customer_name == "Unknown Customer"):
                        customer_name = local_customer
                        logger.info(f"🔄 Self-Healed: Restored customer_name '{customer_name}' from local DB for {job_id}")

                # Advanced pay_rate logic: try to combine min and max if available for a range
                p_min = get_field(j, ["minpayrate", "min_pay_rate", "minimum_pay", "payRateMin", "minimum rate"])
                p_max = get_field(j, ["maxpayrate", "max_pay_rate", "maximum_pay", "payRateMax", "maximum rate"])
                
                # Format to ignore zeros
                if str(p_min) == "0" or str(p_min) == "0.0": p_min = None
                if str(p_max) == "0" or str(p_max) == "0.0": p_max = None
                
                if p_min and p_max:
                    p_range = f"${p_min} - ${p_max}/h"
                elif p_max:
                    p_range = f"${p_max}/h"
                elif p_min:
                    p_range = f"${p_min}/h"
                else:
                    p_range = ""
                
                # Improved Location Type detection - Only use actual location fields, not employment fields
                loc_type_raw = get_field(j, ["location type", "location_type"]) or ""
                
                # Clean the raw value to remove employment type contamination
                def clean_location_type(value):
                    if not value:
                        return ""
                    val_lower = str(value).lower().strip()
                    employment_terms = [
                        "direct placement", "contract", "full-time", "part-time", 
                        "w2", "1099", "c2c", "corp to corp", "open", "pending",
                        "temporary", "permanent", "temp to perm", "fulltime", "parttime"
                    ]
                    if any(term in val_lower for term in employment_terms):
                        return ""
                    return str(value).strip()
                
                cleaned_loc_type = clean_location_type(loc_type_raw)
                
                loc_type = "Onsite" # Default
                if cleaned_loc_type:
                    if "remote" in cleaned_loc_type.lower():
                        loc_type = "Remote"
                    elif "hybrid" in cleaned_loc_type.lower():
                        loc_type = "Hybrid"
                    elif "onsite" in cleaned_loc_type.lower() or "on-site" in cleaned_loc_type.lower():
                        loc_type = "Onsite"
                    else:
                        # Only use the cleaned value if it's not empty and looks like a valid location type
                        loc_type = cleaned_loc_type
                
                # Fallback: check description for location keywords if no valid location type found
                if not cleaned_loc_type or loc_type == "Onsite":
                    if "remote" in description.lower():
                        loc_type = "Remote"
                    elif "hybrid" in description.lower():
                        loc_type = "Hybrid"
                    elif "on-site" in description.lower() or "onsite" in description.lower():
                        loc_type = "Onsite"
                
                result = {
                    "id": get_field(j, ["id", "jobId"]),
                    "jobdiva_id": get_field(j, ["jobdivano", "reference #", "refno", "jobdivaref", "ref"]),
                    "title": get_field(j, ["job title", "title"]),
                    "description": description,
                    "jobdiva_description": description, # Clarified for schema
                    "ai_description": ai_description if ai_description is not None else "",
                    "recruiter_notes": job_notes if job_notes is not None else "",
                    "customer_name": customer_name,
                    "job_status": j.get("JOBSTATUS_BI") or get_field(j, ["job status", "status"]) or "OPEN",
                    "status": j.get("JOBSTATUS_BI") or get_field(j, ["job status", "status"]) or "OPEN", # Database standard
                    "city": _clean_location_field(get_field(j, ["city", "jobCity", "locationCity", "worksitecity"])),
                    "state": _clean_location_field(get_field(j, ["state", "jobState", "locationState", "worksitestate", "province"])),
                    "zip_code": _clean_location_field(get_field(j, ["zip", "postalCode", "zipcode", "postalcode", "worksitezip", "worksitepostalcode"])),
                    "start_date": normalize_jobdiva_date(j.get("STARTDATE_BI") or get_field(j, ["start date", "startDate", "available", "startdate"]) or (local_data.get("start_date") if local_data else "")),
                    "issued_date": normalize_jobdiva_date(j.get("DATEISSUED_BI") or issued_date_udf or get_field(j, ["issued date", "issueddate", "issued_date", "issued"]) or (local_data.get("issued_date") if local_data else "")),
                    "posted_date": normalize_jobdiva_date(j.get("DATEISSUED_BI") or get_field(j, ["posted date", "date", "created date", "posted", "posteddate", "createtimestamp", "date_posted", "posted_at"]) or issued_date_udf or get_field(j, ["issued date", "issueddate", "issued_date", "issued"]) or extract_posted_date_from_text(description) or (local_data.get("posted_date") if local_data else "")) or get_fallback_posted_date(),
                    "location_type": loc_type,
                    "work_authorization": get_field(j, ["work_authorization", "visa", "legal status", "workauth", "work_auth", "work authorization"]) or (local_data.get("work_authorization") if local_data else ""),
                    
                    # Extract multiple recruiter emails from JobDiva API - store in job_configuration only
                    "recruiter_emails": extract_multiple_recruiter_emails(j),
                    
                    "pay_rate": salary_range_udf or p_range or get_field(j, ["pay rate", "salary range", "salary", "rate", "bill rate", "compensation", "billrate", "payrate"]) or extract_pay_rate_from_text(description) or (local_data.get("pay_rate") if local_data else ""),
                    "openings": get_field(j, ["openings", "maxReturned", "positions", "number of openings", "openpositions"]) or (local_data.get("openings") if local_data else ""),
                    "employment_type": normalize_employment_type(get_field(j, ["employment type", "jobType", "assignmentType"]) or (local_data.get("employment_type") if local_data else "")),
                    "required_degree": get_field(j, ["required degree", "required_degree", "criteria degree", "criteria_degree"]) or "",

                    # Extended JobDiva fields
                    "priority": str(get_field(j, ["priority", "jobPriority", "job priority"]) or (local_data.get("priority") if local_data else "") or ""),
                    "program_duration": str(get_field(j, ["duration", "program duration", "contract duration", "program_duration", "assignment duration", "assignmentDuration", "contractDuration"]) or (local_data.get("program_duration") if local_data else "") or ""),
                    "max_allowed_submittals": str(get_field(j, ["max submittals", "maxsubmittals", "max submissions", "maximum submittals", "max_allowed_submittals", "maxResumeSubmittal", "maxAllowedSubmittals"]) or (local_data.get("max_allowed_submittals") if local_data else "") or ""),
                }
                
                # Dynamic Duration Calculation if missing
                if not result.get("program_duration") or result.get("program_duration") == "None":
                    raw_end_date = get_field(j, ["end date", "endDate", "enddate"])
                    if raw_end_date:
                        end_date_str = normalize_jobdiva_date(raw_end_date)
                        calc_duration = calculate_date_duration(result.get("start_date", ""), end_date_str)
                        if calc_duration:
                            result["program_duration"] = calc_duration

                return result
        except Exception as e:
            logger.exception(f"❌ SearchJob Error for job_id {job_id}: {e}")
            return None


    async def get_enhanced_job_candidates(self, job_id: str) -> List[Dict[str, Any]]:
        """
        Enhanced candidate retrieval combining three JobDiva API endpoints:
        1. JobApplicantsDetail - Get job applicants
        2. CandidateDetail - Get candidate info  
        3. ResumeDetail - Get full resume text
        """
        token = await self.authenticate()
        if not token: 
            return []

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        enhanced_candidates = []

        try:
            # Resolve numeric ID if it's a reference number
            safe_id = job_id
            if "-" in job_id:
                logger.info(f"🔄 Resolving numeric ID for reference {job_id}")
                job_info = await self.get_job_by_id(job_id)
                if job_info:
                    # SearchJob returns job id in different fields sometimes
                    resolved_id = get_field(job_info, ["id", "jobId", "jobOrderID"])
                    if resolved_id:
                        safe_id = str(resolved_id)
                        logger.info(f"✅ Resolved {job_id} to internal numeric ID: {safe_id}")

            # Step 1: Get Job Applicants using JobApplicantsDetail
            applicants_url = f"{self.api_url}/apiv2/bi/JobApplicantsDetail"
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"🔍 Fetching job applicants for job_id: {safe_id}")
                
                applicants_response = await client.get(
                    applicants_url, 
                    params={"jobId": safe_id}, 
                    headers=headers
                )
                
                if applicants_response.status_code != 200:
                    logger.error(f"❌ JobApplicantsDetail failed: {applicants_response.status_code}")
                    return enhanced_candidates
                
                applicants_data = applicants_response.json()
                applicants = applicants_data.get("data", []) if isinstance(applicants_data, dict) else applicants_data
                
                logger.info(f"📋 Found {len(applicants)} job applicants")
                
                # Step 2 & 3: For each applicant, get detailed info and resume
                for idx, applicant in enumerate(applicants, 1):
                    try:
                        candidate_id = applicant.get("CANDIDATEID") or applicant.get("candidateId")
                        resume_id = applicant.get("RESUMEID") or applicant.get("resumeId")
                        
                        if not candidate_id:
                            continue
                        
                        logger.debug(f"[{idx}/{len(applicants)}] Processing applicant {candidate_id}")
                            
                        # Get candidate details
                        candidate_detail = await self._get_candidate_detail(candidate_id, client, headers)
                        
                        # Use the specific resume ID from applicant data when JobDiva provides it.
                        resume_text = ""
                        if resume_id:
                            resume_text = await self._get_resume_text_by_id(resume_id, client, headers)
                        else:
                            resume_result = await self._get_resume_detail_with_id(candidate_id, client, headers)
                            resume_text = resume_result.get("resume_text", "")
                            resume_id = resume_result.get("resume_id")
                        
                        # Combine all data
                        enhanced_candidate = self._format_enhanced_candidate(
                            applicant, candidate_detail, resume_text, "job_applicant"
                        )
                        
                        enhanced_candidates.append(enhanced_candidate)
                        
                    except Exception as e:
                        logger.warning(f"⚠️ Error processing applicant {candidate_id}: {e}")
                        continue

        except Exception as e:
            logger.error(f"❌ Error in get_enhanced_job_candidates: {e}")
        
        return enhanced_candidates

    async def _get_candidate_detail(self, candidate_id: str, client: httpx.AsyncClient, headers: dict) -> Dict[str, Any]:
        """Get detailed candidate information using CandidatesDetail endpoint with full details."""
        try:
            # Use CandidatesDetail (plural) endpoint as requested - this includes more comprehensive data
            candidate_url = f"{self.api_url}/apiv2/bi/CandidatesDetail"
            response = await client.get(
                candidate_url,
                params={
                    "candidateId": candidate_id,
                    "includeResume": "true",
                    "includeSkills": "true",
                    "includePersonalInfo": "true"
                },
                headers=headers
            )
            
            if response.status_code == 200:
                data = response.json()
                logger.info(f"✅ CandidatesDetail retrieved for {candidate_id}")
                return data.get("data", [{}])[0] if isinstance(data, dict) else data[0] if data else {}
            else:
                # Silently handle 400 errors - we'll fallback to resume fetching
                logger.debug(f"CandidatesDetail returned {response.status_code} for {candidate_id}, using fallback")
        except Exception as e:
            logger.debug(f"Error fetching candidate detail for {candidate_id}: {e}")

        return {}

    async def get_candidate_profile_url(self, candidate_id: str) -> str:
        """
        Fetch a JobDiva candidate's profile URL on demand.

        JobDiva's Talent Search response does not include PROFILEURL, but the
        CandidatesDetail endpoint does (at least for tenants that publish it).
        Routers use this as a lightweight on-click enrichment so candidate names
        in Step 5 can hyperlink to the JobDiva profile without eagerly pulling
        details for every result.

        Returns an empty string if no URL can be resolved — callers should treat
        that as "render plain text, no link".
        """
        if not candidate_id:
            return ""

        try:
            token = await self.authenticate()
            if not token:
                logger.debug("get_candidate_profile_url: auth failed")
                return ""

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                detail = await self._get_candidate_detail(str(candidate_id), client, headers)

            profile_url = (
                get_field(detail, ["PROFILEURL", "profileUrl", "profile_url", "PROFILE_URL"])
                or ""
            )
            return str(profile_url).strip()
        except Exception as e:
            logger.debug(f"get_candidate_profile_url failed for {candidate_id}: {e}")
            return ""

    def _parse_jobdiva_datetime(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _resume_timestamp(self, resume: Dict[str, Any]) -> datetime:
        """Return the best sortable timestamp JobDiva gives us for a resume."""
        for key in ["DATEUPDATED", "DATECREATED", "DATELASTDOWNLOADED", "DATEFIRSTDOWNLOADED"]:
            parsed = self._parse_jobdiva_datetime(get_field(resume, [key, key.lower(), key.title()]))
            if parsed:
                return parsed
        return datetime.min

    def _resume_created_timestamp(self, resume: Dict[str, Any]) -> datetime:
        parsed = self._parse_jobdiva_datetime(
            get_field(resume, ["DATECREATED", "dateCreated", "datecreated"])
        )
        return parsed or datetime.min

    def _select_resume_record(
        self,
        resumes: List[Dict[str, Any]],
        preferred_resume_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """Prefer the exact resume ID; otherwise choose the newest created resume."""
        if not resumes:
            return None

        if preferred_resume_id:
            preferred = str(preferred_resume_id).strip()
            for resume in resumes:
                resume_id = get_field(resume, ["RESUMEID", "resumeId", "ID", "resume_id"])
                if str(resume_id or "").strip() == preferred:
                    return resume

        def sort_key(resume: Dict[str, Any]):
            doc_id = get_field(resume, ["DOCID", "docId", "ID"]) or 0
            try:
                doc_id = int(doc_id)
            except Exception:
                doc_id = 0
            return (self._resume_created_timestamp(resume), self._resume_timestamp(resume), doc_id)

        return sorted(resumes, key=sort_key, reverse=True)[0]

    async def _get_resume_records(
        self,
        candidate_id: str,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> List[Dict[str, Any]]:
        """Get resume metadata records for a candidate using JobDiva's BI endpoint."""
        endpoint_attempts = [
            (f"{self.api_url}/apiv2/bi/CandidateResumesDetail", {"candidateId": candidate_id}),
            (f"{self.api_url}/apiv2/bi/CandidatesResumesDetail", {"candidateIds": [candidate_id]}),
        ]

        for url, params in endpoint_attempts:
            try:
                response = await client.get(url, params=params, headers=headers)
                if response.status_code != 200:
                    logger.debug(f"{url.rsplit('/', 1)[-1]} returned {response.status_code} for {candidate_id}")
                    continue

                data = response.json()
                resumes = data.get("data", []) if isinstance(data, dict) else data
                if isinstance(resumes, dict):
                    resumes = [resumes]
                if resumes:
                    return resumes
            except Exception as e:
                logger.debug(f"Error fetching resume records for {candidate_id}: {e}")

        return []

    async def _get_resume_detail_with_id(
        self,
        candidate_id: str,
        client: httpx.AsyncClient,
        headers: dict,
        preferred_resume_id: str = None,
    ) -> Dict[str, str]:
        """Get full resume text and the selected resume ID."""
        try:
            logger.debug(f"📄 Fetching resume for candidate ID: {candidate_id}")

            # Step 1: Get all resume IDs for this candidate using CandidateResumesDetail.
            resumes = await self._get_resume_records(candidate_id, client, headers)
            if not resumes:
                logger.debug(f"No resumes found for candidate {candidate_id}")
                return {"resume_text": "", "resume_id": ""}
                
            logger.debug(f"Found {len(resumes)} resume(s) for candidate {candidate_id}")

            selected_resume = self._select_resume_record(resumes, preferred_resume_id)
            selected_resume_id = get_field(selected_resume or {}, ["RESUMEID", "resumeId", "ID", "resume_id"])
            if not selected_resume_id:
                return {"resume_text": "", "resume_id": ""}

            # Step 2: Get resume text using ResumesTextDetail (plural) endpoint.
            resume_text = await self._get_resume_text_by_id(str(selected_resume_id), client, headers)
            return {
                "resume_text": resume_text,
                "resume_id": str(selected_resume_id) if selected_resume_id else "",
            }
                    
        except Exception as e:
            logger.error(f"❌ Error in _get_resume_detail for candidate {candidate_id}: {e}")
        
        return {"resume_text": "", "resume_id": ""}

    async def _get_resume_detail(self, candidate_id: str, client: httpx.AsyncClient, headers: dict) -> str:
        """Get full resume text using CandidateResumesDetail → ResumesTextDetail endpoint flow."""
        result = await self._get_resume_detail_with_id(candidate_id, client, headers)
        return result.get("resume_text", "")

    async def _get_resume_text_by_id(self, resume_id: str, client: httpx.AsyncClient, headers: dict) -> str:
        """Get resume text using a specific resume ID with ResumesTextDetail endpoint."""
        try:
            logger.debug(f"📖 Fetching resume text for resume ID: {resume_id}")
            
            resume_text_url = f"{self.api_url}/apiv2/bi/ResumesTextDetail"
            resume_response = await client.get(
                resume_text_url,
                params={"resumeIds": resume_id},
                headers=headers
            )
            
            if resume_response.status_code == 200:
                resume_detail = resume_response.json()
                
                # Handle different response structures
                if isinstance(resume_detail, dict):
                    resume_content = resume_detail.get("data", [{}])
                    if isinstance(resume_content, list) and resume_content:
                        resume_content = resume_content[0]
                    elif not isinstance(resume_content, dict):
                        resume_content = resume_detail
                else:
                    resume_content = resume_detail[0] if resume_detail else {}
                
                # Extract text from various possible fields
                resume_text = (resume_content.get("PLAINTEXT") or 
                             resume_content.get("plainText") or
                             resume_content.get("text") or 
                             resume_content.get("TEXT") or 
                             resume_content.get("resumeText") or "")
                
                if resume_text and resume_text.strip():
                    from html import unescape
                    logger.debug(f"Fetched resume text ({len(resume_text)} chars) for resume {resume_id}")
                    return unescape(resume_text.strip())
                else:
                    logger.debug(f"Resume text empty for resume ID {resume_id}")
            else:
                logger.debug(f"ResumesTextDetail failed for resume {resume_id}: {resume_response.status_code}")
                
        except Exception as e:
            logger.debug(f"Error fetching resume text for resume {resume_id}: {e}")
        
        return ""

    def _format_enhanced_candidate(self, applicant: Dict[str, Any], candidate_detail: Dict[str, Any], 
                                 resume_text: str, candidate_type: str) -> Dict[str, Any]:
        """Format enhanced candidate data for storage."""
        
        # Extract candidate ID and resume ID
        candidate_id = applicant.get("CANDIDATEID") or candidate_detail.get("CANDIDATEID") or ""
        resume_id = applicant.get("RESUMEID") or candidate_detail.get("RESUMEID") or ""
        
        # Extract basic info with fallbacks
        first_name = (get_field(applicant, ["FIRSTNAME", "firstName"]) or 
                 get_field(candidate_detail, ["FIRSTNAME", "firstName"]) or "")
        last_name = (get_field(applicant, ["LASTNAME", "lastName"]) or 
                get_field(candidate_detail, ["LASTNAME", "lastName"]) or "")
        full_name = f"{first_name} {last_name}".strip() or applicant.get("name", "") or candidate_detail.get("name", "") or "Professional Candidate"
        
        return {
            "jobdiva_id": applicant.get("JOBID") or candidate_detail.get("JOBID") or "",
            "candidate_id": candidate_id,
            "source": "JobDiva-Applicants" if candidate_type == "job_applicant" else "JobDiva-TalentSearch",
            "name": full_name,
            "firstName": first_name,
            "lastName": last_name,
            "email": get_field(candidate_detail, ["EMAIL", "email"]) or get_field(applicant, ["EMAIL", "email"]),
            "phone": get_field(candidate_detail, ["PHONE", "phone"]) or get_field(applicant, ["PHONE", "phone"]),
            "headline": (get_field(candidate_detail, ["TITLE", "title", "currentTitle"]) or 
                        get_field(applicant, ["TITLE", "title"]) or ""),
            "location": self._extract_location(candidate_detail) or self._extract_location(applicant),
            "profile_url": get_field(candidate_detail, ["PROFILEURL", "profileUrl"]) or "",
            "image_url": get_field(candidate_detail, ["IMAGEURL", "imageUrl"]) or "",
            "resume_id": resume_id,
            "resume_text": resume_text,
            "data": {
                "applicant_data": applicant,
                "candidate_detail": candidate_detail,
                "skills": self._extract_skills(candidate_detail) or self._extract_skills(applicant),
                "experience": get_field(candidate_detail, ["EXPERIENCE", "experience"]) or "",
            },
            "status": "sourced"
        }

    async def update_candidate_resume_text(self, candidate_id: str) -> bool:
        """Update resume text for an existing candidate using new CandidateResumesDetail → ResumesTextDetail flow."""
        try:
            logger.info(f"🔄 Updating resume text for candidate: {candidate_id}")
            token = await self.authenticate()
            if not token:
                return False
                
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                resume_text = await self._get_resume_detail(candidate_id, client, headers)
                resume_id = None
                
                if resume_text and resume_text.strip():
                    import psycopg2
                    from core.config import DATABASE_URL
                    
                    with psycopg2.connect(DATABASE_URL, connect_timeout=5) as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE sourced_candidates
                                SET resume_text = %s, resume_id = %s, updated_at = CURRENT_TIMESTAMP
                                WHERE candidate_id = %s
                            """, (resume_text, resume_id, candidate_id))
                            
                            updated_rows = cur.rowcount
                            conn.commit()
                            
                            logger.info(f"✅ Updated resume text for {updated_rows} candidate records ({len(resume_text)} chars)")
                            return updated_rows > 0
                else:
                    logger.warning(f"⚠️ No resume text found for candidate {candidate_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error updating resume for candidate {candidate_id}: {e}")
            return False

    def _extract_location(self, data: Dict[str, Any]) -> str:
        """Extract formatted location from candidate data."""
        city = get_field(data, ["CITY", "city"]) or ""
        state = get_field(data, ["STATE", "state"]) or ""
        country = get_field(data, ["COUNTRY", "country"]) or ""
        
        location_parts = [city, state, country]
        return ", ".join([part for part in location_parts if part])

    def _extract_skills(self, data: Dict[str, Any]) -> List[str]:
        """Extract skills list from candidate data."""
        skills = get_field(data, ["SKILLS", "skills", "skillsList"]) or []
        if isinstance(skills, str):
            return [skill.strip() for skill in skills.split(",") if skill.strip()]
        elif isinstance(skills, list):
            return [str(skill) for skill in skills]
        return []

    async def save_enhanced_candidates_to_db(self, job_id: str, candidates: List[Dict[str, Any]]) -> int:
        """Save enhanced candidates to database with deduplication."""
        from services.sourced_candidates_storage import SourcedCandidatesStorage
        
        storage = SourcedCandidatesStorage()
        saved_count = 0
        
        for candidate in candidates:
            if storage.save_enhanced_candidate(job_id, candidate):
                saved_count += 1
        
        # Deduplicate after saving (prioritize job applicants over talent search)
        dedup_count = storage.deduplicate_candidates(job_id)
        logger.info(f"💾 Saved {saved_count} enhanced candidates, deduplicated {dedup_count}")
        
        return saved_count

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Efficient job status check."""
        job = await self.get_job_by_id(job_id)
        if not job: return {"job_id": job_id, "status": "NOT_FOUND"}
        return {
            "job_id": job_id,
            "status": job.get("job_status", "OPEN"),
            "customer_name": job.get("customer_name", "Unknown"),
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
        """Update JobDiva UDFs with detailed logging."""
        token = await self.authenticate()
        if not token: 
            logger.error("❌ Sync failed: Could not authenticate with JobDiva")
            return False
        
        internal_id = job_id
        if "-" in str(job_id):
            logger.info(f"🔍 Resolving reference string {job_id} to JobDiva ID...")
            job_data = await self.get_job_by_id(job_id)
            if job_data: 
                internal_id = job_data.get("id")
                logger.info(f"✅ Resolved {job_id} to internal ID {internal_id}")
            else: 
                logger.error(f"❌ Failed to resolve {job_id} to a JobDiva internal ID")
                return False
            
        url = f"{self.api_url}/apiv2/jobdiva/updateJob"
        headers = {
            "Authorization": f"Bearer {token}", 
            "Content-Type": "application/json"
        }
        
        # Build a robust UDF list covering multiple JobDiva API variations
        normalized_fields = []
        for f in fields:
            val = str(f.get("userfieldValue") or f.get("value") or "")
            # Truncate to avoid JobDiva 4000-char limit
            if len(val) > 3950: val = val[:3950] + "..."
            
            normalized_fields.append({
                "userfieldId": str(f.get("userfieldId")), 
                "userfieldValue": val,
                "value": val # Some JobDiva v2 endpoints expect 'value'
            })
        
        # JobDiva API is notoriously inconsistent with casing between versions/endpoints
        # We provide redundant keys to ensure the payload is accepted
        payload = {
            "jobId": int(internal_id), 
            "jobid": int(internal_id),
            "userfields": normalized_fields,  # Standard lowercase
            "Userfields": normalized_fields   # Some v2 variations prefer Capital U
        }
        
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                logger.info(f"📡 Pushing UDF updates to JobDiva for Job {internal_id} (Ref: {job_id})...")
                logger.info(f"Payload Preview: {json.dumps(payload)[:200]}...")
                response = await client.post(url, json=payload, headers=headers)
                
                if response.status_code == 200:
                    logger.info(f"✅ JobDiva response: Success (200) for job {job_id}")
                    return True
                else:
                    logger.error(f"❌ JobDiva error ({response.status_code}): {response.text}")
                    return False
        except Exception as e: 
            logger.error(f"❌ HTTP Error during JobDiva UDF push: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False

    def monitor_job_locally(self, job_id: str, data: dict) -> bool:
        """Enhanced monitor_job_locally with complete field coverage and validation"""
        if not self.engine:
            logger.error("Database engine not initialized for monitoring")
            return False
            
        debug_log(f"Starting monitor_job_locally for job {job_id}")
        
        try:
            with self.engine.connect() as conn:
                # Extract recruiter emails for job_configuration
                recruiter_emails = data.get("recruiter_emails", [])
                
                # Check if job exists in monitored_jobs by job_id OR jobdiva_id
                res = conn.execute(text("SELECT 1 FROM monitored_jobs WHERE job_id = :job_id OR jobdiva_id = :job_id"), {"job_id": job_id})
                exists = res.fetchone()
                
                if exists:
                    # Update monitored_jobs with ALL possible fields
                    import json
                    update_parts = []
                    params = {"job_id": job_id}
                    
                    # Define ALL possible columns that can be updated (including metrics fields)
                    valid_columns = [
                        # Core job identification
                        "job_id", "jobdiva_id", "title", "enhanced_title", "customer_name", "status",
                        
                        # Location information
                        "city", "state", "zip_code", "location_type",
                        
                        # Job details
                        "jobdiva_description", "ai_description", "recruiter_notes", 
                        "employment_type", "pay_rate", "openings", "work_authorization",
                        "posted_date", "start_date",

                        # Extended JobDiva fields
                        "priority", "program_duration", "max_allowed_submittals",
                        
                        # Application state
                        "processing_status", "processing_stage", "screening_level",
                        
                        # Lists and configurations
                        "selected_job_boards", "selected_employment_types", "recruiter_emails",
                        
                        # Metrics fields for UI display
                        "candidates_sourced", "resumes_shortlisted", "complete_submissions", 
                        "pass_submissions", "pair_external_subs", "feedback_completed", "time_to_first_pass"
                    ]
                    
                    # Fields where an empty string IS a valid intentional value (cleared UDFs or optional fields)
                    allow_empty_fields = {"recruiter_notes", "ai_description", "priority", "program_duration", "max_allowed_submittals"}
                    
                    for k, v in data.items():
                        if k in valid_columns:
                            # Skip None values always
                            if v is None:
                                continue
                            # For cleared-UDF fields, allow empty strings through
                            if v == "" and k not in allow_empty_fields:
                                continue
                                
                            # Special Protection: Never overwrite a real customer_name with "Unknown"
                            if k == "customer_name" and (str(v or "").lower() == "unknown" or not v):
                                # Skip this key to preserve the existing valid name in DB
                                continue
                            # Store [null] marker for new fields that have no JobDiva value
                            if v == "" and k in {"priority", "program_duration", "max_allowed_submittals"}:
                                v = "[null]"
                            # Clean location fields before storing
                            if k in ["city", "state", "zip"]:
                                v = _clean_location_field(v)
                                if not v:  # Skip empty location values
                                    continue

                                    
                            update_parts.append(f"{k} = :{k}")
                            if k in ["selected_employment_types", "selected_job_boards", "recruiter_emails", "enhancement_metadata"]:
                                if isinstance(v, (list, dict)):
                                    params[k] = json.dumps(v)
                                else:
                                    params[k] = v
                            else:
                                params[k] = v
                    
                    # Always update the timestamp
                    update_parts.append("updated_at = :updated_at")  
                    params["updated_at"] = readable_ist_now()
                    
                    if update_parts:
                        query = f"UPDATE monitored_jobs SET {', '.join(update_parts)} WHERE job_id = :job_id OR jobdiva_id = :job_id"
                        debug_log(f"Updating job {job_id} with fields: {list(params.keys())}")
                        conn.execute(text(query), params)
                    
                else:
                    # Insert into monitored_jobs with comprehensive field mapping
                    import json
                    params = {
                        "job_id": job_id,
                        
                        # Core job information
                        "status": data.get("status") or "OPEN",
                        "customer_name": data.get("customer_name") or "Unknown",
                        "title": data.get("title") or "",
                        
                        # Location information
                        "city": _clean_location_field(data.get("city")) or "",
                        "state": _clean_location_field(data.get("state")) or "",
                        "zip_code": _clean_location_field(data.get("zip_code") or data.get("zip")) or "",
                        "location_type": data.get("location_type") or "Onsite",
                        
                        # Job descriptions and content
                        "jobdiva_description": data.get("jobdiva_description") or "",
                        "ai_description": data.get("ai_description") or "",
                        "enhanced_title": data.get("enhanced_title") or data.get("title") or "",
                        "recruiter_notes": data.get("recruiter_notes") if data.get("recruiter_notes") is not None else (data.get("job_notes") or ""),
                        
                        # Employment details
                        "employment_type": data.get("employment_type") or "",
                        "work_authorization": data.get("work_authorization") or "",
                        "pay_rate": data.get("pay_rate") or "",
                        "openings": data.get("openings") or "",
                        
                        # Dates
                        "posted_date": data.get("posted_date") or "",
                        "start_date": data.get("start_date") or "",
                        
                        # Extended JobDiva fields — store [null] if not provided by JobDiva
                        "priority": data.get("priority") or "[null]",
                        "program_duration": data.get("program_duration") or "[null]",
                        "max_allowed_submittals": data.get("max_allowed_submittals") or "[null]",
                        
                        # Configuration and processing
                        "recruiter_emails": json.dumps(recruiter_emails) if recruiter_emails else '[]',
                        "selected_employment_types": json.dumps(data.get("selected_employment_types", [])),
                        "selected_job_boards": json.dumps(data.get("selected_job_boards", [])),
                        "screening_level": data.get("screening_level", "L1.5"),
                        "processing_status": data.get("processing_status", "pending"),
                        
                        # Identification
                        "job_id": job_id,
                        "jobdiva_id": data.get("jobdiva_id") or "",
                        
                        # Timestamps
                        "created_at": data.get("created_at") or readable_ist_now(),
                        "updated_at": readable_ist_now()
                    }
                    
                    # Build INSERT query dynamically based on available fields
                    columns = list(params.keys())
                    placeholders = [f":{col}" for col in columns]
                    
                    query = f"""
                        INSERT INTO monitored_jobs ({', '.join(columns)})
                        VALUES ({', '.join(placeholders)})
                    """
                    
                    debug_log(f"Inserting new job {job_id} with {len(columns)} fields")
                    conn.execute(text(query), params)

                conn.commit()
                debug_log(f"Successfully saved job {job_id} to monitored_jobs")
                return True
                
        except Exception as e:
            logger.error(f"Error monitoring job locally in DB: {e}")
            debug_log(f"Error monitoring job {job_id}: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            return False

    def get_locally_monitored_job(self, job_id: str) -> dict:
        if not self.engine:
             return {}
        try:
            with self.engine.connect() as conn:
                # Get job data from monitored_jobs - Search BOTH Numeric ID and Hyphenated ID
                res = conn.execute(
                    text("SELECT * FROM monitored_jobs WHERE job_id = :job_id OR jobdiva_id = :job_id"), 
                    {"job_id": job_id}
                )
                row = res.fetchone()
                if row:
                    job_data = dict(row._mapping)
                    
                    # Parse JSON fields if they exist
                    import json
                    for field in ["recruiter_emails", "selected_employment_types", "selected_job_boards"]:
                        if job_data.get(field):
                            try:
                                if isinstance(job_data[field], str):
                                    job_data[field] = json.loads(job_data[field])
                            except (json.JSONDecodeError, TypeError):
                                job_data[field] = []
                        else:
                            job_data[field] = []
                    
                    return job_data
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

    def update_job_basic_info(self, job_id: str, update_data: dict) -> bool:
        """Update basic job information like employment_type, recruiter_notes, work_authorization, and recruiter_emails."""
        if not self.engine:
            logger.error("Database engine not initialized for updating job basic info")
            return False
            
        try:
            with self.engine.connect() as conn:
                # Build update query dynamically based on provided fields
                update_parts = []
                params = {"job_id": job_id}
                
                # Valid fields that can be updated
                valid_fields = ["employment_type", "recruiter_notes", "work_authorization", "recruiter_emails"]
                
                for field, value in update_data.items():
                    if field in valid_fields and value is not None:
                        if field == "recruiter_emails":
                            # Handle JSONB array for recruiter_emails
                            import json
                            update_parts.append(f"{field} = :{field}")
                            params[field] = json.dumps(value if isinstance(value, list) else [])
                        else:
                            update_parts.append(f"{field} = :{field}")
                            params[field] = value
                
                # Auto-extract work authorization if not explicitly provided but other fields are being updated
                if "work_authorization" not in update_data or not update_data["work_authorization"]:
                    work_auth = self._auto_extract_work_authorization(conn, job_id)
                    if work_auth:
                        update_parts.append("work_authorization = :work_authorization")
                        params["work_authorization"] = work_auth
                        logger.info(f"Auto-extracted work authorization for job {job_id}: {work_auth}")
                
                if not update_parts:
                    logger.warning(f"No valid fields to update for job {job_id}")
                    return False
                
                # Add updated timestamp
                update_parts.append("updated_at = :updated_at")  
                params["updated_at"] = readable_ist_now()
                
                # Execute update query - use SQLAlchemy text with proper parameter binding
                query = f"UPDATE monitored_jobs SET {', '.join(update_parts)} WHERE job_id = :job_id"
                logger.info(f"Executing update query: {query}")
                logger.info(f"Parameters: {params}")
                
                result = conn.execute(text(query), params)
                conn.commit()
                
                # Check if any rows were updated
                if result.rowcount > 0:
                    logger.info(f"Updated basic info for job {job_id}: {update_data}")
                    return True
                else:
                    logger.warning(f"No job found with ID {job_id}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error updating basic info for job {job_id}: {e}")
            return False

    def _auto_extract_work_authorization(self, conn, job_id: str) -> str:
        """Auto-extract work authorization from AI JD, job notes, or JobDiva description."""
        try:
            # Get job content for analysis
            result = conn.execute(text("""
                SELECT ai_description, recruiter_notes, jobdiva_description, enhanced_title, title
                FROM monitored_jobs 
                WHERE job_id = :job_id
            """), {"job_id": job_id})
            
            row = result.fetchone()
            if not row:
                return ""
                
            # Combine all available text for analysis
            ai_description, recruiter_notes, jobdiva_desc, enhanced_title, title = row
            
            combined_text = []
            if ai_description:
                combined_text.append(ai_description)
            if recruiter_notes:
                combined_text.append(recruiter_notes) 
            if jobdiva_desc:
                combined_text.append(jobdiva_desc)
            if enhanced_title:
                combined_text.append(enhanced_title)
            if title:
                combined_text.append(title)
                
            full_text = " ".join(combined_text).lower()
            
            if not full_text.strip():
                return ""
            
            # Work authorization patterns (prioritized by specificity)
            work_auth_patterns = [
                # Specific visa types
                ("H1B Transfer", ["h1b transfer", "h-1b transfer"]),
                ("H1B", ["h1b", "h-1b", "h1-b"]),
                ("Green Card", ["green card", "greencard", "permanent resident", "pr holder"]),
                ("US Citizen", ["us citizen", "u.s. citizen", "american citizen", "citizenship required"]),
                ("TN Visa", ["tn visa", "tn-visa", "nafta"]),
                ("L1 Visa", ["l1 visa", "l-1 visa", "l1-visa"]),
                ("EAD", ["ead", "employment authorization", "work authorization document"]),
                ("OPT", ["opt", "optional practical training"]),
                ("CPT", ["cpt", "curricular practical training"]),
                ("F1 Visa", ["f1 visa", "f-1 visa"]),
                # General categories  
                ("Work Authorization Required", ["work authorization required", "must be authorized", "legal right to work"]),
                ("No Sponsorship", ["no sponsorship", "cannot sponsor", "will not sponsor", "unable to sponsor"]),
                ("Sponsorship Available", ["sponsorship available", "will sponsor", "can sponsor", "visa sponsorship"]),
                ("Any Work Authorization", ["any work authorization", "all work authorization"])
            ]
            
            # Check patterns in order of specificity
            for auth_type, patterns in work_auth_patterns:
                for pattern in patterns:
                    if pattern in full_text:
                        logger.info(f"Auto-extracted work authorization '{auth_type}' from pattern '{pattern}' for job {job_id}")
                        return auth_type
                        
            # Fallback: check for generic work authorization terms
            generic_terms = ["visa", "authorization", "citizen", "resident", "sponsorship"]
            if any(term in full_text for term in generic_terms):
                return "Work Authorization Required"
                
            return ""
            
        except Exception as e:
            logger.error(f"Error auto-extracting work authorization for job {job_id}: {e}")
            return ""
    
    async def search_job_candidates_enhanced(
        self, 
        job_id: str,
        title_criteria: List = None,
        skill_criteria: List = None, 
        location_criteria: List = None,
        legacy_skills: List = None
    ) -> List[Dict[str, Any]]:
        """
        Enhanced job applicant search with separate title, skill, and location criteria.
        Applies intelligent filtering to job applicants based on multiple criteria types.
        """
        logger.info(f"🎯 Enhanced job applicant search for job {job_id}")
        
        try:
            # Build search criteria from enhanced format
            search_skills = []
            search_location = ""
            
            # Convert title criteria to searchable skills format
            if title_criteria:
                for title in title_criteria:
                    search_skills.append({
                        "value": title.value,
                        "priority": "Must Have" if title.match_type == "must" else "Flexible", 
                        "years_experience": title.years
                    })
                    
            # Convert skill criteria to searchable format
            if skill_criteria:
                for skill in skill_criteria:
                    search_skills.append({
                        "value": skill.value,
                        "priority": "Must Have" if skill.match_type == "must" else "Flexible",
                        "years_experience": skill.years
                    })
            
            # Use location criteria for location filtering
            if location_criteria:
                search_location = location_criteria[0].value
                
            # Fallback to legacy format if enhanced criteria not provided
            if not search_skills and legacy_skills:
                search_skills = legacy_skills
                
            logger.info(f"📋 Search criteria - Skills: {len(search_skills)}, Location: '{search_location}'")
            
            # Use existing search_candidates method with job_id to get applicants
            return await self.search_candidates(
                skills=search_skills,
                location=search_location,
                job_id=job_id  # This triggers job applicant search with filtering
            )
            
        except Exception as e:
            logger.error(f"Enhanced job applicant search failed for {job_id}: {e}")
            return []
    
    async def search_talent_pool_enhanced(
        self,
        title_criteria: List = None,
        skill_criteria: List = None,
        location_criteria: List = None, 
        legacy_skills: List = None,
        page: int = 1,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Enhanced talent pool search with separate title, skill, and location criteria.
        Searches broader talent database with intelligent multi-criteria filtering.
        """
        logger.info(f"🌐 Enhanced talent pool search - Page {page}, Limit {limit}")
        
        try:
            # Build search criteria from enhanced format
            search_skills = []
            search_location = ""
            
            # Convert title criteria to searchable skills format
            if title_criteria:
                for title in title_criteria:
                    search_skills.append({
                        "value": title.value,
                        "priority": "Must Have" if title.match_type == "must" else "Flexible",
                        "years_experience": title.years
                    })
                    
            # Convert skill criteria to searchable format  
            if skill_criteria:
                for skill in skill_criteria:
                    search_skills.append({
                        "value": skill.value,
                        "priority": "Must Have" if skill.match_type == "must" else "Flexible",
                        "years_experience": skill.years
                    })
            
            # Use location criteria for location filtering
            if location_criteria:
                search_location = location_criteria[0].value
                
            # Fallback to legacy format if enhanced criteria not provided
            if not search_skills and legacy_skills:
                search_skills = legacy_skills
                
            logger.info(f"📋 Talent search criteria - Skills: {len(search_skills)}, Location: '{search_location}'")
            
            # Use existing search_candidates method without job_id for talent pool
            return await self.search_candidates(
                skills=search_skills,
                location=search_location,
                page=page,
                limit=limit,
                job_id=None  # None triggers talent pool search
            )
            
        except Exception as e:
            logger.error(f"Enhanced talent pool search failed: {e}")
            return []

    async def talent_search_api(self, search_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Call JobDiva TalentSearch API with hierarchical search payload
        """
        token = await self.authenticate()
        if not token:
            logger.error("❌ TalentSearch failed: Could not authenticate with JobDiva")
            return []
        
        url = f"{self.api_url}/apiv2/jobdiva/TalentSearch"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        try:
            logger.info(f"🌐 Calling JobDiva TalentSearch API with {len(search_payload.get('advancedSkills', []))} skills, {len(search_payload.get('titles', []))} titles")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=search_payload, headers=headers)
                
                if response.status_code == 200:
                    data = response.json()
                    raw_candidates = data if isinstance(data, list) else (data.get("candidates") or data.get("results") or [])
                    
                    # Convert JobDiva response to standardized format
                    candidates = []
                    for candidate_data in raw_candidates:
                        try:
                            candidate = self._standardize_talent_candidate(candidate_data)
                            if candidate:
                                candidates.append(candidate)
                        except Exception as e:
                            logger.error(f"❌ Error processing talent candidate: {e}")
                            continue
                    
                    logger.info(f"✅ TalentSearch API returned {len(candidates)} candidates")
                    return candidates
                    
                else:
                    logger.error(f"❌ TalentSearch API error: {response.status_code} - {response.text}")
                    return []
                    
        except Exception as e:
            logger.error(f"❌ TalentSearch API call failed: {e}")
            return []
    
    def _standardize_talent_candidate(self, candidate_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Convert JobDiva TalentSearch candidate to standardized format
        """
        try:
            # Extract basic info
            candidate_id = str(get_field(candidate_data, ["candidateId", "id", "ID"]) or "")
            if not candidate_id:
                return None
            
            first_name = get_field(candidate_data, ["firstName", "firstname", "FIRSTNAME"]) or ""
            last_name = get_field(candidate_data, ["lastName", "lastname", "LASTNAME"]) or ""
            name = f"{first_name} {last_name}".strip() or "Unknown Candidate"
            
            # Extract location
            city = get_field(candidate_data, ["city", "locationCity", "CITY"]) or ""
            state = get_field(candidate_data, ["state", "locationState", "STATE"]) or ""
            location = f"{city}, {state}".strip(", ") if city or state else ""
            
            # Extract skills
            skills_raw = get_field(candidate_data, ["skills", "SKILLS", "skillsList"]) or []
            skills = []
            if isinstance(skills_raw, str):
                skills = [skill.strip() for skill in skills_raw.split(",") if skill.strip()]
            elif isinstance(skills_raw, list):
                skills = [str(skill) for skill in skills_raw if skill]
            
            # Extract experience
            years_exp = 0
            exp_raw = get_field(candidate_data, ["experience", "yearsExperience", "totalExperience"]) or "0"
            try:
                years_exp = int(float(str(exp_raw)))
            except (ValueError, TypeError):
                years_exp = 0
            
            # Extract resume data
            resume_text = self._extract_resume_text(candidate_data) or ""
            resume_url = get_field(candidate_data, ["resumeUrl", "resume_url"]) or ""
            
            # Extract companies from resume text
            companies = self._extract_companies_from_resume(resume_text)
            
            return {
                "candidateId": candidate_id,
                "name": name,
                "firstName": first_name,
                "lastName": last_name,
                "email": get_field(candidate_data, ["email", "EMAIL"]) or "",
                "phone": get_field(candidate_data, ["phone", "PHONE", "phoneNumber"]) or "",
                "title": get_field(candidate_data, ["title", "currentTitle", "TITLE"]) or "",
                "location": location,
                "city": city,
                "state": state,
                "skills": skills,
                "experience": years_exp,
                "companies": companies,
                "resumeText": resume_text,
                "resumeUrl": resume_url,
                "source": "talent_search"
            }
            
        except Exception as e:
            logger.error(f"❌ Error standardizing talent candidate: {e}")
            return None
    
    def _extract_companies_from_resume(self, resume_text: str) -> List[str]:
        """
        Extract company names from resume text using simple pattern matching
        """
        if not resume_text:
            return []
        
        companies = []
        
        # Common patterns for company identification in resumes
        import re
        
        # Look for patterns like "Company Name, City" or "Company Name - Title"
        company_patterns = [
            r'(?:^|\n)([A-Z][A-Za-z\s&\.,-]+?)\s*(?:,\s*[A-Z]{2}|,\s*\w+\s*[A-Z]{2}|\s*-\s*)',
            r'(?:at|@)\s+([A-Z][A-Za-z\s&\.,-]+?)(?:\s*,|\s*\n|$)',
            r'(?:Company|Employer|Organization):\s*([A-Za-z\s&\.,-]+)',
        ]
        
        for pattern in company_patterns:
            matches = re.findall(pattern, resume_text, re.MULTILINE)
            for match in matches:
                company = match.strip()
                # Filter out common non-company words
                if (len(company) > 2 and 
                    company not in ['Inc', 'LLC', 'Corp', 'Ltd', 'Company'] and
                    not any(word in company.lower() for word in ['experience', 'education', 'skills', 'summary'])):
                    companies.append(company)
        
        # Remove duplicates and limit to reasonable number
        unique_companies = list(dict.fromkeys(companies))[:10]
        return unique_companies

jobdiva_service = JobDivaService()
