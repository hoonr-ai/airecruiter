"""Sentry initialization for the FastAPI backend.

Captures unhandled exceptions, ERROR-level log records as events, and
WARNING-level records as breadcrumbs across every router, service, and
background scheduler job (FastAPI, Starlette, asyncio, httpx, sqlalchemy,
logging are all auto-instrumented).

Wire-up: call ``init_sentry()`` once at process start, before ``FastAPI``
is constructed. Safe to call multiple times — re-init is a no-op.

Configuration (read from environment):
  * ``SENTRY_DSN``         — DSN; required to enable Sentry. Unset/empty
                             disables the SDK (no-op).
  * ``SENTRY_ENVIRONMENT`` — environment tag (e.g. ``qa``, ``production``).
  * ``SENTRY_ORG``         — org slug; attached as a tag for filtering.

Production helpers exported from this module:
  * ``capture_exception``         — manually report an exception with tags.
  * ``silent_capture``            — context manager: swallow + report.
  * ``install_asyncio_handler``   — capture orphaned asyncio task failures.
  * ``install_scheduler_listener``— capture APScheduler job failures.
  * ``install_fastapi_handlers``  — capture unhandled 5xx with request body.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_initialized = False


def is_enabled() -> bool:
    return _initialized


def init_sentry() -> bool:
    """Initialize the Sentry SDK. Returns True if Sentry is active."""
    global _initialized
    if _initialized:
        return True

    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.info("sentry_disabled (SENTRY_DSN not set)")
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except Exception as e:  # noqa: BLE001
        logger.warning("sentry_sdk_import_failed: %s", e)
        return False

    integrations = [
        LoggingIntegration(
            level=logging.INFO,        # breadcrumbs from INFO+
            event_level=logging.ERROR, # send ERROR+ as events
        ),
    ]

    # Optional integrations — load if available, ignore if the underlying
    # package isn't installed in this deployment.
    def _try_add(import_path: str, attr: str) -> None:
        try:
            mod = __import__(import_path, fromlist=[attr])
            integrations.append(getattr(mod, attr)())
        except Exception:
            return

    _try_add("sentry_sdk.integrations.fastapi", "FastApiIntegration")
    _try_add("sentry_sdk.integrations.starlette", "StarletteIntegration")
    _try_add("sentry_sdk.integrations.asyncio", "AsyncioIntegration")
    _try_add("sentry_sdk.integrations.httpx", "HttpxIntegration")
    _try_add("sentry_sdk.integrations.sqlalchemy", "SqlalchemyIntegration")

    environment = os.getenv("SENTRY_ENVIRONMENT")
    org = os.getenv("SENTRY_ORG")

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            integrations=integrations,
            traces_sample_rate=0.1,
            send_default_pii=False,
            attach_stacktrace=True,
            max_breadcrumbs=100,
        )
        if org:
            sentry_sdk.set_tag("sentry.org", org)
    except Exception as e:  # noqa: BLE001
        logger.error("sentry_init_failed: %s", e, exc_info=True)
        return False

    _initialized = True
    logger.info(
        "sentry_initialized",
        extra={"environment": environment, "org": org or "unset"},
    )
    return True


def set_request_context(request_id: str | None, **tags: str) -> None:
    """Attach the current request_id and arbitrary tags to the active scope."""
    if not _initialized:
        return
    try:
        import sentry_sdk

        scope = sentry_sdk.get_current_scope()
        if request_id:
            scope.set_tag("request_id", request_id)
        for k, v in tags.items():
            if v is not None:
                scope.set_tag(k, str(v))
    except Exception:
        return


def capture_exception(exc: BaseException | None = None, **tags: Any) -> None:
    """Manually report an exception with optional tags. No-op if Sentry off."""
    if not _initialized:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for k, v in tags.items():
                if v is not None:
                    scope.set_tag(k, str(v))
            sentry_sdk.capture_exception(exc)
    except Exception:
        return


def capture_message(message: str, level: str = "error", **tags: Any) -> None:
    """Send a synthetic event without an exception. Use sparingly."""
    if not _initialized:
        return
    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            for k, v in tags.items():
                if v is not None:
                    scope.set_tag(k, str(v))
            sentry_sdk.capture_message(message, level=level)
    except Exception:
        return


@contextlib.contextmanager
def silent_capture(operation: str, reraise: bool = False, **tags: Any) -> Iterator[None]:
    """Context manager: swallow exceptions but report them to Sentry.

    Drop-in replacement for ``try: ... except Exception: pass`` blocks where
    the failure is intentionally non-fatal but should still be visible in
    Sentry. Tag the operation so the issue is filterable::

        with silent_capture("amplitude_track", event=event_type):
            _send_event(...)

    When ``reraise=True`` it captures and re-raises (useful inside finally
    cleanup paths where you want the report but also want the exception
    to propagate).
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — that's the whole point
        capture_exception(exc, operation=operation, **tags)
        if reraise:
            raise


