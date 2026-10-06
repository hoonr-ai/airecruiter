"""Org hierarchy endpoints (admin-managed).

  GET  /api/v1/org-hierarchy          the whole tree + counts + last import
  POST /api/v1/org-hierarchy/import   preview (default) or apply the mapping sheet
  GET  /api/v1/org-hierarchy/export   the current tree as a sheet the importer reads

The tree decides who sees whose jobs and admin analytics (see
services/org_hierarchy.py and core/auth.py). It controls access to every
recruiter's data, so every route here is admin-only, and an import previews
first: nothing is replaced until the caller sends dry_run=false.
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Set

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import UserIdentity, get_current_user
from routers._helpers import _parse_recruiter_emails, get_db_connection
from services import org_hierarchy

router = APIRouter(prefix="/api/v1/org-hierarchy", tags=["Org Hierarchy"])
logger = logging.getLogger(__name__)


class ImportPayload(BaseModel):
    # The sheet saved as CSV (or pasted/tab-separated) — see plan_import for columns.
    csv: str = Field(..., max_length=2_000_000)
    # Preview unless the caller explicitly asks to apply.
    dry_run: bool = True
    # Which level a "Regional/Vertical Head" with no row of their own is. The
    # sheet does not say, so the admin chooses.
    head_role: str = org_hierarchy.DEFAULT_HEAD_ROLE


def _require_admin(user: UserIdentity) -> None:
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin access required to manage the org hierarchy.",
        )


async def init_org_hierarchy_schema() -> None:
    """Startup hook — mirrors init_*_schema on the other routers."""
    await org_hierarchy.init_org_hierarchy_schema()


def _jobs_coverage(emails: Set[str]) -> Optional[Dict[str, int]]:
    """How many of the sheet's emails are assigned to a job today.

    The guard against the quiet failure: the sheet imports cleanly but its
    emails are not the ones JobDiva puts on jobs, so no manager sees anything.
    Best effort — it is a hint on a preview, never a reason to fail one.
    """
    conn = None
    try:
        conn = get_db_connection()
        assigned: Set[str] = set()
        with conn.cursor() as cur:
            cur.execute("SELECT recruiter_emails FROM monitored_jobs")
            for (raw,) in cur.fetchall():
                assigned.update(_parse_recruiter_emails(raw))
        return {
            "people_with_email": len(emails),
            "assigned_to_a_job": len(emails & assigned),
        }
    except Exception as exc:  # noqa: BLE001 - a hint, not a dependency
        logger.warning(f"org hierarchy: job coverage unavailable: {exc}")
        return None
    finally:
        if conn is not None:
            conn.close()


def _import_sync(payload: ImportPayload, imported_by: str) -> Dict[str, Any]:
    plan = org_hierarchy.plan_import(payload.csv, head_role=payload.head_role)
    data = plan.to_dict()
    planned_emails = {m.email for m in plan.members if m.email}
    try:
        current = org_hierarchy.current_emails()
        data["diff"] = {
            "current_people_with_email": len(current),
            "emails_added": len(planned_emails - current),
            "emails_removed": len(current - planned_emails),
            "removed_sample": sorted(current - planned_emails)[:20],
        }
    except Exception as exc:  # noqa: BLE001 - table may not exist yet on a first import
        logger.info(f"org hierarchy: no current tree to diff against: {exc}")
        data["diff"] = None
    data["coverage"] = _jobs_coverage(planned_emails)
    data["dry_run"] = payload.dry_run
    data["applied"] = False
    if not payload.dry_run:
        if plan.blocking:
            raise HTTPException(
                status_code=400,
                detail={"message": "The import has errors; nothing was changed.", **data},
            )
        org_hierarchy.apply_plan(plan, imported_by)
        data["applied"] = True
    return data


@router.get("")
async def get_org_hierarchy(user: UserIdentity = Depends(get_current_user)):
    _require_admin(user)
    members = await asyncio.to_thread(org_hierarchy.list_members)
    last = await asyncio.to_thread(org_hierarchy.last_import)
    return {"status": "success", "data": {**org_hierarchy.build_overview(members), "last_import": last}}


@router.post("/import")
async def import_org_hierarchy(payload: ImportPayload, user: UserIdentity = Depends(get_current_user)):
    _require_admin(user)
    if payload.head_role not in org_hierarchy.LEVELS[1:]:
        raise HTTPException(
            status_code=400,
            detail=f"head_role must be one of: {', '.join(org_hierarchy.LEVELS[1:])}.",
        )
    try:
        data = await asyncio.to_thread(_import_sync, payload, user.email)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "data": data}


@router.get("/export")
async def export_org_hierarchy(user: UserIdentity = Depends(get_current_user)):
    """The current tree in the importer's own format, so an admin can fill in
    missing emails (or add Delivery Managers / Directors / AVPs) and upload it
    back. Returned as JSON so the browser's authenticated fetch can save it."""
    _require_admin(user)
    members = await asyncio.to_thread(org_hierarchy.list_members)
    return {
        "status": "success",
        "data": {"filename": "org-hierarchy.csv", "csv": org_hierarchy.export_csv(members)},
    }
