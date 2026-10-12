// The recruiter-action queue, without timers, DOM or imports — the browser
// wiring is lib/activity.ts; this part is unit-tested (lib/activity.test.ts).
// Mirrors apps/api/services/recruiter_activity.CLIENT_ACTIONS.

export const CLIENT_ACTIONS = [
  "candidate_opened",
  "resume_viewed",
  "recording_played",
  "report_exported",
  "job_archived",
  "job_unarchived",
  "outreach_stopped",
  "job_new_version",
  "jobs_dashboard_filtered",
] as const;
export type ClientAction = (typeof CLIENT_ACTIONS)[number];

export const MAX_BATCH = 20;
export const FLUSH_MS = 60_000;
const MAX_QUEUED = 200;

export type AttrValue = string | number | boolean;
export type ActivityItem = {
  action: ClientAction;
  job_ref?: string;
  candidate_id?: string;
  attrs?: Record<string, AttrValue>;
};
export type ActivitySender = (items: ActivityItem[], keepalive: boolean) => Promise<boolean>;

/** The queue, without timers or DOM (lib/activity.test.ts). */
export function createActivityQueue(send: ActivitySender) {
  let queue: ActivityItem[] = [];
  let inFlight = false;

  const flush = (keepalive: boolean): void => {
    if (queue.length === 0 || (inFlight && !keepalive)) return;
    const batch = queue.slice(0, MAX_BATCH);
    queue = queue.slice(batch.length);
    inFlight = true;
    Promise.resolve()
      .then(() => send(batch, keepalive))
      .catch(() => false)
      .then((ok) => {
        // A failed batch goes back to the front once (bounded by MAX_QUEUED).
        if (!ok && !keepalive) queue = [...batch, ...queue].slice(0, MAX_QUEUED);
      })
      .finally(() => {
        inFlight = false;
      });
  };

  return {
    push(item: ActivityItem): boolean {
      if (!(CLIENT_ACTIONS as readonly string[]).includes(item.action)) return false;
      queue.push(item);
      if (queue.length > MAX_QUEUED) queue = queue.slice(queue.length - MAX_QUEUED);
      if (queue.length >= MAX_BATCH) flush(false);
      return true;
    },
    flush,
    size: () => queue.length,
  };
}
