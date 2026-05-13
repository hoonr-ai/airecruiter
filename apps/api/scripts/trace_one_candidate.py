"""Trace one specific candidate through the scoring/filter functions.

Compares the *abstract competency* rubric the LLM extractor produced for
job 26-14018 (Respiratory Therapist) against a *concrete keyword* rubric
written by hand. Same candidate, same code path — only the rubric changes.

Goal: isolate whether the bug is (a) rubric quality from the JD extractor,
or (b) the fuzzy term matcher itself.

Usage:
    cd apps/api
    set -a && source .env && set +a
    venv/bin/python -m scripts.trace_one_candidate
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))


TARGET_CANDIDATE_ID = "18632869895480"  # CHRIS GHORY, Staff Respiratory Therapist, Cincinnati OH
RAW_JSON = (
    APPS_API_DIR
    / "tmp/sourcing_poc/26-14018/baseline_jobagent/01_raw_jobdiva.json"
)


async def main() -> int:
    from services.unified_candidate_search import UnifiedCandidateSearch, SearchCriteria
    from services.sourced_candidates_storage import process_jobdiva_candidate

    raw = json.loads(RAW_JSON.read_text())
    candidates = raw.get("candidates_preview") or []
    cand = next(
        (c for c in candidates if str(c.get("candidate_id")) == TARGET_CANDIDATE_ID),
        None,
    )
    if not cand:
        print(f"✗ candidate {TARGET_CANDIDATE_ID} not found in raw dump", file=sys.stderr)
        return 1

    print("=" * 72)
    print(f"Candidate: {cand.get('name')} ({TARGET_CANDIDATE_ID})")
    print(f"  title:    {cand.get('title')}")
    print(f"  city/st:  {cand.get('city')}, {cand.get('state')}")
    print(f"  yoe:      {cand.get('experience_years')}")
    print(f"  resume:   {len(cand.get('resume_text') or '')} chars (raw JobAgent)")
    print("=" * 72)

    from services.jobdiva import JobDivaService

    jd = JobDivaService()
    if not (cand.get("resume_text") and "Resume content unavailable" not in cand.get("resume_text", "")):
        print("  fetching resume via JobDiva CandidatesDetail...")
        rd = await jd.get_candidate_resume(
            TARGET_CANDIDATE_ID, resume_id=cand.get("resume_id"),
        )
        if rd and rd.get("resume_text"):
            cand["resume_text"] = rd["resume_text"]
            cand["resume_id"] = rd.get("resume_id") or cand.get("resume_id")
            for k in ("email", "phone", "title", "location"):
                if not cand.get(k) and rd.get(k):
                    cand[k] = rd[k]
            print(f"  ✓ resume fetched: {len(cand['resume_text'])} chars")
        else:
            print(f"  ✗ resume fetch returned nothing")

    enhanced = await process_jobdiva_candidate(cand)
    if isinstance(enhanced, dict) and enhanced is not cand:
        cand["enhanced_info"] = enhanced.get("raw", enhanced)
    else:
        cand["enhanced_info"] = {}
    ei = cand["enhanced_info"]
    cand["name"] = ei.get("candidate_name") or cand.get("name")
    cand["title"] = ei.get("job_title") or cand.get("title")
    cand["location"] = ei.get("current_location") or cand.get("location")
    cand["experience_years"] = ei.get("years_of_experience") or cand.get("experience_years")
    if ei.get("structured_skills") or ei.get("skills"):
        cand["skills"] = ei.get("structured_skills") or ei.get("skills")

    llm_skills = ei.get("structured_skills") or ei.get("skills") or []
    print("\n── LLM-extracted skills from resume ──")
    for s in llm_skills[:30]:
        if isinstance(s, dict):
            print(f"  • {s.get('name') or s.get('value') or s}")
        else:
            print(f"  • {s}")
    print(f"  (total: {len(llm_skills)})")

    print(f"\n  LLM-extracted YOE: {ei.get('years_of_experience')}")
    print(f"  LLM-extracted location: {ei.get('current_location')}")

    abstract_skills = [
        "Critical Care Knowledge",
        "Invasive and Noninvasive Procedures",
        "RRT Credentials",
        "Documentation Skills",
        "Patient Assessment",
        "Patient Education",
    ]
    concrete_skills = [
        "BLS",
        "ACLS",
        "PALS",
        "NRP",
        "CCRN",
        "RRT",
        "Respiratory Therapist",
        "Ventilator",
        "ABG",
        "Intubation",
        "CPAP",
        "BiPAP",
    ]

    def build_criteria(skill_list):
        return SearchCriteria(
            job_id="26-14018",
            title_criteria=[
                {"value": "Respiratory Therapist", "match_type": "required"}
            ],
            skill_criteria=[
                {"value": s, "match_type": "required", "min_years": 1} for s in skill_list
            ],
            location="Cincinnati, OH",
            within_miles=50,
            countries=["US"],
            states=["OH"],
            min_experience_years=1,
        )

    service = UnifiedCandidateSearch()
    service._current_family = service._resolve_search_family(build_criteria(concrete_skills))
    print(f"\n  Detected role family: {service._current_family}")

    for label, skills in [("ABSTRACT (LLM-extracted)", abstract_skills), ("CONCRETE (hand-written)", concrete_skills)]:
        crit = build_criteria(skills)
        score_res = service._score_candidate(cand, crit)
        assess = service._filter_assessment(cand, crit, enforce_years=True)
        print(f"\n── {label} ──")
        print(f"  rubric: {skills}")
        print(f"  score: {score_res.get('score')}")
        print(f"  passes_filter: {assess.get('passes')}")
        print(f"  matched: {assess.get('matched')[:10]}")
        print(f"  missing: {assess.get('missing')[:10]}")
        if assess.get("location_failure_reason"):
            print(f"  location_failure_reason: {assess['location_failure_reason']}")
        details = score_res.get("score_details") or {}
        print(f"  per-dim scores:")
        for dim_name, dim_data in details.items():
            if isinstance(dim_data, dict):
                sc = dim_data.get("score")
                sc_str = f"{sc:.2f}" if isinstance(sc, (int, float)) else str(sc)
                req_m = dim_data.get("required_matched", "?")
                req_t = dim_data.get("required_total", "?")
                print(f"    - {dim_name}: score={sc_str} matched={req_m}/{req_t}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
