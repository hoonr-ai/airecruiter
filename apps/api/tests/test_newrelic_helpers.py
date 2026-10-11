"""New Relic helpers that must behave without the agent (core/newrelic.py,
core/tasks.py, core/logging.py, core/pipeline_trace.py, the JobDiva telemetry
hook, the limiter event merge and the BI-sync skip log level).

Nothing here initialises the real agent: init() is only exercised on paths
that return before `import newrelic`. Agent behaviour (transactions, ASGI
interface, to_thread propagation) is covered in subprocesses by
tests/test_newrelic_agent.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

from core import newrelic as nr
from core import pipeline_trace as pt
from core import tasks as core_tasks

API_DIR = Path(__file__).resolve().parents[1]

_ENV_KEYS = ("NEW_RELIC_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV", "APP_BASE_URL")


@pytest.fixture
def clean_env():
    """Snapshot/restore os.environ: init() setdefaults NEW_RELIC_* keys."""
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("NEW_RELIC_") or key in _ENV_KEYS:
            del os.environ[key]
    try:
        yield os.environ
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def fresh_nr(monkeypatch):
    monkeypatch.setattr(nr, "_initialized", False)
    monkeypatch.setattr(nr, "_newrelic_app", None)
    monkeypatch.setattr(nr, "_env", None)
    return nr


# --------------------------------------------------------------- environment

@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, "local"),
        ({"NEW_RELIC_ENVIRONMENT": "QA"}, "qa"),
        ({"NEW_RELIC_ENVIRONMENT": "production"}, "prod"),
        ({"APP_ENV": "staging"}, "qa"),
        ({"ENVIRONMENT": "production"}, "prod"),
        ({"ENV": "dev"}, "local"),
        ({"ENVIRONMENT": "weird", "APP_BASE_URL": "https://pairqa.pyramidci.com"}, "qa"),
        ({"APP_BASE_URL": "https://pair.pyramidci.com/"}, "prod"),
        ({"APP_BASE_URL": "https://pairqa.pyramidci.com"}, "qa"),
        ({"APP_BASE_URL": "http://localhost:3000"}, "local"),
        # Explicit selector beats everything else.
        ({"NEW_RELIC_ENVIRONMENT": "qa", "ENVIRONMENT": "production", "APP_BASE_URL": "https://pair.pyramidci.com"}, "qa"),
    ],
)
def test_resolve_environment(clean_env, env, expected):
    clean_env.update(env)
    assert nr.resolve_environment() == expected


def test_default_app_names():
    assert nr.default_app_name("qa") == "PAIR API (QA)"
    assert nr.default_app_name("prod") == "PAIR API (PROD)"
    assert nr.default_app_name("local") == "PAIR API (local)"


# ----------------------------------------------------------------------- init

def test_init_without_key_stays_disabled(clean_env, fresh_nr, monkeypatch):
    monkeypatch.setitem(sys.modules, "newrelic", None)  # any import would raise
    fresh_nr.init()
    assert not fresh_nr.is_enabled()
    assert "NEW_RELIC_APP_NAME" not in os.environ


@pytest.mark.parametrize("var, value", [
    ("NEW_RELIC_ENABLED", "false"), ("NEW_RELIC_ENABLED", "0"), ("NEW_RELIC_ENABLED", "OFF"),
    ("NEW_RELIC_MONITOR_MODE", "false"),
])
def test_kill_switch_returns_before_importing_the_agent(clean_env, fresh_nr, monkeypatch, caplog, var, value):
    clean_env["NEW_RELIC_LICENSE_KEY"] = "x" * 40
    clean_env[var] = value
    monkeypatch.setitem(sys.modules, "newrelic", None)
    with caplog.at_level(logging.WARNING, logger="core.newrelic"):
        fresh_nr.init()
    assert not fresh_nr.is_enabled()
    assert any("disabled by NEW_RELIC_ENABLED" in r.getMessage() for r in caplog.records)
    assert not any("not installed" in r.getMessage() for r in caplog.records)


def test_init_never_starts_the_agent_under_pytest(clean_env, fresh_nr, monkeypatch):
    clean_env["NEW_RELIC_LICENSE_KEY"] = "x" * 40
    monkeypatch.setitem(sys.modules, "newrelic", None)
    fresh_nr.init()
    assert not fresh_nr.is_enabled()


def test_old_agent_is_switched_off(clean_env, fresh_nr, monkeypatch, caplog):
    clean_env["NEW_RELIC_LICENSE_KEY"] = "x" * 40
    clean_env["NEW_RELIC_ENVIRONMENT"] = "qa"
    clean_env["NEW_RELIC_ALLOW_UNDER_PYTEST"] = "1"
    fake = types.ModuleType("newrelic")
    fake.version_info = [11, 2, 0]  # old agents expose a list
    agent = types.ModuleType("newrelic.agent")

    def _boom(*_a, **_k):
        raise AssertionError("initialize must not run on an old agent")

    agent.initialize = _boom
    agent.register_application = _boom
    monkeypatch.setitem(sys.modules, "newrelic", fake)
    monkeypatch.setitem(sys.modules, "newrelic.agent", agent)
    with caplog.at_level(logging.ERROR, logger="core.newrelic"):
        fresh_nr.init()
    assert not fresh_nr.is_enabled()
    assert any("older than" in r.getMessage() for r in caplog.records)
    # Env defaults are set before the agent import (the agent reads them once).
    assert os.environ["NEW_RELIC_APP_NAME"] == "PAIR API (QA)"
    assert os.environ["NEW_RELIC_LABELS"] == "env:qa;service:pair-api"
    assert os.environ["NEW_RELIC_LOG"] == "stderr"
    assert os.environ["NEW_RELIC_LOG_LEVEL"] == "warning"


def test_explicit_app_name_is_not_overridden(clean_env, fresh_nr, monkeypatch):
    clean_env.update({"NEW_RELIC_LICENSE_KEY": "x" * 40, "NEW_RELIC_APP_NAME": "Custom", "NEW_RELIC_ENVIRONMENT": "prod"})
    fake = types.ModuleType("newrelic")
    fake.version_info = (11, 0, 0)
    monkeypatch.setitem(sys.modules, "newrelic", fake)
    fresh_nr.init()
    assert os.environ["NEW_RELIC_APP_NAME"] == "Custom"


def test_core_modules_never_import_the_agent():
    """core.logging runs before NR init and must not pull in newrelic (the agent
    reads its env defaults once, at first import); importing the helpers alone
    must not either."""
    code = (
        "import sys; sys.path.insert(0, 'tests');"
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: False;"
        "from env_stubs import stub_required_env; stub_required_env();"
        "import core.logging;"
        "assert 'newrelic' not in sys.modules, 'core.logging imported newrelic';"
        "import core.newrelic, core.tasks, core.pipeline_trace;"
        "assert 'newrelic' not in sys.modules, 'helpers imported newrelic';"
        "print('ok')"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEW_RELIC_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=API_DIR, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().endswith("ok")


def test_main_no_longer_wraps_the_app_by_hand():
    """#388: the manual ASGIApplicationWrapper made uvicorn pick asgi2 and 500
    every request. The agent's own hooks create web transactions."""
    src = (API_DIR / "main.py").read_text()
    assert "ASGIApplicationWrapper" not in src


