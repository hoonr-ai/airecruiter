"use client";

// PAIR Dashboard: Overview, Funnel & Speed, Productivity.
//
// The numbers are computed server-side (services/pair_dashboard.py); this page
// only picks the scope and renders. Every definition is in the Data Dictionary
// (components/dashboard/data-dictionary.tsx). Overview and Productivity are
// activity-based over the selected period; Funnel & Speed follows the
// requirements posted in its own Job Posted range over their whole life.
//
// Scope: admins see every team or pick a Recruiting Manager (a team on the
// Teams page); team leads are pinned to their own team by the backend.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { ArrowLeft, BarChart3, Filter, Gauge, RefreshCw, ShieldAlert, TriangleAlert, UsersRound } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { formatEasternDateTime, todayEastern } from "@/lib/date";
import {
  DASHBOARD_PRESETS,
  customRangeError,
  formatRangeLabel,
  presetRange,
  type DashboardPreset,
  type DateRange,
} from "@/lib/dashboard";
import { DataDictionaryButton } from "@/components/dashboard/data-dictionary";
import { FilterBar } from "@/components/dashboard/filter-bar";
import { FunnelTab } from "@/components/dashboard/funnel-tab";
import { OverviewTab } from "@/components/dashboard/overview-tab";
import { ProductivityTab } from "@/components/dashboard/productivity-tab";
import {
  EMPTY_FILTERS,
  type DashboardFilterState,
  type DashboardOptions,
  type FunnelData,
  type JobDivaCoverage,
  type OverviewData,
  type ProductivityData,
} from "@/components/dashboard/types";

type Tab = "overview" | "funnel" | "productivity";

const TABS: { key: Tab; label: string; icon: typeof Gauge }[] = [
  { key: "overview", label: "Overview", icon: Gauge },
  { key: "funnel", label: "Funnel & Speed", icon: Filter },
  { key: "productivity", label: "Productivity", icon: BarChart3 },
];

const TAB_HEADINGS: Record<Tab, { title: string; sub: string }> = {
  overview: {
    title: "PAIR Performance Overview",
    sub: "All counts reflect activity within the selected period, whichever requirement it happened on.",
  },
  funnel: {
    title: "Candidate Funnel & Speed to Pipeline",
    sub: "Stage by stage, over the whole life of the PAIR requirements posted in the range.",
  },
  productivity: {
    title: "Recruiter Productivity",
    sub: "Weekly averages per recruiter across PAIR and non-PAIR activity, for every requirement.",
  },
};

function initialTab(): Tab {
  if (typeof window === "undefined") return "overview";
  try {
    const value = new URLSearchParams(window.location.search).get("tab");
    return value === "funnel" || value === "productivity" ? value : "overview";
  } catch {
    return "overview";
  }
}

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.message.slice(err.message.indexOf(": ") + 2);
    try {
      const detail = JSON.parse(body)?.detail;
      if (typeof detail === "string" && detail) return detail;
    } catch {
      // not JSON
    }
    if (err.status === 403) return "Access denied. Admin or team lead access is required.";
    if (err.status === 404) return "That team no longer exists.";
  }
  return err instanceof Error && err.message ? err.message : "Failed to load the dashboard.";
}

function coverageNote(coverage: JobDivaCoverage | undefined): string | null {
  if (!coverage) return null;
  if (!coverage.available) {
    return "JobDiva data (all Pyramid requirements, submittals, interviews and starts) is still being copied. The first sync starts a few minutes after a deploy and then runs every 30 minutes; PAIR's own numbers are already live.";
  }
  if (coverage.activities_available && !coverage.activities_complete && coverage.activities_from) {
    return `JobDiva history is still loading: submittals, interviews and starts are complete from ${coverage.activities_from.slice(0, 10)} onward.`;
  }
  return null;
}

