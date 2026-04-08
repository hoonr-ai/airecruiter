#!/usr/bin/env python3
"""
job_skills_extractor.py
-----------------------
Two-phase rubric extraction for Step 3 of the job creation wizard.
"""

from typing import List, Dict, Optional
import re
import json
import logging
from dataclasses import dataclass
from core.graph import ontology
import openai

logger = logging.getLogger(__name__)

# ── Azure AI Agent bootstrap ──────────────────────────────────────────────────
_azure_agent = None
AZURE_AGENT_AVAILABLE = False

try:
    from services.azure_agent_service import AzureAgentService
    from core import AZURE_AI_PROJECT_ENDPOINT, AZURE_OPENAI_API_KEY, AZURE_AI_AGENT_NAME

    if AZURE_AI_PROJECT_ENDPOINT and AZURE_OPENAI_API_KEY:
        _azure_agent = AzureAgentService(
            project_endpoint=AZURE_AI_PROJECT_ENDPOINT,
            api_key=AZURE_OPENAI_API_KEY,
            agent_name=AZURE_AI_AGENT_NAME,
        )
        AZURE_AGENT_AVAILABLE = True
        logger.info(f"✅ LLMExtractor: Azure Agent '{AZURE_AI_AGENT_NAME}' initialized for grounding")
    else:
        logger.warning("⚠️  Azure AI Agent: endpoint/key not configured")
except Exception as _azure_err:
    logger.warning(f"⚠️  Azure AI Agent NOT available: {_azure_err}")


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ExtractedSkill:
    original_text:  str
    normalized_name: str
    skill_id:       Optional[str]
    importance:     str
    min_years:      int   = 0
    proficiency:    Optional[str] = None
    confidence:     float = 0.0
    source_context: str   = ""
    category:       str   = "hard"

@dataclass
class JobRubric:
    job_id:               str = ""
    job_title:            str = ""
    titles:               List[Dict] = None
    hard_skills:          List[Dict] = None
    soft_skills:          List[Dict] = None
    education:            List[Dict] = None
    domain:               List[Dict] = None
    customer_requirements: List[Dict] = None
    other_requirements:   List[Dict] = None
    skills:               List[Dict] = None # Legacy/redundant alias

@dataclass
class JobSkillsAnalysis:
    job_id:             str
    extracted_skills:   List[ExtractedSkill]
    unmapped_skills:    List[str]
    analysis_metadata:  Dict


# ── Main extractor class ──────────────────────────────────────────────────────