# -------------------------------------------------------------- log forwarding

@pytest.mark.parametrize("raw, env, expected", [
    ("", "prod", logging.WARNING),
    ("", "qa", logging.INFO),
    ("", "local", logging.INFO),
    ("OFF", "qa", None),
    ("off", "prod", None),
    ("ERROR", "qa", logging.ERROR),
    ("info", "prod", logging.INFO),
    ("loud", "prod", logging.WARNING),
])
def test_log_forward_level(clean_env, monkeypatch, raw, env, expected):
    monkeypatch.setattr(nr, "_env", env)
    if raw:
        clean_env["NEW_RELIC_LOG_FORWARD_LEVEL"] = raw
    assert nr.log_forward_level() == expected


def test_scrub_pii():
    s = nr.scrub_pii(
        "mail jane.doe+x@example.co.uk phone 5551234567, (555) 123-4567, +1 555-123-4567; "
        "job 26-23319 at 2026-10-11 12:00:00 took 1234 ms"
    )
    assert "@" not in s and "jane" not in s
    assert "5551234567" not in s and "123-4567" not in s
    assert "26-23319" in s and "2026-10-11 12:00:00" in s and "1234 ms" in s


@pytest.mark.parametrize("text", [
    "a" * 200_000,                                   # one long token
    "%2C".join(["field%3Dvalue"] * 2000),            # URL-encoded query string
    ("QUJD" * 100_000),                              # base64 blob
    "x@" * 50_000,                                   # many '@', no domain
    "a@" + "b" * 100_000,                            # domain with no dot
    "1-" * 50_000,                                   # digit/separator soup
])
def test_scrub_pii_is_linear(text):
    """It runs on the event-loop thread: the old unbounded local part took 4 s
    on a 50k-letter token and 65 s on 200k."""
    t0 = time.perf_counter()
    nr.scrub_pii(text)
    assert time.perf_counter() - t0 < 1.0


