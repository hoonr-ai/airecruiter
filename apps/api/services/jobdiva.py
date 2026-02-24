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
        
        # MOCK MODE if no credentials
        if self.client_id == "mock-client":
             return "mock-token-123"

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

            import uuid
            for c in raw_list:
                # Map fields based on reference JobDivaCandidate interface
                # ID, FIRSTNAME, LASTNAME, EMAIL, CITY, STATE, COUNTRY, ABSTRACT(jobTitle)
                # Handle case-insensitive keys or variations
                c_id = c.get("id") or c.get("candidateId") or c.get("ID") or c.get("CANDIDATEID")
                if not c_id:
                    print(f"⚠️ Candidate missing ID. Keys: {c.keys()}")
                    c_id = str(uuid.uuid4()) # Fallback to random ID if missing
                
                
                jd_results.append({
                    "id": str(c_id),
                    "firstName": c.get("firstName") or c.get("FIRSTNAME") or "Unknown",
                    "lastName": c.get("lastName") or c.get("LASTNAME") or "Candidate",
                    "email": c.get("email") or c.get("EMAIL") or "",
                    "city": c.get("city") or c.get("CITY") or "",
                    "state": c.get("state") or c.get("STATE") or "",
                    "title": c.get("title") or c.get("TITLE") or c.get("ABSTRACT") or "",
                    "source": "JobDiva",
                    "match_score": 0
                })
            
            print(f"🔥 DEBUG: JobDiva returned {len(jd_results)} candidates")
                
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

        # MOCK IMPLEMENTATION IF MOCK TOKEN
        if token == "mock-token-123":
             jd_results = [
                 {"id": "101", "firstName": "Alice", "lastName": "Mock", "city": "New York", "state": "NY", "email": "alice@example.com", "source": "JobDiva", "match_score": 85},
                 {"id": "102", "firstName": "Bob", "lastName": "Builder", "city": "San Francisco", "state": "CA", "email": "bob@example.com", "source": "JobDiva", "match_score": 78}
             ]

        return jd_results

    async def get_job_by_id(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a specific job by ID from JobDiva."""
        logger.info(f"Attempting to fetch JobDiva ID: {job_id}")
        token = await self.authenticate()
        if not token:
            logger.error("JobDiva Authentication failed (token is None).")
            return None
            
        # MOCK IMPLEMENTATION
        if token == "mock-token-123":
             if job_id == "404": return None
             mock_description = "Develops and maintains web applications using modern technologies. Creates user-friendly interfaces and ensures optimal performance. Collaborates with cross-functional teams to deliver high-quality software solutions. Participates in code reviews and mentors junior developers. Requires 5+ years of experience in full-stack development. Strong knowledge of JavaScript, React, and Node.js essential. Experience with cloud platforms preferred. Bachelor's degree in Computer Science or related field."
             return {
                 "id": job_id,
                 "title": "Senior Mock Developer", 
                 "description": mock_description,
                 "city": "Remote",
                 "state": "US",
                 "company": "Mock Corp",
                 "customer_name": "Mock Customer Inc.",
                 "job_status": "OPEN"
             }

        # Real Implementation
        url = f"{self.api_url}/apiv2/jobdiva/SearchJob"
        headers = {"Authorization": f"Bearer {token}"}
        
        # Determine if Ref Number (has hyphen) or internal ID
        is_ref = "-" in job_id
        payload = {}
        
        if is_ref:
            payload = {"jobdivaref": job_id, "maxReturned": 1}
        else:
            # Strip non-numeric just in case
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
                
                logger.info(f"JobDiva Search Response Status: {response.status_code}")
                if response.status_code != 200:
                    logger.error(f"JobDiva Error Response: {response.text}")
            
                jobs_list = []
                if response.status_code == 200:
                    data = response.json()
                    # Response can be list or dict with 'data'
                    if isinstance(data, list):
                        jobs_list = data
                    elif isinstance(data, dict) and "data" in data:
                        jobs_list = data.get("data", [])
                    else:
                        logger.warning(f"Unexpected JSON structure: {data}")
                
                # Fallback: If ref search failed, try treating as numeric ID (sometimes refs like 26-123 work as ID 26123?)
                # Or if user provided "26-123" but it's actually an ID `26123`
                if not jobs_list and is_ref:
                     numeric_id = job_id.replace("-", "")
                     if numeric_id.isdigit():
                         logger.info(f"Fallback search by ID: {numeric_id}")
                         payload_fb = {"jobOrderId": int(numeric_id), "maxReturned": 1}
                         resp_fb = await client.post(url, json=payload_fb, headers=headers)
                         if resp_fb.status_code == 200:
                             d_fb = resp_fb.json()
                             if isinstance(d_fb, list): jobs_list = d_fb
                             elif isinstance(d_fb, dict) and "data" in d_fb: jobs_list = d_fb.get("data", [])

                if not jobs_list:
                    logger.warning(f"Job {job_id} not found in JobDiva results.")
                    return None

                j = jobs_list[0]
                
                # Extract Description
                # Use "posting description" if available, else "job description"
                raw_job_desc = j.get("job description") or j.get("description") or ""
                raw_posting_desc = j.get("posting description") or ""
                
                raw_description = raw_posting_desc if raw_posting_desc.strip() else raw_job_desc
                description = format_job_description(raw_description)

                logger.info(f"🔥 DEBUG: Job {job_id} Desc Length: {len(description)}")
                logger.info(f"🔥 DEBUG: Job {job_id} Desc Snippet: {description[:200]}...")

                return {
                    "id": j.get("id"),
                    "title": j.get("job title") or j.get("title"),
                    "description": description,
                    "city": j.get("city"),
                    "state": j.get("state"),
                    "company": j.get("company"),
                    "customer_name": j.get("customer") or j.get("company") or "Unknown Customer",
                    "job_status": j.get("job status") or j.get("status") or "OPEN"
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
            
        # MOCK
        if token == "mock-token-123":
             return f"MOCK RESUME TEXT FOR CANDIDATE {candidate_id}\n\nExperience:\n- Senior Developer at Tech Corp (2020-Present)\n- Junior Dev at StartUp Inc (2018-2020)\n\nSkills: Python, React, TypeScript."

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
                                text = r.get("PLAINTEXT") or r.get("plainText") or r.get("text")
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

jobdiva_service = JobDivaService()
