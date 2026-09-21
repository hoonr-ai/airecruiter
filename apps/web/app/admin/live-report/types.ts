/**
 * Live Report domain types — mirrors PairBot live_report_service shape.
 */

export type LaunchState = "live" | "replayable" | "archived";

export interface LaunchListItem {
  bulk_id: string;
  started_at: string | null;
  last_event_at?: string | null;
  total_candidates: number;
  created_candidates?: number;
  candidates_missing?: number;
  terminal_candidates: number;
  jobdiva_ids: string[];
  titles: string[];
  state: LaunchState;
}

export interface ActivityEvent {
  ts: string | null;
  type: string;
  subtype: string | null;
  phase: string | null;
  status: string | null;
}

export type CallOutcome =
  | "answered"
  | "completed"
  | "voicemail"
  | "no_answer"
  | "busy"
  | "candidate_hangup"
  | "system_drop"
  | "failed"
  | "in_progress";

export interface CandidateRow {
  interview_id: number;
  name: string;
  name_masked?: boolean;
  phase: string;
  outreach_status: string | null;
  events: ActivityEvent[];
  call_outcome: CallOutcome | null;
  call_outcome_severity: string | null;
  call_outcome_recognised: boolean;
  is_stuck: boolean;
  idle_minutes?: number | null;
  event_count?: number;
  event_counts_by_phase?: Record<string, number>;
  events_truncated?: boolean;
  next_attempt_at?: string | null;
  awaiting_retry?: boolean;
  overall_score?: number | null;
  hard_filter_overall?: string | null;
  terminal_reason?: TerminalReason | null;
  call_attempts?: number;
  call_successes?: number;
  handoff_expired?: number;
  handoff_failed?: number;
  handoff_completed?: number;
  sms_sent?: number;
  sms_failed?: number;
  email_sent?: number;
  email_failed?: number;
}

export type TerminalReason =
  | "passed"
  | "hard_filter_failed"
  | "evaluated_below_bar"
  | "failed_unscored"
  | "no_response_after_phase3"
  | "outreach_incomplete"
  | "outreach_failed";

export const TERMINAL_REASON_LABEL: Record<TerminalReason, string> = {
  passed: "Passed",
  hard_filter_failed: "Hard filter failed",
  evaluated_below_bar: "Scored below bar",
  failed_unscored: "Failed — not scored",
  no_response_after_phase3: "No response after phase 3",
  outreach_incomplete: "Outreach incomplete",
  outreach_failed: "Outreach failed",
};

export interface JobBlockData {
  bulk_jd_id: number | null;
  jobdiva_id: string | null;
  title: string | null;
  customer_name: string | null;
  candidates: CandidateRow[];
}

export interface StuckCandidateAnomaly {
  kind: "stuck_candidate";
  interview_id: number;
  jobdiva_id: string | null;
  phase: string;
  minutes_since_event: number | null;
}

export interface CallFailureAnomaly {
  kind: "call_failure";
  interview_id: number;
  jobdiva_id: string | null;
  outcome: string;
  reason: string | null;
}

export interface HandoffExpiredAnomaly {
  kind: "handoff_expired";
  interview_id: number;
  jobdiva_id: string | null;
  expired: number;
}

export type Anomaly =
  | StuckCandidateAnomaly
  | CallFailureAnomaly
  | HandoffExpiredAnomaly;

export interface Snapshot {
  bulk_id: string;
  created_at: string | null;
  state: LaunchState;
  jobs: JobBlockData[];
  anomalies: Anomaly[];
}

export interface HealthData {
  db_pool?: {
    used: number;
    max: number;
    percent: number;
  };
  outreach_queue?: {
    pending: number;
    processing: number;
    total: number;
  };
  agent_fleet?: {
    active_workers: number;
    idle_workers: number;
    total_workers: number;
  };
  healthy: boolean;
}

export const TERMINAL_PHASES = new Set([
  "completed",
  "passed",
  "failed",
  "outreach_incomplete",
  "outreach_failed",
  "expired",
  "abandoned",
]);

export const SCHEDULED_WAIT_EVENTS = new Set([
  "outreach_deferred_quiet_hours",
  "outreach_retry_scheduled",
]);

export interface PhaseStop {
  key: string;
  label: string;
  short: string;
}

export const CHAIN_STOPS: readonly PhaseStop[] = [
  { key: "contact_check", label: "Contact check", short: "CC" },
  { key: "phase1", label: "Phase 1", short: "P1" },
  { key: "phase1_6hr", label: "Phase 2", short: "P2" },
  { key: "phase2", label: "Phase 3", short: "P3" },
  { key: "phase3", label: "Phase 4", short: "P4" },
  { key: "phase1_extra", label: "Extra 1", short: "Ex1" },
  { key: "phase1_6hr_extra", label: "Extra 2", short: "Ex2" },
  { key: "phase2_extra", label: "Extra 3", short: "Ex3" },
] as const;

export const isKnownPhase = (phase: string): boolean => {
  const p = (phase || "").toLowerCase();
  return (
    p === "contact_check" ||
    p === "phase1" ||
    p === "phase1_6hr" ||
    p === "phase2" ||
    p === "phase3" ||
    p.includes("extra") ||
    p.includes("high_score") ||
    TERMINAL_PHASES.has(p)
  );
};

export const phaseToStopIndex = (phase: string): number => {
  const p = (phase || "").toLowerCase();
  if (p === "phase2_extra" || p.includes("extra_phase3") || p.includes("extra 3")) return 7;
  if (p === "phase1_6hr_extra" || p.includes("extra_phase2") || p.includes("extra 2")) return 6;
  if (p.includes("extra") || p.includes("high_score")) return 5;

  switch (p) {
    case "contact_check":
      return 0;
    case "phase1":
      return 1;
    case "phase1_6hr":
      return 2;
    case "phase2":
      return 3;
    case "phase3":
      return 4;
    default:
      return TERMINAL_PHASES.has(p) ? 4 : 0;
  }
};

export const CHANNEL_COLOR: Record<string, string> = {
  email: "#7C6DD6",
  sms: "#C8901F",
  call: "#0D9488",
};

export const OUTCOME_META: Record<
  CallOutcome,
  { label: string; tone: "good" | "warning" | "critical" | "neutral" | "info" }
> = {
  answered: { label: "Answered", tone: "good" },
  completed: { label: "Completed", tone: "good" },
  voicemail: { label: "Voicemail", tone: "neutral" },
  no_answer: { label: "No answer", tone: "neutral" },
  busy: { label: "Busy", tone: "neutral" },
  candidate_hangup: { label: "Candidate hung up", tone: "warning" },
  system_drop: { label: "System drop", tone: "critical" },
  failed: { label: "Call failed", tone: "critical" },
  in_progress: { label: "On call", tone: "info" },
};

