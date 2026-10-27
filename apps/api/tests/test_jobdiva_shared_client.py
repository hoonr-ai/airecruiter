"""services.jobdiva reuses one JobDiva client per worker, and the job-status
poll loads only the columns it uses (memray, 2026-10-28)."""
import asyncio

import httpx

from services import jobdiva


def _mock_shared(monkeypatch, handler):
    made = []

    def factory():
        client = jobdiva._JDAsyncClient(transport=httpx.MockTransport(handler))
        made.append(client)
        return client

    monkeypatch.setattr(jobdiva, "_SHARED_JD_CLIENTS", __import__("weakref").WeakKeyDictionary())
    real = jobdiva._shared_jd_client

    def shared():
        loop = asyncio.get_running_loop()
        client = jobdiva._SHARED_JD_CLIENTS.get(loop)
        if client is None or client.is_closed:
            client = jobdiva._SHARED_JD_CLIENTS[loop] = factory()
        return client

    monkeypatch.setattr(jobdiva, "_shared_jd_client", shared)
    return made, real


def test_every_call_site_shares_one_client_and_keeps_its_own_timeout(monkeypatch):
    seen_timeouts = []

    def handler(request):
        seen_timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={})

    made, _ = _mock_shared(monkeypatch, handler)

    async def main():
        async with jobdiva.httpx.AsyncClient(timeout=10.0) as a:
            await a.get("https://api.jobdiva.test/x")
        async with jobdiva.httpx.AsyncClient(timeout=30.0) as b:
            await b.post("https://api.jobdiva.test/y", json={})
        return made[0].is_closed

    closed_after_with = asyncio.run(main())
    assert len(made) == 1
    assert not closed_after_with  # `async with` leaves the shared client open
    assert seen_timeouts == [10.0, 30.0]


def test_the_shared_handle_still_paces_itself():
    assert jobdiva.httpx.AsyncClient(timeout=5.0).paces_itself is True


def test_other_construction_gets_its_own_client():
    client = jobdiva.httpx.AsyncClient(timeout=5.0, base_url="https://x.test")
    assert isinstance(client, jobdiva._JDAsyncClient)


class _Result:
    def fetchall(self):
        return []


class _Conn:
    def __init__(self, seen):
        self.seen = seen

    def execute(self, sql, *a):
        self.seen.append(str(sql))
        return _Result()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _Engine:
    def __init__(self):
        self.seen = []

    def connect(self):
        return _Conn(self.seen)


def test_poll_snapshot_loads_only_the_columns_it_uses(monkeypatch):
    svc = jobdiva.jobdiva_service
    engine = _Engine()
    monkeypatch.setattr(svc, "engine", engine)
    svc.get_all_monitored_jobs(include_archived=False)
    svc.get_all_monitored_jobs()
    poll_sql, all_sql = engine.seen
    assert "SELECT *" not in poll_sql
    assert poll_sql.startswith("SELECT job_id, status, customer_name, title, is_archived FROM monitored_jobs")
    assert "WHERE is_archived IS NOT TRUE" in poll_sql
    assert "WHERE" not in all_sql


def test_the_shared_client_keeps_no_cookies_between_callers(monkeypatch):
    monkeypatch.setattr(jobdiva, "_SHARED_JD_CLIENTS", __import__("weakref").WeakKeyDictionary())
    sent_cookies = []

    def handler(request):
        sent_cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"Set-Cookie": "session=abc; Path=/"})

    async def main():
        client = jobdiva._shared_jd_client()
        client._transport = httpx.MockTransport(handler)
        await jobdiva.httpx.AsyncClient(timeout=5.0).get("https://api.jobdiva.test/a")
        await jobdiva.httpx.AsyncClient(timeout=5.0).get("https://api.jobdiva.test/b")

    asyncio.run(main())
    assert sent_cookies == [None, None]


def test_head_and_stream_are_forwarded(monkeypatch):
    made, _ = _mock_shared(monkeypatch, lambda req: httpx.Response(200, text="ok"))

    async def main():
        handle = jobdiva.httpx.AsyncClient(timeout=5.0)
        head = await handle.head("https://api.jobdiva.test/h")
        async with handle.stream("GET", "https://api.jobdiva.test/s") as resp:
            body = await resp.aread()
        return head.status_code, body

    assert asyncio.run(main()) == (200, b"ok")


def test_every_jobdiva_write_invalidates_cached_reads():
    """Writes are marked with @_invalidates_reads; a write method added without
    it would let a cached pre-write read answer for up to the cache TTL."""
    import inspect as _inspect

    writes = {
        "update_candidate_resume_text", "update_job_user_fields", "create_candidate_note",
        "create_candidate_sticky_note", "update_candidate_qualification", "link_candidate_to_job",
        "create_job_application_with_resume", "upload_resume", "_complete_created_profile",
        "update_candidate_social_links", "_update_candidate_name", "_update_candidate_profile",
    }
    src = _inspect.getsource(jobdiva.JobDivaService)
    for name in writes:
        assert f"@_invalidates_reads(" in src.split(f"async def {name}(")[0].rsplit("\n\n", 1)[-1], name


def test_a_write_bumps_the_generation_before_and_after(monkeypatch):
    monkeypatch.setattr(jobdiva, "_local_read_gens", {})
    seen = []

    @jobdiva._invalidates_reads(("candidate", "candidate_id"), ("submittals", "job_id"))
    async def write(self, candidate_id, job_id):
        seen.append(dict(jobdiva._local_read_gens))
        return True

    assert asyncio.run(write(None, 42, "26-1")) is True
    assert seen == [{"candidate:42": 1, "submittals:*": 1}]
    assert jobdiva._local_read_gens == {"candidate:42": 2, "submittals:*": 2}


def test_cached_candidate_read_is_dropped_by_a_write(monkeypatch):
    from core.bounded_cache import AsyncTTLCache

    monkeypatch.setattr(jobdiva, "_read_cache", AsyncTTLCache(maxsize=10, ttl_s=30))
    monkeypatch.setattr(jobdiva, "_local_read_gens", {})
    svc = jobdiva.jobdiva_service
    answers = iter([{"name": "old"}, {"name": "new"}])

    async def fetch(candidate_id):
        return next(answers)

    monkeypatch.setattr(svc, "_get_candidate_by_id_uncached", fetch)

    @jobdiva._invalidates_reads(("candidate", "candidate_id"))
    async def write(candidate_id):
        return True

    async def main():
        first = await svc.get_candidate_by_id("7")
        again = await svc.get_candidate_by_id("7")
        await write("7")
        after = await svc.get_candidate_by_id("7")
        return first, again, after

    assert asyncio.run(main()) == ({"name": "old"}, {"name": "old"}, {"name": "new"})
