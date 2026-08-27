"use client";

// Daily PAIR launch report.
//
// One row per job whose FIRST PAIR launch landed on the selected date. The
// date is a calendar date in Eastern time, matching the backend
// (routers/launch_report.py) — a job launched at 22:00 EDT belongs to that
// day, not to the next UTC one. Every timestamp here renders in
// America/New_York for the same reason.
//
// The outreach columns (Pending → Phase 3) are fetched live from pair-bot,
// one call per launched interview. They can come back partially resolved, so
// a row that did not fully resolve is marked rather than silently showing
// zeros — see the "partial" badge on the job cell.

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowLeft, CalendarDays, RefreshCw, ShieldAlert, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

interface LaunchReportRow {
  job_id: string;
  jobdiva_id: string;
  recruiter_emails: string[];
  job_title: string;
  customer_name: string;
  jobdiva_published_date: string | null;
  pair_published_at: string | null;
  time_to_source_minutes: number | null;
  total_candidates_sourced: number;
  pair_launch_at: string | null;
  total_candidates_launched: number;
  time_to_launch_minutes: number | null;
  pending: number;
  in_progress: number;
  completed: number;
  partial_complete: number;
  time_to_first_response_minutes: number | null;
  launch_to_response_minutes: number | null;
  overall_response_time_minutes: number | null;
  submitted_candidates: number;
  rejected_candidates: number;
  outstanding_feedback: number;
  time_to_feedback_minutes: number | null;
  time_to_first_pass_minutes: number | null;
  call: number;
  sms: number;
  web: number;
  phase1: number;
  phase2: number;
  phase3: number;
  percentage: number | null;
  outreach_detail_resolved: number;
  outreach_detail_expected: number;
}

