import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_COPILOT_PREFS,
  cleanSuggestionBatches,
  handoffOutcomeLabel,
  mergeSuggestions,
  reviewSpeakerLabel,
  describeHandoffError,
  formatElapsed,
  loadCopilotPrefs,
  mergeTranscript,
  parseCopilotPacket,
  saveCopilotPrefs,
  speakerLabel,
  formatSecondsLeft,
  isCandidateParticipant,
  isHandoffOver,
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

  it("lets claim decide when ownership is unknown", () => {
    assert.deepEqual(joinViewFor({ state: "requested", is_your_handoff: null }), { kind: "waiting" });
    assert.equal(joinViewFor({ state: "closed", is_your_handoff: null }).kind, "over");
  });

  it("knows which states are final", () => {
    for (const s of ["closed", "missed", "cancelled"]) assert.equal(isHandoffOver(s), true);
    for (const s of ["requested", "claimed", "joined", null]) assert.equal(isHandoffOver(s), false);
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

describe("copilot helpers", () => {
  it("merges transcript lines once per seq, in seq order", () => {
    const a = [{ role: "candidate", text: "hi", seq: 2 }];
    const merged = mergeTranscript(a, [
      { role: "recruiter", text: "hello", seq: 1 },
      { role: "candidate", text: "hi again", seq: 2 },
      { role: "candidate", text: "x", seq: 3 },
      { role: "candidate", text: "dup in batch", seq: 3 },
    ]);
    assert.deepEqual(merged.map((l) => [l.seq, l.text]), [[1, "hello"], [2, "hi"], [3, "x"]]);
    assert.equal(mergeTranscript(merged, [{ role: "candidate", text: "hi", seq: 2 }]), merged);
  });

  it("parses only on-contract packets", () => {
    assert.deepEqual(parseCopilotPacket('{"type":"transcript","role":"recruiter","text":"Hi","t":1,"seq":4}'),
      { type: "transcript", role: "recruiter", text: "Hi", t: 1, seq: 4 });
    const s = parseCopilotPacket('{"type":"suggestions","id":"s1","items":[{"question":"Q?","why":"W"},{"bad":1}],"t":2}');
    assert.deepEqual(s, { type: "suggestions", id: "s1", items: [{ question: "Q?", why: "W" }], t: 2 });
    assert.equal(parseCopilotPacket("not json"), null);
    assert.equal(parseCopilotPacket('{"type":"copilot_settings","suggestions":true}'), null);
    assert.equal(parseCopilotPacket('{"type":"transcript","text":"no seq"}'), null);
  });

  it("labels speakers from the recruiter's side", () => {
    assert.equal(speakerLabel("recruiter"), "You");
    assert.equal(speakerLabel("candidate"), "Candidate");
    assert.equal(speakerLabel("user"), "Candidate");
    assert.equal(speakerLabel("assistant"), "PAIR");
  });

  it("remembers toggles per recruiter, defaulting both on", () => {
    const mem = new Map<string, string>();
    const storage = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) };
    assert.deepEqual(loadCopilotPrefs(storage, "a@x.com"), DEFAULT_COPILOT_PREFS);
    saveCopilotPrefs(storage, "A@x.com", { showTranscript: false, suggestions: true });
    assert.deepEqual(loadCopilotPrefs(storage, "a@x.com"), { showTranscript: false, suggestions: true });
    assert.deepEqual(loadCopilotPrefs(storage, "b@x.com"), DEFAULT_COPILOT_PREFS);
    const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    assert.deepEqual(loadCopilotPrefs(throwing, "a@x.com"), DEFAULT_COPILOT_PREFS);
    saveCopilotPrefs(throwing, "a@x.com", DEFAULT_COPILOT_PREFS);
    assert.deepEqual(loadCopilotPrefs(null, "a@x.com"), DEFAULT_COPILOT_PREFS);
  });

  it("formats the call timer", () => {
    assert.equal(formatElapsed(0), "0:00");
    assert.equal(formatElapsed(725_000), "12:05");
  });
});

describe("handoff suggestions backfill", () => {
  it("cleans stored batches and drops malformed items", () => {
    const out = cleanSuggestionBatches([
      { id: "a", t: 4, items: [{ question: "Q1", why: "W" }, { why: "no question" }] },
      { id: "b", items: [] },
      null,
      "junk",
    ]);
    assert.deepEqual(out, [{ id: "a", t: 4, items: [{ question: "Q1", why: "W" }] }]);
    assert.deepEqual(cleanSuggestionBatches(undefined), []);
  });

  it("merges by batch id, keeping the newest batches", () => {
    const a = { id: "a", items: [{ question: "Q" }] };
    const b = { id: "b", items: [{ question: "Q" }] };
    const cur = [a];
    assert.equal(mergeSuggestions(cur, [a]), cur);
    assert.deepEqual(mergeSuggestions(cur, [a, b]).map((x) => x.id), ["a", "b"]);
    assert.deepEqual(mergeSuggestions([a], [b], 1).map((x) => x.id), ["b"]);
  });
});

describe("handoff review", () => {
  it("hides the section without a handoff and labels outcomes", () => {
    assert.equal(handoffOutcomeLabel(null), null);
    assert.equal(handoffOutcomeLabel("weird"), null);
    assert.equal(handoffOutcomeLabel("closed", "recruiter_closed"), "Ended by the recruiter");
    assert.match(handoffOutcomeLabel("missed") ?? "", /Missed/);
  });

  it("names the recruiter in a reviewed transcript", () => {
    assert.equal(reviewSpeakerLabel("recruiter"), "Recruiter");
    assert.equal(reviewSpeakerLabel("agent"), "PAIR");
  });
});