def test_scrub_pii_long_local_part_is_still_masked():
    s = nr.scrub_pii("to " + "x" * 100 + "@example.com done")
    assert "@" not in s and "example" not in s and s.endswith("<email> done")


def _fmt(msg):
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "%s", (msg,), None)
    return nr.PiiScrubbingFormatter().format(record)


def test_formatter_cuts_before_scrubbing():
    out = _fmt("a" * 500_000 + " jane@example.com")
    assert len(out) == 32768 and "@" not in out


@pytest.mark.parametrize("tail, leaked", [
    ("jane.doe@exam", "jane"),        # address cut inside its domain
    ("jane.doe@", "jane"),            # cut right after the '@'
    ("(555) 123-45", "123-45"),       # number cut short of the phone pattern
])
def test_formatter_masks_what_the_cut_split(tail, leaked):
    head = "word " * (32768 // 5)
    msg = head[: 32768 - len(tail)] + tail + "ple.com and more text after the cut " * 10
    out = _fmt(msg)
    assert len(out) <= 32768
    assert leaked not in out[-400:]
    assert "@" not in out
    assert out.startswith("word word")


def test_formatter_short_messages_are_unchanged_apart_from_pii():
    assert _fmt("plain 26-23319 at 2026-10-11 12:00:00") == "plain 26-23319 at 2026-10-11 12:00:00"


def test_scrubbing_formatter_leaves_the_shared_record_alone():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "user %s", ("a@b.com",), None)
    out = nr.PiiScrubbingFormatter().format(record)
    assert out == "user <email>"
    assert record.getMessage() == "user a@b.com"  # stdout copy unchanged


def test_attach_log_forwarding_is_noop_when_disabled(fresh_nr):
    before = list(logging.getLogger().handlers)
    fresh_nr.attach_log_forwarding()
    assert list(logging.getLogger().handlers) == before


def test_safe_attributes_drop_pii():
    out = nr.safe_attributes({
        "job_id": "26-23319", "request_id": "a" * 32, "n": 3, "ok": True,
        "candidate_name": "Jane", "email": "x", "note": "a@b.com", "blob": "x" * 200, "obj": {"a": 1},
    })
    assert out == {"job_id": "26-23319", "request_id": "a" * 32, "n": 3, "ok": True}


# ------------------------------------------------- background_transaction (off)

def test_background_transaction_disabled_is_a_passthrough(fresh_nr):
    from apscheduler.util import iscoroutinefunction_partial

    async def job(x, y=1):
        return x + y

    async def bad():
        raise ValueError("nope")

    wrapped = nr.background_transaction("scheduler/job")(job)
    assert iscoroutinefunction_partial(wrapped)
    assert wrapped.__name__ == "job"
    assert asyncio.run(wrapped(1, y=2)) == 3
    with pytest.raises(ValueError, match="nope"):
        asyncio.run(nr.background_transaction("scheduler/bad")(bad)())

    async def ctx():
        async with nr.background_transaction("launch:123") as bt:
            assert bt.name == "launch" and bt.task_key == "123"
            return "done"

    assert asyncio.run(ctx()) == "done"
    with nr.background_transaction("sync"):
        pass


