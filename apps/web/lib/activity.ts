// Recruiter actions only the page sees (a recording played, a candidate
// opened, a report exported, a job archived …), for the recruiter-activity
// view (apps/api/services/recruiter_activity.py) and New Relic.
//
// recordActivity() never sends one request per action: actions queue and go
// out together (≤ MAX_BATCH per request) every FLUSH_MS, when the queue fills,
// and on pagehide (keepalive). nginx gives a whole office 30 API writes a
// minute, and this is telemetry. Each action is also a New Relic PageAction
// `recruiter_<action>` (lib/analytics.ts bridge; PII-filtered there) — the
// pseudonymous NR user id is set by TelemetryBootstrap.
//
// Actions the server can attribute (searches, launch clicks, feedback, launch
// sessions) are recorded server-side and are NOT sent from here.

import { trackEvent } from "@/lib/analytics";
import { api } from "@/lib/api";
import {
  FLUSH_MS,
  createActivityQueue,
  type ActivityItem,
  type AttrValue,
  type ClientAction,
} from "@/lib/activity-queue";

export { CLIENT_ACTIONS, type ClientAction } from "@/lib/activity-queue";

let browserQueue: ReturnType<typeof createActivityQueue> | null = null;

function getBrowserQueue(): ReturnType<typeof createActivityQueue> | null {
  if (typeof window === "undefined") return null;
  if (browserQueue) return browserQueue;
  browserQueue = createActivityQueue(async (items, keepalive) => {
    try {
      const res = await api.activity.actions({ actions: items }, { keepalive });
      return res?.status !== "error";
    } catch {
      return false;
    }
  });
  const q = browserQueue;
  window.setInterval(() => q.flush(false), FLUSH_MS);
  window.addEventListener("pagehide", () => q.flush(true));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") q.flush(true);
  });
  return browserQueue;
}

/** Record a page-only recruiter action. Never throws, never blocks. */
export function recordActivity(
  action: ClientAction,
  opts: { jobRef?: string | null; candidateId?: string | null; attrs?: Record<string, AttrValue> } = {},
): void {
  try {
    const item: ActivityItem = { action };
    const jobRef = String(opts.jobRef ?? "").trim();
    if (jobRef) item.job_ref = jobRef;
    const candidateId = String(opts.candidateId ?? "").trim();
    if (candidateId) item.candidate_id = candidateId;
    if (opts.attrs) item.attrs = opts.attrs;
    trackEvent(`recruiter_${action}`, { job_ref: item.job_ref, ...(opts.attrs ?? {}) });
    getBrowserQueue()?.push(item);
  } catch {
    // telemetry must never break the caller
  }
}
