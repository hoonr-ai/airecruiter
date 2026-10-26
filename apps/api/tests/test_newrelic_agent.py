"""New Relic agent behaviour, each case in its own subprocess.

`newrelic.agent.initialize()` is process-global and cannot be undone: run
in-process it would instrument httpx, asyncio and logging for every later test.
So every case runs `python -c ...` with developer_mode (nothing is sent) and a
dummy licence key, with apps/api/.env ignored. Skipped when the interpreter
running the suite has no `newrelic` package (CI installs it from
requirements.txt; locally use a venv that has it).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("newrelic") is None, reason="newrelic agent not installed"
)

# Ignore apps/api/.env (it may hold a real key) and stub the required config.
_PRELUDE = """
import sys, os, json, asyncio, logging
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
from env_stubs import stub_required_env
stub_required_env()
"""


def _run(body: str, *, nr_on: bool = True, extra_env=None, extra_path=None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEW_RELIC_")}
    for key in ("APP_ENV", "ENVIRONMENT", "ENV", "APP_BASE_URL"):
        env.pop(key, None)
    if nr_on:
        env.update({
            "NEW_RELIC_LICENSE_KEY": "0" * 40,
            "NEW_RELIC_DEVELOPER_MODE": "true",
            "NEW_RELIC_STARTUP_TIMEOUT": "10",
        })
    env.update(extra_env or {})
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join([str(extra_path), env.get("PYTHONPATH", "")])
    code = _PRELUDE + textwrap.dedent(body)
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=API_DIR, env=env, capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, f"stdout:\n{out.stdout}\nstderr:\n{out.stderr}"
    last = [line for line in out.stdout.splitlines() if line.startswith("RESULT ")]
    assert last, f"no RESULT line\nstdout:\n{out.stdout}\nstderr:\n{out.stderr}"
    return json.loads(last[-1][len("RESULT "):])


# ------------------------------------------------------- background_transaction

_BG_SEMANTICS = """
from core import newrelic as nr
nr.init()
import newrelic.agent as agent
from newrelic.api.application import application_instance
from newrelic.api.web_transaction import WebTransaction
from newrelic.api.function_trace import FunctionTrace
from apscheduler.util import iscoroutinefunction_partial
from core.tasks import spawn

seen = {}

def _attrs(t):
    return dict(t._custom_params) if t else {}

async def spawned_body(key):
    t = agent.current_transaction()
    seen[key] = t
    seen[key + "_enabled"] = bool(t and t.enabled)
    seen[key + "_attrs"] = _attrs(t)

async def runner_body():
    # nr_txn=False: no transaction of its own, and detached from the request,
    # so the stages it opens are new transactions.
    seen["runner_start"] = agent.current_transaction()
    async with nr.background_transaction("launch_session/stage:S1"):
        seen["runner_stage"] = agent.current_transaction()
    seen["runner_after"] = agent.current_transaction()

async def raw_task_body():
    async with nr.background_transaction("raw:1"):
        seen["raw_task"] = agent.current_transaction()

async def job_body():
    t = agent.current_transaction()
    seen.setdefault("job", []).append(t)

job = nr.background_transaction("scheduler/job")(job_body)

async def main():
    with WebTransaction(application_instance(), "web", scheme="http", host="h", port=80,
                        request_method="GET", request_path="/x") as web:
        web.set_transaction_name("web-route")
        seen["web"] = web
        with FunctionTrace("endpoint"):
            # (a) a task spawned inside a live request gets its own transaction
            await spawn(spawned_body("spawned"), name="launch:abc123")
            await spawn(spawned_body("spawned_pii"), name="launch:jane@example.com")
            await spawn(runner_body(), name="launch_session_runner:S1", nr_txn=False)
            # a raw create_task inherits the request: a span of it, not a new txn
            await asyncio.create_task(raw_task_body())
            # (b) inline await in the same task: a FunctionTrace, same txn
            await job()
            async with nr.background_transaction("inline:1"):
                seen["inline_cm"] = agent.current_transaction()
            seen["web_after_inline"] = agent.current_transaction()
    # outside any transaction: a new background transaction
    await job()

