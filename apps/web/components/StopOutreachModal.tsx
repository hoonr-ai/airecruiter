"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Ban, Check, Loader2, Mail, Phone, RotateCcw } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import {
  useOutreachOptOut,
  type OptOutResult,
  type OptOutStatusResult,
} from "@/hooks/use-outreach-opt-out";

// "Stop outreach" — the action a recruiter needs the moment a candidate says
// "stop contacting me". Before this existed the recruiter had to leave pair,
// find the candidate in pair-bot and click a second button; if they didn't,
// the automated reminder calls kept going.
//
// Three things this component is careful about:
//
// 1. It CONFIRMS first. The stop is not undoable in one click by design — it
//    cancels queued sends across every interview the candidate has.
// 2. It renders the backend's `message` VERBATIM. That text carries pair-bot's
//    "across N interviews" count (the part recruiters need, because the
//    candidate's other campaign is usually the one that "kept calling"), and
//    the plain-English note for when a pair-scoped opt-out had to be enforced
//    across every product anyway.
// 3. It reads the current suppression state on open, so a contact who is
//    already stopped offers Resume instead of a second, pointless Stop.

export interface StopOutreachCandidate {
  candidate_id?: string;
  name?: string;
  email?: string;
  phone?: string;
  /** pair-bot's interview id, when the row has one. Used as a fallback
   *  identifier if no email or phone is on file. */
  interview_id?: string | number;
}

interface StopOutreachModalProps {
  open: boolean;
  candidate: StopOutreachCandidate | null;
  onClose: () => void;
  /** Fired after a successful stop or resume so the caller can refetch. */
  onChanged?: () => void;
}

function toInterviewId(raw?: string | number): number | undefined {
  if (raw === undefined || raw === null || raw === "") return undefined;
  const n = Number(raw);
  return Number.isFinite(n) ? n : undefined;
}

