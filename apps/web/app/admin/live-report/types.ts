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

export interface PhaseStop {
  key: string;
  label: string;
  short: string;
}

export const CHAIN_STOPS: PhaseStop[] = [
  { key: "contact_check", label: "Contact Check", short: "Check" },
  { key: "phase1", label: "Phase 1", short: "P1" },
  { key: "phase2", label: "Phase 2", short: "P2" },
  { key: "phase3", label: "Phase 3", short: "P3" },
  { key: "phase4", label: "Phase 4", short: "P4" },
  { key: "extra", label: "Extra", short: "Ex" },
  { key: "completed", label: "Done", short: "Done" },
];

export const phaseToStopIndex = (phase: string): number => {
  const norm = (phase || "").toLowerCase().trim();
  if (norm === "contact_check") return 0;
  if (norm.startsWith("phase1")) return 1;
  if (norm.startsWith("phase2")) return 2;
  if (norm.startsWith("phase3")) return 3;
  if (norm.startsWith("phase4")) return 4;
  if (norm.includes("extra")) return 5;
  if (TERMINAL_PHASES.has(norm)) return 6;
  return 0;
};
