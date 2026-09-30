import datetime

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