asyncio.run(main())
web = seen["web"]
print("RESULT " + json.dumps({
    "enabled": nr.is_enabled(),
    "pid": os.getpid(),
    "web_name": web.name,
    "web_background": web.background_task,
    "spawned_is_new": seen["spawned"] is not None and seen["spawned"] is not web,
    "spawned_enabled": seen["spawned_enabled"],
    "spawned_name": seen["spawned"].name,
    "spawned_path": seen["spawned"].path,
    "spawned_task_key": seen["spawned_attrs"].get("task_key"),
    "spawned_worker_pid": seen["spawned_attrs"].get("worker_pid"),
    "spawned_pii_enabled": seen["spawned_pii_enabled"],
    "spawned_pii_attrs": sorted(seen["spawned_pii_attrs"]),
    "runner_start_none": seen["runner_start"] is None,
    "runner_stage_new": seen["runner_stage"] is not None and seen["runner_stage"] is not web,
    "runner_stage_name": seen["runner_stage"].name,
    "runner_stage_key": _attrs(seen["runner_stage"]).get("task_key"),
    "runner_after_none": seen["runner_after"] is None,
    "raw_task_same_txn": seen["raw_task"] is web,
    "inline_job_same_txn": seen["job"][0] is web,
    "inline_cm_same_txn": seen["inline_cm"] is web,
    "web_after_inline": seen["web_after_inline"] is web,
    "outside_job_new_txn": seen["job"][1] is not None and seen["job"][1] is not web,
    "outside_job_name": seen["job"][1].name,
    "outside_job_background": seen["job"][1].background_task,
    "outside_job_worker_pid": _attrs(seen["job"][1]).get("worker_pid"),
    "decorated_is_coroutine_function": iscoroutinefunction_partial(job),
}))
"""


def test_background_transaction_semantics():
    r = _run(_BG_SEMANTICS, extra_env={"NEW_RELIC_ENVIRONMENT": "qa"})
    assert r["enabled"] is True
    # (a) own transaction, request untouched (not renamed, not turned background)
    assert r["spawned_is_new"] and r["spawned_enabled"]
    assert r["spawned_name"] == "launch" and r["spawned_path"] == "OtherTransaction/Task/launch"
    assert r["spawned_task_key"] == "abc123"
    assert r["spawned_worker_pid"] == r["pid"]  # 8 workers share one host
    assert r["web_name"] == "web-route" and r["web_background"] is False
    # a task_key that looks like an address is dropped, the transaction is not
    assert r["spawned_pii_enabled"] and r["spawned_pii_attrs"] == ["worker_pid"]
    # nr_txn=False: detached all the same, so its stages are new transactions
    assert r["runner_start_none"] and r["runner_after_none"]
    assert r["runner_stage_new"] and r["runner_stage_name"] == "launch_session/stage"
    assert r["runner_stage_key"] == "S1"
    # a raw create_task from the request is not detached: a span of the request
    assert r["raw_task_same_txn"]
    # (b) inline: function trace inside the live transaction
    assert r["inline_job_same_txn"] and r["inline_cm_same_txn"] and r["web_after_inline"]
    # outside: a new background transaction under the job name
    assert r["outside_job_new_txn"] and r["outside_job_background"]
    assert r["outside_job_name"] == "scheduler/job"
    assert r["outside_job_worker_pid"] == r["pid"]
    # (c) APScheduler still sees a coroutine function
    assert r["decorated_is_coroutine_function"]


_STREAM_APP = """
# main.py's stack: RequestIDMiddleware plus an @app.middleware("http") (a
# BaseHTTPMiddleware, which runs the endpoint in a child task).
from core.logging import configure_logging, RequestIDMiddleware
configure_logging()
from core import newrelic as nr
nr.init()
import newrelic.agent as agent
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import StreamingResponse
from core.tasks import spawn

seen = {}
app = FastAPI()
app.add_middleware(RequestIDMiddleware)

@app.middleware("http")
async def passthrough(request, call_next):
    return await call_next(request)

@app.get("/stream")
async def stream():
    seen["stream_req"] = agent.current_transaction()
    async def gen():
        seen["gen_before"] = agent.current_transaction()
        async with nr.background_transaction("gen_cm"):
            seen["gen_in"] = agent.current_transaction()
        seen["gen_after"] = agent.current_transaction()
        nr.suppress_apdex()
        yield b"x"
    return StreamingResponse(gen())

async def bg():
    async with nr.background_transaction("bg_cm"):
        seen["bg_in"] = agent.current_transaction()

@app.get("/bgtask")
async def bgtask(background_tasks: BackgroundTasks):
    seen["bgtask_req"] = agent.current_transaction()
    background_tasks.add_task(bg)
    return {}

async def spawned():
    seen["spawned"] = agent.current_transaction()

@app.get("/spawn")
async def spawn_route():
    seen["spawn_req"] = agent.current_transaction()
    await spawn(spawned(), name="launch:1")
    return {}
"""

_CALL_STREAM_APP = """
import uvicorn
import nr_stream_app as m
cfg = uvicorn.Config("nr_stream_app:app", log_level="warning")
cfg.load()

