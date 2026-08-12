"""No-contact company list — read-only admin view.

The list itself lives in code (core.sourcing_config.NO_CONTACT_COMPANIES);
adding/removing companies is a code change by design for now, so this router
deliberately exposes no write endpoint. Admin-gated: the list names client
relationships and has no use outside the admin surface.

Mounted with prefix under /api/v1 so the existing nginx `location /api/`
passthrough routes it — no nginx allowlist change needed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import UserIdentity, get_current_user
from services.no_contact import get_no_contact_companies

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/no-contact", tags=["No Contact"])


@router.get("/companies")
def list_no_contact_companies(
    user: UserIdentity = Depends(get_current_user),
) -> dict:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return {
        "companies": get_no_contact_companies(),
        # The UI renders this list read-only; flips to True if the list ever
        # moves to DB-backed admin editing.
        "editable": False,
    }
