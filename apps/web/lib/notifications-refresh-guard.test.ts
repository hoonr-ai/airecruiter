import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { shouldApplyRefreshResult } from "./notifications-refresh-guard.ts";

// PR #831 review: locks in the exact decision that stops a racing refresh()
// (in context/notifications-context.tsx) from reverting an optimistic
// markRead/markUnread/markAllRead/clearAll.
describe("shouldApplyRefreshResult", () => {
  it("applies when nothing local changed and no mutation is in flight", () => {
    assert.equal(shouldApplyRefreshResult(0, 0, 0), true);
  });

  it("rejects when a mutation started after the fetch began (version moved on)", () => {
    // The fetch captured version 0 at its own start; a markRead that ran
    // while it was in flight bumped the live version to 1 before the fetch
    // resolved — applying this response would silently undo that markRead.
    assert.equal(shouldApplyRefreshResult(0, 1, 0), false);
  });

  it("rejects while a mutation's own request is still in flight, even with no version drift", () => {
    // The gap the version check alone misses: a refresh that starts AFTER a
    // mutation's optimistic update (so it captures the already-bumped
    // version) can still have its GET land before that mutation's own POST
    // commits. The version matches exactly, but the in-flight count doesn't.
    assert.equal(shouldApplyRefreshResult(1, 1, 1), false);
  });

  it("applies once the in-flight mutation has settled and nothing newer started", () => {
    assert.equal(shouldApplyRefreshResult(1, 1, 0), true);
  });

  it("rejects when both a version drift and an in-flight mutation are present", () => {
    assert.equal(shouldApplyRefreshResult(0, 2, 1), false);
  });
});
