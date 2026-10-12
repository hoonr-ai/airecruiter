// Pure, DOM-free helpers backing the desktop-notification feature in
// context/notifications-context.tsx. Kept here (not inline in the component)
// so the permission/visibility gating logic — easy to get subtly wrong, hard
// to eyeball-review in a React effect — has unit test coverage without
// needing a DOM or a React test renderer.
import type { NotificationItem } from "./api";

// Notification.permission's own vocabulary, plus "unsupported" for SSR (no
// `window`) and browsers without the Notification API (e.g. some mobile
// browsers). "unsupported" renders no banner at all on the notifications
// page — there is nothing the user can do about it, unlike "denied" (fixable
// in site settings).
export type BrowserNotificationPermission = NotificationPermission | "unsupported";

// navigator.permissions.query(...).state uses "prompt" where
// Notification.permission uses "default" for the same not-yet-asked state.
// Normalized here so the rest of the app only ever deals with one vocabulary.
export function normalizePermissionState(
  state: "granted" | "denied" | "prompt"
): BrowserNotificationPermission {
  return state === "prompt" ? "default" : state;
}

// A popup is worth showing only when permission is granted AND the user
// plausibly wouldn't already notice the in-app feed update on their own.
// Tab visibility alone isn't enough: a tab can be the visible one on screen
// while a different application window — a second monitor, something on
// top — holds OS focus, and the user is just as likely to miss an in-app
// update in that state as when the tab is fully hidden.
export function shouldShowBrowserPopup(
  permission: BrowserNotificationPermission,
  isDocumentVisible: boolean,
  documentHasFocus: boolean
): boolean {
  if (permission !== "granted") return false;
  return !(isDocumentVisible && documentHasFocus);
}

// Where clicking a desktop popup should take the user. Falls back to the
// notifications page itself when either id is missing (older rows, or a
// malformed event) rather than just focusing the window and going nowhere.
export function notificationClickPath(
  item: Pick<NotificationItem, "jobdiva_id" | "candidate_id">
): string {
  if (item.jobdiva_id && item.candidate_id) {
    return `/jobs/${encodeURIComponent(item.jobdiva_id)}/report?candidateId=${encodeURIComponent(item.candidate_id)}`;
  }
  return "/notifications";
}
