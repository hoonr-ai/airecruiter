// Pulled out of context/notifications-context.tsx (a .tsx file, which Node's
// native TS loader can't import directly — it only strips types, it doesn't
// transpile JSX) so this decision has unit coverage without a renderer.

/** Whether a refresh()'s GET response is still safe to apply. False in
 * either of two cases (PR #831 review): a local mutation (markRead/
 * markUnread/markAllRead/clearAll) started after this fetch began — caught
 * by the version no longer matching what was captured at fetch-start; or a
 * mutation's own request is still in flight — its POST may not have
 * committed yet, so even a GET that started after the mutation's optimistic
 * update can still read pre-commit data. */
export function shouldApplyRefreshResult(
  versionAtFetchStart: number,
  currentVersion: number,
  inFlightMutationCount: number
): boolean {
  return versionAtFetchStart === currentVersion && inFlightMutationCount === 0;
}
