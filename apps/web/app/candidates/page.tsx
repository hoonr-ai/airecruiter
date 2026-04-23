"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import Link from "next/link";
import { Search, ExternalLink, User, MapPin, Briefcase, Linkedin, ShieldCheck, Mail, ArrowLeft, Eye, Zap, Filter, ChevronDown, ChevronLeft, ChevronRight, X, ArrowUp, ArrowDown, ChevronsUpDown, MessageSquare, FileText } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { EngageWizardModal } from "@/components/EngageWizardModal";
import { CandidateMessageModal } from "@/components/candidate-message-modal";
import { ResumeModal } from "@/components/ResumeModal";
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";
import { AssessModal } from "@/components/AssessModal";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { API_BASE } from "@/lib/api";
import { logger } from "@/lib/logger";
import { useEngagementFlow } from "@/hooks/use-engagement-flow";

// Sentinel value for "All X" options. Radix Select forbids "" as an item
// value, so we use this constant and map it to "no filter" in the pipeline.
const ALL = "__all__";

// Match-band filter options. Values encode the score range so the filter
// pipeline can decode without a switch.
const MATCH_BANDS: { value: string; label: string }[] = [
  { value: ALL, label: "Any match" },
  { value: "strong", label: "Strong (≥80%)" },
  { value: "good", label: "Good (60–79%)" },
  { value: "low", label: "Low (<60%)" },
  { value: "unscored", label: "Unscored" },
];

function pickMatchScore(c: any): number | null {
  const s = c?.match_score ?? c?.data?.match_score ?? c?.resume_match_percentage;
  return typeof s === "number" && !Number.isNaN(s) ? s : null;
}

// Excel-style sorting: 1st header click = asc, 2nd = desc, 3rd = clear.
type SortDir = "asc" | "desc" | null;
type SortKey =
  | "name"
  | "match"
  | "job_title"
  | "location"
  | "source"
  | "created_at";

// Sort comparator + accessors moved to the server; see SORT_KEY_TO_API below
// and `sort_key`/`sort_dir` query params on `/candidates`. `pickMatchScore`
// stays because it also drives the Match-cell N/A vs 0% decision at render.

interface Candidate {
  id: number;
  jobdiva_id: string;
  job_title?: string;
  candidate_id: string;
  source: string;
  name: string;
  headline: string;
  location: string;
  profile_url: string;
  image_url: string;
  status: string;
  created_at: string;
  resume_text?: string;
  data?: any;
}

// Sort-key mapping: FE column keys -> backend sort_key enum on /candidates.
// FE uses "job_title" because the column header reads "Applied For" which
// sorts by the joined title; backend uses "job" since the concept is the
// job, not its title verbatim.
const SORT_KEY_TO_API: Record<SortKey, string> = {
  name:       "name",
  match:      "match",
  job_title:  "job",
  location:   "location",
  source:     "source",
  created_at: "created_at",
};

// Server-side pagination. Keep the default small so initial paint is fast;
// recruiter can switch to 100/page from the dropdown. Hard cap at 200 mirrors
// the backend Query regex.
const PAGE_SIZE_OPTIONS = [25, 50, 100];
const DEFAULT_PAGE_SIZE = 50;

type FilterOptions = {
  jobs: { id: string; label: string }[];
  sources: string[];
  locations: string[];
};

