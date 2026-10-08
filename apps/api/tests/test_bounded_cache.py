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
