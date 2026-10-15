"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { CheckCircle2, ChevronDown, ChevronUp, Loader2, Rocket, X, XCircle } from "lucide-react";
import { launchSessionsApi } from "@/lib/api";
import { LAUNCH_STARTED_EVENT, type LaunchSessionView } from "@/lib/launch-session";
import { jobRankingsHref, sourceAndLaunchHref } from "@/lib/routes";

// Bottom-right launch tracker (LinkedIn-style minimized dock). Source & Launch
// runs server-side as a launch session (routers/launch_sessions.py), so the
// recruiter can leave the job page; this dock shows every launch they started,
// on every page, and survives reloads (it reads /launch-sessions/mine).

type TrackedSession = LaunchSessionView & { job_title?: string | null };

const ACTIVE_POLL_MS = 5_000;
const IDLE_POLL_MS = 60_000;
const AUTH_BACKOFF_MS = 5 * 60_000;
const DISMISSED_KEY = "pair.launchTracker.dismissed";
const COLLAPSED_KEY = "pair.launchTracker.collapsed";

const STAGE_LABEL: Record<string, string> = {
  created: "Starting",
  sourcing: "Sourcing candidates",
  selecting: "Selecting candidates",
  enriching: "Finding contact details",
  review: "Waiting for your review",
  splitting: "Checking contacts",
  saving: "Saving candidates",
  launching: "Launching PAIR",
  provisioning: "Syncing to JobDiva",
  done: "Wrapping up",
};

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage blocked: the dock still works for this page view */
  }
}

function isRunning(s: TrackedSession): boolean {
  return !s.terminal && !s.launch_complete;
}

function launched(s: TrackedSession): number {
  return s.launched_count ?? 0;
}

function target(s: TrackedSession): number {
  const c = s.counts;
  return c?.split?.ready ?? c?.selected ?? s.input?.launch_count ?? 0;
}

function statusLine(s: TrackedSession): string {
  if (s.status === "cancelled") return "Cancelled";
  if (s.status === "failed") return s.error ? `Failed: ${s.error}` : "Failed";
  if (s.launch_complete || s.terminal) {
    const parts = [`${launched(s)} launched`];
    if (s.failed_count) parts.push(`${s.failed_count} failed`);
    if (s.needs_contact_count) parts.push(`${s.needs_contact_count} need contact`);
    return parts.join(" · ");
  }
  if (s.status === "queued") {
    return s.queue_position ? `Queued · ${s.queue_position} ahead` : "Queued";
  }
  if (s.status === "awaiting_input") return "Waiting for your review";
  const label = STAGE_LABEL[s.stage] ?? "Working";
  if (s.stage === "sourcing" && s.sourcing) {
    const q = s.sourcing.qualified ?? 0;
    return `${label} · ${q} qualified`;
  }
  if (s.stage === "launching") {
    const t = target(s);
    return t ? `${label} · ${launched(s)}/${t}` : label;
  }
  return label;
}

function progressPct(s: TrackedSession): number | null {
  if (s.launch_complete || s.terminal) return 100;
  if (s.stage === "launching") {
    const t = target(s);
    return t ? Math.min(100, Math.round(((launched(s) + (s.failed_count ?? 0)) / t) * 100)) : null;
  }
  return null; // indeterminate before the launch stage
}

function jobHref(s: TrackedSession): string {
  const ref = s.jobdiva_ref || s.job_id;
  // Running: back to the launch's progress. Launched: to the results.
  return isRunning(s) ? sourceAndLaunchHref(ref) : jobRankingsHref(ref);
}

