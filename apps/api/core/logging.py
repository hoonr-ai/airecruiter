"""Structured logging + request correlation for the API.

Goals:
  * One place to configure the stdlib logger so New Relic / Datadog /
    OpenTelemetry can auto-instrument with zero code change later
    (e.g. ``newrelic-admin run-program uvicorn main:app``).
  * JSON lines by default so log pipelines don't have to parse mixed
    text and so we can add fields (user_id, trace_id) without breaking
    downstream consumers.
  * A request ID that follows the handler through every log line via
    ``contextvars``, and that we echo back as ``X-Request-ID`` so
    clients can correlate too.

Wire-up happens in ``apps/api/main.py``:

    from core.logging import configure_logging, RequestIDMiddleware
    configure_logging()
    app.add_middleware(RequestIDMiddleware)

Nothing else in the codebase needs to change — every
``logging.getLogger(__name__)`` caller inherits the new handlers.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# ContextVar so async tasks each see their own request id. Middleware sets
# it on enter and resets on exit. Loggers read it via the filter below.
request_id_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None
)


class _RequestIDFilter(logging.Filter):
    """Injects the current request_id (if any) into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per line.

    Keeps the field set small and stable so observability backends can
    index reliably. Extra fields can be passed via ``logger.info(msg, extra={...})``
    and will be merged into the output.
    """

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "request_id",
    }

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        # Surface structured "extra" kwargs without clobbering the standard
        # record attributes.
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            # json.dumps falls through to str() for unknown types via default.
            out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class AmplitudeLogHandler(logging.Handler):
    """Best-effort log forwarding for warning/error records to Amplitude."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            from core.amplitude import track_event_async

            event_type = "backend_error" if record.levelno >= logging.ERROR else "backend_log"
            track_event_async(
                event_type,
                {
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "request_id": getattr(record, "request_id", None),
                },
                device_id="airecruiter-api-logs",
            )
        except Exception:
            return


def configure_logging(
    level: str | None = None,
    fmt: str | None = None,
) -> None:
    """Replace root handlers with a single stdout handler using JSON or text.

    Safe to call multiple times — existing handlers are removed first so
    repeated calls (e.g. from uvicorn reload) don't double up output.
    """
    level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.getenv("LOG_FORMAT") or "json").lower()

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIDFilter())
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"
            )
        )
    root.addHandler(handler)

    if os.getenv("AMPLITUDE_API_KEY") and os.getenv("AMPLITUDE_TRACK_LOGS", "true").lower() in {"1", "true", "yes", "on"}:
        root.addHandler(AmplitudeLogHandler())

    root.setLevel(level)

    # Quiet a couple of noisy third-party loggers at INFO — they stay
    # available at DEBUG when needed.
    for noisy in ("uvicorn.access", "httpx", "httpcore", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Generate or honor an ``X-Request-ID`` per request and propagate it.

    Clients that already have their own correlation ID (upstream gateway,
    tracing SDK, etc.) can send ``X-Request-ID: <value>``; we echo it
    back. Otherwise we mint a UUID4 hex so every request is traceable.
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_ctx.reset(token)
