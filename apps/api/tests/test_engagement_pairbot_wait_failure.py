"""`_confirm_failed_candidates_with_pairbot` (routers/engagement.py).

When `_wait_for_pairbot_creation` errors out — a 600s timeout, a dropped SSE
stream, or any other wait failure — PairBot has already accepted the bulk
with a 202 and may still be creating interviews in the background. The
caller must NOT blindly stamp every candidate `failed` on that error: a
later retry on a false failure would create a duplicate bulk for candidates
PairBot already has interviews for. This checks PairBot's own record first,
and only candidates PairBot itself confirms have no interview (once it
reports the bulk is no longer in progress) come back as real failures.
"""
import asyncio
from unittest.mock import patch

from routers import engagement


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for `httpx.AsyncClient(...)`. Instances are callable so the
    same object can be passed where `httpx.AsyncClient` (a class) is used."""

    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc

    def __call__(self, *_args, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, *_args, **_kwargs):
        if self._exc:
            raise self._exc
        return self._response


def _run(bulk_id="bulk-1", job_id="job-1", candidate_ids=("c1", "c2")):
    return asyncio.run(
        engagement._confirm_failed_candidates_with_pairbot(bulk_id, job_id, list(candidate_ids))
    )


def test_no_job_id_returns_empty():
    """Can't check PairBot without a job id — must not guess a failure."""
    assert _run(job_id="") == []


def test_no_candidate_ids_returns_empty():
    assert _run(candidate_ids=()) == []


def test_bulk_still_in_progress_confirms_nothing():
    fake = _FakeAsyncClient(_FakeResponse(200, {"data": {"bulk_in_progress": True, "interviews": []}}))
    with patch("routers.engagement.httpx.AsyncClient", fake):
        assert _run() == []


def test_bulk_done_confirms_only_missing_candidates():
    fake = _FakeAsyncClient(
        _FakeResponse(
            200,
            {
                "data": {
                    "bulk_in_progress": False,
                    "interviews": [{"source_candidate_id": "c1"}],
                }
            },
        )
    )
    with patch("routers.engagement.httpx.AsyncClient", fake):
        result = _run(candidate_ids=("c1", "c2"))
    # c1 has a confirmed interview; only c2 comes back as a real failure.
    assert result == ["c2"]


def test_bulk_done_with_no_interviews_confirms_all_as_failed():
    fake = _FakeAsyncClient(
        _FakeResponse(200, {"data": {"bulk_in_progress": False, "interviews": []}})
    )
    with patch("routers.engagement.httpx.AsyncClient", fake):
        result = _run(candidate_ids=("c1", "c2"))
    assert result == ["c1", "c2"]


def test_non_200_status_confirms_nothing():
    """A broken status check must not be read as 'nothing was created'."""
    fake = _FakeAsyncClient(_FakeResponse(500))
    with patch("routers.engagement.httpx.AsyncClient", fake):
        assert _run() == []


def test_request_exception_confirms_nothing():
    fake = _FakeAsyncClient(exc=RuntimeError("network down"))
    with patch("routers.engagement.httpx.AsyncClient", fake):
        assert _run() == []