class JobSkillsExtractor:
    def __init__(self, openai_api_key: str):
        self.openai_client = openai.AsyncOpenAI(api_key=openai_api_key)

    def _combine_job_texts(self, jobdiva: str, ai: str, notes: str) -> str:
        sections = []
        if jobdiva: sections.append(f"JobDiva Description:\n{jobdiva}")
        if ai:      sections.append(f"Enhanced Description:\n{ai}")
        if notes:   sections.append(f"Recruiter Notes:\n{notes}")
        return "\n\n---\n\n".join(sections)

    async def extract_full_rubric(
        self,
        job_id:           str,
        job_title:        str,
        enhanced_job_title: str = "",
        jobdiva_description: str = "",
        ai_description:   str = "",
        recruiter_notes:  str = "",
        customer_name:    str = "",
        job_location:     str = "",
        location_type:    str = "on-site",
    ) -> JobRubric:
        grounding_text = (
            ai_description.strip()
            if ai_description and len(ai_description) > 100
            else self._combine_job_texts(jobdiva_description, ai_description, recruiter_notes)
        )

        education            = []
        grounded_roles       = []
        all_grounded         = []

        if AZURE_AGENT_AVAILABLE and _azure_agent:
            try:
                agent_result = await _azure_agent.extract_roles_and_skills(grounding_text)

                logger.info("=" * 80)
                logger.info("🛠️  Step 4: Extract grounded roles and placeholder skills from taxonomy.")
                logger.info("-" * 40)

                grounded_roles = _azure_agent.convert_to_rubric_roles(
                    agent_result.get("job_roles", []),
                    target_job_title=job_title,
                )
                all_grounded = _azure_agent.convert_to_rubric_skills(
                    agent_result.get("job_skills") or agent_result.get("skills") or []
                )
                
                if grounded_roles:
                    logger.info(f"   👔 PRIMARY TITLE : {grounded_roles[0]['value']}")
                
                logger.info(f"   🛠️  GROUNDED SKILLS ({len(all_grounded)}) queued for LLM categorization.")
                for s in all_grounded:
                    logger.info(f"      - {s['value']}")
                        
            except Exception as azure_err:
                logger.error(f"❌ Azure Agent call failed: {azure_err}")

        logger.info("=" * 80)
        logger.info("🧠 Step 5: Extract general rubric details & Categorize skills via LLM.")
        logger.info("-" * 40)

        skill_names = [s['value'] for s in all_grounded]
        
        phase2_prompt = f"""
You are a strict recruitment extraction engine. 
Read the following job description and extract specific facts.

1. EDUCATION: 
   - Choose ONLY from ["High School / GED", "Associate's degree", "Bachelor's degree", "Master's degree", "PhD or equivalent", "Certification / License"]
   - field: The specific field (e.g. "Biology"). 
   - MANDATORY: If multiple fields of study are mentioned for the same degree (e.g. "Engineering, Life Sciences, or Biology"), extract EACH as a separate entry in the 'education' array.
   - MANDATORY: If a degree is mentioned without a direct subject (e.g. "Associate degree with 5 years experience in..."), search the SURROUNDING context for the specialty (e.g. "Laboratory Automation") and use that as the Field. 
   - DO NOT use the degree level name as the Field.
   - IGNORE "Board Certified" or general descriptors.

2. DOMAIN: 
   - Short industry names (e.g. "Healthcare").

3. CUSTOMER REQUIREMENTS:
   - Extract EXPLICIT non-employment restrictions.
   - MANDATORY: If a non-employment restriction exists, ONLY output the exact sentence: "Must not have been employed by {customer_name if customer_name else 'the client'}."
   - DO NOT include any other details, timeframes (e.g. "6 months"), or conditions.
   - Keep it strictly to that one sentence.

4. OTHER REQUIREMENTS:
   - Extract Shift (Day/Night, Rotating), Work Authorization, or Travel %.
   - MANDATORY: Use concise, professional sentences. Keep under 15 words.
   - DO NOT extract Location or years of experience.

5. SKILL CATEGORIZATION:
   - Categorize each skill into: ["hard", "soft", "certification"]
   - "hard": Measurable technical skills, tools, or procedures (e.g., "X-Ray", "Vascular Access").
   - "soft": Any skill that cannot be measured (e.g., "Patient Care", "Bedside Manner", "Clinical Behaviors", "Communication").
   - "certification": Licenses or professional certifications (e.g., "BLS", "ARRT").

6. EXPERIENCE:
   - Extract the MINIMUM number of years of total experience required as a single number (e.g., 4).
   - If there is an "OR" condition (e.g. "2 years with Bachelor OR 5 years with Associate"), extract the LOWER number (e.g. 2).
   - DO NOT include education-related details or degree levels in "other_requirements".
   - Return 0 if not explicitly mentioned.

JD TEXT:
{grounding_text}

Return JSON:
{{ 
  "education": [], 
  "domain": [], 
  "customer_requirements": [], 
  "other_requirements": [],
  "min_years_experience": 0,
  "categorized_skills": [ 
     {{ "name": "Skill Name", "category": "hard/soft/certification" }} 
  ]
}}
"""
        try:
            p2_resp = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": phase2_prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            phase2_result = json.loads(p2_resp.choices[0].message.content)
        except Exception as p2_err:
            logger.error(f"❌ Phase 2 failed: {p2_err}")
            phase2_result = {}

        # Merge results - Categorized Skills
        grounded_hard_skills = []
        grounded_soft_skills = []
        
        min_years = int(phase2_result.get("min_years_experience", 0))
        
        cat_map = {item['name'].upper(): item['category'].lower() 
                   for item in phase2_result.get("categorized_skills", []) 
                   if isinstance(item, dict) and 'name' in item}

        for s in all_grounded:
            s_name = s['value'].upper()
            cat = cat_map.get(s_name, "hard")
            
            # Logic Guardrail: If name contains 'Certification' or 'License', force category
            if any(term in s_name for term in ["CERTIFICATION", "LICENSE", "ARRT", "BLS", "CPR"]):
                cat = "certification"
            
            # Logic Guardrail: If name contains behavioral keywords, force soft
            if any(term in s_name for term in ["CARE", "COMMUNICATION", "INTERPERSONAL", "BEHAVIOR", "INTERACTION"]):
                cat = "soft"

            if cat == "certification":
                education.append({
                    "degree": "Certification / License",
                    "field": s['value'],
                    "required": "Required",
                    "source": "PAIR (Agent)"
                })
            elif cat == "soft":
                s['category'] = "soft"
                grounded_soft_skills.append(s)
            else:
                s['category'] = "hard"
                s['minYears'] = min_years
                grounded_hard_skills.append(s)

        if not grounded_roles:
            final_titles = [{"value": enhanced_job_title or job_title or "No Title", "source": "PAIR", "minYears": min_years}]
        else:
            final_titles = []
            for r in grounded_roles:
                r['minYears'] = min_years
                final_titles.append(r)

        # Normalise additional education items
        education_raw = phase2_result.get("education", [])
        for e in education_raw:
            field = e.get("field", "") if isinstance(e, dict) else str(e)
            if not field or "BOARD CERTIFIED" in field.upper() or field.upper() == "CERTIFICATION / LICENSE":
                continue
            
            field_upper = field.upper()
            degree = e.get("degree", "Bachelor's degree") if isinstance(e, dict) else "Certification / License"
            
            # Logic Guardrail: Discard generic/placeholder fields
            DISCARD_FIELDS = ["RELATED DISCIPLINE", "RELATED FIELD", "EQUIVALENT", "RELATED AREA", "RELATED SUBJECT", "TECHNICAL TRAINING"]
            if any(term in field_upper.strip() for term in DISCARD_FIELDS):
                continue
            
            # Logic Guardrail: Ensure GED/High School and Certifications are mapped correctly
            if "GED" in field_upper or "HIGH SCHOOL" in field_upper:
                degree = "High School / GED"
            elif "CERTIFICATION" in field_upper or "LICENSE" in field_upper:
                degree = "Certification / License"
            elif "ASSOCIATE" in field_upper:
                degree = "Associate's degree"
            elif "MASTER" in field_upper:
                degree = "Master's degree"
            elif "PHD" in field_upper or "DOCTOR" in field_upper:
                degree = "PhD or equivalent"

            education.append({
                "degree": degree,
                "field": field,
                "required": e.get("required", "Required") if isinstance(e, dict) else "Required"
            })

        # Strict Deduplication for Education & Certifications
        unique_edu = []
        seen_edu = set()
        for item in education:
            key = f"{item['degree']}|{item['field']}".upper().strip()
            if key not in seen_edu:
                unique_edu.append(item)
                seen_edu.add(key)
        education = unique_edu

        # Normalise Domain
        domain_raw = phase2_result.get("domain", [])
        domain = []
        for d in domain_raw:
            val = d.get("value", "") if isinstance(d, dict) else str(d)
            if not val.strip(): continue
            if isinstance(d, dict): domain.append(d)
            elif isinstance(d, str): domain.append({"value": val, "required": "Required"})

        # Other Requirements
        other_requirements = []
        raw_other = phase2_result.get("other_requirements", [])
        for r in raw_other:
            val = r.get("value", "") if isinstance(r, dict) else str(r)
            if not val.strip(): continue
            # Avoid location duplication if LLM ignored the instruction
            if "LOCATION" in val.upper(): continue
            if len(val.split()) < 4: continue 
            
            if isinstance(r, dict): other_requirements.append(r)
            elif isinstance(r, str): other_requirements.append({"value": val, "required": "Required"})

        # Customer Requirements (and routing technical ones back to Other)
        customer_requirements_raw = phase2_result.get("customer_requirements", [])
        customer_requirements = []
        for r in customer_requirements_raw:
            val = r.get("value", "") if isinstance(r, dict) else str(r)
            if not val.strip(): continue
            
            # String Replacement Guardrail: Swap "the client/company" with actual name
            if customer_name:
                val = val.replace("the client", customer_name).replace("the company", customer_name)
                val = val.replace("The client", customer_name).replace("The company", customer_name)
                val = val.replace("THE CLIENT", customer_name).replace("THE COMPANY", customer_name)

            val_upper = val.upper()
            is_true_customer_req = any(term in val_upper for term in ["EMPLOYED", "WORKED", "CLIENT", "NON-SOLICIT", "SOLICITATION"])
            
            if is_true_customer_req:
                # Only return the candidate name as the UI dropdown already has the prefix
                if customer_name:
                    val = customer_name
                else:
                    val = "the client"
                    
                customer_requirements.append({"value": val, "required": "Required"})
            else:
                # Salvage to Other Requirements since it doesn't fit the solicitation dropdown
                other_requirements.append({"value": val, "required": "Required"})
        
        if not customer_requirements and customer_name:
            customer_requirements.append({
                "type": "Must not be employed by", 
                "value": customer_name, 
                "required": "Required"
            })

        # Other Requirements
        other_requirements = []
        raw_other = phase2_result.get("other_requirements", [])
        for r in raw_other:
            val = r.get("value", "") if isinstance(r, dict) else str(r)
            if not val.strip(): continue
            # Avoid location duplication (even if AI ignores the 'DO NOT extract location' rule)
            if "LOCATION" in val.upper(): continue
            if len(val.split()) < 4: continue 
            
            if isinstance(r, dict): other_requirements.append(r)
            elif isinstance(r, str): other_requirements.append({"value": val, "required": "Required"})

        # Final Location logic with cleanup
        if job_location:
            other_requirements.append({"value": f"The work location for this role is {job_location}.", "required": "Required"})

        # Log Step 5 Results
        logger.info(f"   🛠️  HARD SKILLS ({len(grounded_hard_skills)}):")
        for s in grounded_hard_skills: logger.info(f"      - {s['value']}")
        if grounded_soft_skills:
            logger.info(f"   🧠 SOFT SKILLS ({len(grounded_soft_skills)}):")
            for s in grounded_soft_skills: logger.info(f"      - {s['value']}")

        logger.info(f"   🎓 EDUCATION/CERTS ({len(education)}):")
        for e in education:
            logger.info(f"      - {e.get('degree')} : {e.get('field')}")
            
        logger.info(f"   🏢 DOMAIN: {[d.get('value', '') for d in domain]}")
        logger.info(f"   📋 CUST REQS: {[r.get('value', '') for r in customer_requirements]}")
        logger.info(f"   📝 OTHER REQS: {[r.get('value', '') for r in other_requirements]}")
        
        logger.info("=" * 80)
        logger.info("✅ [Success] Full rubric extraction complete")
        logger.info("=" * 80)

        return JobRubric(
            job_id=job_id, 
            job_title=final_titles[0]["value"], 
            titles=final_titles,
            hard_skills=grounded_hard_skills, 
            soft_skills=grounded_soft_skills,
            education=education, 
            domain=domain,
            customer_requirements=customer_requirements, 
            other_requirements=other_requirements,
            skills=grounded_hard_skills
        )

    async def analyze_job_skills(self, job_id: str, **kwargs) -> JobSkillsAnalysis:
        return JobSkillsAnalysis(job_id=job_id, extracted_skills=[], unmapped_skills=[], analysis_metadata={})

async def process_job_skills(job_id: str, job_data: dict) -> JobSkillsAnalysis:
    return JobSkillsAnalysis(job_id=job_id, extracted_skills=[], unmapped_skills=[], analysis_metadata={})
