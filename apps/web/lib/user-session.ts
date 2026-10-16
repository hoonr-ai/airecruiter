// Platform session for the signed-in user (routers/user_sessions.py): login
// time, time spent, active time and device. Never throws.
//
// Cheap by design — every non-GET /api/ call from an office shares nginx's
// 30 writes/min (nginx-upstreams.conf):
//  - one session per browser: the id lives in localStorage, so tabs share it
//    and a reload resumes it instead of logging a new login;
//  - the browser mints the id, so the first ping IS the login (no start call);
//  - at most one ping per PING_MS across all tabs, and only while someone is
//    using a tab; one more when a tab closes, so the logout time is exact;
//  - a quiet browser is never closed by a write: the server reads "no ping for
//    15 min" as ended at the last ping.

import { api } from "@/lib/api";

const STORAGE_KEY = "pair.session.v1";
const PING_MS = 5 * 60_000; // services/user_sessions.py PING_INTERVAL
const STALE_MS = 15 * 60_000; // services/user_sessions.py STALE_AFTER
const TICK_MS = 30_000;
const IDLE_MS = 5 * 60_000; // no input for this long: not active
const CLOSE_PING_MIN_GAP_MS = 60_000;

type Stored = { id: string; email: string; lastPing: number };

let email: string | null = null;
let userName: string | undefined;
let activeMs = 0;
let lastTick = 0;
let lastInput = 0;
let wired = false;

function read(): Stored | null {
  try {
    const v = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "null");
    return v && typeof v.id === "string" ? (v as Stored) : null;
  } catch {
    return null;
  }
}

function write(v: Stored | null): void {
  try {
    if (v) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // private mode: the session still works, per tab
  }
}

function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, (c) =>
        (+c ^ (Math.random() * 16) >> (+c / 4)).toString(16),
      );
}

/** The live session for this user, minting a new one if needed (true = new). */
function current(): [Stored, boolean] {
  const s = read();
  if (s && s.email === email && Date.now() - s.lastPing < STALE_MS) return [s, false];
  const fresh = { id: newId(), email: email!, lastPing: 0 };
  write(fresh);
  return [fresh, true];
}

function accrue(): void {
  const now = Date.now();
  if (document.visibilityState === "visible" && now - lastInput < IDLE_MS) activeMs += now - lastTick;
  lastTick = now;
}

async function ping(opts: { keepalive?: boolean; end?: "logout" } = {}): Promise<void> {
  if (!email) return;
  accrue();
  const [s, isNew] = current();
  const ms = activeMs;
  activeMs = 0;
  write({ ...s, lastPing: Date.now() }); // claim the slot before other tabs do
  try {
    const res = await api.sessions.ping(
      {
        session_id: s.id,
        active_ms: ms,
        end: opts.end,
        ...(isNew
          ? {
              user_name: userName,
              screen: `${window.screen.width}x${window.screen.height}`,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
              language: navigator.language,
              entry_path: window.location.pathname,
            }
          : {}),
      },
      { keepalive: opts.keepalive },
    );
    // Signed out elsewhere: the next ping starts a new session.
    if (res?.status === "ended") write(null);
    else if (res?.status === "error") activeMs += ms;
  } catch {
    activeMs += ms; // resent with the next ping
  }
}

function tick(): void {
  accrue();
  const s = read();
  const due = !s || s.email !== email || Date.now() - s.lastPing >= PING_MS;
  if (due && document.visibilityState === "visible" && Date.now() - lastInput < IDLE_MS) void ping();
}

/** Start tracking for the signed-in user. Safe to call on every render. */
export function startUserSession(userEmail: string, name?: string): void {
  if (typeof window === "undefined") return;
  email = userEmail.trim().toLowerCase();
  userName = name;
  if (wired) return;
  wired = true;
  lastTick = lastInput = Date.now();
  const onInput = () => {
    lastInput = Date.now();
  };
  for (const ev of ["pointerdown", "keydown", "wheel", "touchstart"]) {
    window.addEventListener(ev, onInput, { passive: true, capture: true });
  }
  document.addEventListener("visibilitychange", () => {
    accrue();
    if (document.visibilityState === "visible") onInput();
  });
  window.addEventListener("pagehide", () => {
    const s = read();
    if (s && Date.now() - s.lastPing >= CLOSE_PING_MIN_GAP_MS) void ping({ keepalive: true });
  });
  window.setInterval(tick, TICK_MS);
  // A new session's first ping is the login; a resumed one waits its turn.
  if (current()[1]) void ping();
}

/** Sign Out: record the logout time and forget the session. */
export async function endUserSession(): Promise<void> {
  if (!email || !read()) return;
  await ping({ end: "logout", keepalive: true });
  write(null);
  email = null;
}