async def call(path):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"", "more_body": False}
    sent = []
    async def send(message):
        sent.append(message)
    await cfg.loaded_app(scope, receive, send)
    return [x.get("status") for x in sent if x["type"] == "http.response.start"]

async def main():
    return [await call(p) for p in ("/stream", "/bgtask", "/spawn")]

statuses = asyncio.run(main())
s = m.seen
def same(a, b):
    return s.get(a) is not None and s.get(a) is s.get(b)
print("RESULT " + json.dumps({
    "statuses": statuses,
    "web": bool(s["stream_req"]) and not s["stream_req"].background_task,
    "gen_before": same("gen_before", "stream_req"),
    "gen_in": same("gen_in", "stream_req"),
    "gen_after": same("gen_after", "stream_req"),
    "apdex_suppressed": bool(s["stream_req"].suppress_apdex),
    "bg_in": same("bg_in", "bgtask_req"),
    "spawned_new": s["spawned"] is not None and s["spawned"] is not s["spawn_req"],
    "spawned_background": bool(s["spawned"] and s["spawned"].background_task),
}))
"""


def test_inline_background_transaction_in_a_stream_stays_in_the_request(tmp_path):
    """A StreamingResponse generator runs in another task than the request; an
    inline background_transaction there must not start a new transaction and
    cut the rest of the stream off from the request."""
    (tmp_path / "nr_stream_app.py").write_text(_STREAM_APP)
    r = _run(_CALL_STREAM_APP, extra_path=tmp_path, extra_env={"NEW_RELIC_ENVIRONMENT": "qa"})
    assert r["statuses"] == [[200], [200], [200]]
    assert r["web"]
    assert r["gen_before"] and r["gen_in"] and r["gen_after"]
    assert r["apdex_suppressed"]
    # Whether Starlette BackgroundTasks stay inside the request transaction is
    # the agent's / Starlette's business and differs across versions (true on
    # starlette 1.0 + Python 3.11, false on CI's 3.12 set). Nothing here relies
    # on it, so it is reported, not asserted.
    print("bg_in", r["bg_in"])
    assert r["spawned_new"] and r["spawned_background"]


def test_background_transaction_disabled_keeps_coroutine_function():
    r = _run("""
        from core import newrelic as nr
        nr.init()
        from apscheduler.util import iscoroutinefunction_partial
        async def body():
            return 41 + 1
        job = nr.background_transaction("scheduler/job")(body)
        print("RESULT " + json.dumps({
            "enabled": nr.is_enabled(),
            "coro_fn": iscoroutinefunction_partial(job),
            "value": asyncio.run(job()),
            "agent_imported": "newrelic.agent" in sys.modules,
        }))
    """, nr_on=False)
    assert r == {"enabled": False, "coro_fn": True, "value": 42, "agent_imported": False}


def test_cancelled_background_work_is_not_an_error():
    r = _run("""
        from core import newrelic as nr
        nr.init()
        import newrelic.agent as agent
        seen = {}
        async def body():
            seen["txn"] = agent.current_transaction()
            await asyncio.sleep(30)
        async def main():
            t = asyncio.create_task(nr.background_transaction("scheduler/long")(body)())
            await asyncio.sleep(0.05)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        asyncio.run(main())
        txn = seen["txn"]
        print("RESULT " + json.dumps({"errors": len(txn._errors), "stopped": txn._state == txn.STATE_STOPPED}))
    """)
    assert r == {"errors": 0, "stopped": True}


# ----------------------------------------------------------- to_thread propagation

_TO_THREAD = """
from core import newrelic as nr
nr.init()
import asyncio.threads
import newrelic.agent as agent
from newrelic.api.background_task import BackgroundTask
from newrelic.api.application import application_instance

def add(a, b=0):
    return a + b, agent.current_transaction()

def boom():
    raise ValueError("kaput")

async def main():
    out = {"patched": asyncio.to_thread is not asyncio.threads.to_thread}
    with BackgroundTask(application_instance(), "outer") as txn:
        value, seen = await asyncio.to_thread(add, 1, b=2)
        out["value_in_txn"] = value
        out["thread_saw_txn"] = seen is txn
        try:
            await asyncio.to_thread(boom)
        except ValueError as e:
            out["raised_in_txn"] = str(e)
    value, seen = await asyncio.to_thread(add, 5)
    out["value_outside"] = value
    out["thread_saw_none_outside"] = seen is None
    try:
        await asyncio.to_thread(boom)
    except ValueError as e:
        out["raised_outside"] = str(e)
    return out

