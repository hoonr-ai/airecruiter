"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Search, Plus, FileText, ArrowUpDown, ArrowUp, ArrowDown, MoreVertical, Link as LinkIcon, AlertTriangle, Archive, Edit3 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Users } from "lucide-react";
import { API_BASE, authFetch } from "@/lib/api";

interface Job {
  id: string;
  jobdiva_id?: string;
  title: string;
  customer_name: string;
  screening_level?: string;
  recruiterEmails: string[];
  status: string;
  location: string;
  priority: string;
  programDuration: string;
  maxAllowedSubmittals: string;
  pairStatus: string;
  pairLaunchedAt: string | null;
  createdAt: string;
  candidatesLaunched: number;
  completeSubmissions: number;
  passSubmissions: number;
  pairSubmits: number;
  pairExternalSubs: number;
  feedbackCompleted: number;
  timeToFirstPass: number;
}

type SortField = keyof Job;
type SortDirection = "asc" | "desc";

const SCREENING_LEVEL_STYLES: Record<string, string> = {
  "L0.5": "bg-gray-100 text-gray-600 border-gray-300",
  "L1":   "bg-blue-50 text-blue-700 border-blue-200",
  "L1.5": "bg-teal-50 text-teal-700 border-teal-200",
  "L2":   "bg-purple-50 text-purple-700 border-purple-200",
};

// How often the dashboard silently re-pulls /jobs/monitored. Metrics like
// FEEDBACK COMPLETED and PAIR EXTERNAL SUBS change from background work
// (a teammate's feedback, the JobDiva sync) — without this the page only
// ever showed the numbers as of the moment it was opened. The backend
// serves this from a 30s cache warmed every 25s, so polling at 30s adds
// essentially no DB load.
const DASHBOARD_REFRESH_MS = 30_000;

const sortJobsBy = (list: Job[], field: SortField, direction: SortDirection): Job[] =>
  [...list].sort((a, b) => {
    const aVal = a[field as keyof Job];
    const bVal = b[field as keyof Job];

    // Handle null values and "—" placeholders — always sort to the bottom regardless of direction
    if (aVal === null && bVal === null) return 0;
    if (aVal === null || aVal === "—") return 1;
    if (bVal === null || bVal === "—") return -1;

    // Arrays (recruiterEmails) — sort by first element alphabetically
    const aStr = Array.isArray(aVal) ? (aVal[0] || "") : aVal;
    const bStr = Array.isArray(bVal) ? (bVal[0] || "") : bVal;

    if (typeof aStr === "string" && typeof bStr === "string") {
      return direction === "asc" ? aStr.localeCompare(bStr) : bStr.localeCompare(aStr);
    }

    if (typeof aVal === "number" && typeof bVal === "number") {
      return direction === "asc" ? aVal - bVal : bVal - aVal;
    }

    return 0;
  });

const matchesSearch = (job: Job, query: string): boolean =>
  Object.values(job).some((value) =>
    (value?.toString() || "").toLowerCase().includes(query.toLowerCase())
  );

