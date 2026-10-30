"use client";

import { cn } from "@/lib/utils";
import { getRemainingRejectChanges, MAX_REJECT_REASON_CHANGES } from "@/lib/rejection-limit";

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
  changeCount?: number | null;
}

/**
 * The only way to change a candidate's already-recorded rejection reason —
 * Radix's Select only fires onValueChange when the picked value differs
 * from its current controlled value, so re-picking "Reject" from the
 * dropdown while it's already "Reject" is a no-op there. Shared by
 * candidates/page.tsx and jobs/[jobId]/rankings/page.tsx (PR #868 review:
 * this was previously copy-pasted in both).
 */
export function ChangeRejectionReasonButton({
  candidateName,
  disabled,
  onClick,
  className,
  changeCount = 0,
}: ChangeRejectionReasonButtonProps) {
  const { changeCount: count, remaining, limitReached, maxChanges } = getRemainingRejectChanges(
    changeCount,
    MAX_REJECT_REASON_CHANGES
  );

  const buttonLabel = limitReached ? "Reason locked" : "Change reason";
  const ariaLabel = candidateName
    ? `${buttonLabel} for ${candidateName} (${count} of ${maxChanges} changes used)`
    : `${buttonLabel} (${count} of ${maxChanges} changes used)`;

  return (
    <div className="flex flex-col items-center gap-0.5">
      <button
        type="button"
        disabled={disabled || limitReached}
        onClick={onClick}
        aria-label={ariaLabel}
        title={
          limitReached
            ? `Rejection reason change limit reached (${count}/${maxChanges})`
            : `Change reason (${remaining}/${maxChanges} attempt${remaining === 1 ? "" : "s"} remaining)`
        }
        className={cn(
          "-mx-2 -my-1 px-2 py-1 rounded text-[10px] font-semibold text-indigo-600 hover:text-indigo-700 underline underline-offset-2 disabled:opacity-50 disabled:cursor-not-allowed disabled:no-underline disabled:text-slate-500",
          className
        )}
      >
        {buttonLabel}
      </button>
      <span
        aria-hidden="true"
        className={cn(
          "text-[10px] font-medium tabular-nums",
          limitReached ? "text-rose-600" : "text-slate-500"
        )}
      >
        {count}/{maxChanges}
      </span>
    </div>
  );
}
