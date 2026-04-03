"""
taxonomy_service.py
-------------------
Hybrid Discovery & Grounding Service (Async Ready) - v11 Optimized.

Grounding Workflow:
1. Discovery (Phase 1): LLM identifies all potential skills/roles in the JD (Recall).
   - Optimized to ignore job benefits/perks and translate phrases into technical labels.
2. Grounding (Phase 2): Each discovered phrase is anchored to the master tables (Precision).
   - Exact Match first.
   - Fuzzy Match (token_set_ratio >= 90) second.
   - DOUBLE-LOCK: Rejects matches that ground to blacklisted generic terms.
"""

import os
import re
import json
import logging
import asyncio
from typing import List, Dict, Tuple, Optional

import psycopg2
import psycopg2.extras
from rapidfuzz import fuzz, process as rfprocess

logger = logging.getLogger(__name__)

# Expanded list of terms that are noise for a technical rubric
GENERIC_SKILLS_BLACKLIST = {
    "DIGITAL", "SOFTWARE", "MANAGEMENT", "MANAGED", "OPERATION", "OPERATIONS",
    "PROCEDURES", "MONITORING", "HEALTHCARE", "CLINICAL", "PROCEDURE",
    "STANDARDS", "BASIC", "LEVEL", "SERVICE", "SERVICES",
    "SUPPORT", "TECHNICAL", "TECHNOLOGY", "SOLUTIONS", "SYSTEMS", "ANALYST",
    "CONSULTING", "DEVELOPMENT", "ENGINEERING", "QUALITY", "ASSURANCE",
    "GOVERNMENT", "ENTERPRISE", "BUSINESS", "PROFESSIONAL", "INDUSTRY",
    "RADIOLOGY", "BENEFITS", "BENEFIT", "DENTAL", "VISION", "INSURANCE",
    "MEDICAL", "401K", "PTO", "SALARY", "COMPENSATION", "VACATION", "XRAYS",
    "X-RAYS", "SCHEDULE", "SCHEDULING", "PATIENT", "PATIENTS", "JD", "JOB",
    "DESCRIPTION", "RESPONSIBILITIES", "REQUIREMENTS", "QUALIFICATIONS",
    "REQUIRED", "PREFERRED", "MUST HAVE", "NICE TO HAVE", "HOSPITAL", 
    "CLINICAL", "RECORDS", "FACILITY", "STAFF", "TEAM", "SHIFTS", "AVAILABILITY",
    "DIAGNOSTIC", "EQUIPMENT", "OPERATION", "IMAGES", "RECORD"
}

# ── Master Taxonomy Cache ───────────────────────────────────────────────────────
_SKILLS_CACHE: Optional[List[str]] = None
_ROLES_CACHE: Optional[List[str]] = None
_SKILLS_LOOKUP_UPPER: Optional[Dict[str, str]] = None
_ROLES_LOOKUP_UPPER: Optional[Dict[str, str]] = None

def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=int(os.environ.get("POSTGRES_PORT", 5433)),
        dbname=os.environ.get("POSTGRES_DB", "ai_recruiter"),
        user=os.environ.get("POSTGRES_USER", "postgres"),
        password=os.environ.get("POSTGRES_PASSWORD", "root"),
    )

def _load_master_caches():
    """Initializes in-memory master taxonomies."""
    global _SKILLS_CACHE, _ROLES_CACHE, _SKILLS_LOOKUP_UPPER, _ROLES_LOOKUP_UPPER
    if _SKILLS_CACHE is not None and _ROLES_CACHE is not None:
        return

    conn = _get_conn()
    try:
        cur = conn.cursor()
        if _SKILLS_CACHE is None:
            logger.info("🧠 Loading 33k master skills into memory...")
            cur.execute("SELECT skill_mapped FROM public.skills_master")
            _SKILLS_CACHE = [r[0] for r in cur.fetchall() if r[0]]
            _SKILLS_LOOKUP_UPPER = {s.upper(): s for s in _SKILLS_CACHE}
            logger.info(f"✅ Cached {len(_SKILLS_CACHE):,} master skills.")

        if _ROLES_CACHE is None:
            logger.info("🧠 Loading 17k master roles into memory...")
            cur.execute("SELECT role_k17000 FROM public.roles_master")
            _ROLES_CACHE = [r[0] for r in cur.fetchall() if r[0]]
            _ROLES_LOOKUP_UPPER = {r.upper(): r for r in _ROLES_CACHE}
            logger.info(f"✅ Cached {len(_ROLES_CACHE):,} master roles.")
        cur.close()
    except Exception as e:
        logger.error(f"❌ Failed to cache master taxonomies: {e}")
        _SKILLS_CACHE, _ROLES_CACHE = [], []
        _SKILLS_LOOKUP_UPPER, _ROLES_LOOKUP_UPPER = {}, {}
    finally:
        conn.close()

