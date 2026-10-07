"""Which monitored jobs an AutoSync cycle syncs, and in what order.

AutoSync used to sync every non-archived job (~980) one after another: a
cycle took ~2 h 15 min, so the 15-minute schedule really ran every ~2.5 h,
and closed jobs spent JobDiva quota during launch hours like open ones.

Now each cycle works through a time-boxed slice:

- open jobs first, least recently synced first (never-synced jobs lead), so
  every open job comes round in turn and none starves;
- jobs closed / filled / cancelled in JobDiva (``PAIR_INACTIVE_STATUSES``) at
  most once a day, after the open ones;
- the cycle stops when its budget runs out; the next cycle carries on from
  the oldest again.

``monitored_jobs.last_auto_sync_at`` records progress across cycles and
workers.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Sequence

logger = logging.getLogger(__name__)

# Seconds of work per cycle. The schedule is every 15 min; leave headroom so
# a cycle never overlaps the next one.
CYCLE_BUDGET_S = float(os.getenv("AUTOSYNC_CYCLE_BUDGET_S", "780"))
INACTIVE_RESYNC_HOURS = float(os.getenv("AUTOSYNC_INACTIVE_RESYNC_HOURS", "24"))
DEFAULT_INACTIVE = "closed,filled,cancelled,canceled,expired,ignored,declined"
INACTIVE_STATUSES: List[str] = [
    s.strip() for s in os.getenv("PAIR_INACTIVE_STATUSES", DEFAULT_INACTIVE).lower().split(",") if s.strip()
]

_column_ready = False

PICK_SQL = """
    SELECT job_id, title,
           LOWER(COALESCE(status, '')) = ANY(%(inactive)s) AS inactive
    FROM monitored_jobs
    WHERE is_archived IS NOT TRUE
      AND (processing_status IN ('monitoring_added', 'manual_created')
           OR processing_status LIKE 'step_%%_complete')
      AND (
            LOWER(COALESCE(status, '')) <> ALL(%(inactive)s)
         OR last_auto_sync_at IS NULL
         OR last_auto_sync_at < NOW() - (%(inactive_hours)s * interval '1 hour')
      )
    ORDER BY inactive, last_auto_sync_at NULLS FIRST, job_id
    LIMIT %(limit)s
"""


def ensure_column(conn) -> None:
    """Add last_auto_sync_at once per process. Checked via the catalog first:
    ALTER TABLE takes an exclusive lock even when the column already exists."""
    global _column_ready
    if _column_ready:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'monitored_jobs' AND column_name = 'last_auto_sync_at'"
        )
        if cur.fetchone() is None:
            cur.execute("ALTER TABLE monitored_jobs ADD COLUMN IF NOT EXISTS last_auto_sync_at TIMESTAMPTZ")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_monitored_jobs_last_auto_sync "
                "ON monitored_jobs (last_auto_sync_at NULLS FIRST)"
            )
    conn.commit()
    _column_ready = True


def pick_jobs(conn, limit: int = 2000) -> List[Dict[str, Any]]:
    ensure_column(conn)
    with conn.cursor() as cur:
        cur.execute(PICK_SQL, {
            "inactive": INACTIVE_STATUSES,
            "inactive_hours": INACTIVE_RESYNC_HOURS,
            "limit": limit,
        })
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.commit()
    return rows


def mark_synced(conn, job_ids: Sequence[str]) -> None:
    if not job_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE monitored_jobs SET last_auto_sync_at = NOW() WHERE job_id = ANY(%s)",
            (list(job_ids),),
        )
    conn.commit()
