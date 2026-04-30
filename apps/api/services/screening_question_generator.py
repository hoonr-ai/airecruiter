"""
screening_question_generator.py
--------------------------------
Step-4 screening-question generator. Replaces the frontend's
"Can you describe your experience with {skill}?" boilerplate with
role + seniority-aware questions that meaningfully differentiate a
candidate who actually did the work from one who only read about it.

Called from `POST /jobs/{job_id}/screening-questions/generate`.

The generator receives the structured rubric (skills, titles, domain,
customer, years), detects seniority from the title, and asks the LLM
to write depth-probing questions. Questions always include:
  - default/intro question (always first, non-role-specific)
  - work-arrangement question (onsite / hybrid; hard-filter if not remote)
  - default-experience overview (total years)
    - N role-specific questions, scaled by screening_level:
            Light=3, Medium=5, Intensive=7
The frontend still owns the "merge user-edits" flow — we return a fresh
set and the UI decides how to reconcile.
"""

from __future__ import annotations

from typing import List, Dict, Any, Optional, Literal
import json
import logging
import re

import openai

logger = logging.getLogger(__name__)

RoleFamily = Literal["it", "non_it", "hybrid"]

_IT_TITLE_KEYWORDS = {
    "software", "engineer", "developer", "devops", "sre", "site reliability",
    "data engineer", "data scientist", "machine learning", "ai", "cloud",
    "architect", "programmer", "full stack", "backend", "frontend", "qa automation",
    "platform", "security engineer", "database", "etl", "analytics engineer",
}

_IT_SKILL_KEYWORDS = {
    "python", "java", "c#", "node", "react", "angular", "aws", "azure", "gcp",
    "kubernetes", "docker", "terraform", "ci/cd", "jenkins", "github actions",
    "sql", "databricks", "snowflake", "airflow", "spark", "microservices",
    "api", "rest", "graphql", "linux", "git", "typescript", "javascript",
}

_NON_IT_TITLE_KEYWORDS = {
    "recruiter", "talent", "account manager", "customer service", "operations",
    "project coordinator", "business operations", "analyst", "financial analyst",
    "marketing", "sales", "hr", "human resources", "legal", "compliance",
    "clinical", "nurse", "technician", "assistant", "specialist", "administrator",
    "supply chain", "procurement", "claims", "benefits", "payroll",
}

_NON_IT_SKILL_KEYWORDS = {
    "stakeholder management", "client communication", "documentation", "scheduling",
    "compliance", "auditing", "reporting", "excel", "power bi", "tableau",
    "customer support", "negotiation", "recruiting", "sourcing", "interviewing",
    "case management", "crm", "salesforce", "process improvement", "quality assurance",
    "training", "presentation", "budgeting", "forecasting", "operations",
}

_NON_IT_BANNED_TERMS = (
    "ci/cd", "deployment", "rollback", "production system", "architecture",
    "microservices", "pipeline checks", "release pipeline",
)

# Recognized seniority tokens in job titles. Order matters: longer/more
# specific phrases first so "vp engineering" beats "vp" and "staff
# engineer" beats "engineer".
_SENIORITY_TOKENS = [
    ("principal", "principal"),
    ("distinguished", "distinguished"),
    ("staff", "staff"),
    ("architect", "architect"),
    ("lead", "lead"),
    ("senior", "senior"),
    ("sr.", "senior"),
    ("sr ", "senior"),
    ("mid-level", "mid"),
    ("mid level", "mid"),
    ("junior", "junior"),
    ("jr.", "junior"),
    ("jr ", "junior"),
    ("entry", "junior"),
    ("intern", "junior"),
]


def detect_seniority(job_title: str) -> str:
    """Return one of: junior | mid | senior | staff | principal."""
    t = (job_title or "").lower()
    for token, level in _SENIORITY_TOKENS:
        if token in t:
            # Collapse to the five buckets we feed the LLM.
            return {
                "junior": "junior",
                "mid": "mid",
                "senior": "senior",
                "lead": "senior",
                "architect": "senior",
                "staff": "staff",
                "principal": "principal",
                "distinguished": "principal",
            }.get(level, "senior")
    # Default to mid — safer than assuming senior.
    return "mid"


