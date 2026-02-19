import os
import httpx
import logging
import asyncio
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class UnipileService:
    def __init__(self):
        # Defaults to api1.unipile.com if not set
        dsn = os.getenv("UNIPILE_DSN", "api1.unipile.com")
        if not dsn.startswith("http"):
            dsn = f"https://{dsn}"
        self.api_url = f"{dsn}/api/v1"
        
        self.api_key = os.getenv("UNIPILE_API_KEY", "")
        self.account_id = None # Cached Account ID

    def _get_headers(self):
        return {
            "X-API-KEY": self.api_key,
            "Accept": "application/json"
        }

    async def get_account_id(self) -> Optional[str]:
        """Fetches the first connected LinkedIn account ID."""
        if self.account_id:
            return self.account_id

        # Check environment variable first
        env_id = os.getenv("UNIPILE_ACCOUNT_ID")
        if env_id:
            self.account_id = env_id
            return env_id
            
        if not self.api_key or "placeholder" in self.api_key:
            logger.warning("Unipile API Key is missing or placeholder.")
            return None

        url = f"{self.api_url}/accounts"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=self._get_headers())
                if resp.status_code == 200:
                    data = resp.json()
                    # Response structure: { items: [...], cursor: ... } or list [...]
                    accounts = []
                    if isinstance(data, dict):
                         accounts = data.get("items", [])
                    elif isinstance(data, list):
                         accounts = data
                    
                    for acc in accounts:
                        # Check type. Usually "LINKEDIN" or "linkedin"
                        # The documentation says type: "LINKEDIN"
                        if str(acc.get("type", "")).upper() == "LINKEDIN" and acc.get("status") == "OK":
                             self.account_id = acc.get("id")
                             logger.info(f"Unipile: Using LinkedIn Account {self.account_id} ({acc.get('name')})")
                             return self.account_id
                    
                    if accounts and not self.account_id:
                        # Fallback: take first account if no explict match? No, safer to fail.
                        logger.warning("No active LinkedIn account found in Unipile.")
                else:
                    logger.error(f"Unipile Accounts Error: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Unipile Account Fetch Exception: {e}")
            
        return None

    async def search_candidates(self, skills: List[Any], location: str, open_to_work: bool = False, limit: int = 25) -> List[Dict[str, Any]]:
        """
        Search LinkedIn via Unipile.
        Filters by 'Open to Work' by appending keywords.
        """
        account_id = await self.get_account_id()
        if not account_id:
            return []

        # Construct Keywords
        keywords = []
        for s in skills:
            # Handle string or object
            if isinstance(s, dict):
                name = s.get("name")
            elif hasattr(s, "name"):
                name = s.name
            else:
                name = str(s)
            
            if name:
                 # Quote simplistic terms to be safe? Or simple string?
                 # Unipile/LinkedIn supports boolean.
                 keywords.append(f'"{name}"')
        
        query_str = " AND ".join(keywords)
        
        # Open To Work Filter (Keyword Approximation)
        if open_to_work:
             query_str += ' AND ("Open to Work" OR "Looking for opportunities" OR "Seeking new roles")'
             
        # Location
        # Unipile accepts 'location' parameter which takes LinkedIn Geurns/IDs usually.
        # But documentation says "Search for location IDs... Use this ID".
        # This is complex. 
        # HOWEVER, 'keywords' param can include location? "Java AND San Francisco"?
        # Or we use 'location' param if valid ID?
        # User prompt said "limit to 25".
        # Docs say param "location" takes IDs.
        # If I pass string "San Francisco", it might fail.
        # SAFE BET: Append location to keywords if I don't have ID.
        if location and location.strip():
             # Heuristic: Split "City, State" and use "City"
             # "New York, NY" -> "New York"
             # "Austin, TX" -> "Austin"
             loc_term = location.split(",")[0].strip()
             query_str += f' AND {loc_term}'
             print(f"🔥 DEBUG: LinkedIn Location Query: Added '{loc_term}' (Unquoted) from '{location}'")

        # Account ID must be a query parameter
        url = f"{self.api_url}/linkedin/search"
        params = {"account_id": account_id}
        
        # Payload must be flat (no 'params' wrapper)
        payload = {
            "api": "classic",
            "keywords": query_str,
            "category": "people",
            "limit": limit
        }

        results = []
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                logger.info(f"Unipile Search: {query_str}")
                # Pass account_id as query param, payload as json body
                resp = await client.post(url, params=params, json=payload, headers=self._get_headers())
                
                if resp.status_code == 200: # Unipile often returns 201 for Async? 
                    # Docs say "Perform search...". Sync/Async?
                    # Getting Started said "export result".
                    # Real-time search might be synchronous.
                    data = resp.json()
                    items = data.get("items", [])
                    
                    for item in items:
                        # Map to Candidate
                        c_id = item.get("id") # Provider ID
                        # Construct internal ID? "unipile_{id}"
                        
                        # Extract basic info
                        match_score = 0 # logic later
                        
                        cand = {
                            "id": f"unipile_{c_id}",
                            "provider_id": c_id,
                            "firstName": item.get("name", "").split(" ")[0],
                            "lastName": " ".join(item.get("name", "").split(" ")[1:]),
                            "email": "", # Not provided usually
                            "city": item.get("location", ""),
                            "state": "",
                            "title": item.get("headline", ""),
                            "source": "LinkedIn",
                            "match_score": 0,
                            "profile_url": item.get("profile_url"),
                            "image_url": item.get("img"),
                            "open_to_work": open_to_work # We filtered for it
                        }
                        results.append(cand)
                else:
                    logger.error(f"Unipile Search Failed: {resp.status_code} - {resp.text}")

        except Exception as e:
            logger.error(f"Unipile Search Exception: {e}")

        logger.info(f"Unipile returned {len(results)} candidates")
        return results

    async def send_message(self, candidate_provider_id: str, text: str) -> bool:
        """
        Send LinkedIn Message (InMail if premium allowed).
        """
        account_id = await self.get_account_id()
        if not account_id: return False
        
        url = f"{self.api_url}/chats"
        
        # Need to handle Multipart/Form or JSON?
        # Docs showed cURL with --form (Multipart).
        # Docs also showed JS client.messaging.startNewChat (JSON).
        # Unipile API usually accepts JSON.
        
        payload = {
            "account_id": account_id,
            "text": text,
            "attendees_ids": [candidate_provider_id],
            "linkedin": {
                "api": "classic",
                "inmail": True
            }
        }
        
        try:
             async with httpx.AsyncClient(timeout=15.0) as client:
                 resp = await client.post(url, json=payload, headers=self._get_headers())
                 if resp.status_code in [200, 201]:
                     return True
                 else:
                     logger.error(f"Unipile Send Message Failed: {resp.text}")
                     return False
        except Exception as e:
            logger.error(f"Unipile Send Message Exception: {e}")
            return False

    async def get_candidate_profile(self, candidate_provider_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetches full LinkedIn profile for a candidate.
        Endpoint: /linkedin/users/{id} (or generic /users/{id} depending on Unipile version)
        """
        account_id = await self.get_account_id()
        if not account_id: return None
        
        # Try specific LinkedIn User endpoint
        # verified via debug: /users/{id} works for provider_id
        url = f"{self.api_url}/users/{candidate_provider_id}"
        
        try:
             async with httpx.AsyncClient(timeout=15.0) as client:
                 params = {"account_id": account_id}
                 resp = await client.get(url, params=params, headers=self._get_headers())
                 
                 if resp.status_code == 200:
                     return resp.json()
                 elif resp.status_code == 404:
                     # Fallback check?
                     logger.warning(f"Unipile Profile 404 for {candidate_provider_id}")
                     return None
                 else:
                     logger.error(f"Unipile Profile Error: {resp.status_code} - {resp.text}")
                     return None
        except Exception as e:
            logger.error(f"Unipile Profile Exception: {e}")
            return None

unipile_service = UnipileService()
