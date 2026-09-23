"use client";

// Daily PAIR launch report.
//
// One row per job whose FIRST successful PAIR launch landed on the selected
// date. The date is a calendar date in Eastern time, matching the backend
// (routers/launch_report.py) — a job launched at 22:00 EDT belongs to that
// day, not to the next UTC one. Every timestamp here renders in
// America/New_York for the same reason, as "MM/DD/YYYY HH:MM:SS" with the
// zone named once in the column header ("… (ET)") rather than in every cell
// and CSV value (lib/date.ts).
//
// Each row is that job's rank list, summarised: the same candidate counts
// the Rankings page shows for the job today (one unit per launched person,
// over the job's whole lifetime), indexed by the day the job first launched.
// The backend shares its population and classification code with the
// Rankings header, so the two screens cannot disagree, and every Interview
// Status / Feedback number counts launched people only.
//
// The outreach columns (Pending → Extra 3) are fetched live from pair-bot,
// one call per launched candidate. They can come back partially resolved, so
// a row that did not fully resolve is marked rather than silently showing
// zeros — see the "partial" badge on the job cell.
//
// Today is selectable. While the requested range includes today the page is
// "Live": it silently re-requests the same range every 2 minutes, only while
// the tab is visible and never on top of a request already in flight, and
// keeps the current rows on screen while it does.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, CalendarDays, Download, RefreshCw, ShieldAlert, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import { PhaseOutreachInfo } from "./PhaseOutreachInfo";
import { UTF8_BOM, toCsv } from "@/lib/csv";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  addIsoDays,
  formatDuration,
  formatEasternDate,
  formatEasternDateTime,
  formatEasternTime,
  inclusiveDateSpanDays,
  todayEastern,
  withEasternLabel,
} from "@/lib/date";

interface LaunchReportRow {
  job_id: string;
  jobdiva_id: string;
  recruiter_emails: string[];
  /** Who posted / first launched the job in PAIR; null = not recorded (jobs before 2026-09-23). */
  posted_by: string | null;
  launched_by: string | null;
  job_title: string;
  customer_name: string;
  launch_date: string | null;
  version: number;
  jobdiva_published_date: string | null;
  pair_published_at: string | null;
  time_to_source_minutes: number | null;
  total_candidates_sourced: number;
  pair_launch_at: string | null;
  total_candidates_launched: number;
  time_to_launch_minutes: number | null;
  turn_around_time_minutes: number | null;
  /**
   * Step 5 ("Source") time. null = not tracked (a job worked before the
   * tracking existed, or the read failed) — shown as "—", never as 0.
   */
  step5_active_minutes: number | null;
  step5_to_launch_minutes: number | null;
  pending: number;
  in_progress: number;
  completed: number;
  partial_complete: number;
  first_attempted_at: string | null;
  first_completed_at: string | null;
  time_to_first_response_minutes: number | null;
  launch_to_response_minutes: number | null;
  overall_response_time_minutes: number | null;
  submitted_candidates: number;
  internal_submitted_candidates: number;
  external_submitted_candidates: number;
  rejected_candidates: number;
  passed_candidates: number;
  failed_candidates: number;
  outstanding_feedback: number;
  time_to_feedback_minutes: number | null;
  first_feedback_at: string | null;
  first_external_submit_at: string | null;
  first_pass_at: string | null;
  time_to_first_pass_minutes: number | null;
  call: number;
  sms: number;
  web: number;
  phase1: number;
  phase2: number;
  phase3: number;
  phase4: number;
  extra: number;
  extra1: number;
  extra2: number;
  extra3: number;
  percentage: number | null;
  outreach_detail_resolved: number;
  outreach_detail_expected: number;
}

interface LaunchReportData {
  report_date: string;
  start_date: string;
  end_date: string;
  timezone: string;
  generated_at: string | null;
  jobs: LaunchReportRow[];
  totals: {
    jobs: number;
    candidates_sourced: number;
    candidates_launched: number;
    outreach_detail_resolved: number;
    outreach_detail_expected: number;
  };
}

const MAX_LAUNCH_REPORT_RANGE_DAYS = 31;

// Live-mode refresh cadence. Each refresh re-runs the whole report, which
// asks pair-bot about every launched candidate (bounded server-side by a
// concurrency cap and a 120s budget), so this stays at minutes, not seconds.
// Measured from when the previous request SETTLED, not when it started: a
// month-to-date range can itself take ~120s, and timing from the start made
// such a report re-request the moment it finished, keeping pair-bot under
// near-constant load from every open tab.
const LIVE_REFRESH_MS = 120_000;
// How often to check whether a live refresh is due. Checking often and
// refreshing only when 2 minutes have passed since the last request settled
// lets a tab that was hidden refresh as soon as it is visible again, without
// a second refresh landing seconds later off an unrelated timer.
const LIVE_CHECK_MS = 15_000;

