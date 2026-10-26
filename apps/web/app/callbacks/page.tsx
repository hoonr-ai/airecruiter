"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Clock, ExternalLink, CheckCircle2, CalendarClock, AlertTriangle, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { callbackDayLabel, callbackRelativeLabel, isCallbackOverdue } from "@/lib/callback-time";
import { cn } from "@/lib/utils";

interface UpcomingCallback {
  candidate_id: string;
  candidate_name: string;
  job_id: string;
  jobdiva_id: string | null;
  job_title: string;
  customer_name: string | null;
  callback_at: string;
}

// Identifies a callback row across state updates — candidate_id alone isn't
// unique across jobs (the same JobDiva candidate could in principle have a
// callback scheduled on more than one job), job_id alone obviously isn't
// either.
function callbackKey(cb: Pick<UpcomingCallback, "candidate_id" | "job_id">): string {
  return `${cb.job_id}:${cb.candidate_id}`;
}

// Mirrors the Rankings callback modal's own `min` (PR #841) — "now" in the
// shape <input type="datetime-local"> expects. Doesn't encode the backend's
// 5-minute/1-year bounds; a value outside those still round-trips to the API
// and surfaces as the same error toast Rankings shows for the same case,
// rather than duplicating that validation client-side here too.
function datetimeLocalMin(): string {
  return new Date(Date.now() - new Date().getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

// `iso`'s time, or +Infinity for an unparsable one (PR #844 review) — sinks
// a malformed timestamp to the very end of both sorts below instead of
// NaN's undefined Array.sort ordering, which could otherwise scatter it
// across more than one place in the list.
function callbackTimeMs(iso: string): number {
  const ms = new Date(iso).getTime();
  return Number.isNaN(ms) ? Infinity : ms;
}

export default function UpcomingCallbacksPage() {
  const [callbacks, setCallbacks] = useState<UpcomingCallback[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rescheduleTarget, setRescheduleTarget] = useState<UpcomingCallback | null>(null);
  const [rescheduleAt, setRescheduleAt] = useState("");

  const load = useCallback(async () => {
    try {
      const res = await api.candidates.getUpcomingCallbacks();
      setCallbacks(Array.isArray(res?.callbacks) ? res.callbacks : []);
      setLoadError(null);
    } catch (err) {
      console.error("Failed to load upcoming callbacks:", err);
      setLoadError("Failed to load upcoming callbacks.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Grouped by local day, soonest-first within each group — the backend
  // already orders by callback_at, but re-sorting here keeps this correct
  // even after a reschedule reorders a single row client-side (see
  // handleConfirmReschedule) rather than refetching the whole list.
  const groups = useMemo(() => {
    // Chronological pass first, purely to build correct, non-fragmented day
    // groups (consecutive same-label runs) — an overdue-aware sort can't run
    // first here, or a day's overdue and still-pending items would scatter
    // into separate buckets of the same label instead of merging into one.
    const chronological = [...(callbacks || [])].sort(
      (a, b) => callbackTimeMs(a.callback_at) - callbackTimeMs(b.callback_at)
    );
    const out: { label: string; items: UpcomingCallback[] }[] = [];
    for (const cb of chronological) {
      const label = callbackDayLabel(cb.callback_at);
      const current = out[out.length - 1];
      if (current && current.label === label) current.items.push(cb);
      else out.push({ label, items: [cb] });
    }
    // Within each day, push already-overdue callbacks to the bottom — an
    // overdue item's own (earlier) time would otherwise rank it above a
    // still-pending one later the same day, burying what's actually next
    // under something already missed.
    for (const group of out) {
      group.items.sort((a, b) => {
        const overdueA = isCallbackOverdue(a.callback_at);
        const overdueB = isCallbackOverdue(b.callback_at);
        if (overdueA !== overdueB) return overdueA ? 1 : -1;
        return callbackTimeMs(a.callback_at) - callbackTimeMs(b.callback_at);
      });
    }
    return out;
  }, [callbacks]);

  const handleMarkDone = async (cb: UpcomingCallback) => {
    const key = callbackKey(cb);
    setBusyKey(key);
    setActionError(null);
    try {
      await api.candidates.markCallbackDone(String(cb.job_id), cb.candidate_id);
      setCallbacks((prev) => (prev || []).filter((c) => callbackKey(c) !== key));
    } catch (err) {
      console.error("Failed to mark callback done:", err);
      setActionError("Failed to mark this callback done. Please try again.");
    } finally {
      setBusyKey(null);
    }
  };

  // Computed once per dialog open rather than on every render (PR #844
  // review) — "now" only needs to be this fresh, not re-evaluated on every
  // keystroke in the time field.
  const rescheduleMin = useMemo(() => datetimeLocalMin(), [rescheduleTarget]);

  const openReschedule = (cb: UpcomingCallback) => {
    setActionError(null);
    setRescheduleTarget(cb);
    setRescheduleAt("");
  };

  const closeReschedule = () => {
    setRescheduleTarget(null);
    setRescheduleAt("");
  };

  const handleConfirmReschedule = async () => {
    if (!rescheduleTarget || !rescheduleAt) return;
    const target = rescheduleTarget;
    const key = callbackKey(target);
    const callbackAtIso = new Date(rescheduleAt).toISOString();
    setBusyKey(key);
    setActionError(null);
    try {
      // Same write Rankings' own Reschedule/Callback Scheduled flow makes —
      // this is a new callback time for the same candidate, not a new
      // concept, so it reuses save_candidate_feedback rather than a
      // dedicated reschedule endpoint. The stale-reminder check added in
      // notifications_service.py (PR #841 review) means the OLD reminder
      // for this candidate's previous time is automatically skipped once
      // this lands, without anything here having to cancel it directly.
      await api.candidates.feedback(String(target.job_id), target.candidate_id, {
        feedback_type: "Callback Scheduled",
        callback_at: callbackAtIso,
      });
      setCallbacks((prev) =>
        (prev || []).map((c) => (callbackKey(c) === key ? { ...c, callback_at: callbackAtIso } : c))
      );
      closeReschedule();
    } catch (err) {
      console.error("Failed to reschedule callback:", err);
      setActionError("Failed to reschedule this callback. Please try again.");
    } finally {
      setBusyKey(null);
    }
  };

  const isEmpty = callbacks !== null && callbacks.length === 0;

  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-center gap-2 mb-6">
        <Clock className="h-5 w-5 text-slate-500" />
        <h1 className="text-xl font-semibold text-slate-900">Upcoming Callbacks</h1>
      </div>

      {actionError && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          {actionError}
        </div>
      )}

      {loadError && (
        <Card className="p-6 flex flex-col items-center gap-3 text-center text-sm text-rose-600">
          {loadError}
          <Button variant="outline" size="sm" onClick={() => load()}>
            Retry
          </Button>
        </Card>
      )}

      {!loadError && callbacks === null && (
        <Card className="p-10 flex items-center justify-center text-slate-400">
          <Loader2 className="h-5 w-5 animate-spin" />
        </Card>
      )}

      {!loadError && isEmpty && (
        <Card className="p-10 text-center">
          <CalendarClock className="h-8 w-8 text-slate-300 mx-auto mb-3" />
          <p className="text-sm text-slate-500">No callbacks scheduled. Schedule one from a candidate on Rankings or the Master Candidate Pool.</p>
        </Card>
      )}

      {!loadError && callbacks !== null && !isEmpty && (
        <div className="space-y-6">
          {groups.map((group) => (
            <div key={group.label}>
              <h2 className="text-xs font-bold uppercase tracking-widest text-slate-400 mb-2">{group.label}</h2>
              <div className="space-y-2">
                {group.items.map((cb) => {
                  const key = callbackKey(cb);
                  const overdue = isCallbackOverdue(cb.callback_at);
                  const busy = busyKey === key;
                  return (
                    <Card key={key} className="p-4 flex flex-row items-center justify-between gap-4">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <Link
                            href={`/jobs/${encodeURIComponent(cb.job_id)}/rankings`}
                            className="font-semibold text-slate-900 hover:underline truncate"
                          >
                            {cb.candidate_name}
                          </Link>
                          <ExternalLink className="h-3.5 w-3.5 text-slate-300 shrink-0" />
                        </div>
                        <p className="text-sm text-slate-500 truncate">
                          {cb.job_title}
                          {cb.customer_name ? ` · ${cb.customer_name}` : ""}
                        </p>
                        <p
                          className={cn(
                            "text-xs font-semibold mt-1",
                            overdue ? "text-rose-600" : "text-sky-600"
                          )}
                        >
                          {overdue && <AlertTriangle className="inline h-3 w-3 mr-1 -mt-0.5" />}
                          {callbackRelativeLabel(cb.callback_at)} · {new Date(cb.callback_at).toLocaleString()}
                        </p>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => openReschedule(cb)}
                          disabled={busy}
                        >
                          Reschedule
                        </Button>
                        <Button
                          size="sm"
                          onClick={() => handleMarkDone(cb)}
                          disabled={busy}
                          className="bg-emerald-600 hover:bg-emerald-700 text-white"
                        >
                          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4 mr-1.5" />}
                          {busy ? "" : "Mark done"}
                        </Button>
                      </div>
                    </Card>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      )}

      <Dialog open={!!rescheduleTarget} onOpenChange={(open) => !open && closeReschedule()}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Reschedule callback</DialogTitle>
            <DialogDescription>
              When do you now plan to call{" "}
              <strong className="text-slate-900 font-semibold">{rescheduleTarget?.candidate_name}</strong> back?
              You&apos;ll get a reminder 5 minutes before the new time.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <label htmlFor="reschedule-callback-at" className="text-xs font-bold text-slate-500 uppercase tracking-widest">
              Callback Time
            </label>
            <input
              id="reschedule-callback-at"
              type="datetime-local"
              aria-label="Callback Time"
              className="w-full h-11 px-3 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-sky-500/20 focus:border-sky-500/50"
              value={rescheduleAt}
              min={rescheduleMin}
              onChange={(e) => setRescheduleAt(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeReschedule}>
              Cancel
            </Button>
            <Button
              onClick={handleConfirmReschedule}
              disabled={!rescheduleAt || (!!rescheduleTarget && busyKey === callbackKey(rescheduleTarget))}
              className="font-bold bg-sky-600 hover:bg-sky-700 text-white"
            >
              {rescheduleTarget && busyKey === callbackKey(rescheduleTarget) ? "Saving..." : "Reschedule"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
