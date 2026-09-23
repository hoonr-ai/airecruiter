import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ACTIVITY_THROTTLE_MS,
  FLUSH_MS,
  IDLE_MS,
  MAX_PENDING_MS,
  MIN_FLUSH_MS,
  SAMPLE_MS,
  createActiveTimeTracker,
  createStepTimeSession,
  type SendOutcome,
  type StepTimeSender,
} from "./step-time.ts";

const SEC = 1_000;
const MIN = 60_000;

describe("step-time active tracker", () => {
  it("exports the agreed timings", () => {
    assert.equal(IDLE_MS, 5 * MIN);
    assert.equal(SAMPLE_MS, 15 * SEC);
    assert.equal(FLUSH_MS, 60 * SEC);
    assert.equal(MIN_FLUSH_MS, 1 * SEC);
    assert.equal(ACTIVITY_THROTTLE_MS, 1 * SEC);
    // Never more than the server accepts in one report.
    assert.equal(MAX_PENDING_MS, 10 * MIN);
  });

  it("counts nothing until the first activity", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.sample(3 * MIN);
    assert.equal(t.take(), 0);
  });

  it("counts visible time after activity, whatever the sampling cadence", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    t.sample(15 * SEC);
    t.sample(30 * SEC);
    t.activity(40 * SEC);
    t.sample(60 * SEC);
    assert.equal(t.peek(), 60 * SEC);
    // Sampling twice at the same instant adds nothing.
    t.sample(60 * SEC);
    assert.equal(t.take(), 60 * SEC);
  });

  it("does not count while hidden", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    t.setVisible(false, 20 * SEC);
    t.sample(2 * MIN);
    assert.equal(t.peek(), 20 * SEC);
    // Back to visible: counting resumes from the moment it became visible,
    // within the idle window of the last activity.
    t.setVisible(true, 2 * MIN);
    t.sample(2 * MIN + 30 * SEC);
    assert.equal(t.take(), 50 * SEC);
  });

  it("starts hidden when told to", () => {
    const t = createActiveTimeTracker({ now: 0, visible: false });
    t.activity(0);
    t.sample(MIN);
    assert.equal(t.take(), 0);
  });

  it("stops counting IDLE_MS after the last activity, not at the next sample", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    t.sample(20 * MIN);
    assert.equal(t.take(), IDLE_MS);
    t.sample(40 * MIN);
    assert.equal(t.take(), 0);
  });

  it("honours a custom idle window", () => {
    const t = createActiveTimeTracker({ now: 0, idleMs: 30 * SEC });
    t.activity(10 * SEC);
    t.sample(2 * MIN);
    assert.equal(t.take(), 30 * SEC);
  });

  it("resumes counting when activity returns after an idle stretch", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    // 12 minutes with no sample at all, then activity: only the first idle
    // window counts, never the whole gap.
    t.activity(12 * MIN);
    assert.equal(t.peek(), IDLE_MS);
    t.sample(12 * MIN + 45 * SEC);
    assert.equal(t.take(), IDLE_MS + 45 * SEC);
  });

  it("activity keeps extending the window", () => {
    const t = createActiveTimeTracker({ now: 0 });
    let taken = 0;
    for (let at = 0; at <= 20 * MIN; at += 4 * MIN) {
      t.activity(at);
      taken += t.take(); // a flush between activities, as the hook does
    }
    t.sample(20 * MIN);
    assert.equal(taken + t.take(), 20 * MIN);
  });

  it("take() returns the pending ms once and resets it", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    t.sample(30 * SEC);
    assert.equal(t.take(), 30 * SEC);
    assert.equal(t.take(), 0);
    assert.equal(t.peek(), 0);
    // Counting continues from where it was accounted, not from zero.
    t.sample(45 * SEC);
    assert.equal(t.take(), 15 * SEC);
  });

  it("returns whole milliseconds for fractional clocks", () => {
    const t = createActiveTimeTracker({ now: 0.25 });
    t.activity(0.25);
    t.sample(1_500.75);
    assert.equal(t.take(), 1_501);
  });

  it("restore() puts a failed chunk back, bounded", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    t.sample(MIN);
    const chunk = t.take();
    t.sample(MIN + 10 * SEC);
    t.restore(chunk);
    assert.equal(t.peek(), MIN + 10 * SEC);
    t.restore(-5);
    t.restore(Number.NaN);
    assert.equal(t.peek(), MIN + 10 * SEC);
    // Sends failing for a long time never grow it past the cap.
    for (let i = 0; i < 50; i++) t.restore(MIN);
    assert.equal(t.take(), MAX_PENDING_MS);
  });

  it("caps pending time accumulated without a flush", () => {
    const t = createActiveTimeTracker({ now: 0 });
    for (let at = 0; at <= 30 * MIN; at += MIN) t.activity(at);
    assert.equal(t.take(), MAX_PENDING_MS);
  });

  it("adds nothing when the clock goes backwards, and never goes negative", () => {
    const t = createActiveTimeTracker({ now: 10 * MIN });
    t.activity(10 * MIN);
    t.sample(10 * MIN + 30 * SEC);
    assert.equal(t.take(), 30 * SEC);
    // The system clock jumps back an hour.
    t.sample(-50 * MIN);
    assert.equal(t.peek(), 0);
    // Counting resumes from the new reading, and the idle window closes when
    // it would have without the jump: 30s were counted before it, so 4m30s
    // are left. The old activity, now "in the future", must not hold the
    // window open for the size of the jump.
    t.sample(-50 * MIN + 20 * MIN);
    assert.equal(t.peek(), IDLE_MS - 30 * SEC);
    t.take();
    t.activity(-20 * MIN);
    t.sample(-20 * MIN + 10 * SEC);
    assert.equal(t.take(), 10 * SEC);
  });

  it("ignores non-finite timestamps", () => {
    const t = createActiveTimeTracker({ now: 0 });
    t.activity(0);
    t.sample(Number.NaN);
    t.activity(Number.POSITIVE_INFINITY);
    t.sample(20 * SEC);
    assert.equal(t.take(), 20 * SEC);
  });
});

