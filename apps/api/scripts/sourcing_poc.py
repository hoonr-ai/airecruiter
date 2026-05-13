"""Sourcing-quality POC: compare JobDiva search variants + filter pipeline.

Runs one or more JobDiva job_ids through several search variants (JobAgent,
TalentSearch with the current translator, TalentSearch with hand-crafted
booleans). For each variant the raw JobDiva response is dumped, then the
existing UnifiedCandidateSearch pipeline (filter → pre-LLM gates → live
OpenAI extraction → post-LLM filter → scoring) is replayed with full
per-candidate tracing. Output goes to a run directory you read by hand.

This script writes NO production code changes and queries only — it is a
diagnostic tool that imports services from apps/api and instruments them
via a TracingUnifiedCandidateSearch subclass.

Run:
    cd apps/api
    venv/bin/python -m scripts.sourcing_poc \
        --jobs scripts/sourcing_poc_jobs.json \
        --variants scripts/sourcing_poc_variants.json \
        --out tmp/sourcing_poc

For each (job × variant) the orchestrator spawns a subprocess so env-var
knobs (e.g. SCORING_PARSING_GAP_FLOOR, EMBEDDING_SKILL_MATCH) take effect
at module import time. The subprocess is invoked with `--internal-run`.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Path setup — make apps/api importable when invoked as a script.
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))


# ---------------------------------------------------------------------------
# Config file loading + criteria construction
# ---------------------------------------------------------------------------
def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r") as f:
        return json.load(f)


def build_search_criteria(job: Dict[str, Any]):
    """Convert a `jobs.json` entry into a SearchCriteria the pipeline expects."""
    from services.unified_candidate_search import SearchCriteria

    rubric = job.get("rubric") or {}
    skill_criteria: List[Dict[str, Any]] = []
    skill_years: Dict[str, int] = rubric.get("skill_years") or {}
    for skill in rubric.get("skills") or []:
        entry: Dict[str, Any] = {"value": skill, "match_type": "required"}
        if skill in skill_years:
            entry["min_years"] = int(skill_years[skill])
        skill_criteria.append(entry)

    title_criteria: List[Dict[str, Any]] = []
    titles = rubric.get("titles") or ([rubric["title"]] if rubric.get("title") else [])
    for title in titles:
        title_criteria.append({"value": title, "match_type": "required"})

    return SearchCriteria(
        job_id=str(job["job_id"]),
        title_criteria=title_criteria,
        skill_criteria=skill_criteria,
        keywords=rubric.get("keywords") or [],
        location=rubric.get("location") or "",
        within_miles=int(rubric.get("within_miles") or 25),
        countries=rubric.get("countries") or [],
        states=rubric.get("states") or [],
        min_experience_years=rubric.get("min_experience_years"),
        sources=["JobDiva", "JobDiva-TalentSearch"],
    )


def apply_rubric_override(job: Dict[str, Any], variant: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow-merged job dict with `variant.rubric_override` applied."""
    override = variant.get("rubric_override")
    if not override:
        return job
    merged = dict(job)
    merged_rubric = dict(job.get("rubric") or {})
    merged_rubric.update(override)
    merged["rubric"] = merged_rubric
    return merged