type DateRange = { start: string; end: string };

function rangeIncludes(range: DateRange | null, day: string): boolean {
  return !!range && range.start <= day && day <= range.end;
}

function earliestIsoDate(a: string, b: string): string {
  return a < b ? a : b;
}

function formatPercent(value: number | null): string {
  return value === null || value === undefined ? "—" : `${value}%`;
}

/** "02/24/2026" for a single day, or "02/24/2026 – 02/28/2026" for a range. */
function formatDateRange(start: string | null | undefined, end: string | null | undefined): string {
  if (!start) return "—";
  if (!end || end === start) return formatEasternDate(start);
  return `${formatEasternDate(start)} – ${formatEasternDate(end)}`;
}

/** An email the backend may not have recorded (null → "—"). */
const person = (email: string | null | undefined) => email || "—";

// Column groups drive the header spans, the cell order AND the CSV export, so
// the three can't drift apart as columns get added. Every date-time column's
// label carries "(ET)" (withEasternLabel) and its cells don't, on screen and
// in the CSV alike; calendar-date columns carry neither.
type Column = {
  key: string;
  label: string;
  /** Header tooltip for a column whose name alone is ambiguous. Screen only. */
  hint?: string;
  /** Plain-text value. Used for the CSV, and for display unless `render` overrides. */
  text: (row: LaunchReportRow) => string;
  /** Optional richer cell; the CSV always uses `text`. */
  render?: (row: LaunchReportRow) => React.ReactNode;
  /** Right-aligned for numbers, left for text/timestamps. */
  numeric?: boolean;
};

type ColumnGroup = { title: string; columns: Column[]; showInfo?: boolean };

const num = (value: number) => (value ? value.toLocaleString() : "0");

