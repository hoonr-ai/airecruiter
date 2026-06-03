"use client";

import { CheckCircle2, AlertCircle, Loader2, Rocket } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

export type BatchStage = "save" | "engage";
export type BatchStatus =
  | "pending"
  | "saving"
  | "engaging"
  | "completed"
  | "failed";

export interface LaunchBatchInfo {
  index: number;
  size: number;
  status: BatchStatus;
  message?: string;
  savedCount: number;
  dncSkipped: number;
  engageSent: number;
  alreadySent: number;
  errorMessage?: string;
}

export type LaunchPhase =
  | "idle"
  | "enriching"
  | "launching"
  | "completed"
  | "failed";

export interface LaunchPairProgress {
  open: boolean;
  phase: LaunchPhase;
  totalCandidates: number;
  batchSize: number;
  // Enrichment
  enrichTotal: number;
  enrichDone: number;
  enrichSucceeded: number;
  enrichMissingLinkedIn: number;
  enrichNoContact: number;
  enrichFailed: number;
  // Hard-filter (0% match) skips — not launched, reported at the end
  hardFilterSkipped: number;
  hardFilterSkippedNames: string[];
  // Batched launch
  batches: LaunchBatchInfo[];
  currentBatchIndex: number;
  totalSaved: number;
  totalEngaged: number;
  totalFailedBatches: number;
  // Finalization
  finalMessage?: string;
}

export const initialLaunchProgress: LaunchPairProgress = {
  open: false,
  phase: "idle",
  totalCandidates: 0,
  batchSize: 15,
  enrichTotal: 0,
  enrichDone: 0,
  enrichSucceeded: 0,
  enrichMissingLinkedIn: 0,
  enrichNoContact: 0,
  enrichFailed: 0,
  hardFilterSkipped: 0,
  hardFilterSkippedNames: [],
  batches: [],
  currentBatchIndex: -1,
  totalSaved: 0,
  totalEngaged: 0,
  totalFailedBatches: 0,
};

interface LaunchPairProgressModalProps {
  progress: LaunchPairProgress;
  onClose: () => void;
}

function StageIcon({ status }: { status: BatchStatus }) {
  if (status === "completed") {
    return <CheckCircle2 className="w-4 h-4 text-emerald-600" />;
  }
  if (status === "failed") {
    return <AlertCircle className="w-4 h-4 text-rose-600" />;
  }
  if (status === "saving" || status === "engaging") {
    return <Loader2 className="w-4 h-4 text-[#6366f1] animate-spin" />;
  }
  return <div className="w-4 h-4 rounded-full border-2 border-slate-200" />;
}

function batchLabel(b: LaunchBatchInfo): string {
  switch (b.status) {
    case "saving":
      return "Saving…";
    case "engaging":
      return "Sending interview links…";
    case "completed":
      return `Saved ${b.savedCount} · Sent ${b.engageSent}${
        b.alreadySent ? ` · ${b.alreadySent} already sent` : ""
      }${b.dncSkipped ? ` · ${b.dncSkipped} DNC` : ""}`;
    case "failed":
      return b.errorMessage || "Failed";
    default:
      return "Pending";
  }
}

