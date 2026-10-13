"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { CheckCircle2, ChevronDown, ChevronUp, Loader2, Rocket, X, XCircle } from "lucide-react";
import { launchSessionsApi } from "@/lib/api";
import type { LaunchSessionView } from "@/lib/launch-session";

// Bottom-right launch tracker (LinkedIn-style minimized dock). Source & Launch
// runs server-side as a launch session (routers/launch_sessions.py), so the
// recruiter can leave the job page; this dock shows every launch they started,
// on every page, and survives reloads (it reads /launch-sessions/mine).

type TrackedSession = LaunchSessionView & { job_title?: string | null };

const ACTIVE_POLL_MS = 5_000;
const IDLE_POLL_MS = 60_000;
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
  return `/jobs/new?jobId=${encodeURIComponent(ref)}&mode=source&step=5`;
}

export function LaunchTracker() {
  const pathname = usePathname();
  const [sessions, setSessions] = useState<TrackedSession[]>([]);
  // Lazy reads are safe for SSR: readJson falls back without localStorage,
  // and the dock renders nothing until the first poll lands anyway.
  const [dismissed, setDismissed] = useState<string[]>(() => readJson<string[]>(DISMISSED_KEY, []));
  const [collapsed, setCollapsed] = useState(() => readJson<boolean>(COLLAPSED_KEY, true));
  const prevRunning = useRef<Set<string>>(new Set());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const pollRef = useRef<() => Promise<void>>(async () => {});

  const poll = useCallback(async () => {
    clearTimeout(timer.current);
    const res = await launchSessionsApi.mine();
    let anyRunning = false;
    if (res.ok && res.data) {
      const list = res.data.sessions;
      anyRunning = list.some(isRunning);
      // A launch that just finished pops the dock open once.
      const nowRunning = new Set(list.filter(isRunning).map((s) => s.id));
      const justFinished = [...prevRunning.current].some((id) => !nowRunning.has(id));
      prevRunning.current = nowRunning;
      if (justFinished) setCollapsed(false);
      setSessions(list);
    }
    const delay = anyRunning && document.visibilityState === "visible" ? ACTIVE_POLL_MS : IDLE_POLL_MS;
    timer.current = setTimeout(() => void pollRef.current(), delay);
  }, []);
  useEffect(() => {
    pollRef.current = poll;
  }, [poll]);

  useEffect(() => {
    timer.current = setTimeout(() => void poll(), 0);
    const onVisible = () => {
      if (document.visibilityState === "visible") void poll();
    };
    // The job page fires this right after it starts a session, so the dock
    // picks it up without waiting for the idle poll.
    const onStarted = () => void poll();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pair:launch-started", onStarted);
    return () => {
      clearTimeout(timer.current);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pair:launch-started", onStarted);
    };
  }, [poll]);

  const toggle = () => {
    setCollapsed((c) => {
      writeJson(COLLAPSED_KEY, !c);
      return !c;
    });
  };

  const dismiss = (id: string) => {
    setDismissed((d) => {
      const next = [...d, id].slice(-100);
      writeJson(DISMISSED_KEY, next);
      return next;
    });
  };

  const visible = sessions.filter((s) => isRunning(s) || !dismissed.includes(s.id));
  if (visible.length === 0) return null;

  const running = visible.filter(isRunning);
  const header =
    running.length > 0
      ? `${running.length} launch${running.length === 1 ? "" : "es"} running`
      : `${visible.length} launch${visible.length === 1 ? "" : "es"} finished`;

  // TiraFab sits bottom-right; keep clear of it.
  return (
    <div className="fixed bottom-0 right-24 z-40 w-80 rounded-t-lg border border-slate-200 bg-white shadow-xl">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center gap-2 rounded-t-lg bg-slate-900 px-3 py-2 text-left text-sm font-medium text-white"
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
                    {!runningRow && !onThisJob && (s.failed_count || s.needs_contact_count) ? (
                      <Link prefetch={false} href={jobHref(s)} className="text-xs font-medium text-blue-600 hover:underline">
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
