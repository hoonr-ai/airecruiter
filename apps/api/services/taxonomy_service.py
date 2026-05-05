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

from core.config import DATABASE_URL

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

# Hierarchy caches: row-by-norm gives "term -> {level: cluster_value}".
# Reverse "by-cluster" gives "level -> cluster_value -> [terms in cluster]".
# Used by find_similar_* and shared_cluster_level for sibling lookups.
_ROLE_ROW_BY_NORM: Optional[Dict[str, Dict[str, str]]] = None
_SKILL_ROW_BY_NORM: Optional[Dict[str, Dict[str, str]]] = None
_ROLE_BY_CLUSTER: Optional[Dict[str, Dict[str, List[str]]]] = None
_SKILL_BY_CLUSTER: Optional[Dict[str, Dict[str, List[str]]]] = None

# Hierarchy levels ordered FINEST → COARSEST.
# Index 0 is the leaf (the term itself); subsequent entries are progressively
# more generic clusters (smaller cluster count = coarser).
ROLE_LEVELS: List[str] = [
    "role_k17000", "role_k10000", "role_k5000", "role_k1500",
    "role_k1000", "role_k500", "role_k150", "role_k50", "role_k10",
]
SKILL_LEVELS: List[str] = [
    "skill_mapped", "skill_k15000", "skill_k5000", "skill_k1500",
    "skill_k500", "skill_k150", "skill_k50", "skill_k15",
]

def _get_conn():
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL not set in environment.")
        raise RuntimeError("DATABASE_URL is missing")
    
    # psycopg2 can connect directly via the DATABASE_URL string (DSN)
    return psycopg2.connect(DATABASE_URL, connect_timeout=5)

def _load_master_caches():
    """Initializes in-memory master taxonomies."""
    global _SKILLS_CACHE, _ROLES_CACHE, _SKILLS_LOOKUP_UPPER, _ROLES_LOOKUP_UPPER
    global _ROLE_ROW_BY_NORM, _SKILL_ROW_BY_NORM, _ROLE_BY_CLUSTER, _SKILL_BY_CLUSTER
    if _SKILLS_CACHE is not None and _ROLES_CACHE is not None:
        return

    conn = _get_conn()
    try:
        cur = conn.cursor()
        if _SKILLS_CACHE is None:
            logger.info("🧠 Loading 33k master skills with hierarchy into memory...")
            cur.execute(f"SELECT {', '.join(SKILL_LEVELS)} FROM public.skills_master")
            rows = cur.fetchall()
            _SKILLS_CACHE = []
            _SKILL_ROW_BY_NORM = {}
            _SKILL_BY_CLUSTER = {lvl: {} for lvl in SKILL_LEVELS}
            for row in rows:
                leaf = row[0]
                if not leaf:
                    continue
                _SKILLS_CACHE.append(leaf)
                hier = {SKILL_LEVELS[i]: row[i] for i in range(len(SKILL_LEVELS)) if row[i]}
                _SKILL_ROW_BY_NORM[leaf.upper()] = hier
                for lvl, val in hier.items():
                    _SKILL_BY_CLUSTER[lvl].setdefault(val, []).append(leaf)
            _SKILLS_LOOKUP_UPPER = {s.upper(): s for s in _SKILLS_CACHE}
            logger.info(f"✅ Cached {len(_SKILLS_CACHE):,} master skills with full hierarchy.")

        if _ROLES_CACHE is None:
            logger.info("🧠 Loading 17k master roles with hierarchy into memory...")
            cur.execute(f"SELECT {', '.join(ROLE_LEVELS)} FROM public.roles_master")
            rows = cur.fetchall()
            _ROLES_CACHE = []
            _ROLE_ROW_BY_NORM = {}
            _ROLE_BY_CLUSTER = {lvl: {} for lvl in ROLE_LEVELS}
            for row in rows:
                leaf = row[0]
                if not leaf:
                    continue
                _ROLES_CACHE.append(leaf)
                hier = {ROLE_LEVELS[i]: row[i] for i in range(len(ROLE_LEVELS)) if row[i]}
                _ROLE_ROW_BY_NORM[leaf.upper()] = hier
                for lvl, val in hier.items():
                    _ROLE_BY_CLUSTER[lvl].setdefault(val, []).append(leaf)
            _ROLES_LOOKUP_UPPER = {r.upper(): r for r in _ROLES_CACHE}
            logger.info(f"✅ Cached {len(_ROLES_CACHE):,} master roles with full hierarchy.")
        cur.close()
    except Exception as e:
        logger.error(f"❌ Failed to cache master taxonomies: {e}")
        _SKILLS_CACHE, _ROLES_CACHE = [], []
        _SKILLS_LOOKUP_UPPER, _ROLES_LOOKUP_UPPER = {}, {}
        _ROLE_ROW_BY_NORM, _SKILL_ROW_BY_NORM = {}, {}
        _ROLE_BY_CLUSTER, _SKILL_BY_CLUSTER = (
            {lvl: {} for lvl in ROLE_LEVELS},
            {lvl: {} for lvl in SKILL_LEVELS},
        )
    finally:
        conn.close()


