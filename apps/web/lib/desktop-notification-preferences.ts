// Per-type opt-OUT for desktop (OS-level) notification popups.
//
// In-app notifications — the feed on /notifications, the sidebar badge, the
// unread count — are unaffected by this and always show every type; this
// only controls whether context/notifications-context.tsx's
// showBrowserNotification() is allowed to pop a native `Notification` for a
// given type. Stored per-browser, not server-side (mirrors
// lib/notification-sound.ts's mute flag) — a desktop popup preference is
// inherently tied to this browser/device, the same way Notification.
// permission itself already is.
//
// Stored as an explicit-DISABLE list rather than an explicit-enable list:
// "in-app is default; desktop is opt-out per type" means a brand-new
// notification type this app ships later must default to enabled without
// requiring a migration of every existing user's stored preference.

export const DESKTOP_NOTIFICATION_PREFS_KEY = "pair.desktopNotificationDisabledTypes.v1";

export interface DesktopNotificationTypeInfo {
  type: string;
  label: string;
  description: string;
}

// Mirrors the `type` column services/notifications_service.py writes.
// Keep in sync with that module's "Ten types today" docstring if a new type
// ships — an unlisted type still defaults to enabled (see
// isDesktopTypeEnabled below), it just won't have its own row in the
// preferences panel until added here.
export const DESKTOP_NOTIFICATION_TYPES: DesktopNotificationTypeInfo[] = [
  { type: "candidate_passed", label: "Candidate Passed", description: "A candidate reaches Pass status." },
  { type: "candidate_submitted", label: "Candidate Submitted", description: "A co-recruiter submits a candidate." },
  { type: "candidate_rejected", label: "Candidate Rejected", description: "A co-recruiter rejects a candidate." },
  {
    type: "candidate_internal_review_requested",
    label: "Internal Review Requested",
    description: "A recruiter asks you, as the named manager, to review an internally submitted candidate.",
  },
  {
    type: "candidate_externally_submitted",
    label: "Internal Review Resolved",
    description: "A candidate you flagged for internal review is later submitted externally.",
  },
  { type: "pair_launched", label: "PAIR Launched", description: "PAIR launches or relaunches candidates for a job." },
  {
    type: "pair_inactive",
    label: "PAIR Inactive",
    description: "A job's JobDiva status flips to closed, filled, cancelled, expired, ignored, or declined.",
  },
  { type: "recruiter_assigned", label: "Recruiter Assigned", description: "You're newly added as a recruiter on a job." },
  { type: "zero_match_warning", label: "Zero Match Warning", description: "A full search you ran finishes with 0 matches." },
  { type: "daily_digest", label: "Daily Digest", description: "Your scheduled daily summary of unread Pass notifications." },
];

export interface DesktopPrefsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** `window.localStorage` wrapped in a try/catch — mirrors
 * lib/notification-sound.ts's getSafeStorage: merely evaluating
 * `window.localStorage` can throw in some environments (blocked
 * third-party storage, certain privacy modes, sandboxed iframes). */
export function getSafeStorage(): DesktopPrefsStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function readDisabledTypes(storage: DesktopPrefsStorage | null | undefined): Set<string> {
  if (!storage) return new Set();
  try {
    const raw = storage.getItem(DESKTOP_NOTIFICATION_PREFS_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((v): v is string => typeof v === "string"));
  } catch {
    return new Set();
  }
}

function writeDisabledTypes(storage: DesktopPrefsStorage | null | undefined, disabled: Set<string>): void {
  if (!storage) return;
  try {
    storage.setItem(DESKTOP_NOTIFICATION_PREFS_KEY, JSON.stringify(Array.from(disabled)));
  } catch {
    // storage full or blocked (private browsing, quota): best-effort only
  }
}

/** All currently-disabled types, for rendering the preferences panel's
 * toggle states in one read rather than one readDisabledTypes call per
 * row. */
export function getDisabledDesktopTypes(storage: DesktopPrefsStorage | null | undefined): Set<string> {
  return readDisabledTypes(storage);
}

/** True unless this type has been explicitly disabled. An unrecognized or
 * not-yet-listed type (see DESKTOP_NOTIFICATION_TYPES) defaults to enabled,
 * same as every other type before the recruiter has touched this setting. */
export function isDesktopTypeEnabled(storage: DesktopPrefsStorage | null | undefined, type: string): boolean {
  return !readDisabledTypes(storage).has(type);
}

export function setDesktopTypeEnabled(
  storage: DesktopPrefsStorage | null | undefined,
  type: string,
  enabled: boolean
): void {
  const disabled = readDisabledTypes(storage);
  if (enabled) {
    disabled.delete(type);
  } else {
    disabled.add(type);
  }
  writeDisabledTypes(storage, disabled);
}
