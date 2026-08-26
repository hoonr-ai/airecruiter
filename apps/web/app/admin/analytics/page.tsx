"use client";

import { useEffect, useState, useCallback } from "react";
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
  jobdiva_status: string;
  pair_status: "Active" | "Inactive" | "Unpublished";
  candidates_sourced: number;
  candidates_launched: number;
  jobdiva_submittals?: number;
  campaign_id: string | null;
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
  pair_external_subs?: number;
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
}

interface AnalyticsData {
  overview: AnalyticsOverview;
  candidates_by_status: Record<string, number>;
  jobs_by_customer: CustomerJob[];
  top_recruiters: RecruiterStat[];
  candidates_by_source?: CandidateSource[];
  jobs_timeline?: JobTimelineEntry[];
  jobs_timeline_total?: number;
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

/** ISO date/datetime → "Feb 24, 2026"; null/invalid → "—". */
const formatDate = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = /^\d{4}-\d{2}-\d{2}$/.test(iso)
    ? new Date(`${iso}T00:00:00`)
    : new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
};

/** ISO date/datetime → "Feb 24, 2026, 10:30 AM EST"; null/invalid → "—". */
const formatDateTime = (iso: string | null | undefined): string => {
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
    timeZoneName: "short"
  });
};

/** ISO Monday date → "Jun 1". */
const formatWeekLabel = (iso: string): string => {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
  });
};

/** ISO datetime → relative "2h ago" / "3d ago"; null → "—". */
const formatRelativeTime = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
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

const renderDateCell = (iso: string | null) => {
  const formatted = formatDateTime(iso);
  return formatted === "—" ? (
    <span className="text-slate-300">—</span>
  ) : (
    <span className="text-slate-700">{formatted}</span>
  );
};

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

