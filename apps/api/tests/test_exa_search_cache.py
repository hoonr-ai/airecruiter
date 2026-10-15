"""Redis cache of Exa people-search results (services/exa_search_cache.py, 2026-10-15).

Pass A bought the same Exa searches again for the sample run, the full run,
relaxation attempts and resumed launch sessions (~4 runs per job on PROD).
Each query's results are now cached in Redis, encrypted, for
EXA_SEARCH_CACHE_TTL_S.

Pins:
  - the key ignores num_results and changes with the query, domains (order
    free), environment and contents spec
  - a round trip keeps the five parsed fields and their order (text as a
    str or a list); a smaller request is a sliced hit, a larger one a miss
  - put never shrinks an entry, skips empty / oversize answers, gives a thin
    answer the 30-minute TTL, and stores an encrypted value
  - an entry from another key is a miss; TTL 0 is ``off``, no REDIS_URL /
    no ENCRYPTION_KEY is ``off_config`` (one WARNING), no client is ``down``
  - a ResponseError (OOM) is a miss that never marks Redis down; a
    connection error opens this module's down window and leaves the JobDiva
    limiter's untouched; get / put never raise
"""
import asyncio
import logging
import types

import pytest
from redis import exceptions as redis_exc

from core import config as core_config
from core import newrelic as nr
from services import exa_search_cache as cache
from services import jobdiva_rate_limit as rl

CONTENTS = {"text": {"max_characters": 1000}, "highlights": {"max_characters": 4000}}


class FakeRedis:
    """The slice of redis.asyncio (decode_responses=True) the cache uses.
    ``fail`` maps a method name to the exception it raises."""

    def __init__(self):
        self.store = {}
        self.ttls = {}
        self.calls = []
        self.fail = {}

    def _maybe_fail(self, op):
        self.calls.append(op)
        exc = self.fail.get(op)
        if exc is not None:
            raise exc

    async def get(self, key):
        self._maybe_fail("get")
        return self.store.get(key)

    async def getrange(self, key, start, end):
        self._maybe_fail("getrange")
        return (self.store.get(key) or "")[start:end + 1]

    async def set(self, key, value, ex=None):
        self._maybe_fail("set")
        assert isinstance(value, str)
        self.store[key] = value
        self.ttls[key] = ex
        return True


@pytest.fixture(autouse=True)
def _default_ttl(monkeypatch):
    # core.config loads apps/api/.env: pin the shipped default.
    monkeypatch.setattr(core_config, "EXA_SEARCH_CACHE_TTL_S", 43200, raising=False)


@pytest.fixture
def redis_on(monkeypatch):
    """Cache on: a fake Redis behind jobdiva_rate_limit._get_redis."""
    fake = FakeRedis()
    monkeypatch.setattr(core_config, "REDIS_URL", "redis://fake:6379/0", raising=False)
    monkeypatch.setattr(core_config, "EXA_SEARCH_CACHE_TTL_S", 43200, raising=False)
    monkeypatch.setattr(rl, "_get_redis", lambda: fake)
    cache.reset_for_tests()
    return fake


def _res(slug, text="Denver, CO\nData Engineer", highlights=("did python",), **extra):
    return types.SimpleNamespace(
        id=f"id-{slug}", url=f"https://www.linkedin.com/in/{slug}", title=f"{slug} | LinkedIn",
        text=text, highlights=list(highlights) if highlights is not None else None,
        score=0.9, author=None, **extra,
    )


def _key(q="Data Engineer in Denver", **kw):
    kw.setdefault("category", "people")
    kw.setdefault("search_type", "auto")
    kw.setdefault("contents", CONTENTS)
    return cache.cache_key(q, **kw)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------

