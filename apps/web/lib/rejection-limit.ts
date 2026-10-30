export const MAX_REJECT_REASON_CHANGES = 2;

export function safeRejectReasonChangeCount(value: unknown): number {
  const num = Number(value);
  return Number.isFinite(num) && num > 0 ? Math.floor(num) : 0;
}

export function getRemainingRejectChanges(changeCount: unknown, maxChanges = MAX_REJECT_REASON_CHANGES): {
  changeCount: number;
  maxChanges: number;
  remaining: number;
  limitReached: boolean;
} {
  const count = safeRejectReasonChangeCount(changeCount);
  const remaining = Math.max(0, maxChanges - count);
  return {
    changeCount: count,
    maxChanges,
    remaining,
    limitReached: remaining === 0,
  };
}
