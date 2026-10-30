"use client";

import { getRemainingRejectChanges, MAX_REJECT_REASON_CHANGES } from "@/lib/rejection-limit";

interface RejectChangeLimitNoticeProps {
  changeCount?: number | null;
  maxChanges?: number;
  className?: string;
}

export function RejectChangeLimitNotice({
  changeCount = 0,
  maxChanges = MAX_REJECT_REASON_CHANGES,
  className = "",
}: RejectChangeLimitNoticeProps) {
  const { changeCount: count, remaining, limitReached } = getRemainingRejectChanges(changeCount, maxChanges);

  return (
    <div
      role="status"
      aria-live="polite"
      className={`flex items-center justify-between text-xs rounded-lg px-3 py-2 border ${
        limitReached
          ? "bg-rose-50 text-rose-700 border-rose-200"
          : "bg-amber-50 text-amber-800 border-amber-200"
      } ${className}`}
    >
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="font-semibold">
          {limitReached ? "Change limit reached" : `${remaining} change${remaining === 1 ? "" : "s"} remaining`}
        </span>
        <span className="text-[11px] opacity-80 tabular-nums">
          ({count}/{maxChanges} used)
        </span>
      </div>
      {limitReached ? (
        <span className="text-[11px] text-rose-600 font-medium">Contact admin to modify</span>
      ) : (
        <span className="text-[11px] text-amber-700 font-medium">Max {maxChanges} changes</span>
      )}
    </div>
  );
}