def test_key_ignores_n_and_changes_with_query_domains_env_and_contents(monkeypatch):
    monkeypatch.setattr(nr, "environment", lambda: "local")
    base = _key()
    assert base.startswith("exa:ppl:v1:local:")
    assert _key() == base
    assert _key(q="Data Analyst in Denver") != base
    assert _key(include_domains=["dice.com"]) != base
    assert _key(include_domains=["b.com", "a.com"]) == _key(include_domains=["a.com", "b.com"])
    assert _key(contents={"text": {"max_characters": 2000}, "highlights": {"max_characters": 4000}}) != base
    assert _key(search_type="neural") != base

    monkeypatch.setattr(nr, "environment", lambda: "prod")
    prod = _key()
    assert prod.startswith("exa:ppl:v1:prod:") and prod.split(":")[-1] == base.split(":")[-1]


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_round_trip_keeps_the_parsed_fields_and_order(redis_on):
    rows = [_res("alice"), _res("bob", text=["Austin, TX", "Engineer"]), _res("carol", highlights=None)]
    rows[2].title = None

    assert _run(cache.put(_key(), rows, 34)) is True
    got = _run(cache.get(_key(), 34))

    assert [r.id for r in got] == ["id-alice", "id-bob", "id-carol"]
    assert got[0].url == rows[0].url and got[0].text == rows[0].text and got[0].highlights == ["did python"]
    assert got[1].text == ["Austin, TX", "Engineer"]
    assert got[2].highlights is None and got[2].title is None
    assert not hasattr(got[0], "score")  # only the five parsed fields are kept


def test_a_field_the_result_lacks_stays_missing(redis_on):
    bare = types.SimpleNamespace(id="x", url="https://www.linkedin.com/in/x", title="X")
    _run(cache.put(_key(), [bare] * 12, 12))
    [got, *_] = _run(cache.get(_key(), 12))
    assert not hasattr(got, "text") and not hasattr(got, "highlights")


def test_a_smaller_request_is_a_sliced_hit_and_a_larger_one_a_miss(redis_on):
    rows = [_res(f"p{i}") for i in range(34)]
    _run(cache.put(_key(), rows, 34))

    assert [r.id for r in _run(cache.get(_key(), 12))] == [f"id-p{i}" for i in range(12)]
    assert len(_run(cache.get(_key(), 34))) == 34
    assert _run(cache.get(_key(), 50)) is None


def test_an_entry_shorter_than_asked_still_serves_its_size(redis_on):
    # Exa had only 15 for a 34 ask: a later 34 (or 20) ask gets the same 15.
    _run(cache.put(_key(), [_res(f"p{i}") for i in range(15)], 34))
    assert len(_run(cache.get(_key(), 34))) == 15
    assert len(_run(cache.get(_key(), 20))) == 15


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def test_put_never_shrinks_an_entry(redis_on):
    _run(cache.put(_key(), [_res(f"p{i}") for i in range(50)], 50))
    assert _run(cache.put(_key(), [_res("other")] * 34, 34)) is False
    assert _run(cache.put(_key(), [_res("other")] * 50, 50)) is False
    assert len(_run(cache.get(_key(), 50))) == 50

    small = _key(q="Data Analyst in Denver")
    _run(cache.put(small, [_res(f"s{i}") for i in range(34)], 34))
    assert _run(cache.put(small, [_res(f"b{i}") for i in range(50)], 50)) is True  # a bigger ask replaces
    assert _run(cache.get(small, 50))[0].id == "id-b0"


@pytest.mark.parametrize("rows, n_req, ttl", [
    (3, 34, cache.THIN_TTL_S),   # thin: fewer than 10
    (9, 50, cache.THIN_TTL_S),
    (10, 34, 43200),
    (5, 5, 43200),               # asked for 5, got 5: full
])
def test_a_thin_answer_gets_the_short_ttl(redis_on, rows, n_req, ttl):
    _run(cache.put(_key(), [_res(f"p{i}") for i in range(rows)], n_req))
    assert redis_on.ttls[_key()] == ttl


def test_a_ttl_below_the_thin_ttl_is_kept(redis_on, monkeypatch):
    monkeypatch.setattr(core_config, "EXA_SEARCH_CACHE_TTL_S", 600)
    _run(cache.put(_key(), [_res("p")], 34))
    assert redis_on.ttls[_key()] == 600


