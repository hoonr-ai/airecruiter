"""core.queue: enqueue to arq when enabled, fall back to an in-process spawn otherwise."""

import asyncio

import pytest

import core.queue as queue
import core.tasks as core_tasks
import core.queue_tasks as queue_tasks


@pytest.fixture
def spawned(monkeypatch):
    calls = []

    def fake_spawn(coro, name=None):
        calls.append(name)
        coro.close()

    monkeypatch.setattr(core_tasks, "spawn", fake_spawn)
    return calls


class _FakeJob:
    job_id = "job-1"


class _FakePool:
    def __init__(self, result=_FakeJob(), error=None):
        self.result, self.error, self.calls = result, error, []

    async def enqueue_job(self, name, *args, _job_id=None, **kwargs):
        self.calls.append((name, args, _job_id))
        if self.error:
            raise self.error
        return self.result


def _enable(monkeypatch, pool):
    monkeypatch.setattr(queue._cfg, "ARQ_ENABLED", True, raising=False)
    monkeypatch.setattr(queue._cfg, "REDIS_URL", "redis://localhost:6379/0")

    async def get_pool():
        return pool

    monkeypatch.setattr(queue, "_get_pool", get_pool)


def test_disabled_queue_spawns_in_process(monkeypatch, spawned):
    monkeypatch.setattr(queue._cfg, "ARQ_ENABLED", False, raising=False)
    out = asyncio.run(queue.enqueue(queue_tasks.PROVISION_JOBDIVA_BATCH, ["a"], "J1"))
    assert out == "spawned"
    assert spawned == ["provision_jobdiva_batch:inline"]


def test_enabled_queue_enqueues_without_spawning(monkeypatch, spawned):
    pool = _FakePool()
    _enable(monkeypatch, pool)
    out = asyncio.run(queue.enqueue(queue_tasks.PROVISION_JOBDIVA_BATCH, ["a"], "J1", job_id="k"))
    assert out == "queued"
    assert pool.calls == [("provision_jobdiva_batch", (["a"], "J1"), "k")]
    assert spawned == []


def test_duplicate_job_id_is_not_spawned(monkeypatch, spawned):
    _enable(monkeypatch, _FakePool(result=None))
    out = asyncio.run(queue.enqueue(queue_tasks.PROVISION_JOBDIVA_BATCH, ["a"], "J1", job_id="k"))
    assert out == "duplicate"
    assert spawned == []


def test_enqueue_failure_falls_back_to_spawn(monkeypatch, spawned):
    _enable(monkeypatch, _FakePool(error=ConnectionError("redis down")))
    out = asyncio.run(queue.enqueue(queue_tasks.PROVISION_JOBDIVA_BATCH, ["a"], "J1", job_id="k"))
    assert out == "spawned"
    assert spawned == ["provision_jobdiva_batch:k"]


def test_ambiguous_enqueue_failure_does_not_rerun_a_run_once_task(monkeypatch, spawned):
    # The job may have landed in Redis; spawning too could send the email twice.
    _enable(monkeypatch, _FakePool(error=TimeoutError("no reply")))
    out = asyncio.run(queue.enqueue(queue_tasks.ZERO_MATCH_NOTIFICATION, "J1", "a@x.com"))
    assert out == "failed"
    assert spawned == []


def test_pool_failure_spawns_even_a_run_once_task(monkeypatch, spawned):
    monkeypatch.setattr(queue._cfg, "ARQ_ENABLED", True, raising=False)
    monkeypatch.setattr(queue._cfg, "REDIS_URL", "redis://localhost:6379/0")

    async def get_pool():
        raise ConnectionError("refused")

    monkeypatch.setattr(queue, "_get_pool", get_pool)
    out = asyncio.run(queue.enqueue(queue_tasks.ZERO_MATCH_NOTIFICATION, "J1", "a@x.com"))
    assert out == "spawned"
    assert spawned == ["zero_match_notification:inline"]


def test_unknown_task_raises():
    with pytest.raises(KeyError):
        asyncio.run(queue.enqueue("no_such_task"))


def test_worker_registers_every_task_with_its_retry_policy():
    import worker

    by_name = {f.name: f for f in worker.WorkerSettings.functions}
    assert set(by_name) == set(queue.registered_tasks())
    assert by_name["provision_jobdiva_batch"].max_tries == 2


def test_worker_wrapper_drops_arq_ctx(monkeypatch):
    import worker
    from routers.engagement import provisioning

    seen = []

    async def fake_provision(ids, job_id, **_k):
        seen.append((ids, job_id))
        return {"success": len(ids)}

    monkeypatch.setattr(provisioning, "_provision_batch_to_jobdiva", fake_provision)
    fn = {f.name: f for f in worker.WorkerSettings.functions}["provision_jobdiva_batch"].coroutine
    out = asyncio.run(fn({"job_id": "x", "job_try": 1}, ["a", "b"], "J1"))
    assert out == {"success": 2}
    assert seen == [(["a", "b"], "J1")]


def test_tasks_forward_their_arguments_to_the_services(monkeypatch):
    from routers.engagement import launch, pairbot, provisioning

    calls = []

    async def rec(label, *a, **k):
        calls.append((label, a, k))
        return {"status": "success", "data": "note-1"} if label == "note" else None

    class _Svc:
        def __getattr__(self, attr):
            return lambda *a, **k: rec(attr if attr != "create_candidate_note" else "note", *a, **k)

    monkeypatch.setattr(launch, "auto_assign_service", _Svc())
    monkeypatch.setattr(provisioning, "auto_assign_service", _Svc())
    monkeypatch.setattr(provisioning, "jobdiva_service", _Svc())
    monkeypatch.setattr(pairbot, "_send_pair_launch_email", lambda **k: rec("email", **k))

    t = {name: v.func for name, v in queue.registered_tasks().items()}
    asyncio.run(t["sync_job_applicants"]("J1"))
    asyncio.run(t["send_pair_launch_email"]("J1", 3, True, "https://x", False))
    asyncio.run(t["update_jobdiva_qualification"]("C1", "PAIR Candidates", "PASS", 7, "2026-10-19", 9))
    asyncio.run(t["create_jobdiva_pass_note"]("C1", "J1", "PASS", "text", 7))
    asyncio.run(t["refresh_job_metrics"]("J1"))

    assert calls == [
        ("synchronize_job_applicants", ("J1",), {}),
        ("email", (), {"job_id": "J1", "candidate_count": 3, "send_job_posting": True,
                       "app_base_url": "https://x", "relaunch": False}),
        ("update_candidate_qualification", (), {"candidate_id": "C1", "qualification_name": "PAIR Candidates",
                                                "value": "PASS", "recruiter_id": 7, "update_date": "2026-10-19",
                                                "qualification_type_id": 9}),
        ("note", (), {"candidate_id": "C1", "job_id": "J1", "action": "PASS", "note_text": "text", "recruiter_id": 7}),
        ("pin_candidate_note", (), {"note_id": "note-1", "is_pinned": True}),
        ("refresh_job_performance_metrics", ("J1",), {}),
    ]
