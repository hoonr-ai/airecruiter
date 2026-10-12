// Local resilience cache for the in-app notification feed
// (context/notifications-context.tsx). Lets a page reload — or a tab whose
// GET /api/v1/notifications call is failing — show the last-known
// notifications instantly instead of going blank. A successful refresh()
// always overwrites this; it is never the source of truth for anything the
// server itself tracks.
//
// One storage key, not one per email (mirrors lib/role-cache.ts): the stored
// payload carries its own `email`, and a read for a DIFFERENT email than the
// one stored returns null. That's what keeps one person's candidate names
// out of the next person's session on a shared browser profile, even if this
// provider ever outlives a login/logout without a full page reload — no key
// bookkeeping to get wrong, and the same cache naturally self-invalidates
// the moment the active user changes.
//
// Pure (storage is passed in) so lib/notifications-cache.test.ts can run it
// under node.

import type { NotificationItem } from "./api";

export const NOTIFICATIONS_CACHE_KEY = "pair.notifications.v1";
// A cache this old is not trusted for the first paint — by then the server
// is a far more useful source of truth than whatever was last seen.
export const NOTIFICATIONS_CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
// Keep the cache small: the REST list() call itself is capped at 50, and an
// unbounded local list (every SSE push ever appended) would just grow
// forever in storage for a very long-lived tab.
export const NOTIFICATIONS_CACHE_MAX_ITEMS = 50;

export interface NotificationsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

export interface CachedNotifications {
  notifications: NotificationItem[];
  // Derived from `notifications` at read time rather than stored as its own
  // field — a separately cached counter can drift from the list it
  // describes (e.g. one write lands, the paired one doesn't); deriving it
  // makes that impossible.
  unreadCount: number;
}

type Stored = { email: string; savedAt: number; notifications: NotificationItem[] };

const norm = (email: string) => email.trim().toLowerCase();

function countUnread(notifications: NotificationItem[]): number {
  return notifications.reduce((n, item) => (item.read_at ? n : n + 1), 0);
}

/** The cached feed for `email`, or null (none, another person's, too old, unreadable). */
export function readCachedNotifications(
  storage: NotificationsStorage | null | undefined,
  email: string,
  now: number
): CachedNotifications | null {
  if (!storage || !email) return null;
  try {
    const raw = storage.getItem(NOTIFICATIONS_CACHE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Partial<Stored>;
    if (!stored || typeof stored !== "object" || !Array.isArray(stored.notifications)) return null;
    if (typeof stored.email !== "string" || norm(stored.email) !== norm(email)) return null;
    const savedAt = Number(stored.savedAt);
    if (!Number.isFinite(savedAt) || now - savedAt > NOTIFICATIONS_CACHE_MAX_AGE_MS || savedAt - now > 60_000) {
      return null;
    }
    return { notifications: stored.notifications, unreadCount: countUnread(stored.notifications) };
  } catch {
    return null;
  }
}

/** Remember `notifications` as `email`'s feed. One person per browser profile: it replaces any other. */
export function writeCachedNotifications(
  storage: NotificationsStorage | null | undefined,
  email: string,
  notifications: NotificationItem[],
  now: number
): void {
  if (!storage || !email) return;
  try {
    const stored: Stored = {
      email: norm(email),
      savedAt: now,
      notifications: notifications.slice(0, NOTIFICATIONS_CACHE_MAX_ITEMS),
    };
    storage.setItem(NOTIFICATIONS_CACHE_KEY, JSON.stringify(stored));
  } catch {
    // storage full or blocked (private browsing, quota): best-effort only
  }
}

export function clearCachedNotifications(storage: NotificationsStorage | null | undefined): void {
  try {
    storage?.removeItem(NOTIFICATIONS_CACHE_KEY);
  } catch {
    // nothing to do
  }
}