export function StopOutreachModal({
  open,
  candidate,
  onClose,
  onChanged,
}: StopOutreachModalProps) {
  const { stopOutreach, resumeOutreach, getOptOutStatus } = useOutreachOptOut();

  const [reason, setReason] = useState("");
  const [allProducts, setAllProducts] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OptOutResult | null>(null);
  const [status, setStatus] = useState<OptOutStatusResult | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);

  const name = candidate?.name?.trim() || "this candidate";
  const candidateKey = candidate?.candidate_id || "";
  const email = candidate?.email?.trim() || "";
  const phone = candidate?.phone?.trim() || "";
  const interviewId = toInterviewId(candidate?.interview_id);
  // candidate_id counts: the backend resolves the latest email/phone off the
  // candidate's own record when the row on screen does not carry them.
  const hasIdentifier = Boolean(
    email || phone || interviewId !== undefined || candidateKey
  );
  const showsOwnContact = Boolean(email || phone);

  // Reset on the closed→open transition, during render rather than in an
  // effect: the effect form triggers a cascading re-render, and the reset must
  // land before the first paint or the previous candidate's result flashes up.
  const [wasOpen, setWasOpen] = useState(open);
  const [openedKey, setOpenedKey] = useState(candidateKey);
  if (open !== wasOpen || (open && candidateKey !== openedKey)) {
    setWasOpen(open);
    setOpenedKey(candidateKey);
    if (open) {
      setReason("");
      setAllProducts(false);
      setError(null);
      setResult(null);
      setStatus(null);
      setStatusLoading(hasIdentifier);
    }
  }

  const alreadySuppressed = Boolean(
    (status?.pairbot?.data?.suppressed_channels?.length ?? 0) > 0 ||
      status?.local?.dnc_listed ||
      (status?.local?.stopped_rows ?? 0) > 0
  );

  // Read the current state rather than assuming it. This is the same question
  // asked right after a complaint arrives: "are we still contacting them?"
  useEffect(() => {
    if (!open || !hasIdentifier) return;
    let cancelled = false;
    getOptOutStatus({
      candidateId: candidateKey || undefined,
      email: email || undefined,
      phone: phone || undefined,
      interviewId,
    })
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {
        // A failed status read must not block the stop — that would be the
        // incident all over again. The action stays available.
        if (!cancelled) setStatus(null);
      })
      .finally(() => {
        if (!cancelled) setStatusLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, candidateKey, email, phone, interviewId, hasIdentifier, getOptOutStatus]);

  const handleStop = useCallback(async () => {
    if (!candidate) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await stopOutreach({
        candidateId: candidateKey || undefined,
        email: email || undefined,
        phone: phone || undefined,
        interviewId,
        reason: reason.trim() || undefined,
        scope: allProducts ? "global" : undefined,
      });
      setResult(res);
      onChanged?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not stop outreach.");
    } finally {
      setSubmitting(false);
    }
  }, [candidate, candidateKey, email, phone, interviewId, reason, allProducts, onChanged, stopOutreach]);

  const handleResume = useCallback(async () => {
    if (!candidate) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await resumeOutreach({
        candidateId: candidateKey || undefined,
        email: email || undefined,
        phone: phone || undefined,
        reason: reason.trim() || undefined,
      });
      setResult(res);
      onChanged?.();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not resume outreach.");
    } finally {
      setSubmitting(false);
    }
  }, [candidate, candidateKey, email, phone, reason, onChanged, resumeOutreach]);

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-[540px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Ban className="w-4 h-4 text-red-600" />
            {alreadySuppressed && !result ? "Outreach already stopped" : "Stop outreach"}
          </DialogTitle>
          <DialogDescription>
            {result
              ? "Result from PAIR Bot"
              : alreadySuppressed
                ? `${name} is already suppressed. Resume only if they asked to hear from us again — it clears the block but does not restart the cancelled campaign.`
                : `Stops every email, SMS and call to ${name} — on every interview they have. Queued messages and calls are cancelled.`}
          </DialogDescription>
        </DialogHeader>

        {/* Result — the backend's message, verbatim. */}
        {result ? (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md border border-green-200 bg-green-50 p-3">
              <Check className="w-4 h-4 text-green-700 mt-0.5 shrink-0" />
              <p className="text-sm text-green-900">{result.message}</p>
            </div>
            {result.local?.error ? (
              <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3">
                <AlertTriangle className="w-4 h-4 text-amber-700 mt-0.5 shrink-0" />
                <p className="text-xs text-amber-900">
                  Outreach is stopped, but pair could not record it locally
                  ({result.local.error}). This candidate may be re-launched on a
                  future import — please tell engineering.
                </p>
              </div>
            ) : null}
          </div>
        ) : (
          <div className="space-y-4">
            {/* What will be suppressed. Both identities are sent when both are
                known: pair-bot stores them separately, and a suppression on
                the address alone will not match a later STOP text. */}
            <div className="rounded-md border border-slate-200 bg-slate-50 p-3 space-y-1.5">
              {email ? (
                <div className="flex items-center gap-2 text-sm text-slate-700">
                  <Mail className="w-3.5 h-3.5 text-slate-500" />
                  <span className="truncate">{email}</span>
                </div>
              ) : null}
              {phone ? (
                <div className="flex items-center gap-2 text-sm text-slate-700">
                  <Phone className="w-3.5 h-3.5 text-slate-500" />
                  <span>{phone}</span>
                </div>
              ) : null}
              {!showsOwnContact && candidateKey ? (
                <p className="text-sm text-slate-600">
                  This row does not carry contact details — PAIR will use the
                  email and phone on {name}&apos;s record.
                </p>
              ) : null}
              {!showsOwnContact && !candidateKey && interviewId !== undefined ? (
                <p className="text-sm text-slate-600">
                  No contact details on this row — using interview #{interviewId}.
                </p>
              ) : null}
              {!hasIdentifier ? (
                <p className="text-sm text-red-700">
                  No email, phone or interview on file for {name}. Add a contact
                  detail before stopping outreach.
                </p>
              ) : null}
            </div>

            {statusLoading ? (
              <p className="text-xs text-slate-500 flex items-center gap-1.5">
                <Loader2 className="w-3 h-3 animate-spin" />
                Checking current status…
              </p>
            ) : status?.pairbot?.message && alreadySuppressed ? (
              <p className="text-xs text-slate-600">{status.pairbot.message}</p>
            ) : null}

            <div className="space-y-1.5">
              <label htmlFor="stop-outreach-reason" className="text-sm font-medium text-slate-700">
                Reason <span className="font-normal text-slate-400">(optional)</span>
              </label>
              <Textarea
                id="stop-outreach-reason"
                value={reason}
                onChange={(e) => setReason(e.target.value.slice(0, 500))}
                rows={3}
                placeholder="e.g. Candidate asked me on the phone not to be contacted again"
                className="text-sm"
              />
              {/* "Why" is the first question asked when a candidate escalates. */}
              <p className="text-xs text-slate-500">
                Stored in the audit trail. Worth filling in — it is the first
                thing anyone asks if the candidate escalates.
              </p>
            </div>

            {!alreadySuppressed ? (
              <label className="flex items-start gap-2.5 cursor-pointer">
                <Checkbox
                  checked={allProducts}
                  onCheckedChange={(v) => setAllProducts(v === true)}
                  className="mt-0.5"
                />
                <span className="text-sm text-slate-700">
                  They do not want to hear from <strong>any</strong> Hoonr product
                  <span className="block text-xs text-slate-500">
                    Leave unchecked to stop pair&apos;s outreach only. Either way,
                    PAIR Bot will tell you if a channel had to be stopped
                    everywhere — all products currently send from the same phone
                    numbers and email address.
                  </span>
                </span>
              </label>
            ) : null}

            {error ? (
              <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3">
                <AlertTriangle className="w-4 h-4 text-red-700 mt-0.5 shrink-0" />
                <p className="text-sm text-red-900">{error}</p>
              </div>
            ) : null}
          </div>
        )}

        <DialogFooter>
          {result ? (
            <Button onClick={onClose}>Done</Button>
          ) : (
            <>
              <Button variant="outline" onClick={onClose} disabled={submitting}>
                Cancel
              </Button>
              {alreadySuppressed ? (
                <Button
                  onClick={handleResume}
                  disabled={submitting || !hasIdentifier}
                  className="bg-slate-700 hover:bg-slate-800 text-white"
                >
                  {submitting ? (
                    <Loader2 className="w-4 h-4 mr-1.5 animate-spin" />
                  ) : (
                    <RotateCcw className="w-4 h-4 mr-1.5" />
                  )}
                  Resume outreach
                </Button>
              ) : (
                <Button
                  onClick={handleStop}
                  disabled={submitting || !hasIdentifier}
                  className="bg-red-600 hover:bg-red-700 text-white"
                >
                  {submitting ? (
                    <Loader2 className="w-4 h-4 mr-1.5 animate-spin" />
                  ) : (
                    <Ban className="w-4 h-4 mr-1.5" />
                  )}
                  Stop outreach
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