interface LaunchReportData {
  report_date: string;
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

/** Yesterday's calendar date in Eastern time, as YYYY-MM-DD. */
function yesterdayEastern(): string {
  const yesterday = new Date(Date.now() - 24 * 60 * 60 * 1000);
  // en-CA gives ISO-shaped output (YYYY-MM-DD) directly.
  return yesterday.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
}

/** ISO date-only → "Feb 24, 2026"; null/invalid → "—". */
function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(`${iso}T00:00:00`) : new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/** ISO datetime → "Feb 24, 2026, 10:30 AM EST"; null/invalid → "—". */
function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

/** Minutes → "45m" / "1h 22m" / "2d 3h"; null → "—". */
function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return "—";
  const total = Math.round(minutes);
  if (total < 60) return `${total}m`;
  const hours = Math.floor(total / 60);
  if (hours < 24) {
    const rem = total % 60;
    return rem ? `${hours}h ${rem}m` : `${hours}h`;
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days}d ${remHours}h` : `${days}d`;
}

function formatPercent(value: number | null): string {
  return value === null || value === undefined ? "—" : `${value}%`;
}

// Column groups drive both the header spans and the cell order, so the two
// can't drift apart as columns get added.
type Column = {
  key: string;
  label: string;
  render: (row: LaunchReportRow) => React.ReactNode;
  /** Right-aligned for numbers, left for text/timestamps. */
  numeric?: boolean;
};

type ColumnGroup = { title: string; columns: Column[] };

const num = (value: number) => (value ? value.toLocaleString() : "0");

const COLUMN_GROUPS: ColumnGroup[] = [
  {
    title: "Job",
    columns: [
      {
        key: "recruiter",
        label: "Recruiter",
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
      { key: "customer", label: "Customer", render: (r) => r.customer_name || "—" },
    ],
  },
  {
    title: "Sourcing",
    columns: [
      { key: "jd_published", label: "JobDiva Published", render: (r) => formatDate(r.jobdiva_published_date) },
      { key: "pair_published", label: "PAIR Published", render: (r) => formatDateTime(r.pair_published_at) },
      { key: "tt_source", label: "Time to Source", numeric: true, render: (r) => formatDuration(r.time_to_source_minutes) },
      { key: "sourced", label: "Sourced", numeric: true, render: (r) => num(r.total_candidates_sourced) },
    ],
  },
  {
    title: "Launch",
    columns: [
      { key: "launch_at", label: "PAIR Launch", render: (r) => formatDateTime(r.pair_launch_at) },
      { key: "launched", label: "Launched", numeric: true, render: (r) => num(r.total_candidates_launched) },
      { key: "tt_launch", label: "Time to Launch", numeric: true, render: (r) => formatDuration(r.time_to_launch_minutes) },
    ],
  },
  {
    title: "Interview Status",
    columns: [
      { key: "pending", label: "Pending", numeric: true, render: (r) => num(r.pending) },
      { key: "in_progress", label: "In Progress", numeric: true, render: (r) => num(r.in_progress) },
      { key: "completed", label: "Completed", numeric: true, render: (r) => num(r.completed) },
      { key: "partial", label: "Partial Complete", numeric: true, render: (r) => num(r.partial_complete) },
      {
        key: "percentage",
        label: "%",
        numeric: true,
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
      { key: "tt_first_resp", label: "To First Response", numeric: true, render: (r) => formatDuration(r.time_to_first_response_minutes) },
      { key: "launch_to_resp", label: "Launch → Response", numeric: true, render: (r) => formatDuration(r.launch_to_response_minutes) },
      { key: "overall_resp", label: "Overall Response", numeric: true, render: (r) => formatDuration(r.overall_response_time_minutes) },
    ],
  },
  {
    title: "Feedback",
    columns: [
      { key: "submitted", label: "Submitted", numeric: true, render: (r) => num(r.submitted_candidates) },
      { key: "rejected", label: "Rejected", numeric: true, render: (r) => num(r.rejected_candidates) },
      { key: "outstanding", label: "Outstanding", numeric: true, render: (r) => num(r.outstanding_feedback) },
      { key: "tt_feedback", label: "Time to Feedback", numeric: true, render: (r) => formatDuration(r.time_to_feedback_minutes) },
      { key: "tt_first_pass", label: "To First Pass", numeric: true, render: (r) => formatDuration(r.time_to_first_pass_minutes) },
    ],
  },
  {
    title: "Channel",
    columns: [
      { key: "call", label: "Call", numeric: true, render: (r) => num(r.call) },
      { key: "sms", label: "SMS", numeric: true, render: (r) => num(r.sms) },
      { key: "web", label: "Web", numeric: true, render: (r) => num(r.web) },
    ],
  },
  {
    title: "Phase",
    columns: [
      { key: "phase1", label: "Phase 1", numeric: true, render: (r) => num(r.phase1) },
      { key: "phase2", label: "Phase 2", numeric: true, render: (r) => num(r.phase2) },
      { key: "phase3", label: "Phase 3", numeric: true, render: (r) => num(r.phase3) },
    ],
  },
];

const FLAT_COLUMNS = COLUMN_GROUPS.flatMap((g) => g.columns);

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

  // Computed once per mount: the report is historical, so re-deriving "today"
  // mid-session would let the max silently drift past midnight.
  const [maxDate] = useState<string>(yesterdayEastern);
  const [date, setDate] = useState<string>(maxDate);
  const [data, setData] = useState<LaunchReportData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Bumped by Refresh to re-run the effect for an unchanged date.
  const [reloadToken, setReloadToken] = useState(0);

  // The spinner is turned ON by whatever triggers a load — initial state, a
  // date change, or Refresh — so this effect only ever updates state after an
  // await. Setting it synchronously here would cascade an extra render.
  const requestReload = useCallback(() => {
    setIsLoading(true);
    setError(null);
    setReloadToken((token) => token + 1);
  }, []);

  useEffect(() => {
    if (isRoleLoading || !canView) return;
    // Guards against a slow response for an earlier date landing after a
    // newer one and overwriting it.
    let cancelled = false;
    (async () => {
      try {
        const res = await api.launchReport.get(date);
        if (cancelled) return;
        setData(res?.data ?? null);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        console.error("Error loading launch report:", err);
        setError(err instanceof Error ? err.message : "Failed to load the launch report.");
        setData(null);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isRoleLoading, canView, date, reloadToken]);

  const rows = useMemo(() => data?.jobs ?? [], [data]);

  // A row is "partial" when pair-bot did not answer for every launched
  // interview — its outreach columns undercount and must not read as real.
  const partialRows = useMemo(
    () => rows.filter((r) => r.outreach_detail_resolved < r.outreach_detail_expected).length,
    [rows],
  );

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
        </div>
        <div className="flex items-center gap-2">
          <label
            htmlFor="report-date"
            className="flex items-center gap-2 h-10 px-3 rounded-lg border border-slate-200 bg-white shadow-sm"
          >
            <CalendarDays className="h-4 w-4 text-slate-400" />
            <input
              id="report-date"
              type="date"
              value={date}
              max={maxDate}
              required
              // Clearing the field yields "" — fall back to the default date
              // rather than ignoring the event, so the input and state never
              // disagree about what is displayed.
              onChange={(e) => {
                setIsLoading(true);
                setError(null);
                setDate(e.target.value || maxDate);
              }}
              className="text-[13px] font-semibold text-slate-700 outline-none bg-transparent"
            />
          </label>
          <Button
            variant="outline"
            onClick={requestReload}
            disabled={isLoading}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${isLoading ? "animate-spin text-primary" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      <p className="text-[13px] text-slate-500 leading-relaxed max-w-[880px]">
        Jobs whose first PAIR launch happened on{" "}
        <span className="font-semibold text-slate-700">{formatDate(data?.report_date ?? date)}</span>. Dates and times
        are Eastern (EDT/EST), so a job launched late in the evening belongs to that day rather than the next. Interview
        status, channel, phase and response columns are read live from PAIR Bot.
      </p>

      {error && (
        <Card className="p-4 border-red-200 bg-red-50 text-[13px] text-red-700 rounded-xl">{error}</Card>
      )}

      {partialRows > 0 && (
        <Card className="flex items-start gap-3 p-4 border-amber-200 bg-amber-50 rounded-xl">
          <TriangleAlert className="h-4 w-4 text-amber-600 mt-0.5 shrink-0" />
          <p className="text-[13px] text-amber-800 leading-relaxed">
            PAIR Bot answered for {data?.totals.outreach_detail_resolved ?? 0} of{" "}
            {data?.totals.outreach_detail_expected ?? 0} launched interviews. {partialRows}{" "}
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
        <StatTile
          label="Generated"
          value={data?.generated_at ? formatDateTime(data.generated_at).split(", ").slice(-1)[0] : "—"}
          hint={data?.generated_at ? formatDate(data.generated_at) : undefined}
        />
      </div>

      {/* The table is wide by design (29 columns) — it scrolls inside its own
          container so the page body never scrolls horizontally, and the job
          column is pinned so a row stays identifiable while scrolling. */}
      <Card className="border-slate-200 bg-white shadow-sm rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="bg-slate-50 border-b border-slate-200">
                <th
                  rowSpan={2}
                  className="sticky left-0 z-20 bg-slate-50 text-left px-4 py-2 font-extrabold uppercase tracking-wider text-[10px] text-slate-500 border-r border-slate-200 min-w-[240px]"
                >
                  Job
                </th>
                {COLUMN_GROUPS.map((group) => (
                  <th
                    key={group.title}
                    colSpan={group.columns.length}
                    className="text-left px-3 py-2 font-extrabold uppercase tracking-wider text-[10px] text-slate-400 border-l border-slate-200"
                  >
                    {group.title}
                  </th>
                ))}
              </tr>
              <tr className="bg-slate-50 border-b border-slate-200">
                {COLUMN_GROUPS.flatMap((group) =>
                  group.columns.map((col, idx) => (
                    <th
                      key={col.key}
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
              {isLoading ? (
                <tr>
                  <td colSpan={FLAT_COLUMNS.length + 1} className="p-8 text-center text-[13px] text-slate-500">
                    Loading launch report…
                  </td>
                </tr>
              ) : rows.length === 0 && !error ? (
                <tr>
                  <td colSpan={FLAT_COLUMNS.length + 1} className="p-8 text-center text-[13px] text-slate-500">
                    No jobs were launched on {formatDate(data?.report_date ?? date)}.
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
                          {isPartial && (
                            <span
                              className="inline-flex items-center rounded-md border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-extrabold uppercase tracking-wider text-amber-700"
                              title={`PAIR Bot answered for ${row.outreach_detail_resolved} of ${row.outreach_detail_expected} interviews — outreach columns undercount`}
                            >
                              Partial
                            </span>
                          )}
                        </div>
                      </td>
                      {FLAT_COLUMNS.map((col) => (
                        <td
                          key={col.key}
                          className={`px-3 py-3 whitespace-nowrap text-slate-700 ${
                            col.numeric ? "text-right tabular-nums" : "text-left"
                          }`}
                        >
                          {col.render(row)}
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