def install_asyncio_handler() -> None:
    """Capture exceptions from orphaned ``asyncio.create_task(...)`` results.

    The codebase fires many fire-and-forget background tasks (provisioning,
    emails, sync). When such a task raises and nothing awaits it, asyncio
    only logs to stderr and the exception is invisible in production. This
    handler routes those failures through Sentry while preserving the
    default logging behavior.
    """
    if not _initialized:
        return
    try:
        import asyncio

        loop = asyncio.get_event_loop()
    except Exception:
        return

    prior = loop.get_exception_handler()

    def _handler(loop_, context):  # noqa: ANN001
        exc = context.get("exception")
        try:
            if exc is not None:
                capture_exception(
                    exc,
                    source="asyncio_unhandled",
                    task=str(context.get("task") or context.get("future") or ""),
                )
            else:
                capture_message(
                    f"asyncio_unhandled: {context.get('message', 'unknown')}",
                    level="error",
                    source="asyncio_unhandled",
                )
        except Exception:
            pass
        if prior is not None:
            prior(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def install_scheduler_listener(scheduler: Any) -> None:
    """Capture APScheduler job failures with job-id tagging.

    APScheduler swallows exceptions inside scheduled callables and only
    surfaces them via ``EVENT_JOB_ERROR``. The existing logger.error inside
    each job already triggers Sentry via LoggingIntegration, but several
    jobs use ``logger.warning`` or ``logger.info`` for skip cases — the
    listener provides a backstop so any uncaught job exception lands in
    Sentry with the job_id tag, regardless of how the job logs.
    """
    if not _initialized or scheduler is None:
        return
    try:
        from apscheduler.events import EVENT_JOB_ERROR

        def _on_error(event):  # noqa: ANN001
            try:
                capture_exception(
                    event.exception,
                    source="apscheduler",
                    job_id=getattr(event, "job_id", "unknown"),
                )
            except Exception:
                return

        scheduler.add_listener(_on_error, EVENT_JOB_ERROR)
    except Exception as e:  # noqa: BLE001
        logger.warning("sentry_scheduler_listener_failed: %s", e)


def install_fastapi_handlers(app: Any) -> None:
    """Attach a global exception handler that captures 5xx with context.

    FastAPI's default behavior is to log + return 500. The handler wraps
    that so each unhandled exception gets the request method/path/query
    attached as Sentry tags before being re-reported. ``HTTPException`` is
    intentionally excluded — those are deliberate 4xx/5xx responses, not
    bugs.
    """
    if not _initialized or app is None:
        return
    try:
        from fastapi import HTTPException
        from fastapi.responses import JSONResponse
        from starlette.exceptions import HTTPException as StarletteHTTPException

        @app.exception_handler(Exception)
        async def _unhandled(request, exc):  # noqa: ANN001
            if isinstance(exc, (HTTPException, StarletteHTTPException)):
                # Let FastAPI/Starlette return their normal response.
                raise exc
            capture_exception(
                exc,
                source="fastapi_unhandled",
                method=request.method,
                path=request.url.path,
            )
            logger.error(
                "fastapi_unhandled_exception",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("sentry_fastapi_handler_failed: %s", e)
