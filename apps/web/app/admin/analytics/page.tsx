"use client";

import { useEffect, useState } from "react";
import { 
  LayoutDashboard, 
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
  Download
} from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

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

interface AnalyticsData {
  overview: AnalyticsOverview;
  candidates_by_status: Record<string, number>;
  jobs_by_customer: CustomerJob[];
  top_recruiters: RecruiterStat[];
  candidates_by_source?: CandidateSource[];
  warning?: string;
}

export default function AdminAnalyticsPage() {
  const { isAdmin, isLoading: isRoleLoading, email, role } = useUserRole();
  const [data, setData] = useState<AnalyticsData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);

  const fetchAnalytics = async (refresh = false) => {
    if (refresh) setIsRefreshing(true);
    else setIsLoading(true);
    setError(null);
    try {
      const res = await api.adminAnalytics.get();
      if (res && res.status === "success" && res.data) {
        setData(res.data);
      } else {
        setError(res?.message || "Failed to load analytics data.");
      }
    } catch (err: any) {
      console.error("Error loading analytics:", err);
      setError(err?.message || "Access denied or server error loading analytics.");
    } finally {
      setIsLoading(false);
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    if (!isRoleLoading && isAdmin) {
      fetchAnalytics();
    }
  }, [isRoleLoading, isAdmin]);

  if (isRoleLoading) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="h-8 w-8 animate-spin rounded-full border-[3px] border-primary border-t-transparent" />
          <p className="text-[13px] font-medium text-slate-500">Verifying administrative access...</p>
        </div>
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="flex h-[80vh] w-full items-center justify-center p-6">
        <Card className="max-w-md w-full text-center p-8 border-slate-200 bg-white shadow-sm rounded-xl">
          <div className="mx-auto w-12 h-12 rounded-full bg-red-50 border border-red-100 flex items-center justify-center mb-4 text-red-600">
            <ShieldAlert className="w-6 h-6" />
          </div>
          <h1 className="text-[20px] font-bold text-slate-900 mb-2">Access Restricted</h1>
          <p className="text-slate-500 text-[13px] mb-6 leading-relaxed">
            You are signed in as <span className="font-semibold text-slate-800">{email || "a Recruiter"}</span> with the <span className="uppercase font-semibold text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">{role}</span> role. System analytics are restricted to Administrators only.
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

  const totalCandidates = Object.values(data?.candidates_by_status || {}).reduce((a, b) => a + b, 0) || 1;
  const maxJobCount = Math.max(...(data?.jobs_by_customer?.map((c) => c.job_count) || [1]), 1);

  // Standard pipeline funnel stages aligning with candidate ranking page statuses
  const pipelineStages = [
    { key: "launched", label: "Launched Candidates", aliases: ["launched", "launched to client", "launched_to_client", "submitted"] },
    { key: "pending", label: "Pending Candidates", aliases: ["pending", "unreviewed", "review", "sourced", "new", ""] },
    { key: "in_progress", label: "In-Progress Candidates", aliases: ["in progress", "in_progress", "screening", "contacted", "outreach", "replied", "interview", "interviewed", "interview completed", "interview_completed"] },
    { key: "failed", label: "Failed Candidates", aliases: ["fail", "failed", "rejected", "reject", "disqualified", "declined"], color: "bg-rose-500" },
    { key: "passed", label: "Passed Candidates", aliases: ["pass", "passed", "qualified", "shortlisted", "hired", "offer accepted", "selected", "interested", "complete", "completed"], color: "bg-emerald-600" },
  ];

  const getStageCount = (stage: { key: string; aliases: string[] }) => {
    if (!data?.candidates_by_status) return 0;
    let count = 0;
    const keysToMatch = stage.aliases.map((k) => k.toLowerCase().replace(/[-_]/g, " ").trim());

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
      s.aliases.map((k) => k.toLowerCase().replace(/[-_]/g, " ").trim()).includes(normalized)
    );
  };

  const exportToCSV = () => {
    if (!data) return;
    const lines = [
      "Hoonr Curate - Executive Analytics Report",
      `Generated: ${new Date().toLocaleDateString()}`,
      "",
      "--- SYSTEM KPI OVERVIEW ---",
      `Active Monitored Jobs,${data.overview.total_monitored_jobs}`,
      `Sourced Candidates,${data.overview.total_sourced_candidates}`,
      `Active Recruiters,${data.overview.total_active_recruiters}`,
      `Archived Jobs,${data.overview.total_archived_jobs}`,
      "",
      "--- CANDIDATE PIPELINE FUNNEL ---",
      "Stage,Count,Percentage",
      ...pipelineStages.map((stage) => {
        const count = getStageCount(stage);
        const pct = Math.round((count / totalCandidates) * 100);
        return `"${stage.label}",${count},${pct}%`;
      }),
      "",
      "--- TOP CLIENT VOLUME ---",
      "Rank,Customer Name,Active Jobs",
      ...(data.jobs_by_customer || []).map((c, idx) => `#${idx + 1},"${c.customer_name}",${c.job_count}`),
      "",
      "--- RECRUITER LEADERBOARD ---",
      "Rank,Recruiter Email,Active Jobs,Candidate Volume",
      ...(data.top_recruiters || []).map((r, idx) => `#${idx + 1},"${r.email}",${r.active_jobs},${r.total_candidates}`),
    ];

    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.setAttribute("download", `hoonr-curate-analytics-${new Date().toISOString().split("T")[0]}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Page Header aligning with Jobs Portfolio */}
      <div className="flex items-center justify-between mt-2">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Admin Analytics</h1>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
            System Overview
          </span>
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
            disabled={isLoading || isRefreshing}
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
          >
            <RefreshCw className={`h-4 w-4 text-slate-500 ${isRefreshing ? "animate-spin text-primary" : ""}`} />
            {isRefreshing ? "Refreshing..." : "Refresh"}
          </Button>
        </div>
      </div>

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
            <span className="text-[13px] font-semibold text-slate-500">Active Monitored Jobs</span>
            <div className="w-8 h-8 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-600">
              <Briefcase className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">{overview.total_monitored_jobs}</div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">Live Portfolios</div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">Sourced Candidates</span>
            <div className="w-8 h-8 rounded-lg bg-emerald-50 border border-emerald-100 flex items-center justify-center text-emerald-600">
              <Users className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">{overview.total_sourced_candidates.toLocaleString()}</div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">Total Talent Pool</div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">Active Recruiters</span>
            <div className="w-8 h-8 rounded-lg bg-violet-50 border border-violet-100 flex items-center justify-center text-violet-600">
              <UserCheck className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">{overview.total_active_recruiters}</div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">Assigned Team Members</div>
          </div>
        </div>

        <div className="bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col justify-between">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-slate-500">Archived Jobs</span>
            <div className="w-8 h-8 rounded-lg bg-amber-50 border border-amber-100 flex items-center justify-center text-amber-600">
              <Archive className="w-4 h-4" />
            </div>
          </div>
          <div className="mt-3">
            {isLoading ? (
              <div className="h-8 w-16 bg-slate-100 animate-pulse rounded" />
            ) : (
              <div className="text-[28px] font-bold text-slate-900 leading-none">{overview.total_archived_jobs}</div>
            )}
            <div className="text-[12px] text-slate-400 mt-1.5 font-medium">Completed Jobs</div>
          </div>
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
              <p className="text-[12px] text-slate-500 mt-0.5">Sourcing and outreach conversion distribution across all jobs</p>
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
                {pipelineStages.map((stage) => {
                  const count = getStageCount(stage);
                  const percentage = Math.round((count / totalCandidates) * 100);
                  const barColor = (stage as any).color || "bg-primary";

                  return (
                    <div key={stage.key} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <span className="font-semibold text-slate-700">{stage.label}</span>
                        <div className="flex items-center gap-2 font-mono">
                          <span className="font-bold text-slate-900">{count.toLocaleString()}</span>
                          <span className="text-[12px] text-slate-400 w-10 text-right">({percentage}%)</span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div 
                          className={`h-full ${barColor} rounded-full transition-all duration-500`}
                          style={{ width: `${count > 0 ? Math.max(percentage, 3) : 0}%` }}
                        />
                      </div>
                    </div>
                  );
                })}

                {/* Catch-all for any other custom statuses */}
                {Object.entries(data?.candidates_by_status || {}).map(([status, count]) => {
                  if (isStageMatched(status)) return null;
                  const percentage = Math.round((count / totalCandidates) * 100);
                  return (
                    <div key={status} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <span className="font-semibold text-slate-700 capitalize">{status}</span>
                        <div className="flex items-center gap-2 font-mono">
                          <span className="font-bold text-slate-900">{count.toLocaleString()}</span>
                          <span className="text-[12px] text-slate-400 w-10 text-right">({percentage}%)</span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div 
                          className="h-full bg-slate-500 rounded-full transition-all duration-500"
                          style={{ width: `${count > 0 ? Math.max(percentage, 3) : 0}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
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
              <p className="text-[12px] text-slate-500 mt-0.5">Top 5 most active accounts by jobs</p>
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
                  const widthPct = Math.round((cust.job_count / maxJobCount) * 100);

                  return (
                    <div key={cust.customer_name} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <div className="flex items-center gap-2 truncate pr-3">
                          <span className="text-[12px] font-mono font-semibold text-slate-400">#{idx + 1}</span>
                          <span className="font-semibold text-slate-800 truncate">{cust.customer_name}</span>
                        </div>
                        <div className="flex items-center gap-1 font-mono shrink-0">
                          <span className="font-bold text-slate-900">{cust.job_count}</span>
                          <span className="text-[12px] text-slate-400">{cust.job_count === 1 ? "Job" : "Jobs"}</span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div 
                          className="h-full bg-teal-500 rounded-full transition-all duration-500"
                          style={{ width: `${cust.job_count > 0 ? Math.max(widthPct, 3) : 0}%` }}
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
              <p className="text-[12px] text-slate-500 mt-0.5">Distribution of candidate profiles by ingestion channel</p>
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
                  const maxSrc = Math.max(...(data?.candidates_by_source?.map((s) => s.count) || [1]), 1);
                  const percentage = Math.round((srcItem.count / (overview.total_sourced_candidates || 1)) * 100);
                  const widthPct = Math.round((srcItem.count / maxSrc) * 100);

                  return (
                    <div key={srcItem.source} className="space-y-2">
                      <div className="flex items-center justify-between text-[13px]">
                        <span className="font-semibold text-slate-700">{srcItem.source}</span>
                        <div className="flex items-center gap-2 font-mono">
                          <span className="font-bold text-slate-900">{srcItem.count.toLocaleString()}</span>
                          <span className="text-[12px] text-slate-400 w-10 text-right">({percentage}%)</span>
                        </div>
                      </div>
                      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
                        <div 
                          className="h-full bg-violet-600 rounded-full transition-all duration-500"
                          style={{ width: `${srcItem.count > 0 ? Math.max(widthPct, 3) : 0}%` }}
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
              <p className="text-[12px] text-slate-500 mt-0.5">Overall qualification benchmark</p>
            </div>
          </div>

          <div className="p-6 flex-1 flex flex-col justify-between space-y-4 my-auto">
            <div className="p-3.5 rounded-xl bg-emerald-50/60 border border-emerald-100/80 flex items-center justify-between">
              <div>
                <div className="text-[12px] font-semibold text-emerald-800 uppercase tracking-wider">Pass Rate Ratio</div>
                <div className="text-[26px] font-bold text-emerald-950 mt-1">
                  {(() => {
                    const passed = getStageCount({ key: "passed", aliases: ["pass", "passed", "qualified", "shortlisted", "hired", "offer accepted", "selected", "interested", "complete", "completed"] });
                    const failed = getStageCount({ key: "failed", aliases: ["fail", "failed", "rejected", "reject", "disqualified", "declined"] });
                    const totalEvaluated = passed + failed;
                    return totalEvaluated > 0 ? `${Math.round((passed / totalEvaluated) * 100)}%` : "0%";
                  })()}
                </div>
                <div className="text-[12px] text-emerald-700 mt-0.5 font-medium">of evaluated candidates shortlisted</div>
              </div>
              <div className="w-10 h-10 rounded-full bg-emerald-100 flex items-center justify-center text-emerald-600 font-bold">
                ✓
              </div>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200/80 flex items-center justify-between">
              <div>
                <div className="text-[12px] font-semibold text-slate-500 uppercase tracking-wider">Avg. Pool Density</div>
                <div className="text-[26px] font-bold text-slate-900 mt-1">
                  {overview.total_monitored_jobs > 0
                    ? Math.round(overview.total_sourced_candidates / overview.total_monitored_jobs)
                    : overview.total_sourced_candidates}
                </div>
                <div className="text-[12px] text-slate-500 mt-0.5 font-medium">candidates sourced per active job</div>
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
            <p className="text-[12px] text-slate-500 mt-0.5">Team members ranked by active jobs and candidate volume</p>
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
                <th className="py-3 px-6 text-center">Total Sourced Candidates</th>
                <th className="py-3 px-6 text-right">Activity Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[14px]">
              {isLoading ? (
                [1, 2, 3].map((i) => (
                  <tr key={i}>
                    <td className="py-4 px-6 text-center"><div className="h-4 w-6 bg-slate-100 animate-pulse rounded mx-auto" /></td>
                    <td className="py-4 px-6"><div className="h-4 w-48 bg-slate-100 animate-pulse rounded" /></td>
                    <td className="py-4 px-6 text-center"><div className="h-4 w-12 bg-slate-100 animate-pulse rounded mx-auto" /></td>
                    <td className="py-4 px-6 text-center"><div className="h-4 w-16 bg-slate-100 animate-pulse rounded mx-auto" /></td>
                    <td className="py-4 px-6 text-right"><div className="h-6 w-20 bg-slate-100 animate-pulse rounded ml-auto" /></td>
                  </tr>
                ))
              ) : (data?.top_recruiters || []).length === 0 ? (
                <tr>
                  <td colSpan={5} className="py-12 text-center text-slate-400 text-[13px]">
                    No active recruiter assignments found.
                  </td>
                </tr>
              ) : (
                (data?.top_recruiters || []).map((rec, idx) => {
                  return (
                    <tr key={rec.email} className="hover:bg-[#f6f8fb] transition-colors">
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
                          let badgeClass = "bg-slate-100 text-slate-600 border border-slate-200";

                          if (rec.active_jobs >= 10 || (rec.active_jobs >= 5 && rec.total_candidates >= 100)) {
                            badgeText = "Power Recruiter";
                            badgeClass = "bg-amber-50 text-amber-700 border border-amber-200";
                          } else if (rec.active_jobs >= 6) {
                            badgeText = "High Activity";
                            badgeClass = "bg-emerald-50 text-emerald-700 border border-emerald-200";
                          } else if (rec.active_jobs >= 3) {
                            badgeText = "Active";
                            badgeClass = "bg-indigo-50 text-indigo-700 border border-indigo-200";
                          } else if (rec.active_jobs >= 1) {
                            badgeText = "Light Activity";
                            badgeClass = "bg-sky-50 text-sky-700 border border-sky-200";
                          } else {
                            badgeText = "Inactive";
                            badgeClass = "bg-slate-100 text-slate-500 border border-slate-200";
                          }

                          return (
                            <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${badgeClass}`}>
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
    </div>
  );
}
