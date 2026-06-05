"""
Sentry SDK initialisation for the AI Recruiter API.

Call `init()` once at application startup (main.py). Every other module that
wants to guard on whether Sentry is active can call `is_enabled()`.
"""
import logging
import os

logger = logging.getLogger(__name__)

_initialized = False


def is_enabled() -> bool:
    """Return True if sentry_sdk has been successfully initialised with a DSN."""
    try:
        import sentry_sdk
        client = sentry_sdk.get_client()
        return client is not None and client.dsn is not None
    except Exception:
        return False


def init() -> None:
    """Initialise Sentry from SENTRY_DSN / SENTRY_ENVIRONMENT env vars.

    Safe to call multiple times — subsequent calls are no-ops.
    Does nothing and logs a warning when SENTRY_DSN is not set.
    """
    global _initialized
    if _initialized:
        return

    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        logger.warning("SENTRY_DSN not set — Sentry error monitoring is disabled")
        _initialized = True  # mark so we don't warn on every request
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.asyncio import AsyncioIntegration

        sentry_logging = LoggingIntegration(
            level=logging.INFO,       # breadcrumb level
            event_level=logging.ERROR,  # capture as Sentry event
        )

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=0.0,   # disable performance tracing (errors only)
            send_default_pii=False,
            integrations=[
                sentry_logging,
                AsyncioIntegration(),
            ],
        )

        _initialized = True
        logger.info(
            "Sentry initialised (env=%s)", os.getenv("SENTRY_ENVIRONMENT", "production")
        )
    except Exception as exc:
        logger.error("Failed to initialise Sentry: %s", exc)
        _initialized = True  # don't retry on every startup step
