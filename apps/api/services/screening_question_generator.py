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


# Role-family + IT-domain detection. Single `_build_prompt` injects the
# right shots / artifacts at template time, so non-data IT roles and non-IT
# roles each get role-shape-appropriate examples instead of being forced
# through a Databricks-flavored prompt.

_IT_TITLE_KEYWORDS = (
    "engineer", "developer", "architect", "devops", "sre", "site reliability",
    "data engineer", "data scientist", "machine learning", "ai engineer",
    "cloud", "programmer", "full stack", "backend", "frontend", "qa automation",
    "platform", "security engineer", "database", "etl", "analytics engineer",
    "ios", "android",
)

_NON_IT_FAMILY_RULES: List[tuple] = [
    ("recruiting", (
        "recruiter", "talent acquisition", "sourcer", "headhunter",
        "ta partner",
    )),
    ("finance", (
        "accountant", "controller", "fp&a", "treasury", "cfo", "audit",
        "gaap", "ifrs", "payroll", "bookkeep",
    )),
    ("ops", (
        "operations manager", "coo", "ops lead", "supply chain",
        "logistics", "procurement", "vendor management", "operations",
    )),
    ("sales", (
        "account executive", "ae ", "bdr", "sdr", "sales development",
        "sales rep", "salesperson", "sales manager",
    )),
    ("hr", (
        "hrbp", "hr manager", "people partner", "chro", "benefits",
        "hris", "human resources",
    )),
    ("marketing", (
        "marketing manager", "demand gen", "content marketing", "seo",
        "brand manager", "growth marketing", "marketing director",
    )),
    ("customer_success", (
        "customer success", "csm", "renewal manager",
    )),
]

_IT_DOMAIN_RULES: List[tuple] = [
    ("data", (
        "databricks", "snowflake", "spark", "kafka", "airflow", "dbt",
        "redshift", "bigquery", "etl", "data engineer", "delta",
        "lakehouse", "data scientist", "analytics engineer",
    )),
    ("frontend", (
        "react", "angular", "vue", "next.js", "next ", "frontend",
        "front-end", "ui engineer", "web developer", "tailwind",
    )),
    ("devops", (
        "kubernetes", "k8s", "terraform", "helm", "jenkins",
        "gitlab ci", "github actions", "devops", "sre", "ansible",
        "ci/cd", "argo",
    )),
    ("mobile", (
        "ios ", "android", "swift ", "kotlin", "react native", "flutter",
        "mobile engineer",
    )),
    ("security", (
        "security engineer", "appsec", "pen test", "infosec", "soc2",
        "iam", "vuln", "ciso",
    )),
    ("qa", (
        "qa engineer", "sdet", "test automation", "selenium",
        "cypress", "playwright", "quality assurance",
    )),
    ("backend", (
        "java", ".net", "c#", "golang", "go ", "node.js", "node ",
        "ruby", "spring boot", "spring", "microservice", "fastapi",
        "django", "flask", "backend", "server-side", "api ",
    )),
]


def _hits_in(haystack: str, terms: tuple) -> int:
    return sum(1 for t in terms if t in haystack)


def detect_role_family(
    job_title: str,
    industry: str,
    required_skills: List[Dict[str, Any]],
) -> str:
    """Return one of: it | recruiting | finance | ops | sales | hr |
    marketing | customer_success | generic_non_it.

    Conservative: defaults to `generic_non_it` when nothing matches, so
    non-IT roles never silently route through the IT prompt path.
    """
    title = (job_title or "").lower()
    skill_blob = " ".join(
        (s.get("value") or s.get("name") or "").lower()
        for s in (required_skills or [])
    )
    haystack = f" {title} {industry.lower() if industry else ''} {skill_blob} "

    family_scores: Dict[str, int] = {}
    for family, terms in _NON_IT_FAMILY_RULES:
        h = _hits_in(haystack, terms)
        if h > 0:
            family_scores[family] = h
    if family_scores:
        return max(family_scores.items(), key=lambda kv: kv[1])[0]

    it_title_hit = any(t in title for t in _IT_TITLE_KEYWORDS)
    it_skill_hit = any(_hits_in(haystack, terms) > 0 for _, terms in _IT_DOMAIN_RULES)
    if it_title_hit or it_skill_hit:
        return "it"
    return "generic_non_it"


