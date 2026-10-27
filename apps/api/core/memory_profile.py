"""Per-worker memory telemetry, and opt-in tracemalloc growth reports.

Workers grew to 2.2 GB within 43 minutes with no record of what was
allocating. ``report()`` (scheduled from main.py) always records RSS as
Custom/Worker/rss_mb — the per-worker growth curve. With
``PAIR_TRACEMALLOC_FRAMES`` > 0 it also logs which source lines grew most
since the last report (MEMORY_GROWTH lines). Tracing costs CPU and memory of
its own, so turn it on for one deploy or one worker, not permanently; for a
native-allocation view, run a worker under ``memray run`` instead.
"""
from __future__ import annotations

import logging
import os
import sys
import tracemalloc
from typing import Optional

log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


TRACE_FRAMES = _env_int("PAIR_TRACEMALLOC_FRAMES", 0)
TOP_N = _env_int("PAIR_TRACEMALLOC_TOP", 15)
REPORT_MINUTES = max(1, _env_int("PAIR_MEMORY_REPORT_MINUTES", 5))

_previous: Optional[tracemalloc.Snapshot] = None


def rss_mb() -> Optional[float]:
    """Current resident set size (Linux), else peak RSS; None if unknown."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0
    except Exception:  # noqa: BLE001
        return None


def start_if_enabled() -> bool:
    if TRACE_FRAMES <= 0 or tracemalloc.is_tracing():
        return tracemalloc.is_tracing()
    tracemalloc.start(TRACE_FRAMES)
    log.warning("tracemalloc on (%d frames) in pid %d: memory growth is logged every %d min",
                TRACE_FRAMES, os.getpid(), REPORT_MINUTES)
    return True


def growth_lines(old: tracemalloc.Snapshot, new: tracemalloc.Snapshot, top: int) -> list:
    """The ``top`` source lines that grew most between two snapshots."""
    skip = (tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, "<frozen importlib._bootstrap>"))
    stats = new.filter_traces(skip).compare_to(old.filter_traces(skip), "lineno")
    return [s for s in stats if s.size_diff > 0][:top]


def report() -> None:
    """Record RSS; when tracing, log the top growth since the last call. Never raises."""
    global _previous
    try:
        rss = rss_mb()
        if rss is not None:
            try:
                from core.newrelic import record_custom_metric

                record_custom_metric("Custom/Worker/rss_mb", rss)
            except Exception:  # noqa: BLE001
                pass
        if not tracemalloc.is_tracing():
            return
        snap = tracemalloc.take_snapshot()
        current, peak = tracemalloc.get_traced_memory()
        log.warning("MEMORY pid=%d rss_mb=%s traced_mb=%.1f traced_peak_mb=%.1f",
                    os.getpid(), f"{rss:.0f}" if rss is not None else "?", current / 2**20, peak / 2**20)
        if _previous is not None:
            for stat in growth_lines(_previous, snap, TOP_N):
                frame = stat.traceback[0]
                log.warning("MEMORY_GROWTH pid=%d +%.1f KiB (%+d blocks) %s:%d",
                            os.getpid(), stat.size_diff / 1024, stat.count_diff, frame.filename, frame.lineno)
        _previous = snap
    except Exception as exc:  # noqa: BLE001
        log.debug("memory report failed: %s", exc)
