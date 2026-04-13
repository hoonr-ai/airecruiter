import httpx
import json
import logging
import asyncio
from typing import List, Dict, Any, Optional
from core import (
    UNIPILE_API_KEY, UNIPILE_DSN, UNIPILE_ACCOUNT_ID
)

logger = logging.getLogger(__name__)

class UnipileService:
    def __init__(self):
        # Use centralized config
        dsn = UNIPILE_DSN
        if not dsn.startswith("http"):
            dsn = f"https://{dsn}"
        self.api_url = f"{dsn}/api/v1"
        
        self.api_key = UNIPILE_API_KEY
        self.account_id = UNIPILE_ACCOUNT_ID # Use from config if available
        self._id_cache = {} # Simple in-memory cache for skill/location IDs

    def _get_headers(self):
        return {
            "X-API-KEY": self.api_key,
            "Accept": "application/json"
        }

    async def _resolve_id(self, category: str, name: str) -> Optional[str]:
        """Resolves a string name to a LinkedIn ID (Geurn) using Unipile endpoints."""
        cache_key = f"{category}:{name.lower()}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        account_id = await self.get_account_id()
        if not account_id: return None

        # Fixed endpoint: /linkedin/search/parameters instead of /linkedin/search/skills 
        # which was returning 404 in the logs.
        url = f"{self.api_url}/linkedin/search/parameters"
        p_type = "SKILL" if category == "skill" else "LOCATION"
        params = {"account_id": account_id, "keywords": name, "type": p_type}
        
        try:
             async with httpx.AsyncClient(timeout=10.0) as client:
                 resp = await client.get(url, params=params, headers=self._get_headers())
                 if resp.status_code == 200:
                     items = resp.json().get("items", [])
                     if items:
                         # IMPROVEDish: Find the best match in the returned list
                         # Unipile parameters list might return many matches
                         best_match = items[0]
                         for item in items:
                             if item.get("title", "").lower() == name.lower():
                                 best_match = item
                                 break
                         
                         res_id = best_match.get("id")
                         self._id_cache[cache_key] = res_id
                         return res_id
                 else:
                     logger.warning(f"Unipile: Parameter resolution returned {resp.status_code} for {category} '{name}'")
        except Exception as e:
            logger.error(f"Unipile: ID resolution failed for {category} '{name}': {e}")
        return None

    async def get_account_id(self) -> Optional[str]:
        if self.account_id:
            return self.account_id
            
        if not self.api_key:
            logger.warning("Unipile API Key is missing.")
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

    async def search_candidates(self, skills: List[Any], location: str, open_to_work: bool = True, limit: int = 25) -> List[Dict[str, Any]]:
        """
        Search LinkedIn via Unipile using the Recruiter API mode.
        """
        account_id = await self.get_account_id()
        if not account_id:
            return []

        # 1. Resolve Skill IDs
        skill_ids = []
        # Prioritize Must Have skills
        must_haves = [s for s in skills if (isinstance(s, dict) and s.get("priority") == "Must Have") or (hasattr(s, "priority") and s.priority == "Must Have")]
        other_skills = [s for s in skills if s not in must_haves]
        
        # Resolve top 5 terms only to keep payload reasonable
        search_terms = (must_haves + other_skills)[:5]
        
        for s in search_terms:
            name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
            if name:
                 s_id = await self._resolve_id("skill", name)
                 if s_id:
                     priority = "MUST_HAVE" if s in must_haves else "CAN_HAVE"
                     skill_ids.append({"id": s_id, "priority": priority, "name_ref": name})
        
        # 2. Resolve Location ID
        location_ids = []
        if location and location.strip():
             loc_term = location.split(",")[0].strip()
             l_id = await self._resolve_id("location", loc_term)
             if l_id:
                 location_ids.append(l_id)

        # 3. Build Payload using Recruiter API structure
        url = f"{self.api_url}/linkedin/search"
        params = {"account_id": account_id, "limit": limit}
        
        # Prepare keywords for anything we couldn't resolve to an ID
        unresolved_terms = []
        for s in search_terms:
            name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
            # If not in skill_ids (which contains resolved IDs), add to keywords
            if not any(sid.get("name_ref") == name for sid in skill_ids):
                unresolved_terms.append(f'"{name}"')
        
        # If location didn't resolve, add to keywords
        if location and not location_ids:
             loc_term = location.split(",")[0].strip()
             unresolved_terms.append(f'"{loc_term}"')

        # Keywords fallback for remaining skills
        if len(search_terms) < len(skills):
            extra_skills = skills[len(search_terms):8] # Limit to avoid query too large
            for s in extra_skills:
                name = s.get("value") or s.get("name") if isinstance(s, dict) else getattr(s, "value", getattr(s, "name", str(s)))
                if name: unresolved_terms.append(f'"{name}"')

        if open_to_work:
             unresolved_terms.append('("Open to Work" OR "Looking for opportunities")')

        payload = {
            "api": "recruiter",
            "category": "people"
        }
        
        logger.info(f"Resolved {len(skill_ids)} skill IDs and {len(location_ids)} location IDs for LinkedIn search")

        if skill_ids:
            payload["skills"] = [{"id": s["id"], "priority": s["priority"]} for s in skill_ids]
            
        if location_ids:
            payload["location"] = [{"id": lid, "priority": "MUST_HAVE"} for lid in location_ids]
        
        if unresolved_terms:
            payload["keywords"] = " AND ".join(unresolved_terms)

        results = []
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                logger.info(f"Unipile Recruiter Search Payload: {json.dumps(payload)}")
                resp = await client.post(url, params=params, json=payload, headers=self._get_headers())
                
                if resp.status_code in [200, 201]: 
                    data = resp.json()
                    items = data.get("items", [])
                    
                    for item in items:
                        c_id = item.get("id")
                        full_name = item.get("name") or "LinkedIn Candidate"
                        
                        # Handle potential nulls and field variations from docs
                        img_url = item.get("img") or item.get("profile_picture_url")
                        p_url = item.get("profile_url") or item.get("public_profile_url")
                        
                        cand = {
                            "id": f"unipile_{c_id}",
                            "provider_id": c_id,
                            "firstName": full_name.split(" ")[0],
                            "lastName": " ".join(full_name.split(" ")[1:]) if " " in full_name else "",
                            "email": "",
                            "city": item.get("location", ""),
                            "state": "",
                            "title": item.get("headline", ""),
                            "source": "LinkedIn",
                            "match_score": 0,
                            "profile_url": p_url,
                            "image_url": img_url,
                            "open_to_work": open_to_work,
                            "recruiter_candidate_id": item.get("recruiter_candidate_id")
                        }
                        results.append(cand)
                else:
                    logger.error(f"Unipile Search Failed: {resp.status_code} - {resp.text}")
                    return []

        except Exception as e:
            logger.error(f"Unipile Search Exception: {e}")
            return []

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