def test_empty_and_oversize_answers_are_not_stored(redis_on, monkeypatch):
    assert _run(cache.put(_key(), [], 34)) is False
    monkeypatch.setattr(cache, "MAX_VALUE_BYTES", 200)
    assert _run(cache.put(_key(), [_res(f"p{i}") for i in range(20)], 34)) is False
    assert redis_on.store == {}


def test_the_value_is_encrypted_and_another_key_is_a_miss(redis_on, monkeypatch):
    _run(cache.put(_key(), [_res("alice", highlights=("Secret résumé line",))] * 12, 34))
    stored = redis_on.store[_key()]
    assert stored.startswith("34.")
    assert "Secret" not in stored and "alice" not in stored

    monkeypatch.setattr(core_config, "ENCRYPTION_KEY", "a-different-key", raising=False)
    cache.reset_for_tests()
    assert _run(cache.get(_key(), 34)) is None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def test_ttl_zero_is_off_and_touches_nothing(redis_on, monkeypatch):
    monkeypatch.setattr(core_config, "EXA_SEARCH_CACHE_TTL_S", 0)
    assert cache.state() == "off"
    assert _run(cache.put(_key(), [_res("p")] * 12, 12)) is False
    assert _run(cache.get(_key(), 12)) is None
    assert redis_on.calls == []


def test_no_redis_url_is_off_config_with_one_warning(monkeypatch, caplog):
    cache.reset_for_tests()
    caplog.set_level(logging.WARNING, logger=cache.__name__)
    assert cache.state() == "off_config"
    assert cache.state() == "off_config"
    assert _run(cache.get(_key(), 12)) is None
    assert len([r for r in caplog.records if "OFF" in r.getMessage()]) == 1


def test_no_encryption_key_is_off_config(redis_on, monkeypatch):
    monkeypatch.setattr(core_config, "ENCRYPTION_KEY", "", raising=False)
    cache.reset_for_tests()
    assert cache.state() == "off_config"
    assert _run(cache.put(_key(), [_res("p")] * 12, 12)) is False
    assert redis_on.calls == []


def test_no_client_while_the_url_is_set_is_down(redis_on, monkeypatch):
    monkeypatch.setattr(rl, "_get_redis", lambda: None)
    assert cache.state() == "down"
    assert _run(cache.get(_key(), 12)) is None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_oom_is_a_miss_that_never_marks_redis_down(redis_on):
    jobdiva_down = rl._redis_down_until
    redis_on.fail["set"] = redis_exc.ResponseError("OOM command not allowed when used memory > 'maxmemory'")

    assert _run(cache.put(_key(), [_res("p")] * 12, 12)) is False
    assert cache.state() == "on" and cache._down_until == 0.0
    assert rl._redis_down_until == jobdiva_down


@pytest.mark.parametrize("error", [
    redis_exc.ConnectionError("Connection refused"),
    redis_exc.TimeoutError("Timeout reading from socket"),
    OSError("network unreachable"),
])
def test_a_connection_error_opens_this_caches_own_down_window(redis_on, error):
    jobdiva_down = rl._redis_down_until
    redis_on.fail["get"] = error

    assert _run(cache.get(_key(), 12)) is None
    assert cache.state() == "down"
    assert rl._redis_down_until == jobdiva_down  # the JobDiva limiter keeps Redis
    assert _run(cache.put(_key(), [_res("p")] * 12, 12)) is False
    assert redis_on.calls == ["get"]  # nothing more while down


def test_get_and_put_never_raise(redis_on, monkeypatch):
    redis_on.fail["get"] = RuntimeError("boom")
    redis_on.fail["getrange"] = RuntimeError("boom")
    assert _run(cache.get(_key(), 12)) is None
    assert _run(cache.put(_key(), [_res("p")] * 12, 12)) is False

    redis_on.fail.clear()
    redis_on.store[_key()] = "34.not-a-token"
    assert _run(cache.get(_key(), 12)) is None

    monkeypatch.setattr(cache, "_encode", lambda *a: (_ for _ in ()).throw(ValueError("bad")))
    assert _run(cache.put(_key(q="other"), [_res("p")] * 12, 12)) is False
