"use client";

import { cn } from "@/lib/utils";

interface ChangeRejectionReasonButtonProps {
  /** Named in the aria-label so a screen reader announces which row's
   * rejection this changes, not just a bare "Change reason" repeated once
   * per candidate in the table. */
  candidateName?: string;
  disabled?: boolean;
  onClick: () => void;
  className?: string;
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
}: ChangeRejectionReasonButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      aria-label={candidateName ? `Change rejection reason for ${candidateName}` : "Change rejection reason"}
      className={cn(
        "-mx-2 -my-1.5 px-2 py-1.5 rounded text-[10px] font-semibold text-indigo-600 hover:text-indigo-700 underline underline-offset-2 disabled:opacity-50 disabled:cursor-not-allowed",
        className
      )}
    >
      Change reason
    </button>
  );
}
