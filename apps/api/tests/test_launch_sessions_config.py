"""services/launch_sessions/config: the two launch-session switches.

PAIR_SERVER_LAUNCH_ENABLED / PAIR_LAUNCH_RUNNER_ENABLED: an explicit
true / false always wins; unset = ON on QA and local, OFF on PROD (from
core.newrelic.resolve_environment); if that import fails, ON only when
nothing looks like PROD. Creation needs both (final review, rollout #2 / #4).
"""

from __future__ import annotations

import sys

import pytest

from services.launch_sessions import config as cfg

_FLAGS = ("PAIR_SERVER_LAUNCH_ENABLED", "PAIR_LAUNCH_RUNNER_ENABLED")
_ENV_SIGNALS = ("NEW_RELIC_ENVIRONMENT", "APP_ENV", "ENVIRONMENT", "ENV", "APP_BASE_URL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """apps/api/.env (loaded by core.config) may set any of these."""
    for name in _FLAGS + _ENV_SIGNALS:
        monkeypatch.delenv(name, raising=False)


def _both():
    return cfg.server_launch_enabled(), cfg.runner_enabled()


@pytest.mark.parametrize("signal, value, expected", [
    ("NEW_RELIC_ENVIRONMENT", "qa", True),
    ("NEW_RELIC_ENVIRONMENT", "prod", False),
    ("NEW_RELIC_ENVIRONMENT", "local", True),
    ("APP_ENV", "production", False),
    ("ENVIRONMENT", "staging", True),
    ("APP_BASE_URL", "https://pair.pyramidci.com", False),
    ("APP_BASE_URL", "https://pairqa.pyramidci.com", True),
    ("APP_BASE_URL", "http://localhost:3000", True),
    (None, None, True),  # nothing set → local
])
def test_unset_switches_default_on_for_qa_and_local_and_off_for_prod(monkeypatch, signal, value, expected):
    if signal:
        monkeypatch.setenv(signal, value)
    assert _both() == (expected, expected)
    assert cfg.creation_enabled() is expected


@pytest.mark.parametrize("environment", ["prod", "qa", "local"])
@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("ON", True), ("false", False),
                                           ("0", False), ("off", False)])
def test_an_explicit_value_wins_in_every_environment(monkeypatch, environment, raw, expected):
    monkeypatch.setenv("NEW_RELIC_ENVIRONMENT", environment)
    for name in _FLAGS:
        monkeypatch.setenv(name, raw)
    assert _both() == (expected, expected)


def test_a_value_that_is_not_a_boolean_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("PAIR_SERVER_LAUNCH_ENABLED", "maybe")
    monkeypatch.setenv("NEW_RELIC_ENVIRONMENT", "prod")
    assert cfg.server_launch_enabled() is False
    monkeypatch.setenv("NEW_RELIC_ENVIRONMENT", "qa")
    assert cfg.server_launch_enabled() is True


def test_creation_needs_sessions_and_the_runner(monkeypatch):
    monkeypatch.setenv("NEW_RELIC_ENVIRONMENT", "prod")
    monkeypatch.setenv("PAIR_SERVER_LAUNCH_ENABLED", "true")
    assert cfg.server_launch_enabled() is True and cfg.runner_enabled() is False
    assert cfg.creation_enabled() is False  # PROD needs BOTH set
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "true")
    assert cfg.creation_enabled() is True
    monkeypatch.setenv("PAIR_SERVER_LAUNCH_ENABLED", "false")
    assert cfg.creation_enabled() is False


@pytest.mark.parametrize("signal, value, expected", [
    ("NEW_RELIC_ENVIRONMENT", "prod", False),
    ("ENV", "Production", False),
    ("APP_BASE_URL", "https://pair.pyramidci.com/", False),
    ("NEW_RELIC_ENVIRONMENT", "qa", True),
    ("APP_BASE_URL", "https://pairqa.pyramidci.com", True),
    (None, None, True),
])
def test_without_core_newrelic_it_is_on_unless_something_looks_like_prod(monkeypatch, signal, value, expected):
    monkeypatch.setitem(sys.modules, "core.newrelic", None)  # `from core.newrelic import …` raises ImportError
    with pytest.raises(ImportError):
        from core.newrelic import resolve_environment  # noqa: F401
    if signal:
        monkeypatch.setenv(signal, value)
    assert _both() == (expected, expected)
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "true")
    assert cfg.runner_enabled() is True  # explicit still wins


def test_queue_expiry_knob(monkeypatch):
    monkeypatch.delenv("PAIR_LAUNCH_QUEUED_MAX_S", raising=False)
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "false")
    assert cfg.queued_max_s() == 1800   # nothing will claim it: expire soon
    monkeypatch.setenv("PAIR_LAUNCH_RUNNER_ENABLED", "true")
    assert cfg.queued_max_s() == 21600  # a busy queue must not lose it
    monkeypatch.setenv("PAIR_LAUNCH_QUEUED_MAX_S", "0")
    assert cfg.queued_max_s() == 0
    monkeypatch.setenv("PAIR_LAUNCH_QUEUED_MAX_S", "-5")
    assert cfg.queued_max_s() == 0