const COLUMN_GROUPS: ColumnGroup[] = [
  {
    title: "Job",
    columns: [
      // Only shown for a multi-day range report — a single-day report has
      // one date for every row, so the column would be redundant there.
      { key: "launch_date", label: "Launch Date", text: (r) => formatEasternDate(r.launch_date) },
      {
        key: "recruiter",
        label: "Recruiter",
        hint: "The job's assigned recruiters",
        text: (r) => r.recruiter_emails.join("; ") || "—",
        render: (r) =>
          r.recruiter_emails.length ? (
            <span title={r.recruiter_emails.join(", ")}>
              {r.recruiter_emails[0]}
              {r.recruiter_emails.length > 1 && (
                <span className="text-slate-400"> +{r.recruiter_emails.length - 1}</span>
              )}
            </span>
          ) : (
            "—"
          ),
      },
      // Who acted in PAIR, not who is assigned. Not recorded ("—") for jobs
      // posted or launched before 2026-09-23.
      {
        key: "posted_by",
        label: "Posted By",
        hint: "Who first saved the job in PAIR",
        text: (r) => person(r.posted_by),
      },
      {
        key: "launched_by",
        label: "Launched By",
        hint: "Whose Launch PAIR click first launched the job",
        text: (r) => person(r.launched_by),
      },
      { key: "customer", label: "Customer", text: (r) => r.customer_name || "—" },
    ],
  },
  {
    title: "Sourcing",
    columns: [
      { key: "jd_published", label: "JobDiva Published", text: (r) => formatEasternDate(r.jobdiva_published_date) },
      { key: "pair_published", label: withEasternLabel("PAIR Published"), text: (r) => formatEasternDateTime(r.pair_published_at) },
      { key: "tt_source", label: "Time to Source", numeric: true, text: (r) => formatDuration(r.time_to_source_minutes) },
      // Same population as the job's Rankings page ("Showing N of M candidates").
      { key: "sourced", label: "Sourced", numeric: true, text: (r) => num(r.total_candidates_sourced) },
    ],
  },
  {
    title: "Launch",
    columns: [
      {
        key: "launch_at",
        label: withEasternLabel("PAIR Launch"),
        hint: "The job's first successful launch",
        text: (r) => formatEasternDateTime(r.pair_launch_at),
      },
      // The Rankings page's "Candidates Launched": people, not interviews. The
      // Interview Status buckets below are computed over exactly this set.
      { key: "launched", label: "Launched", numeric: true, text: (r) => num(r.total_candidates_launched) },
      { key: "tt_launch", label: "Time to Launch", numeric: true, text: (r) => formatDuration(r.time_to_launch_minutes) },
      {
        key: "tat",
        label: "Turn Around Time",
        numeric: true,
        text: (r) => formatDuration(r.turn_around_time_minutes),
        render: (r) => (
          <span title="PAIR Launch − PAIR Published">{formatDuration(r.turn_around_time_minutes)}</span>
        ),
      },
      // Measured by the job wizard itself (services/job_step_time.py), so a
      // job worked before that existed has no data and shows "—", not 0m.
      {
        key: "step5_active",
        label: "Step 5 Active Time",
        numeric: true,
        hint: "Time recruiters spent working on Step 5 (Source) — tab open and in use, all visits and recruiters",
        text: (r) => formatDuration(r.step5_active_minutes),
      },
      // Ends at the PAIR Launch column's instant: both use the first
      // SUCCESSFUL launch.
      {
        key: "step5_to_launch",
        label: "Step 5 → Launch",
        numeric: true,
        hint: "From first opening Step 5 to the first successful PAIR launch",
        text: (r) => formatDuration(r.step5_to_launch_minutes),
      },
    ],
  },
  {
    title: "Interview Status",
    columns: [
      { key: "pending", label: "Pending", numeric: true, text: (r) => num(r.pending) },
      { key: "in_progress", label: "In Progress", numeric: true, text: (r) => num(r.in_progress) },
      { key: "completed", label: "Completed", numeric: true, text: (r) => num(r.completed) },
      // Pass / Fail exactly as the job's rank list labels each launched
      // candidate; together they make up Completed.
      { key: "passed", label: "Pass Candidates", numeric: true, text: (r) => num(r.passed_candidates) },
      { key: "failed", label: "Fail Candidates", numeric: true, text: (r) => num(r.failed_candidates) },
      { key: "partial", label: "Partial Complete", numeric: true, text: (r) => num(r.partial_complete) },
      {
        key: "percentage",
        label: "%",
        numeric: true,
        text: (r) => formatPercent(r.percentage),
        render: (r) => (
          <span
            className="font-semibold text-slate-900"
            title="(Completed + Partial Complete) ÷ Total Launched"
          >
            {formatPercent(r.percentage)}
          </span>
        ),
      },
    ],
  },
  {
    title: "Response",
    columns: [
      { key: "first_attempted", label: withEasternLabel("First Attempted"), text: (r) => formatEasternDateTime(r.first_attempted_at) },
      { key: "first_completed", label: withEasternLabel("First Completed"), text: (r) => formatEasternDateTime(r.first_completed_at) },
      { key: "tt_first_resp", label: "To First Response", numeric: true, text: (r) => formatDuration(r.time_to_first_response_minutes) },
      { key: "launch_to_resp", label: "Launch → Response", numeric: true, text: (r) => formatDuration(r.launch_to_response_minutes) },
      { key: "overall_resp", label: "Overall Response", numeric: true, text: (r) => formatDuration(r.overall_response_time_minutes) },
    ],
  },
  {
    title: "Feedback",
    // Recruiter decisions recorded in PAIR on LAUNCHED candidates, so none of
    // these can exceed Launched. PAIR submittals are what recruiters recorded,
    // not the JobDiva-confirmed count.
    columns: [
      {
        key: "submitted",
        label: "Submitted",
        numeric: true,
        hint: "PAIR submittals, internal + external",
        text: (r) => num(r.submitted_candidates),
      },
      {
        key: "submitted_internal",
        label: "Submitted – Internal",
        numeric: true,
        hint: "Sent to a hiring manager for review",
        text: (r) => num(r.internal_submitted_candidates),
      },
      {
        key: "submitted_external",
        label: "Submitted – External",
        numeric: true,
        hint: "Submitted to the client",
        text: (r) => num(r.external_submitted_candidates),
      },
      {
        key: "first_external_submit_at",
        label: withEasternLabel("First PAIR External Submittal"),
        hint: "Earliest external submittal a recruiter recorded in PAIR",
        text: (r) => formatEasternDateTime(r.first_external_submit_at),
      },
      { key: "rejected", label: "Rejected", numeric: true, text: (r) => num(r.rejected_candidates) },
      {
        key: "outstanding",
        label: "Outstanding",
        numeric: true,
        hint: "Pass or Fail candidates with no recruiter decision yet",
        text: (r) => num(r.outstanding_feedback),
      },
      { key: "tt_feedback", label: "Time to Feedback", numeric: true, text: (r) => formatDuration(r.time_to_feedback_minutes) },
      { key: "tt_first_pass", label: "To First Pass", numeric: true, text: (r) => formatDuration(r.time_to_first_pass_minutes) },
      { key: "first_pass_at", label: withEasternLabel("First Pass Completed At"), text: (r) => formatEasternDateTime(r.first_pass_at) },
      { key: "first_feedback_at", label: withEasternLabel("First Feedback Submitted At"), text: (r) => formatEasternDateTime(r.first_feedback_at) },
    ],
  },
  {
    title: "Channel",
    columns: [
      { key: "call", label: "Call", numeric: true, text: (r) => num(r.call) },
      { key: "sms", label: "SMS", numeric: true, text: (r) => num(r.sms) },
      { key: "web", label: "Web", numeric: true, text: (r) => num(r.web) },
    ],
  },
  {
    title: "Phase",
    showInfo: true,
    columns: [
      { key: "phase1", label: "Phase 1", numeric: true, text: (r) => num(r.phase1) },
      { key: "phase2", label: "Phase 2", numeric: true, text: (r) => num(r.phase2) },
      { key: "phase3", label: "Phase 3", numeric: true, text: (r) => num(r.phase3) },
      { key: "phase4", label: "Phase 4", numeric: true, text: (r) => num(r.phase4) },
    ],
  },
  {
    title: "Extra Outreach (>80% Match)",
    showInfo: true,
    columns: [
      { key: "extra1", label: "Extra 1", numeric: true, text: (r) => num(r.extra1) },
      { key: "extra2", label: "Extra 2", numeric: true, text: (r) => num(r.extra2) },
      { key: "extra3", label: "Extra 3", numeric: true, text: (r) => num(r.extra3) },
      { key: "extra", label: "Total Extra", numeric: true, text: (r) => num(r.extra) },
    ],
  },
];