def detect_it_domain(
    job_title: str,
    required_skills: List[Dict[str, Any]],
    preferred_skills: List[Dict[str, Any]],
) -> str:
    """For IT roles: return `data | backend | frontend | devops | mobile |
    security | qa | generic_it`. Tiebreaker: highest hit count → first
    listed → generic_it."""
    title = (job_title or "").lower()
    skill_blob = " ".join(
        (s.get("value") or s.get("name") or "").lower()
        for s in (required_skills or []) + (preferred_skills or [])
    )
    haystack = f" {title} {skill_blob} "

    best_domain = "generic_it"
    best_hits = 0
    for domain, terms in _IT_DOMAIN_RULES:
        h = _hits_in(haystack, terms)
        if h > best_hits:
            best_hits = h
            best_domain = domain
    return best_domain


# Domain shot bank — one BAD + one GOOD example per IT domain. Injected
# into the prompt at template time so the LLM anchors on role-shape vocab
# instead of the Databricks-only example that broke .NET / React / etc.
_IT_DOMAIN_SHOTS: Dict[str, str] = {
    "data": (
        "BAD:  \"Do you have Databricks experience?\"\n"
        "     GOOD: \"Walk me through how you organized the bronze/silver/gold layers on your\n"
        "            most recent Databricks project. What trade-offs drove using Delta Live\n"
        "            Tables vs raw Structured Streaming for your silver layer?\""
    ),
    "backend": (
        "BAD:  \"Do you have .NET / Java backend experience?\"\n"
        "     GOOD: \"Describe a real production incident in your backend service caused by an\n"
        "            async/await deadlock or thread-pool starvation. What signal led you to it,\n"
        "            and what concrete code change resolved it?\""
    ),
    "frontend": (
        "BAD:  \"Do you have React experience?\"\n"
        "     GOOD: \"Tell me about a component you migrated off useEffect to useMemo or\n"
        "            useSyncExternalStore. What bug forced the change, and how did you verify\n"
        "            the fix didn't regress reconciliation?\""
    ),
    "devops": (
        "BAD:  \"Do you know Kubernetes?\"\n"
        "     GOOD: \"Walk through how you diagnosed a CrashLoopBackOff in a prod Deployment.\n"
        "            What kubectl/log signals led you to root cause, and what was the actual fix —\n"
        "            probe config, image issue, or resource limit?\""
    ),
    "mobile": (
        "BAD:  \"Do you have iOS / Android experience?\"\n"
        "     GOOD: \"Describe a memory leak you found via Instruments / LeakCanary. What\n"
        "            retain cycle or lifecycle bug caused it, and what was the structural fix?\""
    ),
    "security": (
        "BAD:  \"Do you have appsec experience?\"\n"
        "     GOOD: \"Walk through a real OWASP Top-10 finding you triaged. How did you reason\n"
        "            about severity, and what compensating control did you ship before the\n"
        "            permanent fix?\""
    ),
    "qa": (
        "BAD:  \"Do you have test automation experience?\"\n"
        "     GOOD: \"Describe a flaky e2e test you stabilized. What was the root cause class —\n"
        "            timing, shared state, network mock — and how did you assert it stayed fixed?\""
    ),
    "generic_it": (
        "BAD:  \"Tell me about your engineering experience.\"\n"
        "     GOOD: \"Walk me through the most recent production change you owned end to end.\n"
        "            What constraint forced a non-obvious decision, and what was the trade-off?\""
    ),
}

