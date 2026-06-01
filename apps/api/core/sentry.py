"""Sentry initialization for the FastAPI backend.

Captures unhandled exceptions, ERROR-level log records as events, and
WARNING-level records as breadcrumbs across every router, service, and
background scheduler job (FastAPI, Starlette, asyncio, httpx, sqlalchemy,
logging are all auto-instrumented).

Wire-up: call ``init_sentry()`` once at process start, before ``FastAPI``
is constructed. Safe to call multiple times — re-init is a no-op.

Configuration (all optional, sensible defaults shipped):
  * ``SENTRY_DSN``           — DSN; if unset, the project default is used.
                               Set to empty string to disable Sentry.
  * ``SENTRY_ENVIRONMENT``   — defaults to ``ENVIRONMENT`` or ``production``.
  * ``SENTRY_RELEASE``       — git SHA / build tag, optional.
  * ``SENTRY_TRACES_SAMPLE_RATE``   — float 0..1, default 0.1.
  * ``SENTRY_PROFILES_SAMPLE_RATE`` — float 0..1, default 0.0.
  * ``SENTRY_SEND_PII``      — "true" to forward request bodies/users.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_DSN = (
    "https://fb60d6234d86e140d169ea64a114faba@"
    "o4511422243143680.ingest.us.sentry.io/4511492704632832"
)

_initialized = False


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def init_sentry() -> bool:
    """Initialize the Sentry SDK. Returns True if Sentry is active."""
    global _initialized
    if _initialized:
        return True

    dsn = os.getenv("SENTRY_DSN", _DEFAULT_DSN)
    if not dsn:
        logger.info("sentry_disabled (empty SENTRY_DSN)")
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

    environment = (
        os.getenv("SENTRY_ENVIRONMENT")
        or os.getenv("ENVIRONMENT")
        or os.getenv("ENV")
        or "production"
    )
    release = os.getenv("SENTRY_RELEASE") or os.getenv("GIT_SHA")
    send_pii = os.getenv("SENTRY_SEND_PII", "false").lower() in {"1", "true", "yes", "on"}

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release,
            integrations=integrations,
            traces_sample_rate=_float_env("SENTRY_TRACES_SAMPLE_RATE", 0.1),
            profiles_sample_rate=_float_env("SENTRY_PROFILES_SAMPLE_RATE", 0.0),
            send_default_pii=send_pii,
            attach_stacktrace=True,
            max_breadcrumbs=100,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("sentry_init_failed: %s", e, exc_info=True)
        return False

    _initialized = True
    logger.info(
        "sentry_initialized",
        extra={"environment": environment, "release": release or "unset"},
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