const FLAT_COLUMNS = COLUMN_GROUPS.flatMap((g) => g.columns);

/** Column groups with the Launch Date column stripped out for a single-day report. */
function visibleColumnGroups(showLaunchDate: boolean): ColumnGroup[] {
  if (showLaunchDate) return COLUMN_GROUPS;
  return COLUMN_GROUPS.map((g) => ({ ...g, columns: g.columns.filter((c) => c.key !== "launch_date") }));
}

/**
 * Build the report CSV.
 *
 * Columns come from the (possibly range-filtered) column list so the export
 * always matches what is on screen. The leading Job/ID/Version cells mirror
 * the pinned first column, which is rendered outside the column list.
 * Escaping (including formula-injection defence) lives in lib/csv.
 */
function buildCsv(rows: LaunchReportRow[], columns: Column[]): string {
  return toCsv(
    ["Job Title", "JobDiva ID", "Version", ...columns.map((c) => c.label)],
    rows.map((row) => [
      row.job_title || "Untitled job",
      row.jobdiva_id || row.job_id,
      `v${row.version}`,
      ...columns.map((col) => col.text(row)),
    ]),
  );
}

function StatTile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card className="border-slate-200 bg-white shadow-sm rounded-xl px-5 py-4">
      <p className="text-[11px] font-extrabold uppercase tracking-wider text-slate-400">{label}</p>
      <p className="mt-1 text-[24px] font-bold text-slate-900 tabular-nums">{value}</p>
      {hint && <p className="mt-0.5 text-[12px] text-slate-500">{hint}</p>}
    </Card>
  );
}

