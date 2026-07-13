"""
New Relic SDK initialisation and logging helper for the AI Recruiter API.

How data reaches New Relic:
  - Python `logging` calls (INFO+) -> NR Logs (auto-forwarded via newrelic.ini)
  - log_step(...)              -> NR Custom Events (FROM BackendStep SELECT * in NRQL)
  - capture_exception(...)     -> NR APM Errors + NR Custom Events (FROM BackendError)
  - record_custom_event(...)   -> NR Custom Events (FROM <EventType> SELECT * in NRQL)
  - HTTP requests              -> NR APM Transactions (via ASGIApplicationWrapper in main.py)
"""

import os
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_initialized = False
_newrelic_app = None  # NR Application object needed for out-of-transaction reporting


def is_enabled() -> bool:
    """Return True if New Relic APM agent has been successfully initialized."""
    return _initialized and _newrelic_app is not None


def init() -> None:
    """
    Initialize the New Relic Python APM agent.

    Safe to call multiple times — subsequent calls are no-ops. Reads config from
    newrelic.ini (in apps/api directory) and NEW_RELIC_LICENSE_KEY / NEW_RELIC_APP_NAME.
    """
    global _initialized, _newrelic_app

    if _initialized:
        return

    license_key = os.getenv("NEW_RELIC_LICENSE_KEY", "").strip()
    app_name = os.getenv("NEW_RELIC_APP_NAME", "hoonr-api")

    if not license_key:
        logger.warning("[newrelic] NEW_RELIC_LICENSE_KEY not set — New Relic APM monitoring is disabled")
        _initialized = True
        return

    try:
        import newrelic.agent

        config_file = os.getenv(
            "NEW_RELIC_CONFIG_FILE",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newrelic.ini"),
        )

        if os.path.exists(config_file):
            newrelic.agent.initialize(config_file)
            logger.info(f"[newrelic] Agent initialized from: {config_file}")
        else:
            newrelic.agent.initialize()
            logger.info("[newrelic] Agent initialized via environment variables (no newrelic.ini found)")

        _newrelic_app = newrelic.agent.register_application(timeout=10.0)
        _initialized = True
        logger.info(f"[newrelic] APM agent connected — app: {app_name}")
    except ImportError:
        logger.warning("[newrelic] 'newrelic' package not installed")
        _initialized = True
    except Exception as exc:
        logger.error(f"[newrelic] Failed to initialize agent: {exc}", exc_info=True)
        _initialized = True


def _clean_attributes(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure attributes are primitive values compatible with New Relic custom events."""
    if not data:
        return {}
    cleaned = {}
    for k, v in data.items():
        if isinstance(v, (str, int, float, bool)):
            cleaned[k] = v
        elif v is not None:
            cleaned[k] = str(v)
    return cleaned


def record_custom_event(event_type: str, params: Optional[Dict[str, Any]] = None) -> None:
    """Record a custom NRQL event (e.g. JobDivaAPI, EngageGeneratePayload)."""
    if not is_enabled():
        return
    try:
        import newrelic.agent
        cleaned = _clean_attributes(params)
        newrelic.agent.record_custom_event(event_type, cleaned, application=_newrelic_app)
    except Exception as e:
        logger.debug(f"[newrelic] Failed to record custom event '{event_type}': {e}")


def log_step(step_name: str, status: str, details: Optional[Dict[str, Any]] = None, category: str = "backend_step") -> None:
    """
    Emit a structured log line AND record a BackendStep custom event in New Relic.
    Queryable via NRQL: FROM BackendStep SELECT * WHERE step_name = '...'
    """
    msg = f"Step '{step_name}': {status}"
    if details:
        msg += f" — Metadata: {details}"

    logger.info(f"[{category}] {msg}")

    if not is_enabled():
        return

    try:
        import newrelic.agent

        event_params: Dict[str, Any] = {
            "step_name": step_name,
            "status": status,
            "category": category,
        }
        event_params.update(_clean_attributes(details))

        newrelic.agent.record_custom_event("BackendStep", event_params, application=_newrelic_app)

        current_txn = newrelic.agent.current_transaction()
        if current_txn and status.lower() == "failed":
            newrelic.agent.add_custom_attribute("failed_step", step_name)

    except Exception as e:
        logger.debug(f"[newrelic] Failed to record BackendStep event: {e}")


def capture_exception(error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
    """
    Log an exception and report it to New Relic APM error trace + BackendError custom event.
    """
    logger.error(f"[newrelic] Exception captured: {error}", exc_info=True)

    if not is_enabled():
        return

    try:
        import newrelic.agent

        attributes: Dict[str, Any] = {
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        attributes.update(_clean_attributes(context))

        newrelic.agent.notice_error(attributes=attributes)
        newrelic.agent.record_custom_event("BackendError", attributes, application=_newrelic_app)

    except Exception as e:
        logger.debug(f"[newrelic] Failed to report exception to New Relic: {e}")


def record_message(message: str, attributes: Optional[Dict[str, Any]] = None, level: str = "info") -> None:
    """
    Record a message and custom event (drop-in helper replacing capture_message).
    """
    log_fn = logger.warning if level == "warning" else (logger.error if level == "error" else logger.info)
    log_fn(f"[newrelic] {message}")

    if not is_enabled():
        return

    try:
        import newrelic.agent
        params: Dict[str, Any] = {"message": message, "level": level}
        params.update(_clean_attributes(attributes))
        newrelic.agent.record_custom_event("BackendMessage", params, application=_newrelic_app)
    except Exception as e:
        logger.debug(f"[newrelic] Failed to record message: {e}")
