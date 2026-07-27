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
            L0.5=5 (Yes/No boolean only), Light=3, Medium=5, Intensive=7
The frontend still owns the "merge user-edits" flow — we return a fresh
set and the UI decides how to reconcile.
"""

from __future__ import annotations

from typing import List, Dict, Any, Optional
import json
import logging
import re

import openai

from services.role_family import detect_role_family, detect_it_domain

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
    if normalized == "l0.5":
        return 5  # L0.5 Boolean Screen — Yes/No questions only
    if normalized in ("light", "low", "basic", "quick"):
        return 3
    if normalized in ("intensive", "deep", "extensive", "high"):
        return 7
    # Default: Medium
    return 5


# Role-family + IT-domain detection lives in services/role_family.py so the
# scorer + extractor + screening generator all use the same classifier.
# `_build_prompt` injects the right shots / artifacts at template time, so
# non-data IT roles and non-IT roles each get role-shape-appropriate
# examples instead of being forced through a Databricks-flavored prompt.


# Domain shot bank — one BAD + one GOOD example per IT domain. Injected
# into the prompt at template time so the LLM anchors on role-shape vocab
# instead of the Databricks-only example that broke .NET / React / etc.
_IT_DOMAIN_SHOTS: Dict[str, str] = {
    "data": (
        "BAD:  \"Do you have Databricks experience?\"\n"
        "     GOOD: \"Can you describe a basic data pipeline you built or maintained using Databricks?\""
    ),
    "backend": (
        "BAD:  \"Do you have .NET / Java backend experience?\"\n"
        "     GOOD: \"What kind of APIs or services did you typically build using Java Spring Boot?\""
    ),
    "frontend": (
        "BAD:  \"Do you have React experience?\"\n"
        "     GOOD: \"Can you give an example of a simple feature or component you built using React?\""
    ),
    "devops": (
        "BAD:  \"Do you know Kubernetes?\"\n"
        "     GOOD: \"Have you used Kubernetes to manage deployments? Can you describe your general workflow?\""
    ),
    "mobile": (
        "BAD:  \"Do you have iOS / Android experience?\"\n"
        "     GOOD: \"What was the main focus of the last mobile app you worked on?\""
    ),
    "security": (
        "BAD:  \"Do you have appsec experience?\"\n"
        "     GOOD: \"Can you describe a general security best practice you always follow in your projects?\""
    ),
    "qa": (
        "BAD:  \"Do you have test automation experience?\"\n"
        "     GOOD: \"What tools do you typically use for writing automated tests, and what do you usually test?\""
    ),
    "generic_it": (
        "BAD:  \"Tell me about your engineering experience.\"\n"
        "     GOOD: \"Can you briefly describe a recent project you worked on and the technologies you used?\""
    ),
}

# Non-IT family shot bank — same shape, role-shape-appropriate vocab.
_NON_IT_FAMILY_SHOTS: Dict[str, str] = {
    "recruiting": (
        "BAD:  \"Describe your recruiting experience.\"\n"
        "     GOOD: \"Can you briefly describe the types of roles you have sourced for recently?\""
    ),
    "finance": (
        "BAD:  \"Tell me about month-end close.\"\n"
        "     GOOD: \"Can you walk me through your typical responsibilities during the month-end close?\""
    ),
    "ops": (
        "BAD:  \"Tell me about your ops experience.\"\n"
        "     GOOD: \"What are some basic operational processes you've helped manage day-to-day?\""
    ),
    "sales": (
        "BAD:  \"Tell me about your sales experience.\"\n"
        "     GOOD: \"Can you briefly describe the type of product or service you were selling in your last role?\""
    ),
    "hr": (
        "BAD:  \"Describe your HR background.\"\n"
        "     GOOD: \"What were your main day-to-day HR responsibilities in your most recent role?\""
    ),
    "marketing": (
        "BAD:  \"Tell me about your marketing experience.\"\n"
        "     GOOD: \"Can you share a brief example of a marketing campaign you helped run?\""
    ),
    "customer_success": (
        "BAD:  \"Tell me about a tough renewal.\"\n"
        "     GOOD: \"How do you typically ensure your customers are successful with the product?\""
    ),
    "program_management": (
        "BAD:  \"Tell me about a program you ran.\"\n"
        "     GOOD: \"Can you describe a typical project you managed from start to finish?\""
    ),
    "accounting": (
        "BAD:  \"Tell me about your accounting experience.\"\n"
        "     GOOD: \"What are the main accounting software tools you use on a daily basis?\""
    ),
    "healthcare": (
        "BAD:  \"Tell me about your clinical experience.\"\n"
        "     GOOD: \"Can you briefly describe the type of patient care you provided in your last role?\""
    ),
    "legal": (
        "BAD:  \"Describe your legal background.\"\n"
        "     GOOD: \"What kinds of contracts did you spend the most time reviewing recently?\""
    ),
    "education": (
        "BAD:  \"Describe your teaching experience.\"\n"
        "     GOOD: \"What age group or subject matter did you focus on in your most recent teaching role?\""
    ),
    "generic_non_it": (
        "BAD:  \"Describe your background.\"\n"
        "     GOOD: \"Can you describe your core responsibilities in your most recent role?\""
    ),
}

# Artifacts/named-concept lists by family/domain. Replaces the hardcoded
# data-engineering list. Used in rule 4 of the prompt.
_FAMILY_ARTIFACTS: Dict[str, str] = {
    "data": "Medallion architecture, Unity Catalog, Autoloader, Delta Live Tables, Z-order, workspace governance",
    "backend": "connection pooling (HikariCP), retry policies, idempotency keys, structured logging, distributed tracing, p95 latency targets",
    "frontend": "React reconciliation, Suspense boundaries, hydration, code-splitting, Core Web Vitals (LCP/CLS/INP), SSR vs CSR",
    "devops": "Helm charts, GitOps flow, blue/green or canary rollouts, SLOs/error budgets, OpenTelemetry, autoscaling policies",
    "mobile": "view lifecycle, background tasks, push delivery, app-size budgets, offline sync, battery profiling",
    "security": "OWASP Top-10, threat modeling (STRIDE), SAST/DAST findings, secrets management, IAM least-privilege, incident playbooks",
    "qa": "test pyramid, contract testing, deterministic seeding, flake quarantine, coverage targets, regression suites",
    "generic_it": "production incidents, code review patterns, release playbooks, observability dashboards, runbooks",
    "recruiting": "sourcing channels, ATS pipeline stages, intake calls, hiring-manager scorecards, offer-acceptance funnel",
    "finance": "GL accounts, accruals, month-end close (MEC), variance analysis, internal controls, SOX compliance",
    "ops": "process maps, RACI, SLA/OLA, throughput vs cycle time, lean/six sigma artifacts, vendor scorecards",
    "sales": "stage progression, MEDDIC/MEDDPICC, win/loss analysis, ARR/NRR, pipeline coverage, account plans",
    "hr": "ER cases, performance calibration, comp bands, engagement surveys, workforce planning, HRIS records",
    "marketing": "campaign attribution, MQL/SQL handoff, brand guidelines, content calendars, A/B tests, channel ROI",
    "customer_success": "health scores, QBRs, renewal forecast, expansion playbooks, churn cohorts, onboarding milestones",
    "program_management": "RAID logs, dependency maps, executive readouts, status cadence, escalation paths, OKRs/north-star metrics, integrated launch plans",
    "accounting": "general ledger, journal entries, audit workpapers, SOX controls, reconciliations, accruals, trial balance, fixed asset register",
    "healthcare": "patient charts, care pathways, infection-control protocols, vitals trending, escalation criteria, EHR documentation, handoff communication",
    "legal": "contract redlines, clause libraries, NDAs/MSAs, discovery deadlines, regulatory filings, privilege logs, matter-management trackers",
    "education": "lesson plans, formative assessments, IEP accommodations, standards alignment, classroom-management routines, parent communication",
    "generic_non_it": "stakeholder maps, decision logs, OKRs, runbooks, status reports",
}


def _shot_key(family: str, domain: str) -> str:
    """Pick which shot bank to read from. IT roles use the IT-domain bank;
    everything else uses the family bank directly."""
    return domain if family == "it" else family


def _build_prompt(
    *,
    job_title: str,
    seniority: str,
    customer_name: str,
    job_description: str,
    industry: str,
    required_skills: List[Dict[str, Any]],
    preferred_skills: List[Dict[str, Any]],
    total_years: int,
    target_count: int,
    family: str = "it",
    domain: str = "generic_it",
    leniency_mode: bool = False,
    difficulty_mode: str = "medium",
    boolean_mode: bool = False,
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

    def _fmt_job_description(text: str) -> str:
        cleaned = " ".join((text or "").split()).strip()
        if not cleaned:
            return "N/A"
        if len(cleaned) > 1800:
            cleaned = cleaned[:1800].rstrip() + "..."
        return cleaned

    is_it = family == "it"
    shot_key = _shot_key(family, domain)
    shot_block = (
        _IT_DOMAIN_SHOTS.get(shot_key)
        if is_it
        else _NON_IT_FAMILY_SHOTS.get(shot_key, _NON_IT_FAMILY_SHOTS["generic_non_it"])
    ) or _IT_DOMAIN_SHOTS["generic_it"]
    artifacts = _FAMILY_ARTIFACTS.get(shot_key) or _FAMILY_ARTIFACTS["generic_it"]

    difficulty = (difficulty_mode or "medium").strip().lower()
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"

    intro = (
        "You are a senior technical recruiter and AI interview screener specializing in\n"
        f"engineering hiring for a {seniority} {job_title} role.\n\n"
        "This is a 10–20 minute first-round AUDIO screening — NOT a deep technical interview,\n"
        "NOT a behavioral interview, NOT a coding exercise. Your job:\n"
        "  - Verify the candidate has relevant experience with the rubric skills\n"
        "  - Detect genuine understanding through practical discussion\n"
        "  - Validate real-world application and problem-solving ability\n"
        "  - Surface practical knowledge and communication clarity"
        if (is_it and difficulty == "easy")
        else (
            "You are a senior technical recruiter and AI interview screener specializing in\n"
            f"engineering hiring for a {seniority} {job_title} role.\n\n"
            "This is a 10–20 minute first-round AUDIO screening — NOT a deep technical interview,\n"
            "NOT a behavioral interview, NOT a coding exercise. Your job:\n"
            "  - Verify the candidate genuinely possesses the rubric skills (no keyword stuffing)\n"
            "  - Detect fake or surface-level experience\n"
            "  - Validate practical understanding through verbal discussion\n"
            "  - Surface depth, production exposure, and problem-solving maturity"
            if is_it
            else "You are an experienced recruiter writing screening questions for a live phone screen."
        )
    )
    rule3 = (
        "Questions must be INCREDIBLY SIMPLE and CONVERSATIONAL. This is a preliminary AI screening call, NOT a technical interview.\n"
        "   Do NOT ask candidates to design systems, solve problems, manage requests, ensure efficiency, or troubleshoot.\n"
        "   Literally just ask them if they have used a tool, what they used it for, or to give a brief example of a project where they used it.\n\n"
        "   Each question must simply verify:\n"
        "     - Basic familiarity with a required skill\n"
        "     - General awareness of what a tool is used for\n\n"
        "   Example of good questions:\n"
        "     - \"Have you worked with React before? What kind of components did you build?\"\n"
        "     - \"Can you give a brief example of how you used Java Spring Boot in your last role?\"\n"
        "     - \"What was the main focus of the last Java application you worked on?\"\n\n"
        f"   Keep the difficulty extremely low, regardless of the {total_years}+ year target experience.\n"
        "   The goal is just to ensure they aren't completely faking their resume. If they can describe basic usage, they pass.\n\n"
        "   AVOID, every time:\n"
        "     - 'How do you handle X?' or 'How do you ensure Y?' or 'What steps do you take to Z?' (Too difficult!)\n"
        "     - Technical troubleshooting or debugging scenarios\n"
        "     - System design or architecture questions\n"
        "     - Coding exercises or syntax trivia"
    ) if is_it else (
        "Keep the questions extremely simple and high-level. This is an automated AI pre-screen.\n"
        "   Just ask about their general familiarity with the required tools or processes. Do NOT ask complex\n"
        "   situational or behavioral questions. Example: \"How have you used Salesforce in your daily work?\""
    )
    categories_line = (
        '"category": "experience" | "project-example" | "tool-familiarity" | "fundamentals" | "technical-depth",'
        if is_it
        else '"category": "process" | "stakeholder" | "behavioral" | "scenario",'
    )
    rubric_anchor_rule = (
        "RUBRIC COVERAGE (IT): Cover the must-have skills across the question set, but BUNDLE\n"
        "   related skills into single questions where natural — Java + Spring Boot together,\n"
        "   Angular + React in a state-sharing question, HTML + CSS + JavaScript in a UI\n"
        "   question, Agile woven into how the work was delivered. It's better to cover 7\n"
        "   skills sharply across 5 questions than to one-shot each skill awkwardly. Set\n"
        "   `related_skill` to the primary rubric skill the question targets (when bundled,\n"
        "   pick the strongest). Skills that are methodologies (Agile, Scrum, TDD) should be\n"
        "   woven INTO scenario questions, not asked as standalone process questions."
    ) if is_it else (
        "RUBRIC ANCHORING: Where the rubric lists named tools, processes, or frameworks, ground\n"
        "   each question in one of them and set `related_skill` to the matching rubric value."
    )

    task_objective = (
        "Produce exactly {target_count} role-specific screening questions that are job-relevant and\n"
        "INCREDIBLY beginner-friendly. Focus purely on basic familiarity. Example: 'Have you used Java? What did you build?'\n"
        "Avoid ANY technical problem-solving."
        if difficulty == "easy"
        else (
            "Produce exactly {target_count} role-specific screening questions that are job-relevant and\n"
            "extremely simple. Focus on everyday usage. Example: 'Can you give an example of how you used Java?'\n"
            "Avoid ANY 'how do you handle X' or 'what steps do you take to Y' questions."
            if difficulty == "medium"
            else "Produce exactly {target_count} role-specific screening questions that are job-relevant and\n"
            "conversational. Ask about their experience with the tools, but DO NOT ask them to solve technical problems."
        )
    )

    boolean_rule = (
        "\nBOOLEAN MODE — ALL questions MUST be answerable with a simple \"Yes\" or \"No\".\n"
        "   - Phrase every question as \"Do you have...\", \"Have you...\", \"Are you...\", \"Can you confirm...\", etc.\n"
        "   - Do NOT ask open-ended or descriptive questions (no \"Can you describe\", no \"Walk me through\", no \"Tell me about\").\n"
        "   - Example: \"Have you worked with React in a professional project?\"\n"
        "   - Example: \"Do you have hands-on experience with SQL databases?\""
    ) if boolean_mode else ""

    pass_criteria_rule = (
        "7. The `pass_criteria` field MUST instruct the AI evaluator to accept a simple 'Yes' without penalizing for a lack of detail.\n"
        "    - Format exactly like this: \"Pass: Candidate confirms with 'Yes' or affirmative. Do not expect or require descriptive depth. | Red flag: Candidate says 'No'.\""
    ) if boolean_mode else (
        "7. The `pass_criteria` field MUST be ONE string with two parts:\n"
        "    - \"Pass: \" followed by a simple CHECKLIST of 1–3 basic concepts or general tasks a practitioner would mention. NOT a sentence.\n"
        "    - \" | Red flag: \" followed by ONE short phrase a fake/surface candidate would say.\n"
        "    Format examples:\n"
        "      - \"Pass: writing SQL queries, joining tables, basic CRUD operations. | Red flag: 'I just copy pasted code.'\"\n"
        "      - \"Pass: creating React components, using useState, passing props. | Red flag: 'React is a database.'\"\n"
        "    Never use \"N+ years\", \"X years of experience\", or duration thresholds anywhere."
    )

    return f"""{intro}