def _question_count_for_level(level: str) -> int:
    """Exact number of role-specific questions for a screening level."""
    normalized = (level or "").strip().lower()
    if normalized in ("light", "low", "basic", "quick"):
        return 3
    if normalized in ("intensive", "deep", "extensive", "high"):
        return 7
    # Default: Medium
    return 5


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+/#\-. ]+", " ", (text or "").lower())).strip()


def _contains_any(text: str, terms: set[str]) -> int:
    if not text:
        return 0
    return sum(1 for term in terms if term in text)


def detect_role_family(job_title: str, rubric: Dict[str, Any], customer_name: str = "") -> RoleFamily:
    """
    Lightweight role-family classifier used to shape prompts/fallbacks.
    Prioritizes strict non-IT wording unless title/skills clearly indicate IT.
    """
    title_text = _normalize_text(f"{job_title} {customer_name}")

    all_skills: List[Dict[str, Any]] = []
    for key in ("skills", "hard_skills", "soft_skills"):
        bucket = rubric.get(key) or []
        if isinstance(bucket, list):
            all_skills.extend([s for s in bucket if isinstance(s, dict)])
    skill_text = _normalize_text(" ".join(str(s.get("value") or s.get("name") or "") for s in all_skills))

    domain_items = rubric.get("domain") or []
    domain_text = _normalize_text(" ".join(
        str(d.get("value") if isinstance(d, dict) else d) for d in domain_items
    ))

    score_it = 0
    score_non_it = 0

    score_it += _contains_any(title_text, _IT_TITLE_KEYWORDS) * 3
    score_non_it += _contains_any(title_text, _NON_IT_TITLE_KEYWORDS) * 3

    score_it += _contains_any(skill_text, _IT_SKILL_KEYWORDS) * 2
    score_non_it += _contains_any(skill_text, _NON_IT_SKILL_KEYWORDS) * 2

    score_it += _contains_any(domain_text, _IT_TITLE_KEYWORDS)
    score_non_it += _contains_any(domain_text, _NON_IT_TITLE_KEYWORDS)

    if score_it >= score_non_it + 3:
        return "it"
    if score_non_it >= score_it + 3:
        return "non_it"
    if score_it > 0 and score_non_it > 0:
        return "hybrid"

    # Strict-by-default to prevent technical leakage into non-IT roles.
    return "non_it"


def _fmt_skills(skills: List[Dict[str, Any]]) -> str:
    if not skills:
        return "  (none)"
    lines = []
    for s in skills:
        name = s.get("value") or s.get("name") or ""
        years = s.get("minYears") or s.get("min_years") or 0
        lines.append(f"  - {name} (min {years} yrs)" if years else f"  - {name}")
    return "\n".join(lines)


