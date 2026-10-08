import assert from "node:assert/strict";
import { test } from "node:test";
import { mergeLaunchSnapshots } from "./live-report-merge.ts";
import type { Snapshot, CandidateRow } from "../app/admin/live-report/types.ts";

const makeCandidate = (
  interviewId: number,
  name: string,
  phase: string,
  events: Array<{ ts: string | null; type: string; subtype: string | null }> = []
): CandidateRow => ({
  interview_id: interviewId,
  name,
  phase,
  outreach_status: "in_progress",
  events: events.map((e) => ({ ...e, phase: null, status: null })),
  call_outcome: null,
  call_outcome_severity: null,
  call_outcome_recognised: false,
  is_stuck: false,
});

test("mergeLaunchSnapshots orders snapshots chronologically and gives precedence to newer launch", () => {
  const olderSnap: Snapshot = {
    bulk_id: "bulk-older",
    created_at: "2026-10-01T10:00:00Z",
    state: "archived",
    jobs: [
      {
        bulk_jd_id: null,
        jobdiva_id: "JD123",
        title: "Older Title",
        customer_name: "Customer A",
        candidates: [
          makeCandidate(101, "Alice", "phase1", [
            { ts: "2026-10-01T10:01:00Z", type: "email_sent", subtype: null },
          ]),
        ],
      },
    ],
    anomalies: [],
  };

  const newerSnap: Snapshot = {
    bulk_id: "bulk-newer",
    created_at: "2026-10-02T10:00:00Z",
    state: "live",
    jobs: [
      {
        bulk_jd_id: null,
        jobdiva_id: "JD123",
        title: "Newer Title",
        customer_name: "Customer A",
        candidates: [
          makeCandidate(101, "Alice", "pass", [
            { ts: "2026-10-02T10:05:00Z", type: "evaluation_completed", subtype: "passed" },
          ]),
          makeCandidate(102, "Bob", "phase2"),
        ],
      },
    ],
    anomalies: [
      {
        kind: "stuck_candidate",
        interview_id: 102,
        jobdiva_id: "JD123",
        phase: "phase2",
        minutes_since_event: 45,
      },
    ],
  };

  // Pass in reverse order to ensure chronological sorting is effective
  const merged = mergeLaunchSnapshots([newerSnap, olderSnap], "JD123");

  assert.equal(merged.bulk_id, "bulk-newer");
  assert.equal(merged.jobs[0].title, "Newer Title");
  assert.equal(merged.jobs[0].candidates.length, 2);

  const alice = merged.jobs[0].candidates.find((c: CandidateRow) => c.interview_id === 101);
  assert.ok(alice);
  assert.equal(alice.phase, "pass", "Newer launch status 'pass' must take precedence over 'phase1'");
  assert.equal(alice.events.length, 2, "Alice should have both chronological events");
  assert.equal(alice.events[0].type, "email_sent");
  assert.equal(alice.events[1].type, "evaluation_completed");

  assert.equal(merged.anomalies.length, 1);
  assert.equal(merged.anomalies[0].interview_id, 102);
});

test("mergeLaunchSnapshots deduplicates events with full (ts|type|subtype) key and Set", () => {
  const snap1: Snapshot = {
    bulk_id: "bulk-1",
    created_at: "2026-10-01T10:00:00Z",
    state: "archived",
    jobs: [
      {
        bulk_jd_id: null,
        jobdiva_id: "JD123",
        title: "Title",
        customer_name: null,
        candidates: [
          makeCandidate(201, "Charlie", "phase1", [
            { ts: "2026-10-01T10:00:00Z", type: "call", subtype: "busy" },
            { ts: "2026-10-01T10:00:00Z", type: "call", subtype: "no_answer" }, // Same ts and type, distinct subtype!
          ]),
        ],
      },
    ],
    anomalies: [],
  };

  const snap2: Snapshot = {
    bulk_id: "bulk-2",
    created_at: "2026-10-01T11:00:00Z",
    state: "archived",
    jobs: [
      {
        bulk_jd_id: null,
        jobdiva_id: "JD123",
        title: "Title",
        customer_name: null,
        candidates: [
          makeCandidate(201, "Charlie", "phase1", [
            { ts: "2026-10-01T10:00:00Z", type: "call", subtype: "busy" }, // Duplicate of snap1
            { ts: "2026-10-01T10:30:00Z", type: "interview_started", subtype: null },
          ]),
        ],
      },
    ],
    anomalies: [],
  };

  const merged = mergeLaunchSnapshots([snap1, snap2], "JD123");
  const charlie = merged.jobs[0].candidates.find((c: CandidateRow) => c.interview_id === 201);
  assert.ok(charlie);
  // Distinct events: (call, busy), (call, no_answer), (interview_started, null)
  assert.equal(charlie.events.length, 3);
});
