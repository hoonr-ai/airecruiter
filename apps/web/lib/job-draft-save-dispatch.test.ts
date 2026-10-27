import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { decideSaveDispatch, clearIfCurrent } from "./job-draft-save-dispatch.ts";

describe("decideSaveDispatch", () => {
  it("an auto-save proceeds when nothing is in flight", () => {
    const decision = decideSaveDispatch("auto", { explicitInFlight: false, autoInFlight: false });
    assert.deepEqual(decision, { action: "proceed", abort: [] });
  });

  it("an auto-save queues behind an in-flight auto-save instead of aborting it", () => {
    // Aborting only cancels the browser side; the server would still run both
    // saves of the same job, one waiting on the other's row lock.
    const decision = decideSaveDispatch("auto", { explicitInFlight: false, autoInFlight: true });
    assert.deepEqual(decision, { action: "queue", abort: [] });
  });

  it("the page-hide (keepalive) auto-save can't wait, so it still aborts the in-flight one", () => {
    const decision = decideSaveDispatch("auto", { explicitInFlight: false, autoInFlight: true }, { keepalive: true });
    assert.deepEqual(decision, { action: "proceed", abort: ["auto"] });
  });

  it("an auto-save is skipped (not raced) while an explicit save is in flight — the bug this fixes", () => {
    const decision = decideSaveDispatch("auto", { explicitInFlight: true, autoInFlight: false });
    assert.deepEqual(decision, { action: "skip", abort: [] });
  });

  it("an auto-save is still skipped even if an auto-save also happens to be in flight", () => {
    const decision = decideSaveDispatch("auto", { explicitInFlight: true, autoInFlight: true });
    assert.equal(decision.action, "skip");
  });

  it("an explicit save proceeds when nothing is in flight", () => {
    const decision = decideSaveDispatch("explicit", { explicitInFlight: false, autoInFlight: false });
    assert.deepEqual(decision, { action: "proceed", abort: [] });
  });

  it("an explicit save aborts a stale in-flight auto-save", () => {
    const decision = decideSaveDispatch("explicit", { explicitInFlight: false, autoInFlight: true });
    assert.deepEqual(decision, { action: "proceed", abort: ["auto"] });
  });

  it("an explicit save aborts a stale in-flight explicit save (e.g. a double-click)", () => {
    const decision = decideSaveDispatch("explicit", { explicitInFlight: true, autoInFlight: false });
    assert.deepEqual(decision, { action: "proceed", abort: ["explicit"] });
  });

  it("an explicit save aborts both kinds when both are in flight", () => {
    const decision = decideSaveDispatch("explicit", { explicitInFlight: true, autoInFlight: true });
    assert.deepEqual(decision, { action: "proceed", abort: ["auto", "explicit"] });
  });
});

describe("clearIfCurrent", () => {
  it("clears the ref when it still points at the settling call's own value", () => {
    const ref = { current: "controller-a" };
    clearIfCurrent(ref, "controller-a");
    assert.equal(ref.current, null);
  });

  it("leaves the ref alone when a newer call already replaced it (out-of-order settling)", () => {
    // Simulates: call A starts (ref = A), call B supersedes it (ref = B),
    // then A's aborted fetch finally rejects and A's cleanup runs late —
    // it must not null out B's still-in-progress controller.
    const ref: { current: string | null } = { current: "controller-a" };
    ref.current = "controller-b"; // B supersedes A
    clearIfCurrent(ref, "controller-a"); // A's cleanup, running late
    assert.equal(ref.current, "controller-b");
  });

  it("a no-op on an already-cleared ref", () => {
    const ref: { current: string | null } = { current: null };
    clearIfCurrent(ref, "controller-a");
    assert.equal(ref.current, null);
  });
});