ROLE CONTEXT
  Job title: {job_title}
  Seniority level: {seniority}
  Customer: {customer_name or "N/A"}
  Industry: {industry or "N/A"}
  Target total experience: {total_years}+ years
  Role family: {family}
  Role domain: {domain}
  Job description: {_fmt_job_description(job_description)}

RUBRIC — Must-have skills:
{_fmt_skills(required_skills)}

RUBRIC — Nice-to-have skills:
{_fmt_skills(preferred_skills)}
{boolean_rule}
TASK
{task_objective.format(target_count=target_count)}

STRICT RULES — FOLLOW EVERY ONE:
1. Only ask "Have you used X?" or "What kind of projects did you build with X?". Never ask candidates to solve a problem or explain *how* they ensure performance, handle concurrency, or write efficient code. Those are technical interview questions, not screening questions.
2. Ground questions in the rubric skills. Bundle naturally related skills into a single
   question (Java + Spring Boot, Angular + React, HTML + CSS + JS into a UI question).
3. {rubric_anchor_rule}
4. {rule3}
5. Reference specific named concepts, tools, or artifacts where sensible — for THIS
   domain that means: {artifacts}.
6. It is PERFECTLY FINE to ask "Can you describe a recent project where you used X?" or "Tell me about your experience with Y." Do not force artificial scenarios.
{pass_criteria_rule}
8. Each `question_text` is ≤ 25 words and answerable verbally in 30–60 seconds — no coding. Ask simple, direct questions. "What kind of tasks did you do with X?" is perfect.
9. Do not repeat or paraphrase the same question.
10. Return nothing except the JSON below.
11. Do NOT generate questions about work arrangement (onsite / remote / hybrid /
    willingness to relocate or work onsite), candidate location, visa or work
    authorization, salary or compensation, availability or start date, or
    current job-search status. The front-matter already covers those — your
    questions would be duplicates and will be rejected. Every question MUST
    probe a rubric skill or role competence.
