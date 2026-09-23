"use client";

// Recruiter Analytics.
//
// One row per recruiter, credited BY JOB ASSIGNMENT: every email in a job's
// recruiter list gets that job's numbers. A job shared by three recruiters
// shows up in three rows, so the Totals row and the KPI tiles come from the
// backend's own rollup over the DISTINCT job set — never add recruiter rows
// up on this page (routers/recruiter_analytics.py).
//
// Step 5 Active Time is the one column credited differently: it is the time
// each recruiter spent on Step 5 (Source) themselves, on any job in view,
// because PAIR records who had the step open. Step 5 → Launch is a job
// property and follows assignment like the rest.
//
// The date range picks jobs by their first successful PAIR launch (the Launch
// Report's PAIR Launch), as Eastern calendar dates; "All time" is every job in
// scope, launched or not. Every timestamp is
// Eastern "MM/DD/YYYY HH:MM:SS" with the zone stated once in the header.

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowDown,
  ArrowLeft,
  ArrowUp,
  Briefcase,
  ChevronDown,
  ChevronRight,
  Clock,
  Download,
  Rocket,
  RefreshCw,
  Search,
  Send,
  ShieldAlert,
  ShieldCheck,
  TriangleAlert,
  UserCheck,
  UsersRound,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { UTF8_BOM, escapeCSV, toCsv } from "@/lib/csv";
import {
  EMPTY_DATE,
  addIsoDays,
  formatDuration,
  formatEasternDate,
  formatEasternDateTime,
  formatEasternTime,
  inclusiveDateSpanDays,
  todayEastern,
  withEasternLabel,
} from "@/lib/date";

// ---------------------------------------------------------------------------
// Response shape (GET /api/v1/admin/recruiter-analytics)
// ---------------------------------------------------------------------------

interface MetricsBlock {
  jobs: { assigned: number; active: number; archived: number; launched: number; unassigned?: number };
  candidates: { sourced: number; launched: number };
  outcomes: { passed: number; failed: number; in_progress: number };
  feedback: {
    total: number;
    submits: number;
    internal: number;
    external: number;
    rejects: number;
    unreachable: number;
    awaiting: number;
  };
  submittals: { pair_internal: number; pair_external: number; jobdiva_confirmed: number; jobdiva_total: number };
  first_external_submit_at: string | null;
  avg_time_to_first_pass_minutes: number | null;
  /** passed / (passed + failed), 0..1; null when nothing is decided yet. */
  pass_rate: number | null;
  /** Step 5 active time, in minutes. A recruiter row: the time THAT person
   *  spent themselves, on any job in view. Totals: everyone's time on the
   *  distinct jobs. null = nothing tracked (jobs worked before it existed). */
  step5_active_minutes_total: number | null;
  /** The total ÷ the jobs with time (step5_jobs_timed); null when none. */
  step5_active_minutes_avg: number | null;
  step5_jobs_timed: number;
  /** Mean of first Step 5 entry → first successful launch over these jobs
   *  (by assignment); jobs without both ends are left out. */
  step5_to_launch_minutes_avg: number | null;
}

interface RecruiterRow extends MetricsBlock {
  email: string;
  team_name: string | null;
  is_team_lead: boolean;
  /** null = unknown (the team/login lookup failed), not "no login". */
  has_pair_account: boolean | null;
  job_ids: string[];
}

interface JobRow {
  job_id: string;
  jobdiva_id: string | null;
  title: string;
  customer_name: string;
  recruiter_emails: string[];
  is_archived: boolean;
  version: number;
  created_at: string | null;
  launched_at: string | null;
  posted_by: string | null;
  launched_by: string | null;
  candidates_sourced: number;
  candidates_launched: number;
  passed: number;
  failed: number;
  in_progress: number;
  pass_rate: number | null;
  feedback_total: number;
  pair_submits: number;
  pair_internal_submits: number;
  pair_external_submits: number;
  rejects: number;
  unreachable: number;
  awaiting_feedback: number;
  first_feedback_at: string | null;
  first_external_submit_at: string | null;
  jobdiva_confirmed_subs: number;
  jobdiva_total_subs: number;
  time_to_first_pass_minutes: number | null;
  /** Everyone's Step 5 active time on this job; null = not tracked. */
  step5_active_minutes: number | null;
  /** Each assigned recruiter's own share (only those with time). */
  step5_active_minutes_by_recruiter: Record<string, number>;
  step5_first_entered_at: string | null;
  /** The launch end of Step 5 → Launch (same rule as launched_at). */
  step5_first_launch_at: string | null;
  step5_to_launch_minutes: number | null;
}

interface RecruiterAnalyticsData {
  range: {
    start_date: string | null;
    end_date: string | null;
    timezone: string;
    mode: "launched_in_range" | "all_time";
    description: string;
  };
  generated_at: string | null;
  team_scope: { team_id: string; team_name: string; member_count: number } | null;
  recruiter: string | null;
  totals: MetricsBlock & { recruiters: number };
  unassigned_jobs: number;
  recruiters: RecruiterRow[];
  jobs: JobRow[];
  metrics_available: boolean;
  /** false = the Step 5 time read failed on its own this load. */
  step_time_available?: boolean;
  warnings: string[];
  cached: boolean;
}

interface TeamSummary {
  id: string;
  name: string;
  lead_emails: string[];
  member_emails: string[];
}

// ---------------------------------------------------------------------------
// Date range
// ---------------------------------------------------------------------------

type Preset = "7d" | "30d" | "90d" | "all" | "custom";
type DateRange = { start: string; end: string } | null; // null = all time

const PRESETS: { key: Exclude<Preset, "custom">; label: string; days: number | null }[] = [
  { key: "7d", label: "Last 7 days", days: 7 },
  { key: "30d", label: "Last 30 days", days: 30 },
  { key: "90d", label: "Last 90 days", days: 90 },
  { key: "all", label: "All time", days: null },
];

// Mirrors MAX_RANGE_DAYS in routers/recruiter_analytics.py.
const MAX_RANGE_DAYS = 366;

