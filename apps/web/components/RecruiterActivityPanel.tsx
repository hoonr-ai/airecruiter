"use client";

import { useEffect, useRef, useState } from "react";
import { Activity, Clock } from "lucide-react";

import { api } from "@/lib/api";

// Where each recruiter's time goes, and what they did (routers/recruiter_activity.py):
// active time by surface (tab visible + interaction in the last 5 minutes; the
// wizard steps, Rankings, candidate review, the dashboards), searches,
// launches, decisions and how long after a PASS they were made, and the time
// spent waiting on PAIR (launch sessions). The NR id is the pseudonymous id
// every New Relic event of that recruiter carries (RecruiterAction /
// RecruiterActiveTime / browser PageActions), so NR rows can be traced back.

type ActivityRow = {
  email: string;
  recruiter_hash: string | null;
  active_minutes_total: number;
  active_minutes_by_surface: Record<string, number>;
  active_minutes_by_day: Record<string, number>;
  days_active: number;
  actions: Record<string, number>;
  searches: number;
  launches: number;
  searches_per_launch: number | null;
  decisions: number;
  decision_latency_median_minutes: number | null;
  decisions_timed: number;
  waiting_on_pair_minutes: number | null;
};

type ActivityData = {
  start_date: string;
  end_date: string;
  recruiters: ActivityRow[];
  totals: {
    recruiters: number;
    active_minutes_total: number;
    searches: number;
    launches: number;
    decisions: number;
    decision_latency_median_minutes: number | null;
  };
};

const SURFACE_LABELS: Record<string, string> = {
  wizard_step_1: "Intake",
  wizard_step_2: "Publish",
  wizard_step_3: "Rubric",
  wizard_step_4: "Filters",
  wizard_step_5: "Source (Step 5)",
  rankings: "Rankings",
  candidate_detail: "Candidate review",
  assess_modal: "Assess",
  candidates: "Candidates",
  jobs_dashboard: "Jobs dashboard",
  launch_report: "Launch report",
  analytics: "Admin analytics",
  recruiter_analytics: "Recruiter analytics",
  admin_dashboard: "PAIR dashboard",
  campaigns: "Campaigns",
};