# ── Grounding Logic ──────────────────────────────────────────────────────────

def _ground_phrase(phrase: str, is_role: bool = False) -> Tuple[Optional[str], int, str]:
    """Anchors a phrase to the master taxonomy using Exact ➔ Fuzzy (90%)."""
    if not phrase or len(phrase) < 2: return None, 0, "none"
    
    up = phrase.upper().strip()
    # PRE-FILTER: Only block single-word generic noise.
    # Multi-word technical phrases (e.g. "Diagnostic Imaging") must proceed to fuzzy match.
    if len(phrase.split()) == 1 and up in GENERIC_SKILLS_BLACKLIST:
        return None, 0, "blacklisted"
    
    _load_master_caches()
    lookup = _ROLES_LOOKUP_UPPER if is_role else _SKILLS_LOOKUP_UPPER
    choices = _ROLES_CACHE if is_role else _SKILLS_CACHE

    # 1. Exact Match (O(1))
    if up in lookup:
        return lookup[up], 100, "exact"

    # 2. Fuzzy Match (token_set_ratio >= 90)
    result = rfprocess.extractOne(phrase, choices, scorer=fuzz.token_set_ratio, score_cutoff=90)
    if result:
        canonical_name = result[0]
        # DOUBLE-LOCK post-grounding check
        if canonical_name.upper() in GENERIC_SKILLS_BLACKLIST:
            return None, 0, "blacklisted_result"
        return canonical_name, int(result[1]), "fuzzy"

    return None, 0, "no_match"

# ── Phase 1: LLM Discovery Prompts ──────────────────────────────────────────

DISCOVERY_PROMPT = """
Target Role: {job_title}
Job Description Text:
{job_text}

TASK: Classify ALL technical and professional requirements from this job description into 4 categories.

CRITICAL CLASSIFICATION RULES:

1. hard_skills: Specific technical tools, platforms, specialized methodologies, and clinical/domain procedures.
   - PRESERVE CONTEXT: Do not over-compress. Extract full, descriptive multi-word phrases (e.g., "Infection Control Standards", "Diagnostic X-Ray Procedures", "Test Automation").
   - IMPLICIT SKILLS: Look closely at responsibilities and action verbs. If a bullet says "Prepare and position patients accurately", extract "Patient Positioning". If it says "Writing automated tests", extract "Test Automation".
   - THE SINGLE-WORD RULE: Single-word skills are ONLY allowed if they are specific Technologies, Proper Nouns, or Acronyms (e.g., "Python", "Postman", "SQL", "ARRT").
   - BANNED: Never extract general English words or generic actions as standalone skills (e.g., "Testing", "Manual", "Auto", "Review", "Analysis", "Procedures", "Safety", "Quality"). Always combine them into their full methodology (e.g., "Manual Testing", "Quality Control").
   - NOT certifications, NOT domain knowledge, NOT soft skills.

2. soft_skills: Interpersonal or workplace skills.
   - e.g. "Communication Skills", "Problem Solving", "Teamwork", "Attention to Detail"

3. certifications: Licenses, certifications, degrees required.
   - e.g. "ARRT Certification", "BLS Certification", "Bachelor's Degree in IT"
   - Do NOT include these in hard_skills.

4. domains: Industry or knowledge domain areas (NOT specific tools).
   - e.g. "Commercial Auto Insurance", "Healthcare", "Financial Services"
   - Do NOT include these in hard_skills.

5. discovered_roles: Professional titles or roles mentioned as required or alternative backgrounds.
   - e.g. "QA Engineer", "Radiologic Technologist", "Business Analyst"

ALWAYS IGNORE: job benefits, insurance perks (medical/dental/vision), 401k, PTO, pay rates, section headers.

Return ONLY JSON:
{{
  "hard_skills": ["...", "..."],
  "soft_skills": ["...", "..."],
  "certifications": ["...", "..."],
  "domains": ["...", "..."],
  "discovered_roles": ["...", "..."]
}}
"""

