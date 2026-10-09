"""
Per-stage timing for the JobDiva ID -> sourcing -> PAIR launch pipeline.

Every stage wrapped in ``span(...)`` emits one structured log line and one
New Relic ``PipelineStage`` custom event, tagged with the current
``pipeline_run_id``. Sum them per run to see where wall-clock time goes:

    SELECT sum(ms) FROM PipelineStage WHERE run_id = '...' FACET stage

PII policy: ``span(**attrs)`` values are shipped to New Relic, so attrs must
be non-PII identifiers/counters only (job_id, batch_idx, scope, n, source).
``_safe_attrs`` enforces this: only int/float/bool and short strings pass,
and keys that look like PII (name, email, phone, resume, ...) are dropped.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Optional

from core.newrelic import record_custom_event

logger = logging.getLogger(__name__)

run_id: ContextVar[Optional[str]] = ContextVar("pipeline_run_id", default=None)
job_id: ContextVar[Optional[str]] = ContextVar("pipeline_job_id", default=None)


def new_run(jobdiva_id: Any) -> str:
    """Start a pipeline run for this task context and return its id."""
    rid = f"{jobdiva_id}:{uuid.uuid4().hex[:8]}"
    run_id.set(rid)
    job_id.set(str(jobdiva_id) if jobdiva_id is not None else None)
    return rid


_MAX_STR_LEN = 64
_PII_KEY_PARTS = (
    "name", "email", "mail", "phone", "mobile", "resume", "cv", "address",
    "linkedin", "url", "text", "ssn", "dob", "birth", "contact", "body",
    "content", "message", "token", "password", "secret",
)


def _safe_attrs(attrs: dict) -> dict:
    """Keep only non-PII scalar attrs: numbers/bools and short strings."""
    out = {}
    for k, v in attrs.items():
        key = str(k).lower()
        if any(part in key for part in _PII_KEY_PARTS):
            continue
        if isinstance(v, (bool, int, float)):
            out[k] = v
        elif isinstance(v, str) and len(v) <= _MAX_STR_LEN and "@" not in v:
            out[k] = v
    return out


def _emit(stage: str, ms: int, ok: bool, attrs: dict) -> None:
    payload = {"run_id": run_id.get(), "job_id": job_id.get(), "stage": stage, "ms": ms, "ok": ok, **_safe_attrs(attrs)}
    try:
        logger.info("pipeline_stage stage=%s ms=%d ok=%s run_id=%s", stage, ms, ok, payload["run_id"], extra={"pipeline": payload})
        record_custom_event("PipelineStage", payload)
    except Exception:  # tracing must never break the pipeline
        pass


@asynccontextmanager
async def span(stage: str, **attrs: Any):
    t = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        _emit(stage, int((time.perf_counter() - t) * 1000), ok, attrs)


@contextmanager
def span_sync(stage: str, **attrs: Any):
    t = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        _emit(stage, int((time.perf_counter() - t) * 1000), ok, attrs)