const renderPairStatusBadge = (status: JobTimelineEntry["pair_status"]) => {
  const badgeClass =
    status === "Active"
      ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
      : status === "Unpublished"
        ? "bg-amber-50 text-amber-700 border border-amber-200"
        : "bg-slate-100 text-slate-600 border border-slate-200";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${badgeClass}`}
    >
      {status}
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
  const timelineQuery = timelineSearch.trim().toLowerCase();
  const filteredTimeline = timelineRows.filter((job) => {
    if (timelineFilter !== "All" && job.pair_status !== timelineFilter)
      return false;
    if (!timelineQuery) return true;
    return (
      job.title.toLowerCase().includes(timelineQuery) ||
      job.jobdiva_id.toLowerCase().includes(timelineQuery) ||
      job.customer_name.toLowerCase().includes(timelineQuery)
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

  const escapeCsvField = (
    value: string | number | null | undefined,
  ): string => {
    if (value === null || value === undefined) return '""';
    let str = String(value);
    if (/^[=+\-@]/.test(str)) {
      str = `'${str}`;
    }
    return `"${str.replace(/"/g, '""')}"`;
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
      `Generated: ${new Date().toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" })}`,
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
      `PAIR External Submittals,${sm.pair_external_subs ?? 0}`,
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
          escapeCsvField(j.last_submit_date),
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
      "Job Title,JobDiva Ref,Client,Posted on JobDiva,Added to PAIR,Launched on PAIR,Lag (days),PAIR Status,Candidates Sourced,Candidates Launched,JobDiva Submittals",
      ...(data.jobs_timeline || []).map((job) =>
        [
          escapeCsvField(job.title),
          escapeCsvField(job.jobdiva_id),
          escapeCsvField(job.customer_name),
          escapeCsvField(job.jobdiva_posted_on || job.posted_date_raw),
          escapeCsvField(job.added_to_curate_at),
          escapeCsvField(job.curate_launched_at),
          // Mirror the UI's lag chip: negative = unreliable posted date
          escapeCsvField(
            job.posted_to_launch_days === null ||
              job.posted_to_launch_days === undefined
              ? ""
              : job.posted_to_launch_days < 0
                ? "n/a"
                : String(job.posted_to_launch_days),
          ),
          escapeCsvField(job.pair_status),
          job.candidates_sourced,
          job.candidates_launched,
          job.jobdiva_submittals ?? 0,
        ].join(","),
      ),
      // LinkedIn accounts are global infrastructure — only exported on the
      // unscoped (all-teams) view.
      ...(data.team_scope
        ? []
        : [
            "",
            "--- LINKEDIN ACCOUNTS ---",
            "Account,Account ID,Searches,Last Used,Cooling Down Until,Last Error",
            ...(liveAccounts ?? data.linkedin_accounts ?? []).map((acc) =>
              [
                escapeCsvField(acc.account_name || "Unnamed account"),
                escapeCsvField(acc.account_id),
                acc.use_count,
                escapeCsvField(acc.last_used_at),
                escapeCsvField(acc.cooldown_until),
                escapeCsvField(acc.last_error),
              ].join(","),
            ),
          ]),
    ];

    const blob = new Blob([lines.join("\n")], {
      type: "text/csv;charset=utf-8;",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute(
      "download",
      `PAIR_Analytics_${new Date().toISOString().split("T")[0]}.csv`,
    );
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const exportTimelineToCSV = () => {
    if (!filteredTimeline || filteredTimeline.length === 0) return;

    const lines = [
      "--- JOB LAUNCH TIMELINE ---",
      "Job Title,JobDiva Ref,Client,Posted on JobDiva,Added to PAIR,Launched on PAIR,Lag (days),PAIR Status,Candidates Sourced,Candidates Launched,JobDiva Submittals",
      ...filteredTimeline.map((job) =>
        [
          escapeCsvField(job.title),
          escapeCsvField(job.jobdiva_id),
          escapeCsvField(job.customer_name),
          escapeCsvField(job.jobdiva_posted_on || job.posted_date_raw),
          escapeCsvField(formatDateTime(job.added_to_curate_at)),
          escapeCsvField(formatDateTime(job.curate_launched_at)),
          escapeCsvField(
            job.posted_to_launch_days === null ||
              job.posted_to_launch_days === undefined
              ? ""
              : job.posted_to_launch_days < 0
                ? "n/a"
                : String(job.posted_to_launch_days),
          ),
          escapeCsvField(job.pair_status),
          job.candidates_sourced,
          job.candidates_launched,
          job.jobdiva_submittals ?? 0,
        ].join(","),
      ),
    ];

    const csvContent = lines.join("\n");
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute(
      "download",
      `PAIR_Job_Timeline_${new Date().toISOString().split("T")[0]}.csv`,
    );
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
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

            <div className="rounded-xl border border-slate-200 p-4 flex flex-col justify-between">
              <div className="flex items-center justify-between">
                <span className="text-[13px] font-semibold text-slate-500">
                  PAIR External Subs
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
                  submittals matching PAIR criteria
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
                        {formatDate(job.last_submit_date)}
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
            <div className="flex flex-wrap items-center gap-3">
              <div className="relative">
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
              <div className="inline-flex items-center rounded-lg bg-slate-100 p-0.5">
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
                className="flex items-center gap-2 h-8 px-3 border-slate-200 text-slate-700 font-semibold text-[12px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all"
              >
                <Download className="h-3.5 w-3.5 text-slate-500" />
                Export CSV
              </Button>
              <span className="text-[12px] font-medium text-slate-400 whitespace-nowrap">
                Showing {filteredTimeline.length} of{" "}
                {data?.jobs_timeline_total || timelineRows.length} jobs
                {(data?.jobs_timeline_total || 0) > timelineRows.length &&
                  " (most recent 200)"}
              </span>
            </div>
          </div>
        </div>

        <div className="overflow-auto max-h-[700px] relative">
          <table className="w-full text-left border-collapse">
            <thead className="sticky top-0 z-20 bg-slate-50 shadow-[0_1px_0_0_#e2e8f0]">
              <tr className="font-bold text-slate-500 text-[12.5px]">
                <th className="py-3 px-6 sticky left-0 z-30 shadow-[1px_0_0_0_#e2e8f0]">Job</th>
                <th className="py-3 px-6">Client</th>
                <th className="py-3 px-6">Posted (JobDiva)</th>
                <th className="py-3 px-6">Added (PAIR)</th>
                <th className="py-3 px-6">Launched (PAIR)</th>
                <th className="py-3 px-6 text-center">Lag</th>
                <th className="py-3 px-6 text-center">PAIR Status</th>
                <th className="py-3 px-6 text-center">Sourced</th>
                <th className="py-3 px-6 text-center">Launched</th>
                <th className="py-3 px-6 text-center">Submittals</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-[13px]">
              {isLoading ? (
                [1, 2, 3, 4].map((i) => (
                  <tr key={i}>
                    <td className="py-4 px-6 sticky left-0 z-10 bg-white shadow-[1px_0_0_0_#e2e8f0]">
                      <div className="h-4 w-44 bg-slate-100 animate-pulse rounded" />
                      <div className="h-3 w-20 bg-slate-100 animate-pulse rounded mt-1.5" />
                    </td>
                    <td className="py-4 px-6">
                      <div className="h-4 w-24 bg-slate-100 animate-pulse rounded" />
                    </td>
                    <td className="py-4 px-6">
                      <div className="h-4 w-20 bg-slate-100 animate-pulse rounded" />
                    </td>
                    <td className="py-4 px-6">
                      <div className="h-4 w-20 bg-slate-100 animate-pulse rounded" />
                    </td>
                    <td className="py-4 px-6">
                      <div className="h-4 w-20 bg-slate-100 animate-pulse rounded" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-5 w-10 bg-slate-100 animate-pulse rounded-full mx-auto" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-5 w-16 bg-slate-100 animate-pulse rounded-full mx-auto" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-4 w-8 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-4 w-8 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                    <td className="py-4 px-6 text-center">
                      <div className="h-4 w-8 bg-slate-100 animate-pulse rounded mx-auto" />
                    </td>
                  </tr>
                ))
              ) : filteredTimeline.length === 0 ? (
                <tr>
                  <td
                    colSpan={10}
                    className="py-12 text-center text-slate-400 text-[13px]"
                  >
                    {timelineRows.length === 0
                      ? "No job timeline data available yet."
                      : "No jobs match your search or filter."}
                  </td>
                </tr>
              ) : (
                visibleTimeline.map((job) => (
                  <tr
                    key={job.job_id || job.jobdiva_id}
                    className="hover:bg-[#f6f8fb] transition-colors group"
                  >
                    <td className="py-3.5 px-6 sticky left-0 z-10 bg-white group-hover:bg-[#f6f8fb] transition-colors shadow-[1px_0_0_0_#e2e8f0]">
                      <div
                        className="font-semibold text-slate-800 max-w-[260px] truncate"
                        title={job.title}
                      >
                        {job.title}
                      </div>
                      <div className="font-mono text-xs text-slate-400 mt-0.5">
                        {job.jobdiva_id || "—"}
                      </div>
                    </td>
                    <td className="py-3.5 px-6 text-slate-600">
                      <div
                        className="max-w-[160px] truncate"
                        title={job.customer_name}
                      >
                        {job.customer_name}
                      </div>
                    </td>
                    <td className="py-3.5 px-6 whitespace-nowrap">
                      {job.jobdiva_posted_on ? (
                        <span className="text-slate-700">
                          {formatDate(job.jobdiva_posted_on)}
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
                    <td className="py-3.5 px-6 text-center">
                      {renderPairStatusBadge(job.pair_status)}
                    </td>
                    <td className="py-3.5 px-6 text-center font-bold text-slate-800">
                      {job.candidates_sourced.toLocaleString()}
                    </td>
                    <td className="py-3.5 px-6 text-center font-bold text-primary">
                      {job.candidates_launched.toLocaleString()}
                    </td>
                    <td className="py-3.5 px-6 text-center font-bold text-amber-600">
                      {(job.jobdiva_submittals ?? 0).toLocaleString()}
                    </td>
                  </tr>
                ))
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
                    let chipText = "In rotation";
                    let chipClass =
                      "bg-emerald-50 text-emerald-700 border border-emerald-200";
                    if (coolingDown) {
                      chipText = "Cooling down";
                      chipClass =
                        "bg-amber-50 text-amber-700 border border-amber-200";
                    } else if (acc.status && acc.status !== "OK") {
                      chipText = acc.status;
                      chipClass =
                        acc.status === "DETACHED"
                          ? "bg-slate-100 text-slate-600 border border-slate-200"
                          : "bg-rose-50 text-rose-700 border border-rose-200";
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
                            title={
                              coolingDown
                                ? `Until ${formatDate(acc.cooldown_until)}`
                                : undefined
                            }
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
