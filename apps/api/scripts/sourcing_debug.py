"""Diagnose why specific candidates are missing from a JobDiva sourcing search.

Runs four probes against the SAME JobDiva account the API uses, for a single
CandidateSearchRequest payload + a list of target emails to trace:

  Probe A — production mirror
      Apply `_strip_location_from_boolean` + `translate_for_jobdiva`
      exactly like prod, push countries/states into talentSearchDef,
      require_resume=False, page_size=100, pageNumber 0..N-1.

  Probe B — clean strip (drops the orphan `within N mi` tail prod leaves behind)
      Use a tighter regex that removes `AND "<loc>" within <N> mi` as one unit.
      Otherwise same as Probe A.

  Probe C — no OVER N YRS clauses
      Strip every `OVER \\d+ YRS` from the boolean. Same flags as Probe B.

  Probe D — production pipeline trace
      Run `_search_talent_pool` with production flags (require_resume=True)
      and replay Stage 1 / 2 / 3a / 5 (pre-LLM) for the target emails,
      recording which stage drops each one and why.

The script writes raw JSON dumps for all four probes and a `report.md` with
a one-line verdict per target email. No production code paths are modified.

Run:
    cd apps/api
    source .env
    venv/bin/python -m scripts.sourcing_debug \\
        --criteria scripts/sourcing_debug_26-11245.json \\
        --target-emails adarshkt2025@gmail.com,sohitha716@gmail.com,vsne1519@gmail.com \\
        --out tmp/sourcing_debug/26-11245 \\
        --pages 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
APPS_API_DIR = SCRIPT_DIR.parent
if str(APPS_API_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_API_DIR))


# ---------------------------------------------------------------------------
# Boolean rewrites for the probes
# ---------------------------------------------------------------------------
_LOC_WITHIN_TAIL_RE = re.compile(
    r"""
    \s+AND\s+                       # glue before location
    "(?P<loc>[^"]+)"                # quoted location
    (?:\s+within\s+\d+\s*mi(?:les)?)?  # optional "within N mi" tail
    (?=\s+AND\b|\s*$|\s*\))         # stop at next AND, end-of-string, or close paren
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def clean_strip_location_from_boolean(boolean: str) -> str:
    """Remove `AND "<loc>" within N mi` (or `AND "<loc>"`) cleanly.

    Production's `_strip_location_from_boolean` removes only the quoted
    location and leaves `within 25 mi` dangling — this version removes
    the whole tail in one shot so the resulting boolean is JobDiva-valid.
    """
    if not boolean:
        return boolean
    out = _LOC_WITHIN_TAIL_RE.sub("", boolean)
    return re.sub(r"\s+", " ", out).strip()


_OVER_YRS_RE = re.compile(r"\s*OVER\s+\d+\s+YRS\b", flags=re.IGNORECASE)


_STATE_CODE_TO_NAME = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def strip_over_yrs(boolean: str) -> str:
    """Drop every `OVER N YRS` clause. Leaves the rest of the boolean intact."""
    if not boolean:
        return boolean
    return re.sub(r"\s+", " ", _OVER_YRS_RE.sub("", boolean)).strip()


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------
def _build_criteria_from_payload(payload: Dict[str, Any]):
    """Convert a `CandidateSearchRequest`-shaped payload into a SearchCriteria.

    Mirrors the relevant parts of routers/candidates.py:search_jobdiva_candidates
    so Probe D's pipeline trace exercises the same code paths as production.
    """
    from services.unified_candidate_search import SearchCriteria

    job_id = str(payload.get("job_id") or "")
    location = str(payload.get("location") or "")
    within_miles = int(payload.get("within_miles") or 25)
    if within_miles > 100:
        within_miles = 100
    require_resume = payload.get("require_resume")
    require_resume = True if require_resume is None else bool(require_resume)

    page_size = int(payload.get("page_size") or payload.get("limit") or 100)
    return SearchCriteria(
        job_id=job_id,
        title_criteria=list(payload.get("title_criteria") or []),
        skill_criteria=list(payload.get("skill_criteria") or []),
        keywords=list(payload.get("keywords") or []),
        resume_match_filters=list(payload.get("resume_match_filters") or []),
        location=location,
        within_miles=within_miles,
        companies=list(payload.get("companies") or []),
        page_size=page_size,
        sources=list(payload.get("sources") or ["JobDiva"]),
        open_to_work=bool(payload.get("open_to_work", True)),
        boolean_string=str(payload.get("boolean_string") or ""),
        recent_days=payload.get("recent_days"),
        require_resume=require_resume,
        include_relocation_candidates=bool(
            payload.get("include_relocation_candidates", True)
            if payload.get("include_relocation_candidates") is not None
            else True
        ),
        min_experience_years=payload.get("min_experience_years"),
    )


