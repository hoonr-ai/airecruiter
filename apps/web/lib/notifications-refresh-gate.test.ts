import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { createRefreshGate } from "./notifications-refresh-gate.ts";

// A run() whose requests the test finishes by hand.
function controlledRun() {
  const pending: Array<() => void> = [];
  return {
    calls: () => pending.length,
    finish: (i: number) => pending[i](),
    run: () => new Promise<void>((resolve) => pending.push(resolve)),
  };
}

const tick = () => new Promise((r) => setImmediate(r));

describe("createRefreshGate", () => {
  it("overlapping calls share one request", async () => {
    const r = controlledRun();
    const refresh = createRefreshGate(r.run, 10_000, () => 0);
    const a = refresh();
    const b = refresh();
    assert.equal(r.calls(), 1);
    r.finish(0);
    await Promise.all([a, b]);
  });

  it("spaces background calls at least minIntervalMs apart", async () => {
    let t = 0;
    const r = controlledRun();
    const refresh = createRefreshGate(r.run, 10_000, () => t);
    const first = refresh();
    r.finish(0);
    await first;
    t = 9_999;
    await refresh();
    assert.equal(r.calls(), 1);
    t = 10_000;
    void refresh();
    assert.equal(r.calls(), 2);
  });

  it("a forced call skips the spacing", async () => {
    const r = controlledRun();
    const refresh = createRefreshGate(r.run, 10_000, () => 0);
    const first = refresh();
    r.finish(0);
    await first;
    void refresh({ force: true });
    assert.equal(r.calls(), 2);
  });

  it("a forced call during a request runs one more after it, not merged into it", async () => {
    // The in-flight request may have read the server before the caller's own
    // change was saved, so the forced one needs a fresh read of its own.
    const r = controlledRun();
    const refresh = createRefreshGate(r.run, 10_000, () => 0);
    void refresh();
    const forcedA = refresh({ force: true });
    const forcedB = refresh({ force: true });
    assert.equal(r.calls(), 1);
    r.finish(0);
    await tick();
    assert.equal(r.calls(), 2, "one follow-up, shared by both forced callers");
    r.finish(1);
    await Promise.all([forcedA, forcedB]);
    assert.equal(r.calls(), 2);
  });

  it("a failed request doesn't block the next one", async () => {
    let fail = true;
    let runs = 0;
    const refresh = createRefreshGate(async () => {
      runs++;
      if (fail) throw new Error("boom");
    }, 10_000, () => 0);
    await refresh();
    fail = false;
    await refresh({ force: true });
    assert.equal(runs, 2);
  });
});