def _build_prompt(
    *,
    job_title: str,
    seniority: str,
    customer_name: str,
    industry: str,
    required_skills: List[Dict[str, Any]],
    preferred_skills: List[Dict[str, Any]],
    total_years: int,
    target_count: int,
    role_family: RoleFamily,
) -> str:
    common_context = f"""ROLE CONTEXT
  Job title: {job_title}
  Seniority level: {seniority}
  Customer: {customer_name or "N/A"}
  Industry: {industry or "N/A"}
  Target total experience: {total_years}+ years

RUBRIC — Must-have skills:
{_fmt_skills(required_skills)}

RUBRIC — Nice-to-have skills:
{_fmt_skills(preferred_skills)}
"""

    if role_family == "non_it":
        return f"""You are an expert recruiter writing screening questions for a non-IT role.

{common_context}

TASK
Produce exactly {target_count} role-specific screening questions that differentiate candidates
who have actually delivered business outcomes from those giving generic answers.

STRICT RULES — FOLLOW EVERY ONE:
1. Use non-IT/business language. Focus on process execution, stakeholder communication,
     ownership, risk/compliance, prioritization, and measurable outcomes.
2. Do NOT use software-delivery wording (CI/CD, deployment, rollback, architecture,
     production systems, release pipelines), unless the role is explicitly IT (it is not).
3. Avoid generic phrasing like "describe your experience with X".
4. Each question must have a concrete `pass_criteria` signal a recruiter can verify.
5. Questions must be answerable in under 90 seconds.
6. Do not ask years-of-experience questions and do not repeat/paraphrase questions.
7. Return JSON only.

OUTPUT FORMAT:
{{
    "questions": [
        {{
            "question_text": "string",
            "pass_criteria": "string",
            "category": "process" | "scenario" | "behavioral" | "stakeholder",
            "related_skill": "string"
        }}
    ]
}}
"""

    if role_family == "hybrid":
        return f"""You are an expert recruiter writing screening questions for a hybrid business+technical role.

{common_context}

TASK
Produce exactly {target_count} role-specific screening questions that probe practical depth.

STRICT RULES — FOLLOW EVERY ONE:
1. Blend business execution and technical depth based on the listed rubric skills.
2. Keep technical wording only where the skill explicitly demands it; do not force software
     delivery terms into every question.
3. Avoid generic wording like "describe your experience with X".
4. Each question must include concrete `pass_criteria`.
5. Questions must be answerable in under 90 seconds.
6. Do not ask years-of-experience questions and do not repeat/paraphrase questions.
7. Return JSON only.

OUTPUT FORMAT:
{{
    "questions": [
        {{
            "question_text": "string",
            "pass_criteria": "string",
            "category": "technical-depth" | "scenario" | "behavioral" | "stakeholder",
            "related_skill": "string"
        }}
    ]
}}
"""

    return f"""You are a senior technical recruiter writing screening questions for a live phone screen.

{common_context}

TASK
Produce exactly {target_count} role-specific screening questions that would
genuinely differentiate a candidate who has DONE this work from one who has only read about
it or glanced at a tutorial.

STRICT RULES — FOLLOW EVERY ONE:
1. Do NOT write "Can you describe your experience with <skill>?" — that is the boilerplate
   you are replacing. Always probe a specific sub-capability, decision, trade-off, or
   failure mode.
2. For each skill in must-haves, write a question that assumes the candidate has used it
   in production and asks something concrete about HOW they used it.
     BAD:  "Do you have Databricks experience?"
     BAD:  "How many years of Databricks do you have?"
     GOOD: "Walk me through how you organized the bronze/silver/gold layers on your most
            recent Databricks project. What trade-offs drove using Delta Live Tables vs
            raw Structured Streaming for your silver layer?"
3. Mix question types across the set: ~50% technical-depth, ~25% architecture/scenario,
   ~25% behavioral/collaboration. For junior seniority: favor factual + debugging
   questions. For senior/staff/principal: favor architecture, scaling, failure-mode, and
   cross-team decisions.
4. Reference specific named concepts, tools, or artifacts where sensible (e.g. Medallion
   architecture, Unity Catalog, Autoloader, Delta Live Tables, Z-order, workspace
   governance). Do not be generic.
5. Each question must include a `pass_criteria` — a one-sentence CONCRETE signal the
    recruiter should listen for in the answer (e.g. "mentions bronze/silver/gold layering
    AND can explain a real consistency trade-off"). Never ask for years or use wording like
    "N+ years", "X years of experience", "minimum years", or similar duration thresholds.
6. Questions must be answerable in under 90 seconds each during a phone screen.
7. Do not repeat or paraphrase the same question.
8. Return nothing except the JSON array below.

OUTPUT FORMAT — return a STRICT JSON object like this:
{{
  "questions": [
    {{
      "question_text": "string",
      "pass_criteria": "string",
      "category": "technical-depth" | "architecture" | "behavioral" | "scenario",
      "related_skill": "string"
    }},
    ...
  ]
}}

No markdown, no preamble, no trailing commentary. JSON only.
"""


def _system_message_for_role_family(role_family: RoleFamily) -> str:
    if role_family == "non_it":
        return (
            "You write sharp, role-relevant screening questions for non-IT roles. "
            "You avoid software-delivery jargon (CI/CD, deployment, rollback, architecture). "
            "You always return strict JSON."
        )
    if role_family == "hybrid":
        return (
            "You write practical screening questions for hybrid roles, balancing business and "
            "technical depth only where explicitly relevant. You always return strict JSON."
        )
    return (
        "You write sharp, specific screening questions that separate real practitioners from "
        "surface-level candidates. You avoid generic phrasing. You always return strict JSON."
    )