export default function DashboardPage() {
  const router = useRouter();
  const [searchQuery, setSearchQuery] = useState("");
  const [sortField, setSortField] = useState<keyof Job>("createdAt");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [allJobs, setAllJobs] = useState<Job[]>([]);
  const [filteredJobs, setFilteredJobs] = useState<Job[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  // True when the currently-displayed jobs came from a previous fetch
  // (the latest attempt timed out or errored). The list still renders so
  // the user never sees a blank dashboard during a backend slow spike.
  const [isStale, setIsStale] = useState(false);
  // True when the fetch failed AND we have no prior data to show — i.e.
  // first-load failure or 200-empty fallback from the backend. Renders an
  // explicit "Couldn't load — retry" banner instead of leaving the page
  // silently blank.
  const [loadFailed, setLoadFailed] = useState(false);

  // Archive dialog state
  const [archiveDialogOpen, setArchiveDialogOpen] = useState(false);
  const [jobToArchive, setJobToArchive] = useState<Job | null>(null);
  const [isArchiving, setIsArchiving] = useState(false);
  const [archiveReason, setArchiveReason] = useState("");
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  // Unarchive dialog state
  const [unarchiveDialogOpen, setUnarchiveDialogOpen] = useState(false);
  const [jobToUnarchive, setJobToUnarchive] = useState<Job | null>(null);
  const [isUnarchiving, setIsUnarchiving] = useState(false);

  // Stop Job Activity dialog state — one-way action that flips an Active
  // job to Inactive and blocks new candidate launches.
  const [stopDialogOpen, setStopDialogOpen] = useState(false);
  const [jobToStop, setJobToStop] = useState<Job | null>(null);
  const [isStopping, setIsStopping] = useState(false);

  // Edit Job Setup (create a new editable version) dialog state
  const [editVersionDialogOpen, setEditVersionDialogOpen] = useState(false);
  const [jobToEditVersion, setJobToEditVersion] = useState<Job | null>(null);
  const [isCreatingVersion, setIsCreatingVersion] = useState(false);

  // Tab state
  const [activeTab, setActiveTab] = useState<"active" | "archived">("active");

  // The poll below runs outside React's render cycle, so it can't read
  // state directly without capturing a stale closure. Mirror the view
  // state into refs so a background refresh re-applies the search and sort
  // the user currently has, instead of resetting the table under them.
  const viewStateRef = useRef({ searchQuery, sortField, sortDirection });
  viewStateRef.current = { searchQuery, sortField, sortDirection };

  useEffect(() => {
    fetchJobs();
  }, [activeTab]);

  // Keep the metrics live. FEEDBACK COMPLETED and PAIR EXTERNAL SUBS move
  // from work that happens off this page — another recruiter submitting
  // feedback, the JobDiva submittal sync — so an open dashboard would
  // otherwise sit on the numbers it loaded with until someone reloaded.
  // Silent (no spinner), paused while the tab is hidden, and refreshed
  // immediately on returning to the tab so a backgrounded dashboard is
  // never showing minutes-old data.
  useEffect(() => {
    const refreshIfVisible = () => {
      if (document.visibilityState === "visible") fetchJobs({ background: true });
    };
    const interval = setInterval(refreshIfVisible, DASHBOARD_REFRESH_MS);
    document.addEventListener("visibilitychange", refreshIfVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshIfVisible);
    };
  }, [activeTab]);

  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  const fetchJobs = async ({ background = false }: { background?: boolean } = {}) => {
    // A background refresh must not swap the table for a skeleton — the
    // user is reading it.
    if (!background) setIsLoading(true);
    // Bound the fetch at 15s. Backend statement_timeout is 5s, so the live
    // query either succeeds, fails-fast, or falls through to the 200-empty
    // path well inside this window. The previous 8s budget raced the
    // server's own 8s statement_timeout — under contention the server would
    // return a 200-empty around the 8-9s mark but the client had already
    // aborted, so the dashboard rendered blank with no signal to the user.
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    try {
      const includeArchived = activeTab === "archived";
      const response = await authFetch(
        `${API_BASE}/jobs/monitored?include_archived=${includeArchived}`,
        { signal: controller.signal },
      );
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(`${response.status} /jobs/monitored${text ? `: ${text}` : ""}`);
      }
      const data = await response.json();

      // A silent refresh must never blank a table the user is reading. The
      // backend answers 200 with `source: "error"` and an empty job map
      // when its DB/cache is degraded; on a foreground load that
      // (correctly) shows the "couldn't load" banner, but applying it to a
      // background tick would wipe a populated list under the user for a
      // condition that usually clears on the next poll. Keep what we have
      // and just mark it stale.
      if (background && data?.source === "error") {
        setIsStale(true);
        return;
      }

      // Sort explicitly by createdAt DESC after mapping — do not rely on
      // Object.entries order, since JS engines iterate numeric-string keys
      // in ascending numeric order (not insertion order), which would break
      // the backend's created_at DESC ordering for numeric jobdiva IDs.
      const jobs: Job[] = Object.entries(data.jobs || {}).map(([id, details]: [string, any]) => {
        const status = details.status || "Open";
        const procStatus = details.processing_status || "pending";

        const pairStatus = details.pair_status || "Unpublished";

        return {
          id,
          jobdiva_id: details.jobdiva_id || "",
          title: details.enhanced_title || details.title || "—",
          customer_name: details.customer_name || "—",
          screening_level: details.screening_level || "—",
          recruiterEmails: Array.isArray(details.recruiter_emails) ? details.recruiter_emails : [],
          status: status || "—",
          location: [
            details.city ? `${details.city}, ${details.state || ""}`.trim() : "",
            details.zip_code || ""
          ].filter(Boolean).join(" ") || "—",
          priority: (!details.priority || details.priority === "[null]") ? "—" : details.priority,
          programDuration: (!details.program_duration && !details.duration) || details.program_duration === "[null]" || details.duration === "[null]"
            ? "—"
            : details.program_duration || details.duration,
          maxAllowedSubmittals: (!details.max_allowed_submittals || details.max_allowed_submittals === "[null]" || Number.isNaN(Number.parseInt(details.max_allowed_submittals, 10)))
            ? "—"
            : Number.parseInt(details.max_allowed_submittals, 10).toString(),
          pairStatus: pairStatus,
          pairLaunchedAt: details.pair_launched_at || null,
          createdAt: details.created_at || "",
          candidatesLaunched: details.candidates_launched || 0,
          completeSubmissions: details.complete_submissions || 0,
          passSubmissions: details.pass_submissions || 0,
          pairSubmits: details.pair_submits || 0,
          pairExternalSubs: details.pair_external_subs || 0,
          feedbackCompleted: details.feedback_completed || 0,
          timeToFirstPass: parseFloat(details.time_to_first_pass) || 0,
        };
      }).sort((a, b) => b.createdAt.localeCompare(a.createdAt));

      setAllJobs(jobs);
      // Re-apply whatever the user has on screen. A foreground load starts
      // clean (newest-first, no query); a background refresh must land the
      // new numbers without clearing their search box or re-sorting the
      // column they picked.
      if (background) {
        const { searchQuery: q, sortField: f, sortDirection: d } = viewStateRef.current;
        const visible = q ? jobs.filter(job => matchesSearch(job, q)) : jobs;
        setFilteredJobs(f === "createdAt" && d === "desc" ? visible : sortJobsBy(visible, f, d));
      } else {
        setFilteredJobs(jobs);
      }
      setIsStale(false);
      // Backend's 200-empty fallback signals via `source: "error"`. Treat
      // the same as a fetch failure UX-wise — empty list + retry banner —
      // since the underlying DB/cache failed even though HTTP succeeded.
      setLoadFailed(data?.source === "error");
    } catch (error) {
      console.error("Error fetching jobs:", error);
      // Keep showing whatever we had before. Mark the list as stale so the
      // user sees something is off, instead of a blank "No job results" pane.
      if (allJobs.length > 0) {
        setIsStale(true);
      } else {
        setLoadFailed(true);
      }
    } finally {
      clearTimeout(timeoutId);
      if (!background) setIsLoading(false);
    }
  };

  const handleSort = (field: SortField) => {
    // 3rd click on same column (currently DESC) → reset to default newest-first
    if (sortField === field && sortDirection === "desc") {
      setSortField("createdAt");
      setSortDirection("desc");
      const base = [...allJobs].sort((a, b) => b.createdAt.localeCompare(a.createdAt));
      setFilteredJobs(searchQuery ? base.filter(job => matchesSearch(job, searchQuery)) : base);
      return;
    }
    const newDirection = sortField === field && sortDirection === "asc" ? "desc" : "asc";
    setSortField(field);
    setSortDirection(newDirection);

    setFilteredJobs(sortJobsBy(filteredJobs, field, newDirection));
  };

  const handleSearch = (query: string) => {
    setSearchQuery(query);
    setFilteredJobs(allJobs.filter(job => matchesSearch(job, query)));
  };

  const handleExport = () => {
    const headers = [
      "JobDiva ID",
      "Job Title",
      "Customer Name",
      "Recruiter Emails",
      "Location / Zip",
      "Priority",
      "Program Duration",
      "Max Allowed Submittals",
      "Job Status",
      "PAIR Status",
      "Screening Level",
      "First PAIR Launch",
      "Candidates Launched",
      "Complete Submissions",
      "Pass Submissions",
      "PAIR Submits",
      "PAIR External Subs",
      "Feedback Completed",
      "Time to First Pass",
    ];
    const escapeCSV = (val: any) => {
      const str = val === null || val === undefined ? "" : String(val);
      return str.includes(",") || str.includes('"') || str.includes("\n")
        ? `"${str.replace(/"/g, '""')}"`
        : str;
    };
    const rows = filteredJobs.map(job => [
      escapeCSV(job.jobdiva_id || job.id),
      escapeCSV(job.title),
      escapeCSV(job.customer_name),
      escapeCSV((job.recruiterEmails || []).join("; ")),
      escapeCSV(job.location),
      escapeCSV(job.priority),
      escapeCSV(job.programDuration),
      escapeCSV(job.maxAllowedSubmittals),
      escapeCSV(job.status),
      escapeCSV(job.pairStatus),
      escapeCSV(job.screening_level === "—" ? "" : job.screening_level),
      escapeCSV(job.pairLaunchedAt ? new Date(job.pairLaunchedAt).toLocaleString("en-US", { timeZone: "America/New_York", timeZoneName: "short" }) : "—"),
      escapeCSV(job.candidatesLaunched),
      escapeCSV(job.completeSubmissions),
      escapeCSV(job.passSubmissions),
      escapeCSV(job.pairSubmits),
      escapeCSV(job.pairExternalSubs),
      escapeCSV(job.feedbackCompleted),
      escapeCSV(job.timeToFirstPass ? `${job.timeToFirstPass} mins` : "—"),
    ].join(","));
    const csv = [headers.join(","), ...rows].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "jobs_export.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  const getStatusColor = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'open') return 'bg-[#dcfce7] text-[#166534]'; // Custom soft green
    if (s === 'completed') return 'bg-[#ffedd5] text-[#c2410c]'; // Custom soft orange
    if (s === 'cancelled' || s === 'closed') return 'bg-[#fee2e2] text-[#b91c1c]'; // Custom soft red
    return 'bg-slate-100 text-slate-700';
  };

  const getPairStatusColor = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'active') return 'bg-[#dcfce7] text-[#166534]';
    if (s === 'inactive' || s === 'paused') return 'bg-[#fee2e2] text-[#b91c1c]';
    if (s === 'unpublished') return 'bg-[#f1f5f9] text-[#475569]'; // Custom gray
    if (s === 'archived') return 'bg-[#e2e8f0] text-[#64748b]'; // Slate gray for archived
    return 'bg-slate-100 text-slate-700';
  };

  const highlight = (text: string | number | null | undefined): React.ReactNode => {
    if (!searchQuery || text === null || text === undefined) return text ?? "";
    const str = String(text);
    const idx = str.toLowerCase().indexOf(searchQuery.toLowerCase());
    if (idx === -1) return str;
    return (
      <>
        {str.slice(0, idx)}
        <mark className="bg-yellow-200 text-yellow-900 rounded-[3px] px-0.5 not-italic">{str.slice(idx, idx + searchQuery.length)}</mark>
        {str.slice(idx + searchQuery.length)}
      </>
    );
  };

  const SortableHeader = ({ field, children, className = "" }: { field: keyof Job; children: React.ReactNode; className?: string }) => {
    const isActive = sortField === field;
    return (
      <th className={`px-6 py-4 text-center text-[12px] font-bold uppercase tracking-wider border-b border-slate-100 whitespace-nowrap transition-colors ${isActive ? "text-[#4f46e5] bg-indigo-50" : "text-slate-500 bg-[#fcfdfd]"} ${className}`}>
        <div className="flex items-center justify-center gap-1.5 cursor-pointer hover:text-[#4f46e5] transition-colors" onClick={() => handleSort(field)}>
          {children}
          {isActive
            ? sortDirection === "asc"
              ? <ArrowUp className="h-3.5 w-3.5" />
              : <ArrowDown className="h-3.5 w-3.5" />
            : <ArrowUpDown className="h-3.5 w-3.5 text-slate-400" />
          }
        </div>
      </th>
    );
  };

  return (
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Page Header */}
      <div className="flex items-center justify-between mt-2">
        <div className="flex items-center gap-3">
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Jobs Portfolio</h1>
          <span className="inline-flex items-center rounded-full bg-slate-100 px-2.5 py-0.5 text-[12px] font-semibold text-slate-500 ring-1 ring-inset ring-slate-200">
            {filteredJobs.length === allJobs.length
              ? `${allJobs.length} jobs`
              : `${filteredJobs.length} of ${allJobs.length} jobs`}
          </span>
        </div>

        {/* Tabs */}
        <div className="flex bg-slate-100 p-1 rounded-lg">
          <button
            onClick={() => setActiveTab("active")}
            className={`px-4 py-2 rounded-md text-[13px] font-medium transition-all ${activeTab === "active"
              ? "bg-white text-slate-900 shadow-sm"
              : "text-slate-500 hover:text-slate-700"
              }`}
          >
            Active Jobs
          </button>
          <button
            onClick={() => setActiveTab("archived")}
            className={`px-4 py-2 rounded-md text-[13px] font-medium transition-all ${activeTab === "archived"
              ? "bg-white text-slate-900 shadow-sm"
              : "text-slate-500 hover:text-slate-700"
              }`}
          >
            Archived Jobs
          </button>
        </div>
      </div>

      {isStale && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[13px] text-amber-800">
          <AlertTriangle className="h-4 w-4" />
          <span>
            Couldn’t refresh — showing last loaded data.{" "}
            <button
              type="button"
              className="font-semibold underline decoration-amber-400 underline-offset-2 hover:text-amber-900"
              onClick={() => fetchJobs()}
            >
              Retry
            </button>
          </span>
        </div>
      )}

      {loadFailed && !isStale && (
        <div className="mt-2 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-[13px] text-red-800">
          <AlertTriangle className="h-4 w-4" />
          <span>
            Couldn’t load jobs.{" "}
            <button
              type="button"
              className="font-semibold underline decoration-red-400 underline-offset-2 hover:text-red-900"
              onClick={() => fetchJobs()}
            >
              Retry
            </button>
          </span>
        </div>
      )}

      {/* Controls Bar */}
      <div className="flex justify-between items-center gap-4 mt-4">
        <div className="relative w-[360px]">
          <Search className="absolute left-3.5 top-1/2 transform -translate-y-1/2 text-slate-400 h-[18px] w-[18px]" />
          <Input
            placeholder="Search across all fields..."
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            className="pl-10 h-11 border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white rounded-xl text-[14px] shadow-sm"
          />
        </div>
        <div className="flex items-center gap-3">
          <Button variant="outline" className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all" onClick={handleExport}>
            <FileText className="h-4 w-4" />
            Export to Excel
          </Button>
          <Button
            variant="outline"
            disabled
            aria-disabled="true"
            title="Temporarily disabled"
            className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-400 font-semibold text-[13px] rounded-lg bg-slate-50 shadow-sm cursor-not-allowed"
          >
            <Users className="h-4 w-4" />
            All Candidates (Disabled)
          </Button>
          <Button asChild className="flex items-center gap-2 h-10 px-5 bg-[#4f46e5] hover:bg-[#4338ca] text-white font-semibold text-[13px] rounded-lg shadow-sm transition-all active:scale-95 border-none">
            <Link href="/jobs/new">
              <Plus className="h-4 w-4" />
              New Job
            </Link>
          </Button>
        </div>
      </div>

      {/* Avg Time to First Pass stat — jobs launched since most recent Monday */}
      {activeTab === "active" && !isLoading && (() => {
        // Dynamically compute start-of-most-recent-Monday exactly in America/New_York time
        const now = new Date();
        let dt = new Date(now.getTime());
        
        // 1. Walk back by 24h steps until the Eastern time is a Monday
        while (new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' }).format(dt) !== 'Mon') {
          dt = new Date(dt.getTime() - 24 * 60 * 60 * 1000);
        }
        
        // 2. Walk back by 1h steps until it flips to Sunday
        while (new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', weekday: 'short' }).format(dt) === 'Mon') {
          dt = new Date(dt.getTime() - 60 * 60 * 1000);
        }
        
        // 3. Step forward 1 hour to reach exactly 00:00 on Monday in EST/EDT
        const monday = new Date(dt.getTime() + 60 * 60 * 1000);
        const mondayLabel = monday.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "America/New_York" });

        const sinceMondayJobs = allJobs.filter(j =>
          j.pairLaunchedAt &&
          new Date(j.pairLaunchedAt) >= monday &&
          typeof j.timeToFirstPass === "number" &&
          j.timeToFirstPass > 0
        );
        const avg = sinceMondayJobs.length > 0
          ? sinceMondayJobs.reduce((sum, j) => sum + j.timeToFirstPass, 0) / sinceMondayJobs.length
          : null;
        return (
          <div className="flex items-center justify-between px-5 py-3 bg-indigo-50 border border-indigo-100 rounded-xl">
            <div className="flex items-center gap-2 text-[13.5px]">
              <span className="font-semibold text-indigo-700">⚡ Average Time to First Pass</span>
              <span className="text-indigo-300">·</span>
              <span className="text-slate-500">Jobs launched since {mondayLabel}</span>
            </div>
            <div className="flex items-center gap-3">
              {avg !== null ? (
                <>
                  <span className="text-[15px] font-bold text-indigo-700">{avg.toFixed(1)} mins</span>
                  <span className="text-[12.5px] text-slate-500 font-medium">({(avg / 60).toFixed(2)} hrs)</span>
                  <span className="inline-flex items-center rounded-full bg-indigo-100 px-2.5 py-0.5 text-[12px] font-semibold text-indigo-600">
                    {sinceMondayJobs.length} job{sinceMondayJobs.length !== 1 ? "s" : ""} with a first pass
                  </span>
                </>
              ) : (
                <span className="text-[13px] text-slate-400 italic">No first-pass data yet this week</span>
              )}
            </div>
          </div>
        );
      })()}

      {/* Jobs Table */}
      <div className="bg-white rounded-2xl shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)] border border-slate-200 overflow-hidden mt-2">
        <div className="overflow-x-auto overflow-y-auto" style={{ maxHeight: 'calc(100vh - 240px)' }}>
          <table className="min-w-full divide-y divide-slate-100">
            <thead className="bg-[#fcfdfd] sticky top-0 z-20 shadow-sm">
              <tr>
                <SortableHeader field="id">JOBDIVA ID</SortableHeader>
                <SortableHeader field="title" className="sticky left-0 bg-[#fcfdfd] z-30 shadow-[5px_0_15px_-5px_rgba(0,0,0,0.03)] border-r border-slate-100/50 min-w-[220px]">JOB TITLE</SortableHeader>
                <SortableHeader field="customer_name">CUSTOMER NAME</SortableHeader>
                <SortableHeader field="recruiterEmails">RECRUITER EMAILS</SortableHeader>
                <SortableHeader field="location">LOCATION / ZIP</SortableHeader>
                <SortableHeader field="priority">PRIORITY</SortableHeader>
                <SortableHeader field="programDuration">PROGRAM DURATION</SortableHeader>
                <SortableHeader field="maxAllowedSubmittals">MAX ALLOWED SUBMITTALS</SortableHeader>
                <SortableHeader field="status">JOB STATUS</SortableHeader>
                <SortableHeader field="pairStatus">PAIR STATUS</SortableHeader>
                <SortableHeader field="screening_level">SCREENING LEVEL</SortableHeader>
                <SortableHeader field="pairLaunchedAt">FIRST PAIR LAUNCH</SortableHeader>
                <SortableHeader field="candidatesLaunched">CANDIDATES LAUNCHED</SortableHeader>
                <SortableHeader field="completeSubmissions">COMPLETE SUBMISSIONS</SortableHeader>
                <SortableHeader field="passSubmissions">PASS SUBMISSIONS</SortableHeader>
                <SortableHeader field="pairSubmits">PAIR SUBMITS</SortableHeader>
                <SortableHeader field="pairExternalSubs">PAIR EXTERNAL SUBS</SortableHeader>
                <SortableHeader field="feedbackCompleted">FEEDBACK COMPLETED</SortableHeader>
                <SortableHeader field="timeToFirstPass">TIME TO FIRST PASS</SortableHeader>
                <th className="px-6 py-4 text-center text-[12px] font-bold uppercase tracking-wider text-slate-500 border-b border-l border-slate-100/50 sticky right-0 bg-[#fcfdfd] z-30 shadow-[-10px_0_15px_-5px_rgba(0,0,0,0.03)] whitespace-nowrap">
                  ACTIONS
                </th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-slate-100">
              {filteredJobs.length > 0 ? filteredJobs.map((job) => (
                <tr key={job.id} className="hover:bg-slate-50/70 transition-colors group">
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-[#4f46e5] text-center">
                    <Link
                      prefetch={false}
                      href={`/jobs/${job.jobdiva_id || job.id}/rankings`}
                      className="flex items-center justify-center gap-1.5 hover:underline decoration-[#4f46e5]/40 underline-offset-4"
                    >
                      {job.jobdiva_id || job.id}
                      {job.pairStatus !== 'Unpublished' && <LinkIcon className="h-3 w-3 text-[#4f46e5]/70" />}
                    </Link>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap sticky left-0 bg-white group-hover:bg-[#f6f8fb] transition-colors border-r border-slate-100/50 z-10 shadow-[5px_0_15px_-5px_rgba(0,0,0,0.03)] text-center min-w-[220px]">
                    <div className="flex items-center justify-center gap-2">
                      <span className="text-[13.5px] font-semibold text-slate-900">{highlight(job.title)}</span>
                      {job.pairStatus === 'Unpublished' && (
                        <span className="text-[11px] text-slate-400 font-medium">(draft)</span>
                      )}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {highlight(job.customer_name)}
                  </td>
                  <td className="px-6 py-4 text-[13.5px] font-medium text-slate-700 text-center">
                    {(job.recruiterEmails || []).length === 0 ? (
                      "—"
                    ) : (
                      <div className="flex flex-col gap-1 items-center">
                        {job.recruiterEmails.map((email, i) => (
                          <span key={i}>{highlight(email)}</span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {highlight(job.location)}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {highlight(job.priority)}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {highlight(job.programDuration)}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.maxAllowedSubmittals}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-center">
                    <div className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11.5px] font-bold tracking-wide ${getStatusColor(job.status)}`}>
                      {highlight(job.status)}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-center">
                    <div className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11.5px] font-bold tracking-wide ${getPairStatusColor(job.pairStatus)}`}>
                      {highlight(job.pairStatus)}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-center">
                    {job.screening_level && job.screening_level !== "—" ? (
                      <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium border ${SCREENING_LEVEL_STYLES[job.screening_level] ?? "bg-gray-100 text-gray-600 border-gray-300"}`}>
                        {highlight(job.screening_level)}
                      </span>
                    ) : <span className="text-slate-400 text-xs">—</span>}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.pairLaunchedAt ? (
                      <div className="flex flex-col gap-0.5 items-center" title={new Date(job.pairLaunchedAt).toLocaleString("en-US", { timeZone: "America/New_York", timeZoneName: "short" })}>
                        <span>{new Date(job.pairLaunchedAt).toLocaleDateString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", year: "numeric" })}</span>
                        <span className="text-[12px]">{new Date(job.pairLaunchedAt).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", timeZoneName: "short" })}</span>
                      </div>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.candidatesLaunched}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.completeSubmissions}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.passSubmissions}
                  </td>
                  {/* PAIR SUBMITS = what PAIR recorded (recruiter pressed
                      Submit; mirrored to JobDiva as a "PAIR Submit -
                      Externally Submitted" note). PAIR EXTERNAL SUBS = what
                      JobDiva confirms (a submittal to the job's contact for
                      a candidate carrying PAIR Candidates=Pass). They are
                      deliberately separate — a gap between them is a real
                      signal, not a rounding difference. */}
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.pairSubmits}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.pairExternalSubs}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.feedbackCompleted}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700 text-center">
                    {job.timeToFirstPass ? `${job.timeToFirstPass} mins` : "—"}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-center text-slate-400 sticky right-0 bg-white group-hover:bg-[#f6f8fb] transition-colors border-l border-slate-100/50 z-10 shadow-[-10px_0_15px_-5px_rgba(0,0,0,0.03)]">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" className="h-8 w-8 p-0 rounded-full hover:bg-slate-200 transition-colors">
                          <MoreVertical className="h-4 w-4 text-slate-500" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="rounded-xl border-slate-200 font-medium text-[13px] shadow-lg">
                        {/* Per-status primary actions. The wizard at /jobs/new
                            handles all three modes (edit / source / view) via
                            ?mode and ?step query params. */}
                        {activeTab !== "archived" && job.pairStatus === 'Unpublished' && (
                          <DropdownMenuItem asChild className="cursor-pointer bg-primary/5 text-primary font-bold">
                            <Link prefetch={false} href={`/jobs/new?jobId=${job.jobdiva_id || job.id}`} className="w-full">
                              Resume Job Setup
                            </Link>
                          </DropdownMenuItem>
                        )}
                        {activeTab !== "archived" && job.pairStatus === 'Active' && (
                          <>
                            <DropdownMenuItem asChild className="cursor-pointer bg-primary/5 text-primary font-bold">
                              <Link prefetch={false} href={`/jobs/new?jobId=${job.jobdiva_id || job.id}&mode=source&step=5`} className="w-full">
                                Source Candidates
                              </Link>
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              className="cursor-pointer"
                              onClick={() => {
                                setJobToEditVersion(job);
                                setEditVersionDialogOpen(true);
                              }}
                            >
                              <Edit3 className="h-4 w-4 mr-2" />
                              Edit Job Setup
                            </DropdownMenuItem>
                            <DropdownMenuItem
                              className="text-amber-600 focus:text-amber-700 cursor-pointer"
                              onClick={() => {
                                setJobToStop(job);
                                setStopDialogOpen(true);
                              }}
                            >
                              Stop Job Activity
                            </DropdownMenuItem>
                          </>
                        )}
                        {activeTab !== "archived" && job.pairStatus === 'Inactive' && (
                          <DropdownMenuItem asChild className="cursor-pointer">
                            <Link prefetch={false} href={`/jobs/new?jobId=${job.jobdiva_id || job.id}&mode=view`} className="w-full">
                              View Job Setup
                            </Link>
                          </DropdownMenuItem>
                        )}
                        {activeTab === "archived" ? (
                          <DropdownMenuItem
                            className="text-green-600 focus:text-green-700 cursor-pointer"
                            onClick={() => {
                              setJobToUnarchive(job);
                              setUnarchiveDialogOpen(true);
                            }}
                          >
                            Unarchive Job
                          </DropdownMenuItem>
                        ) : (
                          <DropdownMenuItem
                            className="text-red-600 focus:text-red-700 cursor-pointer"
                            onClick={() => {
                              setJobToArchive(job);
                              setArchiveDialogOpen(true);
                            }}
                          >
                            Archive Job
                          </DropdownMenuItem>
                        )}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </td>
                </tr>
              )) : isLoading ? (
                // Skeleton rows while the initial fetch is in flight. Prevents the
                // "No job results" empty state from flashing for ~500ms on cold load.
                Array.from({ length: 5 }).map((_, idx) => (
                  <tr key={`skeleton-${idx}`} className="border-b border-slate-100">
                    <td className="px-6 py-4"><Skeleton className="h-4 w-20 bg-slate-100" /></td>
                    <td className="px-6 py-4 sticky left-0 bg-white border-r border-slate-100/50 z-10">
                      <Skeleton className="h-4 w-48 bg-slate-100" />
                    </td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-32 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-28 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-16 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-20 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-5 w-20 rounded-full bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-5 w-24 rounded-full bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-10 bg-slate-100" /></td>
                    <td className="px-6 py-4"><Skeleton className="h-4 w-16 bg-slate-100" /></td>
                    <td className="px-6 py-4 sticky right-0 bg-white border-l border-slate-100/50 z-10">
                      <Skeleton className="h-6 w-6 rounded-full bg-slate-100 mx-auto" />
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={17} className="py-12 px-6">
                    <div className="flex flex-col items-center justify-center gap-3" style={{ minWidth: '600px' }}>
                      <div className="w-12 h-12 rounded-full bg-slate-100 flex items-center justify-center">
                        <Search className="w-6 h-6 text-slate-400" />
                      </div>
                      <p className="text-[15px] font-medium text-slate-500">No job results to display</p>
                      <p className="text-[13px] text-slate-400">Try adjusting your search or create a new job</p>
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Archive Confirmation Dialog */}
      <Dialog open={archiveDialogOpen} onOpenChange={setArchiveDialogOpen}>
        <DialogContent className="sm:max-w-[425px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-red-600">
              <AlertTriangle className="h-5 w-5" />
              Archive Job
            </DialogTitle>
            <DialogDescription>
              Are you sure you want to archive this job? This action will hide the job from the active jobs list.
            </DialogDescription>
          </DialogHeader>
          {jobToArchive && (
            <div className="py-4 space-y-4">
              <div className="bg-slate-50 p-3 rounded-lg border border-slate-200">
                <p className="font-semibold text-slate-900">{jobToArchive.title}</p>
                <p className="text-sm text-slate-500">ID: {jobToArchive.jobdiva_id || jobToArchive.id}</p>
                <p className="text-sm text-slate-500">Customer: {jobToArchive.customer_name}</p>
              </div>
              <div>
                <label className="block text-sm font-medium text-slate-700 mb-2">
                  Archive Reason <span className="text-slate-400">(optional)</span>
                </label>
                <textarea
                  value={archiveReason}
                  onChange={(e) => setArchiveReason(e.target.value)}
                  placeholder="e.g., Position filled, Job cancelled, On hold..."
                  className="w-full px-3 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-red-500/20 focus:border-red-500 resize-none"
                  rows={3}
                />
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setArchiveDialogOpen(false);
                setJobToArchive(null);
                setArchiveReason("");
              }}
              disabled={isArchiving}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={async () => {
                if (!jobToArchive) return;
                setIsArchiving(true);
                try {
                  const response = await authFetch(
                    `${API_BASE}/jobs/${jobToArchive.jobdiva_id || jobToArchive.id}/archive`,
                    {
                      method: "PUT",
                      headers: {
                        "Content-Type": "application/json",
                      },
                      body: JSON.stringify({ reason: archiveReason.trim() || undefined }),
                    }
                  );
                  if (response.ok) {
                    setToast({ message: "Job archived successfully", type: "success" });
                    // Remove the archived job from the list
                    setAllJobs(prev => prev.filter(j => j.id !== jobToArchive.id));
                    setFilteredJobs(prev => prev.filter(j => j.id !== jobToArchive.id));
                  } else {
                    const errorData = await response.json().catch(() => ({ detail: "Unknown error" }));
                    console.error("Archive error:", errorData);
                    setToast({ message: errorData.detail || "Failed to archive job", type: "error" });
                  }
                } catch (error) {
                  console.error("Archive exception:", error);
                  setToast({ message: "Failed to archive job", type: "error" });
                } finally {
                  setIsArchiving(false);
                  setArchiveDialogOpen(false);
                  setJobToArchive(null);
                  setArchiveReason("");
                }
              }}
              disabled={isArchiving}
            >
              {isArchiving ? "Archiving..." : "Archive Job"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Stop Job Activity Confirmation Dialog */}
      <Dialog open={stopDialogOpen} onOpenChange={setStopDialogOpen}>
        <DialogContent className="sm:max-w-[460px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-amber-600">
              <AlertTriangle className="h-5 w-5" />
              Stop Job Activity
            </DialogTitle>
            <DialogDescription>
              This will stop new outreach to candidates for this job. The job will move to Inactive status. <strong>This cannot be undone.</strong>
            </DialogDescription>
          </DialogHeader>
          {jobToStop && (
            <div className="py-4">
              <div className="bg-slate-50 p-3 rounded-lg border border-slate-200">
                <p className="font-semibold text-slate-900">{jobToStop.title}</p>
                <p className="text-sm text-slate-500">ID: {jobToStop.jobdiva_id || jobToStop.id}</p>
                <p className="text-sm text-slate-500">Customer: {jobToStop.customer_name}</p>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setStopDialogOpen(false);
                setJobToStop(null);
              }}
              disabled={isStopping}
            >
              Cancel
            </Button>
            <Button
              className="bg-amber-600 hover:bg-amber-700 text-white"
              onClick={async () => {
                if (!jobToStop) return;
                setIsStopping(true);
                try {
                  const response = await authFetch(
                    `${API_BASE}/jobs/${jobToStop.jobdiva_id || jobToStop.id}/stop-activity`,
                    {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                    }
                  );
                  if (response.ok) {
                    setToast({ message: "Job activity stopped", type: "success" });
                    setAllJobs(prev => prev.map(j => j.id === jobToStop.id ? { ...j, pairStatus: 'Inactive' } : j));
                    setFilteredJobs(prev => prev.map(j => j.id === jobToStop.id ? { ...j, pairStatus: 'Inactive' } : j));
                  } else {
                    const errorData = await response.json().catch(() => ({ detail: "Unknown error" }));
                    console.error("Stop activity error:", errorData);
                    setToast({ message: errorData.detail || "Failed to stop job activity", type: "error" });
                  }
                } catch (error) {
                  console.error("Stop activity exception:", error);
                  setToast({ message: "Failed to stop job activity", type: "error" });
                } finally {
                  setIsStopping(false);
                  setStopDialogOpen(false);
                  setJobToStop(null);
                }
              }}
              disabled={isStopping}
            >
              {isStopping ? "Stopping..." : "Stop Activity"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Job Setup (new version) Confirmation Dialog */}
      <Dialog open={editVersionDialogOpen} onOpenChange={setEditVersionDialogOpen}>
        <DialogContent className="sm:max-w-[480px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-primary">
              <Edit3 className="h-5 w-5" />
              Edit Job Setup
            </DialogTitle>
            <DialogDescription>
              This creates a new editable version of the job (e.g. <strong>v2</strong>) and reopens the setup wizard from Step 1.
              The current version&apos;s candidates and rank list stay intact — the new version sources and launches PAIR fresh.
              The job&apos;s rubric, filters, questions and JD are copied so you can edit from a complete copy.
            </DialogDescription>
          </DialogHeader>
          {jobToEditVersion && (
            <div className="py-4">
              <div className="bg-slate-50 p-3 rounded-lg border border-slate-200">
                <p className="font-semibold text-slate-900">{jobToEditVersion.title}</p>
                <p className="text-sm text-slate-500">ID: {jobToEditVersion.jobdiva_id || jobToEditVersion.id}</p>
                <p className="text-sm text-slate-500">Customer: {jobToEditVersion.customer_name}</p>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setEditVersionDialogOpen(false);
                setJobToEditVersion(null);
              }}
              disabled={isCreatingVersion}
            >
              Cancel
            </Button>
            <Button
              className="bg-primary hover:bg-primary/90 text-white"
              onClick={async () => {
                if (!jobToEditVersion) return;
                setIsCreatingVersion(true);
                try {
                  const ref = jobToEditVersion.jobdiva_id || jobToEditVersion.id;
                  const response = await authFetch(
                    `${API_BASE}/jobs/${encodeURIComponent(ref)}/new-version`,
                    {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                    }
                  );
                  if (response.ok) {
                    const data = await response.json();
                    const newRef = data?.new_job_id;
                    if (newRef) {
                      setEditVersionDialogOpen(false);
                      setJobToEditVersion(null);
                      router.push(`/jobs/new?jobId=${encodeURIComponent(newRef)}&step=1`);
                      return;
                    }
                    setToast({ message: "Version created but no id returned", type: "error" });
                  } else {
                    const errorData = await response.json().catch(() => ({ detail: "Unknown error" }));
                    console.error("Create version error:", errorData);
                    setToast({ message: errorData.detail || "Failed to create new version", type: "error" });
                  }
                } catch (error) {
                  console.error("Create version exception:", error);
                  setToast({ message: "Failed to create new version", type: "error" });
                } finally {
                  setIsCreatingVersion(false);
                }
              }}
              disabled={isCreatingVersion}
            >
              {isCreatingVersion ? "Creating..." : "Create & Edit v2"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Unarchive Confirmation Dialog */}
      <Dialog open={unarchiveDialogOpen} onOpenChange={setUnarchiveDialogOpen}>
        <DialogContent className="sm:max-w-[425px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-green-600">
              <Archive className="h-5 w-5" />
              Unarchive Job
            </DialogTitle>
            <DialogDescription>
              Are you sure you want to unarchive this job? This will restore the job to the active jobs list.
            </DialogDescription>
          </DialogHeader>
          {jobToUnarchive && (
            <div className="py-4">
              <div className="bg-slate-50 p-3 rounded-lg border border-slate-200">
                <p className="font-semibold text-slate-900">{jobToUnarchive.title}</p>
                <p className="text-sm text-slate-500">ID: {jobToUnarchive.jobdiva_id || jobToUnarchive.id}</p>
                <p className="text-sm text-slate-500">Customer: {jobToUnarchive.customer_name}</p>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setUnarchiveDialogOpen(false);
                setJobToUnarchive(null);
              }}
              disabled={isUnarchiving}
            >
              Cancel
            </Button>
            <Button
              className="bg-green-600 hover:bg-green-700 text-white"
              onClick={async () => {
                if (!jobToUnarchive) return;
                setIsUnarchiving(true);
                try {
                  const response = await authFetch(
                    `${API_BASE}/jobs/${jobToUnarchive.jobdiva_id || jobToUnarchive.id}/unarchive`,
                    {
                      method: "PUT",
                      headers: {
                        "Content-Type": "application/json",
                      },
                    }
                  );
                  if (response.ok) {
                    setToast({ message: "Job unarchived successfully", type: "success" });
                    fetchJobs();
                  } else {
                    const errorData = await response.json().catch(() => ({ detail: "Unknown error" }));
                    setToast({ message: errorData.detail || "Failed to unarchive job", type: "error" });
                  }
                } catch (error) {
                  setToast({ message: "Failed to unarchive job", type: "error" });
                } finally {
                  setIsUnarchiving(false);
                  setUnarchiveDialogOpen(false);
                  setJobToUnarchive(null);
                }
              }}
              disabled={isUnarchiving}
            >
              {isUnarchiving ? "Unarchiving..." : "Unarchive Job"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Toast Notification */}
      {toast && (
        <div className={`fixed top-4 right-4 z-50 rounded-lg p-4 text-white ${toast.type === 'success' ? 'bg-green-500' : 'bg-red-500'
          }`}>
          {toast.message}
        </div>
      )}
    </div>
  );
}
