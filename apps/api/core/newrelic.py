"""
New Relic APM agent bootstrap and fail-open helpers for the PAIR API.

How data reaches New Relic (runbook: docs/observability/new-relic.md):
  - HTTP requests              -> APM web transactions (the agent's own uvicorn/
                                  Starlette/FastAPI hooks; there is NO manual
                                  ASGI wrapper - that was the #388 outage)
  - background_transaction()   -> APM background transactions (scheduler jobs,
                                  core.tasks.spawn work)
  - asyncio.to_thread(...)     -> runs under the caller's trace, so psycopg2
                                  calls show up as Postgres spans
  - Python logging             -> NR Logs through ONE forwarding handler with a
                                  level (PROD WARNING, else INFO), the request_id
                                  filter, the shared warning sampler and a
                                  PII-scrubbing formatter. The agent's automatic
                                  logging hook is off (newrelic.ini).
  - record_custom_event(...)   -> NR custom events, linked to the current trace
  - record_custom_metric(...)  -> NR metrics (Custom/...)
  - capture_exception(...)     -> APM errors + BackendError events

Everything here is a silent no-op unless init() found a licence key and the
agent package. Telemetry must never break a request.

Environment (all optional; see apps/api/.env.example):
  NEW_RELIC_LICENSE_KEY         no key -> agent off
  NEW_RELIC_ENABLED=false       kill switch (also NEW_RELIC_MONITOR_MODE=false)
  NEW_RELIC_ENVIRONMENT         local|qa|prod; else APP_ENV/ENVIRONMENT/ENV;
                                else the APP_BASE_URL host; else local
  NEW_RELIC_APP_NAME            default "PAIR API (QA)" / "PAIR API (PROD)" /
                                "PAIR API (local)"
  NEW_RELIC_LOG_FORWARD_LEVEL   WARNING/INFO/... or OFF
  NEW_RELIC_STARTUP_TIMEOUT     seconds to wait for the collector at boot (0)
  NEW_RELIC_PROPAGATE_TO_THREAD default on
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
import re
import sys
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_initialized = False
_newrelic_app = None  # NR Application object needed for out-of-transaction reporting
_env: Optional[str] = None
_original_to_thread: Optional[Callable[..., Any]] = None

# Oldest agent this module is written against (record_custom_event linking,
# context_wrapper, expected_status_codes). deploy-azure.sh's pip fallback can
# leave an older agent behind; it is switched off rather than half-working.
MIN_AGENT_VERSION = (12,)

_OFF_VALUES = {"0", "false", "off", "no", "disabled", "none"}

_ENV_ALIASES = {
    "prod": "prod", "production": "prod", "prd": "prod", "live": "prod",
    "qa": "qa", "staging": "qa", "stage": "qa", "uat": "qa",
    "local": "local", "dev": "local", "development": "local", "test": "local", "testing": "local",
}
_HOST_ENVS = {"pair.pyramidci.com": "prod", "pairqa.pyramidci.com": "qa"}
_APP_NAME_ENV = {"prod": "PROD", "qa": "QA", "local": "local"}


# ---------------------------------------------------------------------------
# Environment / naming
# ---------------------------------------------------------------------------

def _normalise_env(value: Optional[str]) -> Optional[str]:
    return _ENV_ALIASES.get((value or "").strip().lower())


def resolve_environment() -> str:
    """local | qa | prod.

    NEW_RELIC_ENVIRONMENT, then APP_ENV / ENVIRONMENT / ENV (unknown values are
    skipped), then the APP_BASE_URL host (deploy-azure.sh always reconciles it,
    so a manual `./manage.sh deploy` still reports under the right name),
    else local.
    """
    for var in ("NEW_RELIC_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV"):
        env = _normalise_env(os.getenv(var))
        if env:
            return env
    host = ""
    try:
        host = (urlparse(os.getenv("APP_BASE_URL", "").strip()).hostname or "").lower()
    except Exception:
        host = ""
    return _HOST_ENVS.get(host, "local")


def default_app_name(env: str) -> str:
    return f"PAIR API ({_APP_NAME_ENV.get(env, env)})"


def environment() -> str:
    """The environment init() resolved (resolved on demand before init)."""
    return _env or resolve_environment()


def _apply_env_defaults(env: str) -> None:
    """Defaults the agent reads from the environment when newrelic.core.config is
    first imported, so this must run before any `import newrelic.*` below.
    newrelic.ini must not set these keys: the ini beats the environment."""
    os.environ.setdefault("NEW_RELIC_APP_NAME", default_app_name(env))
    os.environ.setdefault("NEW_RELIC_LABELS", f"env:{env};service:pair-api")
    os.environ.setdefault("NEW_RELIC_LOG", "stderr")  # agent diagnostics -> journald
    os.environ.setdefault("NEW_RELIC_LOG_LEVEL", "warning")


def _kill_switch_on() -> bool:
    """NEW_RELIC_ENABLED=false (or NEW_RELIC_MONITOR_MODE=false) turns the agent
    off before it is imported. deploy-azure.sh never writes NEW_RELIC_ENABLED."""
    off = {"0", "false", "off", "no", "disabled"}
    return (
        (os.getenv("NEW_RELIC_ENABLED") or "").strip().lower() in off
        or (os.getenv("NEW_RELIC_MONITOR_MODE") or "").strip().lower() in off
    )


def _agent_version() -> tuple:
    import newrelic

    raw = getattr(newrelic, "version_info", None)
    if raw is None:
        raw = str(getattr(newrelic, "version", "0")).split(".")
    parts = []
    for p in raw:  # leading numeric parts only: (14, 0, 1, "dev3") -> (14, 0, 1)
        if isinstance(p, int) or (isinstance(p, str) and p.isdigit()):
            parts.append(int(p))
        else:
            break
    return tuple(parts) or (0,)


def _startup_timeout() -> float:
    try:
        return max(0.0, float(os.getenv("NEW_RELIC_STARTUP_TIMEOUT", "0") or 0))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """True once init() initialised the agent with a key (the collector
    connection may still be pending; helpers stay no-ops until it connects)."""
    return _initialized and _newrelic_app is not None


def init() -> None:
    """Initialise the agent once per process. Safe to call repeatedly.

    Runs in every uvicorn worker (spawned processes), after core.config has
    loaded apps/api/.env and before services/routers are imported.
    """
    global _initialized, _newrelic_app, _env

    if _initialized:
        return
    _initialized = True
    _env = resolve_environment()

    license_key = os.getenv("NEW_RELIC_LICENSE_KEY", "").strip()
    if not license_key:
        logger.warning("[newrelic] NEW_RELIC_LICENSE_KEY not set — New Relic APM monitoring is disabled")
        return
    if _kill_switch_on():
        logger.warning("[newrelic] disabled by NEW_RELIC_ENABLED/NEW_RELIC_MONITOR_MODE")
        return

    _apply_env_defaults(_env)

    if "pytest" in sys.modules and os.getenv("NEW_RELIC_ALLOW_UNDER_PYTEST") != "1":
        # The agent instruments the whole process and cannot be undone: a test
        # importing main with a key in apps/api/.env would report test traffic
        # and change every later test. Agent tests run in subprocesses.
        logger.info("[newrelic] not started under pytest")
        return

    try:
        version = _agent_version()
        if version < MIN_AGENT_VERSION:
            logger.error(
                "[newrelic] agent %s is older than %s; APM disabled (pip install 'newrelic>=12,<15')",
                ".".join(map(str, version)), ".".join(map(str, MIN_AGENT_VERSION)),
            )
            return

        import newrelic.agent

        config_file = os.getenv(
            "NEW_RELIC_CONFIG_FILE",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newrelic.ini"),
        )
        if os.path.exists(config_file):
            newrelic.agent.initialize(config_file)
        else:
            newrelic.agent.initialize()

        # Non-blocking by default: an unreachable collector must not stall every
        # worker at import. Transactions started before the connection lands are
        # not recorded, which only affects a worker's first seconds.
        _newrelic_app = newrelic.agent.register_application(timeout=_startup_timeout())
        logger.info(
            "[newrelic] agent initialized app=%s env=%s version=%s (collector connection pending)",
            os.getenv("NEW_RELIC_APP_NAME"), _env, ".".join(map(str, version)),
        )
    except ImportError:
        logger.warning("[newrelic] 'newrelic' package not installed")
        return
    except Exception as exc:
        logger.error(f"[newrelic] Failed to initialize agent: {exc}", exc_info=True)
        _newrelic_app = None
        return

    try:
        attach_log_forwarding()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[newrelic] log forwarding not attached: {exc}")
    try:
        _install_to_thread_propagation()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[newrelic] to_thread propagation not installed: {exc}")
    try:
        _instrument_running_loop()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[newrelic] running event loop not instrumented: {exc}")


def _instrument_running_loop() -> None:
    """Instrument the event loop that is already running when main is imported.

    The agent carries a transaction into child tasks by wrapping
    ``create_task``. For the stock asyncio loop that is a class-level patch,
    so it covers loops created before ``initialize()``. uvloop's Loop is a C
    class: the agent can only wrap each instance as it is created (via
    ``asyncio.Runner._lazy_init`` / ``set_event_loop``), and uvicorn creates
    its uvloop loop BEFORE importing ``main``, where this ``init()`` runs. So
    under ``uvicorn main:app`` (loop "auto" picks uvloop when it is installed)
    every task the request spawns, including the one BaseHTTPMiddleware
    runs the route in, lost the transaction: all web transactions were
    named ``starlette.middleware.base:BaseHTTPMiddleware`` and their DB and
    HTTP spans went missing. Wrapping the running loop here, with the agent's
    own helper, closes that gap. A no-op when no loop is running (tests,
    scripts) or the loop is already wrapped.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    from newrelic.hooks.coroutines_asyncio import _instrument_event_loop

    _instrument_event_loop(loop)


