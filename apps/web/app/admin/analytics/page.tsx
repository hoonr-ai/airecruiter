"use client";

import { useEffect, useState, useCallback, type ReactNode } from "react";
import {
  Briefcase,
  Archive,
  Users,
  UserCheck,
  RefreshCw,
  TrendingUp,
  Award,
  Building2,
  ShieldAlert,
  ArrowLeft,
  AlertTriangle,
  Download,
  Timer,
  Rocket,
  Clock,
  Hourglass,
  Activity,
  CalendarClock,
  Linkedin,
  Search,
  Send,
  UsersRound,
  BadgeCheck,
  ClipboardCheck,
  X,
} from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  EMPTY_DATE,
  formatDuration,
  formatEasternDate,
  formatEasternDateTime,
  normalizeToUtcDate,
  todayEastern,
  withEasternLabel,
} from "@/lib/date";
import { escapeCSV, toCsv, UTF8_BOM } from "@/lib/csv";
interface AnalyticsOverview {
  total_monitored_jobs: number;
  total_archived_jobs: number;
  total_sourced_candidates: number;
  total_active_recruiters: number;
}

interface CustomerJob {
  customer_name: string;
  job_count: number;
}

interface RecruiterStat {
  email: string;
  active_jobs: number;
  total_candidates: number;
}

interface CandidateSource {
  source: string;
  count: number;
}

interface JobTimelineEntry {
  job_id: string;
  jobdiva_id: string;
  title: string;
  customer_name: string;
  posted_date_raw: string;
  jobdiva_posted_on: string | null;
  added_to_curate_at: string | null;
  curate_launched_at: string | null;
  outreach_stopped_at: string | null;
  posted_to_launch_days: number | null;
  is_archived: boolean;
  archive_reason?: string | null;
  jobdiva_status: string;
  pair_status: "Active" | "Inactive" | "Unpublished";
  candidates_sourced: number;
  candidates_launched: number;
  jobdiva_submittals?: number;
  campaign_id: string | null;
  recruiter_emails?: string[];
  first_feedback_at: string | null;
  // The fields below are optional so a page served during a deploy, ahead of
  // the backend that sends them, renders "—" instead of crashing. The
  // candidate counts are null when the backend couldn't compute them this
  // time (jobs_timeline_metrics_available=false) — "—", never a fake 0.
  /** Who first saved / first launched the job in PAIR. null = not recorded
   *  (every job created before 09/23/2026, when stamping started). */
  posted_by?: string | null;
  launched_by?: string | null;
  /** Candidates whose PAIR interview reads Pass / Fail on the rank list. */
  pass_candidates?: number | null;
  fail_candidates?: number | null;
  /** Candidates with a recorded recruiter decision, split by kind. */
  feedback_total?: number | null;
  feedback_submits?: number | null;
  feedback_rejects?: number | null;
  feedback_unreachable?: number | null;
  /** PAIR-recorded Submits: internal = to a hiring manager for review,
   *  external = to the client. */
  pair_internal_submits?: number | null;
  pair_external_submits?: number | null;
  /** JobDiva-verified: a JobDiva submittal to the job contact for a
   *  PAIR-passed candidate (monitored_jobs.pair_external_subs). */
  jobdiva_confirmed_subs?: number;
  /** Earliest current external Submit recorded in PAIR. */
  first_pair_external_submit_at?: string | null;
  /** Step 5 ("Source") time in minutes: active time summed over every visit
   *  and recruiter, and first Step 5 entry → first SUCCESSFUL launch. null =
   *  not tracked (every job worked before 09/23/2026), not launched yet, or
   *  unavailable this time (jobs_timeline_step_time_available=false). */
  step5_active_minutes?: number | null;
  step5_to_launch_minutes?: number | null;
}

interface LaunchSpeed {
  launched_jobs?: number;
  unlaunched_active_jobs?: number;
  aged_unlaunched_jobs?: number;
  avg_days_posted_to_launch?: number | null;
  median_days_posted_to_launch?: number | null;
}

interface WeeklyTrends {
  weeks?: string[];
  jobs_added?: number[];
  jobs_launched?: number[];
  candidates_sourced?: number[];
  candidates_launched?: number[];
  jobdiva_submittals?: number[];
}

interface SubmissionTopJob {
  job_id: string;
  jobdiva_id: string;
  title: string;
  customer_name: string;
  submittals: number;
  last_submit_date: string | null;
}

interface SubmissionMetrics {
  jobdiva_total_submittals?: number;
  jobdiva_recorded_submittals?: number;
  jobdiva_distinct_candidates?: number;
  jobdiva_submittals_last_30_days?: number;
  complete_submissions?: number;
  pass_submissions?: number;
  /** JobDiva-confirmed PAIR submittals (shown as "JobDiva-Confirmed PAIR Subs"). */
  pair_external_subs?: number;
  pair_submits?: number;
  /** Split of PAIR Submits over every scoped job; null when unavailable. */
  pair_internal_submits?: number | null;
  pair_external_submits?: number | null;
  top_jobs_by_submittals?: SubmissionTopJob[];
}

interface TeamScope {
  team_id: string;
  team_name: string;
  member_count?: number;
}

interface TeamSummary {
  id: string;
  name: string;
  lead_emails: string[];
  member_emails: string[];
}

interface LinkedInAccount {
  account_id: string;
  account_name: string;
  use_count: number;
  last_used_at: string | null;
  cooldown_until: string | null;
  last_error: string;
  /** Live Unipile workspace status ("OK", "CREDENTIALS", "DETACHED", ...) — only present on the /admin/linkedin-accounts live view. */
  status?: string;
  /** Which LinkedIn search API last worked on this account: "recruiter", or "classic" when the account has no Recruiter seat. */
  search_api?: "recruiter" | "classic" | string | null;
  /** True when the LinkedIn session is gone (logged out elsewhere, expired, checkpoint) and someone must reconnect it in Unipile. */
  needs_reconnect?: boolean;
}

interface AnalyticsData {
  overview: AnalyticsOverview;
  candidates_by_status: Record<string, number>;
  jobs_by_customer: CustomerJob[];
  top_recruiters: RecruiterStat[];
  candidates_by_source?: CandidateSource[];
  jobs_timeline?: JobTimelineEntry[];
  jobs_timeline_total?: number;
  /** false: the timeline's candidate columns (pass, feedback, PAIR
   *  submittals, first-feedback / first-external times) are null this time. */
  jobs_timeline_metrics_available?: boolean;
  /** false: the Step 5 time columns are null because the read failed, not
   *  because the jobs predate the tracking. */
  jobs_timeline_step_time_available?: boolean;
  launch_speed?: LaunchSpeed;
  weekly_trends?: WeeklyTrends;
  submission_metrics?: SubmissionMetrics;
  linkedin_accounts?: LinkedInAccount[];
  team_scope?: TeamScope | null;
  warning?: string;
}

const PAIR_STATUS_FILTERS = [
  "All",
  "Active",
  "Unpublished",
  "Inactive",
] as const;
type PairStatusFilter = (typeof PAIR_STATUS_FILTERS)[number];

// Dates and times use the shared report formatters in lib/date.ts:
// "MM/DD/YYYY HH:MM:SS" in US Eastern with no zone suffix in the cell or CSV
// value; the zone is stated once, in the column header (withEasternLabel).

/** ISO Monday date (YYYY-MM-DD) → "Jun 1". A calendar date, rendered as
 *  written: parsing it as local midnight and converting to Eastern moved it
 *  back a day for viewers east of New York (India). */
const formatWeekLabel = (iso: string): string => {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", {
    timeZone: "UTC",
    month: "short",
    day: "numeric",
  });
};

/** ISO datetime → relative "2h ago" / "3d ago"; null → "—". */
const formatRelativeTime = (iso: string | null | undefined): string => {
  const date = normalizeToUtcDate(iso);
  if (!date) return "—";
  const t = date.getTime();
  const diffMins = Math.floor((Date.now() - t) / 60000);
  if (diffMins < 1) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  const hours = Math.floor(diffMins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
};

/** One decimal only when not whole: 3 → "3", 3.5 → "3.5". */
const formatLagValue = (lag: number): string =>
  Number.isInteger(lag) ? `${lag}` : lag.toFixed(1);

const renderDateCell = (iso: string | null | undefined) => {
  const formatted = formatEasternDateTime(iso);
  return formatted === EMPTY_DATE ? (
    <span className="text-slate-300">{EMPTY_DATE}</span>
  ) : (
    <span className="text-slate-700">{formatted}</span>
  );
};

/** Header label with an explanatory tooltip (dotted underline = hover me). */
const HeaderHint = ({ hint, children }: { hint: string; children: ReactNode }) => (
  <span title={hint} className="cursor-help border-b border-dotted border-slate-400">
    {children}
  </span>
);

/** Posted By / Launched By: the email, or "—" when not recorded — every job
 *  created before 09/23/2026, when PAIR started stamping these. */
const renderPersonCell = (email: string | null | undefined) =>
  email ? (
    <div className="text-slate-600 text-[13px] break-words max-w-[220px]" title={email}>
      {email}
    </div>
  ) : (
    <span className="text-slate-300">—</span>
  );

/** A candidate count, or "—" when it is null (not computed this time) or
 *  missing (an older backend) — never a 0 that reads as real data. */
const renderCountCell = (value: number | null | undefined, className: string) =>
  value === null || value === undefined ? (
    <span className="text-slate-300 font-normal">{EMPTY_DATE}</span>
  ) : (
    <span className={className}>{value.toLocaleString()}</span>
  );

/** PAIR Submits headline. When the internal/external split is available the
 *  headline is their sum, so the card can never read "0 — 1 internal ·
 *  3 external": the split is computed live, while the `pair_submits` counter
 *  is refreshed on each feedback click and by the 15-minute sync. Both count
 *  people with a Submit; they can differ only for someone stored under both
 *  job keys with different decisions on the two rows (the split reads the
 *  newest one — services/job_candidate_metrics.py). Without the split the
 *  counter is the only number there is. */
const pairSubmitsTotal = (sm: SubmissionMetrics): number =>
  sm.pair_internal_submits != null && sm.pair_external_submits != null
    ? sm.pair_internal_submits + sm.pair_external_submits
    : sm.pair_submits ?? 0;

/** Step 5 time: "1h 22m", or "—" when null — not tracked (jobs worked
 *  before 09/23/2026), not launched yet, or unavailable. A job nobody timed
 *  must never read "0m". The CSV uses formatDuration directly. */
const renderDurationCell = (minutes: number | null | undefined) => {
  const formatted = formatDuration(minutes);
  return formatted === EMPTY_DATE ? (
    <span className="text-slate-300">{EMPTY_DATE}</span>
  ) : (
    <span className="font-semibold text-slate-700">{formatted}</span>
  );
};

/** CSV twin of renderCountCell. No locale grouping: "1,234" would split the cell. */
const countCsvValue = (value: number | null | undefined): string =>
  value === null || value === undefined ? EMPTY_DATE : String(value);

/** Top Jobs "Last Submittal": jobdiva_submittals.submit_date is a naive
 *  TIMESTAMP copied from JobDiva's SUBMITDATE, whose zone is unverified (a US
 *  ATS, so plausibly an Eastern wall clock). Converting it as a UTC instant
 *  moved 00:00–04:00 submittals to the previous day, and it would disagree
 *  with the weekly trend, which buckets the value as written. So show the
 *  calendar date as stored; a value that does carry an offset is a real
 *  instant and is converted normally. */
const formatStoredSubmitDate = (value: string | null | undefined): string => {
  if (!value) return EMPTY_DATE;
  const hasOffset = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value.trim());
  return formatEasternDate(hasOffset ? value : value.trim().slice(0, 10));
};

