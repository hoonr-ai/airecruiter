import os
import json
import logging
from typing import List, Dict, Any
import httpx
from openai import AsyncOpenAI
from core.models import JobDescription, CandidateProfile

logger = logging.getLogger(__name__)

class AIService:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None
        
        # Initialize Ontology (Graph) if not loaded
        from core.graph import ontology
        # Ensure graph is loaded (idempotent-ish if we check)
        # In a real app, this should be done on startup event
        # But we'll do lazy load here for safety
        if len(ontology.graph.nodes) == 0:
             # Try loading from DB
             ontology.load_from_db()

    async def _extract_jd(self, text: str) -> JobDescription:
        """
        Extracts structured JD from text.
        """
        if not self.client:
             raise Exception("OpenAI Client not initialized")

        system_prompt = "You are a Job Description Parser. Extract structured data."
        try:
            completion = await self.client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:20000]} # Increased Limit
                ],
                response_format=JobDescription,
                temperature=0.0
            )
            return completion.choices[0].message.parsed
        except Exception as e:
            logger.error(f"JD Extraction Failed: {e}")
            # Return dummy/empty or re-raise
            # Fallback to minimal JD
            from core.models import JobMetadata, GatingRules, SenioritySignals
            return JobDescription(
                id="unknown",
                job_metadata=JobMetadata(title="Unknown"),
                gating_rules=GatingRules(),
                requirements=[],
                seniority_signals=SenioritySignals(),
                is_valid=False,
                parsing_error=str(e)
            )

    async def _extract_candidate(self, text: str, cid: str) -> CandidateProfile:
        """
        Extracts structured Candidate from resume text.
        """
        if not self.client:
             raise Exception("OpenAI Client not initialized")
             
        system_prompt = "You are a Resume Parser. Extract structured data including skills, timeline, and education."
        try:
            completion = await self.client.beta.chat.completions.parse(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:30000]} # Increased Limit
                ],
                response_format=CandidateProfile,
                temperature=0.0
            )
            profile = completion.choices[0].message.parsed
            profile.id = cid # Ensure ID matches
            return profile
            
        except Exception as e:
            logger.error(f"Candidate Extraction Failed for {cid}: {e}")
            # Fallback
            from core.models import CandidateMetadata, ComputedCandidateStats
            return CandidateProfile(
                id=cid,
                candidate_metadata=CandidateMetadata(name="Unknown"),
                computed_stats=ComputedCandidateStats(),
                formatted_name="Unknown",
                is_valid=False,
                parsing_error=str(e)
            )

    def _convert_extracted_to_jd(self, extracted: Dict[str, Any]) -> JobDescription:
        """
        Converts frontend ExtractedData JSON into internal JobDescription model.
        This preserves user edits (priorities, seniority).
        """
        from core.models import JobMetadata, GatingRules, SenioritySignals, Requirement, Competency, JobDescription
        import uuid
        
        # 1. Metadata
        meta = JobMetadata(
            title=extracted.get("title", "Untitled"),
            location=extracted.get("location"),
            work_mode=extracted.get("location_type", "Onsite").lower()
        )
        
        # 2. Requirements (Hard Skills)
        reqs = []
        for s in extracted.get("hard_skills", []):
            prio = s.get("priority", "Must Have").lower().replace(" ", "_") # "must_have" or "flexible" -> "nice_to_have"
            if "flexible" in prio: prio = "nice_to_have"
            
            reqs.append(Requirement(
                req_id=f"req_{uuid.uuid4().hex[:8]}",
                skill_id=s.get("name"),
                priority=prio,
                level=s.get("seniority", "Mid").lower(),
                is_hard_filter=(prio == "must_have")
            ))
            
        # 3. Competencies (Soft Skills)
        comps = []
        for ss in extracted.get("soft_skills", []):
            comps.append(Competency(name=ss))
            
        return JobDescription(
            id=str(uuid.uuid4()),
            job_metadata=meta,
            gating_rules=GatingRules(), # Defaults
            requirements=reqs,
            competencies=comps,
            seniority_signals=SenioritySignals(),
            is_valid=True
        )

    async def analyze_candidates_batch(self, candidates: List[Dict[str, Any]], job_description_text: str, structured_jd: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Full Pipeline:
        1. Parse JD (or use provided Structure)
        2. Parse Candidates (Parallel)
        3. Match Engine (Parallel)
        4. Return Results
        """
        import asyncio
        from core.engine import calculate_match
        
        if not candidates:
            return []
            
        # 1. Parse JD
        try:
            if structured_jd:
                logger.info("Using provided Structured JD (Skipping LLM Extraction)")
                jd_obj = self._convert_extracted_to_jd(structured_jd)
            else:
                jd_obj = await self._extract_jd(job_description_text)
        except Exception as e:
            logger.error(f"Critical: JD Parsing failed: {e}")
            return []

        # 2. Process Candidates

        async def process_single(c_dict):
            cid = c_dict.get('id', 'unknown')
            r_text = c_dict.get('resume_text', '')
            
            # Extract
            try:
                cand_obj = await self._extract_candidate(r_text, cid)
            except Exception as e:
                logger.error(f"Skipping candidate {cid}: extraction error {e}")
                return None
            
            # Match
            # Inject Metadata from Source if missing in parsing (Fix for "Insufficient Data" error)
            if not cand_obj.candidate_metadata.location:
                city = c_dict.get('city')
                state = c_dict.get('state')
                if city or state:
                    raw_loc = f"{city or ''}, {state or ''}".strip(', ')
                    cand_obj.candidate_metadata.location = raw_loc

            try:
                result = await calculate_match(cand_obj, jd_obj)
            except Exception as e:
                logger.error(f"Skipping candidate {cid}: matching error {e}")
                return None
                
            # Serialize
            res_dict = result.model_dump()
            res_dict['candidate_id'] = cid
            res_dict['candidate_name'] = c_dict.get('firstName', '') + ' ' + c_dict.get('lastName', '')
            
            # Map colors for UI if needed or handle in frontend
            # Frontend expects: "score", "candidate_id"
            # And we'll add the full object as 'details' or top level?
            # Existing specific fields:
            # "tribunal_status" (derived from verdict)
            
            verdict = result.tribunal_verdict
            if verdict:
                tag = verdict.narrative_tag
                if tag in ["top_tier_potential", "solid_performer"]:
                    res_dict['tribunal_status'] = "Green"
                elif tag in ["high_risk", "mismatch"]:
                    res_dict['tribunal_status'] = "Red"
                else:
                    res_dict['tribunal_status'] = "Yellow"
            else:
                res_dict['tribunal_status'] = "Gray"
                
            return res_dict

        # Run Parallel
        tasks = [process_single(c) for c in candidates]
        results = await asyncio.gather(*tasks)
        
        return [r for r in results if r is not None]

    async def generate_resume_from_profile(self, profile_data: Dict[str, Any]) -> str:
        """
        Converts a raw JSON profile (e.g. from LinkedIn) into a structured text Resume.
        """
        if not self.client:
            return "AI Service Unavailable for Resume Generation."

        system_prompt = "You are an expert Resume Writer. Convert the provided Profile JSON into a clean, professional Resume text."
        user_prompt = f"Profile JSON:\n{json.dumps(profile_data, indent=2)}\n\nPlease format this as a text-based Resume."
        
        try:
            completion = await self.client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3
            )
            return completion.choices[0].message.content
        except Exception as e:
            logger.error(f"Resume Generation Failed: {e}")
            return "Failed to generate resume from profile."

ai_service = AIService()
