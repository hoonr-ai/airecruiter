from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import asyncio
import json
import logging
from datetime import datetime, timezone
import httpx
import re

from services.ai_service import ai_service
from services.jobdiva import jobdiva_service
from services.unipile import unipile_service
from services.sourced_candidates_storage import sourced_candidates_storage
from services.unified_candidate_search import SearchCriteria, unified_search_service
from models import (
    CandidateSearchRequest, CandidateMessageRequest, CandidatesSaveRequest,
    CandidateAnalysisRequest, CandidateAnalysisResponse, CandidateFeedbackRequest,
)
from routers._helpers import get_db_connection

router = APIRouter()
logger = logging.getLogger(__name__)


def _json_load_safe(value: Any, default: Any):
    if value is None:
        return default
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except Exception:
            return default
    return default


def _build_resume_matching_criteria(job_ref: str) -> Optional[SearchCriteria]:
    """Build SearchCriteria from monitored_jobs for detailed resume re-scoring."""
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT resume_match_filters, sourcing_filters, jobdiva_id
                    FROM monitored_jobs
                    WHERE job_id = %s OR jobdiva_id = %s
                    LIMIT 1
                    """,
                    (job_ref, job_ref),
                )
                row = cur.fetchone()
                if not row:
                    return None
        finally:
            conn.close()

        resume_match_filters = _json_load_safe(row[0], [])
        sourcing_filters = _json_load_safe(row[1], {})
        resolved_job_ref = row[2] or job_ref

        title_criteria = [
            {
                "value": t.get("value", ""),
                "match_type": t.get("matchType", "must"),
                "years": t.get("years", 0),
                "recent": t.get("recent", False),
                "similar_terms": t.get("selectedSimilarTitles") or [],
            }
            for t in (sourcing_filters.get("titles") or [])
        ]
        skill_criteria = [
            {
                "value": s.get("value", ""),
                "match_type": s.get("matchType", "must"),
                "years": s.get("years", 0),
                "recent": s.get("recent", False),
                "similar_terms": s.get("selectedSimilarSkills") or [],
            }
            for s in (sourcing_filters.get("skills") or [])
        ]
        locations = sourcing_filters.get("locations") or []
        primary_location = locations[0].get("value", "") if locations else ""

        return SearchCriteria(
            job_id=str(resolved_job_ref),
            title_criteria=title_criteria,
            skill_criteria=skill_criteria,
            keywords=sourcing_filters.get("keywords") or [],
            companies=sourcing_filters.get("companies") or [],
            resume_match_filters=resume_match_filters,
            location=primary_location,
            page_size=100,
            sources=["JobDiva"],
            bypass_screening=False,
        )
    except Exception as e:
        logger.warning(f"resume matching criteria load failed for {job_ref}: {e}")
        return None


def _build_candidate_for_resume_matching(payload: Dict[str, Any]) -> Dict[str, Any]:
    data_blob = _json_load_safe(payload.get("data"), {})
    enhanced = payload.get("enhanced_info") or data_blob.get("enhanced_info") or {}
    return {
        "candidate_id": str(payload.get("candidate_id") or ""),
        "name": payload.get("name") or "",
        "title": payload.get("headline") or "",
        "headline": payload.get("headline") or "",
        "location": payload.get("location") or "",
        "resume_text": payload.get("resume_text") or "",
        "skills": data_blob.get("skills") or payload.get("skills") or [],
        "experience_years": data_blob.get("experience_years") or payload.get("experience_years") or 0,
        "company_experience": data_blob.get("company_experience") or [],
        "education": data_blob.get("education") or [],
        "certifications": data_blob.get("certifications") or [],
        "enhanced_info": enhanced if isinstance(enhanced, dict) else {},
    }


def _compute_resume_matching(payload: Dict[str, Any], criteria: Optional[SearchCriteria]) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    fallback_score = payload.get("match_score")
    try:
        fallback_numeric = float(fallback_score) if fallback_score is not None else 0.0
    except Exception:
        fallback_numeric = 0.0

    if not criteria:
        return {
            "score": max(0.0, fallback_numeric),
            "status": "pending",
            "missing_skills": [],
            "matched_skills": [],
            "explainability": ["Resume matching criteria unavailable"],
            "score_details": {},
            "scored_at": now_iso,
        }

    candidate = _build_candidate_for_resume_matching(payload)
    try:
        scored = unified_search_service._score_candidate(candidate, criteria)
        return {
            "score": float(scored.get("score") or 0),
            "status": "done",
            "missing_skills": scored.get("missing_skills") or [],
            "matched_skills": scored.get("matched_skills") or [],
            "explainability": scored.get("explainability") or [],
            "score_details": scored.get("score_details") or {},
            "scored_at": now_iso,
        }
    except Exception as e:
        logger.warning(f"resume matching score failed for {payload.get('candidate_id')}: {e}")
        return {
            "score": max(0.0, fallback_numeric),
            "status": "pending",
            "missing_skills": [],
            "matched_skills": [],
            "explainability": [f"Detailed matching failed: {str(e)}"],
            "score_details": {},
            "scored_at": now_iso,
        }


def _candidate_to_persist_row(job_id: str, cand: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a search-stream candidate dict into the bind params accepted by
    `sourced_candidates_storage.save_enhanced_candidate`. Keep the full
    candidate blob in `data` so we don't lose any source-specific fields
    (scores, explainability, enhanced_info, etc.)."""
    enhanced = cand.get("enhanced_info") or {}
    candidate_id = str(cand.get("candidate_id") or cand.get("id") or "").strip()
    return {
        "jobdiva_id": str(job_id),
        "candidate_id": candidate_id,
        "source": str(cand.get("source") or "Unknown"),
        "name": cand.get("name") or enhanced.get("candidate_name"),
        "email": cand.get("email") or enhanced.get("email"),
        "phone": cand.get("phone") or enhanced.get("phone"),
        "headline": cand.get("headline") or enhanced.get("job_title"),
        "location": cand.get("location") or enhanced.get("current_location"),
        "profile_url": (
            cand.get("profile_url")
            or cand.get("linkedin_url")
            or cand.get("url")
        ),
        "image_url": cand.get("image_url") or cand.get("profile_image_url"),
        "resume_id": cand.get("resume_id"),
        "resume_text": cand.get("resume_text"),
        "data": json.dumps(cand, default=str),
        "status": "sourced",
    }


@router.post("/candidates/search")
async def search_jobdiva_candidates(request: CandidateSearchRequest):
    """
    Unified candidate search with hierarchical skills/titles and intelligent resume processing.

    TIER 1: JobDiva Job Applicants with hierarchical matching
    TIER 2: TalentSearch Pool with hierarchical matching

    Features:
    - Hierarchical skill/title matching from taxonomy
    - Smart resume deduplication and extraction tracking
    - Company experience matching from resume text
    - Two-pool search strategy with prioritization
    """
    logger.info(f"🔍 Unified candidate search for job_id: {request.job_id}")

    if not request.job_id:
        return {"candidates": [], "message": "job_id required for candidate search"}

    try:
        from services.unified_candidate_search import unified_search_service, SearchCriteria

        # `title_criteria` is now the single source of truth — the legacy flat
        # `titles`/`skills` fields were removed from CandidateSearchRequest.
        combined_title_criteria = request.title_criteria or []

        # Canonicalize location: top-level `location` is authoritative.
        location = ""
        if request.location:
            location = request.location
        elif request.locations:
            location = request.locations[0].value

        effective_limit = int(request.limit or request.page_size or 100)
        require_resume = True if request.require_resume is None else bool(request.require_resume)

        # Resolve per-location radius if provided (fallback 25 miles).
        within_miles = 25
        if request.within_miles is not None and int(request.within_miles) > 0:
            within_miles = int(request.within_miles)
        elif request.locations:
            radius_match = str(request.locations[0].radius or "")
            digits = "".join(ch for ch in radius_match if ch.isdigit())
            if digits:
                within_miles = int(digits)

        companies = request.companies or []

        # Load resume match filters from database if not provided in request
        resume_match_filters = []
        if request.resume_match_filters and len(request.resume_match_filters) > 0:
            resume_match_filters = [f.dict() for f in request.resume_match_filters]
            logger.info(f"Using {len(resume_match_filters)} resume match filters from request")
        else:
            try:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT resume_match_filters FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                    (request.job_id, request.job_id)
                )
                row = cursor.fetchone()
                if row and row[0]:
                    resume_match_filters = row[0] if isinstance(row[0], list) else json.loads(row[0])
                    logger.info(f"Loaded {len(resume_match_filters)} resume match filters from database for job {request.job_id}")
                cursor.close()
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to load resume match filters from database: {e}")

        # Build search criteria — no more flat `titles`/`skills` duplication;
        # per-source search methods derive what they need via
        # SearchCriteria.sourcing_skill_values().
        criteria = SearchCriteria(
            job_id=request.job_id,
            title_criteria=[t.dict() for t in combined_title_criteria],
            skill_criteria=[s.dict() for s in (request.skill_criteria or [])],
            keywords=request.keywords or [],
            resume_match_filters=resume_match_filters,
            location=location,
            within_miles=within_miles,
            companies=companies,
            page_size=effective_limit,
            sources=request.sources or ["JobDiva"],
            open_to_work=request.open_to_work,
            boolean_string=request.boolean_string or "",
            recent_days=request.recent_days,
            require_resume=require_resume,
        )

        # Execute unified search as a stream. Persist each candidate to
        # `sourced_candidates` as it's yielded — fire-and-forget via
        # asyncio.to_thread so the sync SQLAlchemy call doesn't block the
        # event loop. We keep a reference list so pending tasks aren't
        # garbage-collected mid-flight.
        async def stream_candidates():
            persist_tasks: List[asyncio.Task] = []
            try:
                async for event in unified_search_service.search_candidates(criteria):
                    yield json.dumps(event) + "\n"
                    if event.get("type") == "candidate":
                        cand = event.get("data") or {}
                        # Auto-persistence of applicants has been disabled to ensure 
                        # only explicitly selected candidates (via Launch PAIR) are 
                        # saved to the database/Master Pool.
                        pass
            except Exception as e:
                logger.error(f"Error in search stream: {e}", exc_info=True)
                yield json.dumps({"type": "error", "message": str(e)}) + "\n"
            finally:
                # Drain persist tasks so failures surface in logs instead of
                # vanishing when the generator closes.
                if persist_tasks:
                    results = await asyncio.gather(*persist_tasks, return_exceptions=True)
                    failures = [r for r in results if isinstance(r, Exception)]
                    if failures:
                        logger.warning(
                            f"Candidate persistence: {len(failures)}/{len(results)} failed"
                        )
                    else:
                        logger.info(
                            f"Candidate persistence: saved {len(results)} rows for "
                            f"job {request.job_id}"
                        )

        return StreamingResponse(
            stream_candidates(),
            media_type="application/x-ndjson"
        )

    except Exception as e:
        logger.error(f"❌ Unified search failed: {e}")
        # Fallback to original search logic
        try:
            from services.jobdiva import JobDivaService
            from services.unified_candidate_search import unified_search_service, SearchCriteria
            jobdiva_service = JobDivaService()

            # Lightweight fallback: talent-pool search only (no applicants in Step-5).
            token = await jobdiva_service.authenticate()
            candidates = await jobdiva_service.search_candidates(
                skills=[],
                location=location,
                limit=effective_limit,
                job_id=None,
                boolean_string=request.boolean_string or "",
                recent_days=request.recent_days,
                require_resume=require_resume,
            ) if token else []
            for candidate in candidates:
                candidate["source"] = "JobDiva-TalentSearch"

            # Fix 3 (Path C): score fallback candidates so the UI doesn't render
            # every applicant at 0%. We rebuild SearchCriteria from the request
            # inline because the outer try block may have failed before
            # `criteria` was constructed.
            try:
                fallback_location = ""
                if request.location:
                    fallback_location = request.location
                elif request.locations:
                    fallback_location = request.locations[0].value

                fallback_resume_match_filters = (
                    [f.dict() for f in request.resume_match_filters]
                    if request.resume_match_filters else []
                )

                fallback_criteria = SearchCriteria(
                    job_id=request.job_id,
                    title_criteria=[t.dict() for t in (request.title_criteria or [])],
                    skill_criteria=[s.dict() for s in (request.skill_criteria or [])],
                    keywords=request.keywords or [],
                    resume_match_filters=fallback_resume_match_filters,
                    location=fallback_location,
                    within_miles=within_miles,
                    companies=request.companies or [],
                    page_size=effective_limit,
                    sources=request.sources or ["JobDiva"],
                    open_to_work=request.open_to_work,
                    boolean_string=request.boolean_string or "",
                    recent_days=request.recent_days,
                    require_resume=require_resume,
                )

                for candidate in candidates:
                    try:
                        scored = unified_search_service._score_candidate(candidate, fallback_criteria)
                        if isinstance(scored, dict):
                            score = scored.get("match_score", scored.get("score"))
                            if score is not None:
                                candidate["match_score"] = score
                            if scored.get("explainability"):
                                candidate["explainability"] = scored["explainability"]
                            if scored.get("matched_skills"):
                                candidate["matched_skills"] = scored["matched_skills"]
                    except Exception as score_err:
                        logger.debug(f"Fallback scoring skipped for one candidate: {score_err}")
            except Exception as score_setup_err:
                logger.warning(f"Fallback scoring setup failed, returning unscored: {score_setup_err}")

            return {
                "candidates": candidates[:effective_limit],
                "total": len(candidates),
                "job_applicants": 0,
                "talent_pool": len(candidates),
                "message": f"Found {len(candidates)} candidates using fallback search (unified search failed)"
            }

        except Exception as fallback_error:
            logger.error(f"❌ Fallback search also failed: {fallback_error}")
            raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")


