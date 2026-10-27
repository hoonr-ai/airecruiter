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
