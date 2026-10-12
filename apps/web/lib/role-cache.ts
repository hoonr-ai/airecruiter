// The signed-in person's last known role, kept in localStorage so a page load
// shows the right navigation straight away instead of a "recruiter" guess
// while /api/v1/auth/me answers (hooks/use-user-role.ts always re-asks the
// server once per page load and overwrites this). It only shapes the UI:
// every admin endpoint still checks the role server-side.
//
// Pure (storage is passed in) so lib/role-cache.test.ts can run it under node.

export const ROLE_CACHE_KEY = "pair.userRole.v1";
// A role this old is not trusted for the first paint any more.
export const ROLE_CACHE_MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;

export interface RoleStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

type Stored<T> = { email: string; savedAt: number; data: T };

const norm = (email: string) => email.trim().toLowerCase();

/** The role saved for `email`, or null (none, another person's, too old, unreadable). */
export function readPersistedRole<T>(storage: RoleStorage | null | undefined, email: string, now: number): T | null {
  if (!storage || !email) return null;
  try {
    const raw = storage.getItem(ROLE_CACHE_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as Partial<Stored<T>>;
    if (!stored || typeof stored !== "object" || !stored.data || typeof stored.data !== "object") return null;
    if (typeof stored.email !== "string" || norm(stored.email) !== norm(email)) return null;
    const savedAt = Number(stored.savedAt);
    if (!Number.isFinite(savedAt) || now - savedAt > ROLE_CACHE_MAX_AGE_MS || savedAt - now > 60_000) return null;
    return stored.data as T;
  } catch {
    return null;
  }
}

/** Remember `data` as `email`'s role. One person per browser profile: it replaces any other. */
export function writePersistedRole<T>(storage: RoleStorage | null | undefined, email: string, data: T, now: number): void {
  if (!storage || !email) return;
  try {
    const stored: Stored<T> = { email: norm(email), savedAt: now, data };
    storage.setItem(ROLE_CACHE_KEY, JSON.stringify(stored));
  } catch {
    // storage full or blocked: the next load just asks the server first
  }
}

export function clearPersistedRole(storage: RoleStorage | null | undefined): void {
  try {
    storage?.removeItem(ROLE_CACHE_KEY);
  } catch {
    // nothing to do
  }
}
