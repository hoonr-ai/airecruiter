import logging
from typing import List, Dict, Any
from core.config import EXA_API_KEY
from exa_py import Exa

logger = logging.getLogger(__name__)

class ExaService:
    def __init__(self):
        self.api_key = EXA_API_KEY
        self.exa = None
        if self.api_key:
            try:
                self.exa = Exa(api_key=self.api_key)
            except Exception as e:
                logger.error(f"Failed to initialize Exa SDK: {e}")

    async def search_candidates(self, skills: List[str], location: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Exa search.")
            return []

        try:
            # Construct a comprehensive search query
            # We want to find people with these skills in this location
            # Note: category="people" is optimized for finding individuals based on role/skills.
            
            # Combine skills into a single comma-separated string
            skills_str = ", ".join(skills) if skills else ""
            
            query = f"software engineer OR developer {skills_str}"
            if location:
                query += f" located in {location}"

            logger.info(f"Executing Exa people search for query: {query}")
            
            # Note: the python SDK's search method supports synchronous wrapper? 
            # If exa_py is sync, we should probably run it in an executor, but we can try it directly.
            # Using type="auto" as recommended in the config for most queries
            import asyncio
            loop = asyncio.get_event_loop()
            
            def do_search():
                return self.exa.search_and_contents(
                    query,
                    category="people",
                    type="auto",
                    num_results=limit,
                    highlights={"max_characters": 4000}
                )

            # Wait for sync search call
            response = await loop.run_in_executor(None, do_search)
            
            results = []
            if response and hasattr(response, 'results'):
                for idx, result in enumerate(response.results):
                    # Exa returns title, url, author, id. 
                    # Often for people search, the title contains their name or headline.
                    title = getattr(result, 'title', 'Unknown Candidate')
                    url = getattr(result, 'url', '')
                    
                    # Try to separate first and last name from the title
                    name_parts = title.split(" - ")[0].split("|")[0].strip().split(" ")
                    first_name = name_parts[0] if len(name_parts) > 0 else "Unknown"
                    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
                    
                    # Store highlights if any
                    highlights_text = ""
                    if getattr(result, 'highlights', None):
                        highlights_text = "\n".join(result.highlights)
                    
                    cand = {
                        "id": f"exa_{idx}_{getattr(result, 'id', idx)}",
                        "provider_id": getattr(result, 'id', f"exa_{idx}"),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": "",
                        "city": location, # We infer string from query
                        "state": "",
                        "title": title,
                        "source": "LinkedIn-Exa",
                        "match_score": 0,
                        "profile_url": url,
                        "image_url": "",
                        "open_to_work": False,
                        "resume_text": highlights_text,
                        "recruiter_candidate_id": None
                    }
                    results.append(cand)
                    
            logger.info(f"Exa search returned {len(results)} candidates.")
            return results

        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return []

    async def search_dice_candidates(self, skills: List[str], location: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Search Dice (dice.com) profiles via Exa with domain filtering.
        Dice hosts tech candidate profiles publicly indexable by Exa; we scope
        the people-search to dice.com to pull those records.
        """
        if not self.exa:
            logger.warning("Exa API key is not set. Skipping Dice search.")
            return []

        try:
            import asyncio
            skills_str = ", ".join(skills) if skills else ""
            query = f"resume profile {skills_str}"
            if location:
                query += f" located in {location}"

            logger.info(f"Executing Dice (via Exa) search for query: {query}")
            loop = asyncio.get_event_loop()

            def do_search():
                return self.exa.search_and_contents(
                    query,
                    category="people",
                    type="auto",
                    num_results=limit,
                    include_domains=["dice.com"],
                    highlights={"max_characters": 4000},
                )

            response = await loop.run_in_executor(None, do_search)

            results = []
            if response and hasattr(response, "results"):
                for idx, result in enumerate(response.results):
                    title = getattr(result, "title", "Unknown Candidate")
                    url = getattr(result, "url", "")
                    name_parts = title.split(" - ")[0].split("|")[0].strip().split(" ")
                    first_name = name_parts[0] if name_parts else "Unknown"
                    last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
                    highlights_text = ""
                    if getattr(result, "highlights", None):
                        highlights_text = "\n".join(result.highlights)
                    results.append({
                        "id": f"dice_{idx}_{getattr(result, 'id', idx)}",
                        "provider_id": getattr(result, "id", f"dice_{idx}"),
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": "",
                        "city": location,
                        "state": "",
                        "title": title,
                        "source": "Dice",
                        "match_score": 0,
                        "profile_url": url,
                        "image_url": "",
                        "open_to_work": False,
                        "resume_text": highlights_text,
                        "recruiter_candidate_id": None,
                    })

            logger.info(f"Dice-via-Exa returned {len(results)} candidates.")
            return results

        except Exception as e:
            logger.error(f"Dice (via Exa) search failed: {e}")
            return []

exa_service = ExaService()
