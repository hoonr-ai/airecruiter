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
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


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


def capture_exception(exc: BaseException | None = None, **tags: str) -> None:
    """Manually report an exception with optional tags."""
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