export default function CandidatesPage() {
  const engagement = useEngagementFlow();
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [totalCandidates, setTotalCandidates] = useState(0);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [filterOptions, setFilterOptions] = useState<FilterOptions>({ jobs: [], sources: [], locations: [] });

  const [searchQuery, setSearchQuery] = useState("");
  // Debounced copy of searchQuery — what we actually send to the server. Gives
  // the recruiter ~250ms to finish typing before we fire another fetch.
  const [searchDebounced, setSearchDebounced] = useState("");
  const [jobFilter, setJobFilter] = useState<string>(ALL);
  const [matchFilter, setMatchFilter] = useState<string>(ALL);
  const [sourceFilter, setSourceFilter] = useState<string>(ALL);
  const [locationFilter, setLocationFilter] = useState<string>(ALL);
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [isLoading, setIsLoading] = useState(true);
  // Used by fetchCandidates to cancel older in-flight polls when a new one starts.
  const abortControllerRef = useRef<AbortController | null>(null);
  const [messageModalOpen, setMessageModalOpen] = useState(false);
  const [selectedCandidateForEmail, setSelectedCandidateForEmail] = useState<any>(null);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const [selectedCandidateForResume, setSelectedCandidateForResume] = useState<any>(null);
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [selectedCandidateForDetails, setSelectedCandidateForDetails] = useState<any>(null);

  // Engage state
  const [isEngageModalOpen, setIsEngageModalOpen] = useState(false);
  const [engagePayload, setEngagePayload] = useState<string>('');
  const [engageLoading, setEngageLoading] = useState(false);
  const [engageError, setEngageError] = useState<string | null>(null);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([]);
  const [apiResponse, setApiResponse] = useState<any>(null);
  const [candidateInterviewData, setCandidateInterviewData] = useState<{[key: string]: any}>({});

  // Assess state
  const [isAssessModalOpen, setIsAssessModalOpen] = useState(false);
  const [selectedAssessCandidate, setSelectedAssessCandidate] = useState<any>(null);
  const [selectedAssessInterviewId, setSelectedAssessInterviewId] = useState<string | null>(null);

  // Debounce searchQuery -> searchDebounced. Filter/sort/page changes fire
  // immediately (below); only free-text search pays the 250ms penalty.
  useEffect(() => {
    const t = setTimeout(() => setSearchDebounced(searchQuery.trim()), 250);
    return () => clearTimeout(t);
  }, [searchQuery]);

  // Reset to page 1 whenever the filter/search/sort query shape changes —
  // otherwise the user could end up on "page 47" of a filter that only
  // has 3 pages of results.
  useEffect(() => {
    setCurrentPage(1);
  }, [searchDebounced, jobFilter, matchFilter, sourceFilter, locationFilter, sortKey, sortDir, pageSize]);

  // Fetch filter-option dropdowns once on mount. These come from a DB-wide
  // DISTINCT query so the dropdowns list every job/source/location in the
  // table — not just whichever appear on the current page.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/candidates/filter-options`);
        const data = await r.json();
        if (cancelled) return;
        if (data.status === "success") {
          setFilterOptions({
            jobs:      Array.isArray(data.jobs)      ? data.jobs      : [],
            sources:   Array.isArray(data.sources)   ? data.sources   : [],
            locations: Array.isArray(data.locations) ? data.locations : [],
          });
        }
      } catch (e) {
        console.warn("Failed to load candidate filter-options:", e);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const fetchCandidates = useCallback(async (isBackground = false) => {
    // Cancel any previous in-flight request before starting a new one
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;

    if (!isBackground) setIsLoading(true);

    const qs = new URLSearchParams();
    qs.set("limit", String(pageSize));
    qs.set("offset", String((currentPage - 1) * pageSize));
    if (searchDebounced)            qs.set("search",     searchDebounced);
    if (jobFilter      !== ALL)     qs.set("job_id",     jobFilter);
    if (sourceFilter   !== ALL)     qs.set("source",     sourceFilter);
    if (locationFilter !== ALL)     qs.set("location",   locationFilter);
    if (matchFilter    !== ALL)     qs.set("match_band", matchFilter);
    if (sortKey && sortDir) {
      qs.set("sort_key", SORT_KEY_TO_API[sortKey]);
      qs.set("sort_dir", sortDir);
    }

    try {
      const response = await fetch(`${API_BASE}/candidates?${qs.toString()}`, {
        signal: controller.signal,
      });
      const data = await response.json();

      if (data.status === "success" && Array.isArray(data.candidates)) {
        setCandidates(data.candidates);
        setTotalCandidates(typeof data.total === "number" ? data.total : data.candidates.length);
        if (!isBackground) {
          console.log(`✅ Loaded ${data.candidates.length} candidates (page ${currentPage}, total ${data.total})`);
        }
      }
    } catch (error: any) {
      // AbortError is expected when a newer poll cancels an older in-flight request
      if (error?.name === 'AbortError') return;
      if (isBackground) {
        console.warn("Background candidate poll failed (will retry):", error);
      } else {
        console.error("Error fetching candidates:", error);
      }
    } finally {
      if (!isBackground) {
        // Small delay for aesthetic — keeps skeleton visible long enough to
        // read on fast networks. Matches previous UX.
        setTimeout(() => setIsLoading(false), 250);
      }
    }
  }, [pageSize, currentPage, searchDebounced, jobFilter, sourceFilter, locationFilter, matchFilter, sortKey, sortDir]);

  // Drive data fetch + background poll off of the filter/paging state. Any
  // dependency change refetches; a 30s interval keeps the current page
  // "live" without hammering the server. The poll is gated on the tab
  // actually being visible — there's no point refetching for a backgrounded
  // tab, and it avoids stacking request churn when the user comes back to a
  // tab that has been open all day.
  useEffect(() => {
    fetchCandidates(false);
    const POLL_MS = 30_000;
    const tick = () => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      fetchCandidates(true);
    };
    const id = setInterval(tick, POLL_MS);
    // Re-fire once the user returns to the tab so they don't stare at stale
    // data for up to 30s.
    const onVisible = () => {
      if (typeof document !== "undefined" && document.visibilityState === "visible") {
        fetchCandidates(true);
      }
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisible);
    }
    return () => {
      clearInterval(id);
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisible);
      }
      if (abortControllerRef.current) abortControllerRef.current.abort();
    };
  }, [fetchCandidates]);

  // Filter/sort/search now happen server-side. Dropdowns source their options
  // from the `/candidates/filter-options` endpoint (DB-wide distinct), and
  // `candidates` is already the current page of the filtered + sorted
  // result set from `/candidates?…`. No client-side derivation needed.
  const jobOptions      = filterOptions.jobs;
  const sourceOptions   = filterOptions.sources;
  const locationOptions = filterOptions.locations;

  const totalPages = Math.max(1, Math.ceil(totalCandidates / pageSize));
  const rangeStart = totalCandidates === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd   = Math.min(currentPage * pageSize, totalCandidates);

  const activeFilterCount =
    (jobFilter !== ALL ? 1 : 0) +
    (matchFilter !== ALL ? 1 : 0) +
    (sourceFilter !== ALL ? 1 : 0) +
    (locationFilter !== ALL ? 1 : 0);

  const clearAllFilters = () => {
    setJobFilter(ALL);
    setMatchFilter(ALL);
    setSourceFilter(ALL);
    setLocationFilter(ALL);
    setSearchQuery("");
  };

  // Excel-style three-state toggle on a column header: off -> asc -> desc -> off.
  const toggleSort = (key: SortKey) => {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir("asc");
      return;
    }
    if (sortDir === "asc") {
      setSortDir("desc");
      return;
    }
    // dir === "desc" -> clear
    setSortKey(null);
    setSortDir(null);
  };

  const renderSortIcon = (key: SortKey) => {
    if (sortKey !== key) {
      return <ChevronsUpDown className="w-3.5 h-3.5 text-slate-300 group-hover/sortable:text-slate-400 transition-colors" />;
    }
    return sortDir === "asc"
      ? <ArrowUp className="w-3.5 h-3.5 text-[#6366f1]" />
      : <ArrowDown className="w-3.5 h-3.5 text-[#6366f1]" />;
  };

  const getSourceIcon = (source: string) => {
    const s = source.toLowerCase();
    if (s.includes('linkedin')) return <Linkedin className="w-3.5 h-3.5 text-[#0A66C2]" />;
    if (s.includes('jobdiva')) return <ShieldCheck className="w-3.5 h-3.5 text-[#6366f1]" />;
    return <User className="w-3.5 h-3.5 text-slate-400" />;
  };

  const handleEmailCandidate = (candidate: any) => {
    setSelectedCandidateForEmail(candidate);
    setMessageModalOpen(true);
  };

  const fetchCandidateResume = async (candidateId: string) => {
    try {
      const response = await fetch(`${API_BASE}/candidates/${candidateId}/resume`);
      if (!response.ok) {
        // F3e: differentiate fetch failure from a genuine empty resume so
        // the recruiter sees an actionable message rather than a silent
        // "not available" fallback that hides real outages.
        logger.error("candidates.resume.fetch_failed", {
          candidateId,
          status: response.status,
        });
        return "We couldn't load this resume right now. Please try again in a moment.";
      }
      const data = await response.json();
      return data.resume_text || "No resume is on file for this candidate.";
    } catch (error) {
      logger.error("candidates.resume.fetch_error", {
        candidateId,
        message: (error as Error)?.message,
      });
      return "We couldn't load this resume right now. Please try again in a moment.";
    }
  };

  const handleViewResume = async (candidate: Candidate) => {
    let resumeText = candidate.resume_text || candidate.data?.resume_text;
    
    // If no resume text available, try to fetch it
    if (!resumeText || resumeText.trim() === "") {
      console.log(`📄 Fetching resume for candidate: ${candidate.name}`);
      resumeText = await fetchCandidateResume(candidate.candidate_id);
    }
    
    setSelectedCandidateForResume({
      name: candidate.name,
      resumeText: resumeText || "Resume content is not available for this candidate."
    });
    setResumeModalOpen(true);
  };

  // ---- Engage Handlers ----
  const handleEngageClick = async (candidate: Candidate) => {
    setEngageLoading(true);
    setEngageError(null);
    try {
      const data = await engagement.generatePayload({
        candidateIds: [candidate.candidate_id],
        jobId: candidate.jobdiva_id,
      });
      setEngagePayload(data.payload);
      setSelectedCandidateIds([candidate.candidate_id]);
      setIsEngageModalOpen(true);
    } catch (err: any) {
      setEngageError(err.message || 'Failed to generate payload');
    } finally {
      setEngageLoading(false);
    }
  };

  const handleScheduleCall = async (payloadOverride?: string) => {
    setEngageLoading(true);
    setEngageError(null);
    setApiResponse(null);
    const payloadToSend = payloadOverride ?? engagePayload;
    try {
      const data = await engagement.sendBulkInterview({
        payload: payloadToSend,
        realCandidateIds: selectedCandidateIds,
      });
      setApiResponse(data);
      if (data.success) {
        // Store interview data for Assess lookups
        if (data.data && Array.isArray(data.data)) {
          const interviewDataMap = { ...candidateInterviewData };
          data.data.forEach((interviewInfo: any) => {
            const candidateId = selectedCandidateIds[0] || interviewInfo.candidate_email;
            interviewDataMap[candidateId] = {
              interview_id: interviewInfo.interview_id,
              candidate_name: interviewInfo.candidate_name,
              candidate_email: interviewInfo.candidate_email,
              links: interviewInfo.links,
              session_token: interviewInfo.session_token,
              created_at: interviewInfo.created_at
            };
          });
          setCandidateInterviewData(interviewDataMap);
        }
        setTimeout(() => setIsEngageModalOpen(false), 1500);
      } else {
        setEngageError(data.message || 'API returned error status');
      }
    } catch (err: any) {
      setEngageError(err.message || 'Unknown error');
    } finally {
      setEngageLoading(false);
    }
  };

  // ---- Assess Handler ----
  const handleAssessClick = async (candidate: Candidate) => {
    setSelectedAssessCandidate(candidate);
    try {
      const data = await engagement.latestInterviewById(candidate.candidate_id);
      if (data.success && data.interview_id) {
        setSelectedAssessInterviewId(data.interview_id);
      } else {
        setSelectedAssessInterviewId(null);
      }
    } catch (e) {
      setSelectedAssessInterviewId(null);
    }
    setIsAssessModalOpen(true);
  };

  return (
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Header */}
      <div className="flex items-center gap-4 mt-2">
        <Button variant="ghost" size="icon" asChild className="rounded-full h-10 w-10">
          <Link href="/">
             <ArrowLeft className="h-5 w-5 text-slate-400" />
          </Link>
        </Button>
        <div>
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Master Candidate Pool</h1>
          <p className="text-slate-500 text-[14px]">All talent sourced across your jobs portfolio.</p>
        </div>
      </div>

      {/* Controls */}
      <div className="space-y-3 mt-4">
        <div className="flex justify-between items-center gap-4">
          <div className="relative w-[400px]">
            <Search className="absolute left-3.5 top-1/2 transform -translate-y-1/2 text-slate-400 h-[18px] w-[18px]" />
            <Input
              placeholder="Search candidates, jobs, or sources..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10 h-11 border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white rounded-xl text-[14px] shadow-sm"
            />
          </div>
          <div className="flex items-center gap-3">
            <Badge variant="outline" className="px-4 py-1.5 h-11 flex items-center gap-2 border-slate-200 bg-white text-slate-600 font-bold rounded-xl shadow-sm">
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              {totalCandidates === 0
                ? "0 candidates"
                : `${rangeStart}–${rangeEnd} of ${totalCandidates}`}
            </Badge>
          </div>
        </div>

        {/* Filter dropdowns. Option values come from
            `/candidates/filter-options` (DB-wide distinct) so toggling one
            filter never empties out the other dropdowns, and the full set
            is always available regardless of the current page. */}
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex items-center gap-1.5 text-[12.5px] font-semibold text-slate-500 mr-1">
            <Filter className="w-4 h-4 text-slate-400" />
            Filters
          </div>

          <Select value={jobFilter} onValueChange={setJobFilter}>
            <SelectTrigger className="h-10 min-w-[220px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="All jobs" />
            </SelectTrigger>
            <SelectContent className="max-h-[320px]">
              <SelectItem value={ALL}>All jobs</SelectItem>
              {jobOptions.map((j) => (
                <SelectItem key={j.id} value={j.id}>
                  {j.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={matchFilter} onValueChange={setMatchFilter}>
            <SelectTrigger className="h-10 min-w-[160px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="Any match" />
            </SelectTrigger>
            <SelectContent>
              {MATCH_BANDS.map((b) => (
                <SelectItem key={b.value} value={b.value}>
                  {b.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={sourceFilter} onValueChange={setSourceFilter}>
            <SelectTrigger className="h-10 min-w-[170px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="Any source" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Any source</SelectItem>
              {sourceOptions.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={locationFilter} onValueChange={setLocationFilter}>
            <SelectTrigger className="h-10 min-w-[170px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="Any location" />
            </SelectTrigger>
            <SelectContent className="max-h-[320px]">
              <SelectItem value={ALL}>Any location</SelectItem>
              {locationOptions.map((l) => (
                <SelectItem key={l} value={l}>
                  {l}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {(activeFilterCount > 0 || searchQuery || sortKey) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                clearAllFilters();
                setSortKey(null);
                setSortDir(null);
              }}
              className="h-10 px-3 text-[12.5px] font-semibold text-slate-500 hover:text-slate-900 hover:bg-slate-100 rounded-xl flex items-center gap-1.5"
            >
              <X className="w-3.5 h-3.5" />
              Clear
              {activeFilterCount > 0 && (
                <span className="ml-1 px-1.5 py-0.5 bg-slate-100 text-slate-600 rounded-md text-[11px] font-bold">
                  {activeFilterCount}
                </span>
              )}
            </Button>
          )}
        </div>
      </div>

      {/* Candidates Table */}
      <div className="bg-white rounded-2xl shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)] border border-slate-200 overflow-hidden mt-2">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center py-40 gap-4">
            <div className="w-10 h-10 border-4 border-[#6366f1]/20 border-t-[#6366f1] rounded-full animate-spin" />
            <p className="text-slate-400 font-medium text-[14px]">Synchronizing talent pool...</p>
          </div>
        ) : candidates.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-40 text-center gap-4">
            <div className="w-20 h-20 bg-slate-50 rounded-full flex items-center justify-center mb-2">
                  <User className="w-10 h-10 text-slate-200" />
            </div>
            <div>
              <h3 className="text-[18px] font-bold text-slate-900">No candidates found</h3>
              <p className="text-slate-500 text-[14px] mt-1">Try adjusting your search filters or source more talent.</p>
            </div>
            <Button asChild className="mt-2 bg-[#6366f1] hover:bg-[#4f46e5] text-white font-bold h-10 px-6 rounded-lg">
              <Link href="/jobs/new">Find Talent</Link>
            </Button>
          </div>
        ) : (
          <Table 
            className="relative" 
            containerClassName="overflow-x-auto overflow-y-auto max-h-[calc(100vh-220px)] border-b border-slate-100"
          >
              <TableHeader className="bg-[#fcfdfd]">
                <TableRow className="border-slate-100">
                  <TableHead className="pl-10 sticky top-0 left-0 bg-[#fcfdfd] z-50 w-[110px] min-w-[110px] h-14 border-b border-slate-100"></TableHead>
                  <TableHead
                    onClick={() => toggleSort("name")}
                    className="group/sortable cursor-pointer select-none w-[300px] min-w-[300px] text-left text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 left-[110px] bg-[#fcfdfd] z-50 border-r border-b border-slate-100 shadow-[4px_0_8px_-4px_rgba(0,0,0,0.1)] hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Candidate Name {renderSortIcon("name")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("match")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Match {renderSortIcon("match")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("job_title")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Applied For {renderSortIcon("job_title")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("location")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Location {renderSortIcon("location")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("source")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Sourcing Details {renderSortIcon("source")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("created_at")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Sourced On {renderSortIcon("created_at")}</span>
                  </TableHead>
                  <TableHead className="text-center pr-10 text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="divide-y divide-slate-100">
                {candidates.map((candidate, idx) => (
                  <TableRow key={`${candidate.id || candidate.candidate_id}-${idx}`} className="hover:bg-slate-50/50 transition-colors group">
                    <TableCell className="pl-10 py-6 sticky left-0 bg-white z-30 w-[110px] min-w-[110px] group-hover:bg-slate-50 transition-colors">
                      <Avatar className="h-12 w-12 border border-slate-200 shadow-sm transition-transform group-hover:scale-105">
                        <AvatarImage src={candidate.image_url} />
                        <AvatarFallback className="bg-slate-100 text-slate-400 text-[14px] font-bold">
                          {candidate.name.split(' ').map(n => n[0]).join('')}
                        </AvatarFallback>
                      </Avatar>
                    </TableCell>
                    <TableCell className="py-6 w-[300px] min-w-[300px] max-w-[300px] sticky left-[110px] bg-white z-30 border-r border-slate-100 shadow-[4px_0_8px_-4px_rgba(0,0,0,0.1)] group-hover:bg-slate-50 transition-colors overflow-hidden">
                      <div className="space-y-1 relative z-10">
                        <a
                          href={candidate.source?.startsWith('LinkedIn') ? candidate.profile_url || '#' : '#'}
                          target={candidate.source?.startsWith('LinkedIn') ? "_blank" : undefined}
                          rel={candidate.source?.startsWith('LinkedIn') ? "noopener noreferrer" : undefined}
                          className="group/name flex items-center gap-3"
                          onClick={(e) => {
                            if (candidate.source !== 'LinkedIn') {
                              e.preventDefault();
                              handleViewResume(candidate);
                            }
                          }}
                        >
                           <span className="flex items-center gap-2 max-w-full">
                             <span className={`text-[15px] font-bold text-slate-900 transition-colors whitespace-normal break-words ${
                               candidate.source?.startsWith('LinkedIn') ? 'group-hover/name:text-[#1d4ed8]' : 
                               candidate.source === 'JobDiva-TalentSearch' ? 'group-hover/name:text-[#c2410c]' : 
                               'group-hover/name:text-[#6366f1]'
                             }`}>
                               {candidate.name}
                             </span>
                             <span 
                               className={`shrink-0 h-6 w-6 flex items-center justify-center border border-slate-200 bg-white text-slate-400 rounded-lg shadow-sm transition-all ${
                                 candidate.source?.startsWith('LinkedIn') 
                                   ? 'group-hover/name:border-[#bfdbfe] group-hover/name:bg-[#eff6ff] group-hover/name:text-[#1d4ed8]' : 
                                 candidate.source === 'JobDiva-TalentSearch' 
                                   ? 'group-hover/name:border-[#D2B48C] group-hover/name:bg-[#FDF8F5] group-hover/name:text-[#8B5A2B]' : 
                                 'group-hover/name:border-[#c7d2fe] group-hover/name:bg-[#f5f3ff] group-hover/name:text-[#6366f1]'
                               }`}
                               title={candidate.source?.startsWith('LinkedIn') ? "View LinkedIn Profile" : "Click to view resume"}
                             >
                               <ExternalLink className="w-3 h-3" />
                             </span>
                           </span>
                        </a>
                        <div className="flex items-center gap-1.5 opacity-70 mt-1" title={candidate.headline || ""}>
                          <Briefcase className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                          <p className="text-[13px] text-slate-600 font-medium truncate">{candidate.headline}</p>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="flex items-center justify-center">
                        {(() => {
                          // Use pickMatchScore so 0 renders as "0% Match" (not
                          // N/A). Previous `||` chain treated 0 as falsy → every
                          // unscored-but-present candidate collapsed to "N/A".
                          const score = pickMatchScore(candidate);
                          if (score === null) {
                            return (
                              <span className="text-[12px] text-slate-400 font-medium px-2 py-1 bg-slate-50 rounded-md border border-slate-100">N/A</span>
                            );
                          }
                          const rounded = Math.round(score);
                          const tone =
                            score >= 80 ? "bg-emerald-100 text-emerald-700 border-emerald-200"
                            : score >= 60 ? "bg-amber-100 text-amber-700 border-amber-200"
                            : "bg-rose-100 text-rose-700 border-rose-200";
                          return (
                            <span className={`px-2.5 py-1 rounded-full text-[12px] font-bold shadow-sm border ${tone}`}>
                              {rounded}% Match
                            </span>
                          );
                        })()}
                      </div>
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="space-y-1 text-center">
                        <p className="text-[14px] font-medium text-slate-900">
                          {candidate.job_title || "Unknown Job"}
                        </p>
                        <p className="text-[11.5px] text-slate-400 font-medium">Ref: {candidate.jobdiva_id}</p>
                      </div>
                    </TableCell>
                    <TableCell className="py-6 min-w-[140px]">
                      {candidate.location ? (
                         <div className="flex items-center justify-center gap-1.5 opacity-80">
                           <MapPin className="w-3.5 h-3.5 text-slate-400" />
                           <p className="text-[12px] text-slate-600 font-medium whitespace-nowrap leading-tight">{candidate.location}</p>
                         </div>
                      ) : (
                         <div className="flex justify-center"><span className="text-[12px] text-slate-400 font-medium">N/A</span></div>
                      )}
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="space-y-2 flex justify-center">
                        <span className={`px-2.5 w-fit py-0.5 rounded-lg text-[10.5px] font-extrabold uppercase tracking-wider flex items-center gap-1.5 shadow-sm h-fit border ${candidate.source?.startsWith('LinkedIn')
                            ? 'bg-[#eff6ff] text-[#1d4ed8] border-[#bfdbfe]'
                            : candidate.source === 'JobDiva-TalentSearch'
                              ? 'bg-[#FDF8F5] text-[#8B5A2B] border-[#D2B48C]'
                              : 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]'
                          }`}>
                          {candidate.source?.startsWith('LinkedIn') ? <Linkedin className="w-3 h-3 fill-current" /> : candidate.source === 'JobDiva-TalentSearch' ? <Zap className="w-3 h-3 fill-current" /> : <ShieldCheck className="w-3 h-3" />}
                          {candidate.source || "JobDiva"}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="py-6 whitespace-nowrap text-center">
                       <p className="text-[13.5px] font-medium text-slate-600">
                         {new Date(candidate.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                       </p>
                    </TableCell>
                    <TableCell className="py-6 pr-10">
                       <div className="flex items-center justify-center gap-2 shrink-0">
                         <Button
                           size="sm"
                           className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                           onClick={() => handleEmailCandidate(candidate)}
                         >
                           <Mail className="w-3.5 h-3.5" />
                           {/* Add explicit span for text alignment if flex struggles */}
                           <span>Email</span>
                         </Button>
                         <Button
                           size="sm"
                           className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                           onClick={() => handleEngageClick(candidate)}
                           disabled={engageLoading}
                         >
                           <MessageSquare className="w-3.5 h-3.5" />
                           {engageLoading ? 'Loading...' : 'Engage'}
                         </Button>
                         <Button
                           size="sm"
                           className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                           onClick={() => handleAssessClick(candidate)}
                         >
                           <FileText className="w-3.5 h-3.5" />
                           Assess
                         </Button>

                       </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
        )}
      </div>

      {/* Pagination. Always rendered so the page-size dropdown stays
          accessible even with 0 rows. Disabled nav buttons when at
          boundary. */}
      {totalCandidates > 0 && (
        <div className="mt-4 flex items-center justify-between bg-white/70 backdrop-blur-xl p-3 px-5 rounded-2xl border border-slate-200/60 shadow-[0_4px_20px_rgb(0,0,0,0.03)]">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 text-[13px]">
              <span className="text-slate-500 font-medium">Showing</span>
              <span className="font-bold text-slate-800">{rangeStart}–{rangeEnd}</span>
              <span className="text-slate-500 font-medium">of {totalCandidates} candidates</span>
            </div>

            <div className="h-4 w-[1px] bg-slate-200/80" />

            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              className="bg-transparent text-[13px] font-bold text-slate-600 outline-none cursor-pointer border hover:bg-white/50 border-transparent hover:border-slate-200 rounded-md py-1 px-2 transition-all"
              aria-label="Rows per page"
            >
              {PAGE_SIZE_OPTIONS.map(n => (
                <option key={n} value={n}>{n} / page</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setCurrentPage(1)}
              disabled={currentPage === 1}
              className="h-8 px-2.5 bg-white border-slate-200 text-[12px] font-semibold text-slate-600 rounded-lg disabled:opacity-40"
            >
              First
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
              disabled={currentPage === 1}
              className="h-8 w-8 p-0 bg-white border-slate-200 text-slate-600 rounded-lg disabled:opacity-40"
              aria-label="Previous page"
            >
              <ChevronLeft className="w-4 h-4" />
            </Button>
            <span className="px-3 text-[13px] font-bold text-slate-700 select-none">
              Page {currentPage} / {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
              disabled={currentPage >= totalPages}
              className="h-8 w-8 p-0 bg-white border-slate-200 text-slate-600 rounded-lg disabled:opacity-40"
              aria-label="Next page"
            >
              <ChevronRight className="w-4 h-4" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setCurrentPage(totalPages)}
              disabled={currentPage >= totalPages}
              className="h-8 px-2.5 bg-white border-slate-200 text-[12px] font-semibold text-slate-600 rounded-lg disabled:opacity-40"
            >
              Last
            </Button>
          </div>
        </div>
      )}

      {/* Email Modal */}
      {selectedCandidateForEmail && (
        <CandidateMessageModal
          candidateName={selectedCandidateForEmail.name || `${selectedCandidateForEmail.firstName} ${selectedCandidateForEmail.lastName}`}
          candidateEmail={selectedCandidateForEmail.email || selectedCandidateForEmail.data?.email || "Email not available"}
          isOpen={messageModalOpen}
          onClose={() => {
            setMessageModalOpen(false);
            setSelectedCandidateForEmail(null);
          }}
        />
      )}

      {/* Resume Modal */}
      {selectedCandidateForResume && (
        <ResumeModal
          candidateName={selectedCandidateForResume.name}
          resumeText={selectedCandidateForResume.resumeText}
          isOpen={resumeModalOpen}
          onClose={() => {
            setResumeModalOpen(false);
            setSelectedCandidateForResume(null);
          }}
        />
      )}

      {/* Details Modal */}
      {selectedCandidateForDetails && (
        <CandidateDetailsModal
          isOpen={detailsModalOpen}
          onClose={() => {
            setDetailsModalOpen(false);
            setSelectedCandidateForDetails(null);
          }}
          candidateName={selectedCandidateForDetails.name}
          profileUrl={selectedCandidateForDetails.profileUrl}
          imageUrl={selectedCandidateForDetails.imageUrl}
          jobTitle={selectedCandidateForDetails.jobTitle}
          location={selectedCandidateForDetails.location}
          experienceYears={selectedCandidateForDetails.experienceYears}
          tags={selectedCandidateForDetails.tags}
          matchScore={selectedCandidateForDetails.matchScore}
          missingSkills={selectedCandidateForDetails.missingSkills}
          explainability={selectedCandidateForDetails.explainability}
          matchScoreDetails={selectedCandidateForDetails.matchScoreDetails}
          matchedSkills={selectedCandidateForDetails.matchedSkills}
        />
      )}

      {/* Engage Wizard Modal */}
      <EngageWizardModal
        open={isEngageModalOpen}
        onClose={() => setIsEngageModalOpen(false)}
        initialPayload={engagePayload}
        candidateIds={selectedCandidateIds}
        onSend={async (payload) => {
          setEngagePayload(payload);
          await handleScheduleCall(payload);
        }}
        loading={engageLoading}
        error={engageError}
        successData={apiResponse}
      />

      {/* Assess Modal */}
      <AssessModal
        open={isAssessModalOpen}
        onClose={() => {
          setIsAssessModalOpen(false);
          setSelectedAssessCandidate(null);
          setSelectedAssessInterviewId(null);
        }}
        interviewId={selectedAssessInterviewId}
        candidateName={selectedAssessCandidate?.name || ''}
      />
    </div>
  );
}