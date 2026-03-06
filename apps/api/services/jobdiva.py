import os
import time
import httpx
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from html import unescape


logger = logging.getLogger(__name__)


# Helper function for readable IST timestamps
def readable_ist_now() -> str:
   """Returns current IST time in readable format: 2026-02-24 16:25:59 IST"""
   ist = timezone(timedelta(hours=5, minutes=30))
   return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")


# Helper function for case-insensitive/multi-key dictionary access
def get_field(data: Dict[str, Any], keys: List[str], default: Any = None) -> Any:
   """
   Safely extract a value from a dictionary by checking multiple potential keys
   case-insensitively and ignoring non-alphanumeric characters.
   """
   if not isinstance(data, dict):
       return default
      
   def normalize(s):
       return re.sub(r'[^a-zA-Z0-9]', '', str(s).lower())
      
   # Standardize all keys in the data to normalized lowercase
   normalized_data = {normalize(k): v for k, v in data.items()}
  
   for key in keys:
       norm_key = normalize(key)
       if norm_key in normalized_data:
           return normalized_data[norm_key]
          
   return default


def format_job_description(raw_desc: str) -> str:
   """
   Format raw job description with minimal changes - keep exact text, just clean HTML.
   No automatic headers, word-for-word preservation of JobDiva content.
   """
   if not raw_desc or not raw_desc.strip():
       return "No job description available."
  
   # Convert HTML entities and clean basic HTML tags
   desc = unescape(raw_desc)
  
   # Remove common HTML tags but preserve line breaks
   import re
   desc = re.sub(r'<br\s*/?>', '\n', desc)
   desc = re.sub(r'<p>', '\n', desc)
   desc = re.sub(r'</p>', '\n', desc)
   desc = re.sub(r'<div[^>]*>', '\n', desc)
   desc = re.sub(r'</div>', '\n', desc)
   desc = re.sub(r'<[^>]*>', '', desc)  # Remove remaining HTML tags
  
   # Clean up excessive whitespace and normalize line breaks
   desc = re.sub(r'\n\s*\n\s*\n+', '\n\n', desc)  # Max 2 consecutive line breaks
   desc = re.sub(r'[ \t]+', ' ', desc)  # Normalize spaces and tabs
   desc = desc.strip()
  
   # Return exactly as is - no headers, no modifications
   return desc




