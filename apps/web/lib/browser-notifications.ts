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

// Where clicking a notification (the in-app row or a desktop popup) should
// take the user:
// - candidate-level (candidate_passed): straight to that candidate's report.
// - job-level (pair_launched, pair_inactive — candidate_id is always NULL
//   for these, see services/notifications_service.py): there's no single
//   candidate to open, so the job's rankings page is the closest "show me
//   what this is about" destination.
// - neither id present (older row, malformed event): the notifications page
//   itself, rather than just focusing the window and going nowhere.
// - recruiter_handoff_requested: the live join page for that interview -
//   the whole point of the notification is "the candidate is waiting now".
export const HANDOFF_REQUESTED_TYPE = "recruiter_handoff_requested";

export function notificationClickPath(
  item: Pick<NotificationItem, "jobdiva_id" | "candidate_id"> & Partial<Pick<NotificationItem, "type" | "metadata">>
): string {
  const interviewId = Number(item.metadata?.interview_id);
  if (item.type === HANDOFF_REQUESTED_TYPE && Number.isInteger(interviewId) && interviewId > 0) {
    return `/handoff/${interviewId}`;
  }
  if (item.jobdiva_id && item.candidate_id) {
    return `/jobs/${encodeURIComponent(item.jobdiva_id)}/report?candidateId=${encodeURIComponent(item.candidate_id)}`;
  }
  if (item.jobdiva_id) {
    return `/jobs/${encodeURIComponent(item.jobdiva_id)}/rankings`;
  }
  return "/notifications";
}