# ---------------------------------------------------------------------------
# Log forwarding
# ---------------------------------------------------------------------------

# Bounded quantifiers (RFC limits: 64-char local part, 63-char labels): an
# unbounded local part made the search quadratic on long tokens (50k letters:
# 4 s on the event-loop thread). A longer local part is masked from its last
# 64 characters.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63})+")
_PHONE_RE = re.compile(r"(?<![\w.])(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]\d{4}(?!\d)")
_LONG_DIGITS_RE = re.compile(r"\d{7,}")
_CUT_NUMBER_RE = re.compile(r"[\d(+][\d\s().\-]{4,}$")
_MAX_FORWARDED_EXC = 4000
# The agent cuts forwarded messages at 32768 characters; cutting first keeps
# the scrub cost bounded.
_MAX_FORWARDED_CHARS = 32768
_CUT_TAIL = 320  # 64-char local part + '@' + a 253-char domain, rounded up


def scrub_pii(text: str) -> str:
    """Emails -> <email>; phone-shaped numbers and digit runs >= 7 -> <num>."""
    if not text:
        return text
    text = _EMAIL_RE.sub("<email>", text)
    text = _PHONE_RE.sub("<num>", text)
    return _LONG_DIGITS_RE.sub("<num>", text)


def _mask_cut_tail(text: str) -> str:
    """After a cut: the last address or number may be split so the patterns
    above no longer match it. Scrubbed addresses carry no '@', so any '@' left
    near the end is masked, else a trailing run of digits."""
    at = text.rfind("@", max(0, len(text) - _CUT_TAIL))
    if at != -1:
        return text[:max(0, at - 64)] + "<email>"
    head, tail = text[:-_CUT_TAIL], text[-_CUT_TAIL:]
    return head + _CUT_NUMBER_RE.sub("<num>", tail)