export default function LaunchReportPage() {
  const { isAdmin, isTeamLead, isLoading: isRoleLoading, email, role } = useUserRole();
  const canView = isAdmin || isTeamLead;

  // Today's Eastern date: the latest selectable date and what "Live" is
  // measured against. Re-derived on every request and every live check, not
  // once at mount, so a page left open past midnight moves its max forward
  // and stops treating the old day as live.
  const [today, setToday] = useState<string>(() => todayEastern());
  // The default selection is the last complete day, like the backend's
  // no-parameter default; "Today" is one click away.
  const defaultDate = addIsoDays(today, -1);
  const [isRange, setIsRange] = useState(false);
  const [selectedDate, setSelectedDate] = useState<string>(() => addIsoDays(todayEastern(), -1));
  const [selectedEndDate, setSelectedEndDate] = useState<string>(() => addIsoDays(todayEastern(), -1));
  const [requestedRange, setRequestedRange] = useState<DateRange | null>(null);
  const [data, setData] = useState<LaunchReportData | null>(null);
  // A full load: the table body shows "Loading…".
  const [isLoading, setIsLoading] = useState(false);
  // A background load (live timer or the Refresh button): rows stay on screen.
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A failed background load. Non-blocking — the last good report stays up.
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const maxRangeEndDate = useMemo(
    () => earliestIsoDate(addIsoDays(selectedDate, MAX_LAUNCH_REPORT_RANGE_DAYS - 1), today),
    [selectedDate, today],
  );

  // Request bookkeeping lives in refs so the live timer reads current values
  // without re-subscribing. requestSeq: the newest request wins — a slower,
  // older response (an earlier range, or a background refresh overtaken by
  // Generate) is dropped instead of overwriting newer data.
  const inFlightRef = useRef(false);
  const requestSeqRef = useRef(0);
  const lastSettledAtRef = useRef(0);

  useEffect(
    () => () => {
      // Unmounting: whatever is still in flight must not land.
      requestSeqRef.current += 1;
    },
    [],
  );

  const fetchReport = useCallback(async (range: DateRange, { background }: { background: boolean }) => {
    const seq = ++requestSeqRef.current;
    inFlightRef.current = true;
    setToday(todayEastern());
    if (background) {
      setIsRefreshing(true);
    } else {
      setIsLoading(true);
      setError(null);
      setRefreshError(null);
    }
    try {
      const res = await api.launchReport.get({ startDate: range.start, endDate: range.end });
      if (seq !== requestSeqRef.current) return;
      setData(res?.data ?? null);
      setError(null);
      setRefreshError(null);
    } catch (err) {
      if (seq !== requestSeqRef.current) return;
      console.error("Error loading launch report:", err);
      const message = err instanceof Error ? err.message : "Failed to load the launch report.";
      if (background) {
        setRefreshError(message);
      } else {
        setError(message);
        setData(null);
      }
    } finally {
      // Only the newest request settles the flags and starts the live
      // countdown; a superseded one leaves both to the request that replaced
      // it. A failed request counts too, so a failing report retries every
      // LIVE_REFRESH_MS rather than every check.
      if (seq === requestSeqRef.current) {
        inFlightRef.current = false;
        lastSettledAtRef.current = Date.now();
        setIsLoading(false);
        setIsRefreshing(false);
      }
    }
  }, []);

  const generateReport = useCallback(() => {
    const now = todayEastern();
    setToday(now);
    if (!selectedDate || (isRange && !selectedEndDate)) {
      setError(isRange ? "Please select both a start and end date." : "Please select a date first.");
      return;
    }
    const end = isRange ? selectedEndDate : selectedDate;
    if (selectedDate > now || end > now) {
      setError("That date is in the future. Please select today or an earlier date.");
      return;
    }
    if (isRange && selectedDate > end) {
      setError("Start date must not be after the end date.");
      return;
    }
    if (isRange && inclusiveDateSpanDays(selectedDate, end) > MAX_LAUNCH_REPORT_RANGE_DAYS) {
      setError(`Date range cannot exceed ${MAX_LAUNCH_REPORT_RANGE_DAYS} days.`);
      return;
    }
    // Set here as well as in fetchReport so the first render after the click
    // already shows "Loading…" rather than a flash of "No jobs were launched".
    setIsLoading(true);
    setError(null);
    setRequestedRange({ start: selectedDate, end });
  }, [selectedDate, selectedEndDate, isRange]);

  const showToday = useCallback(() => {
    const now = todayEastern();
    setToday(now);
    setIsRange(false);
    setSelectedDate(now);
    setSelectedEndDate(now);
    setIsLoading(true);
    setError(null);
    setRequestedRange({ start: now, end: now });
  }, []);

  // Re-runs the report currently on screen. Keeps the rows up while it loads;
  // with nothing on screen yet (e.g. the last load failed) it is a full load.
  const refreshNow = useCallback(() => {
    if (!requestedRange || inFlightRef.current) return;
    void fetchReport(requestedRange, { background: !!data });
  }, [requestedRange, data, fetchReport]);

  // Live = the requested range includes today, so its rows can still change.
  const isLive = canView && rangeIncludes(requestedRange, today);

  // Shows the Launch Date column and the range label only once the loaded
  // report actually spans more than one day, not just because range mode is on.
  const isMultiDay = !!data && data.start_date !== data.end_date;
  const columnGroups = useMemo(() => visibleColumnGroups(isMultiDay), [isMultiDay]);
  const flatColumns = useMemo(() => columnGroups.flatMap((g) => g.columns), [columnGroups]);

  const downloadCsv = useCallback(() => {
    if (!data?.jobs.length) return;
    const blob = new Blob([UTF8_BOM, buildCsv(data.jobs, flatColumns)], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const label = isMultiDay ? `${data.start_date} to ${data.end_date}` : data.report_date;
    link.download = `Pair Bulk Launch Report - ${label}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }, [data, flatColumns, isMultiDay]);

  // A new requested range (Generate / Today) is a full load.
  useEffect(() => {
    if (isRoleLoading || !canView || !requestedRange) return;
    void fetchReport(requestedRange, { background: false });
  }, [isRoleLoading, canView, requestedRange, fetchReport]);

  // Live refresh. Every LIVE_CHECK_MS, and whenever the tab becomes visible,
  // refresh in the background if LIVE_REFRESH_MS has passed since the last
  // request settled — never while the tab is hidden and never while a request
  // is in flight. Torn down when the range changes, when it stops including
  // today (midnight Eastern), and on unmount.
  useEffect(() => {
    if (!isLive || !requestedRange) return;
    const range = requestedRange;
    const refreshIfDue = () => {
      const now = todayEastern();
      setToday(now);
      if (!rangeIncludes(range, now)) return;
      if (document.visibilityState !== "visible") return;
      if (inFlightRef.current) return;
      if (Date.now() - lastSettledAtRef.current < LIVE_REFRESH_MS) return;
      void fetchReport(range, { background: true });
    };
    const interval = setInterval(refreshIfDue, LIVE_CHECK_MS);
    document.addEventListener("visibilitychange", refreshIfDue);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshIfDue);
    };
  }, [isLive, requestedRange, fetchReport]);

  const lastUpdated = data?.generated_at ? formatEasternTime(data.generated_at) : null;


  const rows = useMemo(() => data?.jobs ?? [], [data]);

  // A row is "partial" when pair-bot did not answer for every launched
  // candidate — its outreach columns undercount and must not read as real.
  const partialRows = useMemo(
    () => rows.filter((r) => r.outreach_detail_resolved < r.outreach_detail_expected).length,
    [rows],
  );

  const tableWrapperRef = useRef<HTMLDivElement>(null);
  const [tableMaxHeight, setTableMaxHeight] = useState<string>("calc(100vh - 320px)");

  useEffect(() => {
    const updateHeight = () => {
      if (!tableWrapperRef.current) return;
      const top = tableWrapperRef.current.getBoundingClientRect().top;
      const available = window.innerHeight - top - 32;
      const calculatedHeight = Math.max(350, Math.floor(available));
      setTableMaxHeight(`${calculatedHeight}px`);
    };

    updateHeight();
    window.addEventListener("resize", updateHeight);
    return () => window.removeEventListener("resize", updateHeight);
  }, [data, error, refreshError, partialRows, isRange, rows.length]);

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
            role. The Launch Report is visible to Administrators and Team Leads only.
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

  return (
    <div className="space-y-6 pb-10">
      {/* Page header + date picker */}
      <div className="flex flex-wrap items-center justify-between gap-4 mt-2">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Launch Report</h1>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
            {rows.length} {rows.length === 1 ? "Job" : "Jobs"}
          </span>
          {isLive && (
            <span
              className="inline-flex items-center gap-1.5 rounded-full bg-emerald-50 px-2.5 py-0.5 text-[12px] font-semibold text-emerald-700 ring-1 ring-inset ring-emerald-200"
              title="This report includes today. It refreshes itself every 2 minutes while this tab is open."
            >
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" aria-hidden />
              Live
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 h-10 px-2 text-[12px] font-semibold text-slate-500 select-none">
            <input
              type="checkbox"
              checked={isRange}
              onChange={(e) => {
                setError(null);
                setIsRange(e.target.checked);
              }}
              className="h-3.5 w-3.5 accent-primary"
            />
            Date range
          </label>
          <label
            htmlFor="report-date"
            className="flex items-center gap-2 h-10 px-3 rounded-lg border border-slate-200 bg-white shadow-sm"
          >
            <CalendarDays className="h-4 w-4 text-slate-400" />
            <input
              id="report-date"
              type="date"
              // Deliberately override <html lang="en"> to force MM/DD/YYYY in Chromium/Firefox.
              // Note: Safari ignores this and falls back to OS locale (accepted limitation for Admin tools).
              lang="en-US"
              value={selectedDate}
              max={today}
              required
              // Clearing the field yields "" — fall back to the default date
              // rather than ignoring the event, so the input and state never
              // disagree about what is displayed.
              onChange={(e) => {
                setError(null);
                setSelectedDate(e.target.value || defaultDate);
              }}
              className="text-[13px] font-semibold text-slate-700 outline-none bg-transparent"
            />
          </label>
          {isRange && (
            <label
              htmlFor="report-end-date"
              className="flex items-center gap-2 h-10 px-3 rounded-lg border border-slate-200 bg-white shadow-sm"
            >
              <span className="text-[12px] font-semibold text-slate-400">to</span>
              <input
                id="report-end-date"
                type="date"
                lang="en-US"
                value={selectedEndDate}
                max={maxRangeEndDate}
                min={selectedDate}
                required
                onChange={(e) => {
                  setError(null);
                  setSelectedEndDate(e.target.value || defaultDate);
                }}
                className="text-[13px] font-semibold text-slate-700 outline-none bg-transparent"
              />
            </label>
          )}
          <Button
            variant="outline"
            onClick={showToday}
            disabled={isLoading}
            title="Show jobs first launched today, refreshing live"
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            Today
          </Button>
          <Button
            variant="outline"
            onClick={generateReport}
            disabled={isLoading}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            Generate Report
          </Button>
          <Button
            variant="outline"
            onClick={refreshNow}
            disabled={!requestedRange || isLoading || isRefreshing}
            title={requestedRange ? "Re-run this report now" : "Generate a report first"}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${isRefreshing ? "animate-spin" : ""}`} />
            Refresh
          </Button>
          <Button
            variant="outline"
            onClick={downloadCsv}
            disabled={isLoading || rows.length === 0}
            title={rows.length === 0 ? "Generate a report first, then export CSV" : "Download this report as CSV"}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <Download className="h-4 w-4 text-slate-500" />
            Download CSV
          </Button>
        </div>
      </div>

      <p className="text-[13px] text-slate-500 leading-relaxed max-w-[880px]">
        Jobs whose first successful PAIR launch happened{" "}
        <span className="font-semibold text-slate-700">
          {isMultiDay ? "between " : "on "}
          {formatDateRange(
            data?.start_date ?? requestedRange?.start ?? selectedDate,
            data?.end_date ?? requestedRange?.end ?? (isRange ? selectedEndDate : selectedDate),
          )}
        </span>. All dates and times are Eastern Time (ET), so a job launched late in the evening belongs to that day
        rather than the next. Each row shows the job&apos;s current rank-list numbers — one per launched candidate, over
        the whole life of the job — so Sourced, Launched and the interview status columns match the job&apos;s Rankings
        page, and the interview status and feedback columns count launched candidates only. Interview status, channel,
        phase and response columns are read live from PAIR Bot. The Step 5 time columns only cover jobs worked since
        that tracking began; older jobs show a dash. A report that includes today refreshes itself every 2
        minutes while this tab is open. Date ranges are limited to {MAX_LAUNCH_REPORT_RANGE_DAYS} days.
      </p>

      {/* Freshness: when these numbers were computed, and whether they are
          still moving. A failed background refresh only warns here when a
          report is on screen; with none, the error card above already says
          the load failed. */}
      {(lastUpdated || isRefreshing) && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 -mt-3 text-[12px] text-slate-500">
          {lastUpdated && (
            <span className="tabular-nums">
              Last updated <span className="font-semibold text-slate-700">{lastUpdated}</span> (ET)
              {isLive && " · refreshes every 2 minutes"}
            </span>
          )}
          {isRefreshing && (
            <span className="inline-flex items-center gap-1 text-slate-400">
              <RefreshCw className="h-3 w-3 animate-spin" aria-hidden />
              Refreshing…
            </span>
          )}
          {refreshError && data && !isRefreshing && (
            <span className="inline-flex items-center gap-1 text-amber-700" role="status">
              <TriangleAlert className="h-3.5 w-3.5" aria-hidden />
              Couldn&apos;t refresh ({refreshError}). Showing the last report
              {lastUpdated ? ` from ${lastUpdated} (ET)` : ""}
              {isLive ? " — will retry automatically." : "."}
            </span>
          )}
        </div>
      )}

      {error && (
        <Card className="p-4 border-red-200 bg-red-50 text-[13px] text-red-700 rounded-xl">{error}</Card>
      )}

      {partialRows > 0 && (
        <Card className="flex items-start gap-3 p-4 border-amber-200 bg-amber-50 rounded-xl">
          <TriangleAlert className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
          <p className="text-[13px] text-amber-800 leading-relaxed">
            PAIR Bot answered for {data?.totals.outreach_detail_resolved ?? 0} of{" "}
            {data?.totals.outreach_detail_expected ?? 0} launched candidates. {partialRows}{" "}
            {partialRows === 1 ? "row has" : "rows have"} incomplete outreach columns — status, channel, phase and
            response figures on those rows undercount. Refresh to retry.
          </p>
        </Card>
      )}

      {/* Summary tiles */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatTile label="Jobs Launched" value={num(data?.totals.jobs ?? 0)} />
        <StatTile label="Candidates Sourced" value={num(data?.totals.candidates_sourced ?? 0)} />
        <StatTile label="Candidates Launched" value={num(data?.totals.candidates_launched ?? 0)} />
        {/* Time as the value, date as the hint. (This used to split the
            formatted string on ", " — which it never contains — so the tile
            showed the whole date-time and repeated the date underneath.) */}
        <StatTile
          label={withEasternLabel("Generated")}
          value={data?.generated_at ? formatEasternTime(data.generated_at) : "—"}
          hint={data?.generated_at ? formatEasternDate(data.generated_at) : undefined}
        />
      </div>

      {/* The table is wide by design (45+ columns; COLUMN_GROUPS is the
          count) — it scrolls inside its own container so the page body never
          scrolls horizontally, and the job column is pinned so a row stays
          identifiable while scrolling. */}
      <Card className="border-slate-200 bg-white shadow-sm rounded-xl overflow-hidden">
        <div
          ref={tableWrapperRef}
          style={{ maxHeight: tableMaxHeight }}
          className="overflow-auto min-h-[350px] relative scrollbar-thin scrollbar-thumb-slate-200"
        >
          <table className="w-full border-collapse text-[13px]">
            <thead className="sticky top-0 z-20 bg-slate-50 shadow-[0_1px_0_0_#e2e8f0]">
              <tr className="bg-slate-50 border-b border-slate-200">
                <th
                  rowSpan={2}
                  className="sticky left-0 top-0 z-30 bg-slate-50 text-left px-4 py-2 font-extrabold uppercase tracking-wider text-[10px] text-slate-500 border-r border-slate-200 min-w-[240px]"
                >
                  Job
                </th>
                {columnGroups.map((group) => (
                  <th
                    key={group.title}
                    colSpan={group.columns.length}
                    className="text-left px-3 py-2 font-extrabold uppercase tracking-wider text-[10px] text-slate-400 border-l border-slate-200"
                  >
                    <span className="inline-flex items-center gap-0.5">
                      {group.title}
                      {group.showInfo && <PhaseOutreachInfo />}
                    </span>
                  </th>
                ))}
              </tr>
              <tr className="bg-slate-50 border-b border-slate-200">
                {columnGroups.flatMap((group) =>
                  group.columns.map((col, idx) => (
                    <th
                      key={col.key}
                      title={col.hint}
                      className={`px-3 py-2 font-semibold text-[11px] text-slate-500 whitespace-nowrap ${
                        col.numeric ? "text-right" : "text-left"
                      } ${idx === 0 ? "border-l border-slate-200" : ""}`}
                    >
                      {col.label}
                    </th>
                  )),
                )}
              </tr>
            </thead>
            <tbody>
              {!requestedRange && !isLoading ? (
                <tr>
                  <td colSpan={flatColumns.length + 1} className="p-8 text-center text-[13px] text-slate-500">
                    Select a date and click Generate Report.
                  </td>
                </tr>
              ) : isLoading ? (
                <tr>
                  <td colSpan={flatColumns.length + 1} className="p-8 text-center text-[13px] text-slate-500">
                    Loading launch report…
                  </td>
                </tr>
              ) : rows.length === 0 && !error ? (
                <tr>
                  <td colSpan={flatColumns.length + 1} className="p-8 text-center text-[13px] text-slate-500">
                    No jobs were launched{" "}
                    {isMultiDay ? "between " : "on "}
                    {formatDateRange(
                      data?.start_date ?? requestedRange?.start ?? selectedDate,
                      data?.end_date ?? requestedRange?.end ?? (isRange ? selectedEndDate : selectedDate),
                    )}
                    .
                  </td>
                </tr>
              ) : (
                rows.map((row) => {
                  const isPartial = row.outreach_detail_resolved < row.outreach_detail_expected;
                  return (
                    <tr key={row.job_id} className="border-b border-slate-100 hover:bg-slate-50/70">
                      <td className="sticky left-0 z-10 bg-white px-4 py-3 border-r border-slate-200 min-w-[240px]">
                        <Link
                          href={`/jobs/${row.job_id}/rankings`}
                          className="font-semibold text-slate-900 hover:text-primary hover:underline"
                        >
                          {row.job_title || "Untitled job"}
                        </Link>
                        <div className="flex items-center gap-2 mt-0.5">
                          <span className="text-[12px] text-slate-500 tabular-nums">{row.jobdiva_id || row.job_id}</span>
                          {row.version > 1 && (
                            <span
                              className="inline-flex items-center rounded-md border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[10px] font-extrabold uppercase tracking-wider text-indigo-700"
                              title="A later version of this job, created by Edit Job Setup. It has its own PAIR Published time and its own launches."
                            >
                              v{row.version}
                            </span>
                          )}
                          {isPartial && (
                            <span
                              className="inline-flex items-center rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-extrabold uppercase tracking-wider text-amber-700"
                              title={`PAIR Bot answered for ${row.outreach_detail_resolved} of ${row.outreach_detail_expected} launched candidates — outreach columns undercount`}
                            >
                              Partial
                            </span>
                          )}
                        </div>
                      </td>
                      {flatColumns.map((col) => (
                        <td
                          key={col.key}
                          className={`px-3 py-3 whitespace-nowrap text-slate-700 ${
                            col.numeric ? "text-right tabular-nums" : "text-left"
                          }`}
                        >
                          {col.render ? col.render(row) : col.text(row)}
                        </td>
                      ))}
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
