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
//
// isDesktopTypeEnabled re-parses storage on every call rather than caching
// the disabled set in memory. It's called once per incoming notification —
// cheap at that volume — and a cache would need its own cross-tab
// invalidation (a `storage` event listener) to stay correct, which isn't
// worth the complexity unless notification frequency grows enough to matter.

import type { SafeStorage } from "./safe-local-storage";

export type { SafeStorage as DesktopPrefsStorage };
// Callers get the shared accessor straight from safe-local-storage.ts
// (getSafeLocalStorage) — not re-exported here under another name, so
// there's exactly one getSafeStorage-shaped function in scope wherever a
// caller also needs lib/notification-sound.ts's (PR #811 review
// follow-up: this used to need an import alias to avoid that collision).

export const DESKTOP_NOTIFICATION_PREFS_KEY = "pair.desktopNotificationDisabledTypes.v1";

export interface DesktopNotificationTypeInfo {
  type: string;
  label: string;
  description: string;
}

// Mirrors the `type` column services/notifications_service.py writes, minus
// candidate_submitted — see DESKTOP_DISABLED_BY_DEFAULT below for why that
// one has no row here. Keep in sync with that module's "Nine types today"
// docstring if a new type ships — an unlisted type still defaults to
// enabled (see isDesktopTypeEnabled below), it just won't have its own row
// in the preferences panel until added here.
export const DESKTOP_NOTIFICATION_TYPES: DesktopNotificationTypeInfo[] = [
  { type: "candidate_passed", label: "Candidate Passed", description: "A candidate reaches Pass status." },
  {
    type: "candidate_internal_review_requested",
    label: "Candidate Internally Submitted",
    description: "A recruiter asks you, as the named manager, to review an internally submitted candidate.",
  },
  {
    // Label matches this type's own naming convention (Candidate
    // Passed/Internally Submitted/Rejected) and its generated title text
    // ("X was externally submitted for Y") — there's no separate type-name
    // label shown anywhere in the feed itself to otherwise match against.
    type: "candidate_externally_submitted",
    label: "Candidate Externally Submitted",
    description: "A candidate you flagged for internal review is later submitted externally.",
  },
  { type: "candidate_rejected", label: "Candidate Rejected", description: "A co-recruiter rejects a candidate." },
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

// Types that no longer have a row in the panel above and must not pop a
// desktop alert, regardless of any stored preference — distinct from an
// unlisted FUTURE type (which defaults to enabled; see isDesktopTypeEnabled
// below). candidate_submitted is retired entirely: routers/candidates.py no
// longer creates new rows of this type (see services/notifications_service.py),
// superseded by the more specific candidate_internal_review_requested /
// candidate_externally_submitted notifications now that submissions split
// into internal/external. This entry only matters for any legacy rows
// already in a recipient's feed from before the retirement — it keeps them
// from popping a desktop alert, since there's no toggle anywhere for a
// recruiter to turn one back on.
const DESKTOP_DISABLED_BY_DEFAULT: ReadonlySet<string> = new Set(["candidate_submitted"]);

function readDisabledTypes(storage: SafeStorage | null | undefined): Set<string> {
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

function writeDisabledTypes(storage: SafeStorage | null | undefined, disabled: Set<string>): void {
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
export function getDisabledDesktopTypes(storage: SafeStorage | null | undefined): Set<string> {
  return readDisabledTypes(storage);
}

/** True unless this type has been explicitly disabled, or is permanently
 * off regardless of storage (DESKTOP_DISABLED_BY_DEFAULT). An unrecognized
 * or not-yet-listed FUTURE type defaults to enabled, same as every other
 * type before the recruiter has touched this setting. */
export function isDesktopTypeEnabled(storage: SafeStorage | null | undefined, type: string): boolean {
  if (DESKTOP_DISABLED_BY_DEFAULT.has(type)) return false;
  return !readDisabledTypes(storage).has(type);
}

export function setDesktopTypeEnabled(
  storage: SafeStorage | null | undefined,
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
