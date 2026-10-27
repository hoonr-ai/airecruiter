import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  DESKTOP_NOTIFICATION_PREFS_KEY,
  DESKTOP_NOTIFICATION_TYPES,
  getDisabledDesktopTypes,
  isDesktopTypeEnabled,
  setDesktopTypeEnabled,
  type DesktopPrefsStorage,
} from "./desktop-notification-preferences.ts";

function memoryStorage(): DesktopPrefsStorage & { map: Map<string, string> } {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
  };
}

// Frozen snapshot of the `type` strings services/notifications_service.py's
// module docstring documents as "Nine types today" — independent of
// DESKTOP_NOTIFICATION_TYPES itself, so an accidental edit to that list
// (typo, dropped entry) fails this test rather than silently shipping.
// This can only catch drift in ONE direction: a backend contributor adding
// a 10th type still has to remember to update this list by hand (PR #811
// review follow-up — no automated cross-language sync exists here).
//
// candidate_submitted is NOT in this list: it's fully retired backend-side
// (routers/candidates.py no longer calls anything that creates one) — every
// real Submit is internal or external, each with its own more specific
// notification, so there was never a need for a generic third one.
const BACKEND_TYPES_SNAPSHOT = [
  "candidate_passed",
  "candidate_rejected",
  "pair_launched",
  "pair_inactive",
  "recruiter_assigned",
  "zero_match_warning",
  "daily_digest",
  "candidate_internal_review_requested",
  "candidate_externally_submitted",
  // apps/api/services/recruiter_handoff.py (NOTIFY_REQUESTED / NOTIFY_MISSED)
  "recruiter_handoff_requested",
  "recruiter_handoff_missed",
  // apps/api/services/notifications_service.py's
  // create_candidate_callback_reminder_notification — fires ~5 minutes
  // before a recruiter's own scheduled callback time (routers/candidates.py's
  // 'Callback Scheduled' feedback_type).
  "candidate_callback_reminder",
];

describe("DESKTOP_NOTIFICATION_TYPES", () => {
  it("has no duplicate type entries", () => {
    const types = DESKTOP_NOTIFICATION_TYPES.map((t) => t.type);
    assert.equal(new Set(types).size, types.length);
  });

  it("matches the backend's current type list exactly (see BACKEND_TYPES_SNAPSHOT above)", () => {
    const types = DESKTOP_NOTIFICATION_TYPES.map((t) => t.type).sort();
    assert.deepEqual(types, [...BACKEND_TYPES_SNAPSHOT].sort());
  });
});

describe("isDesktopTypeEnabled", () => {
  it("defaults every listed type to enabled when nothing is stored", () => {
    const storage = memoryStorage();
    for (const t of DESKTOP_NOTIFICATION_TYPES) {
      assert.equal(isDesktopTypeEnabled(storage, t.type), true);
    }
  });

  it("defaults an unrecognized/future type to enabled too", () => {
    const storage = memoryStorage();
    assert.equal(isDesktopTypeEnabled(storage, "some_type_shipped_later"), true);
  });

  it("is false after that type is explicitly disabled", () => {
    const storage = memoryStorage();
    setDesktopTypeEnabled(storage, "zero_match_warning", false);
    assert.equal(isDesktopTypeEnabled(storage, "zero_match_warning"), false);
    // Unrelated types stay enabled.
    assert.equal(isDesktopTypeEnabled(storage, "candidate_passed"), true);
  });

  it("re-enabling a disabled type removes it from the stored set", () => {
    const storage = memoryStorage();
    setDesktopTypeEnabled(storage, "daily_digest", false);
    setDesktopTypeEnabled(storage, "daily_digest", true);
    assert.equal(isDesktopTypeEnabled(storage, "daily_digest"), true);
    assert.deepEqual(JSON.parse(storage.map.get(DESKTOP_NOTIFICATION_PREFS_KEY) as string), []);
  });

  it("returns true (fails open) when storage is null/undefined", () => {
    assert.equal(isDesktopTypeEnabled(null, "candidate_passed"), true);
    assert.equal(isDesktopTypeEnabled(undefined, "candidate_passed"), true);
  });

  it("fails open on a corrupted/non-array stored value", () => {
    const storage = memoryStorage();
    storage.map.set(DESKTOP_NOTIFICATION_PREFS_KEY, "{not valid json");
    assert.equal(isDesktopTypeEnabled(storage, "candidate_passed"), true);

    storage.map.set(DESKTOP_NOTIFICATION_PREFS_KEY, JSON.stringify({ not: "an array" }));
    assert.equal(isDesktopTypeEnabled(storage, "candidate_passed"), true);
  });

  it("candidate_submitted is permanently disabled, with nothing stored", () => {
    const storage = memoryStorage();
    assert.equal(isDesktopTypeEnabled(storage, "candidate_submitted"), false);
  });

  it("candidate_submitted stays disabled even if something stores it as explicitly enabled", () => {
    // There's no UI path that does this (it has no row in
    // DESKTOP_NOTIFICATION_TYPES), but the hard-coded block must win over
    // the stored set regardless, not just over the "nothing stored" case.
    const storage = memoryStorage();
    setDesktopTypeEnabled(storage, "candidate_submitted", true);
    assert.equal(isDesktopTypeEnabled(storage, "candidate_submitted"), false);
  });
});

describe("getDisabledDesktopTypes", () => {
  it("reflects multiple disabled types as a set", () => {
    const storage = memoryStorage();
    setDesktopTypeEnabled(storage, "pair_launched", false);
    setDesktopTypeEnabled(storage, "pair_inactive", false);
    const disabled = getDisabledDesktopTypes(storage);
    assert.deepEqual([...disabled].sort(), ["pair_inactive", "pair_launched"]);
  });

  it("is empty for a storage object with no stored preferences", () => {
    const storage = memoryStorage();
    assert.equal(getDisabledDesktopTypes(storage).size, 0);
  });
});

describe("setDesktopTypeEnabled", () => {
  it("is a no-op (never throws) when storage is null", () => {
    assert.doesNotThrow(() => setDesktopTypeEnabled(null, "candidate_passed", false));
  });

  it("persists to the underlying storage, not just an in-memory artifact of this call", () => {
    const storage = memoryStorage();
    setDesktopTypeEnabled(storage, "recruiter_assigned", false);
    assert.deepEqual(JSON.parse(storage.map.get(DESKTOP_NOTIFICATION_PREFS_KEY) as string), ["recruiter_assigned"]);
    assert.equal(isDesktopTypeEnabled(storage, "recruiter_assigned"), false);
  });
});