def _resolve_term(phrase: str, kind: str) -> Optional[str]:
    """Anchor `phrase` against the master cache. Returns the canonical
    leaf term (case-preserved from master) or None. Exact upper-match,
    then fuzzy via token_set_ratio ≥ 85."""
    if not phrase or len(phrase) < 2:
        return None
    _load_master_caches()
    up = phrase.upper().strip()
    if kind == "role":
        lookup = _ROLES_LOOKUP_UPPER or {}
        choices = _ROLES_CACHE or []
    else:
        lookup = _SKILLS_LOOKUP_UPPER or {}
        choices = _SKILLS_CACHE or []
    if up in lookup:
        return lookup[up]
    if not choices:
        return None
    res = rfprocess.extractOne(phrase, choices, scorer=fuzz.token_set_ratio, score_cutoff=85)
    return res[0] if res else None


def find_similar_titles(term: str, level: str = "role_k1500", limit: int = 8) -> List[str]:
    """Return up to `limit` sibling roles in the same `level` cluster as
    `term`. Empty list when `term` doesn't anchor or has no siblings."""
    canonical = _resolve_term(term, "role")
    if not canonical or _ROLE_ROW_BY_NORM is None or _ROLE_BY_CLUSTER is None:
        return []
    hier = _ROLE_ROW_BY_NORM.get(canonical.upper(), {})
    cluster_val = hier.get(level)
    if not cluster_val:
        return []
    siblings = _ROLE_BY_CLUSTER.get(level, {}).get(cluster_val, [])
    out: List[str] = []
    seen = {canonical.upper()}
    for s in siblings:
        if s.upper() in seen:
            continue
        seen.add(s.upper())
        out.append(s)
        if len(out) >= limit:
            break
    return out


def find_similar_skills(term: str, level: str = "skill_k1500", limit: int = 8) -> List[str]:
    """Return up to `limit` sibling skills in the same `level` cluster.
    Empty list when `term` doesn't anchor or has no siblings."""
    canonical = _resolve_term(term, "skill")
    if not canonical or _SKILL_ROW_BY_NORM is None or _SKILL_BY_CLUSTER is None:
        return []
    hier = _SKILL_ROW_BY_NORM.get(canonical.upper(), {})
    cluster_val = hier.get(level)
    if not cluster_val:
        return []
    siblings = _SKILL_BY_CLUSTER.get(level, {}).get(cluster_val, [])
    out: List[str] = []
    seen = {canonical.upper()}
    for s in siblings:
        if s.upper() in seen:
            continue
        seen.add(s.upper())
        out.append(s)
        if len(out) >= limit:
            break
    return out


def shared_cluster_level(term_a: str, term_b: str, kind: str) -> Optional[str]:
    """Return the FINEST shared hierarchy level between two terms (the
    level with the smallest cluster size where their cluster values
    coincide), or None if they don't share any cluster or either fails
    to anchor. `kind` ∈ {'role', 'skill'}."""
    canon_a = _resolve_term(term_a, kind)
    canon_b = _resolve_term(term_b, kind)
    if not canon_a or not canon_b or canon_a.upper() == canon_b.upper():
        return None
    if kind == "role":
        rows = _ROLE_ROW_BY_NORM or {}
        levels = ROLE_LEVELS
    else:
        rows = _SKILL_ROW_BY_NORM or {}
        levels = SKILL_LEVELS
    hier_a = rows.get(canon_a.upper(), {})
    hier_b = rows.get(canon_b.upper(), {})
    # Skip the leaf level (index 0) — equal there means same term, already
    # short-circuited above. Walk from finest non-leaf toward coarsest.
    for lvl in levels[1:]:
        va = hier_a.get(lvl)
        vb = hier_b.get(lvl)
        if va and vb and va == vb:
            return lvl
    return None

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
   - e.g. "Communication Skills", "Problem Solving", "Teamwork", "Attention to Detail", "Stakeholder Engagement"

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