12. Use the job description as a hard grounding source for responsibilities,
    tools, scope, and expected depth.
13. {"DIFFICULTY = EASY (very beginner): purely ask 'have you used this' and 'what did you use it for'." if difficulty == "easy" else ("DIFFICULTY = MEDIUM (beginner): purely ask 'can you give an example of a project where you used this'." if difficulty == "medium" else "DIFFICULTY = HARD (intermediate): purely ask 'tell me about your general experience with this tool'. No problem solving.")}

OUTPUT FORMAT — return a STRICT JSON object like this:
{{
  "questions": [
    {{
      "question_text": "string",
      "pass_criteria": "string",
      {categories_line}
      "related_skill": "string"
    }},
    ...
  ]
}}

No markdown, no preamble, no trailing commentary. JSON only.
"""


# Phrases that indicate a question is behavioral / observational rather than
# probing concrete technical knowledge. Matched case-insensitively against the
# IT-role question text. When any of these hits, the question is dropped and
# the deterministic technical template fills its slot.
_IT_BEHAVIORAL_BAN_PATTERNS = re.compile(
    r"(when priorities (have )?conflict"
    r"|how did you balance"
    r"|what does (strong execution|success|good) look like"
    r")",
    flags=re.IGNORECASE,
)


def _is_it_behavioral_question(text: str) -> bool:
    """True if an IT-role question reads as behavioral/observational instead
    of probing concrete technical knowledge."""
    return bool(_IT_BEHAVIORAL_BAN_PATTERNS.search(text or ""))


# Logistics topics owned by the front-matter. The LLM keeps re-emitting these
# (work-arrangement, location, visa, comp, availability) as role-specific
# questions, producing exact duplicates of Q3/Q4/Q6/Q7/Q8. Defense in depth on
# top of the prompt rule — anything matching gets dropped and replaced with a
# rubric-anchored technical template by the existing top-up pass.
_LOGISTICS_BAN_PATTERNS = re.compile(
    r"(work\s+arrangement|onsite|on-site|on\s+site|remote\s+work|hybrid\s+work"
    r"|willing(ness)?\s+to\s+(relocate|work|commute)"
    r"|are\s+you\s+open\s+to\s+working\s+(onsite|remote|hybrid|in)"
    r"|current\s+location|where\s+are\s+you\s+(currently\s+)?(based|located)"
    r"|visa\s+sponsorship|work\s+authoriz|authorized\s+to\s+work"
    r"|expected\s+(compensation|salary|pay)|current\s+(compensation|salary|pay)"
    r"|when\s+can\s+you\s+start|earliest\s+(availability|start))",
    flags=re.IGNORECASE,
)


def _is_logistics_question(text: str) -> bool:
    return bool(_LOGISTICS_BAN_PATTERNS.search(text or ""))


def _sanitize_questions(
    raw: List[Dict[str, Any]],
    *,
    is_it_role: bool = False,
) -> List[Dict[str, Any]]:
    """Normalize LLM output onto the shape the frontend expects.

    For IT roles, drop any question whose phrasing reads as behavioral or
    observational so the caller can refill the slot with a deterministic
    technical template instead.
    """
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
        if is_it_role and _is_it_behavioral_question(qt):
            logger.info(
                "screening_question_generator: dropping behavioral IT question (will be replaced): %r",
                qt[:160],
            )
            continue
        if _is_logistics_question(qt):
            logger.info(
                "screening_question_generator: dropping logistics question (front-matter duplicate, will be replaced): %r",
                qt[:160],
            )
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


def _is_remote_role(work_arrangement: str, city: str) -> bool:
    """True for remote roles. Catches "Remote / W2", "Fully Remote", etc.,
    AND the JobDiva-import quirk where location_type is empty but the city
    field literally contains "REMOTE" (e.g. "REMOTE, ON")."""
    norm = (work_arrangement or "").strip().lower().replace("-", "").replace("_", "")
    if any(k in norm for k in ("remote", "wfh", "virtual", "telecommute", "workfromhome")):
        return True
    return (city or "").strip().upper() == "REMOTE"


async def generate_screening_questions(
    openai_client: openai.AsyncOpenAI,
    *,
    model: str,
    job_title: str,
    rubric: Dict[str, Any],
    screening_level: str = "medium",
    customer_name: str = "",
    job_description: str = "",
    work_arrangement: str = "on-site",   # one of: on-site | onsite | hybrid | remote
    city: str = "",
    address: str = "",
    total_years: int = 0,
    leniency_mode: bool = False,
    difficulty_mode: str = "",
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
    boolean_mode = (screening_level or "").strip().lower() == "l0.5"
    target_count = _question_count_for_level(screening_level)
    difficulty_mode_normalized = (difficulty_mode or "").strip().lower()
    if difficulty_mode_normalized not in ("easy", "medium", "hard"):
        if leniency_mode:
            difficulty_mode_normalized = "easy"
        else:
            level = (screening_level or "").strip().lower()
            if level in ("light", "low", "basic", "quick"):
                difficulty_mode_normalized = "easy"
            elif level in ("intensive", "deep", "extensive", "high"):
                difficulty_mode_normalized = "hard"
            else:
                difficulty_mode_normalized = "medium"

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

    # B2/B3: classify the role so the prompt + fallback emit role-shape-
    # appropriate questions instead of forcing every role through the
    # legacy Databricks-flavored template.
    family = detect_role_family(job_title, industry, required_skills)
    domain = detect_it_domain(job_title, required_skills, preferred_skills) if family == "it" else "generic_it"

    # --- Front-matter questions (always included, deterministic) ---------
    questions: List[Dict[str, Any]] = []

    # 1. Intro
    if boolean_mode:
        intro_text = "Are you currently available and open to exploring a new job opportunity?"
        intro_criteria = "Candidate confirms they are available and interested in exploring new opportunities."
    else:
        intro_text = "To start, can you briefly introduce yourself and walk me through your current role?"
        intro_criteria = "Candidate gives a coherent 60-90s intro mentioning current title, team, and recent focus."
    questions.append({
        "question_text": intro_text,
        "pass_criteria": intro_criteria,
        "category": "default",
        "related_skill": "",
        "is_default": True,
        "is_hard_filter": True,  # Q1 is always a qualifying gate for all interview levels
        "order_index": 0,
    })

    # 2. Total-experience
    if boolean_mode:
        years_label = f"{total_years}+ years" if (total_years and total_years > 0) else "relevant"
        exp_text = f"Do you have {years_label} of experience in a {job_title} role?"
        exp_criteria = f"Candidate confirms they have {years_label} of experience as a {job_title}."
    elif total_years and total_years > 0:
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
    if not _is_remote_role(work_arrangement, city):
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
            "is_hard_filter": boolean_mode,  # L0.5: qualifying gate (Q3); L1/L2: informational background
            "order_index": 2,
        })

    # --- LLM-generated role-specific questions ---------------------------
    prompt = _build_prompt(
        job_title=job_title,
        seniority=seniority,
        customer_name=customer_name,
        job_description=job_description,
        industry=industry,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        total_years=total_years,
        target_count=target_count,
        family=family,
        domain=domain,
        leniency_mode=leniency_mode,
        difficulty_mode=difficulty_mode_normalized,
        boolean_mode=boolean_mode,
    )

    is_it_role = family == "it"
    difficulty = difficulty_mode_normalized
    boolean_system_suffix = (
        " BOOLEAN MODE: every question_text MUST be answerable with Yes or No only "
        "(e.g. 'Have you worked with X?', 'Do you have experience in Y?'). "
        "No open-ended or descriptive questions."
    ) if boolean_mode else ""
    system_message = (
        "You are a senior technical recruiter and AI interview screener for engineering hiring. "
        "Write 4–6 first-round audio screening questions at VERY BEGINNER level: extremely simple, "
        "high-level awareness checks tied to rubric skills. Avoid technical depth. NOT a deep "
        "technical interview. NOT a behavioral interview. NOT a coding exercise. Output strict JSON only."
        if difficulty == "easy"
        else (
            "You are a senior technical recruiter and AI interview screener for engineering hiring. "
            "Write 4–6 first-round audio screening questions at BEGINNER level: clear, practical, "
            "day-to-day fundamentals. Avoid advanced edge cases. NOT a deep "
            "technical interview. NOT a behavioral interview. NOT a coding exercise. Output strict JSON only."
            if difficulty == "medium"
            else "You are a senior technical recruiter and AI interview screener for engineering hiring. "
            "Write 4–6 first-round audio screening questions at INTERMEDIATE level: practical "
            "hands-on checks, but keep it accessible. Avoid overly rigorous depth-probing. "
            "NOT a behavioral interview. NOT a coding exercise. Output strict JSON only."
        )
    ) if is_it_role else (
        "You write role-relevant screening questions for non-technical roles at VERY BEGINNER level: "
        "high-level awareness and basic checks. Avoid jargon. Output strict JSON."
        if difficulty == "easy"
        else (
            "You write role-relevant screening questions for non-technical roles at BEGINNER level: "
            "fundamentals and day-to-day execution checks. Avoid jargon. Output strict JSON."
            if difficulty == "medium"
            else "You write role-relevant screening questions for non-technical roles at INTERMEDIATE level: "
            "practical scenarios, but keep it accessible and high-level. Avoid jargon. Output strict JSON."
        )
    )
    system_message = system_message + boolean_system_suffix

    role_specific: List[Dict[str, Any]] = []
    # Cache the LLM JSON output keyed by (system, user) prompt content +
    # screening_level. Recruiters frequently regenerate screening
    # questions on an unchanged JD; this turns each repeat into a Redis
    # round-trip. TTL: 30 days. The cache check lives inside the try so
    # that a corrupt-cache edge case still falls through to the
    # deterministic-template fallback below — same safety net as a real
    # LLM failure.
    from core import llm_cache as _llm_cache
    _screening_cache_key = _llm_cache.make_key(
        "screening", 2, system_message, prompt, screening_level, leniency_mode, difficulty_mode_normalized, boolean_mode
    )
    try:
        _cached = await _llm_cache.get_json(_screening_cache_key)
        if _cached is not None:
            logger.info("screening questions: cache HIT")
            raw = _cached
        else:
            completion = await openai_client.chat.completions.create(
                model=model or "gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
                response_format={"type": "json_object"},
                timeout=45,
                prompt_cache_key="screening-v1",
            )
            raw = json.loads(completion.choices[0].message.content or "{}")
            await _llm_cache.set_json(
                _screening_cache_key, raw, ttl_seconds=30 * 24 * 60 * 60
            )
        role_specific = _sanitize_questions(raw.get("questions", []), is_it_role=is_it_role)
    except Exception as exc:
        logger.error(f"❌ screening_question_generator LLM failed: {exc}")
        # Fall back to deterministic per-skill templates — level-aware,
        # family-aware, and explicitly free of years-of-experience phrasing.
        fallback: List[Dict[str, Any]] = []
        focus_skills = required_skills or preferred_skills
        if not focus_skills:
            focus_skills = [{"value": "core role responsibilities"}]

        level = (screening_level or "").strip().lower()
        # Difficulty selection from Step 4 regenerate overrides fallback
        # template depth so easy/medium/hard stays consistent even if LLM fails.
        if boolean_mode:
            level = "l0.5"
        elif difficulty_mode_normalized == "easy":
            level = "light"
        elif difficulty_mode_normalized == "medium":
            level = "medium"
        elif difficulty_mode_normalized == "hard":
            level = "intensive"
        for idx in range(target_count):
            skill = focus_skills[idx % len(focus_skills)]
            name = skill.get("value") or skill.get("name") or (
                "this technology" if is_it_role else "this area"
            )
            if boolean_mode:
                q_text = f"Have you worked with {name} in a professional or project setting?"
                criteria = (
                    f"Pass: candidate confirms hands-on experience with {name}. "
                    f"| Red flag: 'No' or 'I've only read about it.'"
                )
                fallback.append({
                    "question_text": q_text,
                    "pass_criteria": criteria,
                    "category": "role-specific",
                    "related_skill": name,
                    "is_default": False,
                    "is_hard_filter": False,
                    "order_index": idx,
                })
                continue
            if is_it_role:
                if level in ("intensive", "deep", "extensive", "high"):
                    q_text = (
                        f"In a system using {name}, if errors increased, what would you check first "
                        "and what practical step would you take next?"
                        if leniency_mode
                        else f"In a system using {name}, what is a common issue you might encounter, "
                        "and how would you generally go about troubleshooting it?"
                    )
                    criteria = (
                        f"Candidate demonstrates understanding of how to troubleshoot issues in {name}. "
                        f"They can identify a debugging approach and explain a {name} configuration or code change."
                        if leniency_mode
                        else f"Candidate mentions a typical issue in {name} and provides a reasonable, high-level troubleshooting step."
                    )
                    category = "debugging"
                elif level in ("light", "low", "basic", "quick"):
                    q_text = (
                        f"What is one {name} feature, API, or configuration you have used directly, "
                        "and what changed because of it?"
                        if leniency_mode
                        else f"Can you describe a basic feature or component of {name} that you "
                        "have used in your day-to-day work?"
                    )
                    criteria = (
                        f"Candidate demonstrates familiarity with {name}. They can describe a feature or configuration they've worked with and how it affects behavior."
                        if leniency_mode
                        else f"Candidate describes a real {name} feature or component and shows basic familiarity with its usage."
                    )
                    category = "technical-depth"
                else:
                    q_text = (
                        f"Describe one implementation choice you made with {name}, and one trade-off "
                        "you considered while making that decision."
                        if leniency_mode
                        else f"Describe a time you used {name}. What was the general goal, "
                        "and what basic steps did you take to achieve it?"
                    )
                    criteria = (
                        f"Candidate can explain a {name} implementation decision they made and articulate a trade-off or consideration involved."
                        if leniency_mode
                        else f"Candidate explains a scenario where they used {name} and outlines the basic approach taken."
                    )
                    category = "technical-depth"
            else:
                # Non-IT family fallback — stakeholder/process/outcome wording,
                # no production/architecture jargon.
                if level in ("intensive", "deep", "extensive", "high"):
                    q_text = (
                        f"Describe a situation where {name} influenced an outcome. "
                        "What decision did you make and who did you coordinate with?"
                        if leniency_mode
                        else f"Describe a real situation where {name} drove a measurable outcome. "
                        "What was the decision, who were the stakeholders, and what trade-off did you make?"
                    )
                    criteria = (
                        f"Candidate can discuss a situation involving {name} with some stakeholders and outcome."
                        if leniency_mode
                        else f"Candidate names specific stakeholders, a concrete decision, and a measurable result tied to {name}."
                    )
                    category = "scenario"
                elif level in ("light", "low", "basic", "quick"):
                    q_text = (
                        f"What's one recent task involving {name} where you contributed directly, "
                        "and what changed because of your work?"
                        if leniency_mode
                        else f"What's one recent task involving {name} where you owned the outcome? "
                        "What changed because of your work?"
                    )
                    criteria = (
                        f"Candidate can describe involvement with {name} and a change or outcome they contributed to."
                        if leniency_mode
                        else f"Candidate gives a specific {name} example with clear ownership and a concrete change in outcome."
                    )
                    category = "process"
                else:
                    q_text = (
                        f"Walk me through a recent piece of work involving {name}: who did you coordinate "
                        "with, and what result did your work support?"
                        if leniency_mode
                        else f"Walk me through a recent piece of work involving {name}: who did you coordinate with, "
                        "what trade-off did you make, and what was the result?"
                    )
                    criteria = (
                        f"Candidate can describe a {name} situation with coordination and outcome involvement."
                        if leniency_mode
                        else f"Candidate explains a concrete {name} situation with stakeholders, a trade-off, and a measurable outcome."
                    )
                    category = "stakeholder"

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
    # When questions were dropped (e.g. IT behavioral filter), refill slots
    # with deterministic technical templates so the role-specific budget is
    # always met without re-introducing observational phrasing.
    if len(role_specific) > target_count:
        role_specific = role_specific[:target_count]
    elif len(role_specific) < target_count:
        focus_skills = required_skills or preferred_skills
        if not focus_skills:
            focus_skills = [{"value": "core role responsibilities"}]
        already_anchored = {(q.get("related_skill") or "").lower() for q in role_specific}
        skill_pool = [
            s for s in focus_skills
            if (s.get("value") or s.get("name") or "").lower() not in already_anchored
        ] or focus_skills
        for idx in range(len(role_specific), target_count):
            skill = skill_pool[idx % len(skill_pool)]
            name = skill.get("value") or skill.get("name") or (
                "this technology" if is_it_role else "this area"
            )
            if boolean_mode:
                q_text = f"Have you worked with {name} in a professional or project setting?"
                criteria = (
                    f"Pass: candidate confirms hands-on experience with {name}. "
                    f"| Red flag: 'No' or 'I've only read about it.'"
                )
                category = "role-specific"
            elif is_it_role:
                q_text = (
                    f"What is one {name} feature or behavior you have used directly, "
                    "and what was the outcome?"
                    if leniency_mode
                    else f"Can you give a simple example of how you have used {name} "
                    "in a recent project?"
                )
                criteria = (
                    f"Candidate demonstrates familiarity with {name} and can describe a feature or behavior they've worked with."
                    if leniency_mode
                    else f"Candidate gives a clear, practical example of how they have utilized {name}."
                )
                category = "technical-depth"
            else:
                q_text = (
                    f"Walk through a recent piece of work involving {name}: who did you coordinate "
                    "with, and what result did your work support?"
                    if leniency_mode
                    else f"Walk through a recent piece of work involving {name}: who did you "
                    "coordinate with, what trade-off did you make, and what was the result?"
                )
                criteria = (
                    f"Candidate can describe a {name} situation with coordination and outcome involvement."
                    if leniency_mode
                    else f"Candidate explains a concrete {name} situation with stakeholders, a "
                    "trade-off, and a measurable outcome."
                )
                category = "scenario"
            role_specific.append({
                "question_text": q_text,
                "pass_criteria": criteria,
                "category": category,
                "related_skill": name,
                "is_default": False,
                "is_hard_filter": False,
                "order_index": idx,
            })

    # Re-index role-specific entries to sit after the front-matter.
    # For L0.5 (boolean_mode), only questions at offset ≥ 5 are qualifying hard filters.
    # The first 5 role-specific questions (offsets 0–4) are background/informational:
    #   Hybrid (base=3): offsets 0-4 → order_index 3-7 → question_orders 4-8  (background)
    #                    offsets 5+  → order_index 8+  → question_orders 9+   (qualifying)
    #   Remote  (base=2): offsets 0-4 → order_index 2-6 → question_orders 3-7  (background)
    #                    offsets 5+  → order_index 7+  → question_orders 8+   (qualifying)
    # For L1/L2 hybrid (not boolean_mode, not remote), the first role-specific question
    # (offset 0, question_order 4) is a qualifying hard filter per the classification table.
    # For L1/L2 remote, no role-specific question is qualifying (only Q1 is).
    is_remote_role = _is_remote_role(work_arrangement, city)
    base_index = len(questions)
    for offset, q in enumerate(role_specific):
        if boolean_mode:
            q["is_hard_filter"] = offset >= 5
        elif not is_remote_role and offset == 0:
            q["is_hard_filter"] = True  # L1/L2 hybrid Q4: first role-specific is qualifying
        q["order_index"] = base_index + offset
        questions.append(q)

    return questions
