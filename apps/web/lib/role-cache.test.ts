import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ROLE_CACHE_KEY,
  ROLE_CACHE_MAX_AGE_MS,
  clearPersistedRole,
  readPersistedRole,
  writePersistedRole,
} from "./role-cache.ts";

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

const NOW = Date.UTC(2026, 9, 12, 9, 0, 0);
const ADMIN = { role: "admin", isAdmin: true };

describe("role cache", () => {
  it("round-trips the role for the same person, case-insensitively", () => {
    const s = memoryStorage();
    writePersistedRole(s, "Akarsh@Example.com", ADMIN, NOW);
    assert.deepEqual(readPersistedRole(s, "akarsh@example.com", NOW + 1000), ADMIN);
  });

  it("never hands one person's role to another", () => {
    const s = memoryStorage();
    writePersistedRole(s, "admin@example.com", ADMIN, NOW);
    assert.equal(readPersistedRole(s, "recruiter@example.com", NOW), null);
  });

  it("ignores a stale entry and one from the future", () => {
    const s = memoryStorage();
    writePersistedRole(s, "a@example.com", ADMIN, NOW);
    assert.equal(readPersistedRole(s, "a@example.com", NOW + ROLE_CACHE_MAX_AGE_MS + 1), null);
    assert.equal(readPersistedRole(s, "a@example.com", NOW - 5 * 60_000), null);
  });

  it("survives junk and missing storage", () => {
    const s = memoryStorage();
    s.map.set(ROLE_CACHE_KEY, "{not json");
    assert.equal(readPersistedRole(s, "a@example.com", NOW), null);
    s.map.set(ROLE_CACHE_KEY, JSON.stringify({ email: "a@example.com", savedAt: NOW, data: "admin" }));
    assert.equal(readPersistedRole(s, "a@example.com", NOW), null);
    assert.equal(readPersistedRole(null, "a@example.com", NOW), null);
    assert.equal(readPersistedRole(s, "", NOW), null);
    const throwing = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => {} };
    assert.equal(readPersistedRole(throwing, "a@example.com", NOW), null);
    writePersistedRole(throwing, "a@example.com", ADMIN, NOW);
  });

  it("clears on sign-out", () => {
    const s = memoryStorage();
    writePersistedRole(s, "a@example.com", ADMIN, NOW);
    clearPersistedRole(s);
    assert.equal(readPersistedRole(s, "a@example.com", NOW), null);
  });
});