# Non-IT family shot bank — same shape, role-shape-appropriate vocab.
_NON_IT_FAMILY_SHOTS: Dict[str, str] = {
    "recruiting": (
        "BAD:  \"Describe your recruiting experience.\"\n"
        "     GOOD: \"Walk me through the last hard-to-fill role you closed. What sourcing\n"
        "            channel did you abandon and why, and what changed in your outreach to land\n"
        "            the hire?\""
    ),
    "finance": (
        "BAD:  \"Tell me about month-end close.\"\n"
        "     GOOD: \"Describe the most complex variance you investigated last quarter. Which\n"
        "            GL accounts were involved, what was the root cause, and what control did\n"
        "            you change to prevent recurrence?\""
    ),
    "ops": (
        "BAD:  \"Tell me about your ops experience.\"\n"
        "     GOOD: \"Describe a process you redesigned end to end. What was the bottleneck\n"
        "            metric you targeted, and how did you measure improvement after the change?\""
    ),
    "sales": (
        "BAD:  \"Tell me about your sales experience.\"\n"
        "     GOOD: \"Walk me through your largest closed deal in the last 12 months. What\n"
        "            objection nearly killed it, and how did you reframe to close?\""
    ),
    "hr": (
        "BAD:  \"Describe your HR background.\"\n"
        "     GOOD: \"Describe a real employee-relations case you handled. What policy was in\n"
        "            tension, and how did you balance the parties involved?\""
    ),
    "marketing": (
        "BAD:  \"Tell me about your marketing experience.\"\n"
        "     GOOD: \"Describe a campaign you killed. What metric drove the decision, and\n"
        "            where did you redirect the spend?\""
    ),
    "customer_success": (
        "BAD:  \"Tell me about a tough renewal.\"\n"
        "     GOOD: \"Walk through a churn save in the last 6 months. What signal warned you,\n"
        "            what intervention did you run, and did NRR move?\""
    ),
    "generic_non_it": (
        "BAD:  \"Describe your background.\"\n"
        "     GOOD: \"Walk me through your most measurable win in the last year — the metric,\n"
        "            your specific contribution, and what almost went wrong along the way.\""
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
    industry: str,
    required_skills: List[Dict[str, Any]],
    preferred_skills: List[Dict[str, Any]],
    total_years: int,
    target_count: int,
    family: str = "it",
    domain: str = "generic_it",
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

    is_it = family == "it"
    shot_key = _shot_key(family, domain)
    shot_block = (
        _IT_DOMAIN_SHOTS.get(shot_key)
        if is_it
        else _NON_IT_FAMILY_SHOTS.get(shot_key, _NON_IT_FAMILY_SHOTS["generic_non_it"])
    ) or _IT_DOMAIN_SHOTS["generic_it"]
    artifacts = _FAMILY_ARTIFACTS.get(shot_key) or _FAMILY_ARTIFACTS["generic_it"]

    intro = (
        "You are a senior technical recruiter writing screening questions for a live phone screen."
        if is_it
        else "You are an experienced recruiter writing screening questions for a live phone screen."
    )
    rule3 = (
        "Mix question types across the set: ~70% technical-depth (one per must-have skill where\n"
        "   possible) and ~30% architecture / scenario / debugging. NO behavioral, observational,\n"
        "   or 'tell me about a time when' questions. NO 'describe your experience with X'\n"
        "   phrasing. Every question must require concrete technical knowledge — a tool name,\n"
        "   syntax detail, algorithm, configuration choice, failure-mode signal, or trade-off\n"
        "   rationale. For junior seniority: favor factual + debugging questions. For\n"
        "   senior/staff/principal: favor architecture, scaling, and failure-mode questions."
    ) if is_it else (
        "Mix question types across the set: ~50% process/scenario, ~25% stakeholder/communication,\n"
        "   ~25% behavioral/ownership. For junior seniority: favor concrete-task questions. For\n"
        "   senior/manager: favor cross-team decisions, prioritization trade-offs, and measurable\n"
        "   outcomes. AVOID software-delivery jargon (CI/CD, deployment, rollback, architecture,\n"
        "   production systems, release pipelines) — this is not a technical role."
    )
    categories_line = (
        '"category": "technical-depth" | "architecture" | "scenario" | "debugging",'
        if is_it
        else '"category": "process" | "stakeholder" | "behavioral" | "scenario",'
    )
    rubric_anchor_rule = (
        "RUBRIC ANCHORING (IT): Allocate one technical-depth question per must-have skill in\n"
        "   the rubric, in listed order, until you exhaust must-have skills or hit the\n"
        "   technical-depth budget (~70% of the target count). Each anchored question's\n"
        "   `related_skill` MUST exactly match the rubric skill name. Remaining slots fill with\n"
        "   architecture / scenario / debugging questions that combine ≥2 rubric skills."
    ) if is_it else (
        "RUBRIC ANCHORING: Where the rubric lists named tools, processes, or frameworks, ground\n"
        "   each question in one of them and set `related_skill` to the matching rubric value."
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
   in real work and asks something concrete about HOW they used it. Domain example for
   THIS role ({domain if is_it else family}):
     {shot_block}
3. {rubric_anchor_rule}
4. {rule3}
5. Reference specific named concepts, tools, or artifacts where sensible — for THIS
   domain that means: {artifacts}. Do not be generic, and do not pull in concepts from
   unrelated domains.
6. Each question must include a `pass_criteria` — a one-sentence CONCRETE signal the
    recruiter should listen for in the answer. Never ask for years or use wording like
    "N+ years", "X years of experience", "minimum years", or similar duration thresholds.
7. Questions must be answerable in under 90 seconds each during a phone screen.
8. Do not repeat or paraphrase the same question.
9. Return nothing except the JSON array below.

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

    # B2/B3: classify the role so the prompt + fallback emit role-shape-
    # appropriate questions instead of forcing every role through the
    # legacy Databricks-flavored template.
    family = detect_role_family(job_title, industry, required_skills)
    domain = detect_it_domain(job_title, required_skills, preferred_skills) if family == "it" else "generic_it"

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
        family=family,
        domain=domain,
    )

    is_it_role = family == "it"
    system_message = (
        "You write sharp, specific TECHNICAL screening questions that separate real practitioners "
        "from surface-level candidates. You avoid generic 'describe your experience' phrasing. "
        "You NEVER produce behavioral or 'tell me about a time when' questions for IT roles — "
        "every question must be answerable only by someone who has hands-on coded, configured, "
        "or operated the tool, naming a concrete syntax detail, configuration knob, failure "
        "signal, or trade-off. You always return strict JSON."
    ) if is_it_role else (
        "You write sharp, role-relevant screening questions for non-technical roles. "
        "You avoid software-delivery jargon (CI/CD, deployment, rollback, architecture, "
        "production systems, release pipelines) and ground questions in stakeholder, "
        "process, and outcome language. You always return strict JSON."
    )

    role_specific: List[Dict[str, Any]] = []
    try:
        completion = await openai_client.chat.completions.create(
            model=model or "gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_message},
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
        # Fall back to deterministic per-skill templates — level-aware,
        # family-aware, and explicitly free of years-of-experience phrasing.
        fallback: List[Dict[str, Any]] = []
        focus_skills = required_skills or preferred_skills
        if not focus_skills:
            focus_skills = [{"value": "core role responsibilities"}]

        level = (screening_level or "").strip().lower()
        for idx in range(target_count):
            skill = focus_skills[idx % len(focus_skills)]
            name = skill.get("value") or skill.get("name") or (
                "this technology" if is_it_role else "this area"
            )
            if is_it_role:
                if level in ("intensive", "deep", "extensive", "high"):
                    q_text = (
                        f"In a production system using {name}, what specific failure-mode signal "
                        "(metric, log line, or error class) led you to root cause, and which exact "
                        "configuration or code change prevented recurrence?"
                    )
                    criteria = (
                        f"Candidate names a concrete {name} signal, root cause, and the precise "
                        "configuration knob, code path, or design change that fixed it."
                    )
                    category = "debugging"
                elif level in ("light", "low", "basic", "quick"):
                    q_text = (
                        f"Name one specific configuration, syntax detail, or API in {name} that "
                        "you've tuned or used directly, and what observable behavior changed."
                    )
                    criteria = (
                        f"Candidate names a real {name} flag/API/syntax detail and ties it to a "
                        "concrete, verifiable behavior change — not a generic 'we used it for X'."
                    )
                    category = "technical-depth"
                else:
                    q_text = (
                        f"Walk through one concrete implementation choice you made with {name} — "
                        "what specific alternative did you reject, and what technical trade-off "
                        "(latency, consistency, throughput, cost) drove the decision?"
                    )
                    criteria = (
                        f"Candidate identifies a specific {name} implementation choice, names the "
                        "rejected alternative, and articulates a concrete technical trade-off."
                    )
                    category = "technical-depth"
            else:
                # Non-IT family fallback — stakeholder/process/outcome wording,
                # no production/architecture jargon.
                if level in ("intensive", "deep", "extensive", "high"):
                    q_text = (
                        f"Describe a real situation where {name} drove a measurable outcome. "
                        "What was the decision, who were the stakeholders, and what trade-off did you make?"
                    )
                    criteria = (
                        f"Candidate names specific stakeholders, a concrete decision, and a measurable result tied to {name}."
                    )
                    category = "scenario"
                elif level in ("light", "low", "basic", "quick"):
                    q_text = (
                        f"What's one recent task involving {name} where you owned the outcome? "
                        "What changed because of your work?"
                    )
                    criteria = (
                        f"Candidate gives a specific {name} example with clear ownership and a concrete change in outcome."
                    )
                    category = "process"
                else:
                    q_text = (
                        f"Walk me through a recent piece of work involving {name}: who did you coordinate with, "
                        "what trade-off did you make, and what was the result?"
                    )
                    criteria = (
                        f"Candidate explains a concrete {name} situation with stakeholders, a trade-off, and a measurable outcome."
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
