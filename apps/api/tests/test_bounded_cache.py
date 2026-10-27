from core.bounded_cache import BoundedDict


def test_evicts_least_recently_used():
    d = BoundedDict(2)
    d["a"] = 1
    d["b"] = 2
    assert d["a"] == 1  # touch a
    d["c"] = 3
    assert "b" not in d and "a" in d and "c" in d
    assert len(d) == 2


def test_get_and_overwrite_refresh_recency():
    d = BoundedDict(2)
    d["a"], d["b"] = 1, 2
    assert d.get("a") == 1
    assert d.get("zz", "x") == "x"
    d["b"] = 20
    d["c"] = 3
    assert "a" not in d and d["b"] == 20


def test_none_values_are_cached():
    d = BoundedDict(3)
    d["miss"] = None
    assert "miss" in d and d["miss"] is None


def test_module_caches_are_bounded():
    from services import gender_logic, location

    assert isinstance(location._GEOCODE_CACHE, BoundedDict)
    assert isinstance(gender_logic._AI_NAME_CACHE, BoundedDict)


# ---------------------------------------------------------------------------
# AsyncTTLCache (services.jobdiva's brief read cache)
# ---------------------------------------------------------------------------
import asyncio

from core.bounded_cache import AsyncTTLCache


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_ttl_cache_serves_hits_until_they_expire():
    clock = _Clock()
    cache = AsyncTTLCache(maxsize=10, ttl_s=30, clock=clock)
    calls = []

    async def load():
        calls.append(1)
        return {"id": len(calls)}

    async def main():
        a = await cache.get_or_load("k", load)
        clock.t = 29
        b = await cache.get_or_load("k", load)
        clock.t = 31
        c = await cache.get_or_load("k", load)
        return a, b, c

    a, b, c = asyncio.run(main())
    assert a == b == {"id": 1} and c == {"id": 2}


def test_ttl_cache_never_stores_a_failure():
    cache = AsyncTTLCache(maxsize=10, ttl_s=30, clock=_Clock())
    results = iter([None, {"ok": True}])

    async def load():
        return next(results)

    async def main():
        return await cache.get_or_load("k", load), await cache.get_or_load("k", load)

    assert asyncio.run(main()) == (None, {"ok": True})


def test_ttl_cache_shares_one_load_between_concurrent_callers():
    cache = AsyncTTLCache(maxsize=10, ttl_s=30)
    calls = []

    async def load():
        calls.append(1)
        await asyncio.sleep(0.01)
        return [1, 2]

    async def main():
        return await asyncio.gather(*(cache.get_or_load("k", load) for _ in range(5)))

    assert asyncio.run(main()) == [[1, 2]] * 5
    assert len(calls) == 1


def test_ttl_cache_hits_are_copies_and_invalidate_drops_them():
    cache = AsyncTTLCache(maxsize=10, ttl_s=30, clock=_Clock())
    calls = []

    async def load():
        calls.append(1)
        return {"tags": ["a"]}

    async def main():
        first = await cache.get_or_load("k", load)
        first["tags"].append("mutated")
        second = await cache.get_or_load("k", load)
        cache.invalidate("k")
        await cache.get_or_load("k", load)
        return second

    assert asyncio.run(main()) == {"tags": ["a"]}
    assert len(calls) == 2


def test_ttl_cache_off_when_ttl_is_zero():
    cache = AsyncTTLCache(maxsize=10, ttl_s=0)
    calls = []

    async def load():
        calls.append(1)
        return 1

    async def main():
        await cache.get_or_load("k", load)
        await cache.get_or_load("k", load)

    asyncio.run(main())
    assert len(calls) == 2


def test_ttl_cache_hit_needs_the_current_token():
    from core.bounded_cache import BYPASS

    gen = {"v": 0}
    calls = []
    cache = AsyncTTLCache(maxsize=10, ttl_s=30, clock=_Clock())

    async def token():
        return gen["v"]

    async def load():
        calls.append(1)
        return {"n": len(calls)}

    async def main():
        a = await cache.get_or_load("k", load, token_fn=token)
        b = await cache.get_or_load("k", load, token_fn=token)
        gen["v"] += 1  # a write somewhere (another worker)
        c = await cache.get_or_load("k", load, token_fn=token)
        return a, b, c

    a, b, c = asyncio.run(main())
    assert a == b == {"n": 1} and c == {"n": 2}


def test_ttl_cache_does_not_store_a_load_a_write_overtook():
    gen = {"v": 0}
    calls = []
    cache = AsyncTTLCache(maxsize=10, ttl_s=30, clock=_Clock())

    async def token():
        return gen["v"]

    async def load():
        calls.append(1)
        if len(calls) == 1:
            gen["v"] += 1  # the write lands while the first read is in flight
        return {"n": len(calls)}

    async def main():
        await cache.get_or_load("k", load, token_fn=token)
        return await cache.get_or_load("k", load, token_fn=token)

    assert asyncio.run(main()) == {"n": 2}  # the pre-write value wasn't kept


def test_ttl_cache_bypass_token_skips_the_cache():
    from core.bounded_cache import BYPASS

    calls = []
    cache = AsyncTTLCache(maxsize=10, ttl_s=30)

    async def token():
        return BYPASS

    async def load():
        calls.append(1)
        return 1

    async def main():
        await cache.get_or_load("k", load, token_fn=token)
        await cache.get_or_load("k", load, token_fn=token)

    asyncio.run(main())
    assert len(calls) == 2
