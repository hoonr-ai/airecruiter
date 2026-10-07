// Pure decision helpers for the job wizard's saveJobDraft abort-coordination
// (apps/web/app/jobs/new/page.tsx). Extracted so the "latest wins" race-
// condition fixes are unit-testable without a DOM/React renderer.

/**
 * Whether an incoming save request should be skipped entirely (never sent),
 * because a user-initiated (manual) save is already in flight. An auto-save
 * must never cancel a manual save — the manual save already covers the same
 * (or newer) form state, and cancelling it would surface a false "Save timed
 * out" and block the step transition.
 */
export function shouldSkipForInFlightManualSave(params: {
  inFlightIsAuto: boolean;
  incomingIsAuto: boolean;
}): boolean {
  return !params.inFlightIsAuto && params.incomingIsAuto;
}

/**
 * Whether a caught fetch abort was caused by being superseded by a newer
 * save request, as opposed to the 20s hard timeout. A superseded abort is
 * neither a real failure nor a timeout — the newer save owns the outcome.
 */
export function isSupersededAbort(isAbortError: boolean, abortReason: unknown): boolean {
  return isAbortError && abortReason === "superseded";
}
