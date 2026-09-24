// Active-time accounting for a wizard step: the number behind the reports'
// "Step 5 Active Time" (apps/api/services/job_step_time.py).
//
// "Active" means the tab is visible AND the recruiter did something (clicked,
// typed, scrolled, moved the mouse) within the last IDLE_MS. A Step 5 left
// open in a background tab, or on a screen nobody is at, must not count as
// work. After the last activity we still count up to IDLE_MS, not beyond: a
// recruiter reading a candidate list without touching anything is working,
// and IDLE_MS is how long we give them the benefit of the doubt.
//
// Two layers, both pure: no DOM, no timers, no clock of their own. The caller
// passes `now` to every call, and hooks/use-step-active-time.ts wires the
// browser events and the network to them. That keeps every rule here
// unit-testable (lib/step-time.test.ts).
//
//   createActiveTimeTracker  what counts as active time.
//   createStepTimeSession    when that time is sent, and what happens to it
//                            when a send fails.

export const IDLE_MS = 5 * 60_000;
// How often the hook samples. It bounds how stale the pending total can be
// when a flush happens, not what gets counted: time is computed from
// timestamps, never by adding up ticks.
export const SAMPLE_MS = 15_000;
export const FLUSH_MS = 60_000;
// Below this a flush is skipped (the time stays pending), so tab flicking and
// the unmount of a step opened for a moment don't turn into requests.
export const MIN_FLUSH_MS = 1_000;
// The server clamps a single report to 10 minutes
// (services/job_step_time.MAX_ACTIVE_MS_PER_REPORT). Holding more than that
// while sends keep failing would only be thrown away there, so it's the
// ceiling on pending time here too.
export const MAX_PENDING_MS = 10 * 60_000;
// One activity stamp per second is all the idle rule needs. mousemove and
// scroll fire far more often than that.
export const ACTIVITY_THROTTLE_MS = 1_000;
// Interval ticks land a little late, never early. Without this slack the
// fourth 15s tick can miss 60s by a hair and push every flush to 75s.
const FLUSH_SLACK_MS = 1_000;

export type ActiveTimeTracker = {
  /** The recruiter did something at `now`. */
  activity(now: number): void;
  /** The tab became visible or hidden at `now`. */
  setVisible(visible: boolean, now: number): void;
  /** Count the active time up to `now` into the pending total. */
  sample(now: number): void;
  /** Pending ms, without taking it. */
  peek(): number;
  /** Return the pending ms (whole) and reset it to 0. */
  take(): number;
  /** Put back ms whose send failed, so it goes out with the next flush. */
  restore(ms: number): void;
};

export type ActiveTimeTrackerOptions = {
  now: number;
  idleMs?: number;
  visible?: boolean;
  maxPendingMs?: number;
};

export function createActiveTimeTracker({
  now,
  idleMs = IDLE_MS,
  visible = true,
  maxPendingMs = MAX_PENDING_MS,
}: ActiveTimeTrackerOptions): ActiveTimeTracker {
  let isVisible = visible;
  // null until the first activity: presence alone is not activity.
  let lastActivity: number | null = null;
  // Time before this has been accounted for (counted or deliberately not).
  let accountedTo = now;
  let pending = 0;

  const bound = (ms: number) => Math.min(Math.max(ms, 0), maxPendingMs);

  const sample = (at: number) => {
    if (!Number.isFinite(at)) return;
    if (at < accountedTo) {
      // The clock went backwards (a system clock change). Count nothing for
      // the jump and carry on from the new reading. The last activity moves
      // with it, keeping its distance behind: left where it was, it would
      // sit "in the future" and hold the idle window open for the size of
      // the jump. (lastActivity <= accountedTo always: activity() samples
      // before it stamps.)
      if (lastActivity !== null) lastActivity = at - Math.max(0, accountedTo - lastActivity);
      accountedTo = at;
      return;
    }
    if (isVisible && lastActivity !== null) {
      // Active until IDLE_MS after the last activity and no further. This is
      // also what bounds a huge gap between samples (a laptop that slept, a
      // throttled background timer): at most idleMs from one sample.
      const end = Math.min(at, lastActivity + idleMs);
      if (end > accountedTo) pending = bound(pending + (end - accountedTo));
    }
    accountedTo = at;
  };

  return {
    activity(at) {
      // Account for the stretch BEFORE this activity under the old state
      // first. Stamping the activity first would count a whole idle gap
      // as active.
      sample(at);
      if (Number.isFinite(at)) lastActivity = at;
    },
    setVisible(nextVisible, at) {
      sample(at);
      isVisible = nextVisible;
    },
    sample,
    peek() {
      return Math.round(pending);
    },
    take() {
      const ms = Math.round(pending);
      pending = 0;
      return ms;
    },
    restore(ms) {
      if (!Number.isFinite(ms) || ms <= 0) return;
      pending = bound(pending + ms);
    },
  };
}

// What became of one report. "retry" puts its time back for the next flush
// (a network failure, a 5xx, the server's 200 {"status": "error"}). "drop"
// forgets it (a 4xx the server would refuse again).
export type SendOutcome = "sent" | "retry" | "drop";
export type StepTimeSender = (activeMs: number, keepalive: boolean) => Promise<SendOutcome>;

export type StepTimeSession = {
  /** The recruiter did something. Throttled to one stamp a second. */
  activity(now: number): void;
  /** The page became visible or hidden. */
  visibilityChange(visible: boolean, now: number): void;
  /** The periodic timer (every SAMPLE_MS). */
  tick(now: number): void;
  /** The page is going away (tab close, navigation, back/forward cache). */
  pageHide(now: number): void;
  /** The session is over: left the step, switched job, unmounted. */
  end(now: number): void;
};