def test_background_transaction_leaves_sync_functions_alone():
    def sync_job():
        return 1

    assert nr.background_transaction("x")(sync_job) is sync_job


def test_helpers_are_noops_when_disabled(fresh_nr):
    nr.record_custom_event("X", {"a": 1})
    nr.record_custom_metric("Custom/X", 1)
    nr.add_transaction_attributes(job_id="1")
    nr.suppress_apdex()
    nr.ignore_transaction()
    nr.capture_exception(ValueError("x"))


def test_to_thread_is_untouched_when_disabled():
    import asyncio.threads

    if not nr.is_enabled():
        assert asyncio.to_thread is asyncio.threads.to_thread


# ------------------------------------------------------------------ core.tasks

def test_spawn_without_agent_runs_the_coroutine_itself(fresh_nr):
    async def work():
        return 7

    async def main():
        coro = work()
        task = core_tasks.spawn(coro, name="launch:abc")
        assert task.get_coro() is coro  # no wrapper when NR is off
        assert task.get_name() == "launch:abc"
        return await task

    assert asyncio.run(main()) == 7


def test_spawn_wraps_in_a_background_transaction_when_enabled(monkeypatch):
    opened = []

    class _FakeTxn:
        def __init__(self, name, group="Task", **_):
            self.name = name

        async def __aenter__(self):
            opened.append(self.name)
            return self

        async def __aexit__(self, *exc):
            return False

    detached = []
    monkeypatch.setattr(nr, "is_enabled", lambda: True)
    monkeypatch.setattr(nr, "background_transaction", _FakeTxn)
    monkeypatch.setattr(nr, "detach_inherited_trace", lambda: detached.append(asyncio.current_task().get_name()))

    async def work():
        return "ok"

    async def main():
        results = [
            await core_tasks.spawn(work(), name="launch:abc"),
            await core_tasks.spawn(work(), name="runner", nr_txn=False),
            await core_tasks.spawn(work(), name="active_job_refresh:J1"),
            await core_tasks.spawn(work()),
        ]
        return results

    assert asyncio.run(main()) == ["ok"] * 4
    assert opened == ["launch:abc", "test_spawn_wraps_in_a_background_transaction_when_enabled.<locals>.work"]
    # Every spawned task leaves the spawning request's trace, transaction or not,
    # inside the new task itself.
    assert detached == [
        "launch:abc", "runner", "active_job_refresh:J1",
        "test_spawn_wraps_in_a_background_transaction_when_enabled.<locals>.work",
    ]


def test_detach_inherited_trace_is_a_noop_when_disabled(fresh_nr, monkeypatch):
    monkeypatch.setitem(sys.modules, "newrelic", None)  # any import would raise
    nr.detach_inherited_trace()


def test_stop_flag():
    flag = core_tasks.stopping
    try:
        assert not core_tasks.is_stopping()

        async def main():
            waiter = asyncio.create_task(flag.wait())
            await asyncio.sleep(0)
            assert not waiter.done()
            core_tasks.mark_stopping()
            return await asyncio.wait_for(waiter, 1)

        assert asyncio.run(main()) is True
        assert core_tasks.is_stopping()
        # A second loop sees the flag immediately.
        assert asyncio.run(flag.wait()) is True
    finally:
        flag.clear()
    assert not core_tasks.is_stopping()


def test_drain_does_not_raise_the_stop_flag():
    asyncio.run(core_tasks.drain(timeout=0.01))
    assert not core_tasks.is_stopping()


# -------------------------------------------------------------- pipeline_trace

def test_adopt_run_and_started_at_ms(monkeypatch):
    sent = []
    monkeypatch.setattr(pt, "record_custom_event", lambda name, payload: sent.append((name, payload)))

    async def main():
        assert pt.adopt_run(run_id="sess-1", job_id="J-9") == "sess-1"
        before = int(time.time() * 1000)
        async with pt.span("launch_batch", batch_idx=0):
            pass
        with pt.span_sync("selection"):
            pass
        return before

    before = asyncio.run(main())
    assert [p["stage"] for _n, p in sent] == ["launch_batch", "selection"]
    for name, p in sent:
        assert name == "PipelineStage"
        assert p["run_id"] == "sess-1" and p["job_id"] == "J-9"
        assert before <= p["started_at_ms"] <= int(time.time() * 1000)


