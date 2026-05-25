import hashlib
import json
import logging
import re
from typing import List, Dict, Any, Optional
import httpx
from core.config import OPENAI_API_KEY
from core.llm_client import get_openai_client, model_for
from core.models import JobDescription, CandidateProfile, SkillProfileEntry
from core import llm_cache
# Azure-Agent grounding was retired from job_skills_extractor (see its
# module docstring) but the conditional usage below still references the
# old symbols. Guard the import so a re-export removal can't crash app
# startup on every worker, which 502s the whole API.
try:
    from services.job_skills_extractor import _azure_agent, AZURE_AGENT_AVAILABLE
except ImportError:
    _azure_agent = None
    AZURE_AGENT_AVAILABLE = False

logger = logging.getLogger(__name__)

# 30 days. The on-disk resume-hash cache in sourced_candidates_storage
# uses the same 30-day window, so this just mirrors the DB TTL.
_CANDIDATE_PROFILE_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60


def _resume_hash(text: str) -> Optional[str]:
    """sha256 over normalized resume text. Mirrors
    sourced_candidates_storage._resume_text_hash so the same resume
    parsed via either path produces the same cache key."""
    if not text:
        return None
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) < 50:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

class AIService:
    def __init__(self):
        self.api_key = OPENAI_API_KEY
        self.client = get_openai_client()
        
        # Initialize Ontology (Graph) if not loaded
        from core.graph import ontology
        # Ensure graph is loaded (idempotent-ish if we check)
        # In a real app, this should be done on startup event
        # But we'll do lazy load here for safety
        if len(ontology.graph.nodes) == 0:
             # Try loading from DB (Ronak's skills database)
             try:
                 ontology.load_from_db()
                 print("✅ Skills ontology loaded successfully")
             except Exception as e:
                 print(f"⚠️ Skills ontology unavailable (requires Ronak's credentials): {str(e)[:100]}")
                 print("ℹ️ Core job functionality will work without advanced skills matching")

    async def _extract_jd(self, text: str) -> JobDescription:
        """
        Extracts structured JD from text.
        """
        if not self.client:
             raise Exception("OpenAI Client not initialized")

        system_prompt = "You are a Job Description Parser. Extract structured data."
        try:
            # Tier-3 #11: mechanical JD schema fill — dropped from
            # gpt-4o-mini to gpt-4.1-nano. Override via LLM_MODEL_JD_PARSE.
            model = model_for("jd_parse", "gpt-4.1-nano")
            completion = await self.client.beta.chat.completions.parse(
                model=model,
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
        import asyncio
        if not self.client:
             raise Exception("OpenAI Client not initialized")

        # Resume-hash cache check. Pre-tier-1 the LLM fired even when we
        # had a fully-parsed profile for an identical resume from a prior
        # ingest. We keep the same hashing rule as
        # sourced_candidates_storage._resume_text_hash so JobDiva
        # re-syncs and Tira's `tira_match_resume` flow share the cache.
        rhash = _resume_hash(text)
        cache_key = llm_cache.make_key("candidate", 1, rhash) if rhash else None
        if cache_key:
            cached = await llm_cache.get_json(cache_key)
            if cached is not None:
                try:
                    profile = CandidateProfile.model_validate(cached)
                    # cid is per-ingest — overwrite the cached id so the
                    # caller's id is honored. resume_text is dropped from
                    # the cached payload (see set below) to keep entries
                    # small; restore from the live arg.
                    profile.id = cid
                    profile.resume_text = text
                    logger.info(f"candidate parse: cache HIT for {cid}")
                    return profile
                except Exception as exc:
                    logger.warning(
                        f"candidate parse: cached profile failed validation for {cid}, re-parsing: {exc}"
                    )

        system_prompt = (
            "You are a professional Resume Parser and Taxonomy Expert. "
            "Extract structured data from the resume text including Name, Location (City, State), "
            "LinkedIn Profile URL (add to 'links' array), timeline (all jobs with dates and titles), and education. "
            "You MUST also calculate the total years of professional experience (total_yoe) as a float. "
            "Be precise with company names and job titles."
        )
        try:
            model = "gpt-4o-mini"
            # 1. Base Extraction (GPT)
            gpt_task = self.client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text[:30000]}
                ],
                response_format=CandidateProfile,
                temperature=0.0,
                prompt_cache_key="candidate-parse-v1",
            )
            
            # 2. Grounded Skill Extraction (Azure Agent) - Parallel
            agent_task = None
            if AZURE_AGENT_AVAILABLE and _azure_agent:
                agent_task = _azure_agent.extract_roles_and_skills(text[:25000]) # Cap for speed/reliability
                
            # Wait for both
            if agent_task:
                gpt_resp, agent_resp = await asyncio.gather(gpt_task, agent_task)
            else:
                gpt_resp = await gpt_task
                agent_resp = None

            profile = gpt_resp.choices[0].message.parsed
            profile.id = cid 
            profile.resume_text = text # Store the original content
            
            # 3. Merge Grounded Skills
            if agent_resp:
                grounded_skills = _azure_agent.convert_to_profile_skills(agent_resp.get("job_skills", []) or agent_resp.get("skills", []))

                # Check for existing skills to avoid duplicates
                existing_slugs = {s.skill_slug.lower().strip() for s in profile.skill_profile}
                for gs in grounded_skills:
                    if gs["skill_slug"].lower().strip() not in existing_slugs:
                        profile.skill_profile.append(SkillProfileEntry(**gs))

            if cache_key:
                try:
                    # Drop resume_text from the cached payload — entries
                    # would otherwise be 30kb+ each. Caller restores it
                    # from the live `text` arg on cache hit. id is left
                    # as a placeholder since the cache is keyed by resume
                    # hash and each cache hit overwrites id with the
                    # caller's per-ingest cid.
                    payload = profile.model_dump()
                    payload["resume_text"] = ""
                    payload["id"] = "__cached__"
                    await llm_cache.set_json(
                        cache_key,
                        payload,
                        ttl_seconds=_CANDIDATE_PROFILE_CACHE_TTL_SECONDS,
                    )
                except Exception as cache_exc:
                    logger.debug(f"candidate parse: cache set failed for {cid}: {cache_exc}")
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
            
            # Include extracted metadata for sync back to sourcing table
            res_dict['extracted_location'] = cand_obj.candidate_metadata.location
            res_dict['extracted_links'] = cand_obj.candidate_metadata.links
            res_dict['resume_text'] = cand_obj.resume_text
            
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
            model = "gpt-4o-mini"
            completion = await self.client.chat.completions.create(
                model=model,
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
