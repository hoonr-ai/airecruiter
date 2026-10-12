import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { matchesNotificationFilter, NOTIFICATION_FILTERS } from "./notification-filters.ts";

const unreadCandidate = { read_at: null, candidate_id: "C1" };
const readCandidate = { read_at: "2025-01-01T00:00:00Z", candidate_id: "C1" };
const unreadJob = { read_at: null, candidate_id: null };
const readJob = { read_at: "2025-01-01T00:00:00Z", candidate_id: null };

describe("NOTIFICATION_FILTERS", () => {
  it("has the four quick tabs in display order", () => {
    assert.deepEqual(
      NOTIFICATION_FILTERS.map((f) => f.value),
      ["all", "unread", "jobs", "candidates"]
    );
    assert.deepEqual(
      NOTIFICATION_FILTERS.map((f) => f.label),
      ["All", "Unread", "Jobs (PAIR)", "Candidates"]
    );
  });
});

describe("matchesNotificationFilter", () => {
  it("'all' matches everything regardless of read state or candidate_id", () => {
    for (const item of [unreadCandidate, readCandidate, unreadJob, readJob]) {
      assert.equal(matchesNotificationFilter(item, "all"), true);
    }
  });

  it("'unread' matches only items with no read_at, candidate-level or job-level", () => {
    assert.equal(matchesNotificationFilter(unreadCandidate, "unread"), true);
    assert.equal(matchesNotificationFilter(unreadJob, "unread"), true);
    assert.equal(matchesNotificationFilter(readCandidate, "unread"), false);
    assert.equal(matchesNotificationFilter(readJob, "unread"), false);
  });

  it("'jobs' matches only candidate_id === null, regardless of read state", () => {
    assert.equal(matchesNotificationFilter(unreadJob, "jobs"), true);
    assert.equal(matchesNotificationFilter(readJob, "jobs"), true);
    assert.equal(matchesNotificationFilter(unreadCandidate, "jobs"), false);
    assert.equal(matchesNotificationFilter(readCandidate, "jobs"), false);
  });

  it("'candidates' matches only a set candidate_id, regardless of read state", () => {
    assert.equal(matchesNotificationFilter(unreadCandidate, "candidates"), true);
    assert.equal(matchesNotificationFilter(readCandidate, "candidates"), true);
    assert.equal(matchesNotificationFilter(unreadJob, "candidates"), false);
    assert.equal(matchesNotificationFilter(readJob, "candidates"), false);
  });

  it("'jobs' and 'candidates' are mutually exclusive and exhaustive over candidate_id", () => {
    for (const item of [unreadCandidate, readCandidate, unreadJob, readJob]) {
      const inJobs = matchesNotificationFilter(item, "jobs");
      const inCandidates = matchesNotificationFilter(item, "candidates");
      assert.notEqual(inJobs, inCandidates);
    }
  });

  it("treats an undefined candidate_id (pre-field local cache entry) as job-level, not neither", () => {
    // The type says string | null, but a notification cached before this
    // field existed could have it as undefined at runtime — == null must
    // still route it to "jobs" so the split stays exhaustive.
    const legacyCached = { read_at: null, candidate_id: undefined as unknown as null };
    assert.equal(matchesNotificationFilter(legacyCached, "jobs"), true);
    assert.equal(matchesNotificationFilter(legacyCached, "candidates"), false);
  });
});