def test_adopt_run_is_task_local():
    async def child():
        pt.adopt_run("child-run", "J")
        return pt.run_id.get()

    async def main():
        pt.adopt_run("parent-run", "J")
        got = await asyncio.create_task(child())
        return got, pt.run_id.get()

    assert asyncio.run(main()) == ("child-run", "parent-run")


# ------------------------------------------------------- JobDiva telemetry hook

def _jd_response(status, *, content=b"", headers=None, url="https://api.jobdiva.com/apiv2/bi/CandidatesDetail", t0_ago=0.05):
    req = httpx.Request("GET", url)
    req.extensions["_jd_t0"] = time.monotonic() - t0_ago
    return httpx.Response(status, content=content, headers=headers or {}, request=req)


@pytest.fixture
def jd_capture(monkeypatch):
    from services import jobdiva as jd

    metrics, events = [], []
    monkeypatch.setattr(nr, "is_enabled", lambda: True)
    monkeypatch.setattr(nr, "record_custom_metric", lambda name, value: metrics.append((name, value)))
    monkeypatch.setattr(nr, "record_custom_event", lambda et, params=None: events.append((et, params)))
    return jd, metrics, events


def test_jobdiva_hook_off_never_reads_the_body(monkeypatch):
    from services import jobdiva as jd

    monkeypatch.setattr(nr, "is_enabled", lambda: False)
    resp = _jd_response(500, content=b"boom")

    async def _no_read():
        raise AssertionError("aread must not run when New Relic is off")

    resp.aread = _no_read
    asyncio.run(jd._jd_on_response(resp))


def test_jobdiva_hook_fast_success_is_metrics_only(jd_capture):
    jd, metrics, events = jd_capture
    resp = _jd_response(200, content=b'{"data": "candidate stuff"}')

    async def _no_read():
        raise AssertionError("success bodies are never read")

    resp.aread = _no_read
    asyncio.run(jd._jd_on_response(resp))
    names = [m[0] for m in metrics]
    assert names == ["Custom/JobDiva/apiv2/bi/CandidatesDetail/ms", "Custom/JobDiva/apiv2/bi/CandidatesDetail/status/200"]
    assert metrics[1][1] == {"count": 1}
    assert events == []


def test_jobdiva_hook_slow_call_event_has_no_url_query_or_body(jd_capture):
    jd, _metrics, events = jd_capture
    resp = _jd_response(
        200, content=b"x" * 10, headers={"content-length": "10"}, t0_ago=6.0,
        url="https://api.jobdiva.com/apiv2/jobdiva/searchCandidateProfile?email=jane%40x.com&firstName=Jane&phone=5551234567",
    )
    asyncio.run(jd._jd_on_response(resp))
    assert len(events) == 1
    et, params = events[0]
    assert et == "JobDivaAPI"
    assert set(params) == {"endpoint", "method", "status_code", "elapsed_ms", "response_size_bytes"}
    assert params["endpoint"] == "/apiv2/jobdiva/searchCandidateProfile"
    assert params["elapsed_ms"] >= 5000 and params["response_size_bytes"] == 10
    assert "Jane" not in str(params) and "5551234567" not in str(params)


def test_jobdiva_hook_error_kind(jd_capture):
    jd, _metrics, events = jd_capture
    body = b'{"message": "Too many requests"}'
    asyncio.run(jd._jd_on_response(_jd_response(429, content=body, headers={"content-length": str(len(body))})))
    pii = b'{"message": "No candidate jane@x.com"}'
    asyncio.run(jd._jd_on_response(_jd_response(404, content=pii, headers={"content-length": str(len(pii))})))
    ids = b"Candidate 123456789 not found"
    asyncio.run(jd._jd_on_response(_jd_response(400, content=ids, headers={"content-length": str(len(ids))})))
    # Undeclared / large bodies are not read: the reason phrase names the error.
    big = _jd_response(500, content=b"x" * 5000, headers={"content-length": "5000"})

    async def _no_read():
        raise AssertionError("large error bodies are not read")

    big.aread = _no_read
    asyncio.run(jd._jd_on_response(big))
    kinds = [p["error_kind"] for _et, p in events]
    assert kinds == ["Too many requests", "Not Found", "Bad Request", "Internal Server Error"]


