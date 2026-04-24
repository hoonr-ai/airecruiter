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

from typing import List, Dict, Any, Optional
import json
import logging
import re

import openai

logger = logging.getLogger(__name__)

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
) -> str:
    def _fmt_skills(skills: List[Dict[str, Any]]) -> str:
        if not skills:
            return "  (none)"
        lines = []
        for s in skills:
            name = s.get("value") or s.get("name") or ""
            years = s.get("minYears") or s.get("min_years") or 0
            lines.append(f"  - {name} (min {years} yrs)" if years else f"  - {name}")
        return "\n".join(lines)

    return f"""You are a senior technical recruiter writing screening questions for a live phone screen.

ROLE CONTEXT
  Job title: {job_title}
  Seniority level: {seniority}
  Customer: {customer_name or "N/A"}
  Industry: {industry or "N/A"}
  Target total experience: {total_years}+ years

RUBRIC — Must-have skills:
{_fmt_skills(required_skills)}

RUBRIC — Nice-to-have skills:
{_fmt_skills(preferred_skills)}

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
    )

    role_specific: List[Dict[str, Any]] = []
    try:
        completion = await openai_client.chat.completions.create(
            model=model or "gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write sharp, specific screening questions that separate "
                        "real practitioners from surface-level candidates. You avoid "
                        "generic 'describe your experience' phrasing. You always "
                        "return strict JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
            timeout=45,
        )
        raw = json.loads(completion.choices[0].message.content or "{}")
        role_specific = _sanitize_questions(raw.get("questions", []))
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
            name = skill.get("value") or skill.get("name") or "this technology"
            if level in ("intensive", "deep", "extensive", "high"):
                q_text = (
                    f"In a production system using {name}, describe a failure mode you encountered, "
                    "how you diagnosed root cause, and what design change prevented recurrence."
                )
                criteria = (
                    f"Candidate details a real {name} incident with diagnosis steps, trade-offs, "
                    "and a concrete prevention mechanism."
                )
                category = "architecture"
            elif level in ("light", "low", "basic", "quick"):
                q_text = (
                    f"What's one concrete task you handled with {name} recently, and what result did it drive?"
                )
                criteria = (
                    f"Candidate gives a specific {name} example with clear ownership and measurable impact."
                )
                category = "technical-depth"
            else:
                q_text = (
                    f"Walk me through a meaningful implementation using {name}: what constraints did you face, "
                    "what decision did you make, and why?"
                )
                criteria = (
                    f"Candidate explains a concrete {name} implementation with constraints, rationale, and outcomes."
                )
                category = "scenario"

            fallback.append({
                "question_text": q_text,
                "pass_criteria": criteria,
                "category": category,
                "related_skill": name,
                "is_default": False,
                "is_hard_filter": False,
                "order_index": idx,
            })
        role_specific = fallback

    # Enforce exact role-specific count regardless of model output variance.
    if len(role_specific) > target_count:
        role_specific = role_specific[:target_count]
    elif len(role_specific) < target_count:
        focus_skills = required_skills or preferred_skills
        if not focus_skills:
            focus_skills = [{"value": "core role responsibilities"}]
        for idx in range(len(role_specific), target_count):
            skill = focus_skills[idx % len(focus_skills)]
            name = skill.get("value") or skill.get("name") or "this area"
            role_specific.append({
                "question_text": (
                    f"Describe a real example where you used {name} to solve a non-trivial problem under constraints."
                ),
                "pass_criteria": (
                    "Candidate provides a specific situation, concrete decisions, and clear outcomes."
                ),
                "category": "scenario",
                "related_skill": name,
                "is_default": False,
                "is_hard_filter": False,
                "order_index": idx,
            })

    # Re-index role-specific entries to sit after the front-matter.
    base_index = len(questions)
    for offset, q in enumerate(role_specific):
        q["order_index"] = base_index + offset
        questions.append(q)

    return questions