export function LaunchPairProgressModal({
  progress,
  onClose,
}: LaunchPairProgressModalProps) {
  const {
    open,
    phase,
    totalCandidates,
    batchSize,
    enrichTotal,
    enrichDone,
    enrichSucceeded,
    enrichMissingLinkedIn,
    enrichNoContact,
    enrichFailed,
    hardFilterSkipped,
    hardFilterSkippedNames,
    batches,
    totalSaved,
    totalEngaged,
    totalFailedBatches,
    finalMessage,
  } = progress;

  const isDone = phase === "completed" || phase === "failed";
  const totalBatches = batches.length;
  const completedBatches = batches.filter(
    (b) => b.status === "completed" || b.status === "failed",
  ).length;
  const overallPct =
    totalBatches > 0
      ? Math.round((completedBatches / totalBatches) * 100)
      : phase === "enriching" && enrichTotal > 0
        ? Math.round((enrichDone / enrichTotal) * 100)
        : 0;

  const enrichPct =
    enrichTotal > 0 ? Math.round((enrichDone / enrichTotal) * 100) : 0;

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && isDone) onClose(); }}>
      <DialogContent
        className="max-w-2xl"
        onPointerDownOutside={(e) => { if (!isDone) e.preventDefault(); }}
        onEscapeKeyDown={(e) => { if (!isDone) e.preventDefault(); }}
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Rocket className="w-5 h-5 text-[#6366f1]" />
            {phase === "completed"
              ? "PAIR launched"
              : phase === "failed"
                ? "PAIR launch finished with issues"
                : "Launching PAIR…"}
          </DialogTitle>
          <DialogDescription>
            {phase === "enriching" && (
              <>Enriching contact details for selected candidates…</>
            )}
            {phase === "launching" && (
              <>
                Processing {totalCandidates} candidate
                {totalCandidates === 1 ? "" : "s"} in batches of {batchSize}.
              </>
            )}
            {phase === "completed" && (
              <>
                Saved {totalSaved} · Engaged {totalEngaged}
                {totalFailedBatches > 0
                  ? ` · ${totalFailedBatches} batch${
                      totalFailedBatches === 1 ? "" : "es"
                    } failed`
                  : ""}
                {hardFilterSkipped > 0
                  ? ` · ${hardFilterSkipped} skipped`
                  : ""}
                .
              </>
            )}
            {phase === "failed" && (
              <>{finalMessage || "Some batches did not complete successfully."}</>
            )}
          </DialogDescription>
        </DialogHeader>

        {/* Overall progress bar */}
        <div className="space-y-2">
          <div className="flex items-center justify-between text-[12px] font-medium text-slate-500">
            <span>Overall progress</span>
            <span>{overallPct}%</span>
          </div>
          <div className="h-2 w-full rounded-full bg-slate-100 overflow-hidden">
            <div
              className={`h-full transition-all ${
                phase === "failed" ? "bg-rose-500" : "bg-[#6366f1]"
              }`}
              style={{ width: `${overallPct}%` }}
            />
          </div>
        </div>

        {/* Enrichment section */}
        {enrichTotal > 0 && (
          <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3 space-y-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-[13px] font-semibold text-slate-700">
                {phase === "enriching" ? (
                  <Loader2 className="w-4 h-4 text-[#6366f1] animate-spin" />
                ) : (
                  <CheckCircle2 className="w-4 h-4 text-emerald-600" />
                )}
                Contact enrichment
              </div>
              <span className="text-[12px] font-medium text-slate-500">
                {enrichDone}/{enrichTotal} ({enrichPct}%)
              </span>
            </div>
            <div className="h-1.5 w-full rounded-full bg-white overflow-hidden border border-slate-100">
              <div
                className="h-full bg-emerald-500 transition-all"
                style={{ width: `${enrichPct}%` }}
              />
            </div>
            {(enrichSucceeded > 0 ||
              enrichMissingLinkedIn > 0 ||
              enrichNoContact > 0 ||
              enrichFailed > 0) && (
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-slate-600">
                {enrichSucceeded > 0 && (
                  <span className="text-emerald-700 font-medium">
                    ✓ {enrichSucceeded} enriched
                  </span>
                )}
                {enrichMissingLinkedIn > 0 && (
                  <span>{enrichMissingLinkedIn} missing LinkedIn</span>
                )}
                {enrichNoContact > 0 && (
                  <span>{enrichNoContact} no contact found</span>
                )}
                {enrichFailed > 0 && (
                  <span className="text-rose-600">{enrichFailed} failed</span>
                )}
              </div>
            )}
          </div>
        )}

        {/* Hard-filter (0% match) skips */}
        {hardFilterSkipped > 0 && (
          <div className="rounded-lg border border-amber-200 bg-amber-50/70 p-3 space-y-2">
            <div className="flex items-center gap-2 text-[13px] font-semibold text-amber-800">
              <AlertCircle className="w-4 h-4 text-amber-600" />
              {hardFilterSkipped} candidate{hardFilterSkipped === 1 ? "" : "s"} skipped — hard filter failed (0% match)
            </div>
            {hardFilterSkippedNames.length > 0 && (
              <div className="text-[12px] text-amber-700">
                {hardFilterSkippedNames.slice(0, 5).join(", ")}
                {hardFilterSkippedNames.length > 5
                  ? ` +${hardFilterSkippedNames.length - 5} more`
                  : ""}
              </div>
            )}
          </div>
        )}

        {/* Batch list */}
        {batches.length > 0 && (
          <div className="rounded-lg border border-slate-200 max-h-[280px] overflow-y-auto">
            <div className="sticky top-0 bg-slate-50 border-b border-slate-200 px-3 py-2 flex items-center justify-between text-[12px] font-semibold text-slate-600">
              <span>Batches ({batchSize} candidates each)</span>
              <span>
                {completedBatches}/{totalBatches}
              </span>
            </div>
            <ul className="divide-y divide-slate-100">
              {batches.map((b) => (
                <li
                  key={b.index}
                  className="px-3 py-2 flex items-center gap-3 text-[13px]"
                >
                  <StageIcon status={b.status} />
                  <span className="font-semibold text-slate-700 w-20 shrink-0">
                    Batch {b.index + 1}
                  </span>
                  <span className="text-slate-500 w-20 shrink-0">
                    {b.size} cand.
                  </span>
                  <span
                    className={`flex-1 truncate ${
                      b.status === "failed"
                        ? "text-rose-600"
                        : b.status === "completed"
                          ? "text-slate-600"
                          : "text-slate-500"
                    }`}
                  >
                    {batchLabel(b)}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        <DialogFooter>
          <Button
            disabled={!isDone}
            onClick={onClose}
            className="bg-[#6366f1] hover:bg-[#4f46e5] text-white"
          >
            {isDone ? "Close" : "Working…"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