/** "Last N days" = today and the N-1 Eastern days before it. */
function presetRange(preset: Exclude<Preset, "custom">, today: string = todayEastern()): DateRange {
  const days = PRESETS.find((p) => p.key === preset)?.days ?? null;
  return days === null ? null : { start: addIsoDays(today, -(days - 1)), end: today };
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const num = (value: number | null | undefined) => (value ? value.toLocaleString() : "0");

function formatRate(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? "—" : `${Math.round(rate * 1000) / 10}%`;
}

function rangeLabel(range: RecruiterAnalyticsData["range"] | undefined, fallback: DateRange): string {
  const start = range ? range.start_date : fallback?.start ?? null;
  const end = range ? range.end_date : fallback?.end ?? null;
  if (!start || !end) return "All time";
  return start === end ? formatEasternDate(start) : `${formatEasternDate(start)} – ${formatEasternDate(end)}`;
}

/** The FastAPI `detail` out of an ApiError message ("403 /path: {json}"). */
function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.message.slice(err.message.indexOf(": ") + 2);
    try {
      const detail = JSON.parse(body)?.detail;
      if (typeof detail === "string" && detail) return detail;
    } catch {
      // not JSON — fall through to the generic messages
    }
    if (err.status === 403) return "Access denied. Admin or team lead access is required.";
    if (err.status === 404) return "That team no longer exists.";
  }
  return err instanceof Error && err.message ? err.message : "Failed to load recruiter analytics.";
}

// ---------------------------------------------------------------------------
// Columns — one list drives the header, the cells, the Totals row, sorting and
// the CSV, so the export cannot drift from what is on screen.
// ---------------------------------------------------------------------------

type SortDir = "asc" | "desc";
type SortState = { key: string; dir: SortDir };

interface MetricColumn {
  key: string;
  label: string;
  /** Shown under the label in the header. */
  sub?: string;
  /** Header tooltip: what the number means. */
  hint: string;
  sortValue: (b: MetricsBlock) => number | null;
  cell: (b: MetricsBlock) => React.ReactNode;
  /** CSV header → value pairs; a combined screen cell exports as separate columns. */
  csv: (b: MetricsBlock) => [string, string][];
  /** Comes from the candidate-metrics read, which can fail on its own
   *  (metrics_available=false); the backend then sends zeros, shown as "—". */
  candidateDerived?: true;
  /** Comes from the Step 5 time read, which can fail on its own too
   *  (step_time_available=false). */
  stepTimeDerived?: true;
}

const FIRST_EXTERNAL_LABEL = withEasternLabel("First PAIR External Submittal");

function OutcomeBar({ passed, failed, inProgress }: { passed: number; failed: number; inProgress: number }) {
  const total = passed + failed + inProgress;
  const label = `Pass ${passed} · Fail ${failed} · In progress ${inProgress}`;
  if (!total) return <div className="h-1.5 w-20 rounded-full bg-slate-100" title={label} />;
  const pct = (n: number) => `${(n / total) * 100}%`;
  return (
    <div className="flex h-1.5 w-20 overflow-hidden rounded-full bg-slate-100" title={label} role="img" aria-label={label}>
      <div className="bg-emerald-500" style={{ width: pct(passed) }} />
      <div className="bg-rose-400" style={{ width: pct(failed) }} />
      <div className="bg-amber-300" style={{ width: pct(inProgress) }} />
    </div>
  );
}