VALIDATION_PROMPT = """
You are an expert technical recruiter mapping extracted job requirements to an official taxonomy.
You are provided with the Job Description text and a JSON object mapping extracted skills/roles to potential taxonomy options.

Your task is to select the SINGLE MOST ACCURATE taxonomy matched term for each extracted item, based strictly on the context of the Job Description.

CRITICAL RULES:
1. Contextual Meaning vs Spelling: Read the context. Match based on professional function, not just string similarity. 
   - e.g., "X-Ray Technician" does NOT match "TV Technician".
2. Strictness: If NONE of the taxonomy options accurately represent the extracted skill in the context of this job, you MUST return null. Do not select a loose or inaccurate match.
3. Domain Consistency: Heavily penalize and REJECT taxonomy options that belong to a completely unrelated industry. For example, if the role is Healthcare, options like "Welding Procedures", "Stored Procedures", or "Civil Procedures" are mathematically similar but contextually INCORRECT. Reject them.
4. Specificity Filter: If the originally extracted term is overly generic (e.g. just "Procedures" or "Tests"), and none of the dictionary options exactly capture the specific context of the job description, return null. Do not force a match on generic words.

Job Description Context:
------------------------
{job_text}
------------------------

Return ONLY a JSON mapping each exact extracted term to its single selected taxonomy option or null:
{{
  "extracted_term1": "Option B",
  "extracted_term2": null
}}
"""

async def _call_discovery_llm(prompt: str, client) -> Dict:
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
            timeout=20
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error(f"❌ Discovery LLM failed: {e}")
        return {"hard_skills": [], "soft_skills": [], "certifications": [], "domains": [], "discovered_roles": []}

# ── Main Integrated Grounding ───────────────────────────────────────────────

