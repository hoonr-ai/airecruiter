"use client";

import { cn } from "@/lib/utils";

const MAX_REJECT_REASON_CHANGES = 2;

interface ChangeRejectionReasonButtonProps {
  /** Named in the aria-label so a screen reader announces which row's
   * rejection this changes, not just a bare "Change reason" repeated once
   * per candidate in the table. */
  candidateName?: string;
  disabled?: boolean;
  onClick: () => void;
  className?: string;
  /** How many times the reason has already been changed (from the API's
   * reject_reason_change_count field). Used to show remaining attempts
   * and disable the button once the limit is reached. */
  changeCount?: number;
}

/**
 * The only way to change a candidate's already-recorded rejection reason —
 * Radix's Select only fires onValueChange when the picked value differs
 * from its current controlled value, so re-picking "Reject" from the
 * dropdown while it's already "Reject" is a no-op there. Shared by
 * candidates/page.tsx and jobs/[jobId]/rankings/page.tsx (PR #868 review:
 * this was previously copy-pasted in both).
 *
 * The padding/negative-margin pair below enlarges the actual click target
 * well past the small 10px label (PR #868 review) without shifting the
 * badge stack's layout.
 */
export function ChangeRejectionReasonButton({
  candidateName,
  disabled,
  onClick,
  className,
  changeCount = 0,
}: ChangeRejectionReasonButtonProps) {
  const remaining = Math.max(0, MAX_REJECT_REASON_CHANGES - changeCount);
  const limitReached = remaining === 0;

  return (
    <div className="flex flex-col items-center gap-0.5">
      <button
        type="button"
        disabled={disabled || limitReached}
        onClick={onClick}
        aria-label={
          candidateName
            ? `Change rejection reason for ${candidateName}`
            : "Change rejection reason"
        }
        title={
          limitReached
            ? "Rejection reason change limit reached (2/2)"
            : `Change reason (${remaining}/${MAX_REJECT_REASON_CHANGES} attempt${remaining === 1 ? "" : "s"} remaining)`
        }
        className={cn(
          "-mx-2 -my-0.5 px-2 py-1 rounded text-[10px] font-semibold text-indigo-600 hover:text-indigo-700 underline underline-offset-2 disabled:opacity-40 disabled:cursor-not-allowed disabled:no-underline disabled:text-slate-400",
          className
        )}
      >
        {limitReached ? "Reason locked" : "Change reason"}
      </button>
      <span
        className={cn(
          "text-[9px] font-medium tabular-nums",
          limitReached ? "text-rose-400" : "text-slate-400"
        )}
      >
        {changeCount}/{MAX_REJECT_REASON_CHANGES}
      </span>
    </div>
  );
}
