import logging
import asyncio
import json
import random
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

from services.jobdiva import JobDivaService
from services.unipile import unipile_service
from services.vetted import vetted_service

logger = logging.getLogger(__name__)

class SearchCriteria(BaseModel):
    job_id: str
    titles: List[str] = []
    skills: List[str] = []
    location: str = ""
    within_miles: int = 25
    companies: List[str] = []
    page_size: int = 100
    sources: List[str] = ["JobDiva", "LinkedIn"]
    open_to_work: bool = True

class UnifiedCandidateSearch:
    def __init__(self):
        self.jobdiva_service = JobDivaService()
        self.unipile_service = unipile_service
        self.vetted_service = vetted_service

    async def search_candidates(self, criteria: SearchCriteria) -> Dict[str, Any]:
        """
        Orchestrate candidate search across multiple providers.
        """
        logger.info(f"🚀 Starting Unified Search for job_id: {criteria.job_id}")
        
        tasks = []
        
        # 1. JobDiva Applicants (Always included if job_id exists)
        tasks.append(self._search_jobdiva_applicants(criteria))
        
        # 2. LinkedIn (Unipile)
        if "LinkedIn" in criteria.sources:
            tasks.append(self._search_linkedin(criteria))
            
        # 3. VettedDB
        if "VettedDB" in criteria.sources:
            tasks.append(self._search_vetted(criteria))
            
        # 4. JobDiva Talent Search (Disabled in current JobDivaService but structured here)
        # tasks.append(self._search_jobdiva_talent(criteria))

        # Execute all searches concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        combined_candidates = []
        summary = {
            "total_candidates": 0,
            "job_applicants_count": 0,
            "linkedin_count": 0,
            "vetted_count": 0,
            "talent_search_count": 0,
            "cached_results": 0,
            "new_extractions": 0
        }
        
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"Search task {i} failed: {res}")
                continue
                
            if not res:
                continue
                
            source_candidates = res.get("candidates", [])
            source_type = res.get("source_type")
            
            # Add results to combined list
            for cand in source_candidates:
                # Add source flag
                if "source" not in cand:
                    cand["source"] = source_type
                combined_candidates.append(cand)
                
            # Update summary
            if source_type == "JobDiva-Applicants":
                summary["job_applicants_count"] = len(source_candidates)
            elif source_type == "LinkedIn":
                summary["linkedin_count"] = len(source_candidates)
            elif source_type == "VettedDB":
                summary["vetted_count"] = len(source_candidates)
            elif source_type == "JobDiva-TalentSearch":
                summary["talent_search_count"] = len(source_candidates)

        # Deduplicate candidates (by email if available, or name+title)
        deduplicated = self._deduplicate_candidates(combined_candidates)
        
        # Apply match logic for sourced candidates with the JD
        required_set = set(s.lower() for s in criteria.skills)

        for candidate in deduplicated:
            cand_skills_list = candidate.get("skills", [])
            candidate_skills = set(s.lower() for s in cand_skills_list)

            common = required_set.intersection(candidate_skills)
            missing = required_set - candidate_skills

            # Base score random + bonus for skills
            score = random.randint(50, 70)
            if len(required_set) > 0:
                score += int((len(common) / len(required_set)) * 30)

            final_score = min(score, 100)

            # Explainability (same logic as old repo)
            explanation = []
            if final_score > 80:
                explanation.append("Strong skill match")
            elif final_score < 60:
                explanation.append("Low skill overlap")

            if missing:
                explanation.append(f"Missing: {', '.join(list(missing)[:2])}")
            else:
                explanation.append("All required skills present")

            # Attach to candidate WITHOUT changing return structure
            candidate["match_score"] = final_score
            candidate["missing_skills"] = list(missing)
            candidate["explainability"] = explanation
            
        summary["total_candidates"] = len(deduplicated)
        
        return {
            "candidates": deduplicated,
            "summary": summary,
            "search_criteria": criteria.dict()
        }

    async def _search_jobdiva_applicants(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Map search criteria to JobDiva skills format if needed
            # For now, get_enhanced_job_candidates handles the job_id
            candidates = await self.jobdiva_service.get_enhanced_job_candidates(criteria.job_id)
            for c in candidates:
                c["source"] = "JobDiva-Applicants"
            return {"candidates": candidates, "source_type": "JobDiva-Applicants"}
        except Exception as e:
            logger.error(f"JobDiva Applicants search failed: {e}")
            return {"candidates": [], "source_type": "JobDiva-Applicants"}

    async def _search_linkedin(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Unipile expects skills as a list of dicts or strings
            skills = [{"value": s, "priority": "Must Have"} for s in criteria.skills]
            candidates = await self.unipile_service.search_candidates(
                skills=skills,
                location=criteria.location,
                open_to_work=criteria.open_to_work,
                limit=criteria.page_size
            )
            return {"candidates": candidates, "source_type": "LinkedIn"}
        except Exception as e:
            logger.error(f"LinkedIn search failed: {e}")
            return {"candidates": [], "source_type": "LinkedIn"}

    async def _search_vetted(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            # Simple vetted search
            candidates = await self.vetted_service.search_candidates(
                skills=criteria.skills,
                location=criteria.location,
                limit=criteria.page_size
            )
            return {"candidates": candidates, "source_type": "VettedDB"}
        except Exception as e:
            logger.error(f"VettedDB search failed: {e}")
            return {"candidates": [], "source_type": "VettedDB"}

    def _deduplicate_candidates(self, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen = {}
        unique_results = []
        
        for cand in candidates:
            # Use email or combined name+city as key
            email = cand.get("email", "").lower().strip()
            name = f"{cand.get('firstName', '')} {cand.get('lastName', '')}".lower().strip()
            city = cand.get("city", "").lower().strip()
            
            key = email if email else f"{name}|{city}"
            
            if not key or key == "|":
                unique_results.append(cand)
                continue
                
            if key not in seen:
                seen[key] = cand
                unique_results.append(cand)
            else:
                # If we have a duplicate, prioritize JobDiva-Applicants over others
                existing = seen[key]
                if cand.get("source") == "JobDiva-Applicants" and existing.get("source") != "JobDiva-Applicants":
                    # Replace existing with current
                    for i, r in enumerate(unique_results):
                        if r == existing:
                            unique_results[i] = cand
                            break
                    seen[key] = cand
                    
        return unique_results

unified_search_service = UnifiedCandidateSearch()
