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

describe("DESKTOP_NOTIFICATION_TYPES", () => {
  it("lists every type the service currently writes, with no duplicates", () => {
    const types = DESKTOP_NOTIFICATION_TYPES.map((t) => t.type);
    assert.equal(new Set(types).size, types.length);
    assert.ok(types.includes("candidate_passed"));
    assert.ok(types.includes("candidate_internal_review_requested"));
    assert.ok(types.includes("candidate_externally_submitted"));
    assert.ok(types.includes("daily_digest"));
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

  it("persists across separate reads against the same storage", () => {
    const storage = memoryStorage();
    setDesktopTypeEnabled(storage, "recruiter_assigned", false);
    assert.equal(isDesktopTypeEnabled(storage, "recruiter_assigned"), false);
    assert.equal(isDesktopTypeEnabled(storage, "recruiter_assigned"), false);
  });
});
