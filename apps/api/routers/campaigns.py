"""Campaigns router.

A Campaign groups multiple jobs under shared common properties (employment
type, recruiter emails, screening level, job boards, bot intro) plus a reusable
JD/rubric/screening-questions template. Child jobs are ordinary `monitored_jobs`
rows stamped with `campaign_id` (a logical link — no FK, matching every other
cross-table relationship in this codebase), so all existing sourcing / Launch
PAIR / dashboard code keeps working unchanged.

Schema/DDL convention mirrors routers/job_criteria.py: a router-owned
`init_campaigns_schema()` run once at startup from main.py's lifespan.
Child-job creation reuses services.jobdiva.monitor_job_locally — the single
place a monitored_jobs row is born — rather than fanning out across the several
job-create endpoints.

NOTE: this router is mounted with prefix="/api" in main.py, so the routes below
are served at /api/campaigns, /api/campaigns/{id}, /api/campaigns/{id}/jobs.
The /api prefix routes through nginx's `location /api/` passthrough and avoids
colliding with the frontend's /campaigns pages (mirrors the /jobs page vs
/jobs/monitored API split).
"""

from fastapi import APIRouter, HTTPException, Query, Depends
from typing import List, Dict, Any, Optional
import logging
import json
import time
import uuid

from models import CampaignData, CampaignAddJobRequest, CampaignBulkAddRequest
from routers._helpers import get_db_connection, get_dict_cursor_connection
from routers.jobs import invalidate_monitored_jobs_cache
from services.jobdiva import jobdiva_service
from core.auth import get_current_user, UserIdentity, verify_job_access

# Cap on ids accepted by the bulk-add endpoint (each id is a synchronous
# JobDiva fetch + DB write; keep the request bounded).
_MAX_BULK_JOBS = 200

router = APIRouter()
logger = logging.getLogger(__name__)

# TEXT columns that hold a json.dumps'd list (mirrors monitored_jobs encoding).
_LIST_TEXT_FIELDS = ("recruiter_emails", "selected_employment_types", "selected_job_boards")
# JSONB columns holding the shared template payload.
_JSONB_FIELDS = ("template_rubric", "template_screen_questions", "template_sourcing_filters")

# Full column order used by INSERT/UPSERT. campaign_id first; timestamps are
# handled by column defaults / CURRENT_TIMESTAMP.
_COLUMNS = (
    "campaign_id", "name", "customer_name",
    "recruiter_emails", "selected_employment_types", "screening_level",
    "recruiter_notes", "work_authorization", "selected_job_boards", "bot_introduction",
    "outreach_delay_mins",
    "template_enhanced_title", "template_ai_description",
    "template_rubric", "template_screen_questions", "template_sourcing_filters",
    "pair_enabled", "status", "user_session",
)


async def init_campaigns_schema():
    """Create the campaigns table. Idempotent; safe to re-run."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS campaigns (
                        campaign_id               TEXT PRIMARY KEY,
                        name                      TEXT NOT NULL,
                        customer_name             TEXT,
                        recruiter_emails          TEXT,
                        selected_employment_types TEXT,
                        screening_level           TEXT DEFAULT 'L1.5',
                        recruiter_notes           TEXT,
                        work_authorization        TEXT,
                        selected_job_boards       TEXT,
                        bot_introduction          TEXT,
                        outreach_delay_mins       INTEGER DEFAULT NULL,
                        template_enhanced_title   TEXT,
                        template_ai_description   TEXT,
                        template_rubric           JSONB,
                        template_screen_questions JSONB,
                        template_sourcing_filters JSONB,
                        pair_enabled              BOOLEAN DEFAULT FALSE,
                        status                    TEXT DEFAULT 'active',
                        user_session              TEXT,
                        created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                cur.execute(
                    """
                    ALTER TABLE campaigns
                    ADD COLUMN IF NOT EXISTS outreach_delay_mins INTEGER DEFAULT NULL;
                    """
                )
                conn.commit()
    except Exception as e:
        logger.error(f"init_campaigns_schema failed: {e}")


def _param_value(field: str, value: Any) -> Any:
    """Encode a CampaignData field for storage. List-TEXT and JSONB columns are
    json.dumps'd (Postgres implicitly casts the text→jsonb on assignment — same
    pattern as routers/job_criteria.py)."""
    if field in _LIST_TEXT_FIELDS:
        return json.dumps(value or [])
    if field in _JSONB_FIELDS:
        return json.dumps(value) if value is not None else None
    return value


def _parse_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Decode a DB row back into a JSON-friendly campaign dict."""
    out = dict(row)
    for f in _LIST_TEXT_FIELDS:
        v = out.get(f)
        if isinstance(v, str):
            try:
                out[f] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[f] = []
        elif v is None:
            out[f] = []
    for f in _JSONB_FIELDS:
        v = out.get(f)
        # psycopg2 typically returns JSONB pre-parsed; guard for text.
        if isinstance(v, str):
            try:
                out[f] = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                out[f] = None
    # ISO-ify timestamps for JSON serialization.
    for f in ("created_at", "updated_at"):
        if out.get(f) is not None and not isinstance(out[f], str):
            out[f] = out[f].isoformat()
    return out