export function LaunchTracker() {
  const pathname = usePathname();
  const [sessions, setSessions] = useState<TrackedSession[]>([]);
  // Lazy reads are safe for SSR: readJson falls back without localStorage,
  // and the dock renders nothing until the first poll lands anyway.
  const [dismissed, setDismissed] = useState<string[]>(() => readJson<string[]>(DISMISSED_KEY, []));
  const [collapsed, setCollapsed] = useState(() => readJson<boolean>(COLLAPSED_KEY, true));
  // Ids of launches that finished while this tab watched: their rows (and the
  // header) are highlighted instead of forcing the dock open.
  const [fresh, setFresh] = useState<string[]>([]);
  const prevRunning = useRef<Set<string>>(new Set());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pollRef = useRef<() => Promise<void>>(async () => {});
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const failures = useRef(0);

  const poll = useCallback(async () => {
    // One request at a time: the timer, a tab becoming visible and a new
    // launch can all ask for a poll at once.
    if (inFlight.current || !mounted.current) return;
    inFlight.current = true;
    clearTimeout(timer.current);
    let delay = IDLE_POLL_MS;
    try {
      const res = await launchSessionsApi.mine();
      if (!mounted.current) return;
      if (res.ok && res.data) {
        failures.current = 0;
        const list = res.data.sessions;
        const nowRunning = new Set(list.filter(isRunning).map((s) => s.id));
        const finished = [...prevRunning.current].filter((id) => !nowRunning.has(id));
        prevRunning.current = nowRunning;
        if (finished.length) setFresh((f) => [...f, ...finished]);
        setSessions(list);
        if (nowRunning.size > 0 && document.visibilityState === "visible") delay = ACTIVE_POLL_MS;
      } else if (res.status === 401 || res.status === 403) {
        // Signed out or token expired: the auth flow handles it; don't hammer.
        delay = AUTH_BACKOFF_MS;
      } else {
        failures.current += 1;
        delay = Math.min(IDLE_POLL_MS, ACTIVE_POLL_MS * 2 ** failures.current);
      }
    } finally {
      inFlight.current = false;
    }
    if (mounted.current) timer.current = setTimeout(() => void pollRef.current(), delay);
  }, []);
  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  useEffect(() => {
    mounted.current = true;
    timer.current = setTimeout(() => void poll(), 0);
    const onVisible = () => {
      if (document.visibilityState === "visible") void poll();
    };
    // The job page fires this right after it starts a session, so the dock
    // picks it up without waiting for the idle poll.
    const onStarted = () => void poll();
    // Keep dismiss / collapse in step across tabs.
    const onStorage = (e: StorageEvent) => {
      if (e.key === DISMISSED_KEY) setDismissed(readJson<string[]>(DISMISSED_KEY, []));
      if (e.key === COLLAPSED_KEY) setCollapsed(readJson<boolean>(COLLAPSED_KEY, true));
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener(LAUNCH_STARTED_EVENT, onStarted);
    window.addEventListener("storage", onStorage);
    return () => {
      mounted.current = false;
      clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener(LAUNCH_STARTED_EVENT, onStarted);
      window.removeEventListener("storage", onStorage);
    };
  }, [poll]);

  const toggle = () => {
    setFresh([]); // seen
    setCollapsed((c) => {
      writeJson(COLLAPSED_KEY, !c);
      return !c;
    });
  };

  const dismiss = (id: string) => {
    setDismissed((d) => {
      const next = [...d, id].slice(-100);
      setFresh((f) => f.filter((x) => x !== id));
      writeJson(DISMISSED_KEY, next);
      return next;
    });
  };

  const visible = sessions.filter((s) => isRunning(s) || !dismissed.includes(s.id));
  if (visible.length === 0) return null;

  const running = visible.filter(isRunning);
  const newlyDone = visible.filter((s) => fresh.includes(s.id)).length;
  let header =
    running.length > 0
      ? `${running.length} launch${running.length === 1 ? "" : "es"} running`
      : `${visible.length} launch${visible.length === 1 ? "" : "es"} finished`;
  if (running.length > 0 && newlyDone > 0) header += ` · ${newlyDone} just finished`;

  // TiraFab (56px, bottom-6 right-6) sits bottom-right: on wide screens the
  // dock sits left of it; on narrow ones it goes full-width above it.
  return (
    <div className="fixed bottom-24 left-4 right-4 z-40 rounded-lg border border-slate-200 bg-white shadow-xl sm:bottom-0 sm:left-auto sm:right-24 sm:w-80 sm:rounded-b-none">
      <button
        type="button"
        onClick={toggle}
        className={`flex w-full items-center gap-2 rounded-t-lg px-3 py-2 text-left text-sm font-medium text-white ${
          newlyDone > 0 ? "bg-emerald-700" : "bg-slate-900"
        }`}
      >
        {running.length > 0 ? <Loader2 className="h-4 w-4 animate-spin" /> : <Rocket className="h-4 w-4" />}
        <span className="flex-1">{header}</span>
        {collapsed ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
      </button>
      {!collapsed && (
        <ul className="max-h-80 divide-y divide-slate-100 overflow-y-auto">
          {visible.map((s) => {
            const pct = progressPct(s);
            const runningRow = isRunning(s);
            const bad = s.status === "failed" || s.status === "cancelled";
            const onThisJob = pathname?.startsWith("/jobs/new");
            return (
              <li key={s.id} className="px-3 py-2">
                <div className="flex items-start gap-2">
                  <span className="mt-0.5">
                    {runningRow ? (
                      <Loader2 className="h-4 w-4 animate-spin text-blue-600" />
                    ) : bad ? (
                      <XCircle className="h-4 w-4 text-red-600" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                    )}
                  </span>
                  <div className="min-w-0 flex-1">
                    <Link
                      prefetch={false}
                      href={jobHref(s)}
                      className="block truncate text-sm font-medium text-slate-900 hover:underline"
                      title={s.job_title || s.jobdiva_ref || s.job_id}
                    >
                      {s.job_title || `Job ${s.jobdiva_ref || s.job_id}`}
                    </Link>
                    <p className={`truncate text-xs ${bad ? "text-red-600" : "text-slate-500"}`}>{statusLine(s)}</p>
                    {runningRow && (
                      <div className="mt-1 h-1.5 w-full overflow-hidden rounded bg-slate-100">
                        {pct === null ? (
                          <div className="h-full w-1/3 animate-pulse rounded bg-blue-400" />
                        ) : (
                          <div className="h-full rounded bg-blue-600 transition-all" style={{ width: `${pct}%` }} />
                        )}
                      </div>
                    )}
                    {/* Failed / needs-contact rows are retried from the launch step. */}
                    {!runningRow && !onThisJob && (s.failed_count || s.needs_contact_count) ? (
                      <Link
                        prefetch={false}
                        href={sourceAndLaunchHref(s.jobdiva_ref || s.job_id)}
                        className="text-xs font-medium text-blue-600 hover:underline"
                      >
                        Review
                      </Link>
                    ) : null}
                  </div>
                  {!runningRow && (
                    <button
                      type="button"
                      onClick={() => dismiss(s.id)}
                      className="text-slate-400 hover:text-slate-700"
                      aria-label="Dismiss"
                    >
                      <X className="h-4 w-4" />
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
