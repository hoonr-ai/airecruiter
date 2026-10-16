import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  describeHandoffError,
  formatSecondsLeft,
  isCandidateParticipant,
  joinViewFor,
} from "./handoff.ts";

describe("isCandidateParticipant", () => {
  it("is the candidate, never the bot, its avatar or a recruiter", () => {
    assert.equal(isCandidateParticipant({ identity: "candidate_4242", isAgent: false }), true);
    assert.equal(isCandidateParticipant({ identity: "sip_+15550001111", isAgent: false }), true);
    assert.equal(isCandidateParticipant({ identity: "agent-AJ_x", isAgent: true }), false);
    assert.equal(isCandidateParticipant({ identity: "recruiter_4242_ab12cd", isAgent: false }), false);
    assert.equal(isCandidateParticipant({ identity: "", isAgent: false }), false);
  });
});

describe("joinViewFor", () => {
  it("lets the launch's recruiter join while the candidate waits, and rejoin after a drop", () => {
    for (const s of ["requested", "claimed", "joined"]) {
      assert.deepEqual(joinViewFor({ state: s, is_your_handoff: true }), { kind: "waiting" });
    }
  });

  it("tells another recruiter it isn't theirs", () => {
    assert.deepEqual(joinViewFor({ state: "requested", is_your_handoff: false }), { kind: "not_yours" });
  });

  it("explains each way it can be over", () => {
    assert.match((joinViewFor({ state: "missed" }) as { reason: string }).reason, /in time/);
    assert.match((joinViewFor({ state: "cancelled" }) as { reason: string }).reason, /left/);
    assert.match((joinViewFor({ state: "closed" }) as { reason: string }).reason, /ended/);
  });

  it("has nothing to join without a handoff", () => {
    assert.deepEqual(joinViewFor({ state: null }), { kind: "none" });
  });
});

describe("describeHandoffError", () => {
  it("maps Pair-bot's refusals to plain messages", () => {
    assert.match(describeHandoffError({ status: 403 }), /launched this interview/);
    assert.match(describeHandoffError({ status: 409 }), /isn't waiting/);
    assert.match(describeHandoffError({ status: 410 }), /no longer waiting/);
    assert.match(describeHandoffError({ status: 502 }), /can't be reached/);
    assert.equal(describeHandoffError(new Error("boom")), "boom");
  });
});

describe("formatSecondsLeft", () => {
  it("counts down in m:ss and never below zero", () => {
    const now = Date.parse("2026-10-16T14:00:00Z");
    assert.equal(formatSecondsLeft("2026-10-16T14:04:05Z", now), "4:05");
    assert.equal(formatSecondsLeft("2026-10-16T13:59:00Z", now), "0:00");
    assert.equal(formatSecondsLeft(null, now), null);
    assert.equal(formatSecondsLeft("not a date", now), null);
  });
});
