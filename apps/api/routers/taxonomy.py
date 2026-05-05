"""
taxonomy.py
-----------
Read-only sibling lookup over the role/skill taxonomies cached by
taxonomy_service. Used by the step-5 search builder to auto-populate
"similar" checklists when a recruiter types an ad-hoc title or skill
that wasn't in the JD-grounded rubric.

Pure in-memory; sub-millisecond after the per-process cache is warm.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from services import taxonomy_service

router = APIRouter()
logger = logging.getLogger(__name__)


class SimilarResponse(BaseModel):
    term: str
    anchor: Optional[str]
    similar: List[str]


@router.get("/similar-titles", response_model=SimilarResponse)
def similar_titles(
    term: str = Query(..., min_length=1, description="The title to find siblings for"),
    limit: int = Query(8, ge=1, le=50),
    level: str = Query("role_k1500", description="Hierarchy level for the cluster lookup"),
):
    similar = taxonomy_service.find_similar_titles(term, level=level, limit=limit)
    anchor = taxonomy_service._resolve_term(term, "role") if similar else None
    return SimilarResponse(term=term, anchor=anchor, similar=similar)


@router.get("/similar-skills", response_model=SimilarResponse)
def similar_skills(
    term: str = Query(..., min_length=1, description="The skill to find siblings for"),
    limit: int = Query(8, ge=1, le=50),
    level: str = Query("skill_k1500", description="Hierarchy level for the cluster lookup"),
):
    similar = taxonomy_service.find_similar_skills(term, level=level, limit=limit)
    anchor = taxonomy_service._resolve_term(term, "skill") if similar else None
    return SimilarResponse(term=term, anchor=anchor, similar=similar)