const METRIC_COLUMNS: MetricColumn[] = [
  {
    key: "jobs",
    label: "Jobs",
    sub: "Assigned / Launched",
    hint: "Jobs assigned to the recruiter in this view, and how many of them have launched in PAIR. Sorts by launched.",
    sortValue: (b) => b.jobs.launched,
    cell: (b) => (
      <span className="tabular-nums">
        <span className="font-semibold text-slate-800">{num(b.jobs.assigned)}</span>
        <span className="text-slate-400"> / </span>
        <span className="font-semibold text-slate-800">{num(b.jobs.launched)}</span>
      </span>
    ),
    csv: (b) => [
      ["Jobs Assigned", String(b.jobs.assigned)],
      ["Jobs Launched", String(b.jobs.launched)],
      ["Active Jobs", String(b.jobs.active)],
      ["Archived Jobs", String(b.jobs.archived)],
    ],
  },
  {
    key: "sourced",
    label: "Sourced",
    hint: "Candidates sourced across these jobs (job counters, whole life of each job).",
    sortValue: (b) => b.candidates.sourced,
    cell: (b) => num(b.candidates.sourced),
    csv: (b) => [["Candidates Sourced", String(b.candidates.sourced)]],
  },
  {
    key: "launched",
    label: "Launched",
    hint: "Candidates launched to PAIR across these jobs.",
    sortValue: (b) => b.candidates.launched,
    cell: (b) => num(b.candidates.launched),
    csv: (b) => [["Candidates Launched", String(b.candidates.launched)]],
  },
  {
    key: "passed",
    candidateDerived: true,
    label: "Pass",
    hint: "Candidates the rank list shows as Pass.",
    sortValue: (b) => b.outcomes.passed,
    cell: (b) => <span className="font-semibold text-emerald-700">{num(b.outcomes.passed)}</span>,
    csv: (b) => [["Pass", String(b.outcomes.passed)]],
  },
  {
    key: "failed",
    candidateDerived: true,
    label: "Fail",
    hint: "Candidates the rank list shows as Fail.",
    sortValue: (b) => b.outcomes.failed,
    cell: (b) => num(b.outcomes.failed),
    csv: (b) => [["Fail", String(b.outcomes.failed)]],
  },
  {
    key: "in_progress",
    candidateDerived: true,
    label: "In Progress",
    hint: "Candidates whose PAIR interview is under way.",
    sortValue: (b) => b.outcomes.in_progress,
    cell: (b) => num(b.outcomes.in_progress),
    csv: (b) => [["In Progress", String(b.outcomes.in_progress)]],
  },
  {
    key: "pass_rate",
    candidateDerived: true,
    label: "Pass Rate",
    hint: "Pass ÷ (Pass + Fail). The bar shows Pass / Fail / In Progress.",
    sortValue: (b) => b.pass_rate,
    cell: (b) => (
      <div className="flex flex-col items-end gap-1">
        <span className="tabular-nums">{formatRate(b.pass_rate)}</span>
        <OutcomeBar passed={b.outcomes.passed} failed={b.outcomes.failed} inProgress={b.outcomes.in_progress} />
      </div>
    ),
    csv: (b) => [["Pass Rate", formatRate(b.pass_rate)]],
  },
  {
    key: "feedback",
    candidateDerived: true,
    label: "Feedback",
    sub: "Submit · Reject · Unreachable",
    hint: "Candidates with a recorded recruiter decision, split by kind.",
    sortValue: (b) => b.feedback.total,
    cell: (b) => (
      <div className="flex flex-col items-end">
        <span className="font-semibold text-slate-800 tabular-nums">{num(b.feedback.total)}</span>
        <span className="text-[11px] text-slate-400 tabular-nums whitespace-nowrap">
          S {num(b.feedback.submits)} · R {num(b.feedback.rejects)} · U {num(b.feedback.unreachable)}
        </span>
      </div>
    ),
    csv: (b) => [
      ["Feedback", String(b.feedback.total)],
      ["Feedback: Submit", String(b.feedback.submits)],
      ["Feedback: Reject", String(b.feedback.rejects)],
      ["Feedback: Unreachable", String(b.feedback.unreachable)],
    ],
  },
  {
    key: "pair_internal",
    candidateDerived: true,
    label: "PAIR Internal",
    hint: "Submits recorded in PAIR as internal (sent to a hiring manager for review).",
    sortValue: (b) => b.submittals.pair_internal,
    cell: (b) => num(b.submittals.pair_internal),
    csv: (b) => [["PAIR Internal Submittals", String(b.submittals.pair_internal)]],
  },
  {
    key: "pair_external",
    candidateDerived: true,
    label: "PAIR External",
    hint: "Submits recorded in PAIR as external (sent to the client). Submits from before the split count as external.",
    sortValue: (b) => b.submittals.pair_external,
    cell: (b) => <span className="font-semibold text-primary">{num(b.submittals.pair_external)}</span>,
    csv: (b) => [["PAIR External Submittals", String(b.submittals.pair_external)]],
  },
  {
    key: "jobdiva_confirmed",
    label: "JobDiva-Confirmed",
    hint: "External submittals JobDiva confirms for these jobs. v1 and v2 of one JobDiva job count once.",
    sortValue: (b) => b.submittals.jobdiva_confirmed,
    cell: (b) => num(b.submittals.jobdiva_confirmed),
    csv: (b) => [
      ["JobDiva-Confirmed Submittals", String(b.submittals.jobdiva_confirmed)],
      ["JobDiva Total Submittals", String(b.submittals.jobdiva_total)],
    ],
  },
  {
    key: "awaiting",
    candidateDerived: true,
    label: "Awaiting Feedback",
    hint: "Interview decided (Pass or Fail) but no recruiter decision recorded yet.",
    sortValue: (b) => b.feedback.awaiting,
    cell: (b) =>
      b.feedback.awaiting ? (
        <span className="font-semibold text-amber-700">{num(b.feedback.awaiting)}</span>
      ) : (
        "0"
      ),
    csv: (b) => [["Awaiting Feedback", String(b.feedback.awaiting)]],
  },
  {
    key: "first_external",
    candidateDerived: true,
    label: FIRST_EXTERNAL_LABEL,
    hint: "Earliest current external PAIR submit on these jobs.",
    sortValue: (b) => (b.first_external_submit_at ? Date.parse(b.first_external_submit_at) : null),
    cell: (b) => <span className="whitespace-nowrap tabular-nums">{formatEasternDateTime(b.first_external_submit_at)}</span>,
    csv: (b) => [[FIRST_EXTERNAL_LABEL, formatEasternDateTime(b.first_external_submit_at)]],
  },
  {
    key: "ttfp",
    label: "Avg Time to First Pass",
    hint: "Mean over these jobs of the time from launch to the job's first Pass.",
    sortValue: (b) => b.avg_time_to_first_pass_minutes,
    cell: (b) => formatDuration(b.avg_time_to_first_pass_minutes),
    csv: (b) => [["Avg Time to First Pass", formatDuration(b.avg_time_to_first_pass_minutes)]],
  },
  {
    key: "step5_active",
    stepTimeDerived: true,
    label: "Step 5 Active Time",
    sub: "Avg per job",
    hint:
      "Time this recruiter spent on Step 5 (Source) themselves: the step open and in use, paused when the tab is " +
      "hidden or after 5 minutes idle. Unlike the other columns it is not credited by assignment, so it includes " +
      "their time on colleagues' jobs in this view. Shows the average per job they timed, with their total and the " +
      "job count below. Totals: everyone's time on the distinct jobs. — = not tracked (jobs worked before this existed).",
    sortValue: (b) => b.step5_active_minutes_avg,
    cell: (b) => (
      <div className="flex flex-col items-end">
        <span className="font-semibold text-slate-800 tabular-nums">{formatDuration(b.step5_active_minutes_avg)}</span>
        {b.step5_jobs_timed > 0 && (
          <span className="text-[11px] text-slate-400 tabular-nums whitespace-nowrap">
            {formatDuration(b.step5_active_minutes_total)} total · {num(b.step5_jobs_timed)}{" "}
            {b.step5_jobs_timed === 1 ? "job" : "jobs"}
          </span>
        )}
      </div>
    ),
    csv: (b) => [
      ["Step 5 Active Time (Avg per Job)", formatDuration(b.step5_active_minutes_avg)],
      ["Step 5 Active Time (Total)", formatDuration(b.step5_active_minutes_total)],
      ["Step 5 Jobs Timed", String(b.step5_jobs_timed ?? 0)],
    ],
  },
  {
    key: "step5_to_launch",
    stepTimeDerived: true,
    label: "Step 5 → Launch",
    sub: "Avg per job",
    hint:
      "Wall-clock time from a job's first Step 5 visit (by anyone) to its first successful PAIR launch, averaged " +
      "over the recruiter's assigned jobs that have both — a job number, credited by assignment like the other " +
      "columns. — = not tracked, not launched, or launched before Step 5 was first timed.",
    sortValue: (b) => b.step5_to_launch_minutes_avg,
    cell: (b) => formatDuration(b.step5_to_launch_minutes_avg),
    csv: (b) => [["Step 5 → Launch (Avg per Job)", formatDuration(b.step5_to_launch_minutes_avg)]],
  },
];

function sortRecruiters(rows: RecruiterRow[], sort: SortState): RecruiterRow[] {
  const dir = sort.dir === "asc" ? 1 : -1;
  const column = METRIC_COLUMNS.find((c) => c.key === sort.key);
  return [...rows].sort((a, b) => {
    if (!column) return a.email.localeCompare(b.email) * dir;
    const av = column.sortValue(a);
    const bv = column.sortValue(b);
    // Blanks sink to the bottom whichever way the column is sorted.
    if (av === null || bv === null) {
      if (av !== bv) return av === null ? 1 : -1;
    } else if (av !== bv) {
      return (av - bv) * dir;
    }
    return a.email.localeCompare(b.email);
  });
}

function ariaSort(sort: SortState, key: string): "ascending" | "descending" | undefined {
  if (sort.key !== key) return undefined;
  return sort.dir === "asc" ? "ascending" : "descending";
}

/** Which of the backend's independently-failing reads failed on this load. */
interface Unavailable {
  metrics: boolean;
  stepTime: boolean;
}

function isUnavailable(col: MetricColumn, unavailable: Unavailable): boolean {
  return (!!col.candidateDerived && unavailable.metrics) || (!!col.stepTimeDerived && unavailable.stepTime);
}