def _get_campaign_row(campaign_id: str) -> Optional[Dict[str, Any]]:
    with get_dict_cursor_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM campaigns WHERE campaign_id = %s", (campaign_id,))
            row = cur.fetchone()
            return _parse_row(row) if row else None


def _user_can_access_campaign(campaign: Dict[str, Any], user: UserIdentity) -> bool:
    """Boolean form of the ownership check for list filtering. Mirrors jobs'
    model via verify_job_access: admins always; recruiters when assigned in the
    campaign's recruiter_emails, or when the campaign has no assigned recruiters
    (legacy/unassigned)."""
    try:
        verify_job_access(campaign, user)
        return True
    except HTTPException:
        return False


def _ensure_campaign_access(campaign: Dict[str, Any], user: UserIdentity) -> None:
    """Raise 403 unless the user may access/modify this campaign. Reuses the same
    recruiter_emails ownership rule enforced for jobs (verify_job_access)."""
    verify_job_access(campaign, user)


@router.post("/campaigns")
async def create_campaign(campaign: CampaignData, user: UserIdentity = Depends(get_current_user)):
    """Create (or upsert) a campaign. Generates a collision-resistant CMP_ id
    when absent."""
    try:
        campaign_id = campaign.campaign_id or f"CMP_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        # POST is an upsert (ON CONFLICT DO UPDATE). If an id was supplied and a
        # campaign already exists under it, the caller must be allowed to modify
        # it — otherwise this endpoint would let anyone overwrite any campaign.
        if campaign.campaign_id:
            existing = _get_campaign_row(campaign_id)
            if existing:
                _ensure_campaign_access(existing, user)
        data = campaign.dict()
        data["campaign_id"] = campaign_id

        if not (data.get("bot_introduction") or "").strip():
            data["bot_introduction"] = (
                f"Hi {{{{candidate name}}}}, I'm Alex, a virtual recruiter with Pyramid Consulting. "
                f"We are helping our client recruit for a {{{{job_title}}}} in {{{{job_location}}}}, "
                f"and you seem to be a good fit for the role. Please note that conversation may be recorded "
                f"for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?"
            )

        values = [_param_value(col, data.get(col)) for col in _COLUMNS]
        placeholders = ", ".join(["%s"] * len(_COLUMNS))
        update_set = ", ".join(
            [f"{col} = EXCLUDED.{col}" for col in _COLUMNS if col != "campaign_id"]
        )

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO campaigns ({', '.join(_COLUMNS)})
                    VALUES ({placeholders})
                    ON CONFLICT (campaign_id) DO UPDATE
                    SET {update_set}, updated_at = CURRENT_TIMESTAMP
                    """,
                    values,
                )
                conn.commit()

        created = _get_campaign_row(campaign_id)
        return {"status": "success", "campaign_id": campaign_id, "campaign": created}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"create_campaign failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create campaign")


@router.get("/campaigns")
async def list_campaigns(
    include_archived: bool = False,
    view: str = Query("summary", pattern="^(summary|full)$"),
    user: UserIdentity = Depends(get_current_user),
):
    """List campaigns (active by default), each with a live child job_count.
    Non-admins only see campaigns they're assigned to (or unassigned ones),
    matching the jobs ownership model."""
    try:
        where = "" if include_archived else "WHERE c.status != 'archived'"
        with get_dict_cursor_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT c.*,
                           (SELECT COUNT(*) FROM monitored_jobs m
                            WHERE m.campaign_id = c.campaign_id) AS job_count
                    FROM campaigns c
                    {where}
                    ORDER BY c.created_at DESC
                    """
                )
                rows = cur.fetchall()

        campaigns = []
        for row in rows:
            parsed = _parse_row(row)
            # Ownership filter uses the full recruiter_emails list, so run it
            # before the summary view drops any fields.
            if not user.is_admin and not _user_can_access_campaign(parsed, user):
                continue
            if view == "summary":
                # Drop the heavy template blobs from the list payload.
                for f in _JSONB_FIELDS + ("template_ai_description",):
                    parsed.pop(f, None)
            campaigns.append(parsed)
        return {"campaigns": campaigns, "total_count": len(campaigns)}
    except Exception as e:
        logger.error(f"list_campaigns failed: {e}", exc_info=True)
        return {"campaigns": [], "total_count": 0}


