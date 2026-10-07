import logging

from core.logging import RepeatedWarningSampler


def _rec(msg, level=logging.WARNING, name="svc"):
    return logging.LogRecord(name, level, __file__, 1, msg, None, None)


def test_keeps_burst_then_drops_similar_warnings():
    s = RepeatedWarningSampler(burst=3, window_s=60)
    kept = [s.filter(_rec(f"Apollo non-2xx for {i}: 429")) for i in range(10)]
    assert kept == [True] * 3 + [False] * 7


def test_different_messages_and_loggers_are_independent():
    s = RepeatedWarningSampler(burst=1, window_s=60)
    assert s.filter(_rec("a 1"))
    assert s.filter(_rec("b 1"))
    assert s.filter(_rec("a 1", name="other"))
    assert not s.filter(_rec("a 2"))


def test_errors_and_info_are_never_sampled():
    s = RepeatedWarningSampler(burst=1, window_s=60)
    assert all(s.filter(_rec("boom 1", logging.ERROR)) for _ in range(5))
    assert all(s.filter(_rec("hi 1", logging.INFO)) for _ in range(5))


def test_next_window_reports_suppressed_count(monkeypatch):
    import core.logging as cl

    now = [1000.0]
    monkeypatch.setattr(cl.time, "monotonic", lambda: now[0])
    s = RepeatedWarningSampler(burst=1, window_s=60)
    for i in range(4):
        s.filter(_rec(f"x {i}"))
    now[0] += 61
    r = _rec("x 9")
    assert s.filter(r)
    assert "+3 similar warnings suppressed" in r.getMessage()


def test_one_decision_per_record_across_handlers():
    s = RepeatedWarningSampler(burst=1, window_s=60)
    r = _rec("dup 1")
    assert s.filter(r) and s.filter(r)  # second handler sees the same verdict
    assert not s.filter(_rec("dup 2"))
