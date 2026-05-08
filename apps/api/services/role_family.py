"""
role_family.py
--------------
Shared role-family + IT-domain detection.

Used by:
  - screening_question_generator (chooses prompt + artifact bank)
  - taxonomy_service (chooses which blacklist subset applies)
  - job_skills_extractor (decides whether to run a non-IT second-pass)
  - unified_candidate_search (chooses scoring weight set)

The single source of truth — keep all family/keyword tables here so the
classifier the screening prompt sees is the same one the scorer sees.

IT-invariant: pure-IT titles (Software Engineer, Backend Developer,
DevOps Engineer, Data Scientist, Frontend Architect, ML Engineer,
Cloud Architect) classify identically to the legacy
screening_question_generator implementation. Mixed-signal titles like
"Technical Program Manager" or "Sales Engineer" — which the old
classifier wrongly flipped to `it` — now route to their explicit
non-IT family.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


_IT_TITLE_KEYWORDS = (
    "engineer", "developer", "architect", "devops", "sre", "site reliability",
    "data engineer", "data scientist", "machine learning", "ai engineer",
    "cloud", "programmer", "full stack", "backend", "frontend", "qa automation",
    "platform", "security engineer", "database", "etl", "analytics engineer",
    "ios", "android",
)


# Order matters — earlier families take precedence on ambiguous titles
# (e.g. "Healthcare Recruiter" should classify as recruiting, not
# healthcare). This mirrors how the original list ordered recruiting
# first.
_NON_IT_FAMILY_RULES: List[tuple] = [
    ("recruiting", (
        "recruiter", "talent acquisition", "sourcer", "headhunter",
        "ta partner",
    )),
    ("program_management", (
        "program manager", "project manager", "pmo", "tpm",
        "technical program manager", "scrum master", "delivery manager",
        "release manager", "chief of staff",
    )),
    ("accounting", (
        "cpa", "bookkeep", "auditor", "controller",
    )),
    ("finance", (
        "accountant", "fp&a", "treasury", "cfo", "audit",
        "gaap", "ifrs", "payroll",
    )),
    ("ops", (
        "operations manager", "coo", "ops lead", "supply chain",
        "logistics", "procurement", "vendor management", "operations",
    )),
    ("sales", (
        "account executive", "ae ", "bdr", "sdr", "sales development",
        "sales rep", "salesperson", "sales manager", "sales director",
        "sales engineer", "vp sales", "vp of sales",
    )),
    ("hr", (
        "hrbp", "hr business partner", "hr manager", "people partner",
        "chro", "benefits", "hris", "human resources",
    )),
    ("marketing", (
        "marketing manager", "demand gen", "content marketing", "seo",
        "brand manager", "growth marketing", "marketing director",
    )),
    ("customer_success", (
        "customer success", "csm", "renewal manager",
    )),
    ("healthcare", (
        "nurse", "nursing", "rn ", "lpn", "physician", "clinical",
        "paramedic", "technologist", "radiology technician", "phlebotomist",
        "respiratory therapist", "medical assistant",
    )),
    ("legal", (
        "attorney", "paralegal", "counsel", "compliance officer",
        "contracts manager", "general counsel",
    )),
    ("education", (
        "teacher", "instructor", "professor", "curriculum",
        "principal of school",
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


# Families recognized by the rest of the pipeline. Useful when callers
# need to validate a family string (e.g. config lookups).
KNOWN_FAMILIES = (
    "it",
    "recruiting",
    "program_management",
    "accounting",
    "finance",
    "ops",
    "sales",
    "hr",
    "marketing",
    "customer_success",
    "healthcare",
    "legal",
    "education",
    "generic_non_it",
)


def _hits_in(haystack: str, terms: tuple) -> int:
    return sum(1 for t in terms if t in haystack)


def detect_role_family(
    job_title: str,
    industry: str = "",
    required_skills: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Return one of KNOWN_FAMILIES.

    Tightened tiebreaker: IT wins outright when the title contains an
    IT keyword AND no non-IT family rule fires (preserves legacy IT
    behavior). When both fire — e.g. "Technical Program Manager" hits
    both `engineer`-adjacent and `program manager` — IT only wins when
    its weighted score is at least 2x the strongest non-IT family
    signal. This stops mixed-signal titles flipping to IT and asking
    engineering screening questions.
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

    it_title_hits = sum(1 for t in _IT_TITLE_KEYWORDS if t in title)
    it_skill_hits = sum(_hits_in(haystack, terms) for _, terms in _IT_DOMAIN_RULES)
    # Title evidence is stronger than skill evidence: a JD titled
    # "Software Engineer" is unambiguously IT; one merely mentioning
    # "Java" might be a non-tech role using a tool name.
    it_score = it_title_hits * 3 + it_skill_hits

    top_non_it_score = max(family_scores.values()) if family_scores else 0

    # Pure-IT path: IT keyword in title AND no non-IT signal → IT
    # (identical to legacy behavior on every clean IT title).
    if it_title_hits > 0 and top_non_it_score == 0:
        return "it"

    # Mixed-signal path: IT and non-IT both fired. IT only wins if it
    # dominates 2:1 — otherwise defer to the non-IT family rule that
    # was almost certainly the recruiter's actual intent.
    if it_title_hits > 0 and it_score >= 2 * top_non_it_score:
        return "it"

    if family_scores:
        return max(family_scores.items(), key=lambda kv: kv[1])[0]

    if it_score > 0:
        return "it"
    return "generic_non_it"


def detect_it_domain(
    job_title: str,
    required_skills: Optional[List[Dict[str, Any]]] = None,
    preferred_skills: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """For IT roles: return one of `data | backend | frontend | devops |
    mobile | security | qa | generic_it`. Tiebreaker: highest hit count
    → first listed → generic_it."""
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
