"""Platform sessions: who signed in, when, from what device, for how long.

  user_sessions   one row per signed-in browser (shared by its tabs).
                  started_at   first authenticated load (login time)
                  last_seen_at last ping: every PING_INTERVAL while someone is
                               using a tab, and once more when a tab closes
                  ended_at     set only by Sign Out (end_reason 'logout')
                  active_ms    time a tab was visible and in use

PAIR signs in through Microsoft Entra in the browser (MSAL), so the server
never sees a login form; the browser reports its session
(apps/web/lib/user-session.ts). It mints the session id itself, so login and
every later ping are one idempotent upsert — no start round trip.

Cost: all non-GET /api/ calls from one office IP share nginx's 30 writes/min
(nginx-upstreams.conf api_write_limit), so pings are rare (one per browser per
5 min, only while in use) and a page reload resumes the session instead of
starting a new one. A browser that went quiet is never closed by a write: the
read treats a session with no ping for STALE_AFTER as ended at its last ping.

Telemetry: the endpoints degrade to 200 {"status": "error"}, never a 500.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

PING_INTERVAL = datetime.timedelta(minutes=5)   # apps/web/lib/user-session.ts PING_MS
STALE_AFTER = datetime.timedelta(minutes=15)    # three missed pings
MAX_ACTIVE_MS_PER_PING = 15 * 60 * 1000

# Run by routers/jobs._ensure_monitored_jobs_schema at startup.
SCHEMA_STATEMENTS: Tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS user_sessions (
        id UUID PRIMARY KEY,
        user_email TEXT NOT NULL,
        user_name TEXT,
        role TEXT,
        team_name TEXT,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ended_at TIMESTAMPTZ,
        end_reason TEXT,
        active_ms BIGINT NOT NULL DEFAULT 0,
        ip_address TEXT,
        user_agent TEXT,
        device_type TEXT,
        os TEXT,
        browser TEXT,
        screen TEXT,
        timezone TEXT,
        language TEXT,
        entry_path TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_sessions_started ON user_sessions (started_at)",
    "CREATE INDEX IF NOT EXISTS idx_user_sessions_user_started ON user_sessions (user_email, started_at)",
)

_DEVICE_RULES = (
    (re.compile(r"iPad|Tablet"), "tablet"),
    (re.compile(r"Mobi|iPhone|Android"), "mobile"),
)
_OS_RULES = tuple((re.compile(p), n) for p, n in (
    (r"Windows NT", "Windows"), (r"iPhone|iPad|iPod", "iOS"), (r"Mac OS X|Macintosh", "macOS"),
    (r"Android", "Android"), (r"CrOS", "ChromeOS"), (r"Linux", "Linux"),
))
_BROWSER_RULES = tuple((re.compile(p), n) for p, n in (
    (r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/|FxiOS", "Firefox"),
    (r"Chrome/|CriOS", "Chrome"), (r"Safari/", "Safari"),
))


def parse_user_agent(ua: str) -> Dict[str, str]:
    """Coarse device / OS / browser from a User-Agent (no dependency)."""
    s = ua or ""
    device = "desktop" if s else "unknown"
    if "Android" in s and "Mobile" not in s:
        device = "tablet"
    else:
        for rx, name in _DEVICE_RULES:
            if rx.search(s):
                device = name
                break
    os_name = next((n for rx, n in _OS_RULES if rx.search(s)), "Other")
    browser = next((n for rx, n in _BROWSER_RULES if rx.search(s)), "Other")
    return {"device_type": device, "os": os_name, "browser": browser}


def _clip(value: Any, n: int) -> Optional[str]:
    v = str(value or "").strip()
    return v[:n] or None


def _clamp_ms(value: Any) -> int:
    try:
        return max(0, min(int(value or 0), MAX_ACTIVE_MS_PER_PING))
    except (TypeError, ValueError):
        return 0


# First ping inserts the session (login); later ones bump last_seen and
# active time. Another user's id, or a signed-out session, is left alone.
_PING_SQL = """
INSERT INTO user_sessions (id, user_email, user_name, role, team_name, active_ms, ip_address,
                           user_agent, device_type, os, browser, screen, timezone, language, entry_path)
VALUES (%(id)s, %(email)s, %(name)s, %(role)s, %(team)s, %(ms)s, %(ip)s,
        %(ua)s, %(device)s, %(os)s, %(browser)s, %(screen)s, %(tz)s, %(lang)s, %(path)s)
ON CONFLICT (id) DO UPDATE
   SET last_seen_at = NOW(),
       active_ms = user_sessions.active_ms + EXCLUDED.active_ms,
       ended_at = CASE WHEN %(end)s::text IS NULL THEN NULL ELSE NOW() END,
       end_reason = %(end)s
 WHERE user_sessions.user_email = EXCLUDED.user_email
   AND user_sessions.ended_at IS NULL
"""


def ping(conn, *, session_id: str, email: str, active_ms: Any, end_reason: Optional[str] = None,
         user_name: Any = None, role: Any = None, team_name: Any = None, ip: Any = None,
         user_agent: Any = None, screen: Any = None, timezone: Any = None,
         language: Any = None, entry_path: Any = None) -> bool:
    """Upsert one session ping (committed). Raises on DB errors."""
    ua = _clip(user_agent, 500)
    dev = parse_user_agent(ua or "")
    with conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '3000ms'")
        cur.execute(_PING_SQL, {
            "id": session_id, "email": email, "name": _clip(user_name, 200), "role": _clip(role, 40),
            "team": _clip(team_name, 200), "ms": _clamp_ms(active_ms), "ip": _clip(ip, 64), "ua": ua,
            "device": dev["device_type"], "os": dev["os"], "browser": dev["browser"],
            "screen": _clip(screen, 20), "tz": _clip(timezone, 64), "lang": _clip(language, 20),
            "path": _clip(entry_path, 300), "end": end_reason,
        })
        ok = cur.rowcount > 0
    conn.commit()
    return ok


# Logout time is the Sign Out, else (gone quiet) the last ping.
_FETCH_SQL = """
SELECT id::text, user_email, user_name, role, team_name, started_at, last_seen_at,
       COALESCE(ended_at, CASE WHEN last_seen_at < NOW() - %(stale)s THEN last_seen_at END) AS logout_at,
       COALESCE(end_reason, CASE WHEN last_seen_at < NOW() - %(stale)s THEN 'closed' END) AS end_reason,
       active_ms, ip_address, device_type, os, browser, screen, timezone, language, entry_path
  FROM user_sessions
 WHERE started_at >= %(start)s AND started_at < %(end)s
   AND (%(emails)s::text[] IS NULL OR user_email = ANY(%(emails)s::text[]))
 ORDER BY started_at DESC
 LIMIT %(limit)s
"""


def fetch_sessions(conn, *, start: datetime.datetime, end: datetime.datetime,
                   emails: Optional[List[str]] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    if emails is not None and not emails:
        return []
    with conn.cursor() as cur:
        cur.execute(_FETCH_SQL, {"stale": STALE_AFTER, "start": start, "end": end,
                                 "emails": emails, "limit": limit})
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["is_active"] = r["logout_at"] is None
        r["duration_seconds"] = int(((r["logout_at"] or r["last_seen_at"]) - r["started_at"]).total_seconds())
        r["active_seconds"] = int((r.pop("active_ms") or 0) / 1000)
        for k in ("started_at", "last_seen_at", "logout_at"):
            r[k] = r[k].isoformat() if r[k] else None
    return rows