class PiiScrubbingFormatter(logging.Formatter):
    """Message (plus a capped traceback) with PII patterns masked.

    A formatter, not a filter: filters mutate the LogRecord every handler
    shares, which would change the journald copy too.
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        if record.exc_info:
            try:
                msg = f"{msg}\n{self.formatException(record.exc_info)[-_MAX_FORWARDED_EXC:]}"
            except Exception:
                pass
        if len(msg) <= _MAX_FORWARDED_CHARS:
            return scrub_pii(msg)
        return _mask_cut_tail(scrub_pii(msg[:_MAX_FORWARDED_CHARS]))


class _NrLogNoiseFilter(logging.Filter):
    """NR-only: drop INFO lines that already reach NR as custom events."""

    _INFO_DUPLICATES = ("core.pipeline_trace",)

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.levelno < logging.WARNING and record.name in self._INFO_DUPLICATES)


def log_forward_level() -> Optional[int]:
    """None = forwarding off. Default PROD WARNING, QA/local INFO."""
    raw = (os.getenv("NEW_RELIC_LOG_FORWARD_LEVEL") or "").strip()
    if raw.lower() in _OFF_VALUES:
        return None
    if raw:
        level = logging.getLevelName(raw.upper())
        if isinstance(level, int):
            return level
        if raw.isdigit():
            return int(raw)
        logger.warning(f"[newrelic] NEW_RELIC_LOG_FORWARD_LEVEL={raw!r} not understood; using the default")
    return logging.WARNING if environment() == "prod" else logging.INFO


def attach_log_forwarding() -> None:
    """(Re)attach the NR forwarding handlers. Idempotent.

    Root logger at log_forward_level(); a WARNING one on "uvicorn" because
    uvicorn's logging config stops "uvicorn.error" at that logger
    (propagate=False), so "Exception in ASGI application" / startup failures
    would otherwise never reach NR. Never on uvicorn.access.
    core.logging.configure_logging() calls this again after it rebuilds the
    root handlers.
    """
    if not is_enabled():
        return
    targets = (logging.getLogger(), logging.getLogger("uvicorn"))
    for lg in targets:
        for h in list(lg.handlers):
            if getattr(h, "_pair_nr_forwarder", False):
                lg.removeHandler(h)
    level = log_forward_level()
    if level is None:
        return

    from newrelic.api.log import NewRelicLogForwardingHandler

    from core.logging import request_id_filter, warning_sampler

    sampler = warning_sampler()

    def _handler(lvl: int) -> logging.Handler:
        h = NewRelicLogForwardingHandler(level=lvl)
        h._pair_nr_forwarder = True  # type: ignore[attr-defined]
        h.addFilter(request_id_filter())
        if sampler is not None:
            # Same instance as the stdout handler: one keep/drop decision per record.
            h.addFilter(sampler)
        h.addFilter(_NrLogNoiseFilter())
        h.setFormatter(PiiScrubbingFormatter())
        return h

    targets[0].addHandler(_handler(level))
    if not targets[1].propagate:
        # Only when uvicorn's logging config cut "uvicorn" off from the root
        # (always under the uvicorn CLI); otherwise the root handler sees it.
        targets[1].addHandler(_handler(max(level, logging.WARNING)))


# ---------------------------------------------------------------------------
# asyncio.to_thread trace propagation
# ---------------------------------------------------------------------------

def _propagate_to_thread() -> bool:
    return (os.getenv("NEW_RELIC_PROPAGATE_TO_THREAD") or "on").strip().lower() not in _OFF_VALUES


def _wrap_in_trace(func: Callable[..., Any]) -> Callable[..., Any]:
    try:
        from newrelic.api.time_trace import current_trace
        from newrelic.core.context import context_wrapper

        trace = current_trace()
        if trace is not None:
            return context_wrapper(func, trace=trace, strict=False)
    except Exception:
        pass
    return func


def wrap_for_thread(func: Callable[..., Any]) -> Callable[..., Any]:
    """``func`` bound to the caller's trace, for ``loop.run_in_executor``
    (which, unlike the patched ``asyncio.to_thread``, carries no trace): the
    blocking call's external/DB segments then land in the caller's
    transaction. ``func`` itself when the agent is off, outside a transaction
    or with NEW_RELIC_PROPAGATE_TO_THREAD off. Call it on the event loop
    thread, at submit time."""
    if not is_enabled() or not _propagate_to_thread():
        return func
    return _wrap_in_trace(func)


def _install_to_thread_propagation() -> None:
    """Run asyncio.to_thread callables under the caller's trace so blocking
    psycopg2 work shows up in transactions (the agent only propagates
    Starlette's run_in_threadpool). Kill switch NEW_RELIC_PROPAGATE_TO_THREAD."""
    global _original_to_thread
    if not _propagate_to_thread():
        return
    if _original_to_thread is not None:
        return

    original = asyncio.to_thread

    @functools.wraps(original)
    async def to_thread(func, /, *args, **kwargs):
        return await original(_wrap_in_trace(func), *args, **kwargs)

    _original_to_thread = original
    asyncio.to_thread = to_thread


# ---------------------------------------------------------------------------
# Attribute hygiene
# ---------------------------------------------------------------------------

_MAX_STR_LEN = 64
_PII_KEY_PARTS = (
    "name", "email", "mail", "phone", "mobile", "resume", "cv", "address",
    "linkedin", "url", "text", "ssn", "dob", "birth", "contact", "body",
    "content", "message", "token", "password", "secret",
)


def safe_attributes(attrs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Only non-PII scalars: numbers/bools and short strings without '@',
    under keys that don't look like PII. Same policy as pipeline_trace."""
    out: Dict[str, Any] = {}
    for k, v in (attrs or {}).items():
        key = str(k).lower()
        if any(part in key for part in _PII_KEY_PARTS):
            continue
        if isinstance(v, (bool, int, float)):
            out[str(k)] = v
        elif isinstance(v, str) and len(v) <= _MAX_STR_LEN and "@" not in v:
            out[str(k)] = v
    return out


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


def _in_transaction() -> bool:
    import newrelic.agent

    return newrelic.agent.current_transaction() is not None


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def add_transaction_attributes(**attrs: Any) -> None:
    """Custom attributes on the current transaction (PII-filtered)."""
    if not is_enabled():
        return
    try:
        import newrelic.agent

        items = safe_attributes(attrs)
        if items:
            newrelic.agent.add_custom_attributes(list(items.items()))
    except Exception as e:
        logger.debug(f"[newrelic] add_transaction_attributes failed: {e}")


def suppress_apdex() -> None:
    """Call at the start of a long streaming endpoint so minute-long streams
    don't drag Apdex down."""
    if not is_enabled():
        return
    try:
        import newrelic.agent

        newrelic.agent.suppress_apdex_metric(True)
    except Exception as e:
        logger.debug(f"[newrelic] suppress_apdex failed: {e}")


def ignore_transaction() -> None:
    """Drop the current transaction (cheap polls, empty scheduler ticks)."""
    if not is_enabled():
        return
    try:
        import newrelic.agent

        newrelic.agent.ignore_transaction(True)
    except Exception as e:
        logger.debug(f"[newrelic] ignore_transaction failed: {e}")


def detach_inherited_trace() -> None:
    """Call first thing in a fire-and-forget task (core.tasks.spawn does).

    The agent copies the creating task's trace into every new asyncio task, so
    work spawned from a request would otherwise report into that request (and
    be cut off when it ends). Drops only a trace that was entered in another
    task, so calling it anywhere else is harmless.
    """
    if not is_enabled():
        return
    try:
        from newrelic.core.trace_cache import trace_cache

        cache = trace_cache()
        tid = cache.current_thread_id()
        trace = cache.get(tid)
        if trace is not None and getattr(trace, "thread_id", None) != tid:
            cache.pop(tid, None)
    except Exception as e:
        logger.debug(f"[newrelic] detach_inherited_trace failed: {e}")


class background_transaction:
    """A background transaction for scheduler jobs and spawned work.

        async with background_transaction("scheduler/jobdiva_bi_sync"):
            ...
        wrapped = background_transaction("scheduler/x")(coroutine_function)

    - A transaction is live in this task or in the task that created it
      (inline await from a request or job, a StreamingResponse generator,
      Starlette's middleware task group, gather): a FunctionTrace inside it,
      never a new or renamed transaction. The agent's @background_task would
      rename the request instead.
    - No live transaction (scheduler jobs, or core.tasks.spawn, which detaches
      the inherited trace first): a new BackgroundTask. A raw
      asyncio.create_task() from a request stays in the request's transaction,
      so fire-and-forget work goes through spawn().
    - The transaction name is name.split(":", 1)[0]; the suffix (usually an id)
      goes into the `task_key` attribute so names stay low-cardinality. Every
      transaction carries `worker_pid` (8 uvicorn workers share one host).
    - NR off: a passthrough. The decorator always returns an `async def`, so
      APScheduler still sees a coroutine function (otherwise AsyncIOExecutor
      would run it in a thread and the coroutine would never be awaited).
    - Wrap work AFTER initial sleeps: with a non-blocking registration a
      transaction started in a worker's first seconds stays disabled.
    - One `async with` per instance (the decorator makes a fresh one per call).
    """

    def __init__(self, name: str, group: str = "Task", **attrs: Any):
        raw = str(name or "task")
        base, _, key = raw.partition(":")
        self._raw_name = raw
        self.name = base or "task"
        self.task_key = key or None
        self.group = group
        self.attrs = attrs
        self._entered: Any = None

    # -- decorator ----------------------------------------------------------
    def __call__(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(fn):
            return fn  # coroutine functions only; anything else is left alone
        raw, group, attrs = self._raw_name, self.group, self.attrs

        @functools.wraps(fn)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            async with background_transaction(raw, group, **attrs):
                return await fn(*args, **kwargs)

        return _wrapped

    # -- context manager ----------------------------------------------------
    def _enter(self) -> None:
        if not is_enabled():
            return
        try:
            from newrelic.core.trace_cache import trace_cache

            cache = trace_cache()
            trace = cache.current_trace()
            root = getattr(trace, "root", None) if trace is not None else None
            if (
                root is not None
                and getattr(root, "transaction", None) is not None
                and not getattr(root, "exited", False)
            ):
                # Live in this task or the one that created it. If the
                # inherited trace itself has exited, the agent records no
                # segment, but the request stays current (no cut-off).
                from newrelic.api.function_trace import FunctionTrace

                ft = FunctionTrace(self.name)
                ft.__enter__()
                self._entered = ft
                return
            if trace is not None:
                # Left over from a finished transaction: clear it so the new
                # root can be saved (save_trace refuses otherwise).
                cache.pop(cache.current_thread_id(), None)

            from newrelic.api.background_task import BackgroundTask

            bt = BackgroundTask(_newrelic_app, self.name, self.group)
            bt.__enter__()
            self._entered = bt
            if bt.enabled:
                items = dict(safe_attributes(self.attrs))
                if self.task_key is not None and "@" not in self.task_key:
                    items["task_key"] = self.task_key[:_MAX_STR_LEN]
                items["worker_pid"] = os.getpid()
                bt.add_custom_attributes(list(items.items()))
        except Exception as e:
            self._entered = None
            logger.debug(f"[newrelic] background_transaction enter failed: {e}")

    def _exit(self, exc_type, exc, tb) -> None:
        entered, self._entered = self._entered, None
        if entered is None:
            return
        try:
            if exc_type is not None and issubclass(exc_type, (asyncio.CancelledError, GeneratorExit)):
                # Cancellation is control flow (shutdown, lease loss), not an error.
                entered.__exit__(None, None, None)
            else:
                entered.__exit__(exc_type, exc, tb)
        except Exception as e:
            logger.debug(f"[newrelic] background_transaction exit failed: {e}")

    async def __aenter__(self) -> "background_transaction":
        self._enter()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._exit(exc_type, exc, tb)
        return False

    def __enter__(self) -> "background_transaction":
        self._enter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._exit(exc_type, exc, tb)
        return False


# ---------------------------------------------------------------------------
# Events / metrics / errors
# ---------------------------------------------------------------------------

def record_custom_event(event_type: str, params: Optional[Dict[str, Any]] = None) -> None:
    """Record a custom NRQL event (e.g. PipelineStage, PairLaunchSession).

    Recorded against the application so it also works outside transactions;
    inside one it carries trace.id/span.id so NRQL can join it to the trace.
    Callers own the PII policy of `params` (ids/counters/enums only).
    """
    if not is_enabled():
        return
    try:
        import newrelic.agent
        from newrelic.api.time_trace import get_trace_linking_metadata

        cleaned = _clean_attributes(params)
        cleaned.setdefault("env", _env or "local")
        try:
            linking = get_trace_linking_metadata() or {}
            for key in ("trace.id", "span.id"):
                if linking.get(key):
                    cleaned.setdefault(key, linking[key])
        except Exception:
            pass
        newrelic.agent.record_custom_event(event_type, cleaned, application=_newrelic_app)
    except Exception as e:
        logger.debug(f"[newrelic] Failed to record custom event '{event_type}': {e}")


def record_custom_metric(name: str, value: Any) -> None:
    """Record a metric (`Custom/...`). A number is a timing/value sample; a dict
    like {"count": 1} is a count. Uses the application outside transactions,
    where the agent would otherwise drop it."""
    if not is_enabled():
        return
    try:
        import newrelic.agent

        if _in_transaction():
            newrelic.agent.record_custom_metric(name, value)
        else:
            newrelic.agent.record_custom_metric(name, value, application=_newrelic_app)
    except Exception as e:
        logger.debug(f"[newrelic] Failed to record metric '{name}': {e}")


def log_step(step_name: str, status: str, details: Optional[Dict[str, Any]] = None, category: str = "backend_step") -> None:
    """
    Emit a structured log line AND record a PairBackendStep custom event.
    (Was `BackendStep`, which collided with Pairbot's event of the same name in
    the shared account.) Queryable via NRQL: FROM PairBackendStep SELECT * WHERE step_name = '...'
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

        record_custom_event("PairBackendStep", event_params)

        current_txn = newrelic.agent.current_transaction()
        if current_txn and status.lower() == "failed":
            newrelic.agent.add_custom_attribute("failed_step", step_name)

    except Exception as e:
        logger.debug(f"[newrelic] Failed to record PairBackendStep event: {e}")


def capture_exception(error: Exception, context: Optional[Dict[str, Any]] = None) -> None:
    """
    Log an exception and report it to New Relic APM error trace + BackendError custom event.
    """
    logger.error(f"[newrelic] Exception captured: {error}", exc_info=(type(error), error, error.__traceback__))

    if not is_enabled():
        return

    try:
        import newrelic.agent

        attributes: Dict[str, Any] = {
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        attributes.update(_clean_attributes(context))

        exc_info = (type(error), error, error.__traceback__)
        if _in_transaction():
            newrelic.agent.notice_error(error=exc_info, attributes=attributes)
        else:
            # Outside a transaction the agent drops it unless given the app.
            newrelic.agent.notice_error(error=exc_info, attributes=attributes, application=_newrelic_app)
        record_custom_event("BackendError", attributes)

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
        params: Dict[str, Any] = {"message": message, "level": level}
        params.update(_clean_attributes(attributes))
        record_custom_event("BackendMessage", params)
    except Exception as e:
        logger.debug(f"[newrelic] Failed to record message: {e}")