def test_jobdiva_metric_path_is_bounded():
    from services import jobdiva as jd

    assert jd._jd_metric_path("/apiv2/bi/CandidatesDetail") == "apiv2/bi/CandidatesDetail"
    assert jd._jd_metric_path("/x/apiv2/jobdiva/getCandidateById/123456") == "jobdiva/getCandidateById/#"
    assert jd._jd_metric_path("") == "root"


def test_jobdiva_hook_never_raises(monkeypatch):
    from services import jobdiva as jd

    monkeypatch.setattr(nr, "is_enabled", lambda: True)

    def _explode(*_a, **_k):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(nr, "record_custom_metric", _explode)
    asyncio.run(jd._jd_on_response(_jd_response(200)))


# ------------------------------------------------------------ limiter / BI sync

def test_limiter_counters_share_the_limiter_event_type(monkeypatch):
    from services import jobdiva_rate_limit as rl

    sent = []
    monkeypatch.setattr(nr, "record_custom_event", lambda et, params=None: sent.append((et, params)))
    monkeypatch.setattr(rl, "_prio_stats", {"background": {"bi_wait_ms": 5, "bi_cooldown_ms": 0, "bi_429_total": 1}})
    rl._emit_counters("background", "sweep")
    assert sent == [("JobDivaBiLimiter", {
        "outcome": "counters", "label": "sweep", "priority": "background",
        "bi_wait_ms": 5, "bi_cooldown_ms": 0, "bi_429_total": 1,
    })]


def test_bi_sync_lock_skip_logs_at_debug(monkeypatch, caplog):
    from services import jobdiva_bi_sync as bi

    monkeypatch.setattr(bi, "SYNC_ENABLED", True)
    summaries = iter([{"skipped": "another worker holds the JobDiva BI sync lock"}, {"users": 3}])

    async def fake_cycle(*_a, **_k):
        return next(summaries)

    monkeypatch.setattr(bi, "run_sync_cycle", fake_cycle)
    with caplog.at_level(logging.DEBUG, logger=bi.logger.name):
        asyncio.run(bi.scheduled_sync())
        asyncio.run(bi.scheduled_sync())
    levels = [r.levelno for r in caplog.records if "[JobDivaBI] cycle done" in r.getMessage()]
    assert levels == [logging.DEBUG, logging.INFO]


# ------------------------------------------------------------- RequestID middleware

def test_request_id_middleware_tags_the_transaction(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.logging import RequestIDMiddleware

    tagged = []
    monkeypatch.setattr(nr, "is_enabled", lambda: True)
    monkeypatch.setattr(nr, "add_transaction_attributes", lambda **kw: tagged.append(kw))

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/jobs/{job_id}/thing")
    async def thing(job_id: str):
        return {"job_id": job_id}

    @app.get("/plain")
    async def plain():
        return {}

    client = TestClient(app)
    r = client.get("/jobs/26-23319/thing", headers={"X-Request-ID": "rid-1"})
    assert r.status_code == 200 and r.headers["X-Request-ID"] == "rid-1"
    assert tagged == [{"request_id": "rid-1"}, {"job_id": "26-23319"}]
    tagged.clear()
    client.get("/plain")
    assert len(tagged) == 1 and set(tagged[0]) == {"request_id"}


def test_request_id_middleware_survives_telemetry_errors(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.logging import RequestIDMiddleware

    monkeypatch.setattr(nr, "is_enabled", lambda: True)

    def _explode(**_kw):
        raise RuntimeError("nr down")

    monkeypatch.setattr(nr, "add_transaction_attributes", _explode)
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/jobs/{job_id}")
    async def job(job_id: str):
        return {}

    assert TestClient(app).get("/jobs/1").status_code == 200
