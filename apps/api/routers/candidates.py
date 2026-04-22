from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import List, Dict, Any, Optional
import asyncio
import json
import logging

from services.ai_service import ai_service
from services.jobdiva import jobdiva_service
from services.unipile import unipile_service
from services.sourced_candidates_storage import sourced_candidates_storage
from models import (
    CandidateSearchRequest, CandidateMessageRequest, CandidatesSaveRequest,
    CandidateAnalysisRequest, CandidateAnalysisResponse,
)
from routers._helpers import get_db_connection

router = APIRouter()
logger = logging.getLogger(__name__)


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

        # Extract location
        location = ""
        if request.locations:
            location = request.locations[0].value
        elif request.location:
            location = request.location

        # Resolve per-location radius if provided (fallback 25 miles).
        within_miles = 25
        if request.locations:
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
            page_size=request.limit or 100,
            sources=request.sources or ["JobDiva"],
            open_to_work=request.open_to_work,
            boolean_string=request.boolean_string or ""
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

            # Lightweight fallback: do not hydrate every applicant during search.
            token = await jobdiva_service.authenticate()
            candidates = await jobdiva_service._get_all_job_applicants(
                request.job_id,
                request.limit or 100,
                token
            ) if token else []
            for candidate in candidates:
                candidate["source"] = "JobDiva-Applicants"

            # Fix 3 (Path C): score fallback candidates so the UI doesn't render
            # every applicant at 0%. We rebuild SearchCriteria from the request
            # inline because the outer try block may have failed before
            # `criteria` was constructed.
            try:
                fallback_location = ""
                if request.locations:
                    fallback_location = request.locations[0].value
                elif request.location:
                    fallback_location = request.location

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
                    companies=request.companies or [],
                    page_size=request.limit or 100,
                    sources=request.sources or ["JobDiva"],
                    open_to_work=request.open_to_work,
                    boolean_string=request.boolean_string or "",
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
                "candidates": candidates[:request.limit or 100],
                "total": len(candidates),
                "job_applicants": len(candidates),
                "talent_pool": 0,
                "message": f"Found {len(candidates)} candidates using fallback search (unified search failed)"
            }

        except Exception as fallback_error:
            logger.error(f"❌ Fallback search also failed: {fallback_error}")
            raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

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
        from core.config import DATABASE_URL
        import psycopg2
        from psycopg2.extras import RealDictCursor

        # Resolve the alphanumeric jobdiva_id (e.g. '26-05172') from monitored_jobs.
        # sourced_candidates.jobdiva_id must always store the alphanumeric ref, NOT the numeric PK.
        resolved_jobdiva_id = job_id_or_ref  # fallback: use whatever was passed
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT jobdiva_id, job_id FROM monitored_jobs 
                    WHERE job_id = %s OR jobdiva_id = %s 
                    LIMIT 1
                """, (job_id_or_ref, job_id_or_ref))
                result = cur.fetchone()
                if result:
                    # Prefer the alphanumeric jobdiva_id; fall back to job_id if jobdiva_id is NULL
                    resolved_jobdiva_id = result[0] or result[1]

        # Query sourced_candidates using the resolved alphanumeric jobdiva_id
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, jobdiva_id, candidate_id, name, email, phone, headline, location,
                           source, resume_match_percentage as match_score, created_at, data
                    FROM sourced_candidates
                    WHERE jobdiva_id = %s
                    ORDER BY created_at DESC;
                """, (resolved_jobdiva_id,))
                candidates = cur.fetchall()

        # Handle the data field (it might be a string or a dict)
        for cand in candidates:
            if cand.get("data") and isinstance(cand["data"], str):
                try:
                    cand["data"] = json.loads(cand["data"])
                except:
                    pass

        return {"status": "success", "candidates": candidates}
    except Exception as e:
        logger.error(f"Error fetching job candidates: {e}")
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
        import psycopg2 as _psycopg2
        from core.config import DATABASE_URL as _DB_URL
        resolved_jobdiva_id = request.jobdiva_id  # safe fallback
        try:
            with _psycopg2.connect(_DB_URL) as _conn:
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
        except Exception as _resolve_err:
            print(f"⚠️ Could not resolve jobdiva_id, using as-is: {_resolve_err}")

        print(f"✅ Resolved jobdiva_id: {request.jobdiva_id!r} → {resolved_jobdiva_id!r}")

        # Filter only selected candidates for saving
        selected_candidates = [c for c in request.candidates if c.is_selected]
        print(f"📝 Saving {len(selected_candidates)} selected candidates out of {len(request.candidates)} total")

        for idx, c in enumerate(selected_candidates):
            print(f"   Selected Candidate {idx+1}: {c.name} (ID: {c.candidate_id}, Source: {c.source})")

        import psycopg2
        import json
        from core.config import DATABASE_URL

        saved_count = 0
        processing_payloads = []

        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                for c in selected_candidates:
                    try:
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
                            "profile_url": getattr(c, 'profile_url', None),
                            "image_url": getattr(c, 'image_url', None),
                            "resume_id": getattr(c, 'resume_id', None),
                            "resume_text": getattr(c, 'resume_text', None),
                            "data": json.dumps({
                                "skills": c.skills or [],
                                "experience_years": c.experience_years or 0,
                                "education": getattr(c, 'education', []) or getattr(c, 'candidate_education', []),
                                "certifications": getattr(c, 'certifications', []) or getattr(c, 'candidate_certification', []),
                                "company_experience": getattr(c, 'company_experience', []),
                                "urls": getattr(c, 'urls', {}),
                                "is_selected": True,
                                "match_score": getattr(c, 'match_score', 0),
                                "enhanced_info": getattr(c, 'enhanced_info', None)  # Full LLM extraction data
                            }),
                            "status": "sourced"
                        }

                        cur.execute("""
                            INSERT INTO sourced_candidates (
                                jobdiva_id, candidate_id, source, name, email, phone, headline, location,
                                profile_url, image_url, resume_id, resume_text, data, status, updated_at
                            ) VALUES (
                                %(jobdiva_id)s, %(candidate_id)s, %(source)s, %(name)s, %(email)s, %(phone)s, %(headline)s, %(location)s,
                                %(profile_url)s, %(image_url)s, %(resume_id)s, %(resume_text)s, %(data)s, %(status)s, CURRENT_TIMESTAMP
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
                        await process_jobdiva_candidate(payload)
                        enhanced_count += 1
                    # Process LinkedIn candidates
                    elif source == "LinkedIn":
                        await process_linkedin_candidate(payload)
                        enhanced_count += 1
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

@router.get("/candidates")
async def get_all_candidates(limit: int = Query(100, ge=1, le=1000)):
    """Get all sourced candidates across all jobs."""
    try:
        import services.sourced_candidates_storage as scs
        storage = scs.SourcedCandidatesStorage()
        candidates = storage.get_all_candidates(limit=limit)

        # Map jobdiva_id to job_id in the response for backwards compatibility
        for candidate in candidates:
            if 'jobdiva_id' in candidate:
                candidate['job_id'] = candidate['jobdiva_id']  # Backend compatibility
                # Keep jobdiva_id for frontend

        return {
            "status": "success",
            "candidates": candidates,
            "total": len(candidates)
        }
    except Exception as e:
        logger.error(f"Error fetching all candidates: {e}")
        return {"status": "error", "candidates": [], "message": str(e)}

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