async def fetch_rubric_from_jobdiva(job_id: str) -> Dict[str, Any]:
    """Fetch title/location/description from JobDiva and run the LLM rubric extractor.

    Returns a `rubric` dict in the POC's schema (title, titles, skills,
    skill_years, location, within_miles, min_experience_years, countries,
    states, keywords). Raises on failure so caller can surface the error.
    """
    from services.jobdiva import JobDivaService
    from services.job_skills_extractor import JobSkillsExtractor
    from core.config import OPENAI_API_KEY

    jd = JobDivaService()
    job_detail = await jd.get_job_by_id(job_id)
    if not job_detail:
        raise RuntimeError(f"JobDiva returned no job for id={job_id}")

    job_title = str(job_detail.get("title") or "").strip()
    city = str(job_detail.get("city") or "").strip()
    state = str(job_detail.get("state") or "").strip()
    location_str = ", ".join([p for p in [city, state] if p]).strip(", ")
    description = str(job_detail.get("description") or job_detail.get("jobdiva_description") or "")
    ai_description = str(job_detail.get("ai_description") or "")
    customer = str(job_detail.get("customer_name") or "")

    extractor = JobSkillsExtractor(openai_api_key=OPENAI_API_KEY)
    rubric_obj = await extractor.extract_full_rubric(
        job_id=str(job_id),
        job_title=job_title,
        enhanced_job_title=job_title,
        jobdiva_description=description,
        ai_description=ai_description,
        recruiter_notes="",
        customer_name=customer,
        job_location=location_str,
        location_type=str(job_detail.get("location_type") or "Onsite"),
    )

    hard_skills = list(rubric_obj.hard_skills or [])
    titles_field = list(rubric_obj.titles or [])
    titles = [
        str(t.get("value") if isinstance(t, dict) else t).strip()
        for t in titles_field
    ]
    titles = [t for t in titles if t] or ([job_title] if job_title else [])

    skills_values: List[str] = []
    skill_years: Dict[str, int] = {}
    for s in hard_skills:
        v = str(s.get("value") or "").strip()
        if not v:
            continue
        skills_values.append(v)
        my = int(s.get("minYears") or s.get("min_years") or 0)
        if my > 0:
            skill_years[v] = my

    min_years = 0
    for s in hard_skills:
        if str(s.get("importance") or "").lower() == "required":
            my = int(s.get("minYears") or s.get("min_years") or 0)
            if my > min_years:
                min_years = my

    rubric = {
        "title": job_title or (titles[0] if titles else ""),
        "titles": titles,
        "skills": skills_values,
        "skill_years": skill_years,
        "keywords": [],
        "location": location_str,
        "within_miles": 50,
        "min_experience_years": min_years or None,
        "countries": ["US"],
        "states": [state] if state else [],
        "_source": "jobdiva+llm_extractor",
        "_jobdiva_customer": customer,
        "_jobdiva_description_chars": len(description),
        "_jobdiva_location_type": job_detail.get("location_type"),
    }
    return rubric


async def ensure_rubric(job: Dict[str, Any], cache_path: Path) -> Dict[str, Any]:
    """If job has no usable rubric, fetch one from JobDiva (cached on disk).

    Cache is keyed per job_id under `cache_path`. Re-runs reuse the cached
    rubric to avoid re-paying for the LLM extraction.
    """
    rubric = job.get("rubric") or {}
    has_skills = bool(rubric.get("skills"))
    if has_skills:
        return job

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        merged = dict(job)
        merged_rubric = dict(rubric)
        for k, v in cached.items():
            if k not in merged_rubric or merged_rubric.get(k) in (None, "", [], {}):
                merged_rubric[k] = v
        merged["rubric"] = merged_rubric
        print(f"  ✓ reused cached rubric for job {job['job_id']} from {cache_path}")
        return merged

    print(f"  ⋯ fetching rubric for job {job['job_id']} from JobDiva + LLM (one-time)...")
    fetched = await fetch_rubric_from_jobdiva(str(job["job_id"]))
    cache_path.write_text(json.dumps(fetched, indent=2, default=str))
    merged = dict(job)
    merged_rubric = dict(rubric)
    for k, v in fetched.items():
        if k not in merged_rubric or merged_rubric.get(k) in (None, "", [], {}):
            merged_rubric[k] = v
    merged["rubric"] = merged_rubric
    print(f"  ✓ extracted {len(fetched.get('skills', []))} skill(s); cached to {cache_path}")
    return merged


