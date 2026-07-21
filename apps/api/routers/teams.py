"""Team management endpoints (admin-managed recruiter teams).

Admins create/update/delete teams; each team has one or more leads and any
number of members (comma-separated emails in the UI). Team leads get a
team-scoped analytics dashboard (see routers/admin_analytics.py) and a
team-scoped jobs list (see routers/jobs.py:_filter_jobs_for_user).
"""

import asyncio
import logging
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import get_current_user, UserIdentity
from services import teams_db

router = APIRouter(prefix="/api/v1", tags=["Teams"])
logger = logging.getLogger(__name__)


class TeamPayload(BaseModel):
    name: str
    # Accept either a list of emails or one comma-separated string — the
    # Add-Team modal sends whatever the admin typed, verbatim.
    lead_emails: Union[List[str], str, None] = None
    member_emails: Union[List[str], str, None] = None


def _require_admin(user: UserIdentity) -> None:
    if not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Admin access required to manage teams.",
        )


async def init_teams_schema() -> None:
    """Startup hook — mirrors init_*_schema on the other routers."""
    await teams_db.init_teams_schema()


@router.get("/teams")
async def list_teams(user: UserIdentity = Depends(get_current_user)):
    """Admins see every team; team leads see only their own team."""
    if user.is_admin:
        teams = await asyncio.to_thread(teams_db.list_teams)
    elif user.is_team_lead and user.team_id:
        team = await asyncio.to_thread(teams_db.get_team, user.team_id)
        teams = [team] if team else []
    else:
        raise HTTPException(
            status_code=403,
            detail="Access denied. Teams are visible to admins and team leads only.",
        )
    return {"status": "success", "data": {"teams": teams}}


@router.post("/teams")
async def create_team(payload: TeamPayload, user: UserIdentity = Depends(get_current_user)):
    _require_admin(user)
    try:
        team = await asyncio.to_thread(
            teams_db.create_team,
            payload.name,
            payload.lead_emails,
            payload.member_emails,
            user.email,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "data": {"team": team}}


@router.put("/teams/{team_id}")
async def update_team(
    team_id: str, payload: TeamPayload, user: UserIdentity = Depends(get_current_user)
):
    _require_admin(user)
    try:
        team = await asyncio.to_thread(
            teams_db.update_team,
            team_id,
            payload.name,
            payload.lead_emails,
            payload.member_emails,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "data": {"team": team}}


@router.delete("/teams/{team_id}")
async def delete_team(team_id: str, user: UserIdentity = Depends(get_current_user)):
    _require_admin(user)
    deleted = await asyncio.to_thread(teams_db.delete_team, team_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Team '{team_id}' not found.")
    return {"status": "success", "data": {"deleted": True}}