def _sanitize_questions(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize LLM output onto the shape the frontend expects."""
    years_phrase = re.compile(
        r"(\b\d+\s*\+?\s*years?\b|\byears?\s+of\s+experience\b|\bminimum\s+years?\b)",
        flags=re.IGNORECASE,
    )

    def _strip_years_language(text: str) -> str:
        t = re.sub(years_phrase, "", text or "")
        t = re.sub(r"\s{2,}", " ", t)
        t = re.sub(r"\s+([,.;:!?])", r"\1", t)
        return t.strip(" ,.;:")

    cleaned: List[Dict[str, Any]] = []
    for idx, q in enumerate(raw or []):
        if not isinstance(q, dict):
            continue
        qt = _strip_years_language((q.get("question_text") or q.get("question") or "").strip())
        if not qt:
            continue
        pc = _strip_years_language((q.get("pass_criteria") or q.get("criteria") or "").strip())
        if not pc:
            pc = "Candidate gives concrete, project-level details with specific decisions and outcomes."
        cleaned.append({
            "question_text": qt,
            "pass_criteria": pc,
            "category": (q.get("category") or "role-specific").strip().lower(),
            "related_skill": (q.get("related_skill") or q.get("skill") or "").strip(),
            "is_default": False,
            "is_hard_filter": False,
            "order_index": idx,
        })
    return cleaned


def _dedupe_questions(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove duplicate/near-duplicate question_text values while preserving order."""
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for q in raw or []:
        text = str((q or {}).get("question_text") or "").strip()
        if not text:
            continue
        fp = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", "", text.lower())).strip()
        if not fp or fp in seen:
            continue
        seen.add(fp)
        deduped.append(q)
    return deduped


def _build_role_aware_fallback_question(
    *,
    role_family: RoleFamily,
    level: str,
    name: str,
    variant: int,
) -> Dict[str, str]:
    safe_name = name or "core responsibilities"
    level_norm = (level or "").strip().lower()

    if role_family == "non_it":
        if level_norm in ("intensive", "deep", "extensive", "high"):
            options = [
                (
                    f"Tell me about a high-stakes situation where {safe_name} directly impacted business outcomes. "
                    "How did you prioritize actions and align stakeholders?",
                    f"Candidate explains a concrete {safe_name} scenario with clear prioritization, stakeholder alignment, and measurable outcome.",
                    "scenario",
                ),
                (
                    f"Describe a time {safe_name} created execution risk. How did you detect the risk early and prevent downstream impact?",
                    f"Candidate shows proactive risk detection and a specific prevention action tied to {safe_name}.",
                    "process",
                ),
            ]
        elif level_norm in ("light", "low", "basic", "quick"):
            options = [
                (
                    f"Share one recent example where you used {safe_name} to deliver a concrete result.",
                    f"Candidate provides a specific {safe_name} example with ownership and clear result.",
                    "behavioral",
                ),
                (
                    f"What does strong execution in {safe_name} look like in your current role?",
                    f"Candidate articulates practical execution standards for {safe_name} with a real example.",
                    "process",
                ),
            ]
        else:
            options = [
                (
                    f"Walk me through a recent situation where {safe_name} required balancing speed, quality, and stakeholder expectations.",
                    f"Candidate describes trade-offs in {safe_name} and explains rationale behind the chosen approach.",
                    "stakeholder",
                ),
                (
                    f"Describe a challenging decision you made involving {safe_name}. What options did you evaluate and why?",
                    f"Candidate compares options for {safe_name} and gives a clear decision framework.",
                    "scenario",
                ),
            ]
    else:
        if level_norm in ("intensive", "deep", "extensive", "high"):
            options = [
                (
                    f"In a production context using {safe_name}, describe a difficult failure mode and how you prevented recurrence.",
                    f"Candidate details a real {safe_name} incident, diagnosis path, and prevention mechanism.",
                    "architecture",
                ),
                (
                    f"Walk me through a complex decision involving {safe_name}. What trade-offs did you evaluate and what outcome did you get?",
                    f"Candidate explains concrete trade-offs for {safe_name} with measurable impact.",
                    "technical-depth",
                ),
            ]
        elif level_norm in ("light", "low", "basic", "quick"):
            options = [
                (
                    f"What is one concrete task you completed recently using {safe_name}, and what result did it drive?",
                    f"Candidate gives a specific {safe_name} example with clear ownership and outcome.",
                    "technical-depth",
                ),
                (
                    f"Share a recent example where {safe_name} helped you resolve a practical problem.",
                    f"Candidate describes a real {safe_name} problem-resolution example with outcome.",
                    "scenario",
                ),
            ]
        else:
            options = [
                (
                    f"Walk me through a meaningful implementation using {safe_name}: what constraints did you face and what decision mattered most?",
                    f"Candidate explains a concrete {safe_name} implementation with constraints, rationale, and outcome.",
                    "scenario",
                ),
                (
                    f"Describe a challenging issue involving {safe_name}. How did you isolate root cause and validate the fix?",
                    f"Candidate demonstrates structured debugging or investigation steps for {safe_name} and validation approach.",
                    "technical-depth",
                ),
            ]

    q, c, cat = options[variant % len(options)]
    angle = [
        "prioritization approach",
        "stakeholder alignment",
        "risk handling",
        "decision criteria",
        "execution quality",
        "trade-off rationale",
        "measurable outcomes",
    ][variant % 7]
    q = f"{q} Please focus on your {angle}."
    if role_family == "non_it":
        q_lower = q.lower()
        if any(term in q_lower for term in _NON_IT_BANNED_TERMS):
            q = f"Tell me about a recent situation where {safe_name} influenced a business result."
            c = f"Candidate provides a concrete {safe_name} example with clear actions and measurable impact."
            cat = "process"
    return {"question_text": q, "pass_criteria": c, "category": cat, "related_skill": safe_name}


async def generate_screening_questions(
    openai_client: openai.AsyncOpenAI,
    *,
    model: str,
    job_title: str,
    rubric: Dict[str, Any],
    screening_level: str = "medium",
    customer_name: str = "",
    work_arrangement: str = "on-site",   # one of: on-site | onsite | hybrid | remote
    address: str = "",
    total_years: int = 0,
) -> List[Dict[str, Any]]:
    """
    Generate a full screening-question set for a job.

    Returns a list of question dicts:
      { question_text, pass_criteria, category, related_skill,
        is_default, is_hard_filter, order_index }

    The list always starts with these front-matter questions:
      1. Intro (default)
      2. Total-experience (default)
      3. Work-arrangement (hard filter, unless remote)
    followed by N role-specific questions from the LLM.
    """
    seniority = detect_seniority(job_title)
    target_count = _question_count_for_level(screening_level)

    # Split rubric skills by required/preferred.
    all_skills: List[Dict[str, Any]] = []
    for bucket_key in ("skills", "hard_skills", "soft_skills"):
        bucket = rubric.get(bucket_key) or []
        if isinstance(bucket, list):
            all_skills.extend(bucket)

    def _is_required(s: Dict[str, Any]) -> bool:
        r = (s.get("required") or s.get("importance") or "").lower()
        return r in ("required", "must have", "must-have", "must")

    required_skills = [s for s in all_skills if _is_required(s)]
    preferred_skills = [s for s in all_skills if not _is_required(s)]

    role_family = detect_role_family(job_title, rubric or {}, customer_name)

    industry_items = rubric.get("domain") or []
    industry = ""
    if industry_items and isinstance(industry_items, list):
        first = industry_items[0]
        industry = first.get("value") if isinstance(first, dict) else str(first)

    # --- Front-matter questions (always included, deterministic) ---------
    questions: List[Dict[str, Any]] = []

    # 1. Intro
    questions.append({
        "question_text": "To start, can you briefly introduce yourself and walk me through your current role?",
        "pass_criteria": "Candidate gives a coherent 60-90s intro mentioning current title, team, and recent focus.",
        "category": "default",
        "related_skill": "",
        "is_default": True,
        "is_hard_filter": False,
        "order_index": 0,
    })

    # 2. Total-experience
    if total_years and total_years > 0:
        exp_text = (
            f"Can you summarize the most relevant parts of your background for a {job_title} role, "
            f"including the kinds of projects and scope you've handled?"
        )
        exp_criteria = (
            "Candidate ties their background to comparable project scope, role expectations, and concrete outcomes."
        )
    else:
        exp_text = (
            f"Can you summarize the most relevant parts of your background for a {job_title} role, "
            "including the kinds of projects and scope you've handled?"
        )
        exp_criteria = (
            "Candidate explains directly relevant projects and responsibilities with concrete examples."
        )
    questions.append({
        "question_text": exp_text,
        "pass_criteria": exp_criteria,
        "category": "default",
        "related_skill": "",
        "is_default": True,
        "is_hard_filter": False,
        "order_index": 1,
    })

    # 3. Work-arrangement (hard filter unless remote)
    arrangement_norm = (work_arrangement or "").strip().lower().replace("-", "").replace("_", "")
    if arrangement_norm not in ("remote", "fullyremote", "wfh"):
        is_hybrid = "hybrid" in arrangement_norm
        arrangement_label = "a hybrid" if is_hybrid else "an onsite"
        addr_str = address.strip() if address else "the client site"
        questions.append({
            "question_text": (
                f"This role follows {arrangement_label} work arrangement based in {addr_str}. "
                f"Are you open to working in this setup?"
            ),
            "pass_criteria": (
                f"Candidate confirms they are open to {arrangement_label} work arrangement in {addr_str}."
            ),
            "category": "logistics",
            "related_skill": "",
            "is_default": True,
            "is_hard_filter": True,
            "order_index": 2,
        })

    # --- LLM-generated role-specific questions ---------------------------
    prompt = _build_prompt(
        job_title=job_title,
        seniority=seniority,
        customer_name=customer_name,
        industry=industry,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        total_years=total_years,
        target_count=target_count,
        role_family=role_family,
    )

    role_specific: List[Dict[str, Any]] = []
    try:
        completion = await openai_client.chat.completions.create(
            model=model or "gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": _system_message_for_role_family(role_family),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
            timeout=45,
        )
        raw = json.loads(completion.choices[0].message.content or "{}")
        role_specific = _dedupe_questions(_sanitize_questions(raw.get("questions", [])))
    except Exception as exc:
        logger.error(f"❌ screening_question_generator LLM failed: {exc}")
        # Fall back to deterministic per-skill templates — level-aware and
        # explicitly free of years-of-experience phrasing.
        fallback: List[Dict[str, Any]] = []
        focus_skills = required_skills or preferred_skills
        if not focus_skills:
            focus_skills = [{"value": "core role responsibilities"}]

        level = (screening_level or "").strip().lower()
        for idx in range(target_count):
            skill = focus_skills[idx % len(focus_skills)]
            name = str(skill.get("value") or skill.get("name") or "core responsibilities")
            prompt_obj = _build_role_aware_fallback_question(
                role_family=role_family,
                level=level,
                name=name,
                variant=idx,
            )
            fallback.append({
                "question_text": prompt_obj["question_text"],
                "pass_criteria": prompt_obj["pass_criteria"],
                "category": prompt_obj["category"],
                "related_skill": prompt_obj["related_skill"],
                "is_default": False,
                "is_hard_filter": False,
                "order_index": idx,
            })
        role_specific = _dedupe_questions(fallback)

    # Enforce exact role-specific count regardless of model output variance.
    role_specific = _dedupe_questions(role_specific)
    if len(role_specific) > target_count:
        role_specific = role_specific[:target_count]
    elif len(role_specific) < target_count:
        focus_skills = required_skills or preferred_skills
        if not focus_skills:
            focus_skills = [{"value": "core role responsibilities"}]
        safety = 0
        while len(role_specific) < target_count and safety < target_count * 4:
            idx = len(role_specific)
            skill = focus_skills[idx % len(focus_skills)]
            name = str(skill.get("value") or skill.get("name") or "core responsibilities")
            prompt_obj = _build_role_aware_fallback_question(
                role_family=role_family,
                level=screening_level,
                name=name,
                variant=idx + safety,
            )
            role_specific.append({
                "question_text": prompt_obj["question_text"],
                "pass_criteria": prompt_obj["pass_criteria"],
                "category": prompt_obj["category"],
                "related_skill": prompt_obj["related_skill"],
                "is_default": False,
                "is_hard_filter": False,
                "order_index": idx,
            })
            role_specific = _dedupe_questions(role_specific)
            safety += 1

    # Re-index role-specific entries to sit after the front-matter.
    base_index = len(questions)
    for offset, q in enumerate(role_specific):
        q["order_index"] = base_index + offset
        questions.append(q)

    return questions
