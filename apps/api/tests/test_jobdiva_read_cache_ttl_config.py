"""JOBDIVA_READ_CACHE_TTL_S parsing (PR #870 review): float() happily parses
"-5" and "nan" without raising, so the except-ValueError fallback alone
doesn't catch either — a negative value defeats AsyncTTLCache's own
``ttl_s <= 0`` disable check (it only special-cases literal 0-or-below, which
holds for a genuine negative too, but NaN comparisons are always False and
would silently leave the cache enabled with an undefined TTL).
"""
import importlib

import core.config as config


def _reload_with_env(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("JOBDIVA_READ_CACHE_TTL_S", raising=False)
    else:
        monkeypatch.setenv("JOBDIVA_READ_CACHE_TTL_S", value)
    importlib.reload(config)
    return config.JOBDIVA_READ_CACHE_TTL_S


def test_negative_value_falls_back_to_default(monkeypatch):
    assert _reload_with_env(monkeypatch, "-5") == 30.0


def test_nan_value_falls_back_to_default(monkeypatch):
    assert _reload_with_env(monkeypatch, "nan") == 30.0


def test_unparseable_value_falls_back_to_default(monkeypatch):
    assert _reload_with_env(monkeypatch, "garbage") == 30.0


def test_zero_is_kept_as_the_documented_disable_value(monkeypatch):
    assert _reload_with_env(monkeypatch, "0") == 0.0


def test_positive_value_is_kept(monkeypatch):
    assert _reload_with_env(monkeypatch, "45") == 45.0


def test_unset_uses_the_default(monkeypatch):
    assert _reload_with_env(monkeypatch, None) == 30.0