async def extract_grounded_rubric(job_text: str, job_title: str, client, max_skills: int = 15, max_titles: int = 5) -> Dict:
    """Consolidated Grounding Flow (v12) — 4-category aware."""

    # 1. Discovery
    logger.info("─" * 60)
    logger.info("🧠 PHASE 1: LLM Discovery")
    discovery = await _call_discovery_llm(DISCOVERY_PROMPT.format(
        job_title=job_title,
        job_text=job_text[:5000]
    ), client)

    raw_hard_skills = discovery.get("hard_skills", []) or discovery.get("discovered_skills", [])
    raw_soft_skills = discovery.get("soft_skills", [])
    raw_certs       = discovery.get("certifications", [])
    raw_domains     = discovery.get("domains", [])
    raw_roles       = discovery.get("discovered_roles", [])

    logger.info(f"   📋 Hard Skills ({len(raw_hard_skills)}):")
    for s in raw_hard_skills:  logger.info(f"      - {s}")
    logger.info(f"   📋 Soft Skills ({len(raw_soft_skills)}):")
    for s in raw_soft_skills:  logger.info(f"      - {s}")
    logger.info(f"   📋 Certifications ({len(raw_certs)}):")
    for s in raw_certs:        logger.info(f"      - {s}")
    logger.info(f"   📋 Domains ({len(raw_domains)}):")
    for s in raw_domains:      logger.info(f"      - {s}")
    logger.info(f"   📋 Roles ({len(raw_roles)}):")
    for r in raw_roles:        logger.info(f"      - {r}")

    # 2. Database Fast Retrieval (Context Generation)
    logger.info("─" * 60)
    logger.info("⚡ PHASE 2: Fast Taxonomy Option Retrieval (RAG)")

    def get_taxonomy_options(phrase: str, is_role: bool = False) -> List[str]:
        if not phrase or len(phrase) < 2: return []
        up = phrase.upper().strip()
        if len(phrase.split()) == 1 and up in GENERIC_SKILLS_BLACKLIST:
            return []
            
        _load_master_caches()
        lookup  = _ROLES_LOOKUP_UPPER if is_role else _SKILLS_LOOKUP_UPPER
        choices = _ROLES_CACHE        if is_role else _SKILLS_CACHE

        results = []
        if up in lookup:
            results.append(lookup[up])
            
        # Get top 40 options to act as our context window (increased from 15 to prevent missing valid distant matches)
        fuzzy_results = rfprocess.extract(phrase, choices, scorer=fuzz.token_set_ratio, limit=40)
        for r in fuzzy_results:
            can = r[0]
            if can not in results and can.upper() not in GENERIC_SKILLS_BLACKLIST:
                results.append(can)
                
        return results

    mapping_request = {}
    for skill in raw_hard_skills:
        options = get_taxonomy_options(skill, is_role=False)
        if options: 
            mapping_request[skill] = options
            logger.info(f"   🔍 Options for [HARD SKILL] '{skill}': {options}")

    for soft_skill in raw_soft_skills:
        options = get_taxonomy_options(soft_skill, is_role=False)
        if options: 
            mapping_request[soft_skill] = options
            logger.info(f"   🔍 Options for [SOFT SKILL] '{soft_skill}': {options}")

    for role in raw_roles:
        options = get_taxonomy_options(role, is_role=True)
        if options: 
            mapping_request[role] = options
            logger.info(f"   🔍 Options for [TITLE] '{role}': {options}")

    # 3. LLM Validation (Manager's Matcher)
    logger.info("─" * 60)
    logger.info("🤖 PHASE 3: LLM Taxonomy Validation")
    
    mapping_response = {}
    if mapping_request:
        formatted_validation_prompt = VALIDATION_PROMPT.format(job_text=job_text[:5000])
        val_prompt = formatted_validation_prompt + f"\n\nExtracted Items and Taxonomy Options:\n{json.dumps(mapping_request, indent=2)}"
        try:
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a precise JSON mapping machine. Only output accurate valid JSON."},
                    {"role": "user", "content": val_prompt}
                ],
                temperature=0,
                response_format={"type": "json_object"},
                timeout=30
            )
            mapping_response = json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.error(f"❌ Validation LLM failed: {e}")

    final_skills = []
    final_titles = []
    seen_skills  = set()
    seen_roles   = set()

    for phrase in raw_hard_skills:
        mapped = mapping_response.get(phrase)
        if mapped:
            can_up = mapped.upper()
            if can_up not in seen_skills and can_up in _SKILLS_LOOKUP_UPPER:
                logger.info(f"   ✅ [SKILL]    {phrase:<35} ➔  {mapped} (LLM Validated)")
                final_skills.append({
                    "value": mapped, "minYears": 0, "recent": True,
                    "matchType": "Similar", "required": "Required", "category": "hard"
                })
                seen_skills.add(can_up)
            else:
                logger.info(f"   ❌ [DROPPED]  {phrase} (LLM returned invalid taxonomy term)")
        else:
            logger.info(f"   ❌ [DROPPED]  {phrase} (LLM rejected all taxonomy options)")

    for phrase in raw_roles:
        mapped = mapping_response.get(phrase)
        if mapped:
             can_up = mapped.upper()
             if can_up not in seen_roles and can_up in _ROLES_LOOKUP_UPPER:
                 logger.info(f"   👔 [TITLE]    {phrase:<35} ➔  {mapped} (LLM Validated)")
                 final_titles.append({
                     "value": mapped, "minYears": 0, "recent": False,
                     "matchType": "Similar", "required": "Preferred"
                 })
                 seen_roles.add(can_up)
             else:
                 logger.info(f"   ❌ [DROPPED]  {phrase} (LLM returned invalid taxonomy term)")
        else:
             logger.info(f"   ❌ [DROPPED]  {phrase} (LLM rejected all taxonomy options)")

    final_soft_skills = []
    for phrase in raw_soft_skills:
        mapped = mapping_response.get(phrase)
        if mapped:
            can_up = mapped.upper()
            # Note: We reuse seen_skills so we don't accidentally add the same skill twice 
            # if it was found as both a hard and soft skill somehow.
            if can_up not in seen_skills and can_up in _SKILLS_LOOKUP_UPPER:
                logger.info(f"   ✅ [SOFT SKILL] {phrase:<33} ➔  {mapped} (LLM Validated)")
                final_soft_skills.append({
                    "value": mapped, "minYears": 0, "recent": False,
                    "matchType": "Similar", "required": "Preferred", "category": "soft"
                })
                seen_skills.add(can_up)
            else:
                logger.info(f"   ❌ [DROPPED]  {phrase} (LLM returned invalid taxonomy term)")
        else:
            logger.info(f"   ❌ [DROPPED]  {phrase} (LLM rejected all taxonomy options)")

    logger.info(f"   💬 Validated Soft Skills ({len(final_soft_skills)}):")
    for s in final_soft_skills:
        logger.info(f"      - {s['value']}")

    logger.info("─" * 60)

    return {
        "hard_skills":        final_skills[:max_skills],
        "soft_skills":        final_soft_skills,
        "extra_titles":       final_titles[:max_titles],
        "raw_certifications": raw_certs,
        "raw_domains":        raw_domains,
    }