@router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, user: UserIdentity = Depends(get_current_user)):
    """Campaign detail + its child jobs."""
    try:
        campaign = _get_campaign_row(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        _ensure_campaign_access(campaign, user)

        with get_dict_cursor_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id, jobdiva_id, title, enhanced_title, customer_name,
                           status, screening_level, processing_status,
                           pair_launched_at, created_at,
                           city, state, location_type, employment_type, pay_rate, openings,
                           -- candidates_* are INTEGER per the DDL but TEXT on some
                           -- older DBs; cast-through-text keeps this robust to both.
                           COALESCE(NULLIF(candidates_launched::text, '')::int, 0) AS candidates_launched,
                           COALESCE(NULLIF(candidates_sourced::text, '')::int, 0) AS candidates_sourced
                    FROM monitored_jobs
                    WHERE campaign_id = %s AND is_archived IS NOT TRUE
                    ORDER BY created_at DESC
                    """,
                    (campaign_id,),
                )
                jobs = [dict(r) for r in cur.fetchall()]
        for j in jobs:
            for f in ("pair_launched_at", "created_at"):
                if j.get(f) is not None and not isinstance(j[f], str):
                    j[f] = j[f].isoformat()

        campaign["jobs"] = jobs
        campaign["job_count"] = len(jobs)
        return campaign
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_campaign failed for {campaign_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch campaign")


@router.put("/campaigns/{campaign_id}")
async def update_campaign(
    campaign_id: str,
    campaign: CampaignData,
    user: UserIdentity = Depends(get_current_user),
):
    """Update a campaign's common props + template. Does NOT re-propagate to
    existing child jobs (they copied their values at creation)."""
    existing = _get_campaign_row(campaign_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Campaign not found")
    _ensure_campaign_access(existing, user)
    campaign.campaign_id = campaign_id
    return await create_campaign(campaign, user)


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str, user: UserIdentity = Depends(get_current_user)):
    """Soft-delete: mark archived. Child jobs keep their campaign_id + rows
    (they may be mid-PAIR and are needed for history)."""
    try:
        existing = _get_campaign_row(campaign_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Campaign not found")
        _ensure_campaign_access(existing, user)
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE campaigns SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE campaign_id = %s",
                    (campaign_id,),
                )
                conn.commit()
        invalidate_monitored_jobs_cache()
        return {"status": "success", "campaign_id": campaign_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_campaign failed for {campaign_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete campaign")


async def _seed_job_rubric(campaign: Dict[str, Any], ref: str, bot_introduction: Optional[str] = None) -> None:
    """Seed a child job's rubric + screening-questions satellite tables from the
    campaign template. Generates custom technical questions for the specific job description
    and merges them with campaign baseline defaults before persisting."""
    template_rubric = campaign.get("template_rubric") or {}
    template_questions = list(campaign.get("template_screen_questions") or [])
    template_sourcing = campaign.get("template_sourcing_filters") or {}
    try:
        from services.job_rubric_db import JobRubricDB
        from routers._helpers import get_db_connection
        import json

        # Always resolve to the *canonical* jobdiva_id stored in monitored_jobs
        # and retrieve child-job details to generate tailored questions.
        canonical_ref = ref
        job_title = ""
        job_desc = ""
        city = ""
        loc_type = "Onsite"
        screening_lvl = "L1.5"

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT jobdiva_id, title, jobdiva_description, city, location_type, screening_level, enhanced_title 
                    FROM monitored_jobs WHERE jobdiva_id = %s OR job_id = %s LIMIT 1
                    """,
                    (ref, ref),
                )
                row = cur.fetchone()
                if row:
                    canonical_ref = row[0] or ref
                    job_title = (row[6] or row[1] or "").strip()
                    job_desc = row[2] or ""
                    city = row[3] or ""
                    loc_type = row[4] or "Onsite"
                    screening_lvl = row[5] or "L1.5"

        if not (template_rubric or template_questions or template_sourcing or bot_introduction or campaign.get("template_enhanced_title") or campaign.get("template_ai_description") or job_title or job_desc):
            return

        # Generate dynamic technical/role-specific questions for this job description
        try:
            from core.llm_client import get_openai_client
            from services.screening_question_generator import generate_screening_questions
            openai_client = get_openai_client()
            if openai_client:
                logger.info(f"Generating custom technical questions for child job {canonical_ref}...")
                tech_questions = await generate_screening_questions(
                    openai_client=openai_client,
                    model="gpt-4o-mini",
                    job_title=job_title,
                    rubric=template_rubric,
                    screening_level=screening_lvl,
                    customer_name=campaign.get("customer_name") or "",
                    job_description=job_desc,
                    work_arrangement=loc_type,
                    city=city,
                )
                # Filter: keep only technical/role-specific questions from the generated set.
                # Exclude generic front-matter and logistics categories that come from the LLM.
                _EXCLUDE_CATS = {"default", "work-arrangement", "intro", "logistics"}
                tech_only = [
                    q for q in (tech_questions or [])
                    if str((q or {}).get("category", "")).lower() not in _EXCLUDE_CATS
                ]
                logger.info(f"Generated {len(tech_only)} custom technical questions for child job {canonical_ref}.")

                # Target order:
                #   [default / intro / behavioral]   ← campaign template questions
                #   [logistics tail]                  ← availability, compensation, work-auth
                #   [role-specific tech]              ← always at the very end
                template_questions = template_questions + tech_only

                # Re-index the full merged list sequentially to avoid order_index collisions.
                for _i, _q in enumerate(template_questions):
                    _q["order_index"] = _i
        except Exception as gen_err:
            logger.warning(f"Technical question generation failed for child job {canonical_ref}: {gen_err}")

        rubric_payload = dict(template_rubric)
        if template_questions:
            rubric_payload["screen_questions"] = template_questions
        JobRubricDB().save_full_rubric(
            jobdiva_id=canonical_ref,
            rubric_obj=rubric_payload,
            recruiter_notes=campaign.get("recruiter_notes"),
            bot_introduction=bot_introduction,
        )

        sourcing_payload = dict(template_sourcing) if isinstance(template_sourcing, dict) and template_sourcing else {}
        if not sourcing_payload and template_rubric:
            titles = [
                {
                    "id": i + 1,
                    "value": t.get("value", ""),
                    "matchType": "must" if t.get("required") == "Required" else "can",
                    "years": t.get("minYears", 0),
                    "recent": False,
                    "similarCount": "0",
                    "similarTitles": [],
                    "fromRubric": True,
                }
                for i, t in enumerate(template_rubric.get("titles") or [])
            ]
            skills = [
                {
                    "id": i + 1,
                    "value": s.get("value", ""),
                    "matchType": "must" if s.get("required") == "Required" else "can",
                    "years": s.get("minYears", 0),
                    "recent": False,
                    "similarCount": "0",
                    "similarSkills": [],
                    "fromRubric": True,
                }
                for i, s in enumerate(template_rubric.get("skills") or [])
            ]
            sourcing_payload = {
                "sources": {"jobdiva": True, "linkedin": False, "dice": False, "exa": False},
                "titles": titles,
                "skills": skills,
                "locations": [],
                "companies": [],
                "keywords": [],
                "recentDaysFilter": 90,
                "includeNoResume": False,
            }

        resume_filters = []
        filter_id = 1
        for title in (template_rubric.get("titles") or []):
            is_req = title.get("required") == "Required"
            cat = "Required Title" if is_req else "Preferred Title"
            val = title.get("value", "")
            display = f"{val} — {title.get('minYears', 0)}+ yrs, {title.get('matchType', 'broad')} match"
            resume_filters.append({
                "id": filter_id, "category": cat, "value": display, "active": is_req, "ai": True, "fromRubric": True, "rubricKey": f"{cat}:{val.split('—')[0].strip().lower()}", "weight": 1
            })
            filter_id += 1
        for skill in (template_rubric.get("skills") or []):
            is_req = skill.get("required") == "Required"
            cat = "Required Skill" if is_req else "Preferred Skill"
            val = skill.get("value", "")
            display = f"{val} — {skill.get('minYears', 0)}+ yrs, {skill.get('matchType', 'broad')} match"
            resume_filters.append({
                "id": filter_id, "category": cat, "value": display, "active": is_req, "ai": True, "fromRubric": True, "rubricKey": f"{cat}:{val.split('—')[0].strip().lower()}", "weight": 1
            })
            filter_id += 1
        for edu in (template_rubric.get("education") or []):
            is_req = edu.get("required") == "Required"
            display = f"{edu.get('degree', '')}{' in ' + edu.get('field', '') if edu.get('field') else ''}"
            resume_filters.append({
                "id": filter_id, "category": "Education", "value": display, "active": is_req, "ai": True, "fromRubric": True, "rubricKey": f"Education:{display.split('—')[0].strip().lower()}", "weight": 1
            })
            filter_id += 1

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE monitored_jobs
                    SET sourcing_filters = CASE
                            WHEN sourcing_filters IS NULL OR sourcing_filters::text IN ('null', '[]', '{}') OR (jsonb_typeof(sourcing_filters->'skills') = 'array' AND jsonb_array_length(sourcing_filters->'skills') = 0)
                            THEN %s::jsonb ELSE sourcing_filters END,
                        resume_match_filters = CASE
                            WHEN resume_match_filters IS NULL OR resume_match_filters::text IN ('null', '[]') OR (jsonb_typeof(resume_match_filters) = 'array' AND jsonb_array_length(resume_match_filters) = 0)
                            THEN %s::jsonb ELSE resume_match_filters END
                    WHERE jobdiva_id = %s OR job_id = %s
                """, (json.dumps(sourcing_payload), json.dumps(resume_filters), ref, ref))
            conn.commit()
    except Exception as e:
        logger.warning(f"template rubric seed failed for {ref}: {e}")


async def _create_campaign_job(
    campaign: Dict[str, Any],
    campaign_id: str,
    jobdiva_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    screening_level: Optional[str] = None,
    selected_job_boards: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Create one child job under a campaign. When a jobdiva_id is given, first
    pull the real requirement details from JobDiva; on failure (or for external
    requirements) fall back to the campaign's JD template. Common props + JD +
    campaign_id are always inherited; rubric/questions are seeded from the
    template. Returns a per-job result dict."""
    from services.jobdiva import jobdiva_service
    import time

    fetched = None
    if jobdiva_id:
        try:
            fetched = await jobdiva_service.get_job_by_id(jobdiva_id)
        except Exception as e:
            logger.warning(f"JobDiva fetch failed for {jobdiva_id}: {e}")
            fetched = None

    if fetched and fetched.get("title"):
        # Real JobDiva metadata: numeric id is the PK, ref is the hyphenated code.
        job_id = str(fetched.get("id") or jobdiva_id)
        ref = str(fetched.get("jobdiva_id") or jobdiva_id)
        data: Dict[str, Any] = {
            "job_id": job_id,
            "jobdiva_id": ref,
            "title": fetched.get("title") or campaign.get("template_enhanced_title") or "",
            "customer_name": fetched.get("customer_name") or fetched.get("company") or campaign.get("customer_name") or "",
            "city": fetched.get("city") or "",
            "state": fetched.get("state") or "",
            "zip_code": fetched.get("zip_code") or "",
            "location_type": fetched.get("location_type") or "Onsite",
            "jobdiva_description": fetched.get("jobdiva_description") or fetched.get("description") or "",
            "employment_type": fetched.get("employment_type") or "",
            "pay_rate": fetched.get("pay_rate") or "",
            "openings": fetched.get("openings") or "",
            "posted_date": fetched.get("posted_date") or "",
            "start_date": fetched.get("start_date") or "",
            "priority": fetched.get("priority") or "",
            "program_duration": fetched.get("program_duration") or "",
            "max_allowed_submittals": fetched.get("max_allowed_submittals") or "",
        }
    else:
        # External requirement, or JobDiva fetch unavailable — template-based stub.
        job_id = jobdiva_id or f"MANUAL_{int(time.time() * 1000)}"
        ref = jobdiva_id or job_id
        data = {
            "job_id": job_id,
            "jobdiva_id": jobdiva_id or "",
            "title": title or campaign.get("template_enhanced_title") or "",
            "customer_name": campaign.get("customer_name") or "",
            "jobdiva_description": description or "",
        }

    # Resolve Bot Introduction: use campaign template or default standard intro,
    # then interpolate job-specific details when the child job is added under the campaign.
    # Matching the job wizard: enhanced_title is prioritized over title, location prefixes are stripped, and staffing agency is always Pyramid Consulting.
    def _clean_job_title_for_intro(title: str) -> str:
        if not title:
            return "role"
        import re
        cleaned = re.sub(r'^(?:(?:US|USA|CAN|CANADA|UK|INDIA|MEX|APAC|EMEA|LATAM|[A-Z]{2,3}(?:\/[A-Z]{2,3})?)\s*[-:/|]\s*)+', '', str(title), flags=re.IGNORECASE).strip()
        return cleaned or "role"

    raw_intro = campaign.get("bot_introduction") or ""
    child_job_title = (data.get("title") or "").strip()
    campaign_seed_title = (campaign.get("template_enhanced_title") or "").strip()
    raw_title_str = child_job_title or campaign_seed_title or "role"
    job_title_str = _clean_job_title_for_intro(raw_title_str)
    job_location_str = (
        f"{data.get('city')}, {data.get('state')}".strip(", ")
        if (data.get("city") and data.get("state"))
        else (data.get("city") or data.get("state") or "your area")
    )

    if not raw_intro.strip():
        raw_intro = (
            f"Hi {{{{candidate name}}}}, I'm Alex, a virtual recruiter with Pyramid Consulting. "
            f"We are helping our client recruit for a {job_title_str} in {job_location_str}, "
            f"and you seem to be a good fit for the role. Please note that conversation may be recorded "
            f"for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?"
        )
    else:
        import re
        raw_intro = re.sub(r'\{\{\s*(?:job_title|title)\s*\}\}|\{\s*(?:job_title|title)\s*\}', job_title_str, raw_intro, flags=re.IGNORECASE)
        raw_intro = re.sub(r'\{\{\s*(?:job_location|location)\s*\}\}|\{\s*(?:job_location|location)\s*\}', job_location_str, raw_intro, flags=re.IGNORECASE)
        raw_intro = re.sub(r'\{\{\s*(?:customer_name|company)\s*\}\}|\{\s*(?:customer_name|company)\s*\}', 'Pyramid Consulting', raw_intro, flags=re.IGNORECASE)
        seed_title = campaign_seed_title
        if seed_title and seed_title in raw_intro and seed_title != job_title_str and job_title_str:
            raw_intro = raw_intro.replace(seed_title, job_title_str)
        campaign_name = (campaign.get("name") or "").strip()
        if campaign_name and campaign_name in raw_intro and campaign_name != job_title_str and job_title_str:
            raw_intro = re.sub(r'(recruit\s+for\s+a(?:n)?\s+)' + re.escape(campaign_name), r'\1' + job_title_str, raw_intro, flags=re.IGNORECASE)

    data.update({
        "campaign_id": campaign_id,
        "enhanced_title": job_title_str if child_job_title else campaign_seed_title,
        "ai_description": campaign.get("template_ai_description") or "",
        "recruiter_notes": campaign.get("recruiter_notes") or "",
        "work_authorization": campaign.get("work_authorization") or data.get("work_authorization") or "",
        "recruiter_emails": campaign.get("recruiter_emails") or [],
        "selected_employment_types": campaign.get("selected_employment_types") or [],
        "selected_job_boards": (
            selected_job_boards if selected_job_boards is not None else (campaign.get("selected_job_boards") or [])
        ),
        "screening_level": screening_level or campaign.get("screening_level") or "L1.5",
        "bot_introduction": raw_intro,
        "processing_status": "campaign_created",
        "sourcing_filters": campaign.get("template_sourcing_filters") or None,
    })

    ok = jobdiva_service.monitor_job_locally(data["job_id"], data)
    if ok:
        await _seed_job_rubric(campaign, ref, bot_introduction=raw_intro)
        if ref != str(data["job_id"]):
            await _seed_job_rubric(campaign, str(data["job_id"]), bot_introduction=raw_intro)

    return {
        "jobdiva_id": jobdiva_id or "",
        "job_id": data["job_id"],
        "ref": ref,
        "title": data.get("title"),
        "fetched": bool(fetched and fetched.get("title")),
        "ok": ok,
    }