/** A fallback value from a failed read is not a real value: "—" on screen and in the file. */
function metricCell(col: MetricColumn, b: MetricsBlock, unavailable: Unavailable): React.ReactNode {
  return isUnavailable(col, unavailable) ? <span className="text-slate-400">{EMPTY_DATE}</span> : col.cell(b);
}

const METRICS_UNAVAILABLE_CSV_NOTE =
  "Note: candidate outcome columns (pass / fail / in progress / feedback / PAIR submittals / awaiting feedback / first PAIR external submittal) were unavailable for this export and show —";
// Without it a "—" in these columns would read as "never tracked".
const STEP_TIME_UNAVAILABLE_CSV_NOTE =
  "Note: Step 5 time columns (Step 5 Active Time / Step 5 → Launch) were unavailable for this export and show —";

function buildCsv(rows: RecruiterRow[], totals: RecruiterAnalyticsData["totals"], unavailable: Unavailable): string {
  const headerPairs = METRIC_COLUMNS.flatMap((c) => c.csv(totals));
  const metricCells = (b: MetricsBlock) =>
    METRIC_COLUMNS.flatMap((c) => c.csv(b).map(([, value]) => (isUnavailable(c, unavailable) ? EMPTY_DATE : value)));
  const csv = toCsv(
    ["Recruiter", "Team", "PAIR Login", ...headerPairs.map(([label]) => label)],
    [
      ...rows.map((r) => [
        r.email,
        r.team_name || "",
        r.has_pair_account === null ? "Unknown" : r.has_pair_account ? "Yes" : "No",
        ...metricCells(r),
      ]),
      // Distinct jobs in scope — deliberately not the sum of the rows above.
      ["All recruiters (distinct jobs)", "", "", ...metricCells(totals)],
    ],
  );
  // The file travels without the page's warning banner, so it says so itself.
  const notes = [
    ...(unavailable.metrics ? [METRICS_UNAVAILABLE_CSV_NOTE] : []),
    ...(unavailable.stepTime ? [STEP_TIME_UNAVAILABLE_CSV_NOTE] : []),
  ];
  return notes.length ? `${csv}\n\n${notes.map(escapeCSV).join("\n")}` : csv;
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

const TONES = {
  indigo: "bg-indigo-50 border-indigo-100 text-indigo-600",
  emerald: "bg-emerald-50 border-emerald-100 text-emerald-600",
  violet: "bg-violet-50 border-violet-100 text-violet-600",
  sky: "bg-sky-50 border-sky-100 text-sky-600",
  teal: "bg-teal-50 border-teal-100 text-teal-600",
  amber: "bg-amber-50 border-amber-100 text-amber-600",
} as const;

function KpiTile({
  label,
  value,
  hint,
  icon: Icon,
  tone,
  loading,
}: {
  label: string;
  value: string;
  hint?: string;
  icon: React.ComponentType<{ className?: string }>;
  tone: keyof typeof TONES;
  loading: boolean;
}) {
  return (
    <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[13px] font-semibold text-slate-500">{label}</span>
        <div className={`w-8 h-8 shrink-0 rounded-lg border flex items-center justify-center ${TONES[tone]}`}>
          <Icon className="w-4 h-4" />
        </div>
      </div>
      <div className="mt-3">
        {loading ? (
          <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
        ) : (
          <div className="text-[28px] font-bold text-slate-900 leading-none tabular-nums">{value}</div>
        )}
        <div className="text-[12px] text-slate-400 mt-1.5 font-medium min-h-[18px]">{loading ? "" : hint}</div>
      </div>
    </div>
  );
}

function Step5ActiveCell({ job, recruiterEmail }: { job: JobRow; recruiterEmail: string }) {
  // The job's own number is everyone's time; the row above it is this
  // recruiter's own time, so their share is shown when others worked it too.
  const mine = job.step5_active_minutes_by_recruiter?.[recruiterEmail];
  const total = job.step5_active_minutes;
  const showMine = mine !== undefined && total !== null && mine < total;
  return (
    <div
      className="flex flex-col items-end"
      title={
        total === null
          ? "Not tracked"
          : `Everyone's Step 5 active time on this job${mine !== undefined ? `; ${recruiterEmail}: ${formatDuration(mine)}` : ""}`
      }
    >
      <span>{formatDuration(total)}</span>
      {showMine && <span className="text-[11px] text-slate-400">theirs {formatDuration(mine)}</span>}
    </div>
  );
}

function RecruiterJobs({
  jobs,
  recruiterEmail,
  unavailable,
}: {
  jobs: JobRow[];
  recruiterEmail: string;
  unavailable: Unavailable;
}) {
  const metricsUnavailable = unavailable.metrics;
  const dash = <span className="text-slate-400">{EMPTY_DATE}</span>;
  if (!jobs.length) {
    return <p className="px-6 py-4 text-[13px] text-slate-400">No jobs in this view.</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left border-collapse text-[12.5px]">
        <thead>
          <tr className="text-slate-400 font-semibold border-b border-slate-200">
            <th className="py-2 px-4">Job</th>
            <th className="py-2 px-4">JobDiva ID</th>
            <th className="py-2 px-4">Customer</th>
            <th className="py-2 px-4 whitespace-nowrap">{withEasternLabel("Launched")}</th>
            <th className="py-2 px-4">Posted By</th>
            <th className="py-2 px-4">Launched By</th>
            <th className="py-2 px-4 text-right">Pass</th>
            <th className="py-2 px-4 text-right whitespace-nowrap" title="PAIR Internal · PAIR External · JobDiva-Confirmed">
              Submittals <span className="font-medium text-slate-300">Int · Ext · JobDiva</span>
            </th>
            <th
              className="py-2 px-4 text-right whitespace-nowrap"
              title="Everyone's Step 5 (Source) active time on the job; this recruiter's share below when others worked it too."
            >
              Step 5 Active Time
            </th>
            <th
              className="py-2 px-4 text-right whitespace-nowrap"
              title="From the job's first Step 5 visit (by anyone) to its first successful PAIR launch."
            >
              Step 5 → Launch
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {jobs.map((job) => (
            <tr key={job.job_id} className="text-slate-600">
              <td className="py-2 px-4 max-w-[320px]">
                <Link
                  href={`/jobs/${encodeURIComponent(job.job_id)}/rankings`}
                  onClick={(e) => e.stopPropagation()}
                  className="font-semibold text-slate-800 hover:text-primary hover:underline"
                >
                  {job.title || "Untitled job"}
                </Link>
                {job.version > 1 && <span className="ml-1.5 text-[11px] font-semibold text-slate-400">v{job.version}</span>}
                {job.is_archived && (
                  <span className="ml-1.5 inline-flex rounded-full bg-slate-100 px-1.5 py-0.5 text-[10.5px] font-semibold text-slate-500">
                    Archived
                  </span>
                )}
              </td>
              <td className="py-2 px-4 font-mono text-[12px] whitespace-nowrap">{job.jobdiva_id || EMPTY_DATE}</td>
              <td className="py-2 px-4">{job.customer_name || EMPTY_DATE}</td>
              <td className="py-2 px-4 whitespace-nowrap tabular-nums">{formatEasternDateTime(job.launched_at)}</td>
              <td className="py-2 px-4">{job.posted_by || EMPTY_DATE}</td>
              <td className="py-2 px-4">{job.launched_by || EMPTY_DATE}</td>
              <td className="py-2 px-4 text-right tabular-nums whitespace-nowrap">
                {metricsUnavailable ? (
                  dash
                ) : (
                  <>
                    <span className="font-semibold text-emerald-700">{num(job.passed)}</span>
                    <span className="text-slate-400"> of {num(job.passed + job.failed)} decided</span>
                  </>
                )}
              </td>
              <td className="py-2 px-4 text-right tabular-nums whitespace-nowrap">
                {metricsUnavailable ? EMPTY_DATE : num(job.pair_internal_submits)} ·{" "}
                {metricsUnavailable ? EMPTY_DATE : num(job.pair_external_submits)} · {num(job.jobdiva_confirmed_subs)}
              </td>
              <td className="py-2 px-4 text-right tabular-nums whitespace-nowrap">
                {unavailable.stepTime ? dash : <Step5ActiveCell job={job} recruiterEmail={recruiterEmail} />}
              </td>
              <td
                className="py-2 px-4 text-right tabular-nums whitespace-nowrap"
                title={
                  !unavailable.stepTime && job.step5_first_entered_at
                    ? `First Step 5 visit ${formatEasternDateTime(job.step5_first_entered_at)} → first launch ${formatEasternDateTime(job.step5_first_launch_at)} (ET)`
                    : undefined
                }
              >
                {unavailable.stepTime ? dash : formatDuration(job.step5_to_launch_minutes)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function RecruiterAnalyticsPage() {
  const { isAdmin, isTeamLead, teamName, isLoading: isRoleLoading, email, role } = useUserRole();
  const canView = isAdmin || isTeamLead;

  const [data, setData] = useState<RecruiterAnalyticsData | null>(null);
  // true until the first answer, so the table shows skeletons rather than
  // flashing its empty state for the render before the first fetch starts.
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [preset, setPreset] = useState<Preset>("30d");
  const [range, setRange] = useState<DateRange>(() => presetRange("30d"));
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [rangeError, setRangeError] = useState<string | null>(null);

  const [teams, setTeams] = useState<TeamSummary[]>([]);
  const [activeTeamId, setActiveTeamId] = useState<string | null>(() => {
    // Deep link: /admin/recruiter-analytics?team=<id>, read once at mount like
    // the Admin Analytics page (no useSearchParams Suspense boundary needed).
    if (typeof window === "undefined") return null;
    try {
      return new URLSearchParams(window.location.search).get("team");
    } catch {
      return null;
    }
  });

  const [sort, setSort] = useState<SortState>({ key: "jobs", dir: "desc" });
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [hideNoLogin, setHideNoLogin] = useState(false);
  const [search, setSearch] = useState("");

  // Only the newest request may write state: switching team or range while a
  // slow request is in flight must not let the older answer land last.
  const requestSeq = useRef(0);

  const load = useCallback(
    async (refresh = false) => {
      const seq = ++requestSeq.current;
      if (refresh) setIsRefreshing(true);
      setIsLoading(true);
      try {
        const res = await api.recruiterAnalytics.get({
          startDate: range?.start ?? null,
          endDate: range?.end ?? null,
          // Team leads never send a team — the backend pins them to their own.
          teamId: isAdmin ? activeTeamId : null,
          refresh,
        });
        if (seq !== requestSeq.current) return;
        if (res?.status === "success" && res.data) {
          setData(res.data as RecruiterAnalyticsData);
          setError(null);
        } else {
          setError("Failed to load recruiter analytics.");
        }
      } catch (err) {
        if (seq !== requestSeq.current) return;
        console.error("Error loading recruiter analytics:", err);
        setError(describeError(err));
      } finally {
        if (seq === requestSeq.current) {
          setIsLoading(false);
          setIsRefreshing(false);
        }
      }
    },
    [range, isAdmin, activeTeamId],
  );

  useEffect(() => {
    if (!isRoleLoading && canView) load();
  }, [isRoleLoading, canView, load]);

  useEffect(() => {
    if (isRoleLoading || !isAdmin) return;
    let cancelled = false;
    api.teams
      .list()
      .then((res) => {
        if (!cancelled && res?.status === "success" && res.data?.teams) setTeams(res.data.teams as TeamSummary[]);
      })
      .catch((err) => console.error("Error loading teams:", err));
    return () => {
      cancelled = true;
    };
  }, [isRoleLoading, isAdmin]);

  // A new scope or range is a different population: collapse the drill-downs.
  useEffect(() => {
    setExpanded(new Set());
  }, [range, activeTeamId]);

  const choosePreset = (key: Preset) => {
    setRangeError(null);
    setPreset(key);
    if (key === "custom") {
      // Seed the inputs with whatever is showing now.
      const seed = range ?? presetRange("30d");
      setCustomStart(seed?.start ?? "");
      setCustomEnd(seed?.end ?? "");
      return;
    }
    // "Today" is re-read on every click so a tab left open past midnight moves on.
    setRange(presetRange(key));
  };

  const applyCustomRange = () => {
    const today = todayEastern();
    if (!customStart || !customEnd) return setRangeError("Pick both a start and an end date.");
    if (customStart > customEnd) return setRangeError("The start date must not be after the end date.");
    if (customEnd > today) return setRangeError("The end date cannot be in the future.");
    if (inclusiveDateSpanDays(customStart, customEnd) > MAX_RANGE_DAYS)
      return setRangeError(`A range can cover at most ${MAX_RANGE_DAYS} days.`);
    setRangeError(null);
    setRange({ start: customStart, end: customEnd });
  };

  const jobsById = useMemo(() => new Map((data?.jobs ?? []).map((j) => [j.job_id, j])), [data]);
  const allRecruiters = useMemo(() => data?.recruiters ?? [], [data]);
  const noLoginCount = useMemo(() => allRecruiters.filter((r) => r.has_pair_account === false).length, [allRecruiters]);
  const visibleRecruiters = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const filtered = allRecruiters.filter(
      (r) =>
        (!hideNoLogin || r.has_pair_account !== false) &&
        (!needle || r.email.includes(needle) || (r.team_name || "").toLowerCase().includes(needle)),
    );
    return sortRecruiters(filtered, sort);
  }, [allRecruiters, hideNoLogin, search, sort]);

  const toggleSort = (key: string) =>
    setSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: key === "email" ? "asc" : "desc" },
    );

  const toggleExpanded = (recruiterEmail: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(recruiterEmail)) next.delete(recruiterEmail);
      else next.add(recruiterEmail);
      return next;
    });

  const activeTeam = teams.find((t) => t.id === activeTeamId) || null;
  const scopeName = data?.team_scope?.team_name || (!isAdmin ? teamName : null) || activeTeam?.name || null;

  const downloadCsv = () => {
    if (!data) return;
    const unavailableNow: Unavailable = {
      metrics: data.metrics_available === false,
      stepTime: data.step_time_available === false,
    };
    const blob = new Blob([UTF8_BOM, buildCsv(visibleRecruiters, data.totals, unavailableNow)], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const parts = ["PAIR Recruiter Analytics", rangeLabel(data.range, range).replace(/\//g, "-")];
    if (scopeName) parts.push(scopeName);
    link.download = `${parts.join(" - ")}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  if (isRoleLoading) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-primary border-t-transparent" />
          <p className="text-[13px] font-medium text-slate-500">Verifying access...</p>
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
          <h1 className="text-[20px] font-bold text-slate-900 mb-2">Access Restricted</h1>
          <p className="text-slate-500 text-[13px] mb-6 leading-relaxed">
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the{" "}
            <span className="uppercase font-semibold text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">
              {role.replace("_", " ")}
            </span>{" "}
            role. Recruiter Analytics is visible to Administrators and Team Leads only.
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

  const totals = data?.totals;
  const firstLoad = isLoading && !data;
  // Nothing has ever loaded (the first request failed): every number is
  // unknown, so the page must not read as "0 jobs, no recruiters".
  const loadFailed = !data && !isLoading;
  const metricsUnavailable = data?.metrics_available === false;
  const unavailable: Unavailable = { metrics: metricsUnavailable, stepTime: data?.step_time_available === false };
  const tile = (candidateDerived: boolean, value: string, hint: string) =>
    loadFailed
      ? { value: EMPTY_DATE, hint: "" }
      : candidateDerived && metricsUnavailable
        ? { value: EMPTY_DATE, hint: "Temporarily unavailable" }
        : { value, hint };
  const columnCount = METRIC_COLUMNS.length + 1;

  return (
    <div className="space-y-6 max-w-[1440px] mx-auto pb-10">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4 mt-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Recruiter Analytics</h1>
            {scopeName ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-2.5 py-0.5 text-[12px] font-semibold text-indigo-700 ring-1 ring-inset ring-indigo-200">
                <UsersRound className="w-3.5 h-3.5" />
                {scopeName}
                {typeof data?.team_scope?.member_count === "number" && (
                  <span className="font-medium text-indigo-500">· {data.team_scope.member_count} people</span>
                )}
              </span>
            ) : (
              isAdmin && (
                <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
                  All Teams
                </span>
              )
            )}
            {!isAdmin && (
              <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200 uppercase tracking-wide">
                Team Lead
              </span>
            )}
          </div>
          <p className="mt-1.5 text-[13px] text-slate-500">
            Credited by job assignment — a job shared by several recruiters counts for each of them; totals count it
            once. Step 5 Active Time is the exception: it is the time each recruiter spent themselves.
          </p>
        </div>

        <div className="flex items-center gap-3">
          {data?.generated_at && (
            <span
              className="text-[12px] font-medium text-slate-400 tabular-nums"
              title={data.cached ? "Served from the short-lived server cache. Refresh recomputes." : undefined}
            >
              Last updated {formatEasternTime(data.generated_at)} (ET)
            </span>
          )}
          <Button
            variant="outline"
            onClick={downloadCsv}
            disabled={!data || visibleRecruiters.length === 0}
            title={!data || visibleRecruiters.length === 0 ? "Nothing to export yet" : "Download the table as CSV"}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <Download className="h-4 w-4 text-slate-500" />
            Export CSV
          </Button>
          <Button
            variant="outline"
            onClick={() => load(true)}
            disabled={isLoading}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${isLoading ? "animate-spin text-primary" : ""}`} />
            {isRefreshing ? "Refreshing..." : "Refresh"}
          </Button>
        </div>
      </div>

      {/* Team tabs (admins only; team leads are pinned server-side) */}
      {isAdmin && teams.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex flex-wrap items-center rounded-lg bg-slate-100 p-0.5">
            {[{ id: null as string | null, name: "All Teams" }, ...teams].map((team) => (
              <button
                key={team.id ?? "__all"}
                type="button"
                onClick={() => setActiveTeamId(team.id)}
                className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-colors ${
                  activeTeamId === team.id ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {team.name}
              </button>
            ))}
          </div>
          <Link href="/admin/teams" className="ml-auto text-[12.5px] font-semibold text-primary hover:underline">
            Manage teams →
          </Link>
        </div>
      )}

      {/* Date range */}
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="inline-flex flex-wrap items-center rounded-lg bg-slate-100 p-0.5">
            {[...PRESETS, { key: "custom" as const, label: "Custom" }].map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => choosePreset(p.key)}
                className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-colors ${
                  preset === p.key ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
          {preset === "custom" && (
            <div className="flex flex-wrap items-center gap-2">
              <input
                type="date"
                aria-label="Start date"
                // Force MM/DD/YYYY in Chromium/Firefox (Safari follows the OS locale).
                lang="en-US"
                value={customStart}
                max={customEnd || todayEastern()}
                onChange={(e) => {
                  setRangeError(null);
                  setCustomStart(e.target.value);
                }}
                className="h-9 px-3 rounded-lg border border-slate-200 bg-white text-[13px] font-semibold text-slate-700 shadow-sm outline-none"
              />
              <span className="text-[12px] font-semibold text-slate-400">to</span>
              <input
                type="date"
                aria-label="End date"
                lang="en-US"
                value={customEnd}
                min={customStart || undefined}
                max={todayEastern()}
                onChange={(e) => {
                  setRangeError(null);
                  setCustomEnd(e.target.value);
                }}
                className="h-9 px-3 rounded-lg border border-slate-200 bg-white text-[13px] font-semibold text-slate-700 shadow-sm outline-none"
              />
              <Button
                variant="outline"
                onClick={applyCustomRange}
                disabled={isLoading}
                className="h-9 px-3 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50"
              >
                Apply
              </Button>
            </div>
          )}
        </div>
        {rangeError && <p className="text-[12.5px] font-medium text-red-600">{rangeError}</p>}
        <p className="text-[13px] text-slate-500">
          {(data?.range.mode ?? (range ? "launched_in_range" : "all_time")) === "launched_in_range" ? (
            <>
              <span className="font-semibold text-slate-700">Jobs launched in range:</span> jobs whose first
              successful PAIR launch (the Launch Report&apos;s PAIR Launch) falls between{" "}
              <span className="font-semibold text-slate-700">{rangeLabel(data?.range, range)}</span> (ET), inclusive.
            </>
          ) : (
            <>
              <span className="font-semibold text-slate-700">All time:</span> every job in scope, launched or not.
            </>
          )}{" "}
          Candidate numbers cover each job&apos;s whole life, the same counts its Rankings page shows.
        </p>
      </div>

      {error && (
        <div className="flex items-center justify-between rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
          <div className="flex items-center gap-2">
            <TriangleAlert className="h-4 w-4 text-red-600" />
            <span>{error}</span>
          </div>
          <button
            type="button"
            className="font-semibold underline decoration-red-400 underline-offset-2 hover:text-red-900"
            onClick={() => load(true)}
          >
            Retry
          </button>
        </div>
      )}

      {!!data?.warnings?.length && (
        <Card className="flex items-start gap-3 p-4 border-amber-200 bg-amber-50 rounded-xl">
          <TriangleAlert className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
          <div className="text-[13px] text-amber-800 leading-relaxed space-y-1">
            {data.warnings.map((w) => (
              <p key={w}>{w}</p>
            ))}
          </div>
        </Card>
      )}

      {/* KPI tiles — the backend's distinct-job totals */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        <KpiTile
          label="Jobs Launched"
          icon={Rocket}
          tone="indigo"
          loading={firstLoad}
          {...tile(false, num(totals?.jobs.launched), `of ${num(totals?.jobs.assigned)} jobs in view`)}
        />
        <KpiTile
          label="Candidates Launched"
          icon={Briefcase}
          tone="sky"
          loading={firstLoad}
          {...tile(false, num(totals?.candidates.launched), `${num(totals?.candidates.sourced)} sourced`)}
        />
        <KpiTile
          label="Pass Candidates"
          icon={UserCheck}
          tone="emerald"
          loading={firstLoad}
          {...tile(true, num(totals?.outcomes.passed), `Pass rate ${formatRate(totals?.pass_rate)}`)}
        />
        <KpiTile
          label="PAIR Submittals"
          icon={Send}
          tone="violet"
          loading={firstLoad}
          {...tile(
            true,
            num((totals?.submittals.pair_internal ?? 0) + (totals?.submittals.pair_external ?? 0)),
            `Internal ${num(totals?.submittals.pair_internal)} · External ${num(totals?.submittals.pair_external)}`,
          )}
        />
        <KpiTile
          label="JobDiva-Confirmed"
          icon={ShieldCheck}
          tone="teal"
          loading={firstLoad}
          {...tile(false, num(totals?.submittals.jobdiva_confirmed), `of ${num(totals?.submittals.jobdiva_total)} JobDiva submittals`)}
        />
        <KpiTile
          label="Awaiting Feedback"
          icon={Clock}
          tone="amber"
          loading={firstLoad}
          {...tile(true, num(totals?.feedback.awaiting), "Decided interviews, no decision yet")}
        />
      </div>

      {/* Recruiter table */}
      <div className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden">
        <div className="px-6 py-4 border-b border-slate-200 bg-[#fcfdfd] flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[16px] font-bold text-slate-900">Recruiters</h2>
            <p className="text-[12px] text-slate-500 mt-0.5">
              Click a recruiter to see their jobs.{" "}
              <span className="inline-flex items-center gap-1 align-middle">
                <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" /> Pass
                <span className="inline-block h-2 w-2 rounded-full bg-rose-400 ml-1.5" /> Fail
                <span className="inline-block h-2 w-2 rounded-full bg-amber-300 ml-1.5" /> In progress
              </span>
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-1.5 text-[12.5px] font-semibold text-slate-500 select-none">
              <input
                type="checkbox"
                checked={hideNoLogin}
                onChange={(e) => setHideNoLogin(e.target.checked)}
                className="h-3.5 w-3.5 accent-primary"
              />
              Hide recruiters without a PAIR login{noLoginCount ? ` (${noLoginCount})` : ""}
            </label>
            <div className="flex items-center gap-2 h-9 px-3 rounded-lg border border-slate-200 bg-white">
              <Search className="h-3.5 w-3.5 text-slate-400" />
              <input
                type="search"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Filter recruiters"
                aria-label="Filter recruiters"
                className="w-44 text-[13px] text-slate-700 outline-none bg-transparent"
              />
            </div>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 font-bold text-slate-500 text-[12px]">
                <th
                  className="py-3 px-4 sticky left-0 z-10 bg-slate-50 min-w-[240px]"
                  aria-sort={ariaSort(sort, "email")}
                >
                  <SortButton label="Recruiter" active={sort.key === "email"} dir={sort.dir} onClick={() => toggleSort("email")} />
                </th>
                {METRIC_COLUMNS.map((col) => (
                  <th
                    key={col.key}
                    className="py-3 px-3 text-right align-bottom"
                    title={col.hint}
                    aria-sort={ariaSort(sort, col.key)}
                  >
                    <SortButton
                      label={col.label}
                      sub={col.sub}
                      active={sort.key === col.key}
                      dir={sort.dir}
                      onClick={() => toggleSort(col.key)}
                      alignRight
                    />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[13px] text-slate-700">
              {firstLoad ? (
                [1, 2, 3].map((i) => (
                  <tr key={i}>
                    <td className="py-4 px-4">
                      <div className="h-4 w-48 bg-slate-100 animate-pulse rounded" />
                    </td>
                    {METRIC_COLUMNS.map((col) => (
                      <td key={col.key} className="py-4 px-3">
                        <div className="h-4 w-10 bg-slate-100 animate-pulse rounded ml-auto" />
                      </td>
                    ))}
                  </tr>
                ))
              ) : loadFailed ? (
                <tr>
                  <td colSpan={columnCount} className="py-12 text-center text-slate-400 text-[13px]">
                    Couldn&apos;t load recruiter analytics.
                  </td>
                </tr>
              ) : visibleRecruiters.length === 0 ? (
                <tr>
                  <td colSpan={columnCount} className="py-12 text-center text-slate-400 text-[13px]">
                    {allRecruiters.length
                      ? "No recruiters match the current filters."
                      : "No recruiter has a job in this view."}
                  </td>
                </tr>
              ) : (
                visibleRecruiters.map((rec) => {
                  const isOpen = expanded.has(rec.email);
                  return (
                    <Fragment key={rec.email}>
                      <tr
                        className={`group cursor-pointer transition-colors ${isOpen ? "bg-[#f6f8fb]" : "hover:bg-[#f6f8fb]"}`}
                        onClick={() => toggleExpanded(rec.email)}
                      >
                        <td
                          className={`py-3 px-4 sticky left-0 z-10 transition-colors ${
                            isOpen ? "bg-[#f6f8fb]" : "bg-white group-hover:bg-[#f6f8fb]"
                          }`}
                        >
                          <button
                            type="button"
                            aria-expanded={isOpen}
                            aria-label={`${isOpen ? "Hide" : "Show"} jobs for ${rec.email}`}
                            onClick={(e) => {
                              e.stopPropagation();
                              toggleExpanded(rec.email);
                            }}
                            className="flex items-start gap-2 text-left"
                          >
                            {isOpen ? (
                              <ChevronDown className="h-4 w-4 mt-0.5 shrink-0 text-slate-400" />
                            ) : (
                              <ChevronRight className="h-4 w-4 mt-0.5 shrink-0 text-slate-400" />
                            )}
                            <span className="min-w-0">
                              <span className="block font-semibold text-slate-800 break-all">{rec.email}</span>
                              <span className="flex flex-wrap items-center gap-1.5 mt-0.5 text-[11.5px] text-slate-400">
                                {rec.team_name && <span>{rec.team_name}</span>}
                                {rec.is_team_lead && (
                                  <span className="rounded bg-indigo-50 px-1.5 py-px text-[10.5px] font-semibold text-indigo-600">
                                    Lead
                                  </span>
                                )}
                                {rec.has_pair_account === false && (
                                  <span
                                    className="rounded bg-slate-100 px-1.5 py-px text-[10.5px] font-semibold text-slate-400"
                                    title="Not on a PAIR team or in PAIR's user list — often a JobDiva account manager or client contact on the job."
                                  >
                                    no PAIR login
                                  </span>
                                )}
                              </span>
                            </span>
                          </button>
                        </td>
                        {METRIC_COLUMNS.map((col) => (
                          <td key={col.key} className="py-3 px-3 text-right tabular-nums">
                            {metricCell(col, rec, unavailable)}
                          </td>
                        ))}
                      </tr>
                      {isOpen && (
                        <tr className="bg-[#fafbfc]">
                          <td colSpan={columnCount} className="p-0 border-l-2 border-primary/40">
                            <RecruiterJobs
                              jobs={rec.job_ids.map((id) => jobsById.get(id)).filter((j): j is JobRow => !!j)}
                              recruiterEmail={rec.email}
                              unavailable={unavailable}
                            />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })
              )}
            </tbody>
            {totals && visibleRecruiters.length > 0 && (
              <tfoot>
                <tr className="border-t-2 border-slate-200 bg-slate-50 text-[13px] font-semibold text-slate-800">
                  <td className="py-3 px-4 sticky left-0 z-10 bg-slate-50" title="Each job counted once, however many recruiters share it">
                    Totals
                    <span className="block text-[11.5px] font-medium text-slate-400">
                      Distinct jobs, not the sum of the rows
                    </span>
                  </td>
                  {METRIC_COLUMNS.map((col) => (
                    <td key={col.key} className="py-3 px-3 text-right tabular-nums">
                      {metricCell(col, totals, unavailable)}
                    </td>
                  ))}
                </tr>
              </tfoot>
            )}
          </table>
        </div>

        {!!data?.unassigned_jobs && (
          <p className="px-6 py-3 border-t border-slate-100 text-[12.5px] text-slate-500">
            {num(data.unassigned_jobs)} {data.unassigned_jobs === 1 ? "job has" : "jobs have"} no assigned recruiter;{" "}
            {data.unassigned_jobs === 1 ? "it counts" : "they count"} in the totals only.
          </p>
        )}
      </div>

      <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2 text-[12.5px] text-slate-500 max-w-[1100px]">
        <div>
          <dt className="inline font-semibold text-slate-700">Pass / Fail / In Progress: </dt>
          <dd className="inline">the same status the job&apos;s Rankings page shows for each candidate.</dd>
        </div>
        <div>
          <dt className="inline font-semibold text-slate-700">PAIR Internal / External: </dt>
          <dd className="inline">
            Submits recorded in PAIR, to a hiring manager or to the client. Submits from before the split count as
            external.
          </dd>
        </div>
        <div>
          <dt className="inline font-semibold text-slate-700">JobDiva-Confirmed: </dt>
          <dd className="inline">
            external submittals JobDiva confirms. A re-edited job (v2) and its original count once.
          </dd>
        </div>
        <div>
          <dt className="inline font-semibold text-slate-700">Awaiting Feedback: </dt>
          <dd className="inline">the interview is decided but no recruiter decision is recorded yet.</dd>
        </div>
        <div>
          <dt className="inline font-semibold text-slate-700">Step 5 Active Time: </dt>
          <dd className="inline">
            time the recruiter personally had Step 5 (Source) open and in use, on any job in view — paused when the
            tab is hidden or after 5 minutes idle. Shown as the average per job they timed. Totals and each job&apos;s
            own number include everyone who worked it, admins too.
          </dd>
        </div>
        <div>
          <dt className="inline font-semibold text-slate-700">Step 5 → Launch: </dt>
          <dd className="inline">
            from a job&apos;s first Step 5 visit (by anyone) to its first successful PAIR launch, averaged over the
            recruiter&apos;s assigned jobs. A dash means there is no value: not tracked (jobs worked before this was
            measured have no data), not launched yet, or launched before Step 5 was first timed.
          </dd>
        </div>
      </dl>
    </div>
  );
}

function SortButton({
  label,
  sub,
  active,
  dir,
  onClick,
  alignRight = false,
}: {
  label: string;
  sub?: string;
  active: boolean;
  dir: SortDir;
  onClick: () => void;
  alignRight?: boolean;
}) {
  const Arrow = dir === "asc" ? ArrowUp : ArrowDown;
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex flex-col gap-0.5 whitespace-nowrap hover:text-slate-800 ${
        alignRight ? "items-end text-right" : "items-start text-left"
      } ${active ? "text-slate-800" : ""}`}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        <Arrow className={`h-3 w-3 ${active ? "opacity-100" : "opacity-0"}`} />
      </span>
      {sub && <span className="text-[10.5px] font-medium text-slate-400">{sub}</span>}
    </button>
  );
}
