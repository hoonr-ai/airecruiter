"""DNC list read endpoint.

Surfaces the normalized phone set so the frontend can mark sourcing rows
red and skip them at Launch PAIR. The list is small (~95 rows in
production), so we ship the whole set rather than expose a per-phone
``GET /dnc/check?phone=…`` lookup. The backend save endpoint enforces
defense-in-depth in apps/api/routers/candidates.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from services.dnc_storage import load_dnc_phone_set

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dnc", tags=["DNC"])


@router.get("/keys")
def get_dnc_keys() -> dict:
    phones = load_dnc_phone_set()
    return {"phones": sorted(phones)}