@router.get("/candidates/jobdiva/{job_id}/criteria-status")
async def get_jobdiva_criteria_status(job_id: str):
    """Lightweight pre-check for JobDiva AI matcher criteria.

    Used by Step-5 UI to warn recruiters *before* running sourcing if
    JobDiva's JobAgent criteria are not configured for the given job.

    The underlying JobDiva flag (`criteria_unconfigured` from
    `_search_with_job_agent`) only fires when JobDiva returns 500 with the
    literal string "Criteria Not Assigned". In practice that path triggers
    rarely — most genuinely unconfigured matchers respond 200 with an
    empty `data` array. We treat "resolved jobdiva_id present but the
    matcher returned zero candidates" as the same recruiter-actionable
    state, so the Step-5 modal actually surfaces.
    """
    try:
        result = await jobdiva_service.search_via_job_agent(
            job_id=job_id,
            resume_count=1,
            require_resume=False,
        )

        resolved_jobdiva_id = result.get("resolved_jobdiva_id")
        candidate_count = len(result.get("candidates") or [])
        explicit_unconfigured = bool(result.get("criteria_unconfigured"))

        # Heuristic: matcher resolved the job but returned no candidates →
        # criteria are effectively unconfigured (or so narrow they don't
        # match anyone). Either way, the recruiter should be nudged.
        empty_matcher_response = (
            resolved_jobdiva_id is not None and candidate_count == 0
        )

        criteria_unconfigured = explicit_unconfigured or empty_matcher_response

        return {
            "status": "success",
            "job_id": str(job_id),
            "resolved_jobdiva_id": resolved_jobdiva_id,
            "criteria_unconfigured": criteria_unconfigured,
            "criteria_unconfigured_reason": (
                "explicit_jobdiva_flag" if explicit_unconfigured
                else "empty_matcher_response" if empty_matcher_response
                else None
            ),
            "candidate_count_probe": candidate_count,
        }
    except Exception as e:
        logger.error(f"criteria-status check failed for job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to check JobDiva criteria status")

@router.post("/candidates/search/legacy")
async def search_jobdiva_candidates_legacy(request: CandidateSearchRequest):
    """
    Legacy search endpoint (preserved for backward compatibility)

    Enhanced multi-criteria candidate search with separate title, skill, and location filtering.

    TIER 1: JobDiva Job Applicants filtered by titles, skills, and locations
    TIER 2: TalentSearch Pool filtered by same criteria

    Supports both legacy format (combined skills array) and enhanced format (separate criteria).
    """
    print(f"🔍 Enhanced multi-criteria search for job_id: {request.job_id}")

    if not request.job_id:
        return {"candidates": [], "message": "jobdiva_id required for candidate search"}

    try:
        combined_results = []
        applicant_count = 0
        talent_pool_count = 0

        # Parse filtering criteria from the rich title/skill/location shapes.
        # Legacy flat `titles`/`skills` lists were dropped from the request
        # model; `legacy_skills` stays as a name here purely to preserve the
        # downstream JobDiva service signature (which still expects a flat list
        # of {value, priority, years_experience}).
        title_filters = []
        skill_filters = []
        location_filters = []
        legacy_skills = []

        if request.title_criteria:
            title_filters = [t for t in request.title_criteria if t.match_type != 'exclude']
        if request.skill_criteria:
            skill_filters = [s for s in request.skill_criteria if s.match_type != 'exclude']
        if request.locations:
            location_filters = list(request.locations)

        # Derive the flat "legacy_skills" shape from skill_criteria so the old
        # JobDiva service call still works without requiring callers to send
        # the deprecated format.
        for skill in (request.skill_criteria or []):
            if skill.match_type == 'exclude':
                continue
            priority = "Must Have" if skill.match_type == 'must' else "Flexible"
            legacy_skills.append({
                "value": skill.value,
                "priority": priority,
                "years_experience": skill.years or 0,
            })

        # Use location from either enhanced or legacy format
        primary_location = ""
        if location_filters:
            primary_location = location_filters[0].value
        elif request.location:
            primary_location = request.location

        print(f"📋 Search criteria - Titles: {len(title_filters)}, Skills: {len(skill_filters)}, Locations: {len(location_filters)}")

        # TIER 1: JobDiva Job Applicants with enhanced filtering
        print("🎯 TIER 1: Searching job applicants with enhanced resume fetching...")
        try:
            # For job applicant search, use enhanced method to get full resume text
            if not title_filters and not skill_filters and not legacy_skills:
                # Simple applicant search - use enhanced method for complete data
                print("📝 Using enhanced job applicants method (no complex filters)")
                applicants = await jobdiva_service.get_enhanced_job_candidates(request.job_id)

                # Filter by location if provided
                if location_filters or primary_location:
                    filtered_applicants = []
                    search_location = location_filters[0].value if location_filters else primary_location
                    for candidate in applicants:
                        candidate_location = candidate.get("location", "").lower()
                        if search_location.lower() in candidate_location or candidate_location in search_location.lower():
                            filtered_applicants.append(candidate)
                    applicants = filtered_applicants

            else:
                # Complex criteria search - use existing enhanced filtering
                applicants = await jobdiva_service.search_job_candidates_enhanced(
                    job_id=request.job_id,
                    title_criteria=title_filters,
                    skill_criteria=skill_filters,
                    location_criteria=location_filters,
                    legacy_skills=legacy_skills  # Fallback to legacy format
                )

            applicant_count = len(applicants)
            print(f"✅ Found {applicant_count} job applicants matching criteria")

            # Mark applicants as priority source
            for candidate in applicants:
                candidate["source"] = "JobDiva-Applicants"
                candidate["priority"] = True
            combined_results.extend(applicants)

        except Exception as e:
            print(f"⚠️ Job applicants search failed: {e}")
            # Fallback to legacy search if enhanced search fails
            try:
                applicants = await jobdiva_service.search_candidates(
                    skills=legacy_skills,
                    location=primary_location,
                    job_id=request.job_id
                )
                applicant_count = len(applicants)
                for candidate in applicants:
                    candidate["source"] = "JobDiva-Applicants"
                    candidate["priority"] = True
                combined_results.extend(applicants)
                print(f"✅ Fallback search found {applicant_count} applicants")
            except Exception as fallback_e:
                print(f"❌ Fallback search also failed: {fallback_e}")
                applicant_count = 0

        # TIER 2: TalentSearch Pool with enhanced filtering
        # Only search if we need more candidates and have search criteria
        if (title_filters or skill_filters or legacy_skills) and (applicant_count < request.limit):
            print("🌐 TIER 2: Searching talent pool with multi-criteria filters...")
            try:
                remaining_limit = max(0, request.limit - applicant_count)

                talent_pool = await jobdiva_service.search_talent_pool_enhanced(
                    title_criteria=title_filters,
                    skill_criteria=skill_filters,
                    location_criteria=location_filters,
                    legacy_skills=legacy_skills,
                    page=request.page,
                    limit=remaining_limit if remaining_limit > 0 else request.limit
                )
                talent_pool_count = len(talent_pool)
                print(f"✅ Found {talent_pool_count} additional candidates from talent pool")

                # Mark talent pool as secondary source
                for candidate in talent_pool:
                    candidate["source"] = "JobDiva-TalentSearch"
                    candidate["priority"] = False
                combined_results.extend(talent_pool)

            except Exception as e:
                print(f"⚠️ Enhanced talent pool search failed: {e}")
                # Fallback to legacy talent search
                try:
                    talent_pool = await jobdiva_service.search_candidates(
                        skills=legacy_skills,
                        location=primary_location,
                        page=request.page,
                        limit=remaining_limit if remaining_limit > 0 else request.limit,
                        job_id=None
                    )
                    talent_pool_count = len(talent_pool)
                    for candidate in talent_pool:
                        candidate["source"] = "JobDiva-TalentSearch"
                        candidate["priority"] = False
                    combined_results.extend(talent_pool)
                    print(f"✅ Fallback talent search found {talent_pool_count} candidates")
                except Exception as fallback_e:
                    print(f"❌ Fallback talent search also failed: {fallback_e}")
                    talent_pool_count = 0

        # Summary
        total_found = len(combined_results)
        criteria_summary = []
        if title_filters: criteria_summary.append(f"{len(title_filters)} title criteria")
        if skill_filters: criteria_summary.append(f"{len(skill_filters)} skill criteria")
        if location_filters: criteria_summary.append(f"{len(location_filters)} location criteria")

        message = f"Found {total_found} candidates"
        if criteria_summary:
            message += f" matching {', '.join(criteria_summary)}"
        if applicant_count > 0 and talent_pool_count > 0:
            message += f" ({applicant_count} job applicants + {talent_pool_count} from talent pool)"
        elif applicant_count > 0:
            message += f" ({applicant_count} job applicants)"
        elif talent_pool_count > 0:
            message += f" ({talent_pool_count} from talent pool)"

        # Deduplicate candidates by email or name+location
        print("🔄 Deduplicating candidates...")
        seen_candidates = {}
        deduplicated_results = []

        for candidate in combined_results:
            # Create unique key based on email (preferred) or name+location
            email_key = candidate.get("email", "").lower().strip()
            name_location_key = f"{candidate.get('firstName', '').lower()}_{candidate.get('lastName', '').lower()}_{candidate.get('location', '').lower()}"

            # Use email as primary key, fallback to name+location
            unique_key = email_key if email_key else name_location_key

            if unique_key and unique_key not in seen_candidates:
                # First time seeing this candidate
                seen_candidates[unique_key] = candidate
                deduplicated_results.append(candidate)
            elif unique_key in seen_candidates:
                # Duplicate found - prefer JobDiva-Applicants over TalentSearch
                existing = seen_candidates[unique_key]
                current_source = candidate.get("source", "")
                existing_source = existing.get("source", "")

                if current_source == "JobDiva-Applicants" and existing_source != "JobDiva-Applicants":
                    # Replace with job applicant version (higher priority)
                    seen_candidates[unique_key] = candidate
                    # Replace in results list
                    for i, result in enumerate(deduplicated_results):
                        if result == existing:
                            deduplicated_results[i] = candidate
                            break

        dedup_count = len(combined_results) - len(deduplicated_results)
        if dedup_count > 0:
            print(f"🔄 Removed {dedup_count} duplicate candidates")
            message += f" (removed {dedup_count} duplicates)"

        print(f"🎯 SEARCH COMPLETE: {message}")

        return {"candidates": deduplicated_results, "message": message}

    except Exception as e:
        print(f"❌ Enhanced candidate search failed: {e}")
        return {"candidates": [], "message": f"Search failed: {str(e)}"}

@router.post("/candidates/message")
async def message_candidate(request: CandidateMessageRequest):
    """
    Sends a message to a candidate via the specified source provider.
    Currently supports: LinkedIn (via Unipile).
    """
    if request.source == "LinkedIn":
        success = await unipile_service.send_message(request.candidate_provider_id, request.message)
        if success:
            return {"status": "success", "detail": "Message queued/sent via LinkedIn"}
        else:
            raise HTTPException(status_code=500, detail="Failed to send LinkedIn message")

    elif request.source in ["JobDiva", "VettedDB", "Email"]:
        # Mock Email Send (Log it)
        print(f"📧 EMAIL OUTREACH: Sending email to candidate {request.candidate_provider_id}")
        print(f"📧 Subject: (Auto-generated)")
        print(f"📧 Body: {request.message}")
        return {"status": "success", "detail": f"Email simulation successful for {request.source}"}

    else:
        raise HTTPException(status_code=400, detail=f"Messaging not supported for source: {request.source}")

