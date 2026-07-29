#!/usr/bin/env python3
"""
job_skills_extractor.py
-----------------------
Two-phase rubric extraction for Step 3 of the job creation wizard.

LLM-only — Azure Agent grounding has been retired in favor of a single
LLM extraction pass, with a conditional second-pass through
`taxonomy_service.extract_grounded_rubric` for non-IT roles whose
first-pass rubric came back too thin (rescues phrases the IT-tuned
prompt drops).
"""

from typing import List, Dict, Optional
import re
import json
import logging
from dataclasses import dataclass
from core.graph import ontology
from core import llm_cache
from services.role_family import detect_role_family
from services.taxonomy_service import extract_grounded_rubric
from services import role_taxonomy
from services import rubric_grounding
import openai

logger = logging.getLogger(__name__)

# 30 days. Rubric phase-2 output is a function of the JD text + title +
# customer; recruiters who regenerate the rubric on an unchanged JD pay
# nothing after the first call.
_RUBRIC_PHASE2_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60

# Below this many hard skills, a non-IT JD gets a second-pass through
# the family-aware grounding prompt. IT JDs typically yield 6-8 hard
# skills on the first pass, so they would not trip this threshold even
# if the gate were removed — gating on family makes the IT path
# provably untouched.
_MIN_NON_IT_RUBRIC_SKILLS = 4


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
        # `openai_api_key` is kept in the signature for backwards compat with
        # existing call sites that pass it explicitly; the singleton already
        # reads OPENAI_API_KEY from config so the arg is now unused.
        from core.llm_client import get_openai_client
        self.openai_client = get_openai_client()

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

        family = detect_role_family(enhanced_job_title or job_title, "", [])
        logger.info(f"🧭 Detected role family: {family}")

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
   - **RESUME-MATCHABILITY RULE (read this carefully — it is the single most important rule):**
     Every extracted hard skill is used downstream as a substring search against
     candidate resume text. If the phrase you choose does NOT literally appear
     on a real resume's Skills / Certifications / Tools / Procedures section,
     the candidate will be silently rejected.
     - Extract CONCRETE NOUNS: certifications, credentials, tool/product/framework/
       language names, equipment names, named procedures, named methodologies,
       software titles, named regulations/standards.
     - REJECT ABSTRACT COMPETENCY PHRASES that describe what someone CAN DO
       rather than the proper nouns they would list as proof. The LLM has a
       strong bias to extract these — actively resist it.
       Bad → Good examples (always prefer the right column):
         • "Critical Care Knowledge"        → "ACLS", "BLS", "CCRN", "ICU"
         • "Invasive and Noninvasive Procedures" → "Intubation", "Bronchoscopy", "CPAP", "BiPAP"
         • "Patient Assessment"             → "ABG Analysis", "EKG Interpretation", "Triage"
         • "RRT Credentials"                → "RRT", "Registered Respiratory Therapist"
         • "Documentation Skills" / "Documentation Compliance" → "Epic", "Cerner", "Meditech", "EMR"
         • "Care Management"                → "Case Management", "CCM", "ACM"
         • "Care Plan Development"          → "Care Plan", "ISP", "Treatment Plan"
         • "Collaboration with Providers"   → "Interdisciplinary Team", "MDT"
         • "Quality Assurance"              → "ISO 9001", "Six Sigma", "CQA", "ASQ", "AS9100"
         • "Construction Quality Procedures"→ "ITP", "RFI", "Submittal Review", "Punchlist"
         • "Vendor Management"              → "Procurement", "RFP", "Subcontractor Coordination"
         • "Brand Voice Adaptation"         → "Copywriting", "Style Guide", "Brand Guidelines"
         • "Concept Development"            → "Creative Brief", "Storyboarding"
         • "Advertising Copywriting"        → "Copywriting", "Ad Copy", "Long-form Copy"
         • "Editing and Rewriting"          → "Proofreading", "Line Editing", "Copyediting"
         • "Project Management"             → "PMP", "Scrum", "Jira", "MS Project", "Primavera P6"
     - HEURISTIC: if a skill name ends in "Knowledge", "Skills", "Compliance",
       "Methodology", "Adaptation", "Development", "Best Practices",
       "Procedures" (plural without a named procedure), or contains the words
       "Collaboration", "Coordination", "Management" without a named domain or
       tool — STOP and replace it with the underlying concrete noun a resume
       would list. If you cannot find a concrete noun in the JD, scan the JD
       for: tool/product names, certifications (often 3-5 letter caps like BLS,
       ACLS, CCRN, PMP, AWS, ISO), software titles, named processes, equipment
       names, or regulations — and extract those instead.
     - IT EXCEPTION: For software engineering JDs, the right answer is the
       specific tech stack (e.g. "Python", "AWS", "React", "Docker", "Kafka",
       "Snowflake", "Databricks"). That path is already well-tuned.
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
   - Output **3 to 5 resume-matchable title aliases** for this position, not
     just one. Title is matched downstream by literal substring search against
     candidate resume text, so a single internal/formal title catches almost
     nobody (real candidates list "iOS Developer", not "Application Programmer III").
   - **STRIP LEVEL SUFFIXES AND PREFIXES**. Remove trailing/leading I, II, III,
     IV, V, Sr., Jr., Senior, Junior, Principal, Lead, Staff when they're
     attached as grade markers. Examples:
       • "Application Programmer III" → drop "III"
       • "Sr. Data Engineer III" → "Data Engineer"
       • "Copywriter IV" → "Copywriter"
       • "QA/QC Program Engineer V" → "QA/QC Program Engineer"
   - **CANONICALIZE using the SKILLS as authoritative evidence**. If the input
     title is internal/generic but skills are role-specific, translate to the
     canonical industry title and include common aliases:
       • title="Application Programmer III", skills=[Swift, XCode, iOS, Kotlin, Appium]
         → aliases: ["iOS Developer", "iOS Engineer", "Mobile Developer",
                     "Mobile Engineer", "Application Programmer"]
       • title="Engineer V", skills=[Snowflake, Airflow, dbt, Spark]
         → aliases: ["Data Engineer", "Senior Data Engineer", "Analytics Engineer"]
       • title="Specialist II", skills=[Care Management, RN, BSN]
         → aliases: ["Registered Nurse Case Manager", "Care Manager",
                     "Clinical Case Manager", "RN Care Coordinator"]
   - **Always include common short forms / abbreviations of the role** when
     real resumes use them. Examples:
       • Respiratory Therapist → also include "RRT", "Respiratory Care Practitioner"
       • Registered Nurse → also include "RN", "Staff Nurse"
       • Quality Assurance Engineer → also include "QA Engineer", "QA Analyst"
   - **All match types must be "Similar"**. Do not use Exact, Broad, etc.
   - Order: put the most canonical / most common resume token FIRST. The rest
     are alternatives.
   - **SIMILAR / ADJACENT TITLES**: For the FIRST (most canonical) job_role
     ONLY, also output a "similar_titles" array of 3-8 ADJACENT or ALTERNATIVE
     role titles that a real candidate for THIS SPECIFIC job — given the DOMAIN
     and the SKILLS extracted above — would plausibly also list on their resume,
     and that a recruiter would also want to search for.
     - GROUND every entry in THIS job's domain + skills. Do NOT emit generic
       industry variants the JD never implies. For a HEALTHCARE business-analyst
       JD, GOOD: "Healthcare Business Analyst", "Clinical Business Analyst",
       "Healthcare Systems Analyst"; BAD: "Mortgage Business Analyst", "Robotics
       Analyst", "Payment Business Analyst" (wrong domain — never mentioned).
     - Keep them in the SAME role family as the canonical title (a Business
       Analyst's similar titles are other Business/Systems Analysts, not Nurses).
     - Strip level suffixes/prefixes (I-V, Sr, Jr, Lead, Principal) as above and
       use resume-matchable real titles only — no abstract phrases.
     - QUALITY OVER QUANTITY: if you cannot ground at least 3, return fewer.

JD TEXT:
{grounding_text}

Return JSON:
{{
  "job_roles": [
    {{ "name": "Canonical Title", "match_type": "Similar", "required": "Preferred", "similar_titles": ["Adjacent Title 1", "Adjacent Title 2", "Adjacent Title 3"] }},
    {{ "name": "Alias 1", "match_type": "Similar", "required": "Preferred" }},
    {{ "name": "Alias 2", "match_type": "Similar", "required": "Preferred" }},
    {{ "name": "Alias 3", "match_type": "Similar", "required": "Preferred" }}
  ],
  "education": [],
  "domain": [],
  "customer_requirements": [],
  "other_requirements": [],
  "min_years_experience": 0,
  "skills": [
     {{ "name": "Skill Name", "category": "hard/soft", "importance": "required/preferred", "min_years": 0, "evidence_type": "direct/inferred", "similar_skills": ["Synonym or sub-tool named in the JD"] }}
  ]
}}

IMPORTANT:
- For each skill, "similar_skills" should list synonyms, sub-tools or sibling
  technologies THAT THE JOB DESCRIPTION ITSELF NAMES (e.g. for "Adobe Creative
  Cloud" on a JD that says "Adobe Creative Cloud (Photoshop, Illustrator,
  InDesign)" -> those three). Do not invent tools the JD never mentions; they
  are dropped by a JD-grounding gate anyway. Use [] when there are none.
- "importance" must follow the JD's own Required vs Preferred sections.
- All job_roles MUST have "match_type": "Similar" - this is mandatory.
- Do not use any other match type values like "Exact", "Broad", etc.
- job_roles MUST contain 3 to 5 entries (not just one), with the most
  resume-common token first.
- Only the FIRST job_role carries "similar_titles"; every entry there must be a
  domain-grounded adjacent title in the SAME role family as the canonical title.
"""
        # Cache phase-2 by the user prompt content (the only thing that
        # varies per call — system prompt is byte-identical).
        phase2_cache_key = llm_cache.make_key("rubric_p2_v2", 1, phase2_prompt)
        phase2_result = await llm_cache.get_json(phase2_cache_key)
        if phase2_result is not None:
            logger.info("rubric phase 2: cache HIT")
        else:
            try:
                p2_resp = await self.openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are an expert recruiter and skills analyst. Extract up to 8 HARD skills from the JD itself, plus any truly important SOFT skills. HARD SKILLS ARE THE PRIORITY. Soft skills must never crowd out hard skills or reduce the hard-skill count. The skills array may contain more than 8 total items if needed, but no more than 8 should be hard skills.\n\nTHE SINGLE MOST IMPORTANT RULE: every hard skill name must be a token that would appear verbatim on a real candidate's resume. Skills are matched downstream by literal substring search; abstract competency phrases (e.g. 'Critical Care Knowledge', 'Patient Assessment', 'Documentation Skills', 'Brand Voice Adaptation', 'Quality Assurance', 'Care Management') silently reject every candidate. Prefer concrete proper nouns: certifications (BLS, ACLS, CCRN, RRT, PMP, ISO 9001, AS9100, CQA), tool/product/framework names (Epic, Cerner, Primavera P6, AutoCAD, Bluebeam, Procore, Jira, AWS), equipment/procedure names (Ventilator, Intubation, CPAP, BiPAP, ABG, EKG), software titles, named regulations. If a skill name ends in 'Knowledge', 'Skills', 'Compliance', 'Methodology', 'Best Practices', 'Adaptation', or 'Development', replace it with the underlying concrete noun.\n\nPRIORITY ORDER FOR HARD SKILLS: 1) Explicit skills listed in requirements/qualifications/tools/procedures (HIGHEST), 2) Direct skill mentions in duties or responsibilities (HIGH), 3) Strongly inferred hard skills from the JD only if fewer than 8 direct hard skills are available (MEDIUM). Patient Care, Communication, Teamwork, Flexibility, Attention to Detail, Empathy, Collaboration, and Customer Service are soft skills. Mark each skill with evidence_type = direct or inferred."},
                        {"role": "user", "content": phase2_prompt}
                    ],
                    temperature=0.2,  # Slightly higher to encourage more comprehensive extraction
                    response_format={"type": "json_object"},
                    prompt_cache_key="job-skills-p2-v2",
                )
                phase2_result = json.loads(p2_resp.choices[0].message.content)
                await llm_cache.set_json(
                    phase2_cache_key, phase2_result, ttl_seconds=_RUBRIC_PHASE2_CACHE_TTL_SECONDS
                )
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

        def skill_priority(item: dict) -> tuple[int, int, int, int]:
            evidence_type = (item.get("evidence_type") or "").lower()
            importance = (item.get("importance") or "preferred").lower()
            value = item.get("value", "")
            normalized_value = " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split())
            is_direct_text_match = bool(normalized_value and normalized_value in normalized_grounding_text)

            direct_rank = 0 if evidence_type == "direct" or is_direct_text_match else 1
            importance_rank = 0 if importance == "required" else 1
            inferred_rank = 0 if is_direct_text_match else 1
            # Final tiebreak: where the JD first mentions the skill, NOT the
            # skill name. Alphabetical order used to decide this, and since
            # downstream consumers cap how many skills they AND together, that
            # let "CSS"/"HTML" outrank "Instructional Design" purely on spelling
            # (observed on 26-22970). JD position is a real importance signal —
            # requirements are stated before nice-to-haves.
            position = grounding_text.lower().find(value.lower()) if value else -1
            return (direct_rank, importance_rank, inferred_rank, position if position >= 0 else 10**6)

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
                    # Map the discarded skill containing cert/edu keywords to education/certifications
                    evidence_type = (item.get('evidence_type') or '').lower()
                    category = normalize_skill_category(item["name"], item.get('category', 'hard').lower())
                    normalized_value = " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in item["name"]).split())
                    is_direct_text_match = bool(normalized_value and normalized_value in normalized_grounding_text)
                    is_direct_hard_skill = category == "hard" and (evidence_type == "direct" or is_direct_text_match)
                    required_label = "Required" if is_direct_hard_skill else ("Preferred" if category == "hard" else item.get('importance', 'preferred').capitalize())

                    name_lower = item["name"].lower()
                    if any(kw in name_lower for kw in ["phd", "doctor"]):
                        degree = "PhD or equivalent"
                    elif ("master of" in name_lower or "masters of" in name_lower or "master's" in name_lower or "masters" in name_lower or "degree" in name_lower) and "master" in name_lower and not any(kw in name_lower for kw in ["scrum", "belt", "electrician", "plumber", "agile"]):
                        degree = "Master's degree"
                    elif "bachelor" in name_lower:
                        degree = "Bachelor's degree"
                    elif ("associate of" in name_lower or "associate's" in name_lower or "associates" in name_lower or "degree" in name_lower) and "associate" in name_lower and not any(kw in name_lower for kw in ["certified", "certification", "aws", "azure", "google", "oracle"]):
                        degree = "Associate's degree"
                    elif any(kw in name_lower for kw in ["ged", "high school"]):
                        degree = "High School / GED"
                    else:
                        degree = "Certification / License"

                    education.append({
                        "degree": degree,
                        "field": item["name"],
                        "required": required_label
                    })
                    continue  # Skip certifications and education in skills
                evidence_type = (item.get('evidence_type') or '').lower()
                skill_min_years = item.get('min_years', min_years)
                category = normalize_skill_category(item["name"], item.get('category', 'hard').lower())
                normalized_value = " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in item["name"]).split())
                is_direct_text_match = bool(normalized_value and normalized_value in normalized_grounding_text)
                is_direct_hard_skill = category == "hard" and (evidence_type == "direct" or is_direct_text_match)
                required_label = "Required" if is_direct_hard_skill else ("Preferred" if category == "hard" else item.get('importance', 'preferred').capitalize())

                # The JD's own section headers outrank both the LLM's guess and
                # the is_direct_hard_skill heuristic above. That heuristic treats
                # ANY direct mention as Required, which conflates "the JD says
                # this word" with "the JD demands it" — on 26-22970 it marked
                # HTML, CSS and SharePoint Required even though the JD lists
                # them only under "Preferred Qualifications", while pushing
                # eLearning content (a Key Responsibility) to Preferred. Only
                # overrides when the term is actually found under a recognised
                # header; otherwise the label above stands.
                section_verdict = rubric_grounding.classify_by_jd_section(
                    item["name"], grounding_text
                )
                if section_verdict == "required":
                    required_label = "Required"
                elif section_verdict == "preferred":
                    required_label = "Preferred"
                importance = required_label.lower()

                skill_obj = {
                    "value": item["name"],
                    "source": "PAIR",
                    "matchType": "Similar",  # Always use Similar for all skills
                    "importance": importance,
                    "required": required_label,
                    "minYears": skill_min_years if skill_min_years else min_years,
                    "category": category,
                    "evidence_type": "direct" if is_direct_hard_skill else (evidence_type or ("direct" if is_direct_text_match else "inferred")),
                    # Filled in by the JD-grounded synonym pass below, once every
                    # skill is known (peer skills must not become each other's
                    # synonyms).
                    "similar_skills": [],
                    "_llm_similar_skills": item.get("similar_skills") or [],
                }

                # Separate into hard and soft skills based on category
                if category == "soft":
                    grounded_soft_skills.append(skill_obj)
                else:
                    grounded_hard_skills.append(skill_obj)

        grounded_hard_skills.sort(key=skill_priority)
        # Limit to a maximum of 8 hard skills (soft skills are not counted in this limit)
        grounded_hard_skills = grounded_hard_skills[:8]

        # JD-grounded synonym clusters. Runs here, after the final skill set is
        # known, so every other chip can be excluded as a peer rather than
        # becoming this skill's "synonym". Each surviving alternative is a term
        # the JD itself names, which is what turns a bare "Adobe Creative Cloud"
        # chip into the recruiter's ("ADOBE CREATIVE CLOUD" OR PHOTOSHOP OR
        # ILLUSTRATOR OR INDESIGN) — the JD spells those out in parentheses.
        _peer_names = [s.get("value", "") for s in (grounded_hard_skills + grounded_soft_skills)]
        for _s in grounded_hard_skills:
            _s["similar_skills"] = rubric_grounding.ground_skill_synonyms(
                _s.get("value", ""),
                _s.pop("_llm_similar_skills", []) or [],
                grounding_text,
                peers=_peer_names,
            )
        for _s in grounded_soft_skills:
            _s.pop("_llm_similar_skills", None)

                    
        # Extract Job roles using LLM - ALWAYS use "Similar" match type
        if not grounded_roles:
            for item in phase2_result.get("job_roles", []):
                if isinstance(item, dict) and 'name' in item:
                    grounded_roles.append({
                        "value": item["name"],
                        "source": "PAIR",
                        "matchType": "Similar",  # Always use Similar for job titles
                        "required": item.get('required', 'Preferred'),
                        # Transient: JD-grounded adjacent titles proposed by the
                        # LLM for the canonical role. Consumed (and popped) by the
                        # augmentation loop below before the dict reaches the DB.
                        "_llm_similar": [
                            s for s in (item.get("similar_titles") or [])
                            if isinstance(s, str) and s.strip()
                        ],
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

        # Augment each title with similar_titles, grounded in THIS job.
        # Two sources feed a hard JD-grounding gate so a generic "Business
        # Analyst" no longer pulls in "Mortgage / Robotics / Payment Business
        # Analyst" unless the JD actually names those domains:
        #   (a) context-filtered taxonomy siblings from job_role_taxonomy.json
        #       (role_taxonomy.expand_title_grounded), and
        #   (b) the LLM's domain-grounded adjacent titles, validated/clubbed
        #       against the main title via role_taxonomy.is_grounded_variant.
        # JD context = grounding text + extracted domain sectors + skill names.
        domain_words = " ".join(
            (d.get("value") if isinstance(d, dict) else str(d)) or ""
            for d in phase2_result.get("domain", [])
        )
        skill_words = " ".join(
            s.get("value", "") for s in (grounded_hard_skills + grounded_soft_skills)
        )
        similar_title_context = " ".join([grounding_text, domain_words, skill_words])

        # Multi-title jobs "club" each candidate under the single most-related
        # main title (canonical first), so the same similar title never appears
        # in two groups. No padding: stop when relevant candidates run out.
        # Seed with every main title so a similar-title chip never duplicates a
        # title the recruiter is already searching as its own row.
        # Capped at 7 — recruiters hand-write tight title lists (the 26-22970
        # agent string used exactly 7). Ten slots per title meant a 5-title job
        # emitted ~39 OR'd variants, which reads as thorough but is mostly
        # noise: precision collapses and the group stops describing the role.
        MAX_SIMILAR = 7
        claimed: set[str] = {
            (t.get("value") or "").lower()
            for t in final_titles
            if isinstance(t, dict) and (t.get("value") or "").strip()
        }
        for t in final_titles:
            main = (t.get("value") or "") if isinstance(t, dict) else ""
            llm_titles = t.pop("_llm_similar", []) if isinstance(t, dict) else []

            try:
                expansions = role_taxonomy.expand_title_grounded(
                    main, similar_title_context, max_results=MAX_SIMILAR
                )
            except Exception as exc:
                logger.warning("expand_title_grounded failed for %r: %s", main, exc)
                expansions = []
            taxonomy_titles = [
                entry.get("title") for entry in expansions
                if isinstance(entry, dict) and entry.get("title")
            ]

            validated_llm = [
                cand.strip() for cand in llm_titles
                if isinstance(cand, str) and cand.strip()
                and role_taxonomy.is_grounded_variant(main, cand.strip(), similar_title_context)
            ]

            out: list[str] = []
            seen_local = {main.lower()}
            sources = list(t.get("similar_titles") or []) + taxonomy_titles + validated_llm
            for cand_title in sources:
                key = cand_title.lower()
                if key in seen_local or key in claimed:
                    continue
                # Seniority gate. Neither the taxonomy siblings nor the LLM
                # suggestions respect level, so "Creative Designer" was pulling
                # in "Creative Department Head" and "Global Creative Director"
                # while "Graphic Designer" pulled in "Visual Design Intern".
                # Those are different jobs, not alternatives for the same one —
                # OR-ing them in surfaces candidates no recruiter would submit.
                # One rung of slack keeps the honest neighbours (Designer ↔
                # Senior Designer).
                if not rubric_grounding.seniority_compatible(main, cand_title):
                    logger.debug(
                        "similar_titles: dropping %r for %r (seniority %d vs %d)",
                        cand_title, main,
                        rubric_grounding.seniority_level(cand_title),
                        rubric_grounding.seniority_level(main),
                    )
                    continue
                seen_local.add(key)
                claimed.add(key)
                out.append(cand_title)
                if len(out) >= MAX_SIMILAR:
                    break
            t["similar_titles"] = out

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
            # Domain is deliberately allowed to come from world knowledge of the
            # CUSTOMER (Cummins → "Diesel Engines"), which is why it is not
            # JD-grounded in general. But with no customer name there is nothing
            # to have knowledge about, so an ungrounded sector is pure invention
            # — 26-22970 (no customer, healthcare-finance JD) came back
            # "Telecom", which then scores every résumé against the wrong
            # industry. The prompt already says to return [] when unsure; this
            # enforces it.
            if not (customer_name or "").strip() and not rubric_grounding.is_grounded_term(val, grounding_text):
                logger.info(
                    "domain: dropping %r — no customer name and nothing in the JD supports it", val
                )
                continue
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

        # Non-IT rescue pass — if the LLM-only first-pass came back thin
        # for a non-IT role, re-run through the family-aware grounding
        # pipeline (same LLM, family-aware prompt + blacklist). IT roles
        # are intentionally exempt: the IT path is the well-tuned default
        # and typically returns ≥ 6 hard skills, so the gate also acts as
        # a noop for IT.
        if family != "it" and len(grounded_hard_skills) < _MIN_NON_IT_RUBRIC_SKILLS:
            logger.info(
                f"🔁 Non-IT rescue pass triggered "
                f"(family={family}, hard_skills={len(grounded_hard_skills)} < {_MIN_NON_IT_RUBRIC_SKILLS})"
            )
            try:
                rescue = await extract_grounded_rubric(
                    grounding_text,
                    enhanced_job_title or job_title,
                    self.openai_client,
                    family=family,
                )
            except Exception as rescue_err:
                logger.warning(f"⚠️  Non-IT rescue pass failed: {rescue_err}")
                rescue = {}

            existing_keys = {s["value"].strip().lower() for s in grounded_hard_skills if s.get("value")}
            for s in rescue.get("hard_skills") or []:
                key = (s.get("value") or "").strip().lower()
                if not key or key in existing_keys:
                    continue
                grounded_hard_skills.append({
                    "value": s["value"],
                    "source": "PAIR-rescue",
                    "matchType": "Similar",
                    "importance": "preferred",
                    "required": "Preferred",
                    "minYears": s.get("minYears", 0) or min_years,
                    "category": "hard",
                    "evidence_type": "inferred",
                })
                existing_keys.add(key)
                if len(grounded_hard_skills) >= 8:
                    break

            existing_soft = {s["value"].strip().lower() for s in grounded_soft_skills if s.get("value")}
            for s in rescue.get("soft_skills") or []:
                key = (s.get("value") or "").strip().lower()
                if not key or key in existing_soft:
                    continue
                grounded_soft_skills.append({
                    "value": s["value"],
                    "source": "PAIR-rescue",
                    "matchType": "Similar",
                    "importance": "preferred",
                    "required": "Preferred",
                    "minYears": 0,
                    "category": "soft",
                    "evidence_type": "inferred",
                })
                existing_soft.add(key)

            logger.info(
                f"   ➕ Rescue added {len(grounded_hard_skills)} total hard / "
                f"{len(grounded_soft_skills)} total soft skills"
            )

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

        # Candidate DOMAIN EXPERIENCE — the industry the JD asks the candidate to
        # have worked in. Distinct from the `domain` field below, which is the
        # CUSTOMER's sector and comes from world knowledge of the account (AT&T ->
        # Telecom); this comes from the JD's own words ("...and healthcare or
        # healthcare finance environments") and belongs in the sourcing query,
        # where recruiters reliably put it as a cluster. We had no concept for it,
        # so it never reached the search at all.
        #
        # Appended after EVERY cap and the non-IT rescue pass — there are two
        # separate 8-skill truncations in this function, and an earlier insertion
        # point was silently re-truncated away. Kept off the cap on purpose: it is a different axis from
        # tooling, so it should not have to win a slot against Photoshop. Emitted
        # as a hard-skill chip so it flows through the existing similar_skills /
        # boolean machinery unchanged, and it inherits Required/Preferred from the
        # JD section that stated it (Preferred on 26-22970 — the recruiter chose
        # to harden it, which the recruiter can still do in the UI).
        try:
            _existing_skill_keys = {
                str(s.get("value", "")).strip().lower()
                for s in (grounded_hard_skills + grounded_soft_skills)
            }
            for _dom in rubric_grounding.extract_domain_experience(grounding_text):
                if str(_dom["value"]).strip().lower() in _existing_skill_keys:
                    continue
                grounded_hard_skills.append({
                    "value": _dom["value"],
                    "source": "PAIR",
                    "matchType": "Similar",
                    "importance": _dom["importance"],
                    "required": _dom["importance"].capitalize(),
                    "minYears": 0,
                    "category": "hard",
                    "evidence_type": "direct",
                    "similar_skills": _dom["similar_skills"],
                })
                logger.info(
                    "domain experience: added %r (%s) with cluster %s",
                    _dom["value"], _dom["importance"], _dom["similar_skills"],
                )
        except Exception as exc:
            logger.warning("domain-experience extraction skipped: %s", exc)

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
