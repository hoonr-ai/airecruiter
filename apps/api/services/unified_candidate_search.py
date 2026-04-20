import logging
import asyncio
import json
import random
import time
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

from services.jobdiva import JobDivaService
from services.unipile import unipile_service
from services.vetted import vetted_service
from services.exa_service import exa_service

logger = logging.getLogger(__name__)

class SearchCriteria(BaseModel):
    job_id: str
    titles: List[str] = []
    skills: List[str] = []
    location: str = ""
    within_miles: int = 25
    companies: List[str] = []
    page_size: int = 100
    sources: List[str] = ["JobDiva", "LinkedIn", "Exa"]
    open_to_work: bool = True

class UnifiedCandidateSearch:
    def __init__(self):
        self.jobdiva_service = JobDivaService()
        self.unipile_service = unipile_service
        self.vetted_service = vetted_service
        self.exa_service = exa_service


    async def search_candidates(self, criteria: SearchCriteria) -> Dict[str, Any]:
        """
        Orchestrate candidate search across multiple providers.
        """
        # Start timing the full extraction process
        start_time = time.time()
        
        logger.info("=" * 80)
        logger.info(f"🚀 UNIFIED CANDIDATE SEARCH - Job ID: {criteria.job_id}")
        logger.info(f"⏱️  START TIME: {time.strftime('%H:%M:%S')}")
        logger.info("=" * 80)
        
        # 1. JobDiva sources
        app_candidates = []
        jobdiva_applicants_result = {}
        
        if "JobDiva" in criteria.sources:
            logger.info("📋 STEP 1: Fetching JobDiva Applicants...")
            jobdiva_applicants_result = await self._search_jobdiva_applicants(criteria)
            app_candidates = jobdiva_applicants_result.get("candidates", [])
            logger.info(f"✅ STEP 1 COMPLETE: Found {len(app_candidates)} JobDiva applicants")
        else:
            logger.info("📋 STEP 1: Skipping JobDiva Applicants (not in sources)...")
        
        tasks = []
        
        # 2. LinkedIn (Unipile)
        if "LinkedIn" in criteria.sources:
            logger.info("📋 STEP 2: Adding LinkedIn search...")
            tasks.append(self._search_linkedin(criteria))
            
        # 3. VettedDB
        if "VettedDB" in criteria.sources:
            logger.info("📋 STEP 2: Adding VettedDB search...")
            tasks.append(self._search_vetted(criteria))
            
        # 4. Exa API
        if "Exa" in criteria.sources:
            logger.info("📋 STEP 2: Adding Exa search...")
            tasks.append(self._search_exa(criteria))
            
        # 5. JobDiva Talent Search - Only if less than 3 applicants were found AND JobDiva is explicitly selected
        if "JobDiva" in criteria.sources:
            if len(app_candidates) < 3:
                logger.info(f"📋 STEP 2: Only {len(app_candidates)} applicants found. Adding Talent Search fallback...")
                tasks.append(self._search_jobdiva_talent(criteria))
            else:
                logger.info(f"📋 STEP 2: Skipping Talent Search (found {len(app_candidates)} applicants >= 3)")
        elif "JobDiva Hotlist" in criteria.sources:
            # Optionally implement Hotlist search or just ignore, for now we will just not run Talent Search
            pass

        # Execute remaining searches concurrently
        logger.info("-" * 80)
        logger.info("⏳ Executing additional search sources...")
        results = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(f"✅ Additional searches complete ({len(tasks)} sources)")
            
        # Add applicants result manually to the results list
        if jobdiva_applicants_result:
            results.append(jobdiva_applicants_result)
        
        combined_candidates = []
        summary = {
            "total_candidates": 0,
            "job_applicants_count": 0,
            "linkedin_count": 0,
            "vetted_count": 0,
            "exa_count": 0,
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
            elif source_type == "Exa":
                summary["exa_count"] = len(source_candidates)
            elif source_type == "JobDiva-TalentSearch":
                summary["talent_search_count"] = len(source_candidates)

        # Deduplicate candidates (by email if available, or name+title)
        logger.info("-" * 80)
        logger.info("📋 STEP 3: Deduplicating candidates...")
        deduplicated = self._deduplicate_candidates(combined_candidates)
        logger.info(f"✅ STEP 3 COMPLETE: Deduplicated to {len(deduplicated)} unique candidates")
        
        from services.sourced_candidates_storage import process_jobdiva_candidate
        
        # Pre-process JobDiva candidates to extract AI info and save to candidate_enhanced_info
        logger.info("=" * 80)
        logger.info("🤖 STEP 4: AI EXTRACTION - Azure Agent + LLM Processing")
        logger.info("=" * 80)
        jobdiva_candidates = [c for c in deduplicated if c.get("source", "").startswith("JobDiva")]
        logger.info(f"📊 Found {len(jobdiva_candidates)} JobDiva candidates to process")
        
        for idx, candidate in enumerate(jobdiva_candidates, 1):
            candidate_id = candidate.get("candidate_id", "unknown")
            candidate_name = candidate.get("name", "Unknown")
            logger.info(f"\n{'─' * 60}")
            logger.info(f"🔄 [{idx}/{len(jobdiva_candidates)}] Processing: {candidate_name} (ID: {candidate_id})")
            logger.info(f"{'─' * 60}")
            try:
                await process_jobdiva_candidate(candidate)
                summary["new_extractions"] += 1
                logger.info(f"✅ [{idx}/{len(jobdiva_candidates)}] COMPLETE: {candidate_name}")
            except Exception as e:
                logger.error(f"❌ [{idx}/{len(jobdiva_candidates)}] FAILED: {candidate_name} - {e}")
        
        logger.info("=" * 80)
        logger.info(f"✅ STEP 4 COMPLETE: {summary['new_extractions']}/{len(jobdiva_candidates)} candidates processed")
        logger.info("=" * 80)

        # Apply match logic for sourced candidates with the JD
        required_set = set(s.lower() for s in criteria.skills)

        for candidate in deduplicated:
            # If the candidate was just processed by Azure, formatted structured skills will be in `candidate_enhanced_info` or `candidate["skills"]` if it was modified
            cand_skills_list = []
            if isinstance(candidate.get("skills"), list):
                for skill_entry in candidate.get("skills", []):
                    if isinstance(skill_entry, dict) and "skill" in skill_entry:
                        cand_skills_list.append(skill_entry["skill"])
                    elif isinstance(skill_entry, str):
                        cand_skills_list.append(skill_entry)

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
        
        # Calculate and log total extraction time
        end_time = time.time()
        total_duration = end_time - start_time
        minutes = int(total_duration // 60)
        seconds = int(total_duration % 60)
        
        logger.info("\n" + "=" * 80)
        logger.info("🎉 FULL EXTRACTION COMPLETE")
        logger.info("=" * 80)
        logger.info(f"⏱️  TOTAL TIME: {minutes}m {seconds}s ({total_duration:.1f} seconds)")
        logger.info(f"📊 SUMMARY:")
        logger.info(f"   - JobDiva Applicants: {summary['job_applicants_count']}")
        logger.info(f"   - Talent Search: {summary['talent_search_count']}")
        logger.info(f"   - LinkedIn: {summary['linkedin_count']}")
        logger.info(f"   - VettedDB: {summary['vetted_count']}")
        logger.info(f"   - Exa: {summary['exa_count']}")
        logger.info(f"   - Total Unique Candidates: {summary['total_candidates']}")
        logger.info(f"   - AI Extractions Completed: {summary['new_extractions']}")
        logger.info(f"   - Avg Time Per Candidate: {total_duration/max(summary['new_extractions'], 1):.1f}s")
        logger.info("=" * 80)
        
        return {
            "candidates": deduplicated,
            "summary": summary,
            "search_criteria": criteria.dict(),
            "extraction_time_seconds": round(total_duration, 1)
        }
        
    async def _search_jobdiva_talent(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            candidates = await self.jobdiva_service.search_candidates(
                skills=criteria.skills,
                location=criteria.location,
                limit=criteria.page_size,
                job_id=None
            )
            for c in candidates:
                c["source"] = "JobDiva-TalentSearch"
            return {"candidates": candidates, "source_type": "JobDiva-TalentSearch"}
        except Exception as e:
            logger.error(f"JobDiva Talent Search failed: {e}")
            return {"candidates": [], "source_type": "JobDiva-TalentSearch"}

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

    async def _search_exa(self, criteria: SearchCriteria) -> Dict[str, Any]:
        try:
            skills_values = []
            for s in criteria.skills:
                val = s.get("value") if isinstance(s, dict) else s
                if val:
                    skills_values.append(str(val))
                    
            candidates = await self.exa_service.search_candidates(
                skills=skills_values,
                location=criteria.location,
                limit=min(criteria.page_size, 20)
            )
            return {"candidates": candidates, "source_type": "Exa"}
        except Exception as e:
            logger.error(f"Exa search failed: {e}")
            return {"candidates": [], "source_type": "Exa"}

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