@router.get("/jobs/{job_id_or_ref}/candidates")
async def get_job_candidates(job_id_or_ref: str):
    """
    Fetches all sourced candidates tied to a specific job.
    Supports both numeric job_id and reference jobdiva_id.
    """
    try:
        from psycopg2.extras import RealDictCursor

        # Resolve the alphanumeric jobdiva_id (e.g. '26-05172') from monitored_jobs.
        # sourced_candidates.jobdiva_id must always store the alphanumeric ref, NOT the numeric PK.
        resolved_jobdiva_id = job_id_or_ref  # fallback: use whatever was passed
        resolved_numeric_job_id = job_id_or_ref
        # v22: connect_timeout=5 → slow/unreachable DB fails fast instead of
        # hanging worker for TCP default (~2 min).
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT jobdiva_id, job_id FROM monitored_jobs
                    WHERE job_id = %s OR jobdiva_id = %s
                    LIMIT 1
                """, (job_id_or_ref, job_id_or_ref))
                result = cur.fetchone()
                resolved_jobdiva_id = job_id_or_ref
                resolved_numeric_id = job_id_or_ref
                if result:
                    resolved_jobdiva_id = result[0] or result[1]
                    resolved_numeric_job_id = result[1] or result[0]
        finally:
            conn.close()

        # Query sourced_candidates using the resolved alphanumeric jobdiva_id
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    WITH latest_audit AS (
                        SELECT DISTINCT ON (candidate_id)
                            candidate_id,
                            status,
                            interview_id,
                            created_at
                        FROM engage_interview_audit
                        WHERE (job_id = %s OR job_id = %s)
                        ORDER BY candidate_id, id DESC
                    )
                    SELECT
                        sc.id,
                        sc.jobdiva_id,
                        sc.candidate_id,
                        sc.name,
                        sc.email,
                        sc.phone,
                        sc.headline,
                        sc.location,
                        sc.source,
                        sc.profile_url,
                        sc.image_url,
                        sc.resume_match_percentage as match_score,
                        sc.created_at,
                        sc.data,
                        la.status as audit_status,
                        la.interview_id as audit_interview_id,
                        la.created_at as audit_created_at
                    FROM sourced_candidates sc
                    LEFT JOIN latest_audit la
                        ON la.candidate_id = sc.candidate_id

                    WHERE sc.jobdiva_id = %s
                    ORDER BY sc.created_at DESC;
                """, (str(resolved_jobdiva_id), str(resolved_numeric_job_id), resolved_jobdiva_id,))

                candidates = cur.fetchall()
        finally:
            conn.close()

        # Handle the data field (it might be a string or a dict)
        for cand in candidates:
            if cand.get("data") and isinstance(cand["data"], str):
                try:
                    cand["data"] = json.loads(cand["data"])
                except:
                    pass

            data_blob = cand.get("data") if isinstance(cand.get("data"), dict) else {}
            # Promote engage values from JSONB blob to top-level response fields.
            # These are persisted by engagement sync endpoints in sourced_candidates.data.
            if isinstance(data_blob, dict):
                if data_blob.get("engage_status"):
                    cand["engage_status"] = data_blob.get("engage_status")
                if data_blob.get("engage_interview_id"):
                    cand["engage_interview_id"] = data_blob.get("engage_interview_id")
                if data_blob.get("engage_score") is not None:
                    cand["engage_score"] = data_blob.get("engage_score")
                if data_blob.get("engage_candidate_score") is not None:
                    cand["engage_candidate_score"] = data_blob.get("engage_candidate_score")
                if data_blob.get("engage_total_score") is not None:
                    cand["engage_total_score"] = data_blob.get("engage_total_score")
                if data_blob.get("engage_hard_filter_status"):
                    cand["engage_hard_filter_status"] = data_blob.get("engage_hard_filter_status")
                if data_blob.get("engage_completed_at"):
                    cand["engage_completed_at"] = data_blob.get("engage_completed_at")

            # Read-side fallback: audit table is authoritative when candidate
            # blob doesn't yet have engage status/interview id.
            if not cand.get("engage_status") and cand.get("audit_status"):
                cand["engage_status"] = cand.get("audit_status")
                if isinstance(data_blob, dict):
                    data_blob["engage_status"] = cand.get("audit_status")

            if not cand.get("engage_interview_id") and cand.get("audit_interview_id"):
                cand["engage_interview_id"] = cand.get("audit_interview_id")
                if isinstance(data_blob, dict):
                    data_blob["engage_interview_id"] = cand.get("audit_interview_id")

            if not cand.get("engage_created_at") and cand.get("audit_created_at"):
                cand["engage_created_at"] = cand.get("audit_created_at")

            if isinstance(data_blob, dict):
                cand["data"] = data_blob

            # Hide internal join-only fields from API response payload.
            cand.pop("audit_status", None)
            cand.pop("audit_interview_id", None)
            cand.pop("audit_created_at", None)

        return {"status": "success", "candidates": candidates}
    except Exception as e:
        logger.error(f"Error fetching job candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class RefreshResumeMatchRequest(BaseModel):
    source: Optional[str] = None


@router.post("/jobs/{job_id_or_ref}/candidates/{candidate_id}/refresh-resume-match")
async def refresh_candidate_resume_match(
    job_id_or_ref: str,
    candidate_id: str,
    request: RefreshResumeMatchRequest,
):
    """Re-run resume matching for a single candidate and persist score details."""
    try:
        from psycopg2.extras import RealDictCursor

        resolved_jobdiva_id = job_id_or_ref
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT jobdiva_id, job_id
                    FROM monitored_jobs
                    WHERE job_id = %s OR jobdiva_id = %s
                    LIMIT 1
                    """,
                    (job_id_or_ref, job_id_or_ref),
                )
                job_row = cur.fetchone()
                if job_row:
                    resolved_jobdiva_id = job_row[0] or job_row[1] or job_id_or_ref
        finally:
            conn.close()

        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if request.source:
                    cur.execute(
                        """
                        SELECT *
                        FROM sourced_candidates
                        WHERE jobdiva_id = %s
                          AND candidate_id = %s
                          AND source = %s
                        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                        LIMIT 1
                        """,
                        (resolved_jobdiva_id, candidate_id, request.source),
                    )
                else:
                    cur.execute(
                        """
                        SELECT *
                        FROM sourced_candidates
                        WHERE jobdiva_id = %s
                          AND candidate_id = %s
                        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST
                        LIMIT 1
                        """,
                        (resolved_jobdiva_id, candidate_id),
                    )
                candidate_row = cur.fetchone()

                if not candidate_row:
                    raise HTTPException(status_code=404, detail="Candidate not found for this job")

                row_data = dict(candidate_row)
                data_blob = _json_load_safe(row_data.get("data"), {})

                scoring_payload = {
                    "candidate_id": row_data.get("candidate_id"),
                    "name": row_data.get("name"),
                    "headline": row_data.get("headline"),
                    "location": row_data.get("location"),
                    "resume_text": row_data.get("resume_text") or data_blob.get("resume_text") or "",
                    "skills": data_blob.get("skills") or [],
                    "experience_years": data_blob.get("experience_years") or 0,
                    "enhanced_info": data_blob.get("enhanced_info") or {},
                    "data": data_blob,
                    "match_score": row_data.get("resume_match_percentage") or data_blob.get("match_score") or 0,
                }

                criteria = _build_resume_matching_criteria(str(resolved_jobdiva_id))
                refreshed = _compute_resume_matching(scoring_payload, criteria)

                data_blob["match_score"] = refreshed["score"]
                data_blob["resume_matching_score"] = refreshed["score"]
                data_blob["resume_matching_status"] = refreshed["status"]
                data_blob["resume_matching_scored_at"] = refreshed["scored_at"]
                data_blob["match_score_details"] = refreshed["score_details"]
                data_blob["matched_skills"] = refreshed["matched_skills"]
                data_blob["missing_skills"] = refreshed["missing_skills"]
                data_blob["explainability"] = refreshed["explainability"]

                cur.execute(
                    """
                    UPDATE sourced_candidates
                    SET resume_match_percentage = %s,
                        data = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (refreshed["score"], json.dumps(data_blob), row_data.get("id")),
                )
            conn.commit()
        finally:
            conn.close()

        return {
            "status": "success",
            "candidate_id": candidate_id,
            "jobdiva_id": str(resolved_jobdiva_id),
            "score": refreshed["score"],
            "resume_matching_status": refreshed["status"],
            "resume_matching_scored_at": refreshed["scored_at"],
            "matched_skills": refreshed["matched_skills"],
            "missing_skills": refreshed["missing_skills"],
            "match_score_details": refreshed["score_details"],
            "explainability": refreshed["explainability"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"refresh_candidate_resume_match failed for {candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/candidates/save")
async def save_candidates(request: CandidatesSaveRequest):
    """
    Saves a batch of candidates to the sourced_candidates table.
    Always stores the alphanumeric jobdiva_id (e.g. '26-05172'), never the numeric job_id PK.
    """
    try:
        print(f"🔄 Saving {len(request.candidates)} candidates for job: {request.jobdiva_id}")

        # Resolve the true alphanumeric jobdiva_id from monitored_jobs.
        # The frontend may send the numeric job_id; we always normalise to the alphanumeric ref.
        resolved_jobdiva_id = request.jobdiva_id  # safe fallback
        try:
            _conn = get_db_connection()
            try:
                with _conn.cursor() as _cur:
                    _cur.execute("""
                        SELECT jobdiva_id, job_id FROM monitored_jobs
                        WHERE job_id = %s OR jobdiva_id = %s
                        LIMIT 1
                    """, (request.jobdiva_id, request.jobdiva_id))
                    _row = _cur.fetchone()
                    if _row:
                        # jobdiva_id (alphanumeric) preferred; fall back to job_id if NULL
                        resolved_jobdiva_id = _row[0] or _row[1]
            finally:
                _conn.close()
        except Exception as _resolve_err:
            print(f"⚠️ Could not resolve jobdiva_id, using as-is: {_resolve_err}")

        print(f"✅ Resolved jobdiva_id: {request.jobdiva_id!r} → {resolved_jobdiva_id!r}")

        # Filter only selected candidates for saving
        selected_candidates = [c for c in request.candidates if c.is_selected]
        print(f"📝 Saving {len(selected_candidates)} selected candidates out of {len(request.candidates)} total")

        for idx, c in enumerate(selected_candidates):
            print(f"   Selected Candidate {idx+1}: {c.name} (ID: {c.candidate_id}, Source: {c.source})")

        import json

        saved_count = 0
        processing_payloads = []
        scoring_criteria = _build_resume_matching_criteria(str(resolved_jobdiva_id))

        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                for c in selected_candidates:
                    try:
                        incoming_match_score: Optional[float] = None
                        try:
                            raw_incoming_score = getattr(c, 'match_score', None)
                            if raw_incoming_score is not None:
                                parsed_incoming_score = float(raw_incoming_score)
                                if parsed_incoming_score >= 0:
                                    incoming_match_score = parsed_incoming_score
                        except Exception:
                            incoming_match_score = None

                        raw_urls = getattr(c, 'urls', {})
                        if not isinstance(raw_urls, dict):
                            raw_urls = {}

                        profile_url = (
                            getattr(c, 'profile_url', None)
                            or getattr(c, 'linkedin_url', None)
                            or raw_urls.get('linkedin')
                            or raw_urls.get('linkedin_url')
                            or raw_urls.get('profile')
                            or None
                        )

                        urls_payload = dict(raw_urls)
                        if profile_url:
                            urls_payload.setdefault('linkedin', profile_url)
                            urls_payload.setdefault('linkedin_url', profile_url)

                        pre_score_payload = {
                            "candidate_id": c.candidate_id,
                            "name": c.name,
                            "headline": getattr(c, 'headline', None) or getattr(c, 'title', None),
                            "location": getattr(c, 'location', None),
                            "resume_text": getattr(c, 'resume_text', None),
                            "skills": c.skills or [],
                            "experience_years": c.experience_years or 0,
                            "enhanced_info": getattr(c, 'enhanced_info', None),
                            "data": {
                                "skills": c.skills or [],
                                "experience_years": c.experience_years or 0,
                                "education": getattr(c, 'education', []) or getattr(c, 'candidate_education', []),
                                "certifications": getattr(c, 'certifications', []) or getattr(c, 'candidate_certification', []),
                                "company_experience": getattr(c, 'company_experience', []),
                                "urls": urls_payload,
                                "enhanced_info": getattr(c, 'enhanced_info', None),
                            },
                            "match_score": getattr(c, 'match_score', 0),
                        }
                        if incoming_match_score is not None:
                            # Canonical score from Step-5 sourcing UI. Keep this
                            # value stable across Launch PAIR -> Rank List.
                            scoring = {
                                "score": incoming_match_score,
                                "status": "done",
                                "missing_skills": [],
                                "matched_skills": [],
                                "explainability": ["Score preserved from Step-5 sourcing"],
                                "score_details": {},
                                "scored_at": datetime.now(timezone.utc).isoformat(),
                            }
                        else:
                            scoring = _compute_resume_matching(pre_score_payload, scoring_criteria)

                        # Prepare candidate data with clean schema
                        candidate_data = {
                            "jobdiva_id": resolved_jobdiva_id,
                            "candidate_id": c.candidate_id,
                            "source": c.source,
                            "name": c.name,
                            "email": getattr(c, 'email', None),
                            "phone": getattr(c, 'phone', None),
                            "headline": getattr(c, 'headline', None) or getattr(c, 'title', None),
                            "location": getattr(c, 'location', None),
                            "profile_url": profile_url,
                            "image_url": getattr(c, 'image_url', None),
                            "resume_id": getattr(c, 'resume_id', None),
                            "resume_text": getattr(c, 'resume_text', None),
                            "resume_match_percentage": scoring["score"],
                            "data": json.dumps({
                                "skills": c.skills or [],
                                "experience_years": c.experience_years or 0,
                                "education": getattr(c, 'education', []) or getattr(c, 'candidate_education', []),
                                "certifications": getattr(c, 'certifications', []) or getattr(c, 'candidate_certification', []),
                                "company_experience": getattr(c, 'company_experience', []),
                                "urls": urls_payload,
                                "is_selected": True,
                                "score_locked_from_step5": incoming_match_score is not None,
                                "match_score": scoring["score"],
                                "resume_matching_score": scoring["score"],
                                "resume_matching_status": scoring["status"],
                                "resume_matching_scored_at": scoring["scored_at"],
                                "match_score_details": scoring["score_details"],
                                "matched_skills": scoring["matched_skills"],
                                "missing_skills": scoring["missing_skills"],
                                "explainability": scoring["explainability"],
                                "enhanced_info": getattr(c, 'enhanced_info', None)  # Full LLM extraction data
                            }),
                            "status": "sourced"
                        }

                        cur.execute("""
                            INSERT INTO sourced_candidates (
                                jobdiva_id, candidate_id, source, name, email, phone, headline, location,
                                profile_url, image_url, resume_id, resume_text, data, status, updated_at
                                , resume_match_percentage
                            ) VALUES (
                                %(jobdiva_id)s, %(candidate_id)s, %(source)s, %(name)s, %(email)s, %(phone)s, %(headline)s, %(location)s,
                                %(profile_url)s, %(image_url)s, %(resume_id)s, %(resume_text)s, %(data)s, %(status)s, CURRENT_TIMESTAMP,
                                %(resume_match_percentage)s
                            )
                            ON CONFLICT (jobdiva_id, candidate_id, source) DO UPDATE SET
                                name = EXCLUDED.name,
                                email = EXCLUDED.email,
                                phone = EXCLUDED.phone,
                                headline = EXCLUDED.headline,
                                location = EXCLUDED.location,
                                profile_url = EXCLUDED.profile_url,
                                image_url = EXCLUDED.image_url,
                                resume_id = EXCLUDED.resume_id,
                                resume_text = EXCLUDED.resume_text,
                                resume_match_percentage = EXCLUDED.resume_match_percentage,
                                data = EXCLUDED.data,
                                status = EXCLUDED.status,
                                updated_at = CURRENT_TIMESTAMP
                        """, candidate_data)

                        saved_count += 1
                        processing_payloads.append(candidate_data)

                    except Exception as e:
                        print(f"❌ Error saving candidate {c.candidate_id}: {e}")
                        continue

            conn.commit()
        finally:
            conn.close()

        print(f"✅ Successfully saved {saved_count} sourced candidates to database")
        enhanced_count = 0
        if processing_payloads:
            from services.sourced_candidates_storage import process_jobdiva_candidate, process_linkedin_candidate

            for payload in processing_payloads:
                try:
                    source = str(payload.get("source", ""))

                    # Handle JobDiva candidates
                    if source.startswith("JobDiva") and not payload.get("resume_text"):
                        resume_data = await jobdiva_service.get_candidate_resume(
                            payload["candidate_id"],
                            resume_id=payload.get("resume_id"),
                        )
                        resume_text = (resume_data or {}).get("resume_text", "")
                        if resume_text and "Resume content unavailable" not in resume_text:
                            payload.update({
                                "resume_text": resume_text,
                                "resume_id": (resume_data or {}).get("resume_id") or payload.get("resume_id"),
                                "email": payload.get("email") or (resume_data or {}).get("email"),
                                "phone": payload.get("phone") or (resume_data or {}).get("phone"),
                                "headline": payload.get("headline") or (resume_data or {}).get("title"),
                                "location": payload.get("location") or (resume_data or {}).get("location"),
                            })

                    # Process JobDiva candidates with resume text
                    if payload.get("resume_text") and source.startswith("JobDiva"):
                        extract_result = await process_jobdiva_candidate(payload)
                        if isinstance(extract_result, dict):
                            payload["enhanced_info"] = extract_result.get("raw")
                        enhanced_count += 1
                    # Process LinkedIn candidates
                    elif source == "LinkedIn":
                        extract_result = await process_linkedin_candidate(payload)
                        if isinstance(extract_result, dict):
                            payload["enhanced_info"] = extract_result.get("raw")
                        enhanced_count += 1

                    # Re-score after enrichment pass so rank-list gets detailed
                    # resume-matching status/score from the latest profile data.
                    data_blob = _json_load_safe(payload.get("data"), {})
                    if data_blob.get("score_locked_from_step5"):
                        # Preserve Launch PAIR score selected in Step-5.
                        # Do not overwrite with a second backend recomputation.
                        continue

                    detailed_scoring = _compute_resume_matching(payload, scoring_criteria)
                    data_blob["match_score"] = detailed_scoring["score"]
                    data_blob["resume_matching_score"] = detailed_scoring["score"]
                    data_blob["resume_matching_status"] = detailed_scoring["status"]
                    data_blob["resume_matching_scored_at"] = detailed_scoring["scored_at"]
                    data_blob["match_score_details"] = detailed_scoring["score_details"]
                    data_blob["matched_skills"] = detailed_scoring["matched_skills"]
                    data_blob["missing_skills"] = detailed_scoring["missing_skills"]
                    data_blob["explainability"] = detailed_scoring["explainability"]

                    _conn = get_db_connection()
                    try:
                        with _conn.cursor() as _cur:
                            _cur.execute(
                                """
                                UPDATE sourced_candidates
                                SET resume_match_percentage = %s,
                                    data = %s,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE jobdiva_id = %s
                                  AND candidate_id = %s
                                  AND source = %s
                                """,
                                (
                                    detailed_scoring["score"],
                                    json.dumps(data_blob),
                                    payload.get("jobdiva_id"),
                                    payload.get("candidate_id"),
                                    payload.get("source"),
                                ),
                            )
                        _conn.commit()
                    finally:
                        _conn.close()
                except Exception as e:
                    print(f"⚠️ Enhanced processing failed for candidate {payload.get('candidate_id')}: {e}")

        return {
            "status": "success",
            "detail": f"Saved {saved_count} sourced candidates",
            "saved_count": saved_count,
            "enhanced_count": enhanced_count
        }

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error saving candidates: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Lightweight phone update. Called from the sourcing UI when a recruiter fills
# in a missing number inline. Normalises to digits + optional leading '+' and
# requires at least 7 digits — PAIR's bulk-interviews API rejects clearly
# malformed numbers, which is the root cause of most "launch pair failed"
# errors for candidates sourced from Unipile/Exa (neither returns phone).
class UpdateCandidatePhoneRequest(BaseModel):
    phone: str
    jobdiva_id: Optional[str] = None


class EnrichCandidateContactRequest(BaseModel):
    candidate_id: Optional[str] = None
    jobdiva_id: Optional[str] = None
    source: Optional[str] = None
    linkedin_url: Optional[str] = None
    full_name: Optional[str] = None
    company_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None


def _normalise_phone(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    plus = "+" if raw.startswith("+") else ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"{plus}{digits}" if digits else ""


def _mask_phone_for_log(raw: str) -> str:
    normalised = _normalise_phone(raw)
    digits = "".join(ch for ch in normalised if ch.isdigit())
    if not digits:
        return ""
    if len(digits) <= 4:
        return f"***{digits}"
    return f"***{digits[-4:]}"


def _mask_email_for_log(raw: str) -> str:
    value = (raw or "").strip().lower()
    if "@" not in value:
        return ""
    local, domain = value.split("@", 1)
    if not local:
        return f"***@{domain}"
    return f"{local[:1]}***@{domain}"


def _extract_enrichment_fields(payload: Any) -> Dict[str, str]:
    targets = {"workPhone", "mobilePhone", "workEmail", "personalEmail"}
    targets_lower = {t.lower(): t for t in targets}
    found: Dict[str, str] = {}

    def walk(node: Any):
        if isinstance(node, dict):
            # Shape A: {"fieldName":"mobilePhone","value":"+1..."}
            field_name = node.get("fieldName")
            field_value = node.get("value")
            if isinstance(field_name, str) and isinstance(field_value, str) and field_value.strip():
                canonical = targets_lower.get(field_name.strip().lower())
                if canonical and canonical not in found:
                    found[canonical] = field_value.strip()

            for k, v in node.items():
                # Shape B/C: direct key-value, possibly different case
                canonical = targets_lower.get(str(k).strip().lower())
                if canonical and isinstance(v, str) and v.strip() and canonical not in found:
                    found[canonical] = v.strip()
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _split_name(raw: str) -> Dict[str, str]:
    parts = [p for p in str(raw or "").strip().split() if p]
    if not parts:
        return {"first": "", "last": ""}
    if len(parts) == 1:
        return {"first": parts[0], "last": ""}
    return {"first": parts[0], "last": " ".join(parts[1:])}


def _name_from_linkedin_url(linkedin_url: str) -> str:
    """Extract a best-effort human name from linkedin.com/in/<slug>."""
    try:
        url = str(linkedin_url or "").strip()
        if not url:
            return ""
        m = re.search(r"linkedin\.com/in/([^/?#]+)", url, flags=re.IGNORECASE)
        if not m:
            return ""
        slug = m.group(1)
        slug = re.sub(r"[-_]*[0-9a-f]{6,}$", "", slug, flags=re.IGNORECASE)
        parts = [p for p in re.split(r"[-_]", slug) if p and p.isalpha()]
        if len(parts) < 2:
            return ""
        return " ".join(p.capitalize() for p in parts[:3])
    except Exception:
        return ""


def _extract_new_zoominfo_contact_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    """Parse ZoomInfo new Data API contact enrich response into our canonical fields."""
    data = payload.get("data") or []
    first = data[0] if isinstance(data, list) and data else {}
    attrs = first.get("attributes") if isinstance(first, dict) else {}
    if not isinstance(attrs, dict):
        attrs = {}

    email_alt = attrs.get("emailAlt")
    alt_email = ""
    if isinstance(email_alt, list):
        for item in email_alt:
            if isinstance(item, dict):
                candidate = str(item.get("value") or "").strip()
                if candidate:
                    alt_email = candidate
                    break

    return {
        "mobilePhone": str(attrs.get("mobilePhone") or attrs.get("mobilePhoneAlt") or "").strip(),
        "workPhone": str(attrs.get("phone") or attrs.get("directPhone") or attrs.get("directPhoneAlt") or "").strip(),
        "workEmail": str(attrs.get("email") or "").strip(),
        "personalEmail": alt_email,
    }


APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/people/enrich"
# TODO: Move to env var (e.g., APOLLO_API_KEY) after initial rollout.
APOLLO_API_KEY = "cB7rogHZj4XRrhnTEqTlXQ"


def _extract_apollo_contact_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    person = payload.get("person") if isinstance(payload, dict) else {}
    if not isinstance(person, dict):
        person = {}

    def _first_non_empty(*values: Any) -> str:
        for value in values:
            candidate = str(value or "").strip()
            if candidate:
                return candidate
        return ""

    def _extract_phone_value(item: Any) -> str:
        if isinstance(item, str):
            return str(item).strip()
        if isinstance(item, dict):
            return _first_non_empty(
                item.get("sanitized_number"),
                item.get("raw_number"),
                item.get("number"),
                item.get("value"),
            )
        return ""

    phone_candidates: List[str] = []
    seen_phone_candidates = set()

    def _add_phone_candidate(raw_phone: Any):
        candidate = _normalise_phone(str(raw_phone or "").strip())
        if not candidate:
            return
        if sum(1 for ch in candidate if ch.isdigit()) < 7:
            return
        if candidate in seen_phone_candidates:
            return
        seen_phone_candidates.add(candidate)
        phone_candidates.append(candidate)

    work_email = _first_non_empty(person.get("email"), person.get("work_email"))

    personal_email = ""
    personal_emails = person.get("personal_emails")
    if isinstance(personal_emails, list):
        for item in personal_emails:
            candidate = _first_non_empty(
                item.get("email") if isinstance(item, dict) else None,
                item,
            )
            if candidate:
                personal_email = candidate
                break

    mobile_phone = _first_non_empty(
        person.get("mobile_phone"),
        person.get("mobile"),
        person.get("cell_phone"),
        person.get("cell"),
    )
    work_phone = _first_non_empty(
        person.get("sanitized_phone"),
        person.get("work_phone"),
        person.get("organization_phone"),
        person.get("direct_phone"),
        person.get("office_phone"),
        person.get("home_phone"),
        person.get("phone"),
        person.get("phone_number"),
    )

    _add_phone_candidate(mobile_phone)
    _add_phone_candidate(work_phone)

    phone_numbers = person.get("phone_numbers")
    if isinstance(phone_numbers, list):
        for item in phone_numbers:
            number = _extract_phone_value(item)
            if not number:
                continue

            ptype = str(item.get("type") or "").strip().lower() if isinstance(item, dict) else ""
            if not mobile_phone and ptype in {"mobile", "cell", "cellphone"}:
                mobile_phone = number
            elif not work_phone and ptype in {"work", "office", "direct"}:
                work_phone = number
            elif not work_phone:
                work_phone = number
            _add_phone_candidate(number)

    person_organization = person.get("organization")
    if isinstance(person_organization, dict):
        for key in ("phone", "phone_number", "sanitized_phone", "work_phone", "main_phone", "direct_phone"):
            _add_phone_candidate(person_organization.get(key))

    payload_organization = payload.get("organization") if isinstance(payload, dict) else None
    if isinstance(payload_organization, dict):
        for key in ("phone", "phone_number", "sanitized_phone", "work_phone", "main_phone", "direct_phone"):
            _add_phone_candidate(payload_organization.get(key))

    if not mobile_phone:
        mobile_phone = _extract_phone_value(payload.get("mobile_phone"))
    if not work_phone:
        work_phone = _extract_phone_value(payload.get("phone"))

    _add_phone_candidate(payload.get("mobile_phone") if isinstance(payload, dict) else "")
    _add_phone_candidate(payload.get("phone") if isinstance(payload, dict) else "")
    _add_phone_candidate(payload.get("phone_number") if isinstance(payload, dict) else "")

    if not mobile_phone and phone_candidates:
        mobile_phone = phone_candidates[0]
    if not work_phone and len(phone_candidates) > 1:
        work_phone = phone_candidates[1]
    elif not work_phone and phone_candidates:
        work_phone = phone_candidates[0]

    return {
        "mobilePhone": mobile_phone,
        "workPhone": work_phone,
        "workEmail": work_email,
        "personalEmail": personal_email,
        "phoneCandidates": phone_candidates,
    }


async def _apollo_enrich_by_linkedin(candidate_id: str, linkedin_url: str) -> Dict[str, Any]:
    if not APOLLO_API_KEY or APOLLO_API_KEY == "PASTE_APOLLO_API_KEY_HERE":
        logger.warning("Apollo fallback skipped for %s: API key not configured", candidate_id)
        return {"ok": False, "message": "Apollo API key not configured"}

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": APOLLO_API_KEY,
    }
    payload = {"linkedin_url": linkedin_url}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            ares = await client.post(APOLLO_ENRICH_URL, headers=headers, json=payload)
    except Exception as e:
        logger.warning("Apollo fallback request failed for %s: %s", candidate_id, e)
        return {"ok": False, "message": f"Apollo request failed: {str(e)}"}

    if ares.status_code >= 400:
        logger.warning(
            "Apollo fallback non-2xx for %s: %s %s",
            candidate_id,
            ares.status_code,
            ares.text[:300],
        )
        return {"ok": False, "message": f"Apollo API error ({ares.status_code})"}

    try:
        apollo_data = ares.json()
    except Exception:
        apollo_data = {"raw": ares.text}

    extracted = _extract_apollo_contact_fields(apollo_data)
    return {"ok": True, "fields": extracted}


async def _enrich_candidate_contact_impl(candidate_id: str, request: EnrichCandidateContactRequest):
    """
    Enrich candidate contact details from ZoomInfo using LinkedIn URL.
    If sourced_candidates rows already exist, updates phone/email + data blob.
    """
    from psycopg2.extras import RealDictCursor
    from core.config import (
        ZOOMINFO_ENRICH_URL,
        ZOOMINFO_BEARER_TOKEN,
        ZOOMINFO_CLIENT_ID,
    )
    zoominfo_new_enrich_url = "https://api.zoominfo.com/gtm/data/v1/contacts/enrich"

    linkedin_url = (request.linkedin_url or "").strip()
    existing_rows: List[Dict[str, Any]] = []

    # If linkedin_url not passed, try to infer it from sourced_candidates.profile_url.
    try:
        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                query = """
                    SELECT id, candidate_id, jobdiva_id, source, name, headline, profile_url, email, phone, data
                    FROM sourced_candidates
                    WHERE candidate_id = %s
                """
                params: List[Any] = [candidate_id]
                if request.jobdiva_id:
                    query += " AND jobdiva_id = %s"
                    params.append(request.jobdiva_id)
                if request.source:
                    query += " AND source = %s"
                    params.append(request.source)
                query += " ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST"
                cur.execute(query, tuple(params))
                fetched = cur.fetchall() or []
                existing_rows = [dict(r) for r in fetched]

                if not linkedin_url:
                    for r in existing_rows:
                        candidate_profile = (r.get("profile_url") or "").strip()
                        if candidate_profile:
                            linkedin_url = candidate_profile
                            break
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"enrich_contact prefetch failed for {candidate_id}: {e}")

    if not linkedin_url:
        return {
            "status": "error",
            "candidate_id": candidate_id,
            "message": "LinkedIn URL not available for enrichment",
            "updated_rows": 0,
        }

    provider_used = "zoominfo"
    extracted: Dict[str, Any] = {}
    zres: Optional[httpx.Response] = None
    zoominfo_data: Dict[str, Any] = {}

    if not ZOOMINFO_BEARER_TOKEN:
        logger.warning("ZoomInfo token missing for %s, attempting Apollo fallback", candidate_id)
        apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
        if apollo_result.get("ok"):
            provider_used = "apollo"
            extracted = apollo_result.get("fields") or {}
            logger.info("Apollo fallback succeeded for %s when ZoomInfo token missing", candidate_id)
        else:
            raise HTTPException(status_code=500, detail="ZOOMINFO_BEARER_TOKEN is not configured and Apollo fallback failed")

    headers = {
        "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }
    if ZOOMINFO_CLIENT_ID:
        headers["X-Client-Id"] = ZOOMINFO_CLIENT_ID

    zoominfo_payload = {
        "inputFields": [
            {
                "fieldName": "linkedinUrl",
                "fieldType": "String",
                "value": linkedin_url,
            }
        ],
        "outputFields": ["workPhone", "mobilePhone", "workEmail", "personalEmail"],
    }

    async def _zoominfo_crossfill_from_email_or_phone(seed_email: str, seed_phone: str) -> Dict[str, str]:
        """If we only have email or phone, query ZoomInfo new API to fetch the missing side."""
        if not ZOOMINFO_BEARER_TOKEN:
            return {}

        normalised_email = str(seed_email or "").strip().lower()
        normalised_phone = _normalise_phone(seed_phone or "")
        if sum(1 for ch in normalised_phone if ch.isdigit()) < 7:
            normalised_phone = ""

        match_person_input: Dict[str, Any] = {}
        if normalised_email:
            match_person_input["emailAddress"] = normalised_email
        elif normalised_phone:
            match_person_input["phone"] = normalised_phone
        else:
            return {}

        headers = {
            "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
            "accept": "application/vnd.api+json",
            "content-type": "application/vnd.api+json",
        }
        payload = {
            "data": {
                "type": "ContactEnrich",
                "attributes": {
                    "matchPersonInput": [match_person_input],
                    "outputFields": ["mobilePhone", "phone", "email", "emailAlt"],
                },
            }
        }

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                res = await client.post(zoominfo_new_enrich_url, headers=headers, json=payload)
        except Exception as e:
            logger.warning("ZoomInfo cross-fill request failed for %s: %s", candidate_id, e)
            return {}

        if res.status_code >= 400:
            logger.info(
                "ZoomInfo cross-fill non-2xx for %s: %s",
                candidate_id,
                res.status_code,
            )
            return {}

        try:
            payload_data = res.json()
        except Exception:
            return {}

        return _extract_new_zoominfo_contact_fields(payload_data)

    if provider_used == "zoominfo":
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                zres = await client.post(ZOOMINFO_ENRICH_URL, headers=headers, json=zoominfo_payload)
        except Exception as e:
            logger.error(f"ZoomInfo enrich request failed for {candidate_id}: {e}")
            apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
            if apollo_result.get("ok"):
                provider_used = "apollo"
                extracted = apollo_result.get("fields") or {}
                logger.info("Apollo fallback succeeded for %s after ZoomInfo request failure", candidate_id)
            else:
                raise HTTPException(status_code=502, detail=f"ZoomInfo request failed: {str(e)}")

    if provider_used == "zoominfo" and zres is not None:
        response_text = zres.text
        try:
            zoominfo_data = zres.json()
        except Exception:
            zoominfo_data = {"raw": response_text}
    else:
        response_text = ""

    if provider_used == "zoominfo" and zres.status_code == 401:
        # Legacy endpoint rejected this token. Fallback to the new OAuth Data API.
        row0 = existing_rows[0] if existing_rows else {}
        row_data = _json_load_safe(row0.get("data"), {}) if isinstance(row0, dict) else {}
        row_enhanced = row_data.get("enhanced_info") if isinstance(row_data, dict) else {}
        if not isinstance(row_enhanced, dict):
            row_enhanced = {}

        full_name = (request.full_name or row0.get("name") or "").strip()
        company_name = (
            request.company_name
            or row_data.get("company_name")
            or row_data.get("company")
            or row_enhanced.get("current_company")
            or row_enhanced.get("company")
            or ""
        )
        company_name = str(company_name or "").strip()

        fallback_email = (request.email or row0.get("email") or "").strip()
        fallback_phone = _normalise_phone(request.phone or row0.get("phone") or "")

        match_person_input: Dict[str, Any] = {}
        if fallback_email:
            match_person_input["emailAddress"] = fallback_email
        elif fallback_phone and sum(1 for ch in fallback_phone if ch.isdigit()) >= 7:
            match_person_input["phone"] = fallback_phone
        elif full_name and company_name:
            split = _split_name(full_name)
            if split["first"] and split["last"]:
                match_person_input["firstName"] = split["first"]
                match_person_input["lastName"] = split["last"]
                match_person_input["companyName"] = company_name
            else:
                match_person_input["fullName"] = full_name
                match_person_input["companyName"] = company_name

        if not match_person_input:
            # Last-resort fallback: search by name and enrich by personId.
            # Useful when we only have a LinkedIn URL and no persisted company/email/phone yet.
            search_name = full_name or _name_from_linkedin_url(linkedin_url)
            split = _split_name(search_name)
            if split["first"] and split["last"]:
                search_headers = {
                    "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
                    "accept": "application/vnd.api+json",
                    "content-type": "application/vnd.api+json",
                }
                search_payload = {
                    "data": {
                        "type": "ContactSearch",
                        "attributes": {
                            "firstName": split["first"],
                            "lastName": split["last"],
                        },
                    }
                }
                try:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        sres = await client.post(
                            "https://api.zoominfo.com/gtm/data/v1/contacts/search",
                            headers=search_headers,
                            json=search_payload,
                        )
                    if sres.status_code < 400:
                        sjson = sres.json()
                        sdata = sjson.get("data") if isinstance(sjson, dict) else []
                        if isinstance(sdata, list) and sdata:
                            person_id = sdata[0].get("id")
                            if person_id:
                                match_person_input["personId"] = str(person_id)
                                logger.info(
                                    "ZoomInfo fallback search resolved personId for %s using name '%s'",
                                    candidate_id,
                                    search_name,
                                )
                except Exception as e:
                    logger.warning(f"ZoomInfo fallback search failed for {candidate_id}: {e}")

        if not match_person_input:
            logger.warning(
                "ZoomInfo new API fallback skipped for %s: insufficient match inputs after search (need email OR phone OR full name + company OR resolvable name)",
                candidate_id,
            )
            logger.info(
                "ZoomInfo fallback insufficient inputs for %s; returning no-contact result",
                candidate_id,
            )
            apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
            if apollo_result.get("ok"):
                provider_used = "apollo"
                extracted = apollo_result.get("fields") or {}
                logger.info("Apollo fallback succeeded for %s after ZoomInfo insufficient match inputs", candidate_id)
            else:
                return {
                    "status": "success",
                    "candidate_id": candidate_id,
                    "linkedin_url": linkedin_url,
                    "phone_source": "none",
                    "phone": None,
                    "phoneCandidates": [],
                    "email": None,
                    "workPhone": None,
                    "mobilePhone": None,
                    "workEmail": None,
                    "personalEmail": None,
                    "provider": "none",
                    "updated_rows": 0,
                    "message": "ZoomInfo fallback skipped: insufficient match inputs (no reliable person match).",
                }

        new_headers = {
            "Authorization": f"Bearer {ZOOMINFO_BEARER_TOKEN}",
            "accept": "application/vnd.api+json",
            "content-type": "application/vnd.api+json",
        }
        new_payload = {
            "data": {
                "type": "ContactEnrich",
                "attributes": {
                    "matchPersonInput": [match_person_input],
                    "outputFields": ["mobilePhone", "phone", "email", "emailAlt"],
                },
            }
        }

        if provider_used == "zoominfo":
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    new_res = await client.post(zoominfo_new_enrich_url, headers=new_headers, json=new_payload)
            except Exception as e:
                logger.error(f"ZoomInfo new API fallback request failed for {candidate_id}: {e}")
                apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
                if apollo_result.get("ok"):
                    provider_used = "apollo"
                    extracted = apollo_result.get("fields") or {}
                    logger.info("Apollo fallback succeeded for %s after ZoomInfo new API request failure", candidate_id)
                else:
                    raise HTTPException(status_code=502, detail=f"ZoomInfo fallback request failed: {str(e)}")

        if provider_used == "zoominfo" and new_res.status_code >= 400:
            logger.warning(
                "ZoomInfo fallback non-2xx for %s: %s %s",
                candidate_id,
                new_res.status_code,
                new_res.text[:300],
            )
            apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
            if apollo_result.get("ok"):
                provider_used = "apollo"
                extracted = apollo_result.get("fields") or {}
                logger.info("Apollo fallback succeeded for %s after ZoomInfo non-2xx fallback response", candidate_id)
            elif 400 <= new_res.status_code < 500:
                return {
                    "status": "success",
                    "candidate_id": candidate_id,
                    "linkedin_url": linkedin_url,
                    "phone_source": "none",
                    "phone": None,
                    "phoneCandidates": [],
                    "email": None,
                    "workPhone": None,
                    "mobilePhone": None,
                    "workEmail": None,
                    "personalEmail": None,
                    "provider": "none",
                    "updated_rows": 0,
                    "message": f"ZoomInfo returned no contact match ({new_res.status_code}).",
                }
            else:
                raise HTTPException(status_code=502, detail=f"ZoomInfo API error ({new_res.status_code})")

        if provider_used == "zoominfo":
            try:
                new_data = new_res.json()
            except Exception:
                new_data = {"raw": new_res.text}

            extracted = _extract_new_zoominfo_contact_fields(new_data)
    elif provider_used == "zoominfo" and zres.status_code >= 400:
        logger.warning(
            f"ZoomInfo enrich non-2xx for {candidate_id}: {zres.status_code} {response_text[:300]}"
        )
        apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
        if apollo_result.get("ok"):
            provider_used = "apollo"
            extracted = apollo_result.get("fields") or {}
            logger.info("Apollo fallback succeeded for %s after ZoomInfo non-2xx response", candidate_id)
        elif 400 <= zres.status_code < 500:
            return {
                "status": "success",
                "candidate_id": candidate_id,
                "linkedin_url": linkedin_url,
                "phone_source": "none",
                "phone": None,
                "phoneCandidates": [],
                "email": None,
                "workPhone": None,
                "mobilePhone": None,
                "workEmail": None,
                "personalEmail": None,
                "provider": "none",
                "updated_rows": 0,
                "message": f"ZoomInfo returned no contact match ({zres.status_code}).",
            }
        else:
            raise HTTPException(status_code=502, detail=f"ZoomInfo API error ({zres.status_code})")
    elif provider_used == "zoominfo":
        extracted = _extract_enrichment_fields(zoominfo_data)

    # Cross-fill pass: if we have only one side (email OR phone), use it to fetch the other.
    seed_phone = _normalise_phone((extracted.get("mobilePhone") or extracted.get("workPhone") or ""))
    if sum(1 for ch in seed_phone if ch.isdigit()) < 7:
        seed_phone = ""
    seed_email = str(extracted.get("workEmail") or extracted.get("personalEmail") or "").strip().lower()

    if (seed_email and not seed_phone) or (seed_phone and not seed_email):
        supplemental = await _zoominfo_crossfill_from_email_or_phone(seed_email, seed_phone)
        if supplemental:
            for key in ("mobilePhone", "workPhone", "workEmail", "personalEmail"):
                if not extracted.get(key) and supplemental.get(key):
                    extracted[key] = supplemental.get(key)

            existing_candidates = extracted.get("phoneCandidates") if isinstance(extracted.get("phoneCandidates"), list) else []
            supplemental_candidates = [supplemental.get("mobilePhone"), supplemental.get("workPhone")]
            merged_candidates: List[str] = []
            merged_seen = set()
            for candidate in [*existing_candidates, *supplemental_candidates]:
                n = _normalise_phone(str(candidate or ""))
                if not n:
                    continue
                if sum(1 for ch in n if ch.isdigit()) < 7:
                    continue
                if n in merged_seen:
                    continue
                merged_seen.add(n)
                merged_candidates.append(n)
            if merged_candidates:
                extracted["phoneCandidates"] = merged_candidates

            logger.info(
                "Contact enrich cross-fill succeeded for %s | had_email=%s | had_phone=%s",
                candidate_id,
                bool(seed_email),
                bool(seed_phone),
            )

    if provider_used == "zoominfo":
        probe_phone = _normalise_phone((extracted.get("mobilePhone") or extracted.get("workPhone") or ""))
        probe_email = str(extracted.get("workEmail") or extracted.get("personalEmail") or "").strip().lower()
        if sum(1 for ch in probe_phone if ch.isdigit()) < 7:
            probe_phone = ""

        need_apollo_supplement = (not probe_phone) or (not probe_email)
        if need_apollo_supplement:
            apollo_result = await _apollo_enrich_by_linkedin(candidate_id, linkedin_url)
            if apollo_result.get("ok"):
                apollo_fields = apollo_result.get("fields") or {}

                # Fill only missing sides so ZoomInfo data remains preferred.
                if not probe_phone:
                    if not extracted.get("mobilePhone") and apollo_fields.get("mobilePhone"):
                        extracted["mobilePhone"] = apollo_fields.get("mobilePhone")
                    if not extracted.get("workPhone") and apollo_fields.get("workPhone"):
                        extracted["workPhone"] = apollo_fields.get("workPhone")

                if not probe_email:
                    if not extracted.get("workEmail") and apollo_fields.get("workEmail"):
                        extracted["workEmail"] = apollo_fields.get("workEmail")
                    if not extracted.get("personalEmail") and apollo_fields.get("personalEmail"):
                        extracted["personalEmail"] = apollo_fields.get("personalEmail")

                # Merge phone candidates from both providers.
                existing_candidates = extracted.get("phoneCandidates") if isinstance(extracted.get("phoneCandidates"), list) else []
                apollo_candidates = apollo_fields.get("phoneCandidates") if isinstance(apollo_fields.get("phoneCandidates"), list) else []
                merged_candidates: List[str] = []
                merged_seen = set()
                for candidate in [*existing_candidates, *apollo_candidates, apollo_fields.get("mobilePhone"), apollo_fields.get("workPhone")]:
                    n = _normalise_phone(str(candidate or ""))
                    if not n:
                        continue
                    if sum(1 for ch in n if ch.isdigit()) < 7:
                        continue
                    if n in merged_seen:
                        continue
                    merged_seen.add(n)
                    merged_candidates.append(n)
                if merged_candidates:
                    extracted["phoneCandidates"] = merged_candidates

                # Re-probe after merge; if ZoomInfo had nothing and Apollo filled,
                # mark provider as Apollo for accurate telemetry/response.
                post_probe_phone = _normalise_phone((extracted.get("mobilePhone") or extracted.get("workPhone") or ""))
                post_probe_email = str(extracted.get("workEmail") or extracted.get("personalEmail") or "").strip().lower()
                if sum(1 for ch in post_probe_phone if ch.isdigit()) < 7:
                    post_probe_phone = ""
                if not probe_phone and not probe_email and (post_probe_phone or post_probe_email):
                    provider_used = "apollo"

                logger.info(
                    "Apollo supplement merged for %s | needed_phone=%s | needed_email=%s | has_phone=%s | has_email=%s",
                    candidate_id,
                    not bool(probe_phone),
                    not bool(probe_email),
                    bool(post_probe_phone),
                    bool(post_probe_email),
                )

    raw_mobile_phone = str(extracted.get("mobilePhone") or "").strip()
    raw_work_phone = str(extracted.get("workPhone") or "").strip()
    raw_work_email = extracted.get("workEmail") or ""
    raw_personal_email = extracted.get("personalEmail") or ""

    raw_phone_candidates = extracted.get("phoneCandidates") if isinstance(extracted, dict) else []
    if not isinstance(raw_phone_candidates, list):
        raw_phone_candidates = []

    normalised_candidates: List[str] = []
    seen_candidates = set()
    for candidate in [raw_mobile_phone, raw_work_phone, *raw_phone_candidates]:
        n = _normalise_phone(str(candidate or ""))
        if not n:
            continue
        if sum(1 for ch in n if ch.isdigit()) < 7:
            continue
        if n in seen_candidates:
            continue
        seen_candidates.add(n)
        normalised_candidates.append(n)

    phone_candidates_top2 = normalised_candidates[:2]

    mobile_phone_normalised = _normalise_phone(raw_mobile_phone)
    work_phone_normalised = _normalise_phone(raw_work_phone)
    if sum(1 for ch in mobile_phone_normalised if ch.isdigit()) < 7:
        mobile_phone_normalised = ""
    if sum(1 for ch in work_phone_normalised if ch.isdigit()) < 7:
        work_phone_normalised = ""

    phone_source = "none"
    if mobile_phone_normalised:
        phone_source = "mobilePhone"
    elif work_phone_normalised:
        phone_source = "workPhone"
    elif phone_candidates_top2:
        phone_source = "phoneCandidates"

    enriched_phone = _normalise_phone(raw_mobile_phone or raw_work_phone or (phone_candidates_top2[0] if phone_candidates_top2 else ""))
    if sum(1 for ch in enriched_phone if ch.isdigit()) < 7:
        enriched_phone = ""

    enriched_email = (
        raw_work_email
        or raw_personal_email
        or ""
    ).strip().lower()

    logger.info(
        "Contact enrich parsed for %s | provider=%s | phone_source=%s | has_mobile=%s | has_work=%s | has_email=%s | phone_candidates=%s | mobile=%s | work=%s | email=%s",
        candidate_id,
        provider_used,
        phone_source,
        bool(raw_mobile_phone),
        bool(raw_work_phone),
        bool(enriched_email),
        len(phone_candidates_top2),
        _mask_phone_for_log(raw_mobile_phone),
        _mask_phone_for_log(raw_work_phone),
        _mask_email_for_log(enriched_email),
    )

    # Persist only when sourced candidate rows exist; Step 5 pre-save calls may
    # return enriched contact with updated_rows=0 and the FE will persist during save.
    updated_rows = 0
    if existing_rows and (enriched_phone or enriched_email):
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    for row in existing_rows:
                        data_blob = _json_load_safe(row.get("data"), {})
                        data_blob["zoominfo_contact_enrichment"] = {
                            "linkedin_url": linkedin_url,
                            "provider": provider_used,
                            "workPhone": extracted.get("workPhone"),
                            "mobilePhone": extracted.get("mobilePhone"),
                            "phoneCandidates": phone_candidates_top2,
                            "workEmail": extracted.get("workEmail"),
                            "personalEmail": extracted.get("personalEmail"),
                            "enriched_at": datetime.now(timezone.utc).isoformat(),
                        }

                        cur.execute(
                            """
                            UPDATE sourced_candidates
                            SET phone = COALESCE(%s, phone),
                                email = COALESCE(%s, email),
                                data = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """,
                            (
                                enriched_phone or None,
                                enriched_email or None,
                                json.dumps(data_blob),
                                row.get("id"),
                            ),
                        )
                        updated_rows += cur.rowcount
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Failed persisting ZoomInfo enrichment for {candidate_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to persist enrichment: {str(e)}")

    return {
        "status": "success",
        "candidate_id": candidate_id,
        "linkedin_url": linkedin_url,
        "provider": provider_used,
        "phone_source": phone_source,
        "phone": enriched_phone or None,
        "phoneCandidates": phone_candidates_top2,
        "email": enriched_email or None,
        "workPhone": raw_work_phone or None,
        "mobilePhone": raw_mobile_phone or None,
        "workEmail": raw_work_email or None,
        "personalEmail": raw_personal_email or None,
        "updated_rows": updated_rows,
    }


@router.post("/candidates/enrich-contact")
async def enrich_candidate_contact_body(request: EnrichCandidateContactRequest):
    """
    Body-based enrich endpoint to avoid URL/path encoding edge cases for
    candidate IDs containing reserved/non-ASCII characters.
    """
    candidate_id = str(request.candidate_id or "").strip()
    if not candidate_id:
        raise HTTPException(status_code=400, detail="candidate_id is required")
    return await _enrich_candidate_contact_impl(candidate_id, request)


@router.post("/candidates/{candidate_id:path}/enrich-contact")
async def enrich_candidate_contact(candidate_id: str, request: EnrichCandidateContactRequest):
    return await _enrich_candidate_contact_impl(candidate_id, request)


@router.patch("/candidates/{candidate_id}/phone")
async def update_candidate_phone(candidate_id: str, request: UpdateCandidatePhoneRequest):
    normalised = _normalise_phone(request.phone)
    digit_count = sum(1 for ch in normalised if ch.isdigit())
    if digit_count < 7:
        raise HTTPException(status_code=400, detail="Phone number must contain at least 7 digits")

    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                if request.jobdiva_id:
                    cur.execute(
                        """
                        UPDATE sourced_candidates
                        SET phone = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE candidate_id = %s AND jobdiva_id = %s
                        """,
                        (normalised, candidate_id, request.jobdiva_id),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE sourced_candidates
                        SET phone = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE candidate_id = %s
                        """,
                        (normalised, candidate_id),
                    )
                updated = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return {"status": "success", "candidate_id": candidate_id, "phone": normalised, "updated_rows": updated}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"update_candidate_phone failed for {candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/candidates/list")
async def get_all_candidates(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None),
    job_id: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    match_band: Optional[str] = Query(None, pattern="^(strong|good|low|unscored)$"),
    sort_key: Optional[str] = Query(None, pattern="^(name|match|job|source|location|created_at)$"),
    sort_dir: Optional[str] = Query(None, pattern="^(asc|desc)$"),
):
    """Paginated master candidate pool.

    Pagination + filtering + sorting is server-side so the endpoint operates
    across the full table (~8k rows) without returning the whole set. Filter
    dropdowns source their options from `/candidates/filter-options` so they
    reflect the entire DB, not just the current page.
    """
    try:
        import services.sourced_candidates_storage as scs
        storage = scs.SourcedCandidatesStorage()
        result = storage.get_all_candidates(
            limit=limit,
            offset=offset,
            search=search,
            job_id=job_id,
            source=source,
            location=location,
            match_band=match_band,
            sort_key=sort_key,
            sort_dir=sort_dir,
        )

        # BC: older FE consumers read job_id; mirror from jobdiva_id.
        for candidate in result["candidates"]:
            if candidate.get("jobdiva_id"):
                candidate["job_id"] = candidate["jobdiva_id"]

        return {
            "status": "success",
            "candidates": result["candidates"],
            "total": result["total"],
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error(f"Error fetching all candidates: {e}")
        return {"status": "error", "candidates": [], "total": 0, "message": str(e)}


@router.get("/candidates/filter-options")
async def get_candidate_filter_options():
    """Distinct jobs/sources/locations used by the FE filter dropdowns.

    Returning DB-wide distinct values (not current-page) means filters still
    work correctly across a paginated backend.
    """
    try:
        import services.sourced_candidates_storage as scs
        storage = scs.SourcedCandidatesStorage()
        options = storage.get_filter_options()
        return {"status": "success", **options}
    except Exception as e:
        logger.error(f"Error fetching candidate filter options: {e}")
        return {"status": "error", "jobs": [], "sources": [], "locations": [], "message": str(e)}

@router.post("/candidates/enhanced-fetch")
async def fetch_enhanced_candidates(request: Dict[str, str]):
    """
    Enhanced candidate fetching using combined JobDiva API calls:
    - JobApplicantsDetail: Get job applicants
    - CandidateDetail: Get candidate info
    - ResumeDetail: Get full resume text
    """
    try:
        job_id = request.get("job_id") or request.get("jobdiva_id")
        if not job_id:
            return {"status": "error", "candidates": [], "message": "job_id required"}

        print(f"🚀 Enhanced candidate fetch for job: {job_id}")

        # Use the new enhanced method
        enhanced_candidates = await jobdiva_service.get_enhanced_job_candidates(job_id)

        # Save to database with deduplication
        saved_count = await jobdiva_service.save_enhanced_candidates_to_db(job_id, enhanced_candidates)

        return {
            "status": "success",
            "candidates": enhanced_candidates,
            "total_found": len(enhanced_candidates),
            "total_saved": saved_count,
            "message": f"Found {len(enhanced_candidates)} enhanced candidates with full resume text"
        }

    except Exception as e:
        print(f"❌ Enhanced fetch error: {e}")
        return {"status": "error", "candidates": [], "message": str(e)}

@router.post("/candidates/{candidate_id}/update-resume")
async def update_candidate_resume(candidate_id: str):
    """Update resume text for an existing candidate using enhanced JobDiva integration."""
    try:
        print(f"🔄 Updating resume for candidate: {candidate_id}")

        success = await jobdiva_service.update_candidate_resume_text(candidate_id)

        if success:
            return {
                "status": "success",
                "message": f"Successfully updated resume text for candidate {candidate_id}"
            }
        else:
            return {
                "status": "error",
                "message": f"Failed to update resume text for candidate {candidate_id}"
            }

    except Exception as e:
        logger.error(f"Error updating resume for candidate {candidate_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/candidates/{candidate_id}/resume")
async def get_candidate_resume(candidate_id: str):
    """
    Fetch individual candidate resume by candidate ID from JobDiva.
    Only returns real resumes - no auto-generated content.
    """
    try:
        print(f"📄 Fetching resume for candidate: {candidate_id}")
        resume_data = await jobdiva_service.get_candidate_resume(candidate_id)

        if not resume_data or resume_data is None:
            return {
                "status": "error",
                "resume_text": "Resume content is not available for this candidate.",
                "message": "No real resume found in JobDiva - auto-generated content disabled"
            }

        # Extract resume text from the response
        resume_text = resume_data.get("resume_text", "")

        # Check for auto-generated content patterns and reject them
        if (resume_text and (
            "Professional experience details available upon request" in resume_text or
            "Experienced professional with a strong background" in resume_text or
            "Contact information and detailed work history available upon request" in resume_text
        )):
            print(f"⚠️ Detected auto-generated content for {candidate_id} - rejecting")
            return {
                "status": "error",
                "resume_text": "Resume content is not available for this candidate.",
                "message": "Only real JobDiva resumes are displayed - auto-generated content filtered out"
            }

        if not resume_text or resume_text.strip() == "":
            return {
                "status": "error",
                "resume_text": "Resume content is not available for this candidate.",
                "message": "No resume text found in JobDiva response"
            }

        return {
            "status": "success",
            "resume_text": resume_text,
            "candidate_id": candidate_id
        }

    except Exception as e:
        print(f"❌ Resume fetch error for {candidate_id}: {e}")
        return {
            "status": "error",
            "resume_text": "Resume content is not available for this candidate.",
            "message": f"Error fetching resume: {str(e)}"
        }

@router.post("/candidates/analyze", response_model=CandidateAnalysisResponse)
async def analyze_candidates(request: CandidateAnalysisRequest):
    """
    Batch analyzes candidates against JD using AI.
    """
    candidates_to_process = []

    # We need to ensure we have resume text for analysis.
    # If the client sent it, great. If not, we fetch it given the ID.
    for c in request.candidates:
        c_text = c.get("resume_text")
        if not c_text:
             # Try fetch if missing
             try:
                # Determine Source to Route Correctly
                source = c.get("source", "JobDiva")
                if source == "VettedDB":
                    from services.vetted import vetted_service
                    c_text = await vetted_service.get_candidate_resume(c.get("id"))
                else:
                    # Default to JobDiva
                    c_text = await jobdiva_service.get_candidate_resume(c.get("id"))

                c["resume_text"] = c_text
             except Exception as e:
                # Log but continue, AI will just have less context
                print(f"Error fetching resume for {c.get('id')}: {e}")
                pass
        candidates_to_process.append(c)

    results = await ai_service.analyze_candidates_batch(
        candidates_to_process,
        request.job_description,
        structured_jd=request.structured_jd
    )
    return {"results": results, "name": "", "email": "", "skills": [], "experience_years": 0} # Dummy fields to satisfy model if strict


# ---------------------------------------------------------------------------
# Candidate Evaluation Report
# ---------------------------------------------------------------------------
@router.get("/candidates/{candidate_id}/evaluation-report")
async def get_candidate_evaluation_report(
    candidate_id: str,
    job_id: Optional[str] = Query(None, description="Job ID or JobDiva ID"),
):
    """
    Aggregate a full Candidate Evaluation Report from multiple data sources:
      - sourced_candidates      : basic profile, resume, match scores
      - monitored_jobs          : position details, rubric, screening questions
      - engage_interview_audit  : interview history, send timestamps
      - PAIR API                : live evaluation, Q&A, transcriptions, outreach
    """
    import os, httpx as _httpx
    PAIR_BASE = os.getenv("EXTERNAL_INTERVIEW_API_URL", "https://pairbotqa.hoonr.ai")

    try:
        from psycopg2.extras import RealDictCursor

        conn = get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # 1. sourced_candidates — prefer the row tied to this job
                if job_id:
                    cur.execute(
                        """
                        SELECT sc.*
                        FROM sourced_candidates sc
                        JOIN monitored_jobs mj
                          ON mj.jobdiva_id = sc.jobdiva_id
                         OR mj.job_id      = sc.jobdiva_id
                        WHERE sc.candidate_id = %s
                          AND (mj.job_id = %s OR mj.jobdiva_id = %s)
                        ORDER BY sc.updated_at DESC
                        LIMIT 1
                        """,
                        (candidate_id, job_id, job_id),
                    )
                    cand_row = cur.fetchone()
                    if not cand_row:
                        # Fallback: any row for this candidate
                        cur.execute(
                            "SELECT * FROM sourced_candidates WHERE candidate_id = %s ORDER BY updated_at DESC LIMIT 1",
                            (candidate_id,),
                        )
                        cand_row = cur.fetchone()
                else:
                    cur.execute(
                        "SELECT * FROM sourced_candidates WHERE candidate_id = %s ORDER BY updated_at DESC LIMIT 1",
                        (candidate_id,),
                    )
                    cand_row = cur.fetchone()

                if not cand_row:
                    raise HTTPException(status_code=404, detail=f"Candidate {candidate_id} not found")

                # Parse data blob
                data_blob = cand_row.get("data") or {}
                if isinstance(data_blob, str):
                    try:
                        data_blob = json.loads(data_blob)
                    except Exception:
                        data_blob = {}

                # Resolve effective jobdiva_id for job lookup
                effective_jobdiva_id = cand_row.get("jobdiva_id") or job_id or ""

                # 2. monitored_jobs
                job_row = None
                if effective_jobdiva_id or job_id:
                    cur.execute(
                        """
                        SELECT mj.*,
                               mj.resume_match_filters,
                               mj.sourcing_filters,
                               mj.bot_introduction
                        FROM monitored_jobs mj
                        WHERE mj.job_id = %s OR mj.jobdiva_id = %s
                        LIMIT 1
                        """,
                        (effective_jobdiva_id, effective_jobdiva_id),
                    )
                    job_row = cur.fetchone()
                    if not job_row and job_id and job_id != effective_jobdiva_id:
                        cur.execute(
                            "SELECT * FROM monitored_jobs WHERE job_id = %s OR jobdiva_id = %s LIMIT 1",
                            (job_id, job_id),
                        )
                        job_row = cur.fetchone()

                # 3. Fetch structured rubric using service
                from services.job_rubric_db import JobRubricDB
                rubric_db = JobRubricDB()
                rubric = rubric_db.get_full_rubric(effective_jobdiva_id) or {}

                # 4. engage_interview_audit — most recent row for this candidate + job
                audit_row = None
                audit_where = ["candidate_id = %s"]
                audit_params: list = [candidate_id]
                if effective_jobdiva_id:
                    audit_where.append("(job_id = %s OR job_id = %s)")
                    audit_params += [effective_jobdiva_id, job_id or effective_jobdiva_id]
                cur.execute(
                    f"""
                    SELECT * FROM engage_interview_audit
                    WHERE {' AND '.join(audit_where)}
                      AND interview_id IS NOT NULL AND interview_id::text != ''
                    ORDER BY id DESC LIMIT 1
                    """,
                    tuple(audit_params),
                )
                audit_row = cur.fetchone()

                # 4. screening questions
                screen_questions = []
                if job_row:
                    jd_id = job_row.get("jobdiva_id") or effective_jobdiva_id or ""
                    job_pk = job_row.get("job_id") or ""
                    cur.execute(
                        """
                        SELECT question_text, pass_criteria, is_default, category, order_index
                        FROM job_screen_questions
                        WHERE jobdiva_id = %s OR jobdiva_id = %s
                        ORDER BY order_index
                        """,
                        (jd_id, job_pk),
                    )
                    screen_questions = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

        # ----------------------------------------------------------------
        # Parse sourced_candidates fields
        # ----------------------------------------------------------------
        def _jl(val, default):
            if val is None:
                return default
            if isinstance(val, type(default)):
                return val
            if isinstance(val, str):
                try:
                    p = json.loads(val)
                    return p if isinstance(p, type(default)) else default
                except Exception:
                    return default
            return default

        resume_match_score = float(cand_row.get("resume_match_percentage") or 0)
        engage_score       = float(data_blob.get("engage_score") or 0)
        engage_total_score = float(data_blob.get("engage_total_score") or 0)
        engage_status      = str(data_blob.get("engage_status") or "")
        hard_filter_status = str(data_blob.get("engage_hard_filter_status") or "")
        engage_interview_id = str(data_blob.get("engage_interview_id") or (audit_row or {}).get("interview_id") or "")
        engage_completed_at = data_blob.get("engage_completed_at") or (audit_row or {}).get("updated_at") or None
        engage_created_at   = (audit_row or {}).get("created_at") or None

        # ----------------------------------------------------------------
        # Webhook payload fallback (questions & answers)
        # ----------------------------------------------------------------
        webhook_payload = data_blob.get("engage_last_response")
        if isinstance(webhook_payload, str):
            try:
                webhook_payload = json.loads(webhook_payload)
            except:
                webhook_payload = {}

        # ----------------------------------------------------------------
        # Build job details block
        # ----------------------------------------------------------------
        job_details: dict = {}
        if job_row:
            sourcing_filters = _jl(job_row.get("sourcing_filters"), {})
            resume_match_filters = _jl(job_row.get("resume_match_filters"), [])
            job_details = {
                "job_id":            job_row.get("job_id"),
                "jobdiva_id":        job_row.get("jobdiva_id"),
                "title":             job_row.get("title") or job_row.get("enhanced_title"),
                "customer_name":     job_row.get("customer_name"),
                "city":              job_row.get("city"),
                "state":             job_row.get("state"),
                "job_location":      f"{job_row.get('city') or ''}, {job_row.get('state') or ''}".strip(", "),
                "pay_rate":          job_row.get("pay_rate"),
                "start_date":        str(job_row.get("start_date") or ""),
                "posted_date":       str(job_row.get("posted_date") or ""),
                "openings":          job_row.get("openings"),
                "max_allowed_submittals": job_row.get("max_allowed_submittals"),
                "employment_type":   job_row.get("employment_type"),
                "work_authorization":job_row.get("work_authorization"),
                "ai_description":    job_row.get("ai_description") or job_row.get("jobdiva_description"),
                "recruiter_notes":   job_row.get("recruiter_notes"),
                "bot_introduction":  job_row.get("bot_introduction"),
                "rubric":            rubric,
                "sourcing_filters":  sourcing_filters,
                "resume_match_filters": resume_match_filters,
                "screen_questions":  screen_questions,
            }

        # ----------------------------------------------------------------
        # Fetch PAIR live data if we have an interview_id
        # ----------------------------------------------------------------
        pair_data: dict = {}
        
        # Pre-populate from webhook payload if present
        if webhook_payload:
            wp_data = webhook_payload.get("data", webhook_payload)
            pair_data = {
                "interview":      wp_data.get("interview"),
                "evaluation":     wp_data.get("evaluation") or wp_data,
                "transcriptions": wp_data.get("transcriptions") or wp_data.get("conversation") or [],
                "outreach":       wp_data.get("outreach"),
                "questions_answers": wp_data.get("questions_answers", []),
            }

        if engage_interview_id:
            try:
                async with _httpx.AsyncClient(timeout=20.0) as client:
                    interview_res, evaluation_res, transcription_res, outreach_res = await asyncio.gather(
                        client.get(f"{PAIR_BASE}/api/interviews/{engage_interview_id}"),
                        client.get(f"{PAIR_BASE}/api/interviews/{engage_interview_id}/evaluation"),
                        client.get(f"{PAIR_BASE}/api/interviews/{engage_interview_id}/transcriptions"),
                        client.get(f"{PAIR_BASE}/api/interviews/{engage_interview_id}/outreach-status"),
                        return_exceptions=True,
                    )

                def _safe(res):
                    if isinstance(res, Exception):
                        return None
                    try:
                        if res.status_code == 200:
                            d = res.json()
                            return d.get("data", d)
                        return None
                    except Exception:
                        return None

                interview_data    = _safe(interview_res)
                evaluation_data   = _safe(evaluation_res)
                transcription_data= _safe(transcription_res)
                outreach_data     = _safe(outreach_res)

                # Merge live data into pair_data (live data takes precedence)
                if interview_data: pair_data["interview"] = interview_data
                if evaluation_data: pair_data["evaluation"] = evaluation_data
                if transcription_data: 
                    pair_data["transcriptions"] = transcription_data if isinstance(transcription_data, list) else (transcription_data or [])
                if outreach_data: pair_data["outreach"] = outreach_data

                # Dynamic mapping of questions to evaluation fields
                qa_list = pair_data.get("questions_answers") or []
                eval_map = {}
                for qa in qa_list:
                    txt = (qa.get("question_text") or "").lower()
                    ans = qa.get("answer_text") or ""
                    if "open to exploring new job" in txt: eval_map["isActivelyLookingForJob"] = ans
                    elif "current or most recent role" in txt: eval_map["currentRole"] = ans
                    elif "current location" in txt: eval_map["currentLocation"] = ans
                    elif "authorized to work in the united states" in txt: eval_map["isAuthorizedToWorkInUS"] = ans
                    elif "visa sponsorship" in txt: eval_map["requireVisaSponsorship"] = ans
                    elif "onsite work arrangement" in txt: eval_map["isOpenToRelocation"] = ans
                    elif "availability to start" in txt: eval_map["isAvailableToStartSoon"] = ans
                    elif "compensation" in txt: eval_map["expectedPayRate"] = ans
                
                # Merge into evaluation object
                if isinstance(pair_data.get("evaluation"), dict):
                    pair_data["evaluation"].update(eval_map)
                else:
                    pair_data["evaluation"] = eval_map

                # Refresh scores from live PAIR data
                if isinstance(interview_data, dict):
                    iv = interview_data.get("interview") or interview_data
                    if iv.get("overall_score") is not None:
                        engage_score = float(iv["overall_score"])
                    if iv.get("hard_filter_overall") or iv.get("hard_filter_status"):
                        hard_filter_status = str(iv.get("hard_filter_overall") or iv.get("hard_filter_status") or hard_filter_status)
                    if iv.get("status"):
                        engage_status = str(iv["status"])
                    if iv.get("completed_at"):
                        engage_completed_at = iv["completed_at"]
            except Exception as pair_err:
                logger.warning(f"PAIR data fetch failed for interview {engage_interview_id}: {pair_err}")

        # ----------------------------------------------------------------
        # Compose final report
        # ----------------------------------------------------------------
        candidate_info = {
            "candidate_id":    candidate_id,
            "name":            cand_row.get("name"),
            "email":           cand_row.get("email"),
            "phone":           cand_row.get("phone"),
            "headline":        cand_row.get("headline"),
            "location":        cand_row.get("location"),
            "source":          cand_row.get("source"),
            "profile_url":     cand_row.get("profile_url"),
            "image_url":       cand_row.get("image_url"),
            "availability":    data_blob.get("recent_availability") or data_blob.get("recentAvailability") or data_blob.get("availability_status") or data_blob.get("availability"),
            "resume_text":     cand_row.get("resume_text") or data_blob.get("resume_text"),
            "skills":          _jl(data_blob.get("skills"), []),
            "experience_years":data_blob.get("experience_years"),
            "company_experience": _jl(data_blob.get("company_experience"), []),
            "education":       _jl(data_blob.get("education"), []),
            "certifications":  _jl(data_blob.get("certifications"), []),
        }

        scores = {
            "resume_match_score":    resume_match_score,
            "resume_match_status":   str(data_blob.get("resume_matching_status") or ("done" if resume_match_score > 0 else "pending")),
            "engage_score":          engage_score,
            "engage_total_score":    engage_total_score,
            "engage_status":         engage_status,
            "hard_filter_status":    hard_filter_status,
            "engage_interview_id":   engage_interview_id,
            "engage_completed_at":   str(engage_completed_at) if engage_completed_at else None,
            "engage_created_at":     str(engage_created_at) if engage_created_at else None,
            "matched_skills":        _jl(data_blob.get("matched_skills"), []),
            "missing_skills":        _jl(data_blob.get("missing_skills"), []),
            "explainability":        _jl(data_blob.get("explainability"), []),
            "score_details":         _jl(data_blob.get("match_score_details"), {}),
        }

        return {
            "status":    "success",
            "candidate": candidate_info,
            "scores":    scores,
            "job":       job_details,
            "pair":      pair_data,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"evaluation-report failed for {candidate_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
@router.post("/jobs/{job_id_or_ref}/candidates/{candidate_id}/feedback")
async def save_candidate_feedback(
    job_id_or_ref: str,
    candidate_id: str,
    request: CandidateFeedbackRequest
):
    """
    Saves candidate feedback (Submit/Reject) and creates a JobDiva Note with:
      - actionDate : timestamp of feedback
      - action     : PAIR Reject / PAIR Submit action string
      - recruiterid: PAIR recruiter (JOBDIVA_PAIR_RECRUITER_ID env var)
      - link2AnOpenJob: numeric JobDiva job ID
      - note       : "Click Here to view the report."
    """
    logger.info(f"📝 Receiving feedback for candidate {candidate_id} on job {job_id_or_ref}: {request.feedback_type}")
    
    # 1. Map to JobDiva Action String
    action_string = ""
    if request.feedback_type == "Submit":
        action_string = "PAIR Submit - Externally Submitted"
    elif request.feedback_type == "Reject":
        rejection_mapping = {
            "Skills do not meet requirements": "PAIR Reject - Skills do not meet requirements",
            "Communication skills": "PAIR Reject - Communication skills",
            "Domain experience mismatch": "PAIR Reject - Domain experience mismatch",
            "More qualified candidates identified": "PAIR Reject - More qualified candidates identified",
            "Overqualified for the role": "PAIR Reject - Overqualified for the role",
            "Compensation expectations exceed budget": "PAIR Reject - Compensation expectations exceed budget",
            "Not aligned with employment type (W2 / C2C / 1099)": "PAIR Reject - Not aligned with employment type (W2 / C2C / 1099)",
            "Work authorization / visa constraints": "PAIR Reject - Work authorization / visa constraints",
            "Not comfortable with background check / drug test": "PAIR Reject - Not comfortable with background check / drug test",
            "Not local and not open to relocation": "PAIR Reject - Not local and not open to relocation",
            "Open to remote only": "PAIR Reject - Open to remote only",
            "Not available within required timeline": "PAIR Reject - Not available within required timeline",
            "Accepted another offer": "PAIR Reject - Accepted another offer",
            "Candidate withdrew interest": "PAIR Reject - Candidate withdrew interest",
            "Career gap concern": "PAIR Reject - Career gap concern",
            "Job Hopping (short-term engagements throughout or in the last 5-7 years)": "PAIR Reject - Job Hopping",
            "Fake candidate — Multiple profiles/resumes; misrepresentation of past experience": "PAIR Reject - Fake candidate",
            "Already submitted to same client / hiring manager by another vendor": "PAIR Reject - Already submitted",
            "Previously rejected by client": "PAIR Reject - Previously rejected by client",
            "Not eligible for rehire": "PAIR Reject - Not eligible for rehire",
            "Past performance concern (Internal note as per past Pyramid client feedback)": "PAIR Reject - Past performance concern",
        }
        action_string = rejection_mapping.get(request.reason, f"PAIR Reject - {request.reason}" if request.reason else "PAIR Reject")
    
    # 2. Resolve the real JobDiva candidate_id and numeric job ID from the DB.
    #    The frontend sends `candidate.id` (the sourced_candidates integer PK) in the URL.
    #    JobDiva's createCandidateNote requires the real numeric JobDiva candidate ID
    #    (sourced_candidates.candidate_id) and the numeric job ID (monitored_jobs.jobdiva_id).
    jd_candidate_id = candidate_id   # fallback: use whatever was passed
    jd_job_ref = job_id_or_ref       # fallback: use the raw job ref
    sc_row_id = None                 # sourced_candidates.id (PK) once resolved

    try:
        _conn = get_db_connection()
        try:
            with _conn.cursor() as _cur:
                # Try treating candidate_id as the integer PK (candidate.id from the frontend)
                try:
                    pk_int = int(candidate_id)
                    _cur.execute(
                        "SELECT id, candidate_id, jobdiva_id FROM sourced_candidates WHERE id = %s LIMIT 1",
                        (pk_int,)
                    )
                    row = _cur.fetchone()
                    if row:
                        sc_row_id      = row[0]
                        jd_candidate_id = str(row[1])   # real JobDiva numeric candidate ID
                        jd_job_ref     = str(row[2]) if row[2] else job_id_or_ref
                except (ValueError, TypeError):
                    # candidate_id is not an integer PK – try matching as a candidate_id string
                    _cur.execute(
                        """SELECT id, candidate_id, jobdiva_id
                             FROM sourced_candidates
                            WHERE candidate_id = %s
                              AND (jobdiva_id = %s
                                   OR jobdiva_id IN (
                                         SELECT jobdiva_id FROM monitored_jobs WHERE job_id = %s
                                   ))
                            LIMIT 1""",
                        (candidate_id, job_id_or_ref, job_id_or_ref)
                    )
                    row = _cur.fetchone()
                    if row:
                        sc_row_id      = row[0]
                        jd_candidate_id = str(row[1])
                        jd_job_ref     = str(row[2]) if row[2] else job_id_or_ref
        finally:
            _conn.close()
    except Exception as e:
        logger.warning(f"⚠️ Could not resolve JobDiva IDs from DB for feedback "
                       f"(candidate_id={candidate_id}, job={job_id_or_ref}): {e}")

    logger.info(f"📝 Resolved → jd_candidate_id={jd_candidate_id}, jd_job_ref={jd_job_ref}, sc_row_id={sc_row_id}")

    # 3. Push to JobDiva — POST /apiv2/jobdiva/createCandidateNote
    #    Recruiter = PAIR (configured via JOBDIVA_PAIR_RECRUITER_ID env var)
    from core import JOBDIVA_PAIR_RECRUITER_ID

    jobdiva_result = await jobdiva_service.create_candidate_note(
        candidate_id=jd_candidate_id,
        job_id=jd_job_ref,
        action=action_string,
        note_text="Click Here to view the report.",
        recruiter_id=JOBDIVA_PAIR_RECRUITER_ID,
    )

    if jobdiva_result.get("status") == "error":
        logger.error(f"❌ JobDiva note creation failed: {jobdiva_result.get('message')}")
    else:
        logger.info(f"✅ JobDiva note created — action='{action_string}', "
                    f"candidate={jd_candidate_id}, job={jd_job_ref}")

    # 4. Persist feedback locally in sourced_candidates.data (JSONB merge)
    try:
        _conn2 = get_db_connection()
        with _conn2.cursor() as _cur2:
            feedback_payload = json.dumps({
                "feedback_type": request.feedback_type,
                "feedback_reason": request.reason,
                "feedback_synced": jobdiva_result.get("status") == "success",
                "feedback_at": datetime.now(timezone.utc).isoformat()
            })
            if sc_row_id is not None:
                # Fast path: update exactly the row we resolved
                _cur2.execute(
                    "UPDATE sourced_candidates SET data = data || %s::jsonb WHERE id = %s",
                    (feedback_payload, sc_row_id)
                )
            else:
                # Fallback: match by candidate_id + job ref
                _cur2.execute(
                    """UPDATE sourced_candidates
                          SET data = data || %s::jsonb
                        WHERE candidate_id = %s
                          AND (jobdiva_id = %s
                               OR jobdiva_id IN (
                                     SELECT jobdiva_id FROM monitored_jobs WHERE job_id = %s
                               ))""",
                    (feedback_payload, jd_candidate_id, job_id_or_ref, job_id_or_ref)
                )
            _conn2.commit()
        _conn2.close()
    except Exception as e:
        logger.error(f"❌ Failed to persist feedback locally: {e}")

    return {
        "status": "success",
        "jobdiva_sync": jobdiva_result.get("status"),
        "action_string": action_string,
        "jobdiva_candidate_id": jd_candidate_id,
        "jobdiva_job_ref": jd_job_ref,
    }