function minutes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value > 0 && value < 1) return "<1m";
  if (value < 60) return `${Math.round(value)}m`;
  const h = Math.floor(value / 60);
  const m = Math.round(value % 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function RecruiterActivityPanel({
  range,
  teamId,
  enabled,
  today,
}: {
  /** null = the page's "All time": activity shows the last 90 days. */
  range: { start: string; end: string } | null;
  teamId: string | null;
  enabled: boolean;
  today: string;
}) {
  const start = range?.start ?? addDays(today, -89);
  const end = range?.end ?? today;
  const key = `${start}|${end}|${teamId ?? ""}`;
  // The last answer and the request it answered: loading is derived (a newer
  // key than the answer), so the effect never sets state synchronously.
  const [result, setResult] = useState<{ key: string; data: ActivityData | null; error: string | null } | null>(
    null,
  );
  const seq = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    const mine = ++seq.current;
    api.recruiterAnalytics
      .activity({ startDate: start, endDate: end, teamId })
      .then((res) => {
        if (mine !== seq.current) return;
        setResult(
          res?.status === "success" && res.data
            ? { key, data: res.data as ActivityData, error: null }
            : { key, data: null, error: "Couldn't load recruiter activity." },
        );
      })
      .catch(() => {
        if (mine === seq.current) setResult({ key, data: null, error: "Couldn't load recruiter activity." });
      });
  }, [enabled, start, end, teamId, key]);

  const data = result?.data ?? null;
  const error = result?.key === key ? result.error : null;
  const loading = enabled && result?.key !== key;
  const rows = data?.recruiters ?? [];
  return (
    <section className="mt-8 bg-white border border-slate-200 rounded-xl shadow-sm">
      <header className="flex flex-wrap items-center justify-between gap-2 px-5 py-4 border-b border-slate-100">
        <div className="flex items-center gap-2">
          <Activity className="w-4 h-4 text-indigo-600" />
          <h2 className="text-[15px] font-bold text-slate-800">Activity — where time goes</h2>
        </div>
        <p className="text-[12px] text-slate-500">
          {start} → {end} (ET){range === null && " · last 90 days"} · active time = tab open and in use (5-min idle)
        </p>
      </header>
      {error ? (
        <p className="px-5 py-8 text-center text-[13px] text-slate-400">{error}</p>
      ) : loading && !data ? (
        <p className="px-5 py-8 text-center text-[13px] text-slate-400">Loading activity…</p>
      ) : rows.length === 0 ? (
        <p className="px-5 py-8 text-center text-[13px] text-slate-400">
          No recruiter activity recorded in these dates yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-500 whitespace-nowrap">
              <tr>
                <th className="text-left px-5 py-2.5 font-bold">Recruiter</th>
                <th className="text-right px-3 py-2.5 font-bold" title="Active time in PAIR in these dates">Active</th>
                <th className="text-left px-3 py-2.5 font-bold">Where the time went</th>
                <th className="text-right px-3 py-2.5 font-bold">Days</th>
                <th className="text-right px-3 py-2.5 font-bold" title="Step 5 searches run / Source & Launch clicks">
                  Searches / Launches
                </th>
                <th className="text-right px-3 py-2.5 font-bold" title="Submit / Reject / Unreachable decisions">
                  Decisions
                </th>
                <th
                  className="text-right px-3 py-2.5 font-bold"
                  title="Median time from a candidate passing PAIR to the recruiter's decision on them"
                >
                  Pass → Decision
                </th>
                <th
                  className="text-right px-3 py-2.5 font-bold"
                  title="Server launch sessions: click → first interview, minus time the recruiter spent in review / needs-contact"
                >
                  Waiting on PAIR
                </th>
                <th className="text-left px-3 py-2.5 font-bold" title="The id this recruiter's New Relic events carry">
                  NR id
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const total = r.active_minutes_total || 0;
                const surfaces = Object.entries(r.active_minutes_by_surface).slice(0, 4);
                return (
                  <tr key={r.email} className="border-t border-slate-100 align-top">
                    <td className="px-5 py-3 font-semibold text-slate-800">{r.email}</td>
                    <td className="px-3 py-3 text-right tabular-nums whitespace-nowrap">
                      <span className="inline-flex items-center gap-1 font-semibold text-slate-800">
                        <Clock className="w-3.5 h-3.5 text-slate-400" />
                        {minutes(total)}
                      </span>
                    </td>
                    <td className="px-3 py-3 min-w-[260px]">
                      {total > 0 && (
                        <div className="flex h-2 w-full overflow-hidden rounded-full bg-slate-100 mb-1.5">
                          {surfaces.map(([s, m], i) => (
                            <div
                              key={s}
                              title={`${SURFACE_LABELS[s] ?? s}: ${minutes(m)}`}
                              style={{ width: `${(m / total) * 100}%` }}
                              className={["bg-indigo-500", "bg-sky-400", "bg-emerald-400", "bg-amber-400"][i] ?? "bg-slate-300"}
                            />
                          ))}
                        </div>
                      )}
                      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
                        {surfaces.map(([s, m]) => (
                          <span key={s}>
                            {SURFACE_LABELS[s] ?? s} <span className="font-semibold text-slate-700">{minutes(m)}</span>
                          </span>
                        ))}
                      </div>
                    </td>
                    <td className="px-3 py-3 text-right tabular-nums">{r.days_active}</td>
                    <td className="px-3 py-3 text-right tabular-nums whitespace-nowrap">
                      {r.searches} / {r.launches}
                      {r.searches_per_launch !== null && (
                        <div className="text-[11px] text-slate-400">{r.searches_per_launch} per launch</div>
                      )}
                    </td>
                    <td className="px-3 py-3 text-right tabular-nums">{r.decisions}</td>
                    <td className="px-3 py-3 text-right tabular-nums" title={`${r.decisions_timed} timed decisions`}>
                      {minutes(r.decision_latency_median_minutes)}
                    </td>
                    <td className="px-3 py-3 text-right tabular-nums">{minutes(r.waiting_on_pair_minutes)}</td>
                    <td className="px-3 py-3 font-mono text-[11px] text-slate-500">{r.recruiter_hash ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