const feedbackBreakdownTitle = (job: JobTimelineEntry): string =>
  `${job.feedback_submits ?? 0} Submit · ${job.feedback_rejects ?? 0} Reject · ${job.feedback_unreachable ?? 0} Unreachable`;

/** Feedback total, with the non-zero kinds underneath ("2 submit · 1 reject"). */
const renderFeedbackCell = (job: JobTimelineEntry) => {
  if (job.feedback_total === null || job.feedback_total === undefined) {
    return renderCountCell(job.feedback_total, "");
  }
  const total = job.feedback_total;
  const kinds: Array<[number, string]> = [
    [job.feedback_submits ?? 0, "submit"],
    [job.feedback_rejects ?? 0, "reject"],
    [job.feedback_unreachable ?? 0, "unreachable"],
  ];
  const breakdown = kinds
    .filter(([count]) => count > 0)
    .map(([count, label]) => `${count.toLocaleString()} ${label}`)
    .join(" · ");
  return (
    <div title={feedbackBreakdownTitle(job)}>
      <div className="font-bold text-slate-800">{total.toLocaleString()}</div>
      {breakdown && (
        <div className="mt-0.5 text-[11px] font-medium text-slate-400 whitespace-nowrap">
          {breakdown}
        </div>
      )}
    </div>
  );
};

// Loading-skeleton bars for the timeline's non-sticky columns, in header
// order. Its length plus the 3 sticky columns (#, JobDiva ID, Job Title) is
// the table's column count, which the empty-state colSpan also uses — add a
// bar here whenever a column is added to the header.
const TIMELINE_SKELETON_BARS: Array<{ center?: boolean; bar: string }> = [
  { bar: "h-4 w-24 rounded" }, // Client
  { bar: "h-4 w-24 rounded" }, // Recruiter Emails
  { bar: "h-4 w-24 rounded" }, // Posted By
  { bar: "h-4 w-24 rounded" }, // Launched By
  { bar: "h-4 w-20 rounded" }, // Posted (JobDiva)
  { bar: "h-4 w-20 rounded" }, // Added (PAIR)
  { bar: "h-4 w-20 rounded" }, // Launched (PAIR)
  { center: true, bar: "h-5 w-10 rounded-full" }, // Lag
  { center: true, bar: "h-4 w-12 rounded" }, // Step 5 Active Time
  { center: true, bar: "h-4 w-12 rounded" }, // Step 5 → Launch
  { center: true, bar: "h-5 w-16 rounded-full" }, // Active / Archived
  { center: true, bar: "h-5 w-16 rounded-full" }, // PAIR Status
  { center: true, bar: "h-4 w-8 rounded" }, // Sourced
  { center: true, bar: "h-4 w-8 rounded" }, // Launched
  { center: true, bar: "h-4 w-8 rounded" }, // Pass Candidates
  { center: true, bar: "h-4 w-8 rounded" }, // Feedback
  { bar: "h-4 w-28 rounded" }, // First Feedback Submitted At
  { center: true, bar: "h-4 w-8 rounded" }, // PAIR Submittals: Internal
  { center: true, bar: "h-4 w-8 rounded" }, // PAIR Submittals: External
  { center: true, bar: "h-4 w-8 rounded" }, // JobDiva-Confirmed
  { center: true, bar: "h-4 w-8 rounded" }, // JobDiva Submittals
  { bar: "h-4 w-28 rounded" }, // First PAIR External Submittal
];
const TIMELINE_COLUMN_COUNT = 3 + TIMELINE_SKELETON_BARS.length;

/** A file travels without the page's notice, so say it in the file too. */
const timelineCsvNote = (
  metricsUnavailable: boolean,
  stepTimeUnavailable: boolean,
): string[] => [
  ...(metricsUnavailable
    ? [
        escapeCSV(
          "Note: candidate outcome columns (pass / feedback / PAIR submittals / first feedback / first PAIR external submittal) were unavailable for this export and show —",
        ),
      ]
    : []),
  // Without this, a failed read would be indistinguishable in the file from
  // jobs that predate the Step 5 tracking, which show — too.
  ...(stepTimeUnavailable
    ? [
        escapeCSV(
          "Note: Step 5 time columns (Step 5 Active Time / Step 5 → Launch) were unavailable for this export and show —",
        ),
      ]
    : []),
];

const lagCsvValue = (lag: number | null | undefined): string =>
  // Mirror the UI's lag chip: negative = unreliable posted date
  lag === null || lag === undefined ? "" : lag < 0 ? "n/a" : String(lag);

/** Timeline CSV columns, shared by both exports. Values go through the same
 *  formatters as the screen; the zone is stated once, in the header. */
const timelineCsvHeaders = (withRecruiters: boolean): string[] => [
  "Job Title",
  "JobDiva Ref",
  "Client",
  ...(withRecruiters ? ["Recruiter Emails"] : []),
  "Posted By",
  "Launched By",
  "Posted on JobDiva",
  withEasternLabel("Added to PAIR"),
  withEasternLabel("Launched on PAIR"),
  "Lag (days)",
  // Durations as on screen ("1h 22m"), like the Launch Report's CSV; no zone.
  "Step 5 Active Time",
  "Step 5 → Launch",
  "Active / Archived Jobs",
  "Archive Reason",
  "PAIR Status",
  "Candidates Sourced",
  "Candidates Launched",
  "Pass Candidates",
  "Feedback Total",
  "Feedback Submits",
  "Feedback Rejects",
  "Feedback Unreachable",
  withEasternLabel("First Feedback Submitted At"),
  "PAIR Submittals - Internal",
  "PAIR Submittals - External",
  "PAIR Submittals - JobDiva-Confirmed",
  "JobDiva Submittals",
  withEasternLabel("First PAIR External Submittal"),
];

const timelineCsvRow = (job: JobTimelineEntry, withRecruiters: boolean): string[] => [
  job.title,
  job.jobdiva_id,
  job.customer_name,
  ...(withRecruiters ? [job.recruiter_emails?.join(", ") || ""] : []),
  job.posted_by || "—",
  job.launched_by || "—",
  job.jobdiva_posted_on
    ? formatEasternDate(job.jobdiva_posted_on)
    : job.posted_date_raw || EMPTY_DATE,
  formatEasternDateTime(job.added_to_curate_at),
  formatEasternDateTime(job.curate_launched_at),
  lagCsvValue(job.posted_to_launch_days),
  formatDuration(job.step5_active_minutes),
  formatDuration(job.step5_to_launch_minutes),
  job.is_archived ? "Archived" : "Active",
  job.archive_reason || "",
  job.pair_status,
  String(job.candidates_sourced),
  String(job.candidates_launched),
  countCsvValue(job.pass_candidates),
  countCsvValue(job.feedback_total),
  countCsvValue(job.feedback_submits),
  countCsvValue(job.feedback_rejects),
  countCsvValue(job.feedback_unreachable),
  formatEasternDateTime(job.first_feedback_at),
  countCsvValue(job.pair_internal_submits),
  countCsvValue(job.pair_external_submits),
  String(job.jobdiva_confirmed_subs ?? 0),
  String(job.jobdiva_submittals ?? 0),
  formatEasternDateTime(job.first_pair_external_submit_at),
];

const renderLagChip = (lag: number | null) => {
  if (lag === null || lag === undefined)
    return <span className="text-slate-300">—</span>;
  if (lag < 0) {
    return (
      <span
        title="posted date unreliable"
        className="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold bg-slate-100 text-slate-500 border border-slate-200"
      >
        n/a
      </span>
    );
  }
  let chipClass = "bg-emerald-50 text-emerald-700 border border-emerald-200";
  if (lag > 7) chipClass = "bg-rose-50 text-rose-700 border border-rose-200";
  else if (lag > 3)
    chipClass = "bg-amber-50 text-amber-700 border border-amber-200";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold ${chipClass}`}
    >
      {formatLagValue(lag)} d
    </span>
  );
};

const renderArchivedBadge = (job: JobTimelineEntry) => {
  const isArchived = job.is_archived;
  const badgeClass = isArchived
    ? "bg-slate-100 text-slate-500 border border-slate-200"
    : "bg-emerald-50 text-emerald-700 border border-emerald-200";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold uppercase ${badgeClass}`}
    >
      {isArchived ? "Archived" : "Active"}
    </span>
  );
};

