"use client";

import { useEffect } from "react";

import { ApiError, api } from "@/lib/api";
import { SAMPLE_MS, createStepTimeSession, type SendOutcome } from "@/lib/step-time";

// Reports how long a recruiter actively works a wizard step, for the reports'
// "Step 5 Active Time" and "Step 5 → Launch" (apps/api/services/job_step_time.py),
// to POST /api/v1/jobs/{job}/step-time. The rules live in lib/step-time.ts
// (what counts as active, when to send, what a failed send does), where they
// are unit-tested. This hook only wires browser events, a timer and the API
// client to them.
//
// It is telemetry, so it must never get in the recruiter's way. Listeners are
// passive, every failure is swallowed, and nothing here sets React state, so
// the wizard never re-renders because of it.

// Scroll does not bubble out of inner scroll containers (the candidate
// table), so the listeners capture. Capturing also still sees events that a
// component stops with stopPropagation. The session throttles them to one
// stamp a second.
const ACTIVITY_EVENTS = ["pointerdown", "keydown", "wheel", "scroll", "touchstart", "mousemove"] as const;
const LISTENER_OPTIONS: AddEventListenerOptions = { passive: true, capture: true };

const clock = (): number =>
  // Monotonic where available, so a system clock change can't move it. The
  // session also copes with a clock going backwards.
  typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();

// Worth sending again? A network failure, a timeout, a rate limit (nginx
// answers 503) or a 5xx may clear up. Any other 4xx (403 no access, 422)
// would be refused again.
function isRetryable(err: unknown): boolean {
  if (err instanceof ApiError) return err.status >= 500 || err.status === 408 || err.status === 429;
  return true;
}

export type UseStepActiveTimeOptions = {
  /** The job's JobDiva ref or numeric id; the server resolves either. Empty = not tracked. */
  jobRef: string;
  step: number;
  enabled: boolean;
};

export function useStepActiveTime({ jobRef, step, enabled }: UseStepActiveTimeOptions): void {
  const ref = String(jobRef ?? "").trim();
  const tracking = enabled && ref !== "";

  useEffect(() => {
    if (!tracking || typeof window === "undefined" || typeof document === "undefined") return;

    // One session per (job, step), held in this effect's closure rather than
    // in refs. A change of job or step runs the cleanup, which flushes to the
    // OLD job, and then starts a fresh session. So time is never credited to
    // the job the page switched to, and no timer or listener can see another
    // session's state.
    const send = async (activeMs: number, keepalive: boolean): Promise<SendOutcome> => {
      try {
        const res = await api.jobs.stepTime(ref, { step, active_ms: activeMs }, { keepalive });
        // The server answers 200 {"status": "error"} when its write failed.
        return res?.status === "error" ? "retry" : "sent";
      } catch (err) {
        console.debug("[step-time] report failed", err);
        return isRetryable(err) ? "retry" : "drop";
      }
    };

    const isVisible = () => document.visibilityState !== "hidden";
    const session = createStepTimeSession({ now: clock(), visible: isVisible(), send });

    const onActivity = () => session.activity(clock());
    const onVisibilityChange = () => session.visibilityChange(isVisible(), clock());
    // Unload doesn't run React cleanup, so a closing tab's last flush
    // happens here.
    const onPageHide = () => session.pageHide(clock());
    const interval = window.setInterval(() => session.tick(clock()), SAMPLE_MS);

    for (const type of ACTIVITY_EVENTS) window.addEventListener(type, onActivity, LISTENER_OPTIONS);
    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("pagehide", onPageHide);

    return () => {
      window.clearInterval(interval);
      for (const type of ACTIVITY_EVENTS) window.removeEventListener(type, onActivity, LISTENER_OPTIONS);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("pagehide", onPageHide);
      session.end(clock());
    };
  }, [tracking, ref, step]);
}
