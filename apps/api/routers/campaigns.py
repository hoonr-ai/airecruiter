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

from fastapi import APIRouter, HTTPException, Query
from typing import List, Dict, Any, Optional
import logging
import json
import time

from models import CampaignData, CampaignAddJobRequest
from routers._helpers import get_db_connection, get_dict_cursor_connection
from services.jobdiva import jobdiva_service

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


@router.post("/campaigns")
async def create_campaign(campaign: CampaignData):
    """Create (or upsert) a campaign. Generates a CMP_{ts} id when absent."""
    try:
        campaign_id = campaign.campaign_id or f"CMP_{int(time.time() * 1000)}"
        data = campaign.dict()
        data["campaign_id"] = campaign_id

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
    except Exception as e:
        logger.error(f"create_campaign failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create campaign")


@router.get("/campaigns")
async def list_campaigns(
    include_archived: bool = False,
    view: str = Query("summary", pattern="^(summary|full)$"),
):
    """List campaigns (active by default), each with a live child job_count."""
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
async def get_campaign(campaign_id: str):
    """Campaign detail + its child jobs."""
    try:
        campaign = _get_campaign_row(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        with get_dict_cursor_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id, jobdiva_id, title, enhanced_title, customer_name,
                           status, screening_level, processing_status,
                           pair_launched_at, created_at,
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
async def update_campaign(campaign_id: str, campaign: CampaignData):
    """Update a campaign's common props + template. Does NOT re-propagate to
    existing child jobs (they copied their values at creation)."""
    if not _get_campaign_row(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign.campaign_id = campaign_id
    return await create_campaign(campaign)


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    """Soft-delete: mark archived. Child jobs keep their campaign_id + rows
    (they may be mid-PAIR and are needed for history)."""
    try:
        if not _get_campaign_row(campaign_id):
            raise HTTPException(status_code=404, detail="Campaign not found")
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE campaigns SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE campaign_id = %s",
                    (campaign_id,),
                )
                conn.commit()
        return {"status": "success", "campaign_id": campaign_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_campaign failed for {campaign_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete campaign")


@router.post("/campaigns/{campaign_id}/jobs")
async def add_job_to_campaign(campaign_id: str, req: CampaignAddJobRequest):
    """Create a monitored_jobs row under a campaign, inheriting the campaign's
    common props + JD template and stamped with campaign_id. Reuses
    jobdiva_service.monitor_job_locally (the single row-birth path).

    NOTE: this seeds the scalar common props + AI-JD template. Seeding the full
    rubric/screening-questions template into the satellite tables + auto-running
    sourcing is driven by the frontend add-job flow (Phase 4) via the existing
    per-job endpoints.
    """
    try:
        campaign = _get_campaign_row(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        if not req.jobdiva_id and not (req.title and req.description):
            raise HTTPException(
                status_code=400,
                detail="Provide jobdiva_id, or title + description for an external requirement",
            )

        job_id = req.jobdiva_id or f"MANUAL_{int(time.time())}"

        # Raw Python lists — monitor_job_locally json.dumps them exactly once.
        data = {
            "job_id": job_id,
            "jobdiva_id": req.jobdiva_id or "",
            "campaign_id": campaign_id,
            "title": req.title or campaign.get("template_enhanced_title") or "",
            "enhanced_title": campaign.get("template_enhanced_title") or req.title or "",
            "customer_name": req.customer_name or campaign.get("customer_name") or "",
            "jobdiva_description": req.description or "",
            "ai_description": campaign.get("template_ai_description") or "",
            "recruiter_notes": campaign.get("recruiter_notes") or "",
            "work_authorization": campaign.get("work_authorization") or "",
            "recruiter_emails": campaign.get("recruiter_emails") or [],
            "selected_employment_types": campaign.get("selected_employment_types") or [],
            "selected_job_boards": (
                req.selected_job_boards
                if req.selected_job_boards is not None
                else (campaign.get("selected_job_boards") or [])
            ),
            "screening_level": req.screening_level or campaign.get("screening_level") or "L1.5",
            "bot_introduction": campaign.get("bot_introduction") or "",
            "processing_status": "campaign_created",
        }

        ok = jobdiva_service.monitor_job_locally(job_id, data)
        if not ok:
            raise HTTPException(status_code=500, detail="Failed to create job under campaign")

        # Seed the job's rubric + screening-questions satellite tables from the
        # campaign template so the Source step (and PAIR launch) see the
        # inherited rubric/questions. save_full_rubric accepts the same dict
        # shape generate-rubric produces (which is what template_rubric holds),
        # and persists screen_questions when embedded. Keyed by the job's
        # reference (jobdiva_id for imports, else the MANUAL_* job_id).
        ref = req.jobdiva_id or job_id
        template_rubric = campaign.get("template_rubric") or {}
        template_questions = campaign.get("template_screen_questions") or []
        if template_rubric or template_questions:
            try:
                from services.job_rubric_db import JobRubricDB

                rubric_payload = dict(template_rubric)
                if template_questions:
                    rubric_payload["screen_questions"] = template_questions
                JobRubricDB().save_full_rubric(
                    jobdiva_id=ref,
                    rubric_obj=rubric_payload,
                    recruiter_notes=campaign.get("recruiter_notes"),
                    bot_introduction=campaign.get("bot_introduction"),
                )
            except Exception as e:
                # Non-fatal: the job row is created; the recruiter can still
                # (re)generate the rubric in the Source step if seeding failed.
                logger.warning(f"template rubric seed failed for {ref}: {e}")

        return {
            "status": "success",
            "campaign_id": campaign_id,
            "job_id": job_id,
            "jobdiva_id": req.jobdiva_id or "",
            "ref": ref,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"add_job_to_campaign failed for {campaign_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add job to campaign")