const renderPairStatusBadge = (status: JobTimelineEntry["pair_status"]) => {
  const badgeClass =
    status === "Active"
      ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
      : status === "Unpublished"
        ? "bg-amber-50 text-amber-700 border border-amber-200"
        : "bg-slate-100 text-slate-600 border border-slate-200";
  // Prefixed with "Outreach" so this never reads as a duplicate of the
  // adjacent Active/Archived column — the two badges track unrelated states.
  const label = status === "Active" ? "Outreach Active" : status === "Inactive" ? "Outreach Inactive" : status;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold whitespace-nowrap ${badgeClass}`}
    >
      {label}
    </span>
  );
};

export default function AdminAnalyticsPage() {
  const {
    isAdmin,
    isTeamLead,
    teamName,
    isLoading: isRoleLoading,
    email,
    role,
  } = useUserRole();
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [timelineSearch, setTimelineSearch] = useState("");
  const [timelineFilter, setTimelineFilter] = useState<PairStatusFilter>("All");
  const [timelineStartDate, setTimelineStartDate] = useState("");
  const [timelineEndDate, setTimelineEndDate] = useState("");
  const [showAllTimeline, setShowAllTimeline] = useState(false);
  const [liveAccounts, setLiveAccounts] = useState<LinkedInAccount[] | null>(
    null,
  );
  const [isRefreshingAccounts, setIsRefreshingAccounts] = useState(false);
  const [accountsError, setAccountsError] = useState<string | null>(null);
  // Team scoping: admins can flip between "All Teams" and one team via tabs;
  // team leads are always scoped server-side to their own team.
  const [teams, setTeams] = useState<TeamSummary[]>([]);
  const [activeTeamId, setActiveTeamId] = useState<string | null>(() => {
    // Deep link from the Teams page: /admin/analytics?team=<id>. Read once
    // at mount (plain window access avoids the useSearchParams Suspense
    // requirement for this client-only page).
    if (typeof window === "undefined") return null;
    try {
      return new URLSearchParams(window.location.search).get("team");
    } catch {
      return null;
    }
  });

  const canView = isAdmin || isTeamLead;

  const fetchAnalytics = useCallback(
    async (refresh = false) => {
      if (refresh) setIsRefreshing(true);
      else setIsLoading(true);
      setError(null);
      try {
        // Team leads never pass team_id — the backend pins them to their team.
        const res = await api.adminAnalytics.get(isAdmin ? activeTeamId : null);
        if (res && res.status === "success" && res.data) {
          setData(res.data);
          setLiveAccounts(null); // fall back to the fresh snapshot until the next live refresh
        } else {
          setError(res?.message || "Failed to load analytics data.");
        }
      } catch (err: any) {
        console.error("Error loading analytics:", err);
        setError(
          err?.message || "Access denied or server error loading analytics.",
        );
      } finally {
        setIsLoading(false);
        setIsRefreshing(false);
      }
    },
    [isAdmin, activeTeamId],
  );

  const refreshLinkedInAccounts = useCallback(async () => {
    setIsRefreshingAccounts(true);
    setAccountsError(null);
    try {
      const res = await api.adminAnalytics.linkedinAccounts();
      if (res && res.status === "success" && res.data?.accounts) {
        setLiveAccounts(res.data.accounts as LinkedInAccount[]);
      } else {
        setAccountsError(res?.message || "Failed to load live account status.");
      }
    } catch (err) {
      console.error("Error loading LinkedIn accounts:", err);
      setAccountsError(
        err instanceof Error
          ? err.message
          : "Failed to load live account status.",
      );
    } finally {
      setIsRefreshingAccounts(false);
    }
  }, []);

  useEffect(() => {
    if (!isRoleLoading && canView) {
      fetchAnalytics();
    }
  }, [isRoleLoading, canView, fetchAnalytics]);

  // Admins also load the team list for the scoping tabs.
  useEffect(() => {
    if (isRoleLoading || !isAdmin) return;
    let cancelled = false;
    api.teams
      .list()
      .then((res) => {
        if (!cancelled && res && res.status === "success" && res.data?.teams) {
          setTeams(res.data.teams as TeamSummary[]);
        }
      })
      .catch((err) => console.error("Error loading teams:", err));
    return () => {
      cancelled = true;
    };
  }, [isRoleLoading, isAdmin]);

  if (isRoleLoading) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-primary border-t-transparent" />
          <p className="text-[13px] font-medium text-slate-500">
            Verifying access...
          </p>
        </div>
      </div>
    );
  }

  if (!canView) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center p-6">
        <Card className="max-w-md w-full text-center p-8 border-slate-200 bg-white shadow-sm rounded-xl">
          <div className="mx-auto w-12 h-12 rounded-full bg-red-50 border border-red-100 flex items-center justify-center mb-4 text-red-600">
            <ShieldAlert className="w-6 h-6" />
          </div>
          <h1 className="text-[20px] font-bold text-slate-900 mb-2">
            Access Restricted
          </h1>
          <p className="text-slate-500 text-[13px] mb-6 leading-relaxed">
            You are signed in as{" "}
            <span className="font-semibold text-slate-800">
              {email || "a Recruiter"}
            </span>{" "}
            with the{" "}
            <span className="uppercase font-semibold text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">
              {role.replace("_", " ")}
            </span>{" "}
            role. Analytics are restricted to Administrators and Team Leads.
          </p>
          <Link href="/">
            <Button className="w-full gap-2 bg-slate-900 hover:bg-slate-800 text-white rounded-lg h-10 font-semibold text-[13px]">
              <ArrowLeft className="w-4 h-4" />
              Return to Jobs Dashboard
            </Button>
          </Link>
        </Card>
      </div>
    );
  }

  const overview = data?.overview || {
    total_monitored_jobs: 0,
    total_archived_jobs: 0,
    total_sourced_candidates: 0,
    total_active_recruiters: 0,
  };

  const totalCandidates =
    Object.values(data?.candidates_by_status || {}).reduce(
      (a, b) => a + b,
      0,
    ) || 1;
  const maxJobCount = Math.max(
    ...(data?.jobs_by_customer?.map((c) => c.job_count) || [1]),
    1,
  );
  const maxSrcCount = Math.max(
    ...(data?.candidates_by_source?.map((s) => s.count) || [1]),
    1,
  );

  const launchSpeed: LaunchSpeed = data?.launch_speed || {};

  const trends: WeeklyTrends = data?.weekly_trends || {};
  const trendWeeks = trends.weeks || [];
  const trendSeries = [
    {
      key: "jobs_added",
      label: "Jobs Added",
      values: trends.jobs_added || [],
      barClass: "bg-indigo-500",
    },
    {
      key: "jobs_launched",
      label: "Jobs Launched",
      values: trends.jobs_launched || [],
      barClass: "bg-emerald-500",
    },
    {
      key: "candidates_sourced",
      label: "Candidates Sourced",
      values: trends.candidates_sourced || [],
      barClass: "bg-violet-500",
    },
    {
      key: "candidates_launched",
      label: "Candidates Launched",
      values: trends.candidates_launched || [],
      barClass: "bg-cyan-500",
    },
    {
      key: "jobdiva_submittals",
      label: "JobDiva Submittals",
      values: trends.jobdiva_submittals || [],
      barClass: "bg-amber-500",
    },
  ];
  const hasTrendData = trendWeeks.length > 0;

  const submissionMetrics: SubmissionMetrics = data?.submission_metrics || {};
  const teamScope: TeamScope | null = data?.team_scope || null;
  const activeTeam = teams.find((t) => t.id === activeTeamId) || null;
  const submissionTopJobs = submissionMetrics.top_jobs_by_submittals || [];

  const timelineRows = data?.jobs_timeline || [];
  // Absent (an older backend) is not "unavailable"; only an explicit false is.
  const timelineMetricsUnavailable = data?.jobs_timeline_metrics_available === false;
  const timelineStepTimeUnavailable = data?.jobs_timeline_step_time_available === false;
  // jobs_timeline_total counts every matching job server-side (no LIMIT);
  // timelineRows is capped, so a larger total means older jobs aren't loaded
  // and any date-range filter below is only searching the loaded window.
  const isTimelineTruncated =
    (data?.jobs_timeline_total ?? 0) > timelineRows.length;
  const timelineQuery = timelineSearch.trim().toLowerCase();
  const filteredTimeline = timelineRows.filter((job) => {
    if (timelineFilter !== "All" && job.pair_status !== timelineFilter)
      return false;

    if (timelineStartDate || timelineEndDate) {
      const jobDate = normalizeToUtcDate(
        job.curate_launched_at || job.added_to_curate_at,
      );
      if (!jobDate) return false;

      // Ensure start is not strictly after end
      if (
        timelineStartDate &&
        timelineEndDate &&
        timelineStartDate > timelineEndDate
      ) {
        return false;
      }

      // The job's Eastern calendar date (YYYY-MM-DD), matching the ET
      // timestamps shown in the table.
      const jobDateET = todayEastern(jobDate);

      if (timelineStartDate && jobDateET < timelineStartDate) return false;
      if (timelineEndDate && jobDateET > timelineEndDate) return false;
    }

    if (!timelineQuery) return true;
    const recruiterMatch =
      job.recruiter_emails?.some((e) => e.toLowerCase().includes(timelineQuery)) ?? false;
    const actorMatch = [job.posted_by, job.launched_by].some(
      (e) => !!e && e.toLowerCase().includes(timelineQuery),
    );
    return (
      job.title.toLowerCase().includes(timelineQuery) ||
      job.jobdiva_id.toLowerCase().includes(timelineQuery) ||
      job.customer_name.toLowerCase().includes(timelineQuery) ||
      recruiterMatch ||
      actorMatch
    );
  });
  const visibleTimeline = showAllTimeline
    ? filteredTimeline
    : filteredTimeline.slice(0, 50);

  const linkedInRows: LinkedInAccount[] =
    liveAccounts ?? data?.linkedin_accounts ?? [];

  // Standard pipeline funnel stages aligning with candidate ranking page statuses
  const pipelineStages = [
    {
      key: "launched",
      label: "Launched Candidates",
      aliases: [
        "launched",
        "launched to client",
        "launched_to_client",
        "submitted",
      ],
    },
    {
      key: "pending",
      label: "Pending Candidates",
      aliases: ["pending", "unreviewed", "review", "sourced", "new", ""],
    },
    {
      key: "in_progress",
      label: "In-Progress Candidates",
      aliases: [
        "in progress",
        "in_progress",
        "screening",
        "contacted",
        "outreach",
        "replied",
        "interview",
        "interviewed",
        "interview completed",
        "interview_completed",
      ],
    },
    {
      key: "failed",
      label: "Failed Candidates",
      aliases: [
        "fail",
        "failed",
        "rejected",
        "reject",
        "disqualified",
        "declined",
      ],
      color: "bg-rose-500",
    },
    {
      key: "passed",
      label: "Passed Candidates",
      aliases: [
        "pass",
        "passed",
        "qualified",
        "shortlisted",
        "hired",
        "offer accepted",
        "selected",
        "interested",
        "complete",
        "completed",
      ],
      color: "bg-emerald-600",
    },
  ];

  const getStageCount = (stage: { key: string; aliases: string[] }) => {
    if (!data?.candidates_by_status) return 0;
    let count = 0;
    const keysToMatch = stage.aliases.map((k) =>
      k.toLowerCase().replace(/[-_]/g, " ").trim(),
    );

    Object.entries(data.candidates_by_status).forEach(([statusKey, val]) => {
      const normalized = statusKey.toLowerCase().replace(/[-_]/g, " ").trim();
      if (keysToMatch.includes(normalized)) {
        count += val;
      }
    });
    return count;
  };

  const isStageMatched = (statusKey: string) => {
    const normalized = statusKey.toLowerCase().replace(/[-_]/g, " ").trim();
    return pipelineStages.some((s) =>
      s.aliases
        .map((k) => k.toLowerCase().replace(/[-_]/g, " ").trim())
        .includes(normalized),
    );
  };

  // Shared CSV escaping (lib/csv.ts): formula-injection guard plus quoting.
  const escapeCsvField = (
    value: string | number | null | undefined,
  ): string =>
    escapeCSV(value === null || value === undefined ? "" : String(value));

  const downloadCsv = (lines: string[], filename: string) => {
    // BOM so Excel reads the file as UTF-8 — the "—" placeholders turn into
    // mojibake otherwise.
    const blob = new Blob([UTF8_BOM + lines.join("\n")], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const exportToCSV = () => {
    if (!data) return;

    const passed = getStageCount({
      key: "passed",
      aliases: [
        "pass",
        "passed",
        "qualified",
        "shortlisted",
        "hired",
        "offer accepted",
        "selected",
        "interested",
        "complete",
        "completed",
      ],
    });
    const failed = getStageCount({
      key: "failed",
      aliases: [
        "fail",
        "failed",
        "rejected",
        "reject",
        "disqualified",
        "declined",
      ],
    });
    const totalEvaluated = passed + failed;
    const passRateRatio =
      totalEvaluated > 0
        ? `${Math.round((passed / totalEvaluated) * 100)}%`
        : "0%";
    const poolDensity =
      data.overview.total_monitored_jobs > 0
        ? Math.round(
            data.overview.total_sourced_candidates /
              data.overview.total_monitored_jobs,
          )
        : data.overview.total_sourced_candidates;

    const sm = data.submission_metrics || {};
    const lines = [
      "PAIR - Executive Analytics Report",
      `${withEasternLabel("Generated")}: ${formatEasternDateTime(new Date().toISOString())}`,
      `Scope: ${data.team_scope ? `Team - ${data.team_scope.team_name}` : "All Teams (System-wide)"}`,
      "",
      "--- SYSTEM KPI OVERVIEW ---",
      `Active Monitored Jobs,${data.overview.total_monitored_jobs}`,
      `Sourced Candidates,${data.overview.total_sourced_candidates}`,
      `Active Recruiters,${data.overview.total_active_recruiters}`,
      `Archived Jobs,${data.overview.total_archived_jobs}`,
      "",
      "--- SUBMISSION METRICS (JOBDIVA + PAIR) ---",
      `JobDiva Submittals (all time),${sm.jobdiva_total_submittals ?? 0}`,
      `JobDiva Submittals (last 30 days),${sm.jobdiva_submittals_last_30_days ?? 0}`,
      `Distinct Candidates Submitted (JobDiva),${sm.jobdiva_distinct_candidates ?? 0}`,
      `PAIR Submits,${pairSubmitsTotal(sm)}`,
      // null = the backend couldn't compute the split this time.
      `PAIR Submits - Internal,${countCsvValue(sm.pair_internal_submits)}`,
      `PAIR Submits - External,${countCsvValue(sm.pair_external_submits)}`,
      `JobDiva-Confirmed PAIR Submittals,${sm.pair_external_subs ?? 0}`,
      `Complete Submissions (PAIR),${sm.complete_submissions ?? 0}`,
      `Pass Submissions (PAIR),${sm.pass_submissions ?? 0}`,
      "",
      "--- TOP JOBS BY JOBDIVA SUBMITTALS ---",
      "Job Title,JobDiva Ref,Client,Submittals,Last Submittal",
      ...(sm.top_jobs_by_submittals || []).map((j) =>
        [
          escapeCsvField(j.title),
          escapeCsvField(j.jobdiva_id),
          escapeCsvField(j.customer_name),
          j.submittals,
          escapeCsvField(formatStoredSubmitDate(j.last_submit_date)),
        ].join(","),
      ),
      "",
      "--- CANDIDATE PIPELINE FUNNEL ---",
      "Stage,Count,Percentage",
      ...pipelineStages.map((stage) => {
        const count = getStageCount(stage);
        const pct = Math.round((count / totalCandidates) * 100);
        return `${escapeCsvField(stage.label)},${count},${pct}%`;
      }),
      "",
      "--- SCREENING QUALITY & CONVERSION ---",
      "Metric,Value,Benchmark",
      `Pass Rate Ratio,${passRateRatio},of evaluated candidates shortlisted`,
      `Avg. Pool Density,${poolDensity},candidates sourced per active job`,
      "",
      "--- TALENT SOURCING ORIGINS ---",
      "Source Channel,Profiles,Percentage",
      ...(data.candidates_by_source || []).map((s) => {
        const pct = Math.round((s.count / totalCandidates) * 100);
        return `${escapeCsvField(s.source)},${s.count},${pct}%`;
      }),
      "",
      "--- TOP CLIENT VOLUME (TOP 5) ---",
      "Rank,Customer Name,Active Jobs",
      ...(data.jobs_by_customer || [])
        .slice(0, 5)
        .map(
          (c, idx) =>
            `#${idx + 1},${escapeCsvField(c.customer_name)},${c.job_count}`,
        ),
      "",
      "--- RECRUITER PRODUCTIVITY LEADERBOARD ---",
      "Rank,Recruiter Email,Active Jobs,Candidate Volume",
      ...(data.top_recruiters || []).map(
        (r, idx) =>
          `#${idx + 1},${escapeCsvField(r.email)},${r.active_jobs},${r.total_candidates}`,
      ),
      "",
      "--- JOB LAUNCH TIMELINE ---",
      ...timelineCsvNote(timelineMetricsUnavailable, timelineStepTimeUnavailable),
      toCsv(
        timelineCsvHeaders(false),
        (data.jobs_timeline || []).map((job) => timelineCsvRow(job, false)),
      ),
      // LinkedIn accounts are global infrastructure — only exported on the
      // unscoped (all-teams) view.
      ...(data.team_scope
        ? []
        : [
            "",
            "--- LINKEDIN ACCOUNTS ---",
            `Account,Account ID,Searches,${withEasternLabel("Last Used")},${withEasternLabel("Cooling Down Until")},Last Error`,
            ...(liveAccounts ?? data.linkedin_accounts ?? []).map((acc) =>
              [
                escapeCsvField(acc.account_name || "Unnamed account"),
                escapeCsvField(acc.account_id),
                acc.use_count,
                escapeCsvField(formatEasternDateTime(acc.last_used_at)),
                escapeCsvField(formatEasternDateTime(acc.cooldown_until)),
                escapeCsvField(acc.last_error),
              ].join(","),
            ),
          ]),
    ];

    downloadCsv(lines, `PAIR_Analytics_${todayEastern()}.csv`);
  };

  const exportTimelineToCSV = () => {
    if (!filteredTimeline || filteredTimeline.length === 0) return;

    downloadCsv(
      [
        "--- JOB LAUNCH TIMELINE ---",
        ...timelineCsvNote(timelineMetricsUnavailable, timelineStepTimeUnavailable),
        toCsv(
          timelineCsvHeaders(true),
          filteredTimeline.map((job) => timelineCsvRow(job, true)),
        ),
      ],
      `PAIR_Job_Timeline_${todayEastern()}.csv`,
    );
  };

  return (
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Page Header aligning with Jobs Portfolio */}
      <div className="flex items-center justify-between mt-2">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">
            {isAdmin ? "Admin Analytics" : "Team Lead Dashboard"}
          </h1>
          {isAdmin && !teamScope && (
            <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
              System Overview
            </span>
          )}
          {(teamScope || (!isAdmin && teamName)) && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-2.5 py-0.5 text-[12px] font-semibold text-indigo-700 ring-1 ring-inset ring-indigo-200">
              <UsersRound className="w-3.5 h-3.5" />
              {teamScope?.team_name || teamName}
              {typeof teamScope?.member_count === "number" && (
                <span className="font-medium text-indigo-500">
                  · {teamScope.member_count} people
                </span>
              )}
            </span>
          )}
          {!isAdmin && (
            <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200 uppercase tracking-wide">
              Team Lead
            </span>
          )}
          {data?.warning && (
            <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[12px] font-semibold bg-amber-50 text-amber-800 border border-amber-200">
              {data.warning}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          <Button
            variant="outline"
            onClick={exportToCSV}
            disabled={isLoading || !data}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <Download className="h-4 w-4 text-slate-500" />
            Export Report
          </Button>

          <Button
            variant="outline"
            onClick={() => fetchAnalytics(true)}
            disabled={isLoading || isRefreshing || isRefreshingAccounts}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <RefreshCw
              className={`h-4 w-4 text-slate-500 ${isRefreshing ? "animate-spin text-primary" : ""}`}
            />
            {isRefreshing ? "Refreshing..." : "Refresh"}
          </Button>
        </div>
      </div>

      {/* Team scope tabs — admins flip between the system-wide view and any
          team; selecting a tab refetches server-side scoped analytics. */}
      {isAdmin && teams.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex flex-wrap items-center rounded-lg bg-slate-100 p-0.5">
            <button
              type="button"
              onClick={() => setActiveTeamId(null)}
              className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-colors ${
                activeTeamId === null
                  ? "bg-white text-slate-900 shadow-sm"
                  : "text-slate-500 hover:text-slate-700"
              }`}
            >
              All Teams
            </button>
            {teams.map((team) => (
              <button
                key={team.id}
                type="button"
                onClick={() => setActiveTeamId(team.id)}
                className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-colors ${
                  activeTeamId === team.id
                    ? "bg-white text-slate-900 shadow-sm"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {team.name}
              </button>
            ))}
          </div>
          {activeTeam && (
            <span className="text-[12px] font-medium text-slate-400">
              {activeTeam.lead_emails.length} lead
              {activeTeam.lead_emails.length === 1 ? "" : "s"} ·{" "}
              {activeTeam.member_emails.length} member
              {activeTeam.member_emails.length === 1 ? "" : "s"}
            </span>
          )}
          <Link
            href="/admin/teams"
            className="ml-auto text-[12.5px] font-semibold text-primary hover:underline"
          >
            Manage teams →
          </Link>
        </div>
      )}

      {error ? (
        <div className="flex items-center justify-between rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-red-600" />
            <span>Failed to load analytics: {error}</span>
          </div>
          <button
            type="button"
            className="font-semibold underline decoration-red-400 underline-offset-2 hover:text-red-900"
            onClick={() => fetchAnalytics()}
          >
            Retry
          </button>
        </div>
      ) : null}

      {/* KPI Stat Cards (4 columns) */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Active Monitored Jobs
            </span>
            <div className="w-8 h-8 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600">
              <Briefcase className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {overview.total_monitored_jobs}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              Live Portfolios
            </div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Sourced Candidates
            </span>
            <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-100 flex items-center justify-center text-emerald-600">
              <Users className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {overview.total_sourced_candidates.toLocaleString()}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              Total Talent Pool
            </div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Active Recruiters
            </span>
            <div className="w-8 h-8 rounded-lg bg-violet-50 border border-violet-100 flex items-center justify-center text-violet-600">
              <UserCheck className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {overview.total_active_recruiters}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              Assigned Team Members
            </div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Archived Jobs
            </span>
            <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-100 flex items-center justify-center text-amber-600">
              <Archive className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {overview.total_archived_jobs}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              Completed Jobs
            </div>
          </div>
        </div>
      </div>

      {/* Launch Velocity KPI Row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Median Posted → Launch
            </span>
            <div className="w-8 h-8 rounded-lg bg-sky-50 border border-sky-100 flex items-center justify-center text-sky-600">
              <Timer className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {typeof launchSpeed.median_days_posted_to_launch === "number"
                  ? `${formatLagValue(launchSpeed.median_days_posted_to_launch)} days`
                  : "—"}
                {typeof launchSpeed.avg_days_posted_to_launch === "number" && (
                  <span className="text-[13px] font-semibold text-slate-400 ml-2">
                    avg {formatLagValue(launchSpeed.avg_days_posted_to_launch)}d
                  </span>
                )}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              JobDiva post → PAIR launch
            </div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Jobs Launched on PAIR
            </span>
            <div className="w-8 h-8 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600">
              <Rocket className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {typeof launchSpeed.launched_jobs === "number"
                  ? launchSpeed.launched_jobs.toLocaleString()
                  : "—"}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              all time
            </div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Awaiting Launch
            </span>
            <div className="w-8 h-8 rounded-lg bg-cyan-50 border border-cyan-100 flex items-center justify-center text-cyan-600">
              <Clock className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">
                {typeof launchSpeed.unlaunched_active_jobs === "number"
                  ? launchSpeed.unlaunched_active_jobs.toLocaleString()
                  : "—"}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              active, not yet launched
            </div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">
              Aged Unlaunched
            </span>
            <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-100 flex items-center justify-center text-amber-600">
              <Hourglass className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-amber-600 leading-none">
                {typeof launchSpeed.aged_unlaunched_jobs === "number"
                  ? launchSpeed.aged_unlaunched_jobs.toLocaleString()
                  : "—"}
              </div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
              added &gt;7 days ago, never launched
            </div>
          </div>
        </div>
      </div>

      {/* Submission Metrics (JobDiva v2 BI submittals + local PAIR funnel) */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
          <div>
            <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
              <Send className="w-4 h-4 text-amber-600" />
              Submission Metrics
            </h2>
            <p className="text-[12px] text-slate-500 mt-0.5">
              External submittals reported by JobDiva alongside the PAIR
              screening funnel — refreshed every sync cycle
            </p>
          </div>
          {!isLoading && (
            <span className="inline-flex items-center rounded-md bg-amber-50 px-2 py-1 text-[12px] font-semibold text-amber-700 border border-amber-200">
              {(
                submissionMetrics.jobdiva_submittals_last_30_days ?? 0
              ).toLocaleString()}{" "}
              in last 30 days
            </span>
          )}
        </div>

        <div className="p-6 space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div className="rounded-xl border border-slate-200 p-4 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-500">
                  JobDiva Submittals
                </span>
                <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-100 flex items-center justify-center text-amber-600">
                  <Send className="w-4 h-4" />
                </div>
              </div>
              <div className="mt-3">
                {isLoading ? (
                  <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
                ) : (
                  <div className="text-[26px] font-bold text-slate-900 leading-none">
                    {(
                      submissionMetrics.jobdiva_total_submittals ?? 0
                    ).toLocaleString()}
                  </div>
                )}
                <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
                  all time, from JobDiva v2 BI
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-slate-200 p-4 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-500">
                  Candidates Submitted
                </span>
                <div className="w-8 h-8 rounded-lg bg-sky-50 border border-sky-100 flex items-center justify-center text-sky-600">
                  <UsersRound className="w-4 h-4" />
                </div>
              </div>
              <div className="mt-3">
                {isLoading ? (
                  <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
                ) : (
                  <div className="text-[26px] font-bold text-slate-900 leading-none">
                    {(
                      submissionMetrics.jobdiva_distinct_candidates ?? 0
                    ).toLocaleString()}
                  </div>
                )}
                <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
                  distinct candidates submitted
                </div>
              </div>
            </div>

            {/* Two views of the same funnel step, deliberately side by side:
                what PAIR recorded, and what JobDiva confirms. */}
            <div className="rounded-xl border border-slate-200 p-4 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-500">
                  <HeaderHint hint="A recruiter pressed Submit in PAIR. Internal = submitted to a hiring manager for review; External = submitted to the client. Submits recorded before the split existed count as External.">
                    PAIR Submits
                  </HeaderHint>
                </span>
                <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-100 flex items-center justify-center text-emerald-600">
                  <BadgeCheck className="w-4 h-4" />
                </div>
              </div>
              <div className="mt-3">
                {isLoading ? (
                  <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
                ) : (
                  <div className="text-[26px] font-bold text-slate-900 leading-none">
                    {pairSubmitsTotal(submissionMetrics).toLocaleString()}
                  </div>
                )}
                {/* null split = the backend couldn't compute it this time;
                    hide the line rather than show a misleading 0 · 0. */}
                {!isLoading &&
                  submissionMetrics.pair_internal_submits != null &&
                  submissionMetrics.pair_external_submits != null && (
                    <div className="text-[12px] font-semibold text-slate-600 mt-1.5 whitespace-nowrap">
                      {submissionMetrics.pair_internal_submits.toLocaleString()}{" "}
                      internal ·{" "}
                      {submissionMetrics.pair_external_submits.toLocaleString()}{" "}
                      external
                    </div>
                  )}
                <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
                  recruiter pressed Submit in PAIR
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-slate-200 p-4 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-500">
                  {/* Same number as the old "PAIR External Subs" card: what
                      JobDiva confirms, next to what PAIR recorded. */}
                  <HeaderHint hint="Formerly “PAIR External Subs”. JobDiva submittals to the job's contact for a candidate carrying the PAIR Candidates = Pass qualification (set within 60 days of the submittal), as verified from JobDiva on each sync. v1 and v2 of one JobDiva job count once.">
                    JobDiva-Confirmed PAIR Subs
                  </HeaderHint>
                </span>
                <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-100 flex items-center justify-center text-emerald-600">
                  <BadgeCheck className="w-4 h-4" />
                </div>
              </div>
              <div className="mt-3">
                {isLoading ? (
                  <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
                ) : (
                  <div className="text-[26px] font-bold text-slate-900 leading-none">
                    {(
                      submissionMetrics.pair_external_subs ?? 0
                    ).toLocaleString()}
                  </div>
                )}
                <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
                  JobDiva submittals matching PAIR criteria
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-slate-200 p-4 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-500">
                  Complete / Pass
                </span>
                <div className="w-8 h-8 rounded-lg bg-violet-50 border border-violet-100 flex items-center justify-center text-violet-600">
                  <ClipboardCheck className="w-4 h-4" />
                </div>
              </div>
              <div className="mt-3">
                {isLoading ? (
                  <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
                ) : (
                  <div className="text-[26px] font-bold text-slate-900 leading-none">
                    {(
                      submissionMetrics.complete_submissions ?? 0
                    ).toLocaleString()}
                    <span className="text-[13px] font-semibold text-emerald-600 ml-2">
                      {(
                        submissionMetrics.pass_submissions ?? 0
                      ).toLocaleString()}{" "}
                      pass
                    </span>
                  </div>
                )}
                <div className="text-[12px] text-slate-400 mt-1.5 font-medium">
                  PAIR screening submissions
                </div>
              </div>
            </div>
          </div>

          {/* Top jobs by JobDiva submittals */}
          {!isLoading && submissionTopJobs.length > 0 && (
            <div className="border border-slate-200 rounded-xl overflow-hidden">
              <div className="px-4 py-2.5 bg-slate-50 border-b border-slate-200 text-[12.5px] font-bold text-slate-500">
                Top Jobs by JobDiva Submittals
              </div>
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50/50 font-bold text-slate-500 text-[12px]">
                    <th className="py-2 px-4">Job</th>
                    <th className="py-2 px-4">Client</th>
                    <th className="py-2 px-4 text-center">Submittals</th>
                    <th className="py-2 px-4 text-right">Last Submittal</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-[13px]">
                  {submissionTopJobs.map((job) => (
                    <tr
                      key={job.job_id}
                      className="hover:bg-[#f6f8fb] transition-colors"
                    >
                      <td className="py-2.5 px-4">
                        <div
                          className="font-semibold text-slate-800 max-w-[280px] truncate"
                          title={job.title}
                        >
                          {job.title}
                        </div>
                        <div className="font-mono text-[11px] text-slate-400 mt-0.5">
                          {job.jobdiva_id || "—"}
                        </div>
                      </td>
                      <td className="py-2.5 px-4 text-slate-600">
                        <div
                          className="max-w-[180px] truncate"
                          title={job.customer_name}
                        >
                          {job.customer_name}
                        </div>
                      </td>
                      <td className="py-2.5 px-4 text-center font-bold text-amber-600">
                        {job.submittals.toLocaleString()}
                      </td>
                      <td className="py-2.5 px-4 text-right text-slate-600 whitespace-nowrap">
                        {formatStoredSubmitDate(job.last_submit_date)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {/* Weekly Activity Trends */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
          <div>
            <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
              <Activity className="w-4 h-4 text-indigo-600" />
              Weekly Activity Trends
            </h2>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Jobs and candidate flow over the last 8 weeks
            </p>
          </div>
          {!isLoading && hasTrendData && (
            <span className="inline-flex items-center rounded-md bg-slate-100 px-2 py-1 text-[12px] font-semibold text-slate-700">
              {formatWeekLabel(trendWeeks[0])} –{" "}
              {formatWeekLabel(trendWeeks[trendWeeks.length - 1])}
            </span>
          )}
        </div>

        <div className="p-6">
          {isLoading ? (
            <div className="space-y-6">
              {[1, 2, 3, 4].map((i) => (
                <div key={i} className="flex items-center gap-5">
                  <div className="w-44 shrink-0 space-y-2">
                    <div className="h-4 w-32 bg-slate-100 animate-pulse rounded" />
                    <div className="h-3 w-20 bg-slate-100 animate-pulse rounded" />
                  </div>
                  <div className="h-16 flex-1 bg-slate-100 animate-pulse rounded" />
                </div>
              ))}
            </div>
          ) : !hasTrendData ? (
            <div className="text-center py-10 text-slate-400 text-[13px]">
              No weekly activity recorded yet.
            </div>
          ) : (
            <div className="space-y-6">
              {trendSeries.map((series) => {
                const values = trendWeeks.map((_, i) => series.values[i] ?? 0);
                const total = values.reduce((a, b) => a + b, 0);
                const seriesMax = Math.max(...values, 1);

                return (
                  <div key={series.key} className="flex items-center gap-5">
                    <div className="w-44 shrink-0">
                      <div className="text-[13px] font-semibold text-slate-700">
                        {series.label}
                      </div>
                      <div className="text-[12px] text-slate-400 font-mono mt-0.5">
                        {total.toLocaleString()} total
                      </div>
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-end gap-1.5 h-16">
                        {values.map((value, i) => (
                          <div
                            key={trendWeeks[i]}
                            title={`Week of ${formatWeekLabel(trendWeeks[i])}: ${value}`}
                            className="flex-1 h-full flex items-end"
                          >
                            {value > 0 ? (
                              <div
                                className={`w-full rounded-t ${series.barClass} transition-all duration-500`}
                                style={{
                                  height: `${Math.max(Math.round((value / seriesMax) * 100), 8)}%`,
                                }}
                              />
                            ) : (
                              <div className="w-full h-[2px] rounded bg-slate-100" />
                            )}
                          </div>
                        ))}
                      </div>
                      <div className="flex justify-between mt-1.5 text-[11px] font-medium text-slate-400">
                        <span>{formatWeekLabel(trendWeeks[0])}</span>
                        <span>
                          {formatWeekLabel(trendWeeks[trendWeeks.length - 1])}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Middle Section: Funnel & Clients */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Candidate Pipeline Funnel (2 cols) */}
        <div className="lg:col-span-2 bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden flex flex-col">
          <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
            <div>
              <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-primary" />
                Candidate Pipeline Funnel
              </h2>
              <p className="text-[12px] text-slate-500 mt-0.5">
                Sourcing and outreach conversion distribution across all jobs
              </p>
            </div>
            <span className="inline-flex items-center rounded-md bg-slate-100 px-2 py-1 text-[12px] font-semibold text-slate-700">
              {overview.total_sourced_candidates.toLocaleString()} Total
            </span>
          </div>

          <div className="p-6 flex-1 flex flex-col justify-between">
            {isLoading ? (
              <div className="space-y-4">
                {[1, 2, 3, 4, 5].map((i) => (
                  <div key={i} className="space-y-2">
                    <div className="flex justify-between">
                      <div className="h-4 w-32 bg-slate-100 animate-pulse rounded" />
                      <div className="h-4 w-12 bg-slate-100 animate-pulse rounded" />
                    </div>
                    <div className="h-2 w-full bg-slate-100 animate-pulse rounded-full" />
                  </div>
                ))}
              </div>
            ) : (
              <div className="space-y-5 my-auto">
                {pipelineStages.map((stage, stageIdx) => {
                  const count = getStageCount(stage);
                  const percentage = Math.round(
                    (count / totalCandidates) * 100,
                  );
                  const barColor = (stage as any).color || "bg-primary";

                  return (
                    <div key={stage.key} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <span className="font-semibold text-slate-700">
                          {stage.label}
                        </span>
                        <div className="flex items-center gap-2 font-mono">
                          <span className="font-bold text-slate-900">
                            {count.toLocaleString()}
                          </span>
                          <span className="text-[12px] text-slate-400 w-10 text-right">
                            ({percentage}%)
                          </span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full ${barColor} rounded-full transition-all duration-500`}
                          style={{
                            width: `${count > 0 ? Math.max(percentage, 3) : 0}%`,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}

                {/* Catch-all for any other custom statuses */}
                {Object.entries(data?.candidates_by_status || {}).map(
                  ([status, count]) => {
                    if (isStageMatched(status)) return null;
                    const percentage = Math.round(
                      (count / totalCandidates) * 100,
                    );
                    return (
                      <div key={status} className="space-y-2">
                        <div className="flex items-center justify-between text-[13px]">
                          <span className="font-semibold text-slate-700 capitalize">
                            {status}
                          </span>
                          <div className="flex items-center gap-2 font-mono">
                            <span className="font-bold text-slate-900">
                              {count.toLocaleString()}
                            </span>
                            <span className="text-[12px] text-slate-400 w-10 text-right">
                              ({percentage}%)
                            </span>
                          </div>
                        </div>
                        <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-slate-500 rounded-full transition-all duration-500"
                            style={{
                              width: `${count > 0 ? Math.max(percentage, 3) : 0}%`,
                            }}
                          />
                        </div>
                      </div>
                    );
                  },
                )}
              </div>
            )}
          </div>
        </div>

        {/* Top Clients Volume (1 col) */}
        <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden flex flex-col">
          <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
            <div>
              <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
                <Building2 className="w-4 h-4 text-teal-600" />
                Top Client Volume
              </h2>
              <p className="text-[12px] text-slate-500 mt-0.5">
                Top 5 most active accounts by jobs
              </p>
            </div>
          </div>

          <div className="p-6 flex-1 flex flex-col justify-between">
            {isLoading ? (
              <div className="space-y-4">
                {[1, 2, 3, 4, 5].map((i) => (
                  <div key={i} className="space-y-2">
                    <div className="flex justify-between">
                      <div className="h-4 w-28 bg-slate-100 animate-pulse rounded" />
                      <div className="h-4 w-10 bg-slate-100 animate-pulse rounded" />
                    </div>
                    <div className="h-2 w-full bg-slate-100 animate-pulse rounded-full" />
                  </div>
                ))}
              </div>
            ) : (data?.jobs_by_customer || []).length === 0 ? (
              <div className="text-center py-10 text-slate-400 text-[13px] my-auto">
                No customer accounts found.
              </div>
            ) : (
              <div className="space-y-5 my-auto">
                {(data?.jobs_by_customer || []).slice(0, 5).map((cust, idx) => {
                  const widthPct = Math.round(
                    (cust.job_count / maxJobCount) * 100,
                  );

                  return (
                    <div key={cust.customer_name} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <div className="flex items-center gap-2 truncate pr-3">
                          <span className="text-[12px] font-mono font-semibold text-slate-400">
                            #{idx + 1}
                          </span>
                          <span className="font-semibold text-slate-800 truncate">
                            {cust.customer_name}
                          </span>
                        </div>
                        <div className="flex items-center gap-1 font-mono shrink-0">
                          <span className="font-bold text-slate-900">
                            {cust.job_count}
                          </span>
                          <span className="text-[12px] text-slate-400">
                            {cust.job_count === 1 ? "Job" : "Jobs"}
                          </span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-teal-500 rounded-full transition-all duration-500"
                          style={{
                            width: `${cust.job_count > 0 ? Math.max(widthPct, 3) : 0}%`,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Talent Sourcing Origins & Quality Section */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Talent Sources Breakdown (2 cols) */}
        <div className="lg:col-span-2 bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden flex flex-col">
          <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
            <div>
              <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
                <Users className="w-4 h-4 text-violet-600" />
                Talent Sourcing Origins
              </h2>
              <p className="text-[12px] text-slate-500 mt-0.5">
                Distribution of candidate profiles by ingestion channel
              </p>
            </div>
            <span className="inline-flex items-center rounded-md bg-slate-100 px-2 py-1 text-[12px] font-semibold text-slate-700">
              {overview.total_sourced_candidates.toLocaleString()} Profiles
            </span>
          </div>

          <div className="p-6 flex-1 flex flex-col justify-between">
            {isLoading ? (
              <div className="space-y-4">
                {[1, 2, 3].map((i) => (
                  <div key={i} className="space-y-2">
                    <div className="flex justify-between">
                      <div className="h-4 w-32 bg-slate-100 animate-pulse rounded" />
                      <div className="h-4 w-12 bg-slate-100 animate-pulse rounded" />
                    </div>
                    <div className="h-2 w-full bg-slate-100 animate-pulse rounded-full" />
                  </div>
                ))}
              </div>
            ) : (data?.candidates_by_source || []).length === 0 ? (
              <div className="text-center py-10 text-slate-400 text-[13px] my-auto">
                No sourcing channel data recorded yet.
              </div>
            ) : (
              <div className="space-y-5 my-auto">
                {(data?.candidates_by_source || []).map((srcItem) => {
                  const percentage = Math.round(
                    (srcItem.count / (overview.total_sourced_candidates || 1)) *
                      100,
                  );
                  const widthPct = Math.round(
                    (srcItem.count / maxSrcCount) * 100,
                  );

                  return (
                    <div key={srcItem.source} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <span className="font-semibold text-slate-700">
                          {srcItem.source}
                        </span>
                        <div className="flex items-center gap-2 font-mono">
                          <span className="font-bold text-slate-900">
                            {srcItem.count.toLocaleString()}
                          </span>
                          <span className="text-[12px] text-slate-400 w-10 text-right">
                            ({percentage}%)
                          </span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-violet-600 rounded-full transition-all duration-500"
                          style={{
                            width: `${srcItem.count > 0 ? Math.max(widthPct, 3) : 0}%`,
                          }}
                        />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>

        {/* Screening Quality Card (1 col) */}
        <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden flex flex-col justify-between">
          <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
            <div>
              <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
                <Award className="w-4 h-4 text-emerald-600" />
                Screening Quality & Conversion
              </h2>
              <p className="text-[12px] text-slate-500 mt-0.5">
                Overall qualification benchmark
              </p>
            </div>
          </div>

          <div className="p-6 flex-1 flex flex-col justify-between space-y-4 my-auto">
            <div className="p-3.5 rounded-xl bg-emerald-50/60 border border-emerald-100/80 flex items-center justify-between">
              <div>
                <div className="text-[12px] font-semibold text-emerald-800 uppercase tracking-wider">
                  Pass Rate Ratio
                </div>
                <div className="text-[26px] font-bold text-emerald-950 mt-1">
                  {(() => {
                    const passed = getStageCount({
                      key: "passed",
                      aliases: [
                        "pass",
                        "passed",
                        "qualified",
                        "shortlisted",
                        "hired",
                        "offer accepted",
                        "selected",
                        "interested",
                        "complete",
                        "completed",
                      ],
                    });
                    const failed = getStageCount({
                      key: "failed",
                      aliases: [
                        "fail",
                        "failed",
                        "rejected",
                        "reject",
                        "disqualified",
                        "declined",
                      ],
                    });
                    const totalEvaluated = passed + failed;
                    return totalEvaluated > 0
                      ? `${Math.round((passed / totalEvaluated) * 100)}%`
                      : "0%";
                  })()}
                </div>
                <div className="text-[12px] text-emerald-700 mt-0.5 font-medium">
                  of evaluated candidates shortlisted
                </div>
              </div>
              <div className="w-10 h-10 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600 font-bold">
                ✓
              </div>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200/80 flex items-center justify-between">
              <div>
                <div className="text-[12px] font-semibold text-slate-500 uppercase tracking-wider">
                  Avg. Pool Density
                </div>
                <div className="text-[26px] font-bold text-slate-900 mt-1">
                  {overview.total_monitored_jobs > 0
                    ? Math.round(
                        overview.total_sourced_candidates /
                          overview.total_monitored_jobs,
                      )
                    : overview.total_sourced_candidates}
                </div>
                <div className="text-[12px] text-slate-500 mt-0.5 font-medium">
                  candidates sourced per active job
                </div>
              </div>
              <div className="w-10 h-10 rounded-full bg-slate-200/60 flex items-center justify-center text-slate-600 font-bold">
                👥
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Recruiter Leaderboard Table aligning with Jobs Portfolio table */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex items-center justify-between">
          <div>
            <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
              <Award className="w-4 h-4 text-amber-500" />
              Recruiter Productivity Leaderboard
            </h2>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Team members ranked by active jobs and candidate volume
            </p>
          </div>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
            Top {data?.top_recruiters?.length || 0}
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 font-bold text-slate-500 text-[12.5px]">
                <th className="py-3 px-6 w-20 text-center">Rank</th>
                <th className="py-3 px-6">Recruiter Team Member</th>
                <th className="py-3 px-6 text-center">Assigned Active Jobs</th>
                <th className="py-3 px-6 text-center">
                  Total Sourced Candidates
                </th>
                <th className="py-3 px-6 text-right">Activity Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[14px]">
              {isLoading ? (
                [1, 2, 3].map((i) => (
                  <tr key={i}>
                    <td className="py-4 px-6 text-center">
                      <div className="h-4 w-6 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                    <td className="py-4 px-6">
                      <div className="h-4 w-48 bg-slate-100 animate-pulse rounded" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-4 w-12 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-4 w-16 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                    <td className="py-4 px-6 text-right">
                      <div className="h-6 w-20 bg-slate-100 animate-pulse rounded ml-auto" />
                    </td>
                  </tr>
                ))
              ) : (data?.top_recruiters || []).length === 0 ? (
                <tr>
                  <td
                    colSpan={5}
                    className="py-12 text-center text-slate-400 text-[13px]"
                  >
                    No active recruiter assignments found.
                  </td>
                </tr>
              ) : (
                (data?.top_recruiters || []).map((rec, idx) => {
                  return (
                    <tr
                      key={rec.email}
                      className="hover:bg-[#f6f8fb] transition-colors"
                    >
                      <td className="py-3.5 px-6 text-center font-semibold text-slate-600">
                        <span className="inline-flex items-center justify-center w-6 h-6 rounded-full bg-slate-100 text-slate-700 font-semibold text-[12px]">
                          #{idx + 1}
                        </span>
                      </td>
                      <td className="py-3.5 px-6 font-semibold text-slate-800">
                        {rec.email}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-slate-800">
                        {rec.active_jobs}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-primary">
                        {rec.total_candidates.toLocaleString()}
                      </td>
                      <td className="py-3.5 px-6 text-right">
                        {(() => {
                          let badgeText = "Standard";
                          let badgeClass =
                            "bg-slate-100 text-slate-600 border border-slate-200";

                          if (
                            rec.active_jobs >= 10 ||
                            (rec.active_jobs >= 5 &&
                              rec.total_candidates >= 100)
                          ) {
                            badgeText = "Power Recruiter";
                            badgeClass =
                              "bg-amber-50 text-amber-700 border border-amber-200";
                          } else if (rec.active_jobs >= 6) {
                            badgeText = "High Activity";
                            badgeClass =
                              "bg-emerald-50 text-emerald-700 border border-emerald-200";
                          } else if (rec.active_jobs >= 3) {
                            badgeText = "Active";
                            badgeClass =
                              "bg-indigo-50 text-indigo-700 border border-indigo-200";
                          } else if (rec.active_jobs >= 1) {
                            badgeText = "Light Activity";
                            badgeClass =
                              "bg-sky-50 text-sky-700 border border-sky-200";
                          } else {
                            badgeText = "Inactive";
                            badgeClass =
                              "bg-slate-100 text-slate-500 border border-slate-200";
                          }

                          return (
                            <span
                              className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${badgeClass}`}
                            >
                              {badgeText}
                            </span>
                          );
                        })()}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Job Launch Timeline */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd]">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
                <CalendarClock className="w-4 h-4 text-indigo-600" />
                Job Launch Timeline
              </h2>
              <p className="text-[12px] text-slate-500 mt-0.5">
                JobDiva posting → PAIR launch lifecycle, most recent first
              </p>
            </div>
            <div className="flex items-center gap-3 overflow-x-auto pb-1 hide-scrollbar">
              <div className="relative shrink-0">
                <Search className="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2" />
                <input
                  type="text"
                  value={timelineSearch}
                  onChange={(e) => {
                    setTimelineSearch(e.target.value);
                    setShowAllTimeline(false);
                  }}
                  placeholder="Search title, ref or client..."
                  className="h-8 w-56 rounded-lg border border-slate-200 bg-white pl-8 pr-3 text-[13px] text-slate-700 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/50"
                />
              </div>
              <div className="flex items-center gap-1 bg-white border border-slate-200 rounded-lg px-1.5 py-0.5 shadow-sm shrink-0">
                <label
                  htmlFor="timelineStartDate"
                  className="text-[12px] font-medium text-slate-500 cursor-pointer"
                >
                  From:
                </label>
                <input
                  id="timelineStartDate"
                  type="date"
                  aria-label="Start date filter"
                  value={timelineStartDate}
                  onChange={(e) => {
                    setTimelineStartDate(e.target.value);
                    setShowAllTimeline(false);
                  }}
                  className="h-7 w-[110px] rounded-md hover:bg-slate-50 transition-colors bg-transparent px-1 text-[12.5px] text-slate-700 focus:outline-none focus:ring-2 focus:ring-primary/20"
                />
                <label
                  htmlFor="timelineEndDate"
                  className="text-[12px] font-medium text-slate-500 cursor-pointer"
                >
                  To:
                </label>
                <input
                  id="timelineEndDate"
                  type="date"
                  aria-label="End date filter"
                  value={timelineEndDate}
                  onChange={(e) => {
                    setTimelineEndDate(e.target.value);
                    setShowAllTimeline(false);
                  }}
                  className="h-7 w-[110px] rounded-md hover:bg-slate-50 transition-colors bg-transparent px-1 text-[12.5px] text-slate-700 focus:outline-none focus:ring-2 focus:ring-primary/20"
                />
                {(timelineStartDate || timelineEndDate) && (
                  <button
                    type="button"
                    onClick={() => {
                      setTimelineStartDate("");
                      setTimelineEndDate("");
                      setShowAllTimeline(false);
                    }}
                    className="p-1 hover:bg-slate-100 rounded-md text-slate-400 hover:text-slate-600 transition-colors mr-0.5"
                    aria-label="Clear date filter"
                  >
                    <X className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>
              <div className="inline-flex items-center rounded-lg bg-slate-100 p-0.5 shrink-0">
                {PAIR_STATUS_FILTERS.map((filterOption) => (
                  <button
                    key={filterOption}
                    type="button"
                    onClick={() => {
                      setTimelineFilter(filterOption);
                      setShowAllTimeline(false);
                    }}
                    className={`px-3 py-1 text-[12px] font-medium rounded-md transition-colors ${
                      timelineFilter === filterOption
                        ? "bg-white text-slate-900 shadow-sm"
                        : "text-slate-500 hover:text-slate-700 hover:bg-slate-200/50"
                    }`}
                  >
                    {filterOption}
                  </button>
                ))}
              </div>
              <Button
                variant="outline"
                onClick={exportTimelineToCSV}
                disabled={isLoading || filteredTimeline.length === 0}
                className="flex shrink-0 items-center gap-2 h-8 px-3 border-slate-200 text-slate-700 font-semibold text-[12px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
              >
                <Download className="h-3.5 w-3.5 text-slate-500" />
                Export CSV
              </Button>
              <span className="shrink-0 text-[12px] font-medium text-slate-400 whitespace-nowrap">
                Showing {filteredTimeline.length} of{" "}
                {data?.jobs_timeline_total || timelineRows.length} jobs
                {isTimelineTruncated && (
                  <span className="text-amber-600">
                    {" "}(most recent {timelineRows.length} loaded
                    {(timelineStartDate || timelineEndDate)
                      ? " — older jobs outside this window aren't included in the date filter"
                      : ""}
                    )
                  </span>
                )}
              </span>
            </div>
          </div>
        </div>

        {!isLoading && timelineMetricsUnavailable && timelineRows.length > 0 && (
          <div
            role="status"
            className="flex items-start gap-2 px-6 py-2.5 border-b border-amber-200 bg-amber-50 text-[12.5px] font-medium text-amber-800"
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
            <span>
              Candidate outcome columns (Pass Candidates, Feedback, PAIR
              Submittals, First Feedback and First PAIR External Submittal)
              couldn&apos;t be loaded this time and show &ldquo;—&rdquo;. Job
              columns are current. Reload to try again.
            </span>
          </div>
        )}

        {/* Separate from the candidate notice: either read can fail alone, and
            without it a failed read looks like jobs that predate the tracking. */}
        {!isLoading && timelineStepTimeUnavailable && timelineRows.length > 0 && (
          <div
            role="status"
            className="flex items-start gap-2 px-6 py-2.5 border-b border-amber-200 bg-amber-50 text-[12.5px] font-medium text-amber-800"
          >
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
            <span>
              Step 5 time columns (Step 5 Active Time and Step 5 &rarr; Launch)
              couldn&apos;t be loaded this time and show &ldquo;—&rdquo;. Reload
              to try again.
            </span>
          </div>
        )}

        <div className="overflow-auto max-h-[700px] relative">
          <table className="w-full text-left border-collapse">
            <thead className="sticky top-0 z-20 bg-slate-50 shadow-[0_1px_0_0_#e2e8f0]">
              {/* Two header rows: only "PAIR Submittals" spans a second
                  (Internal / External) row; every other column spans both. */}
              <tr className="font-bold text-slate-500 text-[12.5px]">
                <th
                  scope="col"
                  rowSpan={2}
                  aria-label="Row Number"
                  className="py-3 px-4 w-[50px] min-w-[50px] max-w-[50px] text-center sticky left-0 z-30 bg-slate-50"
                >
                  #
                </th>
                <th rowSpan={2} className="py-3 px-6 w-[130px] min-w-[130px] max-w-[130px] sticky left-[50px] z-30 bg-slate-50">
                  JobDiva ID
                </th>
                <th rowSpan={2} className="py-3 px-6 w-[280px] min-w-[280px] max-w-[280px] sticky left-[180px] z-30 bg-slate-50 shadow-[1px_0_0_0_#e2e8f0]">
                  Job Title
                </th>
                <th rowSpan={2} className="py-3 px-6">Client</th>
                <th rowSpan={2} className="py-3 px-6">Recruiter Emails</th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">
                  <HeaderHint hint="Who first saved this job in PAIR. Recorded since 09/23/2026; older jobs show —.">
                    Posted By
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">
                  <HeaderHint hint="Who first launched PAIR on this job. Recorded since 09/23/2026; older launches show —.">
                    Launched By
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">Posted (JobDiva)</th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">
                  {withEasternLabel("Added (PAIR)")}
                </th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">
                  {withEasternLabel("Launched (PAIR)")}
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center">Lag</th>
                <th rowSpan={2} className="py-3 px-6 text-center whitespace-nowrap">
                  <HeaderHint hint="Time recruiters had Step 5 (Source) open and in use on this job, summed across every visit and every recruiter. It pauses while the tab is hidden or after 5 minutes with no activity. Tracked since 09/23/2026; jobs worked earlier show —.">
                    Step 5 Active Time
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center whitespace-nowrap">
                  <HeaderHint hint="Wall-clock time from the first time anyone opened Step 5 (Source) on this job to its first SUCCESSFUL launch: the first time a candidate was actually launched to PAIR (the Launch Report's PAIR Launch). That can be later than Launched (PAIR), which is the first launch click. Shows — until the job has launched successfully, and for jobs worked before tracking started on 09/23/2026.">
                    Step 5 &rarr; Launch
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center">
                  Active / Archived Jobs
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center">PAIR Status</th>
                <th rowSpan={2} className="py-3 px-6 text-center">Sourced</th>
                <th rowSpan={2} className="py-3 px-6 text-center">Launched</th>
                <th rowSpan={2} className="py-3 px-6 text-center whitespace-nowrap">
                  <HeaderHint hint="Candidates whose PAIR interview result is Pass, by the same rule as the rank list.">
                    Pass Candidates
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center">
                  <HeaderHint hint="Candidates with a recorded recruiter decision: Submit, Reject or Unreachable.">
                    Feedback
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">
                  {withEasternLabel("First Feedback Submitted At")}
                </th>
                <th colSpan={2} className="pt-3 pb-1 px-6 text-center whitespace-nowrap">
                  <HeaderHint hint="Submits recorded in PAIR. Internal = submitted to a hiring manager for review; External = submitted to the client.">
                    PAIR Submittals
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center whitespace-nowrap">
                  <HeaderHint hint="JobDiva submittal to the job contact for a PAIR-passed candidate, as verified from JobDiva.">
                    JobDiva-Confirmed
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 text-center whitespace-nowrap">
                  <HeaderHint hint="Every JobDiva submittal on this job, whether or not it came through PAIR.">
                    JobDiva Submittals
                  </HeaderHint>
                </th>
                <th rowSpan={2} className="py-3 px-6 whitespace-nowrap">
                  <HeaderHint hint="Earliest external Submit recorded in PAIR for this job. PAIR keeps only each candidate's latest decision, so a Submit later changed to Reject no longer counts.">
                    {withEasternLabel("First PAIR External Submittal")}
                  </HeaderHint>
                </th>
              </tr>
              <tr className="font-bold text-slate-500 text-[12px]">
                <th className="pt-1 pb-3 px-4 text-center">
                  <HeaderHint hint="Submitted to a hiring manager for review.">
                    Internal
                  </HeaderHint>
                </th>
                <th className="pt-1 pb-3 px-4 text-center">
                  <HeaderHint hint="Submitted to the client. Submits recorded before the internal/external split count here.">
                    External
                  </HeaderHint>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[13px]">
              {isLoading ? (
                [1, 2, 3, 4].map((i) => (
                  <tr key={i}>
                    <td className="py-4 px-4 w-[50px] min-w-[50px] max-w-[50px] sticky left-0 z-10 bg-white text-center">
                      <div className="h-4 w-4 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                    <td className="py-4 px-6 w-[130px] min-w-[130px] max-w-[130px] sticky left-[50px] z-10 bg-white">
                      <div className="h-4 w-20 bg-slate-100 animate-pulse rounded" />
                    </td>
                    <td className="py-4 px-6 w-[280px] min-w-[280px] max-w-[280px] sticky left-[180px] z-10 bg-white shadow-[1px_0_0_0_#e2e8f0]">
                      <div className="h-4 w-44 bg-slate-100 animate-pulse rounded" />
                    </td>
                    {TIMELINE_SKELETON_BARS.map((cell, idx) => (
                      <td
                        key={idx}
                        className={`py-4 px-6${cell.center ? " text-center" : ""}`}
                      >
                        <div
                          className={`${cell.bar} bg-slate-100 animate-pulse${cell.center ? " mx-auto" : ""}`}
                        />
                      </td>
                    ))}
                  </tr>
                ))
              ) : filteredTimeline.length === 0 ? (
                <tr>
                  <td
                    colSpan={TIMELINE_COLUMN_COUNT}
                    className="py-12 text-center text-slate-400 text-[13px]"
                  >
                    {timelineRows.length === 0
                      ? "No job timeline data available yet."
                      : "No jobs match your search or filter."}
                  </td>
                </tr>
              ) : (
                visibleTimeline.map((job, i) => {
                  const rowBg = job.is_archived ? "bg-slate-50" : "bg-white";
                  return (
                    <tr
                      key={job.job_id || job.jobdiva_id}
                      className={`hover:bg-[#f6f8fb] transition-colors group ${rowBg}`}
                    >
                      <td
                        className={`py-3.5 px-4 w-[50px] min-w-[50px] max-w-[50px] sticky left-0 z-10 ${rowBg} group-hover:bg-[#f6f8fb] transition-colors text-center text-slate-500 font-medium`}
                      >
                        {i + 1}
                      </td>
                      <td
                        className={`py-3.5 px-6 w-[130px] min-w-[130px] max-w-[130px] sticky left-[50px] z-10 ${rowBg} group-hover:bg-[#f6f8fb] transition-colors whitespace-nowrap`}
                      >
                        <div 
                          className="font-mono text-sm text-slate-600 overflow-hidden truncate"
                          title={job.jobdiva_id || "—"}
                        >
                          {job.jobdiva_id || "—"}
                        </div>
                      </td>
                      <td
                        className={`py-3.5 px-6 w-[280px] min-w-[280px] max-w-[280px] sticky left-[180px] z-10 ${rowBg} group-hover:bg-[#f6f8fb] transition-colors shadow-[1px_0_0_0_#e2e8f0]`}
                      >
                        <div className="font-semibold text-slate-800 break-words">
                          {job.title}
                        </div>
                        {job.is_archived && (
                          <div
                            className="mt-1.5 inline-flex items-center rounded bg-slate-100 px-1.5 py-0.5 text-[11px] font-medium text-slate-500 max-w-full"
                            title={job.archive_reason || "—"}
                          >
                            Reason:{" "}
                            <span className="truncate ml-1">
                              {job.archive_reason || "—"}
                            </span>
                          </div>
                        )}
                      </td>
                      <td className="py-3.5 px-6 text-slate-600">
                        <div className="break-words">
                          {job.customer_name}
                        </div>
                      </td>
                      <td className="py-3.5 px-6">
                        {job.recruiter_emails?.length ? (
                          <div className="flex flex-col gap-1 max-w-[250px] max-h-[64px] overflow-y-auto pr-1">
                            {job.recruiter_emails.map((email) => (
                              <div
                                key={email}
                                className="text-slate-600 text-[13px] break-words"
                                title={email}
                              >
                                {email}
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div className="text-slate-600">—</div>
                        )}
                      </td>
                      <td className="py-3.5 px-6">
                        {renderPersonCell(job.posted_by)}
                      </td>
                      <td className="py-3.5 px-6">
                        {renderPersonCell(job.launched_by)}
                      </td>
                      <td className="py-3.5 px-6 whitespace-nowrap">
                        {job.jobdiva_posted_on ? (
                          <span className="text-slate-700">
                            {formatEasternDate(job.jobdiva_posted_on)}
                          </span>
                        ) : job.posted_date_raw ? (
                          <span className="text-slate-500">
                            {job.posted_date_raw}
                          </span>
                        ) : (
                          <span className="text-slate-300">—</span>
                        )}
                      </td>
                      <td className="py-3.5 px-6 whitespace-nowrap">
                        {renderDateCell(job.added_to_curate_at)}
                      </td>
                      <td className="py-3.5 px-6 whitespace-nowrap">
                        {renderDateCell(job.curate_launched_at)}
                      </td>
                      <td className="py-3.5 px-6 text-center whitespace-nowrap">
                        {renderLagChip(job.posted_to_launch_days)}
                      </td>
                      <td className="py-3.5 px-6 text-center whitespace-nowrap">
                        {renderDurationCell(job.step5_active_minutes)}
                      </td>
                      <td className="py-3.5 px-6 text-center whitespace-nowrap">
                        {renderDurationCell(job.step5_to_launch_minutes)}
                      </td>
                      <td className="py-3.5 px-6 text-center">
                        {renderArchivedBadge(job)}
                      </td>
                      <td className="py-3.5 px-6 text-center">
                        {renderPairStatusBadge(job.pair_status)}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-slate-800">
                        {job.candidates_sourced.toLocaleString()}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-primary">
                        {job.candidates_launched.toLocaleString()}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-emerald-600">
                        {renderCountCell(job.pass_candidates, "")}
                      </td>
                      <td className="py-3.5 px-6 text-center">
                        {renderFeedbackCell(job)}
                      </td>
                      <td className="py-3.5 px-6 whitespace-nowrap">
                        {renderDateCell(job.first_feedback_at)}
                      </td>
                      <td className="py-3.5 px-4 text-center font-bold text-slate-800">
                        {renderCountCell(job.pair_internal_submits, "")}
                      </td>
                      <td className="py-3.5 px-4 text-center font-bold text-slate-800">
                        {renderCountCell(job.pair_external_submits, "")}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-emerald-700">
                        {(job.jobdiva_confirmed_subs ?? 0).toLocaleString()}
                      </td>
                      <td className="py-3.5 px-6 text-center font-bold text-amber-600">
                        {(job.jobdiva_submittals ?? 0).toLocaleString()}
                      </td>
                      <td className="py-3.5 px-6 whitespace-nowrap">
                        {renderDateCell(job.first_pair_external_submit_at)}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        {!isLoading && !showAllTimeline && filteredTimeline.length > 50 && (
          <div className="px-6 py-3 border-t border-slate-100 text-center">
            <button
              type="button"
              onClick={() => setShowAllTimeline(true)}
              className="text-[13px] font-semibold text-primary hover:underline"
            >
              Show all {filteredTimeline.length}
            </button>
          </div>
        )}
      </div>

      {/* LinkedIn Sourcing Accounts — global infrastructure, only meaningful
          on the unscoped all-teams admin view. */}
      {!teamScope && (
        <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
          <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-[16px] font-bold text-slate-900 flex items-center gap-2">
                <Linkedin className="w-4 h-4 text-sky-600" />
                LinkedIn Sourcing Accounts
              </h2>
              <p className="text-[12px] text-slate-500 mt-0.5">
                Searches rotate round-robin across all attached LinkedIn
                accounts.
              </p>
            </div>
            <Button
              variant="outline"
              onClick={refreshLinkedInAccounts}
              disabled={isLoading || isRefreshing || isRefreshingAccounts}
              className="flex items-center gap-2 h-9 px-3.5 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
            >
              <RefreshCw
                className={`h-4 w-4 text-slate-500 ${isRefreshingAccounts ? "animate-spin text-primary" : ""}`}
              />
              {isRefreshingAccounts
                ? "Checking Unipile..."
                : "Refresh live status"}
            </Button>
          </div>

          {accountsError && (
            <div className="px-6 py-2.5 border-b border-red-100 bg-red-50 text-[12px] font-medium text-red-700">
              {accountsError}
            </div>
          )}

          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 font-bold text-slate-500 text-[12.5px]">
                  <th className="py-3 px-6">Account</th>
                  <th className="py-3 px-6">Status</th>
                  <th className="py-3 px-6 text-center">Searches</th>
                  <th className="py-3 px-6 text-right">Last used</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-[13px]">
                {isLoading ? (
                  [1, 2].map((i) => (
                    <tr key={i}>
                      <td className="py-4 px-6">
                        <div className="h-4 w-36 bg-slate-100 animate-pulse rounded" />
                        <div className="h-3 w-24 bg-slate-100 animate-pulse rounded mt-1.5" />
                      </td>
                      <td className="py-4 px-6">
                        <div className="h-5 w-20 bg-slate-100 animate-pulse rounded-full" />
                      </td>
                      <td className="py-4 px-6 text-center">
                        <div className="h-4 w-8 bg-slate-100 animate-pulse rounded mx-auto" />
                      </td>
                      <td className="py-4 px-6 text-right">
                        <div className="h-4 w-14 bg-slate-100 animate-pulse rounded ml-auto" />
                      </td>
                    </tr>
                  ))
                ) : linkedInRows.length === 0 ? (
                  <tr>
                    <td
                      colSpan={4}
                      className="py-12 text-center text-slate-400 text-[13px] px-6"
                    >
                      No LinkedIn account activity yet — accounts appear after
                      the first rotated search, or click Refresh to list the
                      accounts attached to Unipile.
                    </td>
                  </tr>
                ) : (
                  linkedInRows.map((acc) => {
                    const coolingDown =
                      !!acc.cooldown_until &&
                      new Date(acc.cooldown_until).getTime() > Date.now();
                    const classicMode = acc.search_api === "classic";
                    let chipText = classicMode
                      ? "In rotation · classic search"
                      : "In rotation";
                    let chipClass = classicMode
                      ? "bg-sky-50 text-sky-700 border border-sky-200"
                      : "bg-emerald-50 text-emerald-700 border border-emerald-200";
                    let chipTitle: string | undefined = classicMode
                      ? "No LinkedIn Recruiter seat on this account — searches use LinkedIn classic people search (10 per page, up to 50 per search)."
                      : undefined;
                    if (acc.status === "DETACHED") {
                      chipText = "Detached";
                      chipClass =
                        "bg-slate-100 text-slate-600 border border-slate-200";
                      chipTitle = "No longer attached to the Unipile workspace.";
                    } else if (acc.needs_reconnect) {
                      // LinkedIn logged the seat out (opened elsewhere), the
                      // session expired, or a checkpoint is pending. Only a
                      // human can fix this: reconnect the account in Unipile.
                      chipText = acc.status && acc.status !== "OK"
                        ? `Needs reconnect · ${acc.status}`
                        : "Needs reconnect";
                      chipClass =
                        "bg-rose-50 text-rose-700 border border-rose-200";
                      chipTitle =
                        "LinkedIn session is gone for this account. Reconnect it in the Unipile dashboard; rotation skips it meanwhile.";
                    } else if (coolingDown) {
                      chipText = "Cooling down";
                      chipClass =
                        "bg-amber-50 text-amber-700 border border-amber-200";
                      chipTitle = `Benched after a transient error until ${formatEasternDateTime(acc.cooldown_until)} (ET)`;
                    } else if (acc.status && acc.status !== "OK") {
                      chipText = acc.status;
                      chipClass =
                        "bg-rose-50 text-rose-700 border border-rose-200";
                    }

                    return (
                      <tr
                        key={acc.account_id}
                        className="hover:bg-[#f6f8fb] transition-colors"
                      >
                        <td className="py-3.5 px-6">
                          <div className="font-semibold text-slate-800">
                            {acc.account_name || "Unnamed account"}
                          </div>
                          <div className="font-mono text-xs text-slate-400 mt-0.5">
                            {acc.account_id}
                          </div>
                        </td>
                        <td className="py-3.5 px-6">
                          <span
                            className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${chipClass}`}
                            title={chipTitle}
                          >
                            {chipText}
                          </span>
                          {acc.last_error ? (
                            <div
                              className="text-xs text-red-600 mt-1 max-w-[320px] truncate"
                              title={acc.last_error}
                            >
                              {acc.last_error.length > 60
                                ? `${acc.last_error.slice(0, 60)}…`
                                : acc.last_error}
                            </div>
                          ) : null}
                        </td>
                        <td className="py-3.5 px-6 text-center font-bold text-slate-800">
                          {acc.use_count.toLocaleString()}
                        </td>
                        <td className="py-3.5 px-6 text-right text-slate-600 whitespace-nowrap">
                          {formatRelativeTime(acc.last_used_at)}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
