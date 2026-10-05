import datetime

import pytest
from botocore.exceptions import ClientError

from services import call_recordings as cr


def test_prefix_uses_utc_day_jobdiva_and_shard():
    prefixes = cr._session_prefixes(60748, "26-29759", "2026-09-27T23:30:00-05:00", None)
    # 23:30 at UTC-5 is already the 28th in UTC.
    assert prefixes == ["recordings/2026-09-28/26-29759/0060/60748"]


def test_prefix_without_jobdiva_or_start():
    assert cr._session_prefixes(5, None, None, None) == ["recordings/unknown-date/no-jobdiva/0000/5"]


def test_prefix_sanitises_jobdiva_id():
    assert "26-29_759" in cr._session_prefixes(1, "26-29/759", "2026-01-01T00:00:00Z", None)[0]


def test_prefix_fallback_days():
    got = cr._session_prefixes(1, "1", None, datetime.date(2026, 1, 10))
    assert got[0].startswith("recordings/unknown-date/")
    assert len(got) == 6 and "recordings/2026-01-09/1/0000/1" in got


class _Paginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}


class _S3:
    def __init__(self, keys):
        self.keys = keys

    def get_paginator(self, _):
        return _Paginator(self.keys)


def test_session_607_does_not_match_6074():
    base = "recordings/2026-01-01/j/0000/"
    s3 = _S3([base + "607_a.ogg", base + "6074_b.ogg", base + "607.ogg", base + "607_a.txt"])
    got = cr._list_session_objects(s3, "b", base + "607", 607)
    assert [o["Key"] for o in got] == [base + "607_a.ogg", base + "607.ogg"]


# ---------------------------------------------------------------------------
# find_recordings: error handling + listing cache
# ---------------------------------------------------------------------------
class _ObjectPaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):
        yield {
            "Contents": [
                {"Key": k, "Size": 123, "LastModified": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)}
                for k in self.keys
                if k.startswith(Prefix)
            ]
        }


class _FakeS3Client:
    """Stands in for the lazily-imported boto3 client: it both lists (via
    `_list_session_objects`, which only calls `get_paginator`) and presigns."""

    def __init__(self, keys, presign_error=None, list_error=None):
        self._keys = keys
        self._presign_error = presign_error
        self._list_error = list_error
        self.list_calls = 0
        self.presign_calls = 0

    def get_paginator(self, _name):
        self.list_calls += 1
        if self._list_error:
            raise self._list_error
        return _ObjectPaginator(self._keys)

    def generate_presigned_url(self, *_args, **_kwargs):
        self.presign_calls += 1
        if self._presign_error:
            raise self._presign_error
        return "https://example.com/signed"


@pytest.fixture(autouse=True)
def _clear_listing_cache():
    cr._listing_cache.clear()
    yield
    cr._listing_cache.clear()


def test_find_recordings_reports_unavailable_on_s3_error(monkeypatch):
    fake = _FakeS3Client(
        keys=[],
        list_error=ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2"),
    )
    monkeypatch.setattr(cr, "_s3", lambda: fake)
    monkeypatch.setattr(cr, "_bucket", lambda: "bucket")

    result = cr.find_recordings([{"id": 1, "started_at": "2026-01-01T00:00:00Z"}], "job-1", None)

    assert result == {"recordings": [], "unavailable": True}


def test_find_recordings_reports_unavailable_on_presign_error(monkeypatch):
    base = "recordings/2026-01-01/job-1/0000/"
    fake = _FakeS3Client(
        keys=[base + "1.ogg"],
        presign_error=ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "GetObject"),
    )
    monkeypatch.setattr(cr, "_s3", lambda: fake)
    monkeypatch.setattr(cr, "_bucket", lambda: "bucket")

    result = cr.find_recordings([{"id": 1, "started_at": "2026-01-01T00:00:00Z"}], "job-1", None)

    assert result == {"recordings": [], "unavailable": True}


def test_find_recordings_caches_listing_within_ttl(monkeypatch):
    base = "recordings/2026-01-01/job-1/0000/"
    fake = _FakeS3Client(keys=[base + "1.ogg"])
    monkeypatch.setattr(cr, "_s3", lambda: fake)
    monkeypatch.setattr(cr, "_bucket", lambda: "bucket")

    sessions = [{"id": 1, "started_at": "2026-01-01T00:00:00Z"}]
    first = cr.find_recordings(sessions, "job-1", None)
    second = cr.find_recordings(sessions, "job-1", None)

    assert first["recordings"] and second["recordings"]
    # The S3 listing itself is cached for LISTING_CACHE_SECONDS; presigning
    # still runs fresh each call since URLs are short-lived.
    assert fake.list_calls == 1
    assert fake.presign_calls == 2


def test_find_recordings_cache_expires(monkeypatch):
    base = "recordings/2026-01-01/job-1/0000/"
    fake = _FakeS3Client(keys=[base + "1.ogg"])
    monkeypatch.setattr(cr, "_s3", lambda: fake)
    monkeypatch.setattr(cr, "_bucket", lambda: "bucket")

    clock = {"t": 0.0}
    monkeypatch.setattr(cr.time, "monotonic", lambda: clock["t"])

    sessions = [{"id": 1, "started_at": "2026-01-01T00:00:00Z"}]
    cr.find_recordings(sessions, "job-1", None)
    clock["t"] += cr.LISTING_CACHE_SECONDS + 1
    cr.find_recordings(sessions, "job-1", None)

    assert fake.list_calls == 2
