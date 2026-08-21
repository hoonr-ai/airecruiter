/**
 * Whether the activity timeline should render "Questions completed".
 *
 * Hidden (PAI-157):
 * - interview_started_web / interview_started_call — session start, not progress
 * - any call_* type — includes call_dispatch_requested (call parking) and
 *   call_initiated (SIP start), plus later call_status_* / AMD rows
 *
 * Shown on interview_partial_completed, interview_completed, and
 * questionnaire_submitted (and any other non-call, non-start type).
 */
const HIDE_QUESTIONS_COMPLETED_EXACT = new Set([
  "interview_started_web",
  "interview_started_call",
]);

export function shouldShowQuestionsCompleted(activityType: string): boolean {
  if (HIDE_QUESTIONS_COMPLETED_EXACT.has(activityType)) return false;
  if (activityType.startsWith("call_")) return false;
  return true;
}