export type StepTimeSessionOptions = {
  now: number;
  visible: boolean;
  send: StepTimeSender;
  idleMs?: number;
};

/**
 * One recruiter's time on one (job, step), from entering it to leaving it.
 * Starting it sends the ENTRY report (0 ms). The server's first report for a
 * recruiter creates their row, which stamps first_entered_at, the start of
 * "Step 5 → Launch". Opening the step counts as activity.
 *
 * A failed entry is sent again, sooner than a chunk of time would be: after
 * SAMPLE_MS, then twice that, then every FLUSH_MS, carrying whatever time is
 * pending, even 0 ms. The stamp lands when the first report gets through, so
 * every second of delay is cut off "Step 5 → Launch", and a launch that beats
 * it leaves the job with no figure at all.
 */
export function createStepTimeSession({
  now,
  visible,
  send,
  idleMs = IDLE_MS,
}: StepTimeSessionOptions): StepTimeSession {
  const tracker = createActiveTimeTracker({ now, idleMs, visible });
  tracker.activity(now);
  let lastActivityAt = now;
  let lastFlushAt = now;
  let inFlight = 0;
  let ended = false;
  // The entry has no time to restore when it fails; it has to go out again.
  // Any report the server takes creates the row, so the first "sent" settles
  // it, and so does a "drop" (the server would refuse it again). Until then,
  // failures back the retry off.
  let entrySettled = false;
  let entryFailures = 0;

  const dispatch = (activeMs: number, keepalive: boolean) => {
    // take() already moved this chunk out of the tracker, so no other send
    // can carry it. On "retry" it goes back for the next flush, and the
    // tracker's cap keeps repeated failures bounded. Once the session has
    // ended nothing will flush it again, so there is nothing to give it
    // back to.
    inFlight += 1;
    let outcome: Promise<SendOutcome>;
    try {
      outcome = Promise.resolve(send(activeMs, keepalive));
    } catch {
      outcome = Promise.resolve("retry");
    }
    outcome
      .catch((): SendOutcome => "retry")
      .then((result) => {
        if (result !== "retry") entrySettled = true;
        else if (!entrySettled) entryFailures += 1;
        if (result === "retry" && activeMs > 0 && !ended) tracker.restore(activeMs);
      })
      .finally(() => {
        inFlight -= 1;
      });
  };

  // activity, visibilityChange and tick start here (pageHide and end flush,
  // which resets the cadence anyway). A clock that went backwards restarts
  // the flush cadence from the new reading; it would otherwise stall until
  // the old reading came round again. The tracker copes with the jump
  // itself, and activity() lets a backwards gap through its throttle.
  const observe = (at: number) => {
    if (at < lastFlushAt) lastFlushAt = at;
  };

  // The entry failed and no report is out that could still stamp the row.
  // (Nothing in flight means every send so far has settled, and none as
  // "sent" or "drop".) While a send is out, a second 0 ms report would only
  // duplicate it.
  const entryDue = () => !entrySettled && inFlight === 0;

  // The periodic cadence: FLUSH_MS, or the entry's backed-off retry.
  const flushInterval = () =>
    entrySettled ? FLUSH_MS : Math.min(SAMPLE_MS * 2 ** Math.max(0, entryFailures - 1), FLUSH_MS);

  const flush = (at: number, keepalive: boolean) => {
    tracker.sample(at);
    lastFlushAt = at;
    // Under MIN_FLUSH_MS there is no time worth a request, but an entry that
    // is due is worth one on its own.
    if (tracker.peek() < MIN_FLUSH_MS && !entryDue()) return;
    dispatch(tracker.take(), keepalive);
  };

  dispatch(0, false);

  return {
    activity(at) {
      if (ended) return;
      observe(at);
      const since = at - lastActivityAt;
      // A negative gap is a clock that went backwards, not a burst.
      if (since >= 0 && since < ACTIVITY_THROTTLE_MS) return;
      lastActivityAt = at;
      tracker.activity(at);
    },
    visibilityChange(nextVisible, at) {
      if (ended) return;
      observe(at);
      tracker.setVisible(nextVisible, at);
      if (nextVisible) {
        // Coming back to the tab is presence, even before the first click.
        tracker.activity(at);
        lastActivityAt = at;
        return;
      }
      // Hidden is often the last event a closing tab or a backgrounded
      // mobile browser gets to run, so send now. The exception is a flush
      // that went out moments ago. nginx rate-limits every API route per
      // client IP (api_limit, 30 requests a minute, nginx-upstreams.conf),
      // and a recruiter flicking between PAIR and JobDiva must not spend
      // that budget on telemetry. The timer keeps ticking while hidden, so
      // what is left still goes out within the minute, and a closing tab
      // still gets pageHide.
      if (at - lastFlushAt >= SAMPLE_MS) flush(at, true);
    },
    tick(at) {
      if (ended) return;
      observe(at);
      tracker.sample(at);
      // The periodic flush waits for the previous send, so a slow server
      // doesn't get a pile of requests. The flushes on hide, pagehide and
      // end don't wait: they may be the last chance to send, and they
      // carry a different chunk.
      if (inFlight > 0 || at - lastFlushAt < flushInterval() - FLUSH_SLACK_MS) return;
      flush(at, false);
    },
    pageHide(at) {
      // The session stays open: a page restored from the back/forward cache
      // carries on counting.
      if (!ended) flush(at, true);
    },
    end(at) {
      if (ended) return;
      // keepalive, because leaving the step may be a navigation.
      flush(at, true);
      ended = true;
    },
  };
}