async def _talent_search_raw(
    jobdiva,
    token: str,
    *,
    boolean_for_skills: str,
    countries: List[str],
    states: List[str],
    page_size: int,
    page_number: int,
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Hit JobDiva TalentSearch with no post-API filtering at all.

    Returns (http_status, parsed_json_or_none). Use this when we need to see
    the raw JobDiva response — Probes A/B/C all bypass our pipeline.
    """
    import httpx

    url = f"{jobdiva.api_url}/apiv2/jobdiva/TalentSearch"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "talentSearchDef": {
            "skills": boolean_for_skills,
            "countries": ",".join([c for c in countries if c]).strip(),
            "states": ",".join([s for s in states if s]).strip(),
            "pageNumber": int(page_number),
            "pageSize": int(page_size),
        }
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        try:
            data = resp.json() if resp.content else None
        except Exception:
            data = {"_raw": resp.text}
    return resp.status_code, data


def _extract_candidates(data: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return list(data or [])
    return list(data.get("data") or data.get("candidates") or data.get("results") or [])


def _email_of(cand: Dict[str, Any]) -> str:
    from services.jobdiva import get_field  # local import; keeps top-level fast

    return str(get_field(cand, ["email", "EMAIL"]) or "").strip().lower()


def _summarize_candidate(cand: Dict[str, Any]) -> Dict[str, Any]:
    from services.jobdiva import get_field

    return {
        "candidate_id": str(get_field(cand, ["candidateId", "CANDIDATEID", "id", "ID"]) or ""),
        "email": _email_of(cand),
        "name": (
            f"{get_field(cand, ['firstName', 'FIRSTNAME']) or ''} "
            f"{get_field(cand, ['lastName', 'LASTNAME']) or ''}"
        ).strip(),
        "title": get_field(cand, ["title", "TITLE"]) or "",
        "city": get_field(cand, ["city", "CITY"]) or "",
        "state": get_field(cand, ["state", "STATE"]) or "",
        "has_resume_id": bool(get_field(cand, ["resumeId", "RESUMEID"])),
        "has_resume_text": bool(
            (get_field(cand, ["resumeText", "RESUMETEXT", "resume_text"]) or "").strip()
        ),
        "experience_years": get_field(cand, ["experienceYears", "EXPERIENCEYEARS"]) or 0,
        "skills_preview": str(get_field(cand, ["skills", "SKILLS"]) or "")[:160],
    }


async def _run_raw_probe(
    *,
    label: str,
    boolean_for_skills: str,
    jobdiva,
    token: str,
    countries: List[str],
    states: List[str],
    pages: int,
    page_size: int,
    targets: List[str],
    out_dir: Path,
    filename: str,
) -> Dict[str, Any]:
    """Run a paginated raw TalentSearch probe; return summary + write JSON dump."""
    pages_data: List[Dict[str, Any]] = []
    total_candidates: List[Dict[str, Any]] = []
    target_hits: Dict[str, Dict[str, Any]] = {}

    t0 = time.time()
    for page_idx in range(pages):
        status, body = await _talent_search_raw(
            jobdiva,
            token,
            boolean_for_skills=boolean_for_skills,
            countries=countries,
            states=states,
            page_size=page_size,
            page_number=page_idx,
        )
        cands = _extract_candidates(body)
        pages_data.append({
            "page_number": page_idx,
            "http_status": status,
            "count": len(cands),
            "candidates": [_summarize_candidate(c) for c in cands],
            "raw_body_keys": list(body.keys()) if isinstance(body, dict) else None,
        })
        total_candidates.extend(cands)
        for c in cands:
            email = _email_of(c)
            if email in targets and email not in target_hits:
                target_hits[email] = {
                    "page_number": page_idx,
                    **_summarize_candidate(c),
                }
        if status != 200 or len(cands) == 0:
            break  # JobDiva returned an error or no more pages

    elapsed_s = round(time.time() - t0, 2)

    dump = {
        "label": label,
        "boolean_for_skills": boolean_for_skills,
        "countries": countries,
        "states": states,
        "page_size": page_size,
        "pages_scanned": len(pages_data),
        "total_candidates": len(total_candidates),
        "elapsed_seconds": elapsed_s,
        "target_hits": target_hits,
        "missing_targets": [t for t in targets if t not in target_hits],
        "pages": pages_data,
    }
    (out_dir / filename).write_text(json.dumps(dump, indent=2, default=str))

    print(
        f"  {label}: {len(total_candidates)} candidates across "
        f"{len(pages_data)} page(s) in {elapsed_s}s — "
        f"target hits: {len(target_hits)}/{len(targets)}"
    )
    for email in targets:
        if email in target_hits:
            h = target_hits[email]
            print(f"    ✓ {email} on page {h['page_number']} (id={h['candidate_id']}, state={h['state']})")
        else:
            print(f"    ✗ {email} not in any returned page")
    return dump


# ---------------------------------------------------------------------------
# Probe D — production pipeline trace (no LLM)
# ---------------------------------------------------------------------------
async def _run_pipeline_trace(
    *,
    payload: Dict[str, Any],
    jobdiva,
    token: str,
    targets: List[str],
    pages: int,
    out_dir: Path,
) -> Dict[str, Any]:
    """Run _search_talent_pool the way production does it, then replay
    pre-LLM filter stages for every candidate. Track target-email drops."""
    from services.unified_candidate_search import UnifiedCandidateSearch

    criteria = _build_criteria_from_payload(payload)
    service = UnifiedCandidateSearch()

    # Mirror production: location stripped, translated boolean built later
    # inside _search_talent_pool via the translator.
    countries, states = service._resolve_jobdiva_geo(criteria)
    prod_boolean = service._strip_location_from_boolean(
        criteria.boolean_string or service._build_boolean_string(criteria),
        criteria.location,
    )
    page_size = int(criteria.page_size or 100)

    # Walk pages to mirror Probe A's scan depth (default 5 pages * 100).
    all_results: List[Dict[str, Any]] = []
    pages_meta: List[Dict[str, Any]] = []
    t0 = time.time()
    for page_idx in range(pages):
        page_results = await jobdiva._search_talent_pool(
            skills=[s for s in (criteria.skill_criteria or [])],
            location=criteria.location,
            limit=page_size,
            token=token,
            boolean_string=prod_boolean,
            recent_days=criteria.recent_days,
            require_resume=criteria.require_resume,
            countries=countries,
            states=states,
            page_number=page_idx,
        )
        pages_meta.append({
            "page_number": page_idx,
            "count_after_search_talent_pool": len(page_results),
        })
        all_results.extend(page_results)
        if not page_results:
            break

    elapsed_s = round(time.time() - t0, 2)

    # Replay the pre-LLM stages for every candidate (no LLM cost).
    service._current_family = service._resolve_search_family(criteria)
    stage1 = service._filter_candidates(
        list(all_results), criteria, source_type="talent_search"
    )
    stage1_ids = {str(c.get("candidate_id") or c.get("id") or "") for c in stage1}

    # Helper: trace a single candidate through stages 2 → 3a → 5.
    def _trace_one(cand: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(cand.get("candidate_id") or cand.get("id") or "")
        trace: Dict[str, Any] = {
            "candidate_id": cid,
            "email": str(cand.get("email") or "").strip().lower(),
            "name": cand.get("name") or "",
            "state": cand.get("state") or "",
            "city": cand.get("city") or "",
            "title": cand.get("title") or "",
            "has_resume_text": bool((cand.get("resume_text") or "").strip()),
            "experience_years": cand.get("experience_years") or 0,
            "stage1_passed": cid in stage1_ids,
            "stage2_below_min_years": None,
            "stage3a": None,
            "stage5": None,
            "drop_stage": None,
            "drop_reason": None,
        }
        if not trace["stage1_passed"]:
            trace["drop_stage"] = "stage1_summary_screen"
            trace["drop_reason"] = "filtered_by_summary_screen"
            return trace

        # Stage 2: pre-LLM YOE heuristic
        try:
            below = service._candidate_below_min_years_pre_llm(cand, criteria)
        except Exception as e:
            below = False
            trace["stage2_error"] = str(e)
        trace["stage2_below_min_years"] = bool(below)
        if below:
            trace["drop_stage"] = "stage2_pre_llm_yoe"
            trace["drop_reason"] = (
                f"heuristic YOE < min_experience_years={criteria.min_experience_years}"
            )
            return trace

        # Stage 3a: filter assessment with enforce_years=False
        assessment_pre = service._filter_assessment(
            cand, criteria, enforce_years=False
        )
        trace["stage3a"] = {
            "passes": bool(assessment_pre.get("passes")),
            "missing": list(assessment_pre.get("missing") or [])[:8],
            "excluded": list(assessment_pre.get("excluded") or [])[:8],
            "location_failure_reason": assessment_pre.get("location_failure_reason"),
        }
        if not assessment_pre.get("passes"):
            reason = assessment_pre.get("location_failure_reason")
            trace["drop_stage"] = "stage3a_filter_assessment_pre"
            trace["drop_reason"] = (
                f"location={reason}" if reason else f"excluded={assessment_pre.get('excluded')}"
            )
            return trace

        # Stage 5: filter assessment with enforce_years=True (post-LLM gate
        # — we run it pre-LLM here using the source's experience_years which
        # is the same field _candidate_profile reads. Catches the strict-YOE
        # drop without paying for LLM extraction.)
        assessment_post = service._filter_assessment(
            cand, criteria, enforce_years=True
        )
        trace["stage5"] = {
            "passes": bool(assessment_post.get("passes")),
            "missing": list(assessment_post.get("missing") or [])[:8],
            "excluded": list(assessment_post.get("excluded") or [])[:8],
            "min_years_failure": bool(assessment_post.get("min_years_failure")),
            "matched_required": assessment_post.get("matched_required"),
            "total_required": assessment_post.get("total_required"),
        }
        if not assessment_post.get("passes"):
            if assessment_post.get("min_years_failure"):
                trace["drop_stage"] = "stage5_post_yoe_floor"
                trace["drop_reason"] = (
                    f"YOE < min_experience_years={criteria.min_experience_years}"
                )
            else:
                trace["drop_stage"] = "stage5_filter_assessment_post"
                trace["drop_reason"] = (
                    f"missing required ratio: matched="
                    f"{assessment_post.get('matched_required')}/"
                    f"{assessment_post.get('total_required')}"
                )
            return trace

        trace["drop_stage"] = None
        trace["drop_reason"] = None
        return trace

    per_candidate_traces = [_trace_one(c) for c in all_results]
    by_email = {tr["email"]: tr for tr in per_candidate_traces if tr.get("email")}
    target_traces = {email: by_email.get(email) for email in targets}

    dump = {
        "label": "Probe D — production pipeline trace",
        "production_boolean_after_strip": prod_boolean,
        "countries": countries,
        "states": states,
        "page_size": page_size,
        "pages": pages_meta,
        "elapsed_seconds": elapsed_s,
        "raw_search_count": len(all_results),
        "stage1_kept": len(stage1),
        "stage1_dropped": len(all_results) - len(stage1),
        "target_traces": target_traces,
        "all_traces_count": len(per_candidate_traces),
        "all_traces": per_candidate_traces,
    }
    (out_dir / "04_probe_d_pipeline_trace.json").write_text(
        json.dumps(dump, indent=2, default=str)
    )

    print(
        f"  Probe D: {len(all_results)} raw → stage1 kept {len(stage1)} in {elapsed_s}s"
    )
    for email in targets:
        tr = target_traces.get(email)
        if tr is None:
            print(f"    ✗ {email} not in production response at all")
        elif tr.get("drop_stage"):
            print(f"    ✗ {email} dropped at {tr['drop_stage']}: {tr['drop_reason']}")
        else:
            print(f"    ✓ {email} reached UI (id={tr.get('candidate_id')})")
    return dump


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _verdict_for_email(
    email: str,
    probe_a: Dict[str, Any],
    probe_b: Dict[str, Any],
    probe_c: Dict[str, Any],
    probe_d: Dict[str, Any],
) -> str:
    in_a = email in (probe_a.get("target_hits") or {})
    in_b = email in (probe_b.get("target_hits") or {})
    in_c = email in (probe_c.get("target_hits") or {})
    d_trace = (probe_d.get("target_traces") or {}).get(email)
    d_drop = d_trace.get("drop_stage") if d_trace else None

    if in_a and d_trace and not d_drop:
        return "Reaches UI (no fix needed)"
    if in_a and d_drop:
        return f"In JobDiva but our pipeline drops at {d_drop}: {d_trace.get('drop_reason')}"
    if not in_a and in_b:
        return "Excluded by orphan `within N mi` left in boolean by _strip_location_from_boolean"
    if not in_a and not in_b and in_c:
        return "Excluded by `OVER N YRS` server-side YOE clause"
    if not in_a and not in_b and not in_c:
        return "Not in JobDiva TalentSearch at any boolean variant — check JobDiva profile / different endpoint"
    return f"Inconclusive (A={in_a} B={in_b} C={in_c} D_drop={d_drop})"


def _write_report(
    *,
    payload: Dict[str, Any],
    targets: List[str],
    probe_a: Dict[str, Any],
    probe_b: Dict[str, Any],
    probe_c: Dict[str, Any],
    probe_d: Dict[str, Any],
    out_dir: Path,
) -> None:
    lines: List[str] = []
    lines.append(f"# Sourcing-debug report — job {payload.get('job_id')}\n")
    lines.append(f"- criteria: `{json.dumps({k: payload.get(k) for k in ['job_id', 'location', 'within_miles', 'recent_days', 'require_resume', 'page', 'page_size']})}`")
    lines.append(f"- targets: `{', '.join(targets)}`")
    lines.append("")

    lines.append("## Probe summary\n")
    lines.append("| Probe | Boolean → JobDiva | Total | A: prod | B: clean | C: no-YRS | D: pipeline |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(
        f"| A — production mirror | `{probe_a.get('boolean_for_skills')}` | "
        f"{probe_a.get('total_candidates')} | — | — | — | — |"
    )
    lines.append(
        f"| B — clean strip       | `{probe_b.get('boolean_for_skills')}` | "
        f"{probe_b.get('total_candidates')} | — | — | — | — |"
    )
    lines.append(
        f"| C — no OVER YRS       | `{probe_c.get('boolean_for_skills')}` | "
        f"{probe_c.get('total_candidates')} | — | — | — | — |"
    )
    lines.append(
        f"| D — pipeline trace    | `{probe_d.get('production_boolean_after_strip')}` | "
        f"{probe_d.get('raw_search_count')} raw / {probe_d.get('stage1_kept')} stage1 | — | — | — | — |"
    )
    lines.append("")

    lines.append("## Per-target verdicts\n")
    for email in targets:
        verdict = _verdict_for_email(email, probe_a, probe_b, probe_c, probe_d)
        lines.append(f"### `{email}`\n")
        lines.append(f"**Verdict:** {verdict}\n")

        a_hit = (probe_a.get("target_hits") or {}).get(email)
        b_hit = (probe_b.get("target_hits") or {}).get(email)
        c_hit = (probe_c.get("target_hits") or {}).get(email)
        d_trace = (probe_d.get("target_traces") or {}).get(email)
        lines.append(f"- Probe A (prod mirror):   {'page ' + str(a_hit['page_number']) if a_hit else 'NOT FOUND'}")
        lines.append(f"- Probe B (clean strip):   {'page ' + str(b_hit['page_number']) if b_hit else 'NOT FOUND'}")
        lines.append(f"- Probe C (no OVER YRS):   {'page ' + str(c_hit['page_number']) if c_hit else 'NOT FOUND'}")
        if d_trace:
            drop = d_trace.get("drop_stage") or "reached UI"
            reason = d_trace.get("drop_reason") or "—"
            lines.append(f"- Probe D (pipeline):      `{drop}` — {reason}")
        else:
            lines.append("- Probe D (pipeline):      NOT FOUND in production response")
        lines.append("")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text("\n".join(lines) + "\n")


async def _run_jobagent_probe(
    *,
    payload: Dict[str, Any],
    jobdiva,
    targets: List[str],
    out_dir: Path,
) -> Dict[str, Any]:
    """Probe F — JobDiva JobAgentSearch (primary path before TalentSearch fallback).

    JobAgentSearch uses JobDiva's native AI matcher tied to the job's
    saved criteria. If criteria are configured in the JobDiva UI for
    this job, this returns the most accurate candidate set. Otherwise
    JobDiva returns "Criteria Not Assigned" and we fall back to
    TalentSearch in production.

    Also replays the pre-LLM pipeline stages (1 → 2 → 3a → 5) on the
    returned candidates so we can verify the 3 targets survive end-to-end.
    """
    from services.unified_candidate_search import UnifiedCandidateSearch

    job_id = str(payload.get("job_id") or "")
    resume_count = max(200, int(payload.get("page_size") or 100) * 4)

    t0 = time.time()
    try:
        result = await jobdiva.search_via_job_agent(
            job_id=job_id,
            resume_count=resume_count,
            require_resume=bool(payload.get("require_resume", True)),
        )
    except Exception as e:
        result = {"candidates": [], "criteria_unconfigured": False, "error": str(e)}
    elapsed_s = round(time.time() - t0, 2)

    cands = result.get("candidates") or []
    target_hits: Dict[str, Dict[str, Any]] = {}
    for c in cands:
        email = str(c.get("email") or "").strip().lower()
        if email in targets and email not in target_hits:
            target_hits[email] = {
                "candidate_id": str(c.get("candidate_id") or ""),
                "email": email,
                "name": c.get("name") or "",
                "state": c.get("state") or "",
                "city": c.get("city") or "",
                "title": c.get("title") or "",
                "has_resume": bool((c.get("resume_text") or "").strip()),
            }

    # Pipeline trace on JobAgent results
    service = UnifiedCandidateSearch()
    criteria = _build_criteria_from_payload(payload)
    service._current_family = service._resolve_search_family(criteria)
    stage1 = service._filter_candidates(list(cands), criteria, source_type="applicants")
    stage1_ids = {str(c.get("candidate_id") or c.get("id") or "") for c in stage1}

    def _trace_one(cand: Dict[str, Any]) -> Dict[str, Any]:
        cid = str(cand.get("candidate_id") or cand.get("id") or "")
        trace: Dict[str, Any] = {
            "candidate_id": cid,
            "email": str(cand.get("email") or "").strip().lower(),
            "name": cand.get("name") or "",
            "state": cand.get("state") or "",
            "city": cand.get("city") or "",
            "title": cand.get("title") or "",
            "has_resume_text": bool((cand.get("resume_text") or "").strip()),
            "experience_years": cand.get("experience_years") or 0,
            "stage1_passed": cid in stage1_ids,
            "drop_stage": None,
            "drop_reason": None,
        }
        if not trace["stage1_passed"]:
            trace["drop_stage"] = "stage1_summary_screen"
            trace["drop_reason"] = "filtered_by_summary_screen"
            return trace
        try:
            if service._candidate_below_min_years_pre_llm(cand, criteria):
                trace["drop_stage"] = "stage2_pre_llm_yoe"
                trace["drop_reason"] = f"heuristic YOE < min_experience_years={criteria.min_experience_years}"
                return trace
        except Exception:
            pass
        a = service._filter_assessment(cand, criteria, enforce_years=False)
        if not a.get("passes"):
            r = a.get("location_failure_reason")
            trace["drop_stage"] = "stage3a_filter_assessment_pre"
            trace["drop_reason"] = f"location={r}" if r else f"excluded={a.get('excluded')}"
            return trace
        b = service._filter_assessment(cand, criteria, enforce_years=True)
        if not b.get("passes"):
            if b.get("min_years_failure"):
                trace["drop_stage"] = "stage5_post_yoe_floor"
                trace["drop_reason"] = f"YOE < min={criteria.min_experience_years}"
            else:
                trace["drop_stage"] = "stage5_filter_assessment_post"
                trace["drop_reason"] = (
                    f"missing required ratio: "
                    f"{b.get('matched_required')}/{b.get('total_required')}"
                )
            return trace
        return trace

    per_traces = [_trace_one(c) for c in cands]
    by_email = {tr["email"]: tr for tr in per_traces if tr.get("email")}
    target_traces = {email: by_email.get(email) for email in targets}
    reached_ui = [t for t in per_traces if not t.get("drop_stage")]

    dump = {
        "label": "Probe F — JobAgentSearch (+ pipeline trace)",
        "job_id": job_id,
        "resume_count_requested": resume_count,
        "candidate_count": len(cands),
        "criteria_unconfigured": bool(result.get("criteria_unconfigured")),
        "resolved_jobdiva_id": result.get("resolved_jobdiva_id"),
        "error": result.get("error"),
        "elapsed_seconds": elapsed_s,
        "target_hits": target_hits,
        "missing_targets": [t for t in targets if t not in target_hits],
        "candidate_ids": [str(c.get("candidate_id") or "") for c in cands],
        "stage1_kept": len(stage1),
        "stage1_dropped": len(cands) - len(stage1),
        "reached_ui_count": len(reached_ui),
        "target_traces": target_traces,
        "all_traces": per_traces,
        "candidates_preview": [
            {
                "candidate_id": str(c.get("candidate_id") or ""),
                "email": str(c.get("email") or "").strip().lower(),
                "name": c.get("name") or "",
                "state": c.get("state") or "",
                "title": c.get("title") or "",
            }
            for c in cands[:50]
        ],
    }
    (out_dir / "06_probe_f_jobagent.json").write_text(json.dumps(dump, indent=2, default=str))

    if result.get("criteria_unconfigured"):
        print(
            f"  Probe F: JobDiva reports criteria_unconfigured=True for jobId="
            f"{result.get('resolved_jobdiva_id')} — JobAgent matcher not set in UI"
        )
    elif result.get("error"):
        print(f"  Probe F: ERROR — {result['error']}")
    else:
        print(
            f"  Probe F: {len(cands)} raw → stage1 {len(stage1)} → reached_UI {len(reached_ui)} in {elapsed_s}s"
        )
        print(f"    target hits in raw: {len(target_hits)}/{len(targets)}")
        for email in targets:
            tr = target_traces.get(email)
            if tr is None:
                print(f"    ✗ {email} not in JobAgent raw response")
            elif tr.get("drop_stage"):
                print(f"    ✗ {email} dropped at {tr['drop_stage']}: {tr['drop_reason']}")
            else:
                print(f"    ✓ {email} reached UI (id={tr.get('candidate_id')}, state={tr.get('state')})")
    return dump


def _apply_lenient_overrides() -> Dict[str, Any]:
    """Force-set every sourcing_config toggle to its lenient value for the
    duration of this process. Returns the resulting config snapshot.

    Editing apps/api/core/sourcing_config.py is the persistent path; this
    helper exists so the diagnostic can flip every flag in one shot
    without touching the file on disk.
    """
    from core import sourcing_config
    sourcing_config.INCLUDE_PROFILE_ONLY = True
    sourcing_config.STRIP_YEARS_FROM_BOOLEAN = True
    sourcing_config.SKIP_JOBDIVA_YOE_PRECHECK = True
    sourcing_config.REQUIRED_MATCH_RATIO = 0.3
    return _read_active_flags()


def _read_active_flags() -> Dict[str, Any]:
    from core import sourcing_config
    flags = {
        "INCLUDE_PROFILE_ONLY": sourcing_config.INCLUDE_PROFILE_ONLY,
        "STRIP_YEARS_FROM_BOOLEAN": sourcing_config.STRIP_YEARS_FROM_BOOLEAN,
        "SKIP_JOBDIVA_YOE_PRECHECK": sourcing_config.SKIP_JOBDIVA_YOE_PRECHECK,
        "REQUIRED_MATCH_RATIO": sourcing_config.REQUIRED_MATCH_RATIO,
    }
    print("Active sourcing_config toggles:")
    for k, v in flags.items():
        print(f"   sourcing_config.{k} = {v}")
    return flags


async def _main_async(args: argparse.Namespace) -> int:
    from services.jobdiva import JobDivaService
    from services.jobdiva_boolean_translator import translate_for_jobdiva
    from services.unified_candidate_search import UnifiedCandidateSearch

    criteria_path = Path(args.criteria).expanduser().resolve()
    if not criteria_path.exists():
        print(f"✗ Criteria file not found: {criteria_path}", file=sys.stderr)
        return 1
    payload = json.loads(criteria_path.read_text())

    targets = [e.strip().lower() for e in args.target_emails.split(",") if e.strip()]
    if not targets:
        print("✗ --target-emails required", file=sys.stderr)
        return 1

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "_criteria.json").write_text(json.dumps(payload, indent=2, default=str))

    print(f"▶ Sourcing debug for job {payload.get('job_id')}")
    print(f"   criteria : {criteria_path}")
    print(f"   targets  : {targets}")
    print(f"   pages    : {args.pages}")
    print(f"   out      : {out_dir}")
    print(f"   lenient  : {args.lenient}")
    print()
    if args.lenient:
        active_flags = _apply_lenient_overrides()
    else:
        active_flags = _read_active_flags()
    (out_dir / "_active_flags.json").write_text(json.dumps(active_flags, indent=2, default=str))

    # Auth once, reuse for every probe.
    jobdiva = JobDivaService()
    token = await jobdiva.authenticate()
    if not token:
        print("✗ JobDiva auth failed — check JOBDIVA_CLIENT_ID/USERNAME/PASSWORD env", file=sys.stderr)
        return 2

    # Build the canonical inputs once.
    service = UnifiedCandidateSearch()
    criteria = _build_criteria_from_payload(payload)
    countries, states = service._resolve_jobdiva_geo(criteria)
    raw_boolean = criteria.boolean_string or service._build_boolean_string(criteria)

    # Probe A — production mirror
    prod_stripped = service._strip_location_from_boolean(raw_boolean, criteria.location)
    prod_translated = translate_for_jobdiva(prod_stripped) or prod_stripped
    print(f"\n── Probe A — production mirror ──")
    print(f"   sent to JobDiva: {prod_translated!r}")
    print(f"   countries={countries} states={states}")
    probe_a = await _run_raw_probe(
        label="Probe A",
        boolean_for_skills=prod_translated,
        jobdiva=jobdiva,
        token=token,
        countries=countries,
        states=states,
        pages=args.pages,
        page_size=100,
        targets=targets,
        out_dir=out_dir,
        filename="01_probe_a_production_mirror.json",
    )

    # Probe B — clean strip
    clean_stripped = clean_strip_location_from_boolean(raw_boolean)
    clean_translated = translate_for_jobdiva(clean_stripped) or clean_stripped
    print(f"\n── Probe B — clean strip ──")
    print(f"   sent to JobDiva: {clean_translated!r}")
    probe_b = await _run_raw_probe(
        label="Probe B",
        boolean_for_skills=clean_translated,
        jobdiva=jobdiva,
        token=token,
        countries=countries,
        states=states,
        pages=args.pages,
        page_size=100,
        targets=targets,
        out_dir=out_dir,
        filename="02_probe_b_clean_strip.json",
    )

    # Probe C — no OVER YRS
    no_years_boolean = strip_over_yrs(clean_stripped)
    no_years_translated = translate_for_jobdiva(no_years_boolean) or no_years_boolean
    print(f"\n── Probe C — no OVER YRS clauses ──")
    print(f"   sent to JobDiva: {no_years_translated!r}")
    probe_c = await _run_raw_probe(
        label="Probe C",
        boolean_for_skills=no_years_translated,
        jobdiva=jobdiva,
        token=token,
        countries=countries,
        states=states,
        pages=args.pages,
        page_size=100,
        targets=targets,
        out_dir=out_dir,
        filename="03_probe_c_no_over_yrs.json",
    )

    # Probe D — production pipeline trace
    print(f"\n── Probe D — production pipeline trace ──")
    probe_d = await _run_pipeline_trace(
        payload=payload,
        jobdiva=jobdiva,
        token=token,
        targets=targets,
        pages=args.pages,
        out_dir=out_dir,
    )

    # Probe F — JobAgentSearch (#1 verification)
    print(f"\n── Probe F — JobAgentSearch ──")
    probe_f = await _run_jobagent_probe(
        payload=payload,
        jobdiva=jobdiva,
        targets=targets,
        out_dir=out_dir,
    )

    # Probe G — same boolean as Probe B but with the state spelled out as
    # the full name ("New Jersey") instead of the 2-letter code ("NJ").
    # Hypothesis: JobDiva's TalentSearch `states` field may be matching
    # against a different value than the 2-letter abbreviation. Maps
    # `criteria.states` → ["New Jersey"] via a simple state-code lookup.
    full_state_names = [_STATE_CODE_TO_NAME.get(s.upper(), s) for s in states]
    print(f"\n── Probe G — full state name (NJ → New Jersey) ──")
    print(f"   sent to JobDiva: {clean_translated!r}")
    print(f"   countries={countries} states={full_state_names}")
    probe_g = await _run_raw_probe(
        label="Probe G",
        boolean_for_skills=clean_translated,
        jobdiva=jobdiva,
        token=token,
        countries=countries,
        states=full_state_names,
        pages=args.pages,
        page_size=100,
        targets=targets,
        out_dir=out_dir,
        filename="07_probe_g_full_state_name.json",
    )

    # Final report
    _write_report(
        payload=payload,
        targets=targets,
        probe_a=probe_a,
        probe_b=probe_b,
        probe_c=probe_c,
        probe_d=probe_d,
        out_dir=out_dir,
    )

    print(f"\n✓ Wrote {out_dir / 'report.md'}")
    print("\nVerdicts:")
    for email in targets:
        print(f"  · {email}: {_verdict_for_email(email, probe_a, probe_b, probe_c, probe_d)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose missing-candidate JobDiva sourcing runs")
    parser.add_argument("--criteria", required=True, help="Path to JSON file containing the CandidateSearchRequest payload.")
    parser.add_argument("--target-emails", required=True, help="Comma-separated list of candidate emails to trace.")
    parser.add_argument("--out", required=True, help="Directory to write probe JSON dumps + report.md.")
    parser.add_argument("--pages", type=int, default=5, help="Pages to scan per probe (page_size=100). Default 5.")
    parser.add_argument("--lenient", action="store_true", help="Apply all lenient sourcing_config overrides for this run.")
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    sys.exit(main())
