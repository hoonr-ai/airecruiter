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

        # AZURE AGENT COMMENTED OUT - Using LLM-only extraction for now
        # TODO: Re-enable Azure Agent when rate limiting issues are resolved
        # if AZURE_AGENT_AVAILABLE and _azure_agent:
        #     try:
        #         agent_result = await _azure_agent.extract_roles_and_skills(grounding_text)
        #
        #         logger.info("=" * 80)
        #         logger.info("🛠️  Step 4: Extract grounded roles and placeholder skills from taxonomy.")
        #         logger.info("-" * 40)
        #
        #         grounded_roles = _azure_agent.convert_to_rubric_roles(
        #             agent_result.get("job_roles", []),
        #             target_job_title=job_title,
        #         )
        #         all_grounded = _azure_agent.convert_to_rubric_skills(
        #             agent_result.get("job_skills") or agent_result.get("skills") or []
        #         )
        #         
        #         if grounded_roles:
        #             logger.info(f"   👔 PRIMARY TITLE : {grounded_roles[0]['value']}")
        #         
        #         logger.info(f"   🛠️  GROUNDED SKILLS ({len(all_grounded)}) queued for LLM categorization.")
        #         for s in all_grounded:
        #             logger.info(f"      - {s['value']}")
        #                 
        #     except Exception as azure_err:
        #         logger.error(f"❌ Azure Agent call failed: {azure_err}")
        
        logger.info("📝 Azure Agent disabled - Using LLM-only extraction")

        logger.info("=" * 80)
        logger.info("🧠 Step 5: Extract general rubric details & Categorize skills via LLM.")
        logger.info("-" * 40)

        skill_names = [s['value'] for s in all_grounded]
        
        phase2_prompt = f"""
You are a strict recruitment extraction engine.
Read the following job description and extract specific facts.

JOB CONTEXT:
- Job title: {enhanced_job_title or job_title}
- Customer: {customer_name or "N/A"}

TITLE HINT (applies to SKILLS section below):
- If the job title contains a specific technology, tool, framework, platform, language, or product
  name (e.g. "Databricks" in "Databricks Data Engineer", "Snowflake" in "Snowflake Architect",
  "Salesforce" in "Salesforce Developer", "Kubernetes" in "Kubernetes SRE"), you MUST include that
  technology as a skill AND mark its "importance" as "required" and "evidence_type" as "direct".
  The title is treated as authoritative evidence — a role named after a technology implies the
  candidate must have that technology.

1. EDUCATION:
   - Choose ONLY from ["High School / GED", "Associate's degree", "Bachelor's degree", "Master's degree", "PhD or equivalent", "Certification / License"]
   - field: The specific field (e.g. "Biology").
   - MANDATORY: If multiple fields of study are mentioned for the same degree (e.g. "Engineering, Life Sciences, or Biology"), extract EACH as a separate entry in the 'education' array.
   - MANDATORY: If a degree is mentioned without a direct subject (e.g. "Associate degree with 5 years experience in..."), search the SURROUNDING context for the specialty (e.g. "Laboratory Automation") and use that as the Field.
   - DO NOT use the degree level name as the Field.
   - IGNORE "Board Certified" or general descriptors.

2. DOMAIN (INDUSTRY SECTOR ONLY — NOT job function):
   - Extract the CUSTOMER's industry sector. The customer name is "{customer_name or 'unknown'}".
     Use world knowledge of that company (e.g. Cummins → "Diesel Engines" / "Automotive",
     Pfizer → "Pharmaceuticals", JPMorgan Chase → "Banking", Boeing → "Aerospace") to determine
     the sector.
   - VALID examples: "Automotive", "Diesel Engines", "Healthcare", "Insurance", "Banking",
     "Pharmaceuticals", "Aerospace", "Retail", "Telecom", "Oil & Gas", "Manufacturing",
     "Government", "Education", "Media", "Hospitality", "Logistics".
   - INVALID — DO NOT EXTRACT any of these (they are job functions, not industries):
     "Data Engineering", "Software Development", "QA", "DevOps", "Machine Learning",
     "Cloud Engineering", "Product Management", "Analytics", "Security", "Sales",
     "Marketing", "Customer Support".
   - If the customer's industry sector is not obvious from the JD text or your world
     knowledge, return an EMPTY array rather than guessing.

3. CUSTOMER REQUIREMENTS:
   - DEFAULT: If customer name is provided, ALWAYS include: "Must not be employed by {customer_name}."
   - Extract any EXPLICIT non-employment restrictions if mentioned.

4. OTHER REQUIREMENTS:
   - Extract Shift (Day/Night, Rotating), Work Authorization, or Travel %.
   - MANDATORY: Use concise, professional label-based formatting (e.g. "Label: Value").
   - MANDATORY: Keep under 10 words per requirement.
   - WORK AUTHORIZATION NORMALIZATION: When the JD mentions work-authorization, normalize the
     extracted value to include one of these standard labels (comma-separated if multiple):
     W2, 1099, Corp-to-Corp (C2C), H1B, Green Card, US Citizen, TN Visa, OPT/CPT, Any.
     Examples:
       * JD says "W2 only" or "W2 candidates" → "Work Authorization: W2 only."
       * JD says "US Citizens or Green Card holders" → "Work Authorization: US Citizen or Green Card."
       * JD says "must be authorized to work in the US" (no specific type) → "Work Authorization: Any US work authorization."
       * JD says "No C2C" → "Work Authorization: W2 or 1099 (no C2C)."
     If the JD is silent on work-authorization, omit this requirement entirely — do NOT
     invent one.
   - EXAMPLES:
     * "Shift: Day and night shifts required." (NOT "Day and night shifts are required.")
     * "Work Authorization: W2 only." (NOT "Candidate must be authorized to work in the United States.")
     * "Travel: Up to 25% travel expected." (NOT "Up to 25% travel is expected for this role.")
   - DO NOT extract Location or years of experience.

5. SKILLS (CRITICAL - Extract ALL explicit AND implied skills from JD):
   - Extract ALL skills mentioned in the job description (both hard and soft skills).
   - PRIORITY ORDER: 
     1. EXPLICIT SKILLS FIRST: Skills that are directly listed in skill sections, requirements lists, or clearly stated (e.g., "Required Skills: Python, AWS, Docker")
     2. DIRECT MENTIONS: Skills explicitly mentioned in sentences (e.g., "Must have experience with React and Node.js")
     3. INFERRED SKILLS LAST: Skills that must be inferred from job responsibilities (e.g., "Build APIs" → "REST API Development")
   - CRITICAL: You MUST infer skills from sentences, job responsibilities, and context - not just explicitly listed skills.
   - READ BETWEEN THE LINES: If a sentence describes a task or responsibility, extract the underlying skills needed to perform it.
   
   EXAMPLES OF EXPLICIT/DIRECT SKILLS (HIGHEST PRIORITY):
   - "Required: Python, JavaScript, AWS" → extract: ["Python", "JavaScript", "AWS"]
   - "Must have 5+ years of Docker experience" → extract: ["Docker"]
   - "Proficiency in SQL required" → extract: ["SQL"]
   
   EXAMPLES OF INFERRED SKILLS (LOWER PRIORITY - only if under 8 skills):
   - "Build RESTful APIs" → infer skills: ["REST API Development", "Backend Development", "API Design"]
   - "Manage cloud infrastructure on AWS" → infer skills: ["AWS", "Cloud Infrastructure Management", "DevOps"]
   - "Analyze data to drive business decisions" → infer skills: ["Data Analysis", "Business Intelligence", "Statistical Analysis"]
   - "Collaborate with cross-functional teams" → infer skills: ["Cross-functional Collaboration", "Team Communication"]
   - "Optimize database queries for performance" → infer skills: ["Database Optimization", "SQL", "Performance Tuning"]
   - "Implement CI/CD pipelines" → infer skills: ["CI/CD", "DevOps", "Automation"]
   - "Conduct code reviews" → infer skills: ["Code Review", "Software Quality Assurance"]
   - "Design microservices architecture" → infer skills: ["Microservices Architecture", "System Design", "Distributed Systems"]
   
   For each skill, extract:
     - "name": The skill name
     - "category": One of ["hard", "soft"]
     - "hard": Measurable technical skills, tools, or procedures (e.g., "Python", "AWS", "Docker", "DAST", "SAST").
       - "soft": Interpersonal or behavioral skills (e.g., "Communication", "Teamwork", "Leadership", "Problem-solving").
     - "importance": One of ["required", "preferred"] - determine based on context ("must have" = required, "nice to have" = preferred, implied responsibilities = required)
     - "min_years": Minimum years of experience for THIS skill if explicitly mentioned, otherwise use the global min_years_experience value
     - "evidence_type": One of ["direct", "inferred"]
       - "direct": Explicitly present in the JD, requirements, qualifications, tools, certifications, procedures, duties, or responsibilities.
       - "inferred": Strongly implied by the role but not stated directly. Only use if fewer than 8 hard skills are available directly from the JD.
   - CRITICAL FORMATTING RULES FOR SKILL NAMES:
     - Use proper Title Case (capitalize first letter of each word): "Radiographic Equipment Operation", NOT "radiographic equipment operation"
     - Fix typos and misspellings: "Radiographic" NOT "Ragiographic"
     - Use singular form for procedures/skills: "Radiographic Procedure" NOT "Radiographic Procedures"
     - Use standard professional terminology: "Radiation Safety Standards" NOT "radiation safety practices"
     - Remove punctuation errors: "Patient Care" NOT "Patient Care,"
     - Keep skill names concise and professional (2-5 words typically)
   - COMPREHENSIVENESS: Extract the MOST IMPORTANT skills only.
   - TARGET: Return UP TO 8 HARD SKILLS FROM THE JD ITSELF. You may also return soft skills separately, but soft skills must NOT displace or reduce the number of hard skills.
   - The total number of items in the `skills` array may exceed 8 if that is needed to include soft skills in addition to up to 8 hard skills.
   - Prioritize the most critical and essential skills for the role.
   - Focus on core technical competencies and key soft skills.
   - **CRITICAL PRIORITY RULE**: Always include explicit/direct hard skills FIRST before considering inferred hard skills.
   - If the JD explicitly supports more than 3 hard skills, keep extracting until you have the strongest set of up to 8 hard skills.
   - Infer additional hard skills only from the JD text, responsibilities, tools, procedures, workflows, and qualifications. Do NOT invent skills unrelated to the JD.
   - Skills like Patient Care, Communication, Teamwork, Flexibility, Attention to Detail, Empathy, Collaboration, and Customer Service are SOFT skills, not hard skills.
   - Look for skills in:
     * Explicit skill lists (HIGHEST PRIORITY)
     * Required qualifications sections (HIGH PRIORITY)
     * Job responsibilities and duties (MEDIUM PRIORITY - infer from these)
     * Day-to-day activities described (MEDIUM PRIORITY - infer from these)
     * Tools and technologies mentioned in context (MEDIUM PRIORITY)
     * Methodologies and frameworks implied (LOWER PRIORITY - infer only if needed)
     * Domain knowledge required (LOWER PRIORITY - infer only if needed)

6. EXPERIENCE:
   - Extract the MINIMUM number of years of total experience required as a single number (e.g., 4).
   - If there is an "OR" condition (e.g. "2 years with Bachelor OR 5 years with Associate"), extract the LOWER number (e.g. 2).
   - DO NOT include education-related details or degree levels in "other_requirements".
   - Return 0 if not explicitly mentioned.

7. JOB ROLE:
   - Extract the most appropriate standardized job title(s) for this position.
   - IMPORTANT: All match types must be "Similar" - do not use any other match type.

JD TEXT:
{grounding_text}

Return JSON:
{{ 
  "job_roles": [ {{ "name": "Role Title", "match_type": "Similar", "required": "Preferred" }} ],
  "education": [], 
  "domain": [], 
  "customer_requirements": [], 
  "other_requirements": [],
  "min_years_experience": 0,
  "skills": [ 
     {{ "name": "Skill Name", "category": "hard/soft", "importance": "required/preferred", "min_years": 0, "evidence_type": "direct/inferred" }} 
  ]
}}

IMPORTANT: 
- All job_roles MUST have "match_type": "Similar" - this is mandatory.
- Do not use any other match type values like "Exact", "Broad", etc.
"""
        try:
            p2_resp = await self.openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are an expert recruiter and skills analyst. Extract up to 8 HARD skills from the JD itself, plus any truly important SOFT skills. HARD SKILLS ARE THE PRIORITY. Soft skills must never crowd out hard skills or reduce the hard-skill count. The skills array may contain more than 8 total items if needed, but no more than 8 should be hard skills. PRIORITY ORDER FOR HARD SKILLS: 1) Explicit skills listed in requirements/qualifications/tools/procedures (HIGHEST), 2) Direct skill mentions in duties or responsibilities (HIGH), 3) Strongly inferred hard skills from the JD only if fewer than 8 direct hard skills are available (MEDIUM). Patient Care, Communication, Teamwork, Flexibility, Attention to Detail, Empathy, Collaboration, and Customer Service are soft skills. Mark each skill with evidence_type = direct or inferred."},
                    {"role": "user", "content": phase2_prompt}
                ],
                temperature=0.2,  # Slightly higher to encourage more comprehensive extraction
                response_format={"type": "json_object"},
            )
            phase2_result = json.loads(p2_resp.choices[0].message.content)
        except Exception as p2_err:
            logger.error(f"❌ Phase 2 failed: {p2_err}")
            phase2_result = {}

        # Merge results - Extract ALL skills from LLM and categorize
        grounded_hard_skills = []
        grounded_soft_skills = []
        other_requirements = []
        customer_requirements = []
        min_years = int(phase2_result.get("min_years_experience", 0))
        
        normalized_grounding_text = "".join(ch.lower() if ch.isalnum() else " " for ch in grounding_text)

        def skill_priority(item: dict) -> tuple[int, int, int, str]:
            evidence_type = (item.get("evidence_type") or "").lower()
            importance = (item.get("importance") or "preferred").lower()
            value = item.get("value", "")
            normalized_value = " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())
            is_direct_text_match = bool(normalized_value and normalized_value in normalized_grounding_text)

            direct_rank = 0 if evidence_type == "direct" or is_direct_text_match else 1
            importance_rank = 0 if importance == "required" else 1
            inferred_rank = 0 if is_direct_text_match else 1
            return (direct_rank, importance_rank, inferred_rank, value)

        # Extract ALL Skills from LLM (Azure Agent disabled)
        skills_from_llm = phase2_result.get("skills", [])
        logger.info(f"📊 LLM returned {len(skills_from_llm)} skills before categorization")

        # Strict filter: Remove any skills that are certifications or education
        def is_cert_or_edu(skill_name: str) -> bool:
            name = skill_name.lower()
            cert_keywords = [
                "certification", "certified", "license", "licence", "licensure", "registration", "registered",
                "diploma", "degree", "bachelor", "master", "phd", "doctor", "associate", "ged", "high school"
            ]
            # e.g. "Basic Life Support Certification", "Registered Nurse License", "Bachelor's Degree in IT"
            return any(kw in name for kw in cert_keywords)

        def normalize_skill_category(skill_name: str, category: str) -> str:
            name = skill_name.lower().strip()
            soft_skill_phrases = {
                "patient care",
                "communication",
                "communication skills",
                "teamwork",
                "attention to detail",
                "flexibility",
                "empathy",
                "collaboration",
                "customer service",
                "interpersonal skills",
                "problem solving",
                "problem-solving",
                "adaptability",
                "time management",
                "active listening",
                "compassion",
                "professionalism",
                "bedside manner",
                "relationship building",
            }
            if name in soft_skill_phrases:
                return "soft"
            return category

        for item in skills_from_llm:
            if isinstance(item, dict) and 'name' in item:
                if is_cert_or_edu(item["name"]):
                    continue  # Skip certifications and education in skills
                evidence_type = (item.get('evidence_type') or '').lower()
                skill_min_years = item.get('min_years', min_years)
                category = normalize_skill_category(item["name"], item.get('category', 'hard').lower())
                normalized_value = " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in item["name"]).split())
                is_direct_text_match = bool(normalized_value and normalized_value in normalized_grounding_text)
                is_direct_hard_skill = category == "hard" and (evidence_type == "direct" or is_direct_text_match)
                required_label = "Required" if is_direct_hard_skill else ("Preferred" if category == "hard" else item.get('importance', 'preferred').capitalize())
                importance = required_label.lower()

                skill_obj = {
                    "value": item["name"],
                    "source": "PAIR",
                    "matchType": "Similar",  # Always use Similar for all skills
                    "importance": importance,
                    "required": required_label,
                    "minYears": skill_min_years if skill_min_years else min_years,
                    "category": category,
                    "evidence_type": "direct" if is_direct_hard_skill else (evidence_type or ("direct" if is_direct_text_match else "inferred"))
                }

                # Separate into hard and soft skills based on category
                if category == "soft":
                    grounded_soft_skills.append(skill_obj)
                else:
                    grounded_hard_skills.append(skill_obj)

        grounded_hard_skills.sort(key=skill_priority)
        # Limit to a maximum of 8 hard skills (soft skills are not counted in this limit)
        grounded_hard_skills = grounded_hard_skills[:8]
                    
        # Extract Job roles using LLM - ALWAYS use "Similar" match type
        if not grounded_roles:
            for item in phase2_result.get("job_roles", []):
                if isinstance(item, dict) and 'name' in item:
                    grounded_roles.append({
                        "value": item["name"], 
                        "source": "PAIR",
                        "matchType": "Similar",  # Always use Similar for job titles
                        "required": item.get('required', 'Preferred')
                    })

        if not grounded_roles:
            final_titles = [{"value": enhanced_job_title or job_title or "No Title", "source": "PAIR", "minYears": min_years, "matchType": "Similar", "required": "Preferred"}]
        else:
            final_titles = []
            for r in grounded_roles:
                r['minYears'] = min_years
                r['matchType'] = 'Similar'  # Always use Similar for job titles
                r['required'] = r.get('required', 'Preferred')
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
            
            # Ensure the value ends with a period (full stop)
            val = val.strip()
            if not val.endswith('.'):
                val = val + '.'
            
            if isinstance(r, dict): 
                r['value'] = val
                other_requirements.append(r)
            elif isinstance(r, str): 
                other_requirements.append({"value": val, "required": "Required"})

        # Customer Requirements (and routing technical ones back to Other)
        customer_requirements_raw = phase2_result.get("customer_requirements", [])
        customer_requirements = []

        def infer_customer_requirement_type(text: str) -> str:
            text_upper = (text or "").upper()
            if "PREVIOUS" in text_upper or "WORKED" in text_upper:
                return "Previously employed by"
            if "CURRENT" in text_upper:
                return "Currently employed by"
            return "Must not be employed by"

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
                req_type = infer_customer_requirement_type(val)
                if customer_name:
                    val = customer_name
                else:
                    val = "the client"
                    
                customer_requirements.append({"type": req_type, "value": val, "required": "Required"})
            else:
                # Salvage to Other Requirements since it doesn't fit the solicitation dropdown
                # Ensure it ends with a period
                if not val.endswith('.'):
                    val = val + '.'
                other_requirements.append({"value": val, "required": "Required"})
        
        if not customer_requirements and customer_name:
            customer_requirements.append({
                "type": "Must not be employed by", 
                "value": customer_name, 
                "required": "Required"
            })

        # Other Requirements
        raw_other = phase2_result.get("other_requirements", [])
        for r in raw_other:
            val = r.get("value", "") if isinstance(r, dict) else str(r)
            if not val.strip(): continue
            # Avoid location duplication (even if AI ignores the 'DO NOT extract location' rule)
            if "LOCATION" in val.upper(): continue
            if len(val.split()) < 4: continue
            
            # Ensure the value ends with a period (full stop)
            val = val.strip()
            if not val.endswith('.'):
                val = val + '.'
            
            if isinstance(r, dict): 
                r['value'] = val
                other_requirements.append(r)
            elif isinstance(r, str): 
                other_requirements.append({"value": val, "required": "Required"})

        # Deduplicate other_requirements by normalized value (case-insensitive)
        seen_other = set()
        unique_other = []
        for req in other_requirements:
            val = req.get("value", "") if isinstance(req, dict) else str(req)
            key = val.strip().lower()
            if key not in seen_other:
                seen_other.add(key)
                unique_other.append(req)
        other_requirements = unique_other

        # Final Location logic with cleanup
        if job_location:
            other_requirements.append({"value": f"Location: {job_location}.", "required": "Required"})

        # Enforce maximum of 8 hard skills. Soft skills do not consume the hard-skill cap.
        total_hard_skills_before = len(grounded_hard_skills)
        if total_hard_skills_before > 8:
            # Prioritize required hard skills, then preferred hard skills, up to 8 total hard skills.
            required_hard = [s for s in grounded_hard_skills if s.get('importance') == 'required']
            preferred_hard = [s for s in grounded_hard_skills if s.get('importance') == 'preferred']
            
            grounded_hard_skills = []
            
            # Add required hard skills first
            for s in required_hard:
                if len(grounded_hard_skills) < 8:
                    grounded_hard_skills.append(s)
            
            # Add preferred hard skills if room
            for s in preferred_hard:
                if len(grounded_hard_skills) < 8:
                    grounded_hard_skills.append(s)

            logger.info(f"⚠️  Limited hard skills from {total_hard_skills_before} to {len(grounded_hard_skills)} (max 8)")

        # Log Step 5 Results
        total_skills = len(grounded_hard_skills) + len(grounded_soft_skills)
        logger.info(f"   📊 TOTAL SKILLS EXTRACTED: {total_skills} (hard skills capped at 8)")
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
