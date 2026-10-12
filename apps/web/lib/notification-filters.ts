// Pure filter logic backing the quick tabs (All | Unread | Jobs (PAIR) |
// Candidates) on app/notifications/page.tsx. Kept here, not inline in the
// component, for unit test coverage without needing a DOM or a React test
// renderer — same rationale as lib/browser-notifications.ts.
import type { NotificationItem } from "./api";

export type NotificationFilter = "all" | "unread" | "jobs" | "candidates";

export const NOTIFICATION_FILTERS: { value: NotificationFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "unread", label: "Unread" },
  { value: "jobs", label: "Jobs (PAIR)" },
  { value: "candidates", label: "Candidates" },
];

// "Jobs (PAIR)" vs "Candidates" is the same job-level/candidate-level split
// the backend already models every notification type around (see
// services/notifications_service.py's module docstring): candidate_id is
// NULL for job-level types (pair_launched, pair_inactive,
// recruiter_assigned, zero_match_warning) and set for candidate-level ones
// (candidate_passed, candidate_submitted, candidate_rejected). Filtering on
// that field instead of a hardcoded type list means a future type is
// automatically sorted into the right tab with no change here.
export function matchesNotificationFilter(
  item: Pick<NotificationItem, "read_at" | "candidate_id">,
  filter: NotificationFilter
): boolean {
  switch (filter) {
    case "unread":
      return !item.read_at;
    case "jobs":
      return item.candidate_id === null;
    case "candidates":
      return item.candidate_id !== null;
    default:
      return true;
  }
}