print("RESULT " + json.dumps(asyncio.run(main())))
"""


def test_to_thread_runs_under_the_callers_trace_and_behaves_identically():
    r = _run(_TO_THREAD)
    assert r == {
        "patched": True,
        "value_in_txn": 3, "thread_saw_txn": True, "raised_in_txn": "kaput",
        "value_outside": 5, "thread_saw_none_outside": True, "raised_outside": "kaput",
    }


def test_to_thread_kill_switch():
    r = _run(_TO_THREAD, extra_env={"NEW_RELIC_PROPAGATE_TO_THREAD": "off"})
    assert r["patched"] is False and r["thread_saw_txn"] is False
    assert r["value_in_txn"] == 3 and r["raised_in_txn"] == "kaput"


# ------------------------------------------------------------- events / metrics

def test_custom_events_link_the_trace_and_metrics_work_outside_transactions():
    r = _run("""
        from core import newrelic as nr
        nr.init()
        import newrelic.agent as agent
        from newrelic.api.background_task import BackgroundTask
        from newrelic.api.application import application_instance
        events, metrics = [], []
        agent.record_custom_event = lambda et, params, application=None: events.append((et, params, application is not None))
        agent.record_custom_metric = lambda name, value, application=None: metrics.append((name, application is not None))
        with BackgroundTask(application_instance(), "outer"):
            nr.record_custom_event("PipelineStage", {"stage": "x", "ms": 1, "skip": None})
            nr.record_custom_metric("Custom/In", 1)
        nr.record_custom_event("PipelineStage", {"stage": "y"})
        nr.record_custom_metric("Custom/Out", 1)
        inside, outside = events[0][1], events[1][1]
        print("RESULT " + json.dumps({
            "inside_trace": bool(inside.get("trace.id")) and bool(inside.get("span.id")),
            "inside_env": inside.get("env"),
            "inside_none_dropped": "skip" not in inside,
            "outside_trace": "trace.id" in outside,
            "always_application": all(e[2] for e in events),
            "metrics": metrics,
        }))
    """, extra_env={"NEW_RELIC_ENVIRONMENT": "qa"})
    assert r["inside_trace"] and r["inside_env"] == "qa" and r["inside_none_dropped"]
    assert r["outside_trace"] is False and r["always_application"]
    # Inside a transaction the metric goes on it; outside it needs the app.
    assert r["metrics"] == [["Custom/In", False], ["Custom/Out", True]]


# ------------------------------------------------- ASGI interface (#388) + config

_STANDIN = """
# Reproduces main.py's start-up order: logging, New Relic, then the app.
from core.logging import configure_logging, RequestIDMiddleware
configure_logging()
from core.newrelic import init
init()
from fastapi import FastAPI
app = FastAPI(redirect_slashes=False)
app.add_middleware(RequestIDMiddleware)

@app.get("/ping")
async def ping():
    return {"ok": True}
"""

_LOAD_AND_CALL = """
import uvicorn
cfg = uvicorn.Config("nr_standin:app", log_level="warning")
cfg.load()