export default function PairDashboardPage() {
  const { isAdmin, isTeamLead, teamName, isLoading: isRoleLoading, email, role } = useUserRole();
  const canView = isAdmin || isTeamLead;

  const [tab, setTab] = useState<Tab>(initialTab);
  const [teamId, setTeamId] = useState<string | null>(null);
  const [filters, setFilters] = useState<DashboardFilterState>(EMPTY_FILTERS);

  const [preset, setPreset] = useState<DashboardPreset>("7d");
  const [range, setRange] = useState<DateRange | null>(() => presetRange("7d", todayEastern()));
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [rangeError, setRangeError] = useState<string | null>(null);

  const [postedDraft, setPostedDraft] = useState<DateRange>(() => ({
    start: `${todayEastern().slice(0, 4)}-01-01`,
    end: todayEastern(),
  }));
  const [posted, setPosted] = useState<DateRange>(postedDraft);
  const [postedError, setPostedError] = useState<string | null>(null);

  const [options, setOptions] = useState<DashboardOptions | null>(null);
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [funnel, setFunnel] = useState<FunnelData | null>(null);
  const [productivity, setProductivity] = useState<ProductivityData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only the newest request may write state: a slow answer for an old scope
  // must never land on top of a newer one.
  const requestSeq = useRef(0);

  const scopedTeamId = isAdmin ? teamId : null;

  const query = useMemo(
    () => ({
      teamId: scopedTeamId,
      job: filters.job || null,
      priority: filters.priority || null,
      client: filters.client || null,
      jdStatus: filters.jdStatus || null,
      pairStatus: filters.pairStatus || null,
      vertical: filters.vertical || null,
    }),
    [scopedTeamId, filters],
  );

  const load = useCallback(
    async (refresh = false) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(null);
      try {
        if (tab === "overview") {
          const res = await api.dashboard.overview({ ...query, startDate: range?.start, endDate: range?.end, refresh });
          if (seq === requestSeq.current) setOverview((res?.data as OverviewData) ?? null);
        } else if (tab === "funnel") {
          const res = await api.dashboard.funnel({ ...query, postedFrom: posted.start, postedTo: posted.end, refresh });
          if (seq === requestSeq.current) setFunnel((res?.data as FunnelData) ?? null);
        } else {
          const res = await api.dashboard.productivity({
            teamId: scopedTeamId,
            priority: query.priority,
            client: query.client,
            vertical: query.vertical,
            startDate: range?.start,
            endDate: range?.end,
            refresh,
          });
          if (seq === requestSeq.current) setProductivity((res?.data as ProductivityData) ?? null);
        }
      } catch (err) {
        if (seq === requestSeq.current) {
          console.error("Error loading the PAIR dashboard:", err);
          setError(describeError(err));
        }
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [tab, query, range, posted, scopedTeamId],
  );

  useEffect(() => {
    if (!isRoleLoading && canView) load();
  }, [isRoleLoading, canView, load]);

  useEffect(() => {
    if (isRoleLoading || !canView) return;
    let cancelled = false;
    api.dashboard
      .options({ teamId: scopedTeamId })
      .then((res) => {
        if (!cancelled && res?.data) setOptions(res.data as DashboardOptions);
      })
      .catch((err) => console.error("Error loading dashboard filters:", err));
    return () => {
      cancelled = true;
    };
  }, [isRoleLoading, canView, scopedTeamId]);

  // A different team has a different job list: drop a job filter it can't have.
  useEffect(() => {
    if (!options || !filters.job) return;
    if (!options.jobs.some((j) => j.job_id === filters.job)) setFilters((f) => ({ ...f, job: "" }));
  }, [options, filters.job]);

  const chooseTab = (next: Tab) => {
    setTab(next);
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("tab", next);
      window.history.replaceState(null, "", url.toString());
    } catch {
      // the URL is a convenience only
    }
  };

  const choosePreset = (key: DashboardPreset) => {
    setRangeError(null);
    setPreset(key);
    if (key === "custom") {
      const seed = range ?? presetRange("30d", todayEastern());
      setCustomStart(seed?.start ?? "");
      setCustomEnd(seed?.end ?? "");
      return;
    }
    setRange(presetRange(key, todayEastern()));
  };

  const applyCustomRange = () => {
    const problem = customRangeError(customStart, customEnd);
    if (problem) return setRangeError(problem);
    setRangeError(null);
    setRange({ start: customStart, end: customEnd });
  };

  const applyPosted = () => {
    const problem = customRangeError(postedDraft.start, postedDraft.end);
    if (problem) return setPostedError(problem);
    setPostedError(null);
    setPosted(postedDraft);
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
        <Card className="w-full max-w-md rounded-xl border-slate-200 bg-white p-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full border border-red-100 bg-red-50 text-red-600">
            <ShieldAlert className="h-6 w-6" />
          </div>
          <h1 className="mb-2 text-[20px] font-bold text-slate-900">Access Restricted</h1>
          <p className="mb-6 text-[13px] leading-relaxed text-slate-500">
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the{" "}
            <span className="rounded bg-slate-100 px-2 py-0.5 text-[11px] font-semibold uppercase text-slate-700">
              {role.replace("_", " ")}
            </span>{" "}
            role. The Dashboard is visible to Administrators and Team Leads only.
          </p>
          <Link href="/">
            <Button className="h-10 w-full gap-2 rounded-lg bg-slate-900 text-[13px] font-semibold text-white hover:bg-slate-800">
              <ArrowLeft className="h-4 w-4" />
              Return to Jobs Dashboard
            </Button>
          </Link>
        </Card>
      </div>
    );
  }

  const current = tab === "overview" ? overview : tab === "funnel" ? funnel : productivity;
  const coverage = current?.jobdiva ?? options?.jobdiva;
  const note = coverageNote(coverage);
  const scope = current?.team_scope ?? options?.team_scope ?? null;
  const scopeName = scope?.team_name || (!isAdmin ? teamName : null) || null;
  const heading = TAB_HEADINGS[tab];
  const warnings = tab === "overview" ? overview?.warnings ?? [] : [];

  return (
    <div className="mx-auto max-w-[1440px] space-y-5 pb-10">
      {/* Header */}
      <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-[28px] font-bold tracking-tight text-slate-900">Dashboard</h1>
            {scopeName ? (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-2.5 py-0.5 text-[12px] font-semibold text-indigo-700 ring-1 ring-inset ring-indigo-200">
                <UsersRound className="h-3.5 w-3.5" />
                {scopeName}
              </span>
            ) : (
              <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
                All Teams
              </span>
            )}
          </div>
          <p className="mt-1.5 text-[13px] text-slate-500">
            How PAIR is doing on the requirements it is used on, and what it does for recruiter productivity.
          </p>
        </div>
        <div className="flex items-center gap-3">
          {coverage?.last_synced_at && (
            <span className="text-[12px] font-medium tabular-nums text-slate-400" title="Last JobDiva sync">
              JobDiva synced {formatEasternDateTime(coverage.last_synced_at)} (ET)
            </span>
          )}
          <Button
            variant="outline"
            onClick={() => load(true)}
            disabled={loading}
            className="flex h-10 items-center gap-2 rounded-lg border-slate-200 bg-white px-4 text-[13px] font-semibold text-slate-700 shadow-sm hover:bg-slate-50"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${loading ? "animate-spin text-primary" : ""}`} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 border-b border-slate-200">
        {TABS.map((t) => {
          const Icon = t.icon;
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => chooseTab(t.key)}
              className={`-mb-px inline-flex items-center gap-1.5 border-b-2 px-4 py-2.5 text-[13px] font-semibold transition-colors ${
                active ? "border-primary text-primary" : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
            >
              <Icon className="h-4 w-4" />
              {t.label}
            </button>
          );
        })}
      </div>

      <FilterBar
        options={options}
        filters={filters}
        onChange={setFilters}
        isAdmin={isAdmin}
        teamId={teamId}
        onTeamChange={setTeamId}
        pinnedTeamName={scopeName}
        reqOnlyDisabled={tab === "productivity"}
      />

      {/* Period (Overview + Productivity) / Job Posted range (Funnel) */}
      {tab === "funnel" ? (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Job Posted</span>
            <input
              type="date"
              aria-label="Posted from"
              lang="en-US"
              value={postedDraft.start}
              max={postedDraft.end || todayEastern()}
              onChange={(e) => setPostedDraft((d) => ({ ...d, start: e.target.value }))}
              className="h-9 rounded-lg border border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-700 shadow-sm outline-none"
            />
            <span className="text-[12px] font-semibold text-slate-400">to</span>
            <input
              type="date"
              aria-label="Posted to"
              lang="en-US"
              value={postedDraft.end}
              min={postedDraft.start || undefined}
              max={todayEastern()}
              onChange={(e) => setPostedDraft((d) => ({ ...d, end: e.target.value }))}
              className="h-9 rounded-lg border border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-700 shadow-sm outline-none"
            />
            <Button
              variant="outline"
              onClick={applyPosted}
              disabled={loading || (postedDraft.start === posted.start && postedDraft.end === posted.end)}
              className="h-9 rounded-lg border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-700 shadow-sm hover:bg-slate-50"
            >
              Apply
            </Button>
            <span className="ml-auto text-[12.5px] font-medium text-slate-500">{formatRangeLabel(posted)}</span>
          </div>
          {postedError && <p className="text-[12.5px] font-medium text-red-600">{postedError}</p>}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex flex-wrap items-center gap-1">
              {[...DASHBOARD_PRESETS, { key: "custom" as const, label: "Custom" }].map((p) => (
                <button
                  key={p.key}
                  type="button"
                  onClick={() => choosePreset(p.key)}
                  className={`rounded-full px-3 py-1.5 text-[12.5px] font-semibold transition-colors ${
                    preset === p.key
                      ? "bg-primary text-white shadow-sm"
                      : "bg-white text-slate-600 ring-1 ring-inset ring-slate-200 hover:bg-slate-50"
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
                  lang="en-US"
                  value={customStart}
                  max={customEnd || todayEastern()}
                  onChange={(e) => {
                    setRangeError(null);
                    setCustomStart(e.target.value);
                  }}
                  className="h-9 rounded-lg border border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-700 shadow-sm outline-none"
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
                  className="h-9 rounded-lg border border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-700 shadow-sm outline-none"
                />
                <Button
                  variant="outline"
                  onClick={applyCustomRange}
                  disabled={loading}
                  className="h-9 rounded-lg border-slate-200 bg-white px-3 text-[13px] font-semibold text-slate-700 shadow-sm hover:bg-slate-50"
                >
                  Apply
                </Button>
              </div>
            )}
            <span className="ml-auto text-[12.5px] font-medium text-slate-500">{formatRangeLabel(range)}</span>
          </div>
          {rangeError && <p className="text-[12.5px] font-medium text-red-600">{rangeError}</p>}
        </div>
      )}

      {/* Section heading */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[17px] font-bold text-slate-900">{heading.title}</h2>
          <p className="mt-0.5 text-[12.5px] text-slate-500">{heading.sub}</p>
        </div>
        <DataDictionaryButton key={tab} initialTab={tab} />
      </div>

      {error && (
        <div className="flex items-center justify-between rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-[13px] text-red-800">
          <div className="flex items-center gap-2">
            <TriangleAlert className="h-4 w-4 text-red-600" />
            {error}
          </div>
          <Button variant="outline" onClick={() => load(true)} className="h-8 border-red-200 bg-white px-3 text-[12.5px] font-semibold text-red-700">
            Retry
          </Button>
        </div>
      )}
      {note && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-[12.5px] text-amber-900">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          {note}
        </div>
      )}
      {warnings.map((w) => (
        <div key={w} className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3 text-[12.5px] text-slate-700">
          <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0 text-slate-500" />
          {w}
        </div>
      ))}

      {tab === "overview" && <OverviewTab data={overview} loading={loading} />}
      {tab === "funnel" && <FunnelTab data={funnel} loading={loading} />}
      {tab === "productivity" && <ProductivityTab data={productivity} loading={loading} />}
    </div>
  );
}
