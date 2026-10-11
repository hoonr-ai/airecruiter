"""
Per-stage timing for the JobDiva ID -> sourcing -> PAIR launch pipeline.

Every stage wrapped in ``span(...)`` emits one structured log line and one
New Relic ``PipelineStage`` custom event, tagged with the current
``pipeline_run_id``. Sum them per run to see where wall-clock time goes:

    SELECT sum(ms) FROM PipelineStage WHERE run_id = '...' FACET stage

Each event also carries ``started_at_ms`` (epoch ms), so stages can be laid out
on a timeline, and (inside a transaction) ``trace.id`` for joining to APM.
A launch session adopts its own id as the run id (``adopt_run``) so one
session = one run across stages, retries and workers.

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
_run_id_var, _job_id_var = run_id, job_id  # for functions whose params shadow them


def new_run(jobdiva_id: Any) -> str:
    """Start a pipeline run for this task context and return its id."""
    rid = f"{jobdiva_id}:{uuid.uuid4().hex[:8]}"
    run_id.set(rid)
    job_id.set(str(jobdiva_id) if jobdiva_id is not None else None)
    return rid


def adopt_run(run_id: Any, job_id: Any = None) -> Optional[str]:
    """Use a caller-supplied run id (e.g. a launch session id) for this task
    context instead of minting one, so every stage of that unit of work — even
    when resumed by another worker — reports under the same run_id."""
    value = str(run_id) if run_id is not None and str(run_id) != "" else None
    _run_id_var.set(value)
    _job_id_var.set(str(job_id) if job_id is not None else None)
    return value


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


def _emit(stage: str, ms: int, ok: bool, attrs: dict, started_at_ms: Optional[int] = None) -> None:
    payload = {
        "run_id": run_id.get(), "job_id": job_id.get(), "stage": stage, "ms": ms, "ok": ok,
        **_safe_attrs(attrs),
    }
    if started_at_ms is not None:
        payload["started_at_ms"] = started_at_ms
    try:
        logger.info("pipeline_stage stage=%s ms=%d ok=%s run_id=%s", stage, ms, ok, payload["run_id"], extra={"pipeline": payload})
        record_custom_event("PipelineStage", payload)
    except Exception:  # tracing must never break the pipeline
        pass


@asynccontextmanager
async def span(stage: str, **attrs: Any):
    started_at_ms = int(time.time() * 1000)
    t = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        _emit(stage, int((time.perf_counter() - t) * 1000), ok, attrs, started_at_ms)


@contextmanager
def span_sync(stage: str, **attrs: Any):
    started_at_ms = int(time.time() * 1000)
    t = time.perf_counter()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        _emit(stage, int((time.perf_counter() - t) * 1000), ok, attrs, started_at_ms)