async def call():
    sent = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/ping", "raw_path": b"/ping", "root_path": "", "query_string": b"",
             "headers": [(b"host", b"localhost")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        sent.append(message)
    await cfg.loaded_app(scope, receive, send)
    return [m.get("status") for m in sent if m["type"] == "http.response.start"]

from core import newrelic as nr
result = {"interface": cfg.interface, "statuses": asyncio.run(call()), "enabled": nr.is_enabled()}
if nr.is_enabled():
    import logging as _l
    from newrelic.core.config import global_settings
    gs = global_settings()
    result.update({
        "app_name": gs.app_name,
        "env_app_name": os.environ.get("NEW_RELIC_APP_NAME"),
        "labels": {l["label_type"]: l["label_value"] for l in gs.labels},
        "agent_log_level": gs.log_level,
        "agent_log_file": gs.log_file,
        "explain": gs.transaction_tracer.explain_enabled,
        "ignore_422": 422 in gs.error_collector.ignore_status_codes,
        "ignore_401": 401 in gs.error_collector.ignore_status_codes,
        "expected_401": 401 in gs.error_collector.expected_status_codes,
        "expected_429": 429 in gs.error_collector.expected_status_codes,
        "logging_hook": hasattr(_l.Logger.callHandlers, "__wrapped__"),
        "root_forwarders": sum(type(h).__name__ == "NewRelicLogForwardingHandler" for h in _l.getLogger().handlers),
        "uvicorn_forwarders": [h.level for h in _l.getLogger("uvicorn").handlers
                               if type(h).__name__ == "NewRelicLogForwardingHandler"],
        "access_forwarders": sum(type(h).__name__ == "NewRelicLogForwardingHandler"
                                 for h in _l.getLogger("uvicorn.access").handlers),
    })
print("RESULT " + json.dumps(result))
"""


@pytest.fixture
def standin(tmp_path):
    (tmp_path / "nr_standin.py").write_text(_STANDIN)
    return tmp_path


def test_uvicorn_sees_asgi3_with_the_agent_on(standin):
    r = _run(_LOAD_AND_CALL, extra_path=standin, extra_env={"NEW_RELIC_ENVIRONMENT": "qa"})
    assert r["enabled"] is True
    assert r["interface"] == "asgi3"
    assert r["statuses"] == [200]
    assert r["app_name"] == r["env_app_name"] == "PAIR API (QA)"
    assert r["labels"] == {"env": "qa", "service": "pair-api"}
    assert r["agent_log_file"] == "stderr" and r["agent_log_level"] == 30  # warning
    assert r["explain"] is False
    assert r["ignore_422"] and not r["ignore_401"] and r["expected_401"] and r["expected_429"]
    assert r["logging_hook"] is False  # [import-hook:logging] enabled = false
    assert r["root_forwarders"] == 1
    assert r["uvicorn_forwarders"] == [30]  # WARNING floor on "uvicorn"
    assert r["access_forwarders"] == 0


def test_uvicorn_sees_asgi3_with_the_agent_off(standin):
    r = _run(_LOAD_AND_CALL, extra_path=standin, nr_on=False)
    assert r == {"interface": "asgi3", "statuses": [200], "enabled": False}


def test_explicit_app_name_and_prod_defaults(standin):
    r = _run(_LOAD_AND_CALL, extra_path=standin, extra_env={
        "APP_BASE_URL": "https://pair.pyramidci.com", "NEW_RELIC_LOG_FORWARD_LEVEL": "",
    })
    assert r["app_name"] == "PAIR API (PROD)"
    assert r["labels"] == {"env": "prod", "service": "pair-api"}
    r = _run(_LOAD_AND_CALL, extra_path=standin, extra_env={"NEW_RELIC_APP_NAME": "PAIR API (sandbox)"})
    assert r["app_name"] == "PAIR API (sandbox)"


def test_log_forwarding_off(standin):
    r = _run(_LOAD_AND_CALL, extra_path=standin, extra_env={"NEW_RELIC_LOG_FORWARD_LEVEL": "OFF"})
    assert r["root_forwarders"] == 0 and r["uvicorn_forwarders"] == []


# ------------------------------------------- loop created before init (uvloop)

# uvicorn creates its event loop BEFORE importing main (where init() runs). The
# agent wraps a uvloop Loop's create_task only when the loop is created, so
# without core.newrelic._instrument_running_loop every task a request spawned
# (BaseHTTPMiddleware runs the route in one) lost the transaction: every web
# transaction was named starlette.middleware.base:BaseHTTPMiddleware and its
# DB/HTTP spans went missing. Reproduced on uvicorn 0.44 + uvloop 0.22.
_LOOP_BEFORE_INIT = """
import uvloop

names = []

async def main():
    from core import newrelic as nr
    nr.init()  # inside an already-running uvloop loop, like `uvicorn main:app`
    import newrelic.core.application as ca
    orig = ca.Application.record_transaction
    def rec(self, data, *a, **k):
        names.append(data.path)
        return orig(self, data, *a, **k)
    ca.Application.record_transaction = rec

    from fastapi import FastAPI, APIRouter
    from starlette.middleware.base import BaseHTTPMiddleware
    import httpx

    app = FastAPI()

    class Mw(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    app.add_middleware(Mw)
    router = APIRouter()

    @router.get("/api/v1/thing/{x}")
    async def thing(x: str):
        return {"x": x}

    app.include_router(router)
    import newrelic.agent as agent
    wrapped = agent.ASGIApplicationWrapper(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://t") as c:
        r = await c.get("/api/v1/thing/abc")
    await asyncio.sleep(0.2)
    print("RESULT " + json.dumps({"status": r.status_code, "names": names}))

uvloop.run(main())
"""


def test_route_named_when_uvloop_started_before_init():
    pytest.importorskip("uvloop")
    out = _run(_LOOP_BEFORE_INIT)
    assert out["status"] == 200
    assert out["names"], out
    assert all("BaseHTTPMiddleware" not in n for n in out["names"]), out["names"]
    assert any(n.endswith("thing") for n in out["names"]), out["names"]
