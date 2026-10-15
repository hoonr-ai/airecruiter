"use client";

import { useEffect } from "react";

import { ApiError, api } from "@/lib/api";
import { SAMPLE_MS, createStepTimeSession, type SendOutcome } from "@/lib/step-time";

// Active time a recruiter spends on one surface of PAIR (Rankings, the Assess
// modal, the candidate details, the dashboards and reports), for the
// recruiter-activity view (apps/api/services/recruiter_activity.py), sent to
// POST /api/v1/activity/time. The rules are the wizard's (lib/step-time.ts:
// tab visible + an interaction within the last 5 minutes; flushed ~every 2
// minutes, on hide and on leave), minus the entry ping — a surface only sums
// time. hooks/use-step-active-time.ts stays the wizard's; its time is mirrored
// into the same view server-side.
//
// One surface at a time: a page pauses its own tracker (enabled=false) while
// a modal that has its own surface is open, so time is never counted twice.
// Telemetry: passive listeners, every failure swallowed, no React state.

const ACTIVITY_EVENTS = ["pointerdown", "keydown", "wheel", "scroll", "touchstart", "mousemove"] as const;
const LISTENER_OPTIONS: AddEventListenerOptions = { passive: true, capture: true };

export type ActivitySurface =
  | "jobs_dashboard"
  | "rankings"
  | "assess_modal"
  | "candidate_detail"
  | "candidates"
  | "launch_report"
  | "analytics"
  | "recruiter_analytics"
  | "admin_dashboard"
  | "campaigns";

const clock = (): number =>
  typeof performance !== "undefined" && typeof performance.now === "function" ? performance.now() : Date.now();

function isRetryable(err: unknown): boolean {
  if (err instanceof ApiError) return err.status >= 500 || err.status === 408 || err.status === 429;
  return true;
}

export function useActiveTime({
  surface,
  jobRef,
  enabled = true,
}: {
  surface: ActivitySurface;
  jobRef?: string | null;
  enabled?: boolean;
}): void {
  const ref = String(jobRef ?? "").trim();

  useEffect(() => {
    if (!enabled || typeof window === "undefined" || typeof document === "undefined") return;

    const send = async (activeMs: number, keepalive: boolean): Promise<SendOutcome> => {
      try {
        const res = await api.activity.time(
          { surface, ...(ref ? { job_ref: ref } : {}), active_ms: activeMs },
          { keepalive },
        );
        return res?.status === "error" ? "retry" : "sent";
      } catch (err) {
        return isRetryable(err) ? "retry" : "drop";
      }
    };

    const isVisible = () => document.visibilityState !== "hidden";
    const session = createStepTimeSession({ now: clock(), visible: isVisible(), send, entry: false });
    const onActivity = () => session.activity(clock());
    const onVisibilityChange = () => session.visibilityChange(isVisible(), clock());
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
  }, [enabled, surface, ref]);
}