// A sender whose every report stays pending until the test settles it.
type Call = { ms: number; keepalive: boolean; settle: (outcome: SendOutcome) => void; fail: (err: unknown) => void };

function fakeSender() {
  const calls: Call[] = [];
  const send: StepTimeSender = (ms, keepalive) =>
    new Promise<SendOutcome>((resolve, reject) => {
      calls.push({ ms, keepalive, settle: resolve, fail: reject });
    });
  return { send, calls };
}

// Let the session's .then/.finally handlers run.
const drain = () => new Promise<void>((resolve) => setImmediate(resolve));

async function settleAll(calls: Call[], outcome: SendOutcome = "sent") {
  for (const c of calls) c.settle(outcome);
  await drain();
}

describe("step-time session", () => {
  it("sends the entry report at once and counts opening the step as activity", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    assert.deepEqual(calls.map((c) => [c.ms, c.keepalive]), [[0, false]]);
    await settleAll(calls);
    for (let at = SAMPLE_MS; at < FLUSH_MS; at += SAMPLE_MS) s.tick(at);
    assert.equal(calls.length, 1, "nothing before the flush interval");
    s.tick(FLUSH_MS);
    assert.deepEqual(calls.map((c) => [c.ms, c.keepalive]), [[0, false], [FLUSH_MS, false]]);
  });

  it("tolerates a timer tick that lands a hair early", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.tick(FLUSH_MS - 500);
    assert.equal(calls.length, 2);
  });

  it("does not stack periodic sends while one is in flight", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    s.tick(FLUSH_MS); // the entry report is still in flight
    assert.equal(calls.length, 1);
    await settleAll(calls);
    s.tick(FLUSH_MS + SAMPLE_MS);
    assert.equal(calls.length, 2);
    // Nothing was lost by waiting: the chunk covers the whole stretch.
    assert.equal(calls[1].ms, FLUSH_MS + SAMPLE_MS);
  });

  it("gives a failed chunk back to the next flush", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.tick(FLUSH_MS);
    calls[1].settle("retry");
    await drain();
    s.tick(2 * FLUSH_MS);
    assert.equal(calls[2].ms, 2 * FLUSH_MS);
  });

  it("treats a rejected send as retryable", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.tick(FLUSH_MS);
    calls[1].fail(new TypeError("Failed to fetch"));
    await drain();
    s.tick(2 * FLUSH_MS);
    assert.equal(calls[2].ms, 2 * FLUSH_MS);
  });

  it("treats a sender that throws synchronously as retryable", async () => {
    let attempt = 0;
    const sent: number[] = [];
    const send: StepTimeSender = (ms) => {
      attempt += 1;
      if (attempt === 2) throw new Error("boom");
      sent.push(ms);
      return Promise.resolve("sent");
    };
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await drain();
    s.tick(FLUSH_MS);
    await drain();
    s.tick(2 * FLUSH_MS);
    assert.deepEqual(sent, [0, 2 * FLUSH_MS]);
  });

  it("forgets a dropped chunk", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.tick(FLUSH_MS);
    calls[1].settle("drop");
    await drain();
    s.tick(2 * FLUSH_MS);
    assert.equal(calls[2].ms, FLUSH_MS);
  });

  it("keeps what it holds bounded while every send fails", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    for (let minute = 1; minute <= 30; minute++) {
      s.activity(minute * MIN);
      s.tick(minute * MIN);
      await settleAll(calls.slice(-1), "retry");
    }
    const sizes = calls.slice(1).map((c) => c.ms);
    assert.equal(sizes.length, 30);
    assert.ok(sizes.every((ms) => ms <= MAX_PENDING_MS));
    assert.equal(sizes[sizes.length - 1], MAX_PENDING_MS);
  });

  it("flushes with keepalive on hide, counts nothing while hidden, and counts coming back as activity", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.visibilityChange(false, 20 * SEC);
    assert.deepEqual([calls[1].ms, calls[1].keepalive], [20 * SEC, true]);
    await settleAll(calls);
    s.tick(2 * MIN);
    s.tick(4 * MIN);
    assert.equal(calls.length, 2, "nothing accrued while hidden");
    // Back after the opening activity's idle window closed: returning to
    // the tab restarts it without a click.
    s.visibilityChange(true, 10 * MIN);
    s.tick(11 * MIN);
    assert.deepEqual([calls[2].ms, calls[2].keepalive], [MIN, false]);
  });

  it("does not send on a hide that follows a flush within SAMPLE_MS", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.tick(FLUSH_MS); // periodic flush
    await settleAll(calls);
    s.visibilityChange(false, FLUSH_MS + 5 * SEC);
    assert.equal(calls.length, 2);
    s.visibilityChange(true, FLUSH_MS + 6 * SEC);
    s.visibilityChange(false, FLUSH_MS + 8 * SEC);
    assert.equal(calls.length, 2);
    // The leftover still goes out with the next periodic flush.
    s.tick(2 * FLUSH_MS);
    assert.equal(calls[2].ms, 5 * SEC + 2 * SEC);
  });

  it("flushes with keepalive on pagehide and keeps counting after a back/forward restore", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.pageHide(30 * SEC);
    assert.deepEqual([calls[1].ms, calls[1].keepalive], [30 * SEC, true]);
    await settleAll(calls);
    s.tick(30 * SEC + FLUSH_MS);
    assert.equal(calls[2].ms, FLUSH_MS);
  });

  it("end() sends the rest with keepalive, then goes quiet", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.end(45 * SEC);
    assert.deepEqual([calls[1].ms, calls[1].keepalive], [45 * SEC, true]);
    // A failure after the end has no session to go back to.
    calls[1].settle("retry");
    await drain();
    s.activity(50 * SEC);
    s.tick(5 * MIN);
    s.pageHide(6 * MIN);
    s.visibilityChange(false, 7 * MIN);
    s.end(8 * MIN);
    assert.equal(calls.length, 2);
  });

  it("does not send less than MIN_FLUSH_MS", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls);
    s.end(MIN_FLUSH_MS - 1);
    assert.equal(calls.length, 1);
  });

  it("re-sends a failed entry on the next tick, even with nothing pending", async () => {
    const { send, calls } = fakeSender();
    // Hidden, so no time accrues: the retry must go out as 0 ms on its own.
    const s = createStepTimeSession({ now: 0, visible: false, send });
    await settleAll(calls, "retry");
    s.tick(SAMPLE_MS);
    assert.deepEqual(calls.map((c) => [c.ms, c.keepalive]), [[0, false], [0, false]]);
    await settleAll(calls.slice(1));
    // Settled: back to the normal rules, and nothing pending means no send.
    for (let at = 2 * SAMPLE_MS; at <= 5 * MIN; at += SAMPLE_MS) s.tick(at);
    assert.equal(calls.length, 2);
  });

  it("a re-sent entry carries the pending time, then the normal cadence resumes", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls, "retry");
    s.tick(SAMPLE_MS);
    assert.equal(calls[1].ms, SAMPLE_MS);
    await settleAll(calls.slice(1));
    for (let at = 2 * SAMPLE_MS; at < SAMPLE_MS + FLUSH_MS; at += SAMPLE_MS) s.tick(at);
    assert.equal(calls.length, 2, "once the entry is in, retries stop");
    s.tick(SAMPLE_MS + FLUSH_MS);
    assert.equal(calls[2].ms, FLUSH_MS);
  });

  it("backs the entry retry off to FLUSH_MS while the server keeps failing", async () => {
    const { send, calls } = fakeSender();
    const sentAt: number[] = [];
    let clockNow = 0;
    const timed: StepTimeSender = (ms, keepalive) => {
      sentAt.push(clockNow);
      return send(ms, keepalive);
    };
    const s = createStepTimeSession({ now: 0, visible: false, send: timed });
    await settleAll(calls, "retry");
    for (clockNow = SAMPLE_MS; clockNow <= 4 * MIN; clockNow += SAMPLE_MS) {
      s.tick(clockNow);
      await settleAll(calls.slice(-1), "retry");
    }
    assert.deepEqual(sentAt, [0, 15 * SEC, 45 * SEC, 105 * SEC, 165 * SEC, 225 * SEC]);
  });

  it("does not duplicate an entry that is still in flight", async () => {
    const { send, calls } = fakeSender();
    // React StrictMode mounts, cleans up and mounts again within a moment:
    // end() must not add a second 0 ms report while the first is out.
    const s = createStepTimeSession({ now: 0, visible: true, send });
    s.end(MIN_FLUSH_MS - 1);
    assert.equal(calls.length, 1);
  });

  it("end() re-sends a failed entry with keepalive", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: true, send });
    await settleAll(calls, "retry");
    // Under MIN_FLUSH_MS, which alone would not be sent: the entry goes out
    // anyway, carrying it.
    s.end(MIN_FLUSH_MS - 1);
    assert.deepEqual([calls[1].ms, calls[1].keepalive], [MIN_FLUSH_MS - 1, true]);
  });

  it("does not re-send a dropped entry", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: false, send });
    await settleAll(calls, "drop");
    for (let at = SAMPLE_MS; at <= 5 * MIN; at += SAMPLE_MS) s.tick(at);
    s.end(5 * MIN);
    assert.equal(calls.length, 1);
  });

  it("starting hidden sends the entry but counts nothing until visible", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 0, visible: false, send });
    assert.equal(calls[0].ms, 0);
    await settleAll(calls);
    s.tick(FLUSH_MS);
    assert.equal(calls.length, 1);
  });

  it("throttles activity to one stamp a second", async () => {
    const throttled = fakeSender();
    const a = createStepTimeSession({ now: 0, visible: true, send: throttled.send });
    await settleAll(throttled.calls);
    a.activity(ACTIVITY_THROTTLE_MS / 2); // inside the throttle: ignored
    a.tick(10 * MIN);
    assert.equal(throttled.calls[1].ms, IDLE_MS);

    const spaced = fakeSender();
    const b = createStepTimeSession({ now: 0, visible: true, send: spaced.send });
    await settleAll(spaced.calls);
    b.activity(ACTIVITY_THROTTLE_MS + 500);
    b.tick(10 * MIN);
    assert.equal(spaced.calls[1].ms, IDLE_MS + ACTIVITY_THROTTLE_MS + 500);
  });

  it("keeps flushing and taking activity after the clock goes backwards", async () => {
    const { send, calls } = fakeSender();
    const s = createStepTimeSession({ now: 10 * MIN, visible: true, send });
    await settleAll(calls);
    s.tick(11 * MIN);
    assert.equal(calls[1].ms, MIN);
    await settleAll(calls);
    s.activity(0); // the clock jumped back 11 minutes
    for (let at = SAMPLE_MS; at <= FLUSH_MS; at += SAMPLE_MS) s.tick(at);
    assert.equal(calls.length, 3, "flushes resume on the new clock");
    assert.equal(calls[2].ms, FLUSH_MS);
  });
});
