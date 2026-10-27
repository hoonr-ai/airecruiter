// Pulled out of context/notifications-context.tsx (same reason as
// notifications-refresh-guard.ts: Node's test runner can't import .tsx) so
// the merge/spacing/force rules below have unit coverage.
//
// refresh() is triggered from many places at once — mount, tab focus, stream
// reconnect, the heartbeat watchdog, the retry timer — so overlapping calls
// share one request and background ones are spaced at least `minIntervalMs`
// apart. `force` (user actions and mutation follow-ups) skips the spacing; if
// a request is already in flight it runs one more right after it, since that
// one may have read the server before the caller's own change committed.

export type RefreshFn = (opts?: { force?: boolean }) => Promise<void>;

export function createRefreshGate(
  run: () => Promise<void>,
  minIntervalMs: number,
  now: () => number = Date.now
): RefreshFn {
  let inFlight: Promise<void> | null = null;
  let queuedForced: Promise<void> | null = null;
  let lastStartedAt = -Infinity;

  const refresh: RefreshFn = (opts) => {
    if (inFlight) {
      if (!opts?.force) return inFlight;
      queuedForced ??= inFlight.then(() => {
        queuedForced = null;
        return refresh({ force: true });
      });
      return queuedForced;
    }
    if (!opts?.force && now() - lastStartedAt < minIntervalMs) return Promise.resolve();
    lastStartedAt = now();
    inFlight = run()
      .catch(() => {})
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };
  return refresh;
}
