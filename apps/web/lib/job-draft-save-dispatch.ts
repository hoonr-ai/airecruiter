// Pure decision logic behind saveJobDraft's explicit-vs-auto-save controller
// split (app/jobs/new/page.tsx) — kept here, not inline in that component,
// for unit test coverage without a DOM or React test renderer (PR #809
// review follow-up; same rationale as hooks/use-step-autosave.ts's
// haveDepsChanged/shouldMarkDirty).
//
// Background: saveJobDraft used to share ONE AbortController across every
// caller — the Next button, Save & Exit, and every step's debounced
// auto-save. A background auto-save firing a moment after an explicit save
// already started would abort it, surfacing "Save timed out — please retry"
// even though nothing timed out. The fix keeps separate explicit/auto-save
// controllers: an auto-save never cancels a save the user is actively
// waiting on, while an explicit save still supersedes anything in flight
// (auto or explicit), since it reflects the most current data.

export type SaveKind = "auto" | "explicit";

export interface SaveInFlightState {
  explicitInFlight: boolean;
  autoInFlight: boolean;
}

export interface SaveDispatchDecision {
  // "skip": an explicit save is already in flight for this job — don't
  // race it with a background auto-save. The caller must leave its own
  // dirty bookkeeping alone so the edit is retried later, not dropped.
  // "proceed": start this save, after aborting whatever's named in `abort`.
  action: "skip" | "proceed";
  abort: SaveKind[];
}

export function decideSaveDispatch(kind: SaveKind, state: SaveInFlightState): SaveDispatchDecision {
  if (kind === "auto") {
    if (state.explicitInFlight) return { action: "skip", abort: [] };
    return { action: "proceed", abort: state.autoInFlight ? ["auto"] : [] };
  }
  const abort: SaveKind[] = [];
  if (state.autoInFlight) abort.push("auto");
  if (state.explicitInFlight) abort.push("explicit");
  return { action: "proceed", abort };
}

// Only clears a controller ref if it still points at the settling call's
// own controller. A newer call may already have replaced it (per
// decideSaveDispatch's `abort` list) and started its own save — that
// newer call's in-progress controller must not be wiped out by this older
// call's cleanup running late (e.g. its fetch was already in flight when
// it got aborted, and the rejection is only handled after the newer call
// has taken over).
export function clearIfCurrent<T>(ref: { current: T | null }, ownValue: T): void {
  if (ref.current === ownValue) {
    ref.current = null;
  }
}
