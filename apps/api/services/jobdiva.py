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
    
    return ""
  
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
                self.engine = sqlalchemy.create_engine(self.db_url)
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
                salary_range_udf = None
                issued_date_udf = None
                for k, v in u_fields.items():
                    k_low = k.lower()
                    if "ai job description" in k_low: ai_description = v
                    if "job notes" in k_low or k == "231": job_notes = v
                    if "salary range" in k_low or "pay range" in k_low or "pay rate" in k_low: salary_range_udf = v
                    if "issued date" in k_low or "posted date" in k_low or "date issued" in k_low or k_low == "issued" or k_low == "posted": issued_date_udf = v

                customer_name = str(get_field(j, ["customer", "company"]) or "").title() or "Unknown Customer"
                description = format_job_description(get_field(j, ["job description", "description"]) or "")

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

                # Advanced pay_rate logic: try to combine min and max if available for a range
                p_min = get_field(j, ["minpayrate", "min_pay_rate", "minimum_pay", "payRateMin"])
                p_max = get_field(j, ["maxpayrate", "max_pay_rate", "maximum_pay", "payRateMax"])
                p_range = f"${p_min} - ${p_max}" if p_min and p_max else (p_min or p_max or "")
                
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
                
                return {
                    "id": get_field(j, ["id", "jobId"]),
                    "jobdiva_id": get_field(j, ["jobdivano", "reference #", "refno", "jobdivaref", "ref"]),
                    "title": get_field(j, ["job title", "title"]),
                    "description": description,
                    "jobdiva_description": description, # Clarified for schema
                    "ai_description": ai_description if ai_description is not None else "",
                    "recruiter_notes": job_notes if job_notes is not None else "",
                    "customer_name": customer_name,
                    "job_status": get_field(j, ["job status", "status"]) or "OPEN",
                    "status": get_field(j, ["job status", "status"]) or "OPEN", # Database standard
                    "city": _clean_location_field(get_field(j, ["city", "jobCity", "locationCity", "worksitecity"])),
                    "state": _clean_location_field(get_field(j, ["state", "jobState", "locationState", "worksitestate", "province"])),
                    "zip": _clean_location_field(get_field(j, ["zip", "postalCode", "zipcode", "postalcode", "worksitezip", "worksitepostalcode"])),
                    "start_date": normalize_jobdiva_date(get_field(j, ["start date", "startDate", "available", "startdate"]) or (local_data.get("start_date") if local_data else "")),
                    "issued_date": normalize_jobdiva_date(issued_date_udf or get_field(j, ["issued date", "issueddate", "issued_date", "issued"]) or (local_data.get("issued_date") if local_data else "")),
                    "posted_date": normalize_jobdiva_date(get_field(j, ["posted date", "date", "created date", "posted", "posteddate", "createtimestamp", "date_posted", "posted_at", "start date", "startDate", "available", "startdate"]) or issued_date_udf or get_field(j, ["issued date", "issueddate", "issued_date", "issued"]) or extract_posted_date_from_text(description) or (local_data.get("posted_date") if local_data else "")) or get_fallback_posted_date(),
                    "location_type": loc_type,
                    "work_authorization": get_field(j, ["work_authorization", "visa", "legal status", "workauth", "work_auth", "work authorization"]) or (local_data.get("work_authorization") if local_data else ""),
                    
                    # Extract multiple recruiter emails from JobDiva API - store in job_configuration only
                    "recruiter_emails": extract_multiple_recruiter_emails(j),
                    
                    "pay_rate": salary_range_udf or p_range or get_field(j, ["pay rate", "salary range", "salary", "rate", "bill rate", "compensation", "billrate", "payrate"]) or extract_pay_rate_from_text(description) or (local_data.get("pay_rate") if local_data else ""),
                    "openings": get_field(j, ["openings", "maxReturned", "positions", "number of openings", "openpositions"]) or (local_data.get("openings") if local_data else ""),
                    "employment_type": normalize_employment_type(get_field(j, ["employment type", "jobType", "assignmentType"]) or (local_data.get("employment_type") if local_data else "")),
                    "required_degree": get_field(j, ["required degree", "required_degree", "criteria degree", "criteria_degree"]) or ""
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
                
                # Check if job exists in monitored_jobs
                res = conn.execute(text("SELECT 1 FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
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
                        "city", "state", "zip", "location_type",
                        
                        # Job details
                        "jobdiva_description", "ai_description", "recruiter_notes", 
                        "employment_type", "pay_rate", "openings", "work_authorization",
                        "posted_date", "start_date",
                        
                        # Application state
                        "processing_status", "processing_stage", "screening_level",
                        
                        # Lists and configurations
                        "selected_job_boards", "selected_employment_types", "recruiter_emails",
                        
                        # Metrics fields for UI display
                        "candidates_sourced", "resumes_shortlisted", "complete_submissions", 
                        "pass_submissions", "pair_external_subs", "feedback_completed", "time_to_first_pass"
                    ]
                    
                    # Fields where an empty string IS a valid intentional value (cleared UDFs)
                    allow_empty_fields = {"recruiter_notes", "ai_description"}
                    
                    for k, v in data.items():
                        if k in valid_columns:
                            # Skip None values always
                            if v is None:
                                continue
                            # For cleared-UDF fields, allow empty strings through
                            if v == "" and k not in allow_empty_fields:
                                continue
                            # Clean location fields before storing
                            if k in ["city", "state", "zip"]:
                                v = _clean_location_field(v)
                                if not v:  # Skip empty location values
                                    continue
                                    
                            update_parts.append(f"{k} = :{k}")
                            if k in ["selected_employment_types", "selected_job_boards", "enhancement_metadata"]:
                                if isinstance(v, (list, dict)):
                                    params[k] = json.dumps(v)
                                else:
                                    params[k] = v
                            else:
                                params[k] = v
                    
                    # Handle recruiter emails separately
                    if recruiter_emails:
                        update_parts.append("recruiter_emails = :recruiter_emails")
                        params["recruiter_emails"] = json.dumps(recruiter_emails) if isinstance(recruiter_emails, list) else recruiter_emails
                    
                    # Always update the timestamp
                    update_parts.append("updated_at = :updated_at")  
                    params["updated_at"] = readable_ist_now()
                    
                    if update_parts:
                        query = f"UPDATE monitored_jobs SET {', '.join(update_parts)} WHERE job_id = :job_id"
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
                        "zip": _clean_location_field(data.get("zip")) or "",
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
                # Get job data from monitored_jobs (now includes all configuration data)
                res = conn.execute(text("SELECT * FROM monitored_jobs WHERE job_id = :job_id"), {"job_id": job_id})
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

jobdiva_service = JobDivaService()