@router.post("/campaigns/{campaign_id}/jobs")
async def add_job_to_campaign(
    campaign_id: str,
    req: CampaignAddJobRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Add a single job under a campaign (JobDiva import or external requirement),
    inheriting common props + JD/rubric/questions template and stamped with
    campaign_id."""
    try:
        campaign = _get_campaign_row(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        _ensure_campaign_access(campaign, user)
        if not req.jobdiva_id and not (req.title and req.description):
            raise HTTPException(
                status_code=400,
                detail="Provide jobdiva_id, or title + description for an external requirement",
            )
        result = await _create_campaign_job(
            campaign, campaign_id,
            jobdiva_id=req.jobdiva_id, title=req.title, description=req.description,
            screening_level=req.screening_level, selected_job_boards=req.selected_job_boards,
        )
        if not result["ok"]:
            raise HTTPException(status_code=500, detail="Failed to create job under campaign")
        invalidate_monitored_jobs_cache()
        return {"status": "success", "campaign_id": campaign_id, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add_job_to_campaign failed for {campaign_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add job to campaign")


@router.post("/campaigns/{campaign_id}/jobs/bulk")
async def bulk_add_jobs_to_campaign(
    campaign_id: str,
    req: CampaignBulkAddRequest,
    user: UserIdentity = Depends(get_current_user),
):
    """Add many JobDiva requirements at once. Accepts a list of ids (the frontend
    splits the comma-separated input). Each id is fetched from JobDiva and created
    under the campaign, inheriting the template. Returns a per-id result list so
    the UI can show which fetched vs fell back to the template."""
    try:
        campaign = _get_campaign_row(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        _ensure_campaign_access(campaign, user)

        # Accept a raw comma/newline string too, for robustness.
        raw_ids: List[str] = []
        for entry in (req.jobdiva_ids or []):
            raw_ids.extend(str(entry).replace("\n", ",").split(","))
        ids = []
        seen = set()
        for rid in raw_ids:
            v = rid.strip()
            if v and v not in seen:
                seen.add(v)
                ids.append(v)
        if not ids:
            raise HTTPException(status_code=400, detail="Provide at least one JobDiva Job ID")
        if len(ids) > _MAX_BULK_JOBS:
            raise HTTPException(
                status_code=400,
                detail=f"Too many jobs in one request ({len(ids)}); the maximum is {_MAX_BULK_JOBS}.",
            )

        results = []
        for jid in ids:
            try:
                results.append(await _create_campaign_job(campaign, campaign_id, jobdiva_id=jid))
            except Exception as e:
                logger.error(f"bulk add failed for {jid}: {e}", exc_info=True)
                results.append({"jobdiva_id": jid, "ok": False, "error": str(e)})

        added = sum(1 for r in results if r.get("ok"))
        fetched = sum(1 for r in results if r.get("fetched"))
        if added > 0:
            invalidate_monitored_jobs_cache()
        return {
            "status": "success",
            "campaign_id": campaign_id,
            "requested": len(ids),
            "added": added,
            "fetched_from_jobdiva": fetched,
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"bulk_add_jobs_to_campaign failed for {campaign_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add jobs to campaign")


@router.delete("/campaigns/{campaign_id}/jobs/{job_id}")
async def remove_job_from_campaign(
    campaign_id: str,
    job_id: str,
    action: str = Query("detach", pattern="^(detach|delete)$"),
    user: UserIdentity = Depends(get_current_user),
):
    """Remove or detach a child job from a campaign.

    If action='detach' (default): clears monitored_jobs.campaign_id, returning the job to standalone status in /jobs while keeping all candidate and screening data intact.
    If action='delete': completely deletes the requirement from monitored_jobs (and satellite tables) if it was added by mistake. Refused once the job has any sourced/launched candidates — detach instead so that history is preserved.
    """
    try:
        campaign = _get_campaign_row(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        _ensure_campaign_access(campaign, user)

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # First check if the job belongs to this campaign. Pull the
                # launch/sourcing markers too so a destructive delete can be
                # refused for jobs that already have candidate history.
                cur.execute(
                    """
                    SELECT job_id, jobdiva_id, pair_launched_at,
                           COALESCE(NULLIF(candidates_launched::text, '')::int, 0) AS cand_launched,
                           COALESCE(NULLIF(candidates_sourced::text, '')::int, 0) AS cand_sourced
                    FROM monitored_jobs
                    WHERE (job_id::text = %s OR jobdiva_id::text = %s) AND campaign_id = %s
                    LIMIT 1
                    """,
                    (job_id, job_id, campaign_id),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Job not found in this campaign")

                actual_job_id, actual_jobdiva_id = row[0], row[1]
                pair_launched_at, cand_launched, cand_sourced = row[2], row[3], row[4]

                if action == "detach":
                    cur.execute(
                        "UPDATE monitored_jobs SET campaign_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE job_id = %s",
                        (actual_job_id,),
                    )
                    conn.commit()
                    logger.info(f"Detached job {actual_job_id} ({actual_jobdiva_id}) from campaign {campaign_id}")
                else:
                    # action == "delete": hard-delete the requirement + satellites.
                    # Refuse when the job has candidate history — deleting it would
                    # orphan sourced_candidates / interview rows (logical links, no
                    # FK cascade) and irreversibly lose the requirement record.
                    if pair_launched_at is not None or (cand_launched or 0) > 0 or (cand_sourced or 0) > 0:
                        raise HTTPException(
                            status_code=409,
                            detail="Cannot delete a requirement that has sourced or launched candidates. Detach it from the campaign instead to preserve its history.",
                        )
                    job_ref = str(actual_jobdiva_id or actual_job_id or "")
                    cur.execute("DELETE FROM job_skills WHERE jobdiva_id = %s", (job_ref,))
                    cur.execute("DELETE FROM job_education WHERE jobdiva_id = %s", (job_ref,))
                    cur.execute("DELETE FROM job_titles WHERE jobdiva_id = %s", (job_ref,))
                    cur.execute("DELETE FROM job_customer_requirements WHERE jobdiva_id = %s", (job_ref,))
                    cur.execute("DELETE FROM job_other_requirements WHERE jobdiva_id = %s", (job_ref,))
                    cur.execute("DELETE FROM job_screen_questions WHERE jobdiva_id = %s", (job_ref,))
                    cur.execute("DELETE FROM monitored_jobs WHERE job_id = %s", (actual_job_id,))
                    conn.commit()
                    logger.info(f"Deleted requirement {actual_job_id} ({actual_jobdiva_id}) from campaign {campaign_id}")
                    
        invalidate_monitored_jobs_cache()
        return {"status": "success", "campaign_id": campaign_id, "job_id": job_id, "action": action}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"remove_job_from_campaign failed for campaign {campaign_id}, job {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to remove job from campaign")