class JobDivaService:
   def __init__(self):
       self.api_url = os.getenv("JOBDIVA_API_URL", "https://api.jobdiva.com")
       self.client_id = os.getenv("JOBDIVA_CLIENT_ID", "mock-client")
       self.username = os.getenv("JOBDIVA_USERNAME", "mock-user")
       self.password = os.getenv("JOBDIVA_PASSWORD", "mock-pass")
      
       self.cached_token = None
       self.token_expiry = 0


   async def authenticate(self) -> str:
       """Authenticate with JobDiva and return JWT token."""
       if self.cached_token and time.time() < self.token_expiry:
           return self.cached_token
      
       # Ensure we have real credentials
       if not self.client_id or self.client_id == "mock-client":
            logger.error("JobDiva Credentials not configured.")
            return None


       auth_url = f"{self.api_url}/api/authenticate"
       params = {
           "clientid": self.client_id,
           "username": self.username,
           "password": self.password
       }


       try:
           async with httpx.AsyncClient() as client:
               logger.info(f"Authenticating to JobDiva: {self.username}")
               response = await client.get(auth_url, params=params)
              
               if response.status_code != 200:
                   logger.error(f"JobDiva Auth Failed: {response.status_code} - {response.text}")
                   return None
              
               token = response.text.replace('"', '')
               if len(token) < 10:
                   return None


               self.cached_token = token
               self.token_expiry = time.time() + (23 * 3600)
               return token


       except Exception as e:
           logger.error(f"JobDiva Auth Exception: {repr(e)}")
           return None


   async def search_candidates(self, skills: List[Any], location: str, page: int = 1, limit: int = 100) -> List[Dict[str, Any]]:
       """
       Search for candidates based on skills and location.
       Merges JobDiva results with Mock Vetted Database results.
       """
       print(f"🔥 DEBUG: search_candidates called for location='{location}'")
       from models import Skill
      
       token = await self.authenticate()
       print(f"🔥 DEBUG: Auth Token: {token[:10]}..." if token else "🔥 DEBUG: Auth Token is NONE")


       jd_results = []
      
       # Build Criteria String
       must_haves = []
       flexible = []
      
       print(f"🔥 DEBUG: Received {len(skills)} skills for search.")
       if skills:
           print(f"🔥 DEBUG: First skill sample: {skills[0]}")


       for s in skills:
           # Handle Dict, Object, or String
           s_name = ""
           s_prio = ""
           s_seniority = "Mid"
           s_years = None
          
           if isinstance(s, dict):
               s_name = s.get("name", "")
               s_prio = s.get("priority", "Must Have")
               s_seniority = s.get("seniority", "Mid")
               s_years = s.get("years_experience")
           elif hasattr(s, "name"): # Pydantic or Class
               s_name = s.name
               s_prio = s.priority
               s_seniority = getattr(s, "seniority", "Mid")
               s_years = getattr(s, "years_experience", None)
           else:
               # Fallback for simple strings (default to Junior/Broad search to avoid over-filtering)
               s_name = str(s)
               s_prio = "Must Have"
               s_seniority = "Junior"


           if not s_name: continue


           # Determine Years for "RECENT OVER X YRS"
           # 1. Use explicit years if available
           # 2. Map Seniority if not
           years = 0
           if s_years is not None:
                try: years = int(s_years)
                except: pass
           else:
               sen = str(s_seniority).lower()
               if "senior" in sen: years = 5
               elif "mid" in sen: years = 3 # Matches user request example
               # "junior" or others -> 0
          
           # Format Term
           if years > 0:
               # Match JobDiva UI syntax: UPPERCASE skill, lowercase "recent over" and "years"
               term = f'("{s_name.upper()}" recent over {years} years)'
           else:
               # Standard Syntax: "SQL" (uppercase)
               term = f'"{s_name.upper()}"'


           s_prio_norm = s_prio.lower().strip().replace("_", " ")
          
           if "must" in s_prio_norm:
               must_haves.append(term)
           else:
               flexible.append(term)


       # Construct Boolean Search Query
       # Strategy: Strictly enforce Must Haves. Flexible skills are for ranking, not filtering.
       # However, to avoid HUGE result sets, we can try to include them if desired, but safely.
       # Current fix: Only restrict by Must Haves for retrieval to ensure accuracy.
       # If no Must Haves, use OR of Flexible.
      
       criteria_parts = []
      
       if must_haves:
           # Enforce (A AND B AND C)
           must_section = " AND ".join(must_haves)
           criteria_parts.append(f"({must_section})")
          
           # Note: We intentionally do NOT append flexible skills to the *mandatory* AND query
           # because that would exclude candidates who have Must Haves but zero Flexible skills.
           # We want to retrieve them (and rank them low later) rather than hide them.
       elif flexible:
           # If no must haves, at least match ONE flexible
           flex_section = " OR ".join(flexible)
           criteria_parts.append(f"({flex_section})")
          
       # Add Location to Search Criteria if provided (Strict Filtering)
       if location and location.strip():
            criteria_parts.append(f'"{location.strip()}"')
          
       # SAFETY CHECK: If skills were provided but criteria is empty (e.g. all empty strings), abort
       if skills and not criteria_parts and not location:
            print("⚠️ DEBUG: Skills provided but no criteria generated. Aborting to avoid Wildcard.")
            return []


       search_value = " AND ".join(criteria_parts) if criteria_parts else "*"
      
       print(f"🔥 DEBUG: Candidate Search Criteria: {search_value}")


       if token and token != "mock-token-123":
           # Correct Endpoint from Reference: TalentSearch
           url = f"{self.api_url}/apiv2/jobdiva/TalentSearch"
           headers = {"Authorization": f"Bearer {token}"}
          
           # WATERFALL STRATEGY: Robust Search to Handle Timeouts & Strictness
           # 1. Smart Boolean: Uses synonyms (e.g. "Data Analysis" OR "Data Science"). High Relevance, Risk of Timeout.
           # 2. Strict Boolean: Uses exact terms (e.g. "Data Analysis"). Max Relevance, Fast, Risk of 0 Results.
           # 3. Loose OR: Uses simple list (e.g. "Data Analysis", "SQL"). Min strictness, Fast, Risk of Noise.
          
           raw_list = [] # Final list of candidates to map
          
           # Helper: Smart Synonyms
           def build_smart_query(skills_list):
               terms = []
               for s in skills_list:
                   name = s.get("name") if isinstance(s, dict) else (s.name if hasattr(s, "name") else str(s))
                   if not name: continue
                   t_lower = name.lower().strip()
                   if "data analysis" in t_lower:
                       terms.append(f'("{name}" OR "Data Analytics" OR "Data Science")')
                   elif "sql" == t_lower:
                       terms.append(f'("SQL" OR "MySQL")')
                   elif "python" == t_lower:
                       terms.append(f'("Python" OR "Pandas")')
                   elif "react" in t_lower:
                       terms.append(f'("React" OR "ReactJS")')
                   else:
                       terms.append(f'"{name}"')
               return " AND ".join(terms)


           # Helper: Strict Terms
           def build_strict_query(skills_list):
               terms = []
               for s in skills_list:
                   name = s.get("name") if isinstance(s, dict) else (s.name if hasattr(s, "name") else str(s))
                   # Use unquoted if boolean validation showed it works better?
                   # Script showed unquoted "SQL AND Data Analysis" worked.
                   if name: terms.append(f'"{name}"')
               return " AND ".join(terms)
          
           # Helper: Loose List
           def build_loose_query(skills_list):
               terms = []
               for s in skills_list:
                   name = s.get("name") if isinstance(s, dict) else (s.name if hasattr(s, "name") else str(s))
                   if name: terms.append(name) # Simple string list
               return terms


           # Define Strategies (Name, PayloadBuilder, Timeout)
           strategies = [
               ("Smart Boolean", lambda: {"skills": [build_smart_query(skills)]}, 15.0), # Fast timeout to failover
               ("Strict Boolean", lambda: {"skills": [build_strict_query(skills)]}, 15.0),
               ("Loose OR", lambda: {"skills": build_loose_query(skills)}, 30.0) # Longer timeout for loose search
           ]
          
           raw_list = [] # Final list of candidates to map
          
           for strategy_name, payload_builder, timeout_sec in strategies:
               try:
                   current_payload = payload_builder()
                   current_payload["pageNumber"] = page
                   current_payload["pageSize"] = limit
                  
                   # Add location
                   if location and location.strip():
                       loc_parts = [p.strip() for p in location.split(',')]
                       if len(loc_parts) > 1:
                           current_payload["states"] = [loc_parts[-1]]
                       else:
                           current_payload["states"] = [location.strip()]


                   print(f"🔥 DEBUG: Strategy '{strategy_name}' Payload: {current_payload}")
                  
                   async with httpx.AsyncClient(timeout=timeout_sec) as client:
                       response = await client.post(url, json=current_payload, headers=headers)
                      
                       if response.status_code == 200:
                           data = response.json()
                           batch_results = []
                           if isinstance(data, list):
                               batch_results = data
                           elif isinstance(data, dict):
                               batch_results = data.get("candidates") or data.get("data") or []
                          
                           count = len(batch_results)
                           print(f"🔥 DEBUG: Strategy '{strategy_name}' found {count} candidates.")
                          
                           # LOGIC FIX: Always keep the BEST result set (most candidates)
                           # Or if we have 0, take anything.
                           # If we have 1, and new strategy gives 10, take 10.
                           # If we have 1, and new strategy gives 0, KEEP 1.
                          
                           if count > len(raw_list):
                               raw_list = batch_results
                              
                           if count >= 5:
                               print(f"✅ Strategy '{strategy_name}' Sufficient. Stopping.")
                               break # Stop waterfall, we have plenty of results
                           elif count > 0:
                               print(f"⚠️ Strategy '{strategy_name}' found {count} (Best so far: {len(raw_list)}). Trying next for more...")
                               continue
                           else:
                               print(f"⚠️ Strategy '{strategy_name}' returned 0 results. Falling back...")
                               continue
                       else:
                           print(f"❌ Strategy '{strategy_name}' Failed: {response.status_code}. Falling back...")
                           continue
                          
               except Exception as e:
                   print(f"❌ Strategy '{strategy_name}' Exception: {type(e).__name__}. Falling back...")
                   continue
          
           print(f"🔥 DEBUG: Final JobDiva Candidates Found: {len(raw_list)}")


           for c in raw_list:
               # Map fields robustly using case-insensitive helper
               c_id = get_field(c, ["id", "candidateId", "candidate_id"])
               if not c_id:
                   logger.warning(f"Candidate missing ID. Keys: {list(c.keys())}")
                   continue
              
               jd_results.append({
                   "id": str(c_id),
                   "firstName": get_field(c, ["firstName", "first_name"]) or "Unknown",
                   "lastName": get_field(c, ["lastName", "last_name"]) or "Candidate",
                   "email": get_field(c, ["email"]) or "",
                   "city": get_field(c, ["city"]) or "",
                   "state": get_field(c, ["state"]) or "",
                   "title": get_field(c, ["title", "abstract", "jobTitle"]) or "",
                   "source": "JobDiva",
                   "match_score": 0
               })
          
           logger.info(f"JobDiva processed {len(jd_results)} candidates.")
              
           # POST-FILTERING for STRICT Location
           if location and location.strip():
               target_loc = location.lower().strip()
               filtered_results = []
               for cand in jd_results:
                   # Check if city or state matches target
                   # Simple check: is "New York" in "New York, NY"? Yes.
                   c_city = (cand.get("city") or "").lower()
                   c_state = (cand.get("state") or "").lower()
                  
                   # If target is in city or state, keep it.
                   # Split target by comma if "City, State" provided
                   target_parts = [p.strip() for p in target_loc.split(',')]
                  
                   match = False
                   for part in target_parts:
                       if part in c_city or part in c_state:
                           match = True
                           break
                  
                   if match:
                       filtered_results.append(cand)
                   else:
                       pass
                      
               print(f"🔥 DEBUG: Location Filter: Reduced {len(jd_results)} to {len(filtered_results)} candidates.")
               jd_results = filtered_results
           else:
               print(f"🔥 DEBUG: No location filter applied. Returning {len(jd_results)} candidates.")


       return jd_results


   async def get_job_by_id(self, job_id: str) -> Optional[Dict[str, Any]]:
       """Fetch a specific job by ID from JobDiva."""
       logger.info(f"Attempting to fetch JobDiva ID: {job_id}")
       token = await self.authenticate()
       if not token:
           logger.error("JobDiva Authentication failed (token is None).")
           return None


       # Real Implementation - Using SearchJob with strict manual verification
       url = f"{self.api_url}/apiv2/jobdiva/SearchJob"
       headers = {"Authorization": f"Bearer {token}"}
      
       # Determine if Ref Number (has hyphen) or internal ID
       is_ref = "-" in job_id
       payload = {}
      
       if is_ref:
           payload = {"jobdivaref": job_id, "maxReturned": 1}
       else:
           # Strip non-numeric and treat as internal Job ID
           safe_id = "".join(filter(str.isdigit, job_id))
           if safe_id:
               payload = {"jobOrderId": int(safe_id), "maxReturned": 1}
           else:
               logger.warning(f"Invalid Job ID format: {job_id}")
               return None


       try:
           logger.info(f"Searching JobDiva: {url} with payload {payload}")
           async with httpx.AsyncClient(timeout=10.0) as client:
               response = await client.post(url, json=payload, headers=headers)
              
               if response.status_code != 200:
                   logger.error(f"JobDiva Search Error: {response.status_code} - {response.text}")
                   return None
          
               data = response.json()
               jobs_list = []
               if isinstance(data, list):
                   jobs_list = data
               elif isinstance(data, dict) and "data" in data:
                   jobs_list = data.get("data", [])
              
               if not jobs_list:
                   logger.warning(f"Job {job_id} not found in JobDiva results.")
                   return None


               j = jobs_list[0]
              
               # STRICT MATCH: JobDiva's SearchJob is fuzzy. We MUST verify the result.
               returned_id = get_field(j, ["id", "jobOrderId", "jobId"])
               returned_ref = get_field(j, ["reference", "ref", "jobdivaref", "reference #"])
              
               # If we searched by Ref, ensure the Ref matches exactly
               if is_ref:
                   if str(returned_ref).strip() != str(job_id).strip():
                       logger.warning(f"Ghost Data Blocked: Ref mismatch. Expected {job_id}, got {returned_ref}. Returning None.")
                       return None
               else:
                   # If we searched by ID, ensure the ID matches exactly
                   if str(returned_id).strip() != str(job_id).strip():
                       logger.warning(f"Ghost Data Blocked: ID mismatch. Expected {job_id}, got {returned_id}. Returning None.")
                       return None
              
               # Extract Metadata Use new get_field to handle casing
               raw_job_desc = get_field(j, ["job description", "description"]) or ""
               raw_posting_desc = get_field(j, ["posting description"]) or ""
              
               # Prioritize full job description over marketing posting description
               raw_description = raw_job_desc if raw_job_desc.strip() else raw_posting_desc
               description = format_job_description(raw_description)


               company_raw = get_field(j, ["company"])
               company_name = str(company_raw).title() if company_raw else None
              
               customer_raw = get_field(j, ["customer", "company"])
               customer_name = str(customer_raw).title() if customer_raw else "Unknown Customer"


               return {
                   "id": get_field(j, ["id", "jobId"]),
                   "title": get_field(j, ["job title", "title"]),
                   "description": description,
                   "city": get_field(j, ["city"]),
                   "state": get_field(j, ["state"]),
                   "company": company_name,
                   "customer_name": customer_name,
                   "job_status": get_field(j, ["job status", "status"]) or "OPEN"
               }


       except Exception as e:
           logger.error(f"JobDiva Search Exception: {e}")
           return None


   async def get_candidate_resume(self, candidate_id: str) -> Optional[str]:
       """
       Fetches the resume text for a candidate.
       Cascading strategy: Plain Text -> Base64 -> Candidate Profile -> Error Message.
       """
       token = await self.authenticate()
       if not token:
           logger.error("Authentication failed during resume fetch.")
           return "Authentication failed. Please check JobDiva credentials."


       try:
           async with httpx.AsyncClient(timeout=10.0) as client:
               headers = {"Authorization": f"Bearer {token}"}
               resume_text = None
              
               try:
                   # Step 1: Try to get Resume ID
                   resumes_url = f"{self.api_url}/apiv2/bi/CandidateResumesDetail"
                   resp = await client.get(resumes_url, params={"candidateId": candidate_id}, headers=headers)
                  
                   logger.info(f"Resume Search Response ({resp.status_code}) for {candidate_id}")
                  
                   resume_id = None
                   if resp.status_code == 200:
                       data = resp.json()
                       records = []
                       if isinstance(data, dict): records = data.get("data", [])
                       elif isinstance(data, list): records = data


                       if records and isinstance(records, list):
                           # Sort by date if possible? For now take first.
                           first = records[0]
                           resume_id = first.get("RESUMEID") or first.get("resumeId") or first.get("resumeID")


                   # Step 2: If we have ID, Try to get Content
                   if resume_id:
                       detail_url = f"{self.api_url}/apiv2/bi/ResumeDetail"
                       resp_det = await client.get(detail_url, params={"resumeId": resume_id}, headers=headers)
                       logger.info(f"Resume Detail Response ({resp_det.status_code}) for {resume_id}")


                       if resp_det.status_code == 200:
                           d_data = resp_det.json()
                           recs = []
                           if isinstance(d_data, dict): recs = d_data.get("data", [])
                           elif isinstance(d_data, list): recs = d_data
                              
                           if recs and isinstance(recs, list):
                               r = recs[0]
                               # Try plaintext
                               text = get_field(r, ["PLAINTEXT", "plainText", "text"])
                               if text:
                                   return unescape(text)
                              
                               # Try Base64
                               import base64
                               b64_content = r.get("BINARYDATA") or r.get("binaryData")
                               if b64_content:
                                   try:
                                       decoded_bytes = base64.b64decode(b64_content)
                                       try: return decoded_bytes.decode('utf-8')
                                       except UnicodeDecodeError: return decoded_bytes.decode('latin-1')
                                   except Exception as e:
                                       logger.error(f"Base64 Decode Error: {e}")


               except Exception as e:
                   logger.error(f"Resume Fetch Error: {e}")


               # FALLBACK: Fetch Candidate Profile if Resume failed (at any step)
               try:
                   logger.info(f"Falling back to Profile for {candidate_id}")
                   profile_url = f"{self.api_url}/apiv2/jobdiva/SearchCandidateProfile"
                   p_resp = await client.get(profile_url, params={"candidateId": candidate_id}, headers=headers)
                  
                   if p_resp.status_code == 200:
                           p_data = p_resp.json()
                           if isinstance(p_data, list) and p_data: p = p_data[0]
                           elif isinstance(p_data, dict): p = p_data.get("data", [{}])[0] if "data" in p_data else p_data
                           else: p = {}
                          
                           if p:
                               txt = f"**Candidate Profile (Resume Request Failed)**\n\n"
                               txt += f"**Name:** {p.get('firstName')} {p.get('lastName')}\n"
                               txt += f"**Email:** {p.get('email')}\n"
                               txt += f"**Phones:** {p.get('homePhone')} / {p.get('cellPhone')}\n"
                               txt += f"**City/State:** {p.get('city')}, {p.get('state')}\n\n"
                               txt += f"**Qualifications:**\n{p.get('qualifications') or 'N/A'}\n"
                               return txt
               except Exception as e:
                   logger.error(f"Profile Fallback Error: {e}")


               return f"Resume content unavailable for Candidate {candidate_id}. (404 Not Found at source)"


       except Exception as e:
           logger.error(f"Get Resume Error: {e}")
           return f"Error fetching resume: {str(e)}"


   async def get_job_status(self, job_id: str) -> Dict[str, Any]:
       """
       Fetch just the status and critical metadata for efficient checks.
       Useful for syncing JobDiva status with local DB.
       """
       try:
           # Note: We reuse get_job_by_id for now as it makes the same API call.
           # In future, if JobDiva has a lighter endpoint, use that.
           job = await self.get_job_by_id(job_id)
          
           if not job:
               logger.warning(f"Job {job_id} not found in JobDiva")
               return {
                   "job_id": job_id,
                   "status": "NOT_FOUND",
                   "customer": "Unknown",
                   "synced_at": readable_ist_now()
               }
          
           logger.info(f"Job data for {job_id}: {job}")
          
           # Extract status with multiple fallbacks
           job_status = job.get("job_status") or job.get("status") or "OPEN"
          
           result = {
               "job_id": job_id,  # Preserve original reference ID
               "status": job_status,
               "customer": job.get("customer_name") or job.get("company") or "Unknown Customer",
               "title": job.get("title", ""),
               "synced_at": readable_ist_now()
           }
          
           logger.info(f"Returning status info: {result}")
           return result
          
       except Exception as e:
           logger.error(f"Sync Status Failed for {job_id}: {e}")
           return {"job_id": job_id, "status": "ERROR", "error": str(e)}
  
   async def get_multiple_jobs_status(self, job_ids: List[str]) -> List[Dict[str, Any]]:
       """
       Batch fetch status for multiple jobs efficiently.
       Used by the polling/cron system.
       """
       results = []
       for job_id in job_ids:
           status_info = await self.get_job_status(job_id)
           results.append(status_info)
       return results




   async def update_job_user_fields(self, job_id: str, fields: list) -> bool:
      import json as _json
      token = await self.authenticate()
      if not token:
         return False
      internal_id = job_id
      if "-" in str(job_id):
         job_data = await self.get_job_by_id(job_id)
         if job_data and job_data.get("id"):
            internal_id = job_data["id"]
         else:
            return False
      url = f"{self.api_url}/apiv2/jobdiva/updateJob"
      headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
      # Normalize field keys to match the exact Swagger schema:
      # payload key = 'Userfields' (capital U), inner keys = 'userfieldId' + 'userfieldValue'
      normalized_fields = [
         {"userfieldId": f.get("userfieldId"), "userfieldValue": f.get("userfieldValue") or f.get("value", "")}
         for f in fields
      ]
      payload = {"jobid": int(internal_id), "Userfields": normalized_fields}
      try:
         import httpx as _httpx
         async with _httpx.AsyncClient(timeout=15.0) as client:
            logger.info(f"Pushing UDFs to JobDiva for {job_id} -> internal {internal_id}")
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 200:
               logger.info(f"JobDiva UDFs updated for {job_id}")
               return True
            logger.error(f"JobDiva UDF update failed ({response.status_code}): {response.text}")
            return False
      except Exception as e:
         logger.error(f"UDF update exception: {e}")
         return False

   def monitor_job_locally(self, job_id: str, data: dict) -> bool:
      import json as _json
      file_path = "monitored_jobs.json"
      try:
         db = {"jobs": {}, "last_sync": None}
         if os.path.exists(file_path):
            with open(file_path, "r") as f:
               try:
                  db = _json.load(f)
               except Exception:
                  pass
         jobs = db.setdefault("jobs", {})
         entry = jobs.get(job_id, {})
         entry.update(data)
         entry["last_updated"] = readable_ist_now()
         jobs[job_id] = entry
         with open(file_path, "w") as f:
            _json.dump(db, f, indent=2)
         logger.info(f"Locally tracked ai_description + job_notes for {job_id}")
         return True
      except Exception as e:
         logger.error(f"Local tracking failed for {job_id}: {e}")
         return False


jobdiva_service = JobDivaService()
