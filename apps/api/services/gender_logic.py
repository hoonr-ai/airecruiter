"""Isolated gender normalization and PAIR payload enrichment helpers.

This module is intentionally additive and side-effect free so teams can adopt
it without modifying existing logic until they are ready.

Canonical labels:
- male
- female
- default

Back-compat input aliases accepted:
- else -> default
- unknown/other/unspecified -> default
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx

from core.config import OPENAI_API_KEY, OPENAI_MODEL

CANONICAL_GENDER_LABELS = {"male", "female", "default"}
_AI_INFER_SEMAPHORE = asyncio.Semaphore(8)
_AI_NAME_CACHE: Dict[str, "GenderPrediction"] = {}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenderPrediction:
    """Normalized gender prediction contract."""

    gender_label: str
    gender_confidence: float
    gender_source: str
    gender_updated_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_label(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"male", "m", "man", "boy"}:
        return "male"
    if raw in {"female", "f", "woman", "girl"}:
        return "female"
    if raw in {"default", "else", "unknown", "other", "unspecified", "n/a", "na", ""}:
        return "default"
    return "default"


def _clamp_confidence(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        return 1.0
    return numeric


def normalize_gender_prediction(
    *,
    predicted_label: Any,
    confidence: Any = 0.0,
    source: Optional[str] = None,
    threshold: float = 0.8,
    updated_at: Optional[str] = None,
) -> GenderPrediction:
    """Normalize arbitrary model output to canonical labels.

    Rules:
    - canonical output is male/female/default only
    - confidence below threshold forces default
    - non-canonical/unknown labels force default
    """

    label = _clean_label(predicted_label)
    conf = _clamp_confidence(confidence)
    src = (source or "inferred").strip().lower() or "inferred"

    if label not in {"male", "female"}:
        label = "default"
    elif conf < float(threshold):
        label = "default"

    ts = updated_at or _utc_now_iso()
    return GenderPrediction(
        gender_label=label,
        gender_confidence=conf,
        gender_source=src,
        gender_updated_at=ts,
    )


def resolve_gender_with_priority(
    *,
    self_declared_label: Any = None,
    self_declared_confidence: Any = 1.0,
    inferred_label: Any = None,
    inferred_confidence: Any = 0.0,
    threshold: float = 0.8,
    updated_at: Optional[str] = None,
) -> GenderPrediction:
    """Resolve final gender using self-declared value first, then inferred.

    Self-declared values are trusted and not threshold-gated unless invalid.
    Invalid self-declared values fall back to inferred normalization.
    """

    declared = _clean_label(self_declared_label)
    if declared in CANONICAL_GENDER_LABELS:
        return GenderPrediction(
            gender_label=declared,
            gender_confidence=_clamp_confidence(self_declared_confidence),
            gender_source="self_declared",
            gender_updated_at=updated_at or _utc_now_iso(),
        )

    return normalize_gender_prediction(
        predicted_label=inferred_label,
        confidence=inferred_confidence,
        source="inferred",
        threshold=threshold,
        updated_at=updated_at,
    )


def to_gender_fields(prediction: GenderPrediction) -> Dict[str, Any]:
    """Serialize prediction to dict payload fields."""

    return {
        "gender_label": prediction.gender_label,
        "gender_confidence": prediction.gender_confidence,
        "gender_source": prediction.gender_source,
        "gender_updated_at": prediction.gender_updated_at,
    }


async def infer_gender_from_name_ai(
    name: Any,
    *,
    threshold: float = 0.6,
) -> GenderPrediction:
    """Infer gender from candidate name via LLM.

    Returns canonical labels only: male/female/default.
    Falls back safely to default on any failure or uncertainty.
    """

    clean_name = str(name or "").strip()
    if not clean_name:
        return normalize_gender_prediction(
            predicted_label="default",
            confidence=0.0,
            source="inferred_ai_name",
            threshold=0.0,
        )

    cache_key = clean_name.lower()
    cached = _AI_NAME_CACHE.get(cache_key)
    if cached is not None:
        return cached

    if not OPENAI_API_KEY or not OPENAI_MODEL:
        return normalize_gender_prediction(
            predicted_label="default",
            confidence=0.0,
            source="inferred_ai_name",
            threshold=0.0,
        )

    prompt = (
        "Predict likely gender from the provided first+last name only. "
        "Return strict JSON with keys: label, confidence. "
        "Allowed label values: male, female, default. "
        "Use default if uncertain or ambiguous. "
        f"Name: {clean_name}"
    )

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict classifier. Respond with JSON only: "
                    "{\"label\":\"male|female|default\",\"confidence\":0.0-1.0}."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 40,
    }

    try:
        async with _AI_INFER_SEMAPHORE:
            async with httpx.AsyncClient(timeout=15.0) as client:
                res = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                res.raise_for_status()
                body = res.json()

        content = (
            (((body.get("choices") or [{}])[0] or {}).get("message") or {}).get("content")
            or "{}"
        )
        parsed = json.loads(content)
        pred = normalize_gender_prediction(
            predicted_label=parsed.get("label"),
            confidence=parsed.get("confidence", 0.0),
            source="inferred_ai_name",
            threshold=threshold,
        )
    except Exception as exc:
        logger.debug("infer_gender_from_name_ai failed for name=%r: %s", clean_name, exc)
        pred = normalize_gender_prediction(
            predicted_label="default",
            confidence=0.0,
            source="inferred_ai_name",
            threshold=0.0,
        )

    _AI_NAME_CACHE[cache_key] = pred
    return pred


def enrich_pair_resumes_payload(
    resumes: Iterable[Dict[str, Any]],
    *,
    gender_by_candidate_id: Optional[Dict[str, Dict[str, Any]]] = None,
    default_label: str = "default",
) -> List[Dict[str, Any]]:
    """Return a new PAIR resumes list with additive gender fields.

    Input resumes are not mutated.

    Expected resume key for lookup:
    - source_candidate_id

    gender_by_candidate_id value format (flexible):
    {
      "<candidate_id>": {
        "gender_label": "male|female|default|else|...",
        "gender_confidence": 0.91,
        "gender_source": "self_declared|inferred|unknown",
        "gender_updated_at": "..."
      }
    }
    """

    default_norm = _clean_label(default_label)
    if default_norm not in CANONICAL_GENDER_LABELS:
        default_norm = "default"

    lookup = gender_by_candidate_id or {}
    enriched: List[Dict[str, Any]] = []

    for row in resumes:
        candidate_id = str(row.get("source_candidate_id") or "").strip()
        existing = lookup.get(candidate_id, {}) if candidate_id else {}

        normalized = normalize_gender_prediction(
            predicted_label=existing.get("gender_label", default_norm),
            confidence=existing.get("gender_confidence", 0.0),
            source=existing.get("gender_source", "unknown"),
            threshold=0.0,  # explicit payload values should pass through normalization only
            updated_at=existing.get("gender_updated_at"),
        )

        out = dict(row)
        out.update(to_gender_fields(normalized))
        enriched.append(out)

    return enriched
