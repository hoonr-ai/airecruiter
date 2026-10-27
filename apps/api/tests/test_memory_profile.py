import tracemalloc

from core import memory_profile


def test_rss_is_reported():
    assert memory_profile.rss_mb() > 0


def test_growth_lines_name_the_allocating_line():
    tracemalloc.start(1)
    try:
        before = tracemalloc.take_snapshot()
        hoard = [bytearray(1024) for _ in range(500)]  # noqa: F841 - the growth under test
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    top = memory_profile.growth_lines(before, after, 3)
    assert top and top[0].traceback[0].filename == __file__


def test_report_never_raises_without_tracing(monkeypatch):
    monkeypatch.setattr(memory_profile, "_previous", None)
    memory_profile.report()
