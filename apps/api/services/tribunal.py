import json
import logging
from typing import Optional
from openai import AsyncOpenAI
from core.intelligence import TribunalVerdict
from core.models import CandidateProfile, JobDescription
from core.toon import encode
from core.config import OPENAI_API_KEY
from core import llm_cache

logger = logging.getLogger(__name__)

# 7 days. Tribunal inputs (parsed resume + JD rubric) only change when a
# candidate is re-parsed or a rubric is re-extracted, both of which
# already invalidate this key via the rubric_hash component.
_TRIBUNAL_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def _distance_bucket(distance_miles: Optional[float]) -> Optional[str]:
    """Bucket continuous distances so a 12.3 mi vs 12.7 mi delta doesn't
    fork the cache. Buckets are wide enough to be cosmetically
    indistinguishable to the Tribunal's location_fit scoring."""
    if distance_miles is None:
        return None
    d = float(distance_miles)
    if d <= 10:
        return "<=10"
    if d <= 25:
        return "<=25"
    if d <= 50:
        return "<=50"
    if d <= 100:
        return "<=100"
    return ">100"


class TribunalService:
    def __init__(self):
        self.api_key = OPENAI_API_KEY
        self.client = AsyncOpenAI(api_key=self.api_key) if self.api_key else None

    async def evaluate_narrative(
        self,
        resume_text: str,
        candidate: CandidateProfile,
        jd: JobDescription,
        distance_miles: Optional[float] = None,
        radius_miles: Optional[int] = None,
    ) -> TribunalVerdict:
        """
        Runs the Adversarial Debate (Skeptic vs Advocate) to analyze career context.
        Uses TOON (Token-Oriented Object Notation) for efficiency.
        """
        if not self.client:
            return self._fail_open("Missing OpenAI Key")

        # 1. Prepare Context with TOON
        try:
            # We strip heavy text fields before encoding to save even more space,
            # as the full resume text is provided separately in the prompt.
            # But here we want the structured data (timeline, skills)
            candidate_toon = encode(candidate)
            jd_toon = encode(jd)
        except Exception as encode_error:
            print(f"⚠️ TOON Encoding Failed: {encode_error}. Falling back to default.")
            candidate_toon = str(candidate.dict())
            jd_toon = str(jd.dict())

        # Cache key: the four things that actually go to the LLM. TOON output
        # is canonical for a given pydantic model, so hashing it is stable
        # across calls without explicit resume_hash / rubric_hash plumbing.
        cache_key = llm_cache.make_key(
            "tribunal", 1,
            resume_text[:30000],
            candidate_toon,
            jd_toon,
            _distance_bucket(distance_miles),
            radius_miles,
        )
        cached = await llm_cache.get_json(cache_key)
        if cached is not None:
            try:
                verdict = TribunalVerdict.model_validate(cached)
                logger.info("tribunal: cache HIT")
                return verdict
            except Exception as exc:
                # Schema drift — drop the entry and re-evaluate. Don't fail open.
                logger.warning(f"tribunal: cached verdict failed validation, re-running: {exc}")
        
        system_prompt = """
        You are "Talience Tribunal", an AI panel that evaluates candidate career narratives.

        MODE: ADVERSARIAL DEBATE
        Persona A (The Skeptic): Ruthlessly scrutinize **ROLE MISMATCH**. If a candidate has a background in Sales, Support, or QA but applies for Core Engineering, ATTACK IT. Also look for tenure gaps, role stagnation, and title inflation.
        Persona B (The Advocate): Look for transferable skills, rapid pivots, self-learning, and high-potential transitions. Be optimistic but realistic.

        TASK:
        1. **Compare Candidate's Primary Domain vs JD**: Is this a "Sales Engineer" applying for "Backend Dev"? Is this a "Manager" applying for "IC"?
        2. Analyze the timeline for stability and trajectory.
        3. **LOCATION FIT** (only if a LOCATION block is provided in the user prompt): Score `location_fit_score` on a 0-100 scale.
           - 100 if same metro / candidate is at <=10% of the requested radius from the JD city.
           - Degrade roughly linearly to ~40 at the radius edge.
           - <=20 if the candidate is outside the requested radius (or the distance >= 1.5x radius).
           - If `open_to_relocation` is true, add a +25 bonus (cap at 80).
           - Set `location_fit_reason` to a one-sentence explanation referencing the distance and radius.
           - If a meaningful mismatch lowers fit, also append a `RiskSignal` of type `location_mismatch`.
           - If no LOCATION block is given, leave `location_fit_score` as null and `location_fit_reason` as "".
        4. Conduct a silent debate.
        5. Form a consensus verdict.

        CRITICAL RULES:
        - **DOMAIN MISMATCH IS FATAL**: If the candidate's recent experience is in a fundamentally different function (e.g. Pre-Sales vs Engineering), the Skeptic MUST win. Mark as "mismatch" or "high_risk".
        - Do NOT simply average the views.
        - IGNORE Name, Gender, Age, and Ethnicity.

        DATA FORMAT: TOON (Token-Oriented Object Notation)
        - Indentation denotes nesting.
        - Header rows usually indicate list start.
        - Treat this as structured data equivalent to JSON.

        OUTPUT:
        Return strict JSON strictly matching the 'TribunalVerdict' schema.
        narrative_tag must be one of: "top_tier_potential", "solid_performer", "high_risk", "mismatch".
        """
        
        location_block = ""
        if distance_miles is not None or radius_miles is not None:
            jd_location = (jd.job_metadata.location or "").strip() or "Unknown"
            cand_location = (candidate.candidate_metadata.location or "").strip() or "Unknown"
            distance_str = (
                f"{round(float(distance_miles), 1)} mi" if distance_miles is not None else "Unknown"
            )
            radius_str = f"{int(radius_miles)} mi" if radius_miles else "not specified"
            location_block = f"""
        === LOCATION ===
        JD location: {jd_location}; search radius: {radius_str}
        Candidate location: {cand_location}; distance from JD: {distance_str}
        """

        user_prompt = f"""
        RESUME CONTENT (For Nuance):
        {resume_text[:30000]}...

        === CANDIDATE STRUCTURED DATA (TOON) ===
        {candidate_toon}

        === JOB DESCRIPTION (TOON) ===
        {jd_toon}
        {location_block}
        """

        try:
            model = "gpt-4o-mini"
            completion = await self.client.beta.chat.completions.parse(
                model=model, # Optimized model
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt}
                ],
                response_format=TribunalVerdict,
                temperature=0.2, # low temp for consistent tagging
                prompt_cache_key="tribunal-v1",
            )

            verdict = completion.choices[0].message.parsed
            try:
                await llm_cache.set_json(
                    cache_key,
                    verdict.model_dump(),
                    ttl_seconds=_TRIBUNAL_CACHE_TTL_SECONDS,
                )
            except Exception as cache_exc:
                logger.debug(f"tribunal: cache set failed: {cache_exc}")
            return verdict

        except Exception as e:
            print(f"❌ Tribunal Error: {e}")
            return self._fail_open(f"LLM Error: {str(e)}")

    def _fail_open(self, reason: str) -> TribunalVerdict:
        """
        Returns a neutral verdict so we don't block the hard skills engine.
        """
        # We need to construct a valid TribunalVerdict object
        # Since we can't instantiate Pydantic models with arbitrary dicts easily without validation,
        # we construct it manually.
        from core.intelligence import CareerTrajectory
        
        return TribunalVerdict(
            skeptic_summary="Analysis Failed.",
            advocate_summary="Analysis Failed.",
            consensus_flags=[],
            consensus_strengths=[],
            trajectory_analysis=CareerTrajectory(direction="stable", reasoning=f"System skipped analysis: {reason}"),
            narrative_tag="analysis_failed"
        )