# ---------------------------------------------------------------------------
# Tracing pipeline — instruments the existing UnifiedCandidateSearch.
# ---------------------------------------------------------------------------
async def run_variant_pipeline(
    job: Dict[str, Any],
    variant: Dict[str, Any],
    out_dir: Path,
) -> Dict[str, Any]:
    """Execute one (job × variant) end-to-end and write trace outputs.

    Returns a small summary dict the orchestrator collects for compare.md.
    """
    from services.jobdiva import JobDivaService
    from services.unified_candidate_search import UnifiedCandidateSearch
    from services.sourced_candidates_storage import process_jobdiva_candidate
    from core.config import SOURCE_TIER_BONUS

    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / "pipeline_trace.jsonl"
    trace_fh = trace_path.open("w")

    def trace(event_type: str, payload: Dict[str, Any]) -> None:
        trace_fh.write(json.dumps({"t": event_type, **payload}, default=str) + "\n")

    pre_llm_low = float(os.environ.get("POC_PRE_LLM_LOW_SCORE", "25"))
    pre_llm_mid = float(os.environ.get("POC_PRE_LLM_MID_SCORE", "40"))

    criteria = build_search_criteria(job)
    service = UnifiedCandidateSearch()
    jobdiva = service.jobdiva_service

    # ----- 1. Raw JobDiva fetch -----
    endpoint = variant.get("endpoint", "jobagent")
    t0 = time.time()
    raw_candidates: List[Dict[str, Any]] = []
    raw_meta: Dict[str, Any] = {"endpoint": endpoint}

    token = await jobdiva.authenticate()
    if not token:
        print(f"  ✗ JobDiva auth failed", file=sys.stderr)
        trace_fh.close()
        return {"variant": variant["name"], "error": "auth_failed"}

    if endpoint == "jobagent":
        ja_res = await jobdiva.search_via_job_agent(
            job_id=str(job["job_id"]),
            resume_count=int(variant.get("resume_count", 200)),
            require_resume=True,
        )
        raw_candidates = list(ja_res.get("candidates") or [])
        raw_meta["criteria_unconfigured"] = bool(ja_res.get("criteria_unconfigured"))
        raw_meta["resolved_jobdiva_id"] = ja_res.get("resolved_jobdiva_id")
    elif endpoint == "talentsearch_current":
        # Reproduces production: lets _build_boolean_string + translator run.
        bool_str = service._build_boolean_string(criteria)
        raw_meta["boolean_string"] = bool_str
        raw_candidates = await jobdiva._search_talent_pool(
            skills=[s for s in criteria.skill_criteria],
            location=criteria.location,
            limit=int(variant.get("limit", 200)),
            token=token,
            boolean_string=bool_str,
            recent_days=criteria.recent_days,
            require_resume=True,
            countries=criteria.countries or [],
            states=criteria.states or [],
            page_number=0,
        )
    elif endpoint == "talentsearch_custom":
        bool_str = variant.get("boolean") or ""
        raw_meta["boolean_string"] = bool_str
        raw_candidates = await jobdiva._search_talent_pool(
            skills=[],  # bypass translator-added OVER clauses
            location=variant.get("location") or criteria.location,
            limit=int(variant.get("limit", 200)),
            token=token,
            boolean_string=bool_str,
            recent_days=None,
            require_resume=True,
            countries=variant.get("countries") or criteria.countries or [],
            states=variant.get("states") or criteria.states or [],
            page_number=0,
        )
    else:
        print(f"  ✗ Unknown endpoint: {endpoint}", file=sys.stderr)
        trace_fh.close()
        return {"variant": variant["name"], "error": f"unknown_endpoint:{endpoint}"}

    raw_meta["count"] = len(raw_candidates)
    raw_meta["fetch_seconds"] = round(time.time() - t0, 2)
    raw_meta["candidate_ids"] = [str(c.get("candidate_id") or c.get("id") or "") for c in raw_candidates]
    print(f"  ✓ JobDiva {endpoint}: {len(raw_candidates)} raw candidates in {raw_meta['fetch_seconds']}s")

    with (out_dir / "01_raw_jobdiva.json").open("w") as f:
        # First 100 for the file; full id list lives in raw_meta.candidate_ids.
        json.dump(
            {
                "meta": raw_meta,
                "candidates_preview": raw_candidates[:100],
            },
            f,
            indent=2,
            default=str,
        )

    if not raw_candidates:
        trace_fh.close()
        return {
            "variant": variant["name"],
            "endpoint": endpoint,
            "raw_count": 0,
            "survived_s1": 0,
            "reached_ui": 0,
            "mean_score": 0,
            "median_score": 0,
            "candidate_ids": [],
        }

    # ----- 2. Resolve family (so embedding flag check works inside _score_candidate) -----
    service._current_family = service._resolve_search_family(criteria)
    trace("family", {"family": service._current_family})

    # ----- 3. Stage 1: _filter_candidates (pre-enrichment SummaryScreen) -----
    source_type = "applicants" if endpoint == "jobagent" else "talent_search"
    before_s1 = len(raw_candidates)
    after_s1 = service._filter_candidates(raw_candidates, criteria, source_type=source_type)
    s1_dropped = {c.get("candidate_id") or c.get("id"): "stage1_summary_screen" for c in raw_candidates if c not in after_s1}
    trace("stage1", {"in": before_s1, "out": len(after_s1)})

    # ----- 4. Per-candidate: replay _enrich_filtered_jobdiva_candidates
    #         with configurable thresholds + full instrumentation. ------------
    per_candidate: Dict[str, Dict[str, Any]] = {}
    for c in raw_candidates:
        cid = str(c.get("candidate_id") or c.get("id") or "")
        per_candidate[cid] = {
            "candidate_id": cid,
            "name": c.get("name") or "",
            "location": c.get("location") or f"{c.get('city','')}, {c.get('state','')}".strip(", "),
            "source": c.get("source") or "",
            "survived_to_stage": 0,
            "drop_reason": s1_dropped.get(cid),
            "pre_llm_score": None,
            "final_score": None,
            "source_tier_bonus": 0,
            "top_skill_matches": "",
            "top_skill_misses": "",
            "llm_extracted_yoe": None,
        }
        if cid in s1_dropped:
            trace("drop", {"cid": cid, "stage": 1, "reason": "summary_screen"})

    stage_counts = {
        "in": before_s1,
        "after_stage1": len(after_s1),
        "after_stage2_yoe": 0,
        "after_stage3_filter_assessment_pre": 0,
        "after_stage3_pre_llm_score_gate": 0,
        "after_stage4_llm": 0,
        "after_stage5_filter_assessment_post": 0,
        "after_stage6_scored": 0,
        "drops": {
            "stage1_summary_screen": before_s1 - len(after_s1),
            "stage2_min_years_pre_llm": 0,
            "stage3a_filter_assessment_failed": 0,
            "stage3a_filter_assessment_location": 0,
            "stage3b_pre_llm_low_score": 0,
            "stage3b_pre_llm_no_required_hit": 0,
            "stage4_llm_error": 0,
            "stage4_no_resume": 0,
            "stage5_post_filter_failed": 0,
            "stage5_post_filter_location": 0,
        },
    }

    # Pre-warm query-side embeddings (mirrors what search_candidates() does).
    try:
        from services import skill_embeddings
        from core.config import embedding_skill_match_for_family

        if embedding_skill_match_for_family(service._current_family):
            query_terms = service._criteria_query_terms(criteria)
            if query_terms:
                await skill_embeddings.warm_terms(query_terms)
    except Exception as e:
        trace("warn", {"step": "embedding_warm", "error": str(e)})

    survivors: List[Dict[str, Any]] = []
    semaphore = asyncio.Semaphore(int(os.environ.get("LLM_CONCURRENCY", "5")))

    async def trace_one(cand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with semaphore:
            cid = str(cand.get("candidate_id") or cand.get("id") or "")
            row = per_candidate.get(cid, {})

            # Stage 2: pre-LLM YOE heuristic.
            try:
                if service._candidate_below_min_years_pre_llm(cand, criteria):
                    row["survived_to_stage"] = 1
                    row["drop_reason"] = "stage2_min_years_pre_llm"
                    stage_counts["drops"]["stage2_min_years_pre_llm"] += 1
                    trace("drop", {"cid": cid, "stage": 2, "reason": "min_years_pre_llm"})
                    return None
            except Exception as e:
                trace("warn", {"cid": cid, "step": "yoe_pre_llm", "error": str(e)})

            # Stage 3a: pre-LLM filter assessment (enforce_years=False).
            assessment = service._filter_assessment(cand, criteria, enforce_years=False)
            if not assessment.get("passes"):
                row["survived_to_stage"] = 2
                reason = assessment.get("location_failure_reason")
                if reason:
                    row["drop_reason"] = f"stage3a_filter_assessment_location:{reason}"
                    stage_counts["drops"]["stage3a_filter_assessment_location"] += 1
                else:
                    row["drop_reason"] = "stage3a_filter_assessment_failed"
                    stage_counts["drops"]["stage3a_filter_assessment_failed"] += 1
                trace("drop", {
                    "cid": cid, "stage": "3a", "reason": row["drop_reason"],
                    "missing": assessment.get("missing", [])[:5],
                    "excluded": assessment.get("excluded", [])[:5],
                })
                return None

            # Stage 3b: pre-LLM scoring gate (configurable thresholds).
            pre_result = service._score_candidate(cand, criteria)
            pre_score = float(pre_result.get("score") or 0)
            row["pre_llm_score"] = pre_score
            score_details = pre_result.get("score_details") or {}
            has_required_hit = any(
                isinstance(dim, dict)
                and int(dim.get("required_total") or 0) > 0
                and int(dim.get("required_matched") or 0) > 0
                for dim in score_details.values()
            )

            if pre_score < pre_llm_low:
                row["survived_to_stage"] = 3
                row["drop_reason"] = "stage3b_pre_llm_low_score"
                stage_counts["drops"]["stage3b_pre_llm_low_score"] += 1
                trace("drop", {"cid": cid, "stage": "3b", "reason": "low_score", "pre_score": pre_score})
                return None
            if pre_score < pre_llm_mid and not has_required_hit:
                row["survived_to_stage"] = 3
                row["drop_reason"] = "stage3b_pre_llm_no_required_hit"
                stage_counts["drops"]["stage3b_pre_llm_no_required_hit"] += 1
                trace("drop", {"cid": cid, "stage": "3b", "reason": "no_required_hit", "pre_score": pre_score})
                return None

            # Stage 4: LLM extraction (live OpenAI calls; cache-hits possible).
            # Fetch resume if missing.
            resume_text = cand.get("resume_text") or ""
            if not resume_text or "Resume content unavailable" in resume_text:
                try:
                    rd = await jobdiva.get_candidate_resume(
                        cid, resume_id=cand.get("resume_id"),
                    )
                    if rd and rd.get("resume_text"):
                        cand["resume_text"] = rd["resume_text"]
                        cand["resume_id"] = rd.get("resume_id") or cand.get("resume_id")
                        cand.setdefault("email", rd.get("email") or "")
                        cand.setdefault("phone", rd.get("phone") or "")
                        cand.setdefault("title", rd.get("title") or "")
                        cand.setdefault("location", rd.get("location") or "")
                except Exception as e:
                    trace("warn", {"cid": cid, "step": "fetch_resume", "error": str(e)})

            if not cand.get("resume_text"):
                row["survived_to_stage"] = 3
                row["drop_reason"] = "stage4_no_resume"
                stage_counts["drops"]["stage4_no_resume"] += 1
                trace("drop", {"cid": cid, "stage": 4, "reason": "no_resume"})
                return None

            try:
                enhanced = await process_jobdiva_candidate(cand)
                if isinstance(enhanced, dict) and enhanced is not cand:
                    cand["enhanced_info"] = enhanced.get("raw", enhanced)
                else:
                    cand["enhanced_info"] = {}
                ei = cand["enhanced_info"]
                if isinstance(ei, dict):
                    cand["name"] = ei.get("candidate_name") or cand.get("name")
                    cand["email"] = ei.get("email") or cand.get("email")
                    cand["phone"] = ei.get("phone") or cand.get("phone")
                    cand["title"] = ei.get("job_title") or cand.get("title")
                    cand["location"] = ei.get("current_location") or cand.get("location")
                    cand["education"] = ei.get("candidate_education", [])
                    cand["certifications"] = ei.get("candidate_certification", [])
                    cand["urls"] = ei.get("urls", {})
                    cand["experience_years"] = ei.get("years_of_experience") or cand.get("experience_years")
                    if ei.get("structured_skills") or ei.get("skills"):
                        cand["skills"] = ei.get("structured_skills") or ei.get("skills")
                    row["llm_extracted_yoe"] = ei.get("years_of_experience")
                    if ei.get("_extraction_error"):
                        stage_counts["drops"]["stage4_llm_error"] += 1
                        trace("warn", {"cid": cid, "step": "llm", "error": ei["_extraction_error"]})
            except Exception as e:
                row["survived_to_stage"] = 3
                row["drop_reason"] = "stage4_llm_exception"
                trace("warn", {"cid": cid, "step": "llm_exception", "error": str(e)})
                return None

            # Stage 5: post-LLM filter assessment (enforce_years=True).
            assessment2 = service._filter_assessment(cand, criteria, enforce_years=True)
            if not assessment2.get("passes"):
                row["survived_to_stage"] = 4
                reason = assessment2.get("location_failure_reason")
                if reason:
                    row["drop_reason"] = f"stage5_post_filter_location:{reason}"
                    stage_counts["drops"]["stage5_post_filter_location"] += 1
                else:
                    row["drop_reason"] = "stage5_post_filter_failed"
                    stage_counts["drops"]["stage5_post_filter_failed"] += 1
                trace("drop", {
                    "cid": cid, "stage": 5, "reason": row["drop_reason"],
                    "missing": assessment2.get("missing", [])[:5],
                })
                return None

            # Stage 6: final scoring + source-tier bonus.
            final_result = service._score_candidate(cand, criteria)
            base_score = float(final_result.get("score") or 0)
            bonus = int(SOURCE_TIER_BONUS.get(cand.get("source") or "", 0) or 0)
            final_score = min(100, base_score + bonus) if base_score > 0 else base_score
            row["final_score"] = final_score
            row["source_tier_bonus"] = bonus if base_score > 0 else 0
            row["survived_to_stage"] = 6
            row["drop_reason"] = None

            matched = final_result.get("matched_skills") or []
            missing = final_result.get("missing_skills") or []
            row["top_skill_matches"] = "; ".join(str(m) for m in matched[:5])
            row["top_skill_misses"] = "; ".join(str(m) for m in missing[:5])

            cand["match_score"] = final_score
            cand["match_score_details"] = final_result.get("score_details", {})
            cand["matched_skills"] = matched
            cand["missing_skills"] = missing

            trace("survived", {
                "cid": cid, "pre_score": pre_score, "final_score": final_score,
                "bonus": row["source_tier_bonus"],
            })
            return cand

    tasks = [trace_one(c) for c in after_s1]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    survivors = [r for r in results if r is not None]

    d = stage_counts["drops"]
    s1 = stage_counts["after_stage1"]
    stage_counts["after_stage2_yoe"] = s1 - d["stage2_min_years_pre_llm"]
    stage_counts["after_stage3_filter_assessment_pre"] = (
        stage_counts["after_stage2_yoe"]
        - d["stage3a_filter_assessment_failed"]
        - d["stage3a_filter_assessment_location"]
    )
    stage_counts["after_stage3_pre_llm_score_gate"] = (
        stage_counts["after_stage3_filter_assessment_pre"]
        - d["stage3b_pre_llm_low_score"]
        - d["stage3b_pre_llm_no_required_hit"]
    )
    stage_counts["after_stage4_llm"] = (
        stage_counts["after_stage3_pre_llm_score_gate"] - d["stage4_no_resume"]
    )
    stage_counts["after_stage5_filter_assessment_post"] = (
        stage_counts["after_stage4_llm"]
        - d["stage5_post_filter_failed"]
        - d["stage5_post_filter_location"]
    )
    stage_counts["after_stage6_scored"] = len(survivors)

    # ----- 5. Write output files -----
    with (out_dir / "02_stage_counts.json").open("w") as f:
        json.dump(stage_counts, f, indent=2)

    csv_fields = [
        "candidate_id", "name", "location", "source", "survived_to_stage",
        "drop_reason", "pre_llm_score", "final_score", "source_tier_bonus",
        "top_skill_matches", "top_skill_misses", "llm_extracted_yoe",
    ]
    with (out_dir / "03_per_candidate_trace.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for row in per_candidate.values():
            w.writerow({k: row.get(k, "") for k in csv_fields})

    # Top-50 markdown — by final_score desc.
    survivors_sorted = sorted(
        survivors,
        key=lambda c: float(c.get("match_score") or 0),
        reverse=True,
    )
    with (out_dir / "04_top50.md").open("w") as f:
        f.write(f"# Top 50 — variant `{variant['name']}`\n\n")
        f.write(f"Raw JobDiva: {before_s1} · reached UI: {len(survivors)}\n\n")
        for i, c in enumerate(survivors_sorted[:50], 1):
            cid = str(c.get("candidate_id") or c.get("id") or "")
            row = per_candidate.get(cid, {})
            f.write(f"## {i}. {c.get('name', '?')} — score {c.get('match_score')}\n")
            f.write(f"- id: `{cid}`  source: `{c.get('source','')}`\n")
            f.write(f"- location: {row.get('location','')}\n")
            f.write(f"- yoe (llm): {row.get('llm_extracted_yoe')}\n")
            f.write(f"- pre_llm_score: {row.get('pre_llm_score')}\n")
            f.write(f"- bonus: +{row.get('source_tier_bonus', 0)}\n")
            f.write(f"- matched: {row.get('top_skill_matches','')}\n")
            f.write(f"- missing: {row.get('top_skill_misses','')}\n")
            details = c.get("match_score_details") or {}
            if details:
                f.write(f"- dims: {json.dumps({k: v.get('score') if isinstance(v, dict) else v for k, v in details.items()}, default=str)[:300]}\n")
            f.write("\n")

    # Bottom-20 of those that REACHED UI (bad-passing).
    with (out_dir / "05_bottom20_passed.md").open("w") as f:
        f.write(f"# Bottom 20 that still passed — variant `{variant['name']}`\n\n")
        f.write("If any of these are obviously bad, the filter pipeline is too lenient.\n\n")
        for i, c in enumerate(survivors_sorted[-20:], 1):
            cid = str(c.get("candidate_id") or c.get("id") or "")
            row = per_candidate.get(cid, {})
            f.write(f"## {i}. {c.get('name', '?')} — score {c.get('match_score')}\n")
            f.write(f"- id: `{cid}`  source: `{c.get('source','')}`\n")
            f.write(f"- location: {row.get('location','')}\n")
            f.write(f"- yoe (llm): {row.get('llm_extracted_yoe')}\n")
            f.write(f"- matched: {row.get('top_skill_matches','')}\n")
            f.write(f"- missing: {row.get('top_skill_misses','')}\n")
            f.write("\n")

    # High pre-score, dropped at later stages (good-filtered-out).
    high_dropped = [
        r for r in per_candidate.values()
        if r.get("pre_llm_score") is not None
        and float(r["pre_llm_score"]) >= 50
        and r.get("survived_to_stage", 0) < 6
    ]
    high_dropped.sort(key=lambda r: float(r.get("pre_llm_score") or 0), reverse=True)
    with (out_dir / "06_high_score_dropped.md").open("w") as f:
        f.write(f"# Dropped after pre-LLM score ≥ 50 — variant `{variant['name']}`\n\n")
        f.write("Candidates whose pre-LLM score suggested fit but got filtered later.\n\n")
        for i, r in enumerate(high_dropped, 1):
            f.write(f"## {i}. {r.get('name','?')} — pre_score {r.get('pre_llm_score')}\n")
            f.write(f"- id: `{r.get('candidate_id','')}`  source: `{r.get('source','')}`\n")
            f.write(f"- location: {r.get('location','')}\n")
            f.write(f"- dropped at: stage {r.get('survived_to_stage')} → {r.get('drop_reason')}\n")
            f.write("\n")

    trace_fh.close()

    scores = [float(c.get("match_score") or 0) for c in survivors]
    summary = {
        "variant": variant["name"],
        "endpoint": endpoint,
        "raw_count": before_s1,
        "survived_s1": stage_counts["after_stage1"],
        "reached_ui": len(survivors),
        "mean_score": round(statistics.mean(scores), 1) if scores else 0,
        "median_score": round(statistics.median(scores), 1) if scores else 0,
        "candidate_ids": raw_meta["candidate_ids"],
    }
    return summary


# ---------------------------------------------------------------------------
# Compare-mode aggregator (orchestrator)
# ---------------------------------------------------------------------------
def write_compare(job_out_dir: Path, summaries: List[Dict[str, Any]]) -> None:
    """Aggregate per-variant summaries into compare.md + a JSON copy."""
    with (job_out_dir / "_summaries.json").open("w") as f:
        json.dump(summaries, f, indent=2, default=str)

    baseline = summaries[0] if summaries else None
    baseline_ids = set(baseline.get("candidate_ids", [])) if baseline else set()

    lines: List[str] = []
    lines.append(f"# Variant comparison — job {job_out_dir.name}\n")
    lines.append("| variant | endpoint | raw | s1 | UI | mean | median | ids∩baseline |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in summaries:
        ids = set(s.get("candidate_ids", []))
        overlap = len(ids & baseline_ids) if baseline_ids else 0
        lines.append(
            f"| {s['variant']} | {s.get('endpoint','')} | {s.get('raw_count',0)} | "
            f"{s.get('survived_s1',0)} | {s.get('reached_ui',0)} | "
            f"{s.get('mean_score',0)} | {s.get('median_score',0)} | "
            f"{overlap}/{s.get('raw_count',0)} |"
        )

    # Fixed-pool sanity check across TalentSearch variants.
    ts_variants = [s for s in summaries if s.get("endpoint", "").startswith("talentsearch")]
    if len(ts_variants) >= 2:
        lines.append("\n## TalentSearch pool stability\n")
        lines.append("Set-diff of raw JobDiva candidate_ids across TalentSearch variants.")
        lines.append("If all rows show ~100% overlap with different booleans, the 'fixed ~1551 pool' bug is real.\n")
        lines.append("| variant_a | variant_b | a_size | b_size | shared | jaccard |")
        lines.append("|---|---|---|---|---|---|")
        for i, a in enumerate(ts_variants):
            for b in ts_variants[i + 1:]:
                a_ids = set(a.get("candidate_ids", []))
                b_ids = set(b.get("candidate_ids", []))
                shared = len(a_ids & b_ids)
                union = len(a_ids | b_ids)
                jaccard = round(shared / union, 3) if union else 0
                lines.append(
                    f"| {a['variant']} | {b['variant']} | {len(a_ids)} | "
                    f"{len(b_ids)} | {shared} | {jaccard} |"
                )

    (job_out_dir / "compare.md").write_text("\n".join(lines) + "\n")


def merge_env(base_env: Dict[str, str], overrides: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out = dict(base_env)
    for k, v in (overrides or {}).items():
        out[str(k)] = str(v)
    return out


def run_orchestrator(
    jobs_path: Path,
    variants_path: Path,
    out_root: Path,
    only_job: Optional[str],
    only_variant: Optional[str],
) -> int:
    jobs_doc = load_json(jobs_path)
    variants_doc = load_json(variants_path)
    jobs = jobs_doc.get("jobs") or jobs_doc  # accept either {jobs:[...]} or [...]
    variants = variants_doc.get("variants") or variants_doc

    if only_job:
        jobs = [j for j in jobs if str(j.get("job_id")) == only_job]
    if only_variant:
        variants = [v for v in variants if v.get("name") == only_variant]

    if not jobs:
        print("✗ No jobs to run.", file=sys.stderr)
        return 1
    if not variants:
        print("✗ No variants to run.", file=sys.stderr)
        return 1

    out_root.mkdir(parents=True, exist_ok=True)

    for job in jobs:
        job_id = str(job["job_id"])
        job_out = out_root / job_id.replace("/", "_")
        job_out.mkdir(parents=True, exist_ok=True)

        rubric_cache = job_out / "_rubric.json"
        try:
            job = asyncio.run(ensure_rubric(job, rubric_cache))
        except Exception as e:
            print(f"  ✗ rubric fetch failed for {job_id}: {e}", file=sys.stderr)
            continue

        rubric = job.get("rubric") or {}
        print(
            f"  rubric: title='{rubric.get('title')}' skills={rubric.get('skills', [])[:8]}"
            f" location='{rubric.get('location')}' min_years={rubric.get('min_experience_years')}"
        )

        (job_out / "_job.json").write_text(json.dumps(job, indent=2, default=str))

        summaries: List[Dict[str, Any]] = []
        for variant in variants:
            name = variant["name"]
            variant_out = job_out / name
            print(f"\n── job {job_id} · variant {name} ──")

            # Each variant runs as a fresh subprocess so env vars take effect
            # at module import time (config.py reads at import).
            child_env = merge_env(os.environ.copy(), variant.get("env"))
            cmd = [
                sys.executable, "-u", str(Path(__file__).resolve()),
                "--internal-run",
                "--jobs", str(jobs_path),
                "--variants", str(variants_path),
                "--job-id", job_id,
                "--variant-name", name,
                "--out", str(variant_out),
            ]
            try:
                rc = subprocess.run(cmd, env=child_env, check=False).returncode
            except KeyboardInterrupt:
                print("  Interrupted by user.")
                return 130
            if rc != 0:
                print(f"  ✗ variant {name} subprocess exited {rc}", file=sys.stderr)
                summaries.append({"variant": name, "error": f"exit_{rc}"})
                continue

            summary_path = variant_out / "_summary.json"
            if summary_path.exists():
                summaries.append(json.loads(summary_path.read_text()))
            else:
                summaries.append({"variant": name, "error": "no_summary"})

        write_compare(job_out, summaries)
        print(f"\n✓ Wrote {job_out / 'compare.md'}")

    return 0


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def _internal_run(args: argparse.Namespace) -> int:
    jobs_doc = load_json(Path(args.jobs))
    variants_doc = load_json(Path(args.variants))
    jobs = jobs_doc.get("jobs") or jobs_doc
    variants = variants_doc.get("variants") or variants_doc

    job = next((j for j in jobs if str(j.get("job_id")) == args.job_id), None)
    if not job:
        print(f"✗ Job {args.job_id} not in {args.jobs}", file=sys.stderr)
        return 1
    variant = next((v for v in variants if v.get("name") == args.variant_name), None)
    if not variant:
        print(f"✗ Variant {args.variant_name} not in {args.variants}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    # When orchestrator pre-fetched rubrics, the merged job sits in the
    # parent dir as `_job.json`. Prefer that over the raw jobs.json entry.
    parent_job_file = out_dir.parent / "_job.json"
    if parent_job_file.exists():
        try:
            job = json.loads(parent_job_file.read_text())
        except Exception as e:
            print(f"⚠ failed to read {parent_job_file}: {e}", file=sys.stderr)
    elif not (job.get("rubric") or {}).get("skills"):
        rubric_cache = out_dir.parent / "_rubric.json"
        job = await ensure_rubric(job, rubric_cache)

    job = apply_rubric_override(job, variant)
    summary = await run_variant_pipeline(job, variant, out_dir)
    (out_dir / "_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sourcing-quality POC: compare JobDiva variants + filter pipeline")
    parser.add_argument("--jobs", required=True, help="Path to jobs.json (the jobs the POC runs on).")
    parser.add_argument("--variants", required=True, help="Path to variants.json (the variant matrix).")
    parser.add_argument("--out", required=True, help="Output root directory.")
    parser.add_argument("--only-job", default=None, help="Run only this job_id (for fast iteration).")
    parser.add_argument("--only-variant", default=None, help="Run only this variant name.")
    parser.add_argument("--internal-run", action="store_true",
                        help="Internal: execute one (job × variant) in this process.")
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--variant-name", default=None)
    args = parser.parse_args()

    if args.internal_run:
        if not args.job_id or not args.variant_name:
            print("✗ --internal-run requires --job-id and --variant-name", file=sys.stderr)
            return 2
        return asyncio.run(_internal_run(args))

    return run_orchestrator(
        jobs_path=Path(args.jobs),
        variants_path=Path(args.variants),
        out_root=Path(args.out),
        only_job=args.only_job,
        only_variant=args.only_variant,
    )


if __name__ == "__main__":
    sys.exit(main())
