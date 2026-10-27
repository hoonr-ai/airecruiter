import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  NOTIFICATIONS_CACHE_KEY,
  NOTIFICATIONS_CACHE_MAX_AGE_MS,
  NOTIFICATIONS_CACHE_MAX_ITEMS,
  NOTIFICATIONS_CACHE_REWRITE_AFTER_MS,
  clearCachedNotifications,
  hasUnseenNotifications,
  readCachedNotifications,
  writeCachedNotifications,
} from "./notifications-cache.ts";
import type { NotificationItem } from "./api.ts";

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

function item(overrides: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 1,
    type: "candidate_passed",
    job_id: "J1",
    jobdiva_id: "26-14907",
    candidate_id: "123",
    title: "Jane Doe passed the interview for MS SQL Developer",
    body: "Score: 90/100",
    score: 90,
    metadata: {},
    read_at: null,
    created_at: "2026-10-12T09:00:00.000Z",
    ...overrides,
  };
}

const NOW = Date.UTC(2026, 9, 12, 9, 0, 0);

describe("notifications cache", () => {
  it("round-trips the feed for the same person, case-insensitively", () => {
    const s = memoryStorage();
    writeCachedNotifications(s, "Gokul@Example.com", [item()], NOW);
    const cached = readCachedNotifications(s, "gokul@example.com", NOW + 1000);
    assert.equal(cached?.notifications.length, 1);
    assert.equal(cached?.notifications[0].id, 1);
  });

  it("derives unreadCount from the notifications themselves rather than trusting a stored counter", () => {
    const s = memoryStorage();
    writeCachedNotifications(
      s,
      "a@example.com",
      [item({ id: 1, read_at: null }), item({ id: 2, read_at: "2026-10-12T09:05:00Z" }), item({ id: 3, read_at: null })],
      NOW
    );
    const cached = readCachedNotifications(s, "a@example.com", NOW);
    assert.equal(cached?.unreadCount, 2);
  });

  it("never hands one person's cached notifications to another", () => {
    const s = memoryStorage();
    writeCachedNotifications(s, "admin@example.com", [item()], NOW);
    assert.equal(readCachedNotifications(s, "recruiter@example.com", NOW), null);
  });

  it("ignores a stale entry and one from the future", () => {
    const s = memoryStorage();
    writeCachedNotifications(s, "a@example.com", [item()], NOW);
    assert.equal(readCachedNotifications(s, "a@example.com", NOW + NOTIFICATIONS_CACHE_MAX_AGE_MS + 1), null);
    assert.equal(readCachedNotifications(s, "a@example.com", NOW - 5 * 60_000), null);
  });

  it("caps how many notifications get cached", () => {
    const s = memoryStorage();
    const many = Array.from({ length: NOTIFICATIONS_CACHE_MAX_ITEMS + 20 }, (_, i) => item({ id: i + 1 }));
    writeCachedNotifications(s, "a@example.com", many, NOW);
    const cached = readCachedNotifications(s, "a@example.com", NOW);
    assert.equal(cached?.notifications.length, NOTIFICATIONS_CACHE_MAX_ITEMS);
  });

  it("survives junk and missing storage", () => {
    const s = memoryStorage();
    s.map.set(NOTIFICATIONS_CACHE_KEY, "{not json");
    assert.equal(readCachedNotifications(s, "a@example.com", NOW), null);
    s.map.set(NOTIFICATIONS_CACHE_KEY, JSON.stringify({ email: "a@example.com", savedAt: NOW, notifications: "nope" }));
    assert.equal(readCachedNotifications(s, "a@example.com", NOW), null);
    assert.equal(readCachedNotifications(null, "a@example.com", NOW), null);
    assert.equal(readCachedNotifications(s, "", NOW), null);
    const throwing = {
      getItem: () => { throw new Error("blocked"); },
      setItem: () => { throw new Error("blocked"); },
      removeItem: () => {},
    };
    assert.equal(readCachedNotifications(throwing, "a@example.com", NOW), null);
    writeCachedNotifications(throwing, "a@example.com", [item()], NOW);
  });

  it("does not read or write without an email", () => {
    const s = memoryStorage();
    writeCachedNotifications(s, "", [item()], NOW);
    assert.equal(s.map.size, 0);
  });

  it("clears on sign-out", () => {
    const s = memoryStorage();
    writeCachedNotifications(s, "a@example.com", [item()], NOW);
    clearCachedNotifications(s);
    assert.equal(readCachedNotifications(s, "a@example.com", NOW), null);
  });

  it("skips rewriting an unchanged feed, so other tabs get no storage event", () => {
    const s = memoryStorage();
    let writes = 0;
    const counting = { ...s, setItem: (k: string, v: string) => { writes++; s.setItem(k, v); } };
    writeCachedNotifications(counting, "a@example.com", [item()], NOW);
    writeCachedNotifications(counting, "A@example.com", [item()], NOW + 1000);
    assert.equal(writes, 1);
    writeCachedNotifications(counting, "a@example.com", [item({ read_at: "2026-10-12T09:01:00.000Z" })], NOW + 2000);
    assert.equal(writes, 2);
    writeCachedNotifications(counting, "b@example.com", [item({ read_at: "2026-10-12T09:01:00.000Z" })], NOW + 3000);
    assert.equal(writes, 3);
    // ...but still refreshes savedAt periodically so a live feed never ages out.
    writeCachedNotifications(counting, "b@example.com", [item({ read_at: "2026-10-12T09:01:00.000Z" })], NOW + 3000 + NOTIFICATIONS_CACHE_REWRITE_AFTER_MS);
    assert.equal(writes, 4);
  });
});

describe("hasUnseenNotifications", () => {
  it("is true only when the other tab's list has an id this tab hasn't seen", () => {
    const list = [item({ id: 7 }), item({ id: 5 })];
    assert.equal(hasUnseenNotifications(list, 6), true);
    assert.equal(hasUnseenNotifications(list, 7), false);
    // An older list (a background tab that missed pushes) is never "newer".
    assert.equal(hasUnseenNotifications([item({ id: 3 })], 9), false);
    assert.equal(hasUnseenNotifications([], 0), false);
  });
});
