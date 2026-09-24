// Payloads of GET /api/v1/admin/dashboard/* (routers/pair_dashboard.py,
// computed in services/pair_dashboard.py).

export type MetricKind = "count" | "ratio" | "average";

export type Metric = {
  kind: MetricKind;
  /** null = unavailable (see `unavailable`) or, for a ratio, an empty denominator. */
  value: number | null;
  /** null = nothing to compare against (All Time). */
  previous: number | null;
  /** One value per week of `weeks`, oldest first. */
  weekly: (number | null)[];
  numerator?: number;
  denominator?: number;
  unavailable?: string;
  /** The period reaches before the JobDiva mirror's first covered day (YYYY-MM-DD). */
  partial_from?: string;
  breakdown?: Record<string, number | number[] | null | (number | null)[]>;
};

export type JobDivaCoverage = {
  available: boolean;
  activities_available?: boolean;
  jobs_available?: boolean;
  users_available?: boolean;
  /** Enough open reqs have had their JobDiva tags read for team-scoped numbers. */
  job_users_ready?: boolean;
  activities_from?: string | null;
  activities_complete?: boolean;
  jobs_from?: string | null;
  jobs_complete?: boolean;
  last_synced_at?: string | null;
  last_error?: string | null;
};

export type TeamScope = { team_id: string; team_name: string; member_count: number } | null;

export type DashboardTeam = { id: string; name: string; lead_emails: string[]; member_emails: string[] };

export type DashboardOptions = {
  jobs: { job_id: string; ref: string; title: string; customer: string }[];
  priorities: string[];
  clients: string[];
  jd_statuses: string[];
  pair_statuses: string[];
  verticals: string[];
  teams: DashboardTeam[];
  team_scope: TeamScope;
  jobdiva: JobDivaCoverage;
};

export type OverviewMetricKey =
  | "net_openings"
  | "pair_volume"
  | "candidates_launched"
  | "candidates_passed"
  | "pair_submissions"
  | "pair_interviews"
  | "pair_starts"
  | "fill_ratio"
  | "submit_to_start"
  | "non_pair_submissions"
  | "non_pair_starts"
  | "pair_share";

export type OverviewData = {
  range: { start: string; end: string } | null;
  previous_range: { start: string; end: string } | null;
  weeks: string[];
  metrics: Record<OverviewMetricKey, Metric>;
  reqs_in_scope: number;
  team_scope: TeamScope;
  jobdiva: JobDivaCoverage;
  warnings: string[];
};

export type FunnelSegment = { key: string; label: string; count: number };

export type FunnelStage = {
  key: string;
  label: string;
  count: number;
  segments?: FunnelSegment[];
  group?: "non_pair";
  unavailable?: boolean;
};

export type SpeedMetric = {
  key: string;
  label: string;
  from: string;
  median_hours: number | null;
  mean_hours: number | null;
  jobs: number;
};

export type PendingCandidate = {
  candidate_id: string;
  name: string;
  job_key: string;
  job_ref: string;
  job_title: string;
  recruiters: string[];
  passed_at: string | null;
  days_pending: number | null;
};

export type FunnelData = {
  cohort: { reqs: number; posted_from: string; posted_to: string };
  stages: FunnelStage[];
  speed: SpeedMetric[];
  rejection_reasons: { total: number; reasons: { reason: string; count: number }[] };
  pending_feedback: {
    pending: number;
    passed: number;
    given: number;
    candidates: PendingCandidate[];
    truncated: boolean;
  };
  team_scope: TeamScope;
  jobdiva: JobDivaCoverage;
};

export type ProductivityData = {
  team_scope: TeamScope;
  teams_configured: number;
  jobdiva: JobDivaCoverage;
  range: { start: string; end: string } | null;
  unavailable?: string;
  recruiters?: number;
  weeks?: string[];
  covered_from?: string | null;
  metrics?: {
    reqs_assigned: Metric;
    client_subs: Metric;
    starts: Metric;
  };
  unmapped_emails?: string[];
};

export type DashboardFilterState = {
  job: string;
  priority: string;
  client: string;
  jdStatus: string;
  pairStatus: string;
  vertical: string;
};

export const EMPTY_FILTERS: DashboardFilterState = {
  job: "",
  priority: "",
  client: "",
  jdStatus: "",
  pairStatus: "",
  vertical: "",
};
