"use client";

import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Search,
  RefreshCw,
  Loader2,
  Mail,
  Phone,
  Medal,
  ChevronDown,
  ChevronUp,
  ChevronsUpDown,
  Filter,
  Calendar,
  MessageSquare,
  Send,
  ExternalLink,
  User,
  Briefcase,
  Building2,
  Zap,
  Check,
  X,
  Activity
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";
import { CandidateMessageModal } from "@/components/candidate-message-modal";
import { EngageWizardModal } from "@/components/EngageWizardModal";
import { UserActivityLogModal } from "@/components/UserActivityLogModal";
import { MissingPhonesModal, type MissingPhoneCandidate } from "@/components/missing-phones-modal";
import { API_BASE, authFetch } from "@/lib/api";
import { buildJobDivaCandidateUrl } from "@/lib/jobdiva";
import { useEngagementFlow } from "@/hooks/use-engagement-flow";

// Utility function to format dates
const formatDate = (dateStr: string) => {
  if (!dateStr) return "—";
  try {
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return dateStr;
    return date.toLocaleString('en-GB', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: true
    }).toUpperCase();
  } catch {
    return dateStr;
  }
};

const FINAL_ENGAGE_STATUSES = new Set([
  "completed",
  "passed",
  "failed",
  "rejected",
  "pass",
  "fail",
]);

const normalizeStatusValue = (value: unknown): string =>
  String(value || "").trim().toLowerCase();

const getEngageScore = (candidate: {
  engage_score?: number | null;
  data?: any;
}): number | null | undefined => {
  return candidate.engage_score !== undefined ? candidate.engage_score : candidate.data?.engage_score;
};

const hasFinalEngageOutcome = (candidate: {
  engage_status?: string;
  engage_hard_filter_status?: string;
  engage_score?: number | null;
  data?: any;
}): boolean => {
  const score = getEngageScore(candidate);
  if (score === null || score === undefined) {
    return false;
  }
  const statusCandidates = [
    candidate.engage_hard_filter_status,
    candidate.data?.engage_hard_filter_status,
    candidate.engage_status,
    candidate.data?.engage_status,
  ];

  return statusCandidates.some((status) => {
    const raw = normalizeStatusValue(status);
    return raw ? FINAL_ENGAGE_STATUSES.has(raw) || raw.includes("complete") : false;
  });
};

const ColumnFilterPopup = ({
  field,
  label,
  onClose,
  onApply,
  onClear,
  currentFilter,
  align = "left"
}: {
  field: string;
  label: string;
  onClose: () => void;
  onApply: (filter: { condition: any; value: string }) => void;
  onClear: () => void;
  currentFilter?: { condition: any; value: string };
  align?: "left" | "right";
}) => {
  const [condition, setCondition] = useState<any>(currentFilter?.condition || "contains");
  const [value, setValue] = useState(currentFilter?.value || "");
  const popupRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (popupRef.current && !popupRef.current.contains(event.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [onClose]);

  return (
    <div
      ref={popupRef}
      className={`absolute top-full ${align === "left" ? "left-0" : "right-0"} mt-1 w-64 bg-white border border-slate-200 rounded-lg shadow-xl z-[100] p-4 text-left normal-case tracking-normal cursor-default`}
    >
      <div className="flex items-center justify-between mb-4 border-b border-slate-100 pb-2">
        <span className="text-[12px] font-bold text-slate-700 uppercase">Filter {label}</span>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
          <X className="w-4 h-4" />
        </button>
      </div>

      <div className="space-y-4">
        <div>
          <label className="block text-[11px] font-bold text-slate-500 uppercase mb-1.5">Condition</label>
          <select
            value={condition}
            onChange={(e) => setCondition(e.target.value as any)}
            className="w-full h-9 px-3 text-[13px] bg-slate-50 border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 transition-all"
          >
            <option value="contains">Contains</option>
            <option value="not_contains">Does Not Contain</option>
            <option value="equals">Equals</option>
            <option value="starts_with">Starts With</option>
          </select>
        </div>

        <div>
          <label className="block text-[11px] font-bold text-slate-500 uppercase mb-1.5">Value</label>
          <input
            type="text"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Search text..."
            className="w-full h-9 px-3 text-[13px] bg-slate-50 border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 transition-all"
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter') onApply({ condition, value });
              if (e.key === 'Escape') onClose();
            }}
          />
        </div>

        <div className="flex items-center gap-2 pt-2">
          <button
            onClick={onClear}
            className="flex-1 h-9 text-[12px] font-bold text-slate-500 hover:bg-slate-100 rounded-md transition-colors border border-slate-200"
          >
            Clear
          </button>
          <button
            onClick={() => onApply({ condition, value })}
            className="flex-1 h-9 text-[12px] font-bold text-white bg-indigo-600 hover:bg-indigo-700 rounded-md transition-colors shadow-sm"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
};



interface JobDetails {
  job_id: string;
  jobdiva_id?: string;
  // JobDiva's internal numeric job id (monitored_jobs.job_id). The route param
  // is usually the human reference (e.g. "26-02576"); the JobDiva portal deep
  // link needs this numeric id, so we capture it separately from the API.
  jobdiva_numeric_id?: string;
  title: string;
  customer_name?: string;
  openings?: number;
  max_allowed_submittals?: number;
}

// B5: applied-filters panel — surfaces context set on Step 3 (criteria) and
// Step 5 (sourcing_filters) on the rankings page so recruiters can see what
// constraints produced this list.
type SourcingTitleEntry = {
  value?: string;
  matchType?: string;
  match_type?: string;
  years?: number;
  recent?: boolean;
};
type SourcingSkillEntry = SourcingTitleEntry;
type SourcingLocationEntry = { value?: string; radius?: string };
interface AppliedFilters {
  titles?: SourcingTitleEntry[];
  skills?: SourcingSkillEntry[];
  locations?: SourcingLocationEntry[];
  companies?: string[];
  keywords?: string[];
}
interface AppliedCriterion {
  id?: string;
  name: string;
  priority_score?: number;
  is_required?: boolean;
  category?: string;
}

interface Candidate {
  id: number;
  jobdiva_id?: string;
  jobdiva_candidate_id?: string;
  candidate_id?: string;
  engage_interview_id?: string;
  name: string;
  email: string;
  phone?: string;
  location?: string;
  work_location?: string;
  work_city?: string;
  work_state?: string;
  headline?: string;
  job_title?: string;
  image_url?: string;
  profile_url?: string;
  source: string;
  match_score: number;
  resume_match_percentage?: number;
  engage_score?: number;
  engage_candidate_score?: number;
  engage_total_score?: number;
  engage_status?: string;
  engage_hard_filter_status?: string;
  engage_hard_filter_details?: {
    question: string;
    status: "Pass" | "Fail" | "Pending";
    score?: number | null;
    total_score?: number | null;
    reason?: string;
  }[];
  engage_completed_at?: string;
  engage_created_at?: string;
  availability?: string;
  created_at: string;
  data?: any;
}

type EnrichStatus = { type: "info" | "error" | "success"; message: string };
type ToastState = { type: "info" | "error" | "success"; message: string } | null;

function ResumeScreeningHoverCard({
  candidate,
  open,
}: {
  candidate: Candidate;
  open: boolean;
}) {
  const dataBlob = candidate.data || {};
  const titleStr = String(candidate.job_title || candidate.headline || dataBlob?.headline || "").trim();
  const companyExp = Array.isArray(dataBlob?.company_experience) ? dataBlob.company_experience : [];
  const companyStr = String(companyExp[0]?.company || "").trim();
  const titleAtCompany = titleStr && companyStr ? `${titleStr} @ ${companyStr}` : titleStr || companyStr;

  const yearsRaw = dataBlob?.experience_years;
  const yearsNum = typeof yearsRaw === "number" ? yearsRaw : Number(yearsRaw);
  const yearsStr = Number.isFinite(yearsNum) && yearsNum > 0 ? `${yearsNum}+ yrs experience` : "";

  const matched = Array.isArray(dataBlob?.matched_skills)
    ? dataBlob.matched_skills.filter((s: unknown) => typeof s === "string" && s.trim().length > 0)
    : [];
  const missing = Array.isArray(dataBlob?.missing_skills)
    ? dataBlob.missing_skills.filter((s: unknown) => typeof s === "string" && s.trim().length > 0).slice(0, 5)
    : [];
  const explainability = Array.isArray(dataBlob?.explainability) ? dataBlob.explainability : [];
  const firstExplain =
    typeof explainability[0] === "string"
      ? explainability[0]
      : explainability[0]?.text || "";

  return (
    <div
      className={`absolute left-1/2 top-full z-40 mt-3 w-[420px] -translate-x-1/2 rounded-2xl border border-slate-200 bg-white/95 p-5 text-left shadow-2xl backdrop-blur-md transition-all duration-300 origin-top ${open
          ? "opacity-100 translate-y-0 scale-100 visible pointer-events-auto"
          : "opacity-0 -translate-y-2 scale-95 invisible pointer-events-none"
        }`}
    >
      {titleAtCompany && (
        <div className="mb-1.5 text-[12.5px] font-semibold text-slate-800 break-words whitespace-normal leading-relaxed" title={titleAtCompany}>
          {titleAtCompany}
        </div>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-[11.5px] text-slate-600">
        {yearsStr && (
          <span className="inline-flex items-center gap-1">
            <Calendar className="h-3 w-3 text-slate-400" />
            {yearsStr}
          </span>
        )}
        {candidate.email && (
          <span className="inline-flex items-center gap-1 break-all" title={candidate.email}>
            <Mail className="h-3 w-3 shrink-0 text-slate-400" />
            <span>{candidate.email}</span>
          </span>
        )}
      </div>

      {matched.length > 0 && (
        <div className="mb-3">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-500">
            Matched Skills
          </div>
          <div className="flex flex-wrap gap-1">
            {matched.map((skill: string, i: number) => (
              <span
                key={`${skill}-${i}`}
                className="rounded-md border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700"
              >
                {skill}
              </span>
            ))}
          </div>
        </div>
      )}

      {missing.length > 0 && (
        <div className="mb-3">
          <div className="mb-1.5 text-[10px] font-bold uppercase tracking-wider text-slate-500">
            Top Missing Skills
          </div>
          <div className="flex flex-wrap gap-1">
            {missing.map((skill: string, i: number) => (
              <span
                key={`${skill}-${i}`}
                className="rounded-md border border-rose-100 bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-700"
              >
                {skill}
              </span>
            ))}
          </div>
        </div>
      )}

      {firstExplain && (
        <div className="border-t border-slate-100 pt-2 text-[11.5px] leading-relaxed text-slate-600 break-words whitespace-normal">
          {firstExplain}
        </div>
      )}
    </div>
  );
}

function HardFilterHoverCard({
  details,
  open,
}: {
  details?: Candidate["engage_hard_filter_details"];
  open: boolean;
}) {
  if (!details || details.length === 0) return null;

  return (
    <div
      className={`absolute left-1/2 top-full z-40 mt-3 w-[420px] -translate-x-1/2 rounded-2xl border border-slate-200 bg-white/95 p-4 text-left shadow-2xl backdrop-blur-md transition-all duration-300 origin-top ${open
          ? "opacity-100 translate-y-0 scale-100 visible pointer-events-auto"
          : "opacity-0 -translate-y-2 scale-95 invisible pointer-events-none"
        }`}
    >
      <div className="mb-3 flex items-center justify-between border-b border-slate-100 pb-2.5">
        <span className="text-[12px] font-bold uppercase tracking-widest text-slate-800 flex items-center gap-2">
          <Zap className="w-3.5 h-3.5 text-indigo-500" />
          Hard Filter Results
        </span>
        <span className="text-[10px] font-medium text-slate-400">
          {details.length} Questions
        </span>
      </div>
      <div className="space-y-2.5 max-h-[380px] overflow-y-auto pr-2 scrollbar-thin scrollbar-thumb-slate-200">
        {details.map((item, index) => (
          <div
            key={`${item.question}-${index}`}
            className="group/item rounded-xl border border-slate-200 bg-slate-50/50 p-3 hover:border-indigo-200 hover:bg-white transition-all duration-200"
          >
            <div className="mb-2 flex items-start justify-between gap-3">
              <div className="text-[13px] font-semibold leading-relaxed text-slate-800 flex-1 break-words whitespace-normal">
                {item.question}
              </div>
              <span
                className={`shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide shadow-sm ${item.status === "Pass"
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : item.status === "Fail"
                      ? "border-rose-200 bg-rose-50 text-rose-700"
                      : "border-amber-200 bg-amber-50 text-amber-700"
                  }`}
              >
                {item.status}
              </span>
            </div>
            <div className="flex flex-col gap-2">
              {item.reason ? (
                <div className="bg-white/50 rounded-lg p-2.5 border border-slate-100">
                  <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">AI Analysis</span>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-600 italic break-words whitespace-normal">
                    {item.reason}
                  </p>
                </div>
              ) : null}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function CandidateRankingsPage() {
  const { jobId } = useParams();
  const router = useRouter();
  const [showBackButton, setShowBackButton] = useState(true);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const params = new URLSearchParams(window.location.search);
      if (params.get("source") === "email" || window.history.length <= 1) {
        setShowBackButton(false);
      }
    }
  }, []);

  const engagement = useEngagementFlow();
  const CANDIDATE_PAGE_SIZE = 100;
  const loadMoreRef = useRef<HTMLDivElement | null>(null);

  const [job, setJob] = useState<JobDetails | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [candidateTotalCount, setCandidateTotalCount] = useState(0);
  const [candidateOffset, setCandidateOffset] = useState(0);
  const [hasMoreCandidates, setHasMoreCandidates] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [launchedRowCount, setLaunchedRowCount] = useState(0);
  const [searchQuery, setSearchQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [appliedFilters, setAppliedFilters] = useState<AppliedFilters | null>(null);
  const [criteriaList, setCriteriaList] = useState<AppliedCriterion[]>([]);
  const [appliedFiltersOpen, setAppliedFiltersOpen] = useState(false);
  const [feedbacks, setFeedbacks] = useState<Record<string, string>>({});
  const [syncingCandidateId, setSyncingCandidateId] = useState<number | null>(null);
  const [integrationModalOpen, setIntegrationModalOpen] = useState<'submit' | 'reject' | null>(null);
  const [actionCandidateId, setActionCandidateId] = useState<number | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  const handleConfirmSubmit = async () => {
    if (actionCandidateId) {
      setSyncingCandidateId(actionCandidateId);
      try {
        const response = await authFetch(`${API_BASE}/jobs/${jobId}/candidates/${actionCandidateId}/feedback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ feedback_type: 'Submit' })
        });

        if (response.ok) {
          setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Submit' }));
        } else {
          console.error('Failed to sync submission with JobDiva');
          setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Submit' }));
        }
      } catch (error) {
        console.error('Error syncing submission:', error);
        setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Submit' }));
      } finally {
        setSyncingCandidateId(null);
        setIntegrationModalOpen(null);
        setActionCandidateId(null);
      }
    }
  };

  const handleConfirmReject = async () => {
    if (actionCandidateId && rejectReason) {
      setSyncingCandidateId(actionCandidateId);
      try {
        const response = await authFetch(`${API_BASE}/jobs/${jobId}/candidates/${actionCandidateId}/feedback`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            feedback_type: 'Reject',
            reason: rejectReason
          })
        });

        if (response.ok) {
          setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Reject' }));
        } else {
          console.error('Failed to sync rejection with JobDiva');
          setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Reject' }));
        }
      } catch (error) {
        console.error('Error syncing rejection:', error);
        setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Reject' }));
      } finally {
        setSyncingCandidateId(null);
        setIntegrationModalOpen(null);
        setActionCandidateId(null);
        setRejectReason('');
      }
    }
  };

  // Filter + sort state. `filteredCandidates` is now derived via useMemo so every
  // filter updates the table synchronously (no stale state via setFilteredCandidates).
  type StatusFilter = "all" | "pass" | "fail" | "in_progress" | "pending" | "n/a";
  type SortField = "index" | "name" | "screening_score" | "engage_score" | "total_score" | "source" | "engage_status";
  type SortDir = "asc" | "desc";
  type ColumnFilterCondition = "contains" | "not_contains" | "equals" | "starts_with";
  interface ColumnFilter {
    condition: ColumnFilterCondition;
    value: string;
  }

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [activityFilter, setActivityFilter] = useState<"all" | "has_activity">("all");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [minScore, setMinScore] = useState<number>(0);
  // Default the rank list to fit-score descending so it actually ranks by
  // score rather than by the source-priority pre-sort applied at load time.
  const [sortField, setSortField] = useState<SortField>("total_score");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [columnFilters, setColumnFilters] = useState<Record<string, ColumnFilter>>({});
  const [activeFilterField, setActiveFilterField] = useState<string | null>(null);
  const [isActivityLogModalOpen, setIsActivityLogModalOpen] = useState(false);
  const [selectedCandidateForActivity, setSelectedCandidateForActivity] = useState<{
    id: string;
    name: string;
  } | null>(null);

  // Resume-matching completion status for filter + table labels.
  const deriveStatus = (c: Candidate): "completed" | "pending" => {
    const fromData = String(c.data?.resume_matching_status || "").toLowerCase();
    if (fromData === "done" || fromData === "completed") return "completed";
    const s = c.match_score ?? c.resume_match_percentage ?? 0;
    return s > 0 ? "completed" : "pending";
  };

  // Pull availability off the JSONB `data` blob. Different producers put it in
  // different keys — surface whichever is present, else return null so we render "—".
  const deriveAvailability = (c: Candidate): string | null => {
    const d = c.data || {};
    return (
      d.recent_availability ||
      d.recentAvailability ||
      d.availability_status ||
      d.available ||
      d.availability ||
      c.availability ||
      null
    );
  };

  const deriveInterviewId = (c: Candidate): string | null => {
    const raw =
      c.data?.engage_interview_id ||
      c.engage_interview_id ||
      null;
    const v = String(raw || "").trim();
    return v || null;
  };

  const normalizeSourceLabel = (source: string | null | undefined): string => {
    const raw = String(source || "").trim();
    const s = raw.toLowerCase();
    if (!s) return "—";
    if (s.includes("applicant")) return "Job-Diva Applicant";
    if (s.includes("talentsearch") || s.includes("talent_search")) return "Job-Diva Candidate";
    if (s.includes("linkedin")) return "LinkedIn";
    return raw;
  };

  const normalizeInterviewStatus = (c: Candidate): { label: string; color: string } => {
    const interviewId = deriveInterviewId(c);
    if (!interviewId) {
      return { label: "N/A", color: "#94a3b8" };
    }

    const raw = String(c.engage_status || c.data?.engage_status || "").trim().toLowerCase();
    if (!raw) {
      return { label: "Pending", color: "#64748b" };
    }

    const pendingStates = new Set([
      "pending",
      "sent",
      "created",
      "queued",
      "scheduled",
      "started",
    ]);
    if (pendingStates.has(raw)) {
      return { label: "Pending", color: "#64748b" };
    }

    if (raw === "in_progress" || raw === "in-progress" || raw === "inprogress" || raw === "in progress") {
      return { label: "In Progress", color: "#f59e0b" };
    }

    if (raw.includes("complete") || raw === "passed" || raw === "pass") {
      return { label: "Pass", color: "#059669" };
    }

    if (raw === "failed" || raw === "fail" || raw === "rejected") {
      const score = getEngageScore(c);
      if (score === null || score === undefined) {
        return { label: "Pending", color: "#64748b" };
      }
      return { label: "Fail", color: "#e11d48" };
    }

    const label = raw.charAt(0).toUpperCase() + raw.slice(1).replace(/_/g, " ");
    return { label, color: "#64748b" };
  };

  const normalizeHardFilterStatus = (c: Candidate): { label: string; color: string } => {
    const raw = String(c.engage_hard_filter_status || c.data?.engage_hard_filter_status || "").trim().toLowerCase();
    if (!raw) {
      return { label: "—", color: "#94a3b8" };
    }

    if (raw.includes("fail")) {
      return { label: "Fail", color: "#dc2626" };
    }
    if (raw.includes("pass")) {
      return { label: "Pass", color: "#059669" };
    }

    const label = raw.charAt(0).toUpperCase() + raw.slice(1);
    return { label, color: "#64748b" };
  };



  const availabilityPillClasses = (raw: string | null): string => {
    if (!raw) return "text-slate-500";
    const v = String(raw).toLowerCase();
    if (v.includes("available") || v.includes("active") || v.includes("open")) {
      return "text-emerald-600";
    }
    if (v.includes("placed") || v.includes("employed") || v.includes("on assignment")) {
      return "text-slate-500";
    }
    if (v.includes("do not") || v.includes("unavailable") || v.includes("closed")) {
      return "text-rose-600";
    }
    return "text-slate-600";
  };

  const compactEnrichStatusMessage = (status: EnrichStatus): string => {
    const raw = String(status.message || "").trim();
    const lower = raw.toLowerCase();
    if (!raw) return "";
    if (lower.includes("no contact info found") || lower.includes("no contact match")) {
      return "No ZoomInfo contact found";
    }
    if (lower.includes("linkedin url missing")) {
      return "LinkedIn URL missing";
    }
    if (lower.includes("applied")) {
      return "Contact info applied";
    }
    if (lower.includes("failed")) {
      return "ZoomInfo request failed";
    }
    return raw;
  };

  // Distinct sources present in the current candidate set, for the source dropdown.
  const availableSources = useMemo(() => {
    const set = new Set<string>();
    candidates.forEach(c => { if (c.source) set.add(c.source); });
    return Array.from(set).sort();
  }, [candidates]);

  const filteredCandidates = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    let rows = candidates.filter(c => {
      // Search
      if (q) {
        const hay = `${c.name || ""} ${c.email || ""} ${c.headline || ""} ${c.location || ""} ${c.work_location || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      // Status
      if (statusFilter !== "all") {
        const engageLabel = normalizeInterviewStatus(c).label.toLowerCase();
        const sf = statusFilter === "in_progress" ? "in progress" : statusFilter;
        if (engageLabel !== sf) return false;
      }
      // Activity History
      if (activityFilter === "has_activity" && !deriveInterviewId(c)) return false;
      // Source
      if (sourceFilter !== "all" && c.source !== sourceFilter) return false;
      // Min score
      const score = c.match_score ?? c.resume_match_percentage ?? 0;
      if (score < minScore) return false;

      // Column (Funnel) Filters
      for (const [field, filter] of Object.entries(columnFilters)) {
        if (!filter.value) continue;

        let val = "";
        if (field === "name") val = c.name || "";
        else if (field === "source") val = normalizeSourceLabel(c.source);
        else if (field === "engage_status") val = normalizeInterviewStatus(c).label;
        else if (field === "screening_score") val = String(c.match_score || 0);
        else if (field === "engage_score") val = hasFinalEngageOutcome(c) ? String(c.engage_score || 0) : "";
        else if (field === "total_score") {
          const screeningScore = c.match_score || 0;
          const showEngageScore = hasFinalEngageOutcome(c);
          const engageScore = showEngageScore ? (c.engage_score || 0) : 0;
          val = String(showEngageScore ? Math.round((screeningScore + engageScore) / 2 * 10) / 10 : screeningScore);
        }

        const v = val.toLowerCase();
        const fv = filter.value.toLowerCase();

        switch (filter.condition) {
          case "contains":
            if (!v.includes(fv)) return false;
            break;
          case "not_contains":
            if (v.includes(fv)) return false;
            break;
          case "equals":
            if (v !== fv) return false;
            break;
          case "starts_with":
            if (!v.startsWith(fv)) return false;
            break;
        }
      }

      return true;
    });

    if (sortField !== "index") {
      const dir = sortDir === "asc" ? 1 : -1;
      // Coerce any score (number | string | null | "") to a finite number so a
      // missing/non-numeric score sorts as 0 instead of producing NaN, which
      // would scramble the comparator's ordering.
      const num = (v: unknown) => {
        const n = Number(v);
        return Number.isFinite(n) ? n : 0;
      };
      // Sort uses the SAME value the table cell displays (match_score, falling
      // back to resume_match_percentage) so rows are never out of order
      // relative to the number shown.
      const getScore = (c: Candidate) => num(c.match_score ?? c.resume_match_percentage);
      const getEngage = (c: Candidate) => (hasFinalEngageOutcome(c) ? num(c.engage_score) : -1);
      const getTotalScore = (c: Candidate) => {
        const screeningScore = getScore(c);
        return hasFinalEngageOutcome(c)
          ? Math.round((screeningScore + num(c.engage_score)) / 2 * 10) / 10
          : screeningScore;
      };
      // Deterministic tie-break so equal scores keep a stable, repeatable order.
      const tieBreak = (a: Candidate, b: Candidate) =>
        (a.name || "").localeCompare(b.name || "") ||
        String(a.candidate_id || a.id || "").localeCompare(String(b.candidate_id || b.id || ""));
      rows = [...rows].sort((a, b) => {
        let primary = 0;
        switch (sortField) {
          case "name":
            primary = (a.name || "").localeCompare(b.name || "");
            break;
          case "screening_score":
            primary = getScore(a) - getScore(b);
            break;
          case "engage_score":
            primary = getEngage(a) - getEngage(b);
            break;
          case "total_score":
            primary = getTotalScore(a) - getTotalScore(b);
            break;
          case "source":
            primary = normalizeSourceLabel(a.source).localeCompare(normalizeSourceLabel(b.source));
            break;
          case "engage_status":
            primary = normalizeInterviewStatus(a).label.localeCompare(normalizeInterviewStatus(b).label);
            break;
          default:
            primary = 0;
        }
        return primary !== 0 ? dir * primary : tieBreak(a, b);
      });
    }
    return rows;
  }, [candidates, searchQuery, statusFilter, activityFilter, sourceFilter, minScore, sortField, sortDir, columnFilters]);

  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDir(prev => (prev === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(field === "name" || field === "source" || field === "engage_status" ? "asc" : "desc");
    }
  };

  const clearFilters = () => {
    setSearchQuery("");
    setStatusFilter("all");
    setActivityFilter("all");
    setSourceFilter("all");
    setMinScore(0);
    setColumnFilters({});
  };

  // Modal states
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null);

  // Rank-list actions (Email / Screen / SMS)
  const [messageModalOpen, setMessageModalOpen] = useState(false);
  const [selectedCandidateForEmail, setSelectedCandidateForEmail] = useState<Candidate | null>(null);

  const [isScreenModalOpen, setIsScreenModalOpen] = useState(false);
  const [screenPayload, setScreenPayload] = useState<string>("");
  const [screeningCandidateIds, setScreeningCandidateIds] = useState<Set<string>>(new Set());
  const [screenError, setScreenError] = useState<string | null>(null);
  const [selectedScreenCandidateIds, setSelectedScreenCandidateIds] = useState<string[]>([]);
  const [screenApiResponse, setScreenApiResponse] = useState<any>(null);
  const [hoveredResumeScoreKey, setHoveredResumeScoreKey] = useState<string | null>(null);
  const [hoveredEngageScoreKey, setHoveredEngageScoreKey] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState>(null);
  const pushToast = (message: string, type: "info" | "error" | "success" = "info") => {
    setToast({ message, type });
  };
  const [refreshingResumeMatchIds, setRefreshingResumeMatchIds] = useState<Set<string>>(new Set());
  const [candidateProfileUrls, setCandidateProfileUrls] = useState<Record<string, string>>({});

  const [missingPhonesOpen, setMissingPhonesOpen] = useState(false);
  const [missingPhoneCandidates, setMissingPhoneCandidates] = useState<MissingPhoneCandidate[]>([]);
  const [pendingScreenCandidate, setPendingScreenCandidate] = useState<Candidate | null>(null);
  const [enrichingCandidateIds, setEnrichingCandidateIds] = useState<Set<string>>(new Set());
  const [enrichStatusByCandidateId, setEnrichStatusByCandidateId] = useState<Record<string, EnrichStatus>>({});
  const tableScrollRef = useRef<HTMLDivElement | null>(null);

  const hasUsablePhone = (p?: string | null) => {
    const digits = String(p || "").replace(/\D/g, "");
    return digits.length >= 7;
  };

  const needsContactEnrichment = (c: Candidate) => {
    const missingPhone = !hasUsablePhone(c.phone);
    const missingEmail = !String(c.email || "").trim();
    return missingPhone || missingEmail;
  };

  const extractLinkedInFromText = (text?: string | null): string => {
    const raw = String(text || "");
    if (!raw) return "";
    const m = raw.match(/https?:\/\/(?:www\.)?linkedin\.com\/in\/[A-Za-z0-9\-_%]+/i);
    return m ? m[0] : "";
  };

  const looksLikeLinkedInProfile = (url?: string | null): boolean => {
    const u = String(url || "").trim().toLowerCase();
    return u.includes("linkedin.com/in/");
  };

  const resolveCandidateLinkedInUrl = (c: Candidate): string => {
    const dataBlob = c.data || {};
    const candidates = [
      c.profile_url,
      (dataBlob?.profile_url as string | undefined),
      (dataBlob?.linkedin_url as string | undefined),
      (dataBlob?.urls?.linkedin as string | undefined),
      (dataBlob?.urls?.linkedin_url as string | undefined),
      extractLinkedInFromText(dataBlob?.resume_text as string | undefined),
    ]
      .map(v => String(v || "").trim())
      .filter(Boolean);

    return candidates.find(u => looksLikeLinkedInProfile(u)) || "";
  };

  const openCandidateProfileUrl = async (candidate: Candidate) => {
    const candidateKey = String(candidate.candidate_id || candidate.id || "").trim();
    if (!candidateKey) return;

    const source = String(candidate.source || "").toLowerCase();
    const isLinkedInSource = source.includes("linkedin");

    if (isLinkedInSource) {
      const linkedinUrl = resolveCandidateLinkedInUrl(candidate);
      if (linkedinUrl) {
        window.open(linkedinUrl, "_blank", "noopener,noreferrer");
      } else {
        pushToast("LinkedIn profile URL not available", "info");
      }
      return;
    }

    // JobDiva candidate: build a direct deep link from the JobDiva candidate id
    // (same verified-live format CandidateDetailsModal uses). The PROFILEURL API
    // fetch is frequently empty and can return a stale/dead format. The
    // /jobs/{id}/candidates API returns the JobDiva candidate id as
    // `candidate_id` (not `jobdiva_candidate_id`), so fall back to candidateKey
    // for JobDiva sources — otherwise the link never resolves.
    const isJobDivaSource = source.startsWith("jobdiva");
    const jobdivaCandidateId = String(
      candidate.jobdiva_candidate_id ||
      candidate.data?.jobdiva_candidate_id ||
      (isJobDivaSource ? candidateKey : "")
    ).trim();
    if (jobdivaCandidateId) {
      const url = buildJobDivaCandidateUrl(jobdivaCandidateId);
      if (url) {
        setCandidateProfileUrls(prev => ({ ...prev, [candidateKey]: url }));
        window.open(url, "_blank", "noopener,noreferrer");
        return;
      }
    }

    // Non-JobDiva source: fall back to an explicit profile URL if present.
    const existingProfileUrl =
      String(candidate.profile_url || "").trim() ||
      String(candidate.data?.profile_url || "").trim() ||
      String(candidateProfileUrls[candidateKey] || "").trim();

    if (existingProfileUrl) {
      window.open(existingProfileUrl, "_blank", "noopener,noreferrer");
      return;
    }

    pushToast("Profile URL not available", "info");
  };

  const handleEnrichContact = async (candidate: Candidate) => {
    const candidateKey = String(candidate.candidate_id || candidate.id || "").trim();
    if (!candidateKey) return;

    const linkedinUrl = resolveCandidateLinkedInUrl(candidate);
    if (!linkedinUrl) {
      const msg = "LinkedIn URL missing — cannot query ZoomInfo.";
      setEnrichStatusByCandidateId(prev => ({
        ...prev,
        [candidateKey]: {
          type: "error",
          message: msg,
        },
      }));
      pushToast(msg, "error");
      return;
    }

    setEnrichStatusByCandidateId(prev => {
      const next = { ...prev };
      delete next[candidateKey];
      return next;
    });

    setEnrichingCandidateIds(prev => {
      const next = new Set(prev);
      next.add(candidateKey);
      return next;
    });

    try {
      const res = await authFetch(`${API_BASE}/candidates/enrich-contact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_id: candidateKey,
          jobdiva_id: candidate.jobdiva_id || job?.jobdiva_id || String(jobId || "") || undefined,
          source: candidate.source || undefined,
          linkedin_url: linkedinUrl,
          full_name: candidate.name || undefined,
          company_name:
            candidate.data?.company_name ||
            candidate.data?.company?.name ||
            candidate.data?.enhanced_info?.current_company ||
            undefined,
          email: candidate.email || undefined,
          phone: candidate.phone || undefined,
        }),
      });

      const payload = await res.json().catch(() => ({}));
      if (!res.ok) {
        const msg = payload?.detail || `ZoomInfo call failed (${res.status})`;
        setEnrichStatusByCandidateId(prev => ({
          ...prev,
          [candidateKey]: {
            type: "error",
            message: msg,
          },
        }));
        pushToast(msg, "error");
        return;
      }

      const nextPhone = payload?.phone || candidate.phone || "";
      const nextEmail = payload?.email || candidate.email || "";

      if (!nextPhone && !nextEmail) {
        const msg = "No contact info found from ZoomInfo for this LinkedIn URL.";
        setEnrichStatusByCandidateId(prev => ({
          ...prev,
          [candidateKey]: {
            type: "info",
            message: msg,
          },
        }));
        pushToast("No ZoomInfo contact found", "info");
        return;
      }

      const successMsg = "ZoomInfo contact info applied.";
      setEnrichStatusByCandidateId(prev => ({
        ...prev,
        [candidateKey]: {
          type: "success",
          message: successMsg,
        },
      }));
      pushToast("Contact info applied", "success");

      setCandidates(prev =>
        prev.map(c => {
          const cid = String(c.candidate_id || c.id || "").trim();
          if (cid !== candidateKey) return c;
          return {
            ...c,
            phone: nextPhone,
            email: nextEmail,
            data: {
              ...(c.data || {}),
              zoominfo_contact_enrichment: {
                ...(c.data?.zoominfo_contact_enrichment || {}),
                linkedin_url: payload?.linkedin_url || linkedinUrl,
                workPhone: payload?.workPhone || null,
                mobilePhone: payload?.mobilePhone || null,
                workEmail: payload?.workEmail || null,
                personalEmail: payload?.personalEmail || null,
                phone_source: payload?.phone_source || null,
              },
            },
          };
        })
      );
    } catch (err: any) {
      const msg = err?.message || "Enrichment request failed";
      setEnrichStatusByCandidateId(prev => ({
        ...prev,
        [candidateKey]: {
          type: "error",
          message: msg,
        },
      }));
      pushToast(msg, "error");
    } finally {
      setEnrichingCandidateIds(prev => {
        const next = new Set(prev);
        next.delete(candidateKey);
        return next;
      });
    }
  };

  const runScreen = async (candidate: Candidate) => {
    const targetId = String(candidate.candidate_id || candidate.id || "");
    setScreeningCandidateIds(prev => {
      const next = new Set(prev);
      next.add(targetId);
      return next;
    });
    setScreenError(null);
    setScreenApiResponse(null);
    setScreenPayload("");
    setSelectedScreenCandidateIds([]);
    try {
      const data = await engagement.generatePayload({
        candidateIds: [targetId],
        jobId: candidate.jobdiva_id || String(jobId || ""),
      });
      setScreenPayload(data.payload);
      setSelectedScreenCandidateIds([targetId]);
      setIsScreenModalOpen(true);
    } catch (err: any) {
      setScreenError(err?.message || "Failed to generate screening payload");
    } finally {
      setScreeningCandidateIds(prev => {
        const next = new Set(prev);
        next.delete(targetId);
        return next;
      });
    }
  };

  const handleScreenModalClose = useCallback(() => {
    setIsScreenModalOpen(false);
    setScreenApiResponse(null);
    setScreenError(null);
  }, []);

  const handleScreenClick = async (candidate: Candidate) => {
    if (!hasUsablePhone(candidate.phone)) {
      setPendingScreenCandidate(candidate);
      setMissingPhoneCandidates([
        {
          candidate_id: String(candidate.candidate_id || candidate.id),
          name: candidate.name || "Unnamed",
          headline: candidate.headline || "",
          location: candidate.location || "",
          source: candidate.source || "",
          jobdiva_id: candidate.jobdiva_id || String(jobId || ""),
        },
      ]);
      setMissingPhonesOpen(true);
      return;
    }
    await runScreen(candidate);
  };

  const handleSendScreen = async (payloadOverride?: string) => {
    const idsAtSend = [...selectedScreenCandidateIds];
    setScreeningCandidateIds(prev => {
      const next = new Set(prev);
      for (const id of idsAtSend) next.add(id);
      return next;
    });
    setScreenError(null);
    setScreenApiResponse(null);
    const payloadToSend = payloadOverride ?? screenPayload;
    try {
      const data = await engagement.sendBulkInterview({
        payload: payloadToSend,
        realCandidateIds: idsAtSend,
      });
      setScreenApiResponse(data);
      if (data.success) {
        // Optimistically update status to Initiated for all selected candidates
        setCandidates(prev => prev.map(c => {
          const cid = String(c.candidate_id || c.id || "");
          if (idsAtSend.includes(cid)) {
            return {
              ...c,
              engage_status: "Initiated",
              engage_created_at: new Date().toISOString()
            };
          }
          return c;
        }));

        // Refresh data in background while modal shows success
        fetchData();
      } else {
        setScreenError(data.message || "Screen API returned an error");
      }
    } catch (err: any) {
      setScreenError(err?.message || "Screen call failed");
    } finally {
      setScreeningCandidateIds(prev => {
        const next = new Set(prev);
        for (const id of idsAtSend) next.delete(id);
        return next;
      });
    }
  };

  const handleEmailCandidate = (candidate: Candidate) => {
    setSelectedCandidateForEmail(candidate);
    setMessageModalOpen(true);
  };

  const handleSmsCandidate = (candidate: Candidate) => {
    const raw = String(candidate.phone || "").trim();
    const digits = raw.replace(/\D/g, "");
    if (!digits) {
      alert("No phone number available for this candidate.");
      return;
    }
    const smsTarget = raw.startsWith("+") ? `+${digits}` : digits;
    window.open(`sms:${smsTarget}`, "_blank");
  };

  const handleRefreshResumeMatch = async (candidate: Candidate) => {
    const candidateKey = String(candidate.candidate_id || candidate.id || "").trim();
    if (!candidateKey) return;

    setRefreshingResumeMatchIds(prev => {
      const next = new Set(prev);
      next.add(candidateKey);
      return next;
    });

    try {
      const res = await authFetch(
        `${API_BASE}/jobs/${jobId}/candidates/${encodeURIComponent(candidateKey)}/refresh-resume-match`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source: candidate.source || undefined }),
        }
      );
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload?.status !== "success") {
        throw new Error(payload?.detail || payload?.message || `Refresh failed (${res.status})`);
      }

      setCandidates(prev =>
        prev.map(c => {
          const cid = String(c.candidate_id || c.id || "").trim();
          if (cid !== candidateKey) return c;

          return {
            ...c,
            match_score: Number(payload?.score || 0),
            data: {
              ...(c.data || {}),
              resume_matching_status: payload?.resume_matching_status || "pending",
              resume_matching_scored_at: payload?.resume_matching_scored_at || null,
              matched_skills: payload?.matched_skills || [],
              missing_skills: payload?.missing_skills || [],
              match_score_details: payload?.match_score_details || {},
              explainability: payload?.explainability || [],
            },
          };
        })
      );
    } catch (err: any) {
      console.error("Failed to refresh resume match score", err);
    } finally {
      setRefreshingResumeMatchIds(prev => {
        const next = new Set(prev);
        next.delete(candidateKey);
        return next;
      });
    }
  };

  useEffect(() => {
    if (jobId) {
      fetchData();
    }
  }, [jobId]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 2500);
    return () => clearTimeout(timer);
  }, [toast]);

  const normalizeCandidateRows = useCallback((rows: any[]): Candidate[] => {
    // A row is JobDiva when its source says so, an entry in its merged
    // `sources` list says so, or it carries a JobDiva candidate id. (The API
    // returns the JobDiva candidate id as `candidate_id` for JobDiva-sourced
    // rows; `jobdiva_id` is the *job* reference, not the candidate, so it is
    // NOT used here.) JobDiva rows must never be the dropped side of a dedup.
    const rowIsJobDiva = (c: any) => {
      const s = String(c.source || "").toLowerCase();
      if (s.includes("jobdiva")) return true;
      const srcs = Array.isArray(c.sources) ? c.sources : [];
      if (srcs.some((x: any) => String(x || "").toLowerCase().includes("jobdiva"))) return true;
      return Boolean(String(c.data?.jobdiva_candidate_id || "").trim());
    };
    const isPlaceholderEmail = (e?: string) => {
      const n = String(e || "").trim().toLowerCase();
      if (!n || !n.includes("@")) return true;
      const domain = n.split("@").pop() || "";
      if (domain === "jobdiva.com") return true;
      if (n.endsWith("@noemail.pair.ai")) return true;
      return false;
    };

    const getCanonicalCandidateKey = (c: any) => {
      const emailRaw = String(c.email || c.data?.email || "").trim().toLowerCase();
      const emailKey = emailRaw && !isPlaceholderEmail(emailRaw) ? emailRaw : "";
      const phoneKey = String(c.phone || c.data?.phone || "").replace(/\D/g, "");
      const profileKey = String(c.profile_url || c.data?.urls?.linkedin || "").trim().toLowerCase();
      const sourceCandidateId = String(c.candidate_id || "").trim();
      const nameKey = String(c.name || "").trim().toLowerCase();

      // Strong identity first (real email / phone+name / LinkedIn) so the same
      // person merges across sources; bare name is only a last resort.
      if (emailKey) return `email:${emailKey}`;
      if (phoneKey.length >= 7 && nameKey) return `phone-name:${phoneKey}:${nameKey}`;
      if (profileKey.includes("linkedin.com")) return `profile:${profileKey}`;
      if (sourceCandidateId) return `cid:${sourceCandidateId}`;
      if (nameKey) return `name:${nameKey}`;
      return `row:${String(c.id || "").trim()}`;
    };

    const getCandidateRank = (c: any) => {
      const hasJobDivaCandidateId = rowIsJobDiva(c);
      const source = String(c.source || "").toLowerCase();
      const sourcePriority =
        source.includes("linkedin") ? 0 :
          source.includes("talentsearch") || source.includes("talent_search") ? 1 :
            source.includes("applicants") ? 2 : 3;
      const matchScore = Number(c.match_score || c.resume_match_percentage || c.data?.match_score || 0);
      return {
        hasJobDivaCandidateId,
        sourcePriority,
        matchScore,
        createdAt: Date.parse(String(c.created_at || 0)) || 0,
      };
    };

    // Fold the loser's best info into the surviving row (union sources, fill
    // gaps, prefer a real email over a synthetic one) so a merge never loses
    // contact data.
    const mergeRowBestOf = (dst: any, src: any) => {
      const srcList = (c: any) => {
        const out: string[] = [];
        if (Array.isArray(c.sources)) out.push(...c.sources.filter(Boolean).map(String));
        if (c.source) out.push(String(c.source));
        return out;
      };
      const merged = Array.from(new Set([...srcList(dst), ...srcList(src)]));
      if (merged.length) dst.sources = merged;
      for (const f of [
        "phone", "location", "headline", "title", "profile_url", "linkedin_url", "image_url",
        "engage_status", "engage_interview_id", "engage_score",
        "audit_status", "audit_interview_id", "audit_created_at", "audit_payload", "audit_response"
      ]) {
        if (!dst[f] && src[f]) dst[f] = src[f];
      }
      const dEmail = String(dst.email || "");
      const sEmail = String(src.email || "");
      if (sEmail && sEmail !== dEmail && (!dEmail || (isPlaceholderEmail(dEmail) && !isPlaceholderEmail(sEmail)))) {
        dst.email = sEmail;
      }
      // Merge candidate data dictionary so nested fields like engage_status are preserved
      if (src.data && typeof src.data === "object" && !Array.isArray(src.data)) {
        dst.data = {
          ...src.data,
          ...(dst.data || {})
        };
      }
      return dst;
    };

    const dedupedByIdentity = new Map<string, any>();
    rows.forEach((candidate: any) => {
      const dedupKey = getCanonicalCandidateKey(candidate);
      const existing = dedupedByIdentity.get(dedupKey);
      if (!existing) {
        dedupedByIdentity.set(dedupKey, candidate);
        return;
      }

      const currentRank = getCandidateRank(candidate);
      const existingRank = getCandidateRank(existing);
      const shouldReplace =
        (currentRank.hasJobDivaCandidateId ? 1 : 0) > (existingRank.hasJobDivaCandidateId ? 1 : 0) ||
        (
          currentRank.hasJobDivaCandidateId === existingRank.hasJobDivaCandidateId &&
          currentRank.sourcePriority < existingRank.sourcePriority
        ) ||
        (
          currentRank.hasJobDivaCandidateId === existingRank.hasJobDivaCandidateId &&
          currentRank.sourcePriority === existingRank.sourcePriority &&
          currentRank.matchScore > existingRank.matchScore
        ) ||
        (
          currentRank.hasJobDivaCandidateId === existingRank.hasJobDivaCandidateId &&
          currentRank.sourcePriority === existingRank.sourcePriority &&
          currentRank.matchScore === existingRank.matchScore &&
          currentRank.createdAt > existingRank.createdAt
        );

      // Keep ONE survivor and absorb the other's best info into it. The
      // survivor is whichever wins the rank above (JobDiva always beats a
      // non-JobDiva), so a JobDiva row is never dropped in favour of LinkedIn.
      if (shouldReplace) {
        const survivor = { ...candidate };
        mergeRowBestOf(survivor, existing);
        dedupedByIdentity.set(dedupKey, survivor);
      } else {
        const survivor = { ...existing };
        mergeRowBestOf(survivor, candidate);
        dedupedByIdentity.set(dedupKey, survivor);
      }
    });
    const uniqueCandidates = Array.from(dedupedByIdentity.values());

    const getSourcePriority = (source: string) => {
      const s = (source || "").toLowerCase();
      if (s.includes('applicants')) return 1;
      if (s.includes('linkedin')) return 2;
      if (s.includes('talentsearch') || s.includes('talent_search')) return 3;
      return 4;
    };

    const sorted = uniqueCandidates.sort((a: any, b: any) => {
      const prioA = getSourcePriority(a.source);
      const prioB = getSourcePriority(b.source);
      if (prioA !== prioB) return prioA - prioB;

      const totalA = (a.match_score || a.resume_match_percentage || 0);
      const totalB = (b.match_score || b.resume_match_percentage || 0);
      return totalB - totalA;
    });

    return sorted as Candidate[];
  }, []);

  const fetchCandidatesPage = useCallback(async (offset: number, replace: boolean) => {
    const apiBase = API_BASE;
    const query = new URLSearchParams({
      limit: String(CANDIDATE_PAGE_SIZE),
      offset: String(offset),
    });
    const candRes = await authFetch(`${apiBase}/jobs/${jobId}/candidates?${query.toString()}`);
    const candData = await candRes.json();

    if (candData.status !== "success" || !Array.isArray(candData.candidates)) return;

    const pageRows = candData.candidates;
    const pageFeedbacks: Record<string, string> = {};
    pageRows.forEach((c: any) => {
      if (c.data?.feedback_type) {
        pageFeedbacks[c.id] = c.data.feedback_type;
      }
    });
    setFeedbacks(prev => ({ ...prev, ...pageFeedbacks }));

    const launchedCount = Number(candData?.launched_count);
    if (Number.isFinite(launchedCount)) {
      setLaunchedRowCount(launchedCount);
    }

    const total = Number(candData?.pagination?.total);
    if (Number.isFinite(total)) {
      setCandidateTotalCount(total);
    }

    setHasMoreCandidates(Boolean(candData?.pagination?.has_more));
    setCandidateOffset(offset + pageRows.length);

    setCandidates(prev => {
      const mergedRows = replace ? pageRows : [...prev, ...pageRows];
      return normalizeCandidateRows(mergedRows);
    });

    // Fallback: if monitored job title is missing, infer from the first loaded row.
    setJob(prev => {
      if (!prev || prev.title === `Job ${jobId}`) {
        const firstCand = pageRows[0];
        const recoveredTitle = firstCand?.headline || firstCand?.job_title || `Job ${jobId}`;
        return {
          ...(prev || {}),
          job_id: jobId as string,
          title: recoveredTitle,
        };
      }
      return prev;
    });
  }, [CANDIDATE_PAGE_SIZE, jobId, normalizeCandidateRows]);

  const loadMoreCandidates = useCallback(async () => {
    if (!jobId || isLoading || isLoadingMore || !hasMoreCandidates) return;
    setIsLoadingMore(true);
    try {
      await fetchCandidatesPage(candidateOffset, false);
    } catch (error) {
      console.error("Error loading next candidate page:", error);
    } finally {
      setIsLoadingMore(false);
    }
  }, [candidateOffset, fetchCandidatesPage, hasMoreCandidates, isLoading, isLoadingMore, jobId]);

  const fetchData = async () => {
    setIsLoading(true);
    setIsLoadingMore(false);
    setCandidates([]);
    setFeedbacks({});
    setCandidateTotalCount(0);
    setLaunchedRowCount(0);
    setCandidateOffset(0);
    setHasMoreCandidates(false);
    try {
      const apiBase = API_BASE;

      // Fetch job details
      const jobRes = await authFetch(`${apiBase}/jobs/${jobId}/monitored-data`);
      const jobData = await jobRes.json();

      // Handle both { data: { ... } } and flat { ... } structures
      const data = jobData.data || jobData;

      if (data) {
        setJob({
          job_id: jobId as string,
          jobdiva_id: data.jobdiva_id,
          jobdiva_numeric_id: data.job_id ? String(data.job_id) : undefined,
          title: data.enhanced_title || data.title || `Job ${jobId}`,
          customer_name: data.customer_name,
          openings: data.openings,
          max_allowed_submittals: data.max_allowed_submittals
        });
        // B5: surface step-5 sourcing filters on this page.
        const sf = data.sourcing_filters || {};
        if (sf && typeof sf === "object" && Object.keys(sf).length > 0) {
          setAppliedFilters({
            titles: Array.isArray(sf.titles) ? sf.titles : [],
            skills: Array.isArray(sf.skills) ? sf.skills : [],
            locations: Array.isArray(sf.locations) ? sf.locations : [],
            companies: Array.isArray(sf.companies) ? sf.companies : [],
            keywords: Array.isArray(sf.keywords) ? sf.keywords : [],
          });
        } else {
          setAppliedFilters(null);
        }
      }

      // B5: parallel fetch step-3 criteria so the applied-filters panel can
      // render priority + required/preferred chips next to sourcing filters.
      try {
        const critRes = await authFetch(`${apiBase}/api/jobs/${jobId}/criteria`);
        if (critRes.ok) {
          const critData = await critRes.json();
          if (Array.isArray(critData?.criteria)) {
            setCriteriaList(critData.criteria);
          }
        }
      } catch (e) {
        // Non-fatal: panel just hides the criteria column when unavailable.
        console.warn("Failed to fetch job criteria:", e);
      }

      await fetchCandidatesPage(0, true);
    } catch (error) {
      console.error("Error fetching ranking data:", error);
    } finally {
      setIsLoading(false);
    }
  };

  // Ref-based scroll listener to avoid stale closure issues
  const loadMoreCandidatesRef = useRef(loadMoreCandidates);
  useEffect(() => { loadMoreCandidatesRef.current = loadMoreCandidates; }, [loadMoreCandidates]);

  useEffect(() => {
    const container = tableScrollRef.current;
    if (!container) return;

    const onScroll = () => {
      if (!hasMoreCandidates || isLoading || isLoadingMore) return;
      const remaining = container.scrollHeight - container.scrollTop - container.clientHeight;
      if (remaining < 400) {
        loadMoreCandidatesRef.current();
      }
    };

    container.addEventListener("scroll", onScroll, { passive: true });
    return () => container.removeEventListener("scroll", onScroll);
  }, [hasMoreCandidates, isLoading, isLoadingMore]);

  // IntersectionObserver sentinel — fires when the bottom sentinel enters view
  useEffect(() => {
    const sentinel = loadMoreRef.current;
    if (!sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          loadMoreCandidatesRef.current();
        }
      },
      { root: tableScrollRef.current, rootMargin: "200px", threshold: 0 }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  // Re-attach when hasMoreCandidates changes so we stop observing when done
  }, [hasMoreCandidates]);

  const openDetails = (candidate: Candidate) => {
    setSelectedCandidate(candidate);
    setDetailsModalOpen(true);
  };

  const isInitialLoading = isLoading && !job && candidates.length === 0;
  const isRefreshing = isLoading && !isInitialLoading;
  const hasActiveFilters = Boolean(
    searchQuery.trim() ||
    statusFilter !== "all" ||
    activityFilter !== "all" ||
    sourceFilter !== "all" ||
    minScore > 0
  );
  const totalCandidates = candidateTotalCount || candidates.length;
  const isPartiallyLoaded = hasMoreCandidates || candidateOffset < totalCandidates;
  const displayedCount = hasActiveFilters ? filteredCandidates.length : candidates.length;

  return (
    <div className="max-w-[1600px] mx-auto px-2 space-y-4 pb-10">
      {/* Top Navigation */}
      {showBackButton && (
        <div className="pt-2 mb-4">
          <Button
            variant="ghost"
            onClick={() => router.back()}
            className="text-slate-400 hover:text-slate-600 p-0 h-auto font-medium flex items-center gap-1.5 text-[14px]"
          >
            <ArrowLeft className="w-3.5 h-3.5" />
            Back to Jobs Page
          </Button>
        </div>
      )}

      {/* Rankings Page Header matching the premium UI */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 flex flex-col md:flex-row md:items-center justify-between shadow-sm mb-6 gap-6">
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            {isInitialLoading ? (
              <Skeleton className="h-8 w-64 rounded bg-slate-100" />
            ) : (
              <>
                <Medal className="w-8 h-8 text-indigo-600" />
                <h2 className="text-2xl font-bold text-slate-900 m-0 flex items-center gap-2">
                  {job?.title}
                  <span className="text-slate-500 font-medium text-lg">
                    ({job?.jobdiva_id || job?.job_id || jobId})
                    {(job?.jobdiva_numeric_id || job?.jobdiva_id) && (
                      <a
                        href={`https://www1.jobdiva.com/employers/myjobs/vieweditjobform.jsp?lstjobs=1&jobid=${encodeURIComponent((job.jobdiva_numeric_id || job.jobdiva_id || "").replace(/-v\d+$/, ""))}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="Open job in JobDiva"
                        className="inline-flex items-center text-indigo-600 ml-1.5 align-middle hover:text-indigo-800"
                      >
                        <ExternalLink className="w-4 h-4" />
                      </a>
                    )}
                  </span>
                </h2>
              </>
            )}
          </div>
          <div className="text-base text-slate-500 font-medium pl-[44px]">Candidate Rank List</div>
        </div>

        <div className="flex items-center gap-8">
          <div className="flex gap-8 border-r border-slate-200 pr-8">
            <div className="flex flex-col gap-3 text-sm text-slate-600">
              {isInitialLoading ? (
                <>
                  <Skeleton className="h-5 w-48 bg-slate-100" />
                  <Skeleton className="h-5 w-56 bg-slate-100" />
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-slate-300"></div> Candidates Launched: <strong className="text-slate-900 ml-1">{launchedRowCount}</strong>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-slate-300"></div> Openings: <strong className="text-slate-900 ml-1">{!job?.openings ? "—" : job.openings}</strong>
                  </div>
                </>
              )}
            </div>
            <div className="flex flex-col gap-3 text-sm text-slate-600">
              {isInitialLoading ? (
                <>
                  <Skeleton className="h-5 w-48 bg-slate-100" />
                  <Skeleton className="h-5 w-32 bg-slate-100" />
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full bg-slate-300"></div> Max. Allowed Submittals: <strong className="text-slate-900 ml-1">{!job?.max_allowed_submittals ? "—" : job.max_allowed_submittals}</strong>
                  </div>
                </>
              )}
            </div>
          </div>
          <Button
            variant="outline"
            className="w-10 h-10 p-0 flex items-center justify-center text-slate-500 hover:text-slate-800 rounded-lg"
            onClick={fetchData}
            disabled={isLoading}
          >
            <RefreshCw className={`w-5 h-5 ${isRefreshing ? "animate-spin text-indigo-600" : ""}`} />
          </Button>
        </div>
      </div>

      {/* B5: applied filters panel — criteria from Step 3 + sourcing filters
          from Step 5. Read-only, collapsed by default. */}
      {(appliedFilters || criteriaList.length > 0) && (
        <div className="mb-4 bg-white border border-slate-200 rounded-[8px] shadow-sm">
          <button
            type="button"
            onClick={() => setAppliedFiltersOpen(v => !v)}
            className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-slate-50 rounded-[8px] transition-colors"
          >
            <div className="flex items-center gap-2">
              <Filter className="w-4 h-4 text-slate-400" />
              <span className="text-[13px] font-semibold text-slate-700">
                Filters applied at sourcing
              </span>
              <span className="text-[11px] text-slate-400">
                {criteriaList.length > 0 && `${criteriaList.length} criteria`}
                {appliedFilters && criteriaList.length > 0 && " · "}
                {appliedFilters && (() => {
                  const counts: string[] = [];
                  if (appliedFilters.titles?.length) counts.push(`${appliedFilters.titles.length} titles`);
                  if (appliedFilters.skills?.length) counts.push(`${appliedFilters.skills.length} skills`);
                  if (appliedFilters.locations?.length) counts.push(`${appliedFilters.locations.length} locations`);
                  if (appliedFilters.companies?.length) counts.push(`${appliedFilters.companies.length} companies`);
                  return counts.join(" · ");
                })()}
              </span>
            </div>
            {appliedFiltersOpen ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
          </button>

          {appliedFiltersOpen && (
            <div className="border-t border-slate-100 p-4 grid grid-cols-1 md:grid-cols-2 gap-4">
              {/* Column 1 — criteria from Step 3 */}
              <div>
                <h4 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
                  Criteria (Step 3)
                </h4>
                {criteriaList.length === 0 ? (
                  <div className="text-[12px] text-slate-400 italic">
                    No criteria recorded for this job.
                  </div>
                ) : (
                  <ul className="space-y-1.5">
                    {criteriaList.map((c, i) => (
                      <li key={c.id || i} className="flex items-start gap-2 text-[12px]">
                        <span className="flex-shrink-0 inline-flex items-center justify-center w-7 h-5 rounded bg-slate-100 text-slate-700 font-bold text-[10px]">
                          {(c.priority_score ?? 0)}/10
                        </span>
                        <span className="flex-1 text-slate-700">{c.name}</span>
                        <span className={`flex-shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium ${c.is_required ? "bg-indigo-50 text-indigo-700 border border-indigo-200" : "bg-slate-50 text-slate-600 border border-slate-200"}`}>
                          {c.is_required ? "Required" : "Preferred"}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              {/* Column 2 — sourcing filters from Step 5 */}
              <div>
                <h4 className="text-[11px] font-semibold uppercase tracking-wide text-slate-500 mb-2">
                  Sourcing filters (Step 5)
                </h4>
                {!appliedFilters || (
                  !appliedFilters.titles?.length &&
                  !appliedFilters.skills?.length &&
                  !appliedFilters.locations?.length &&
                  !appliedFilters.companies?.length &&
                  !appliedFilters.keywords?.length
                ) ? (
                  <div className="text-[12px] text-slate-400 italic">
                    No structured filters captured for this run.
                  </div>
                ) : (
                  <div className="space-y-2 text-[12px]">
                    {appliedFilters.titles && appliedFilters.titles.length > 0 && (
                      <div>
                        <div className="text-[10px] font-semibold text-slate-500 uppercase mb-1">Titles</div>
                        <div className="flex flex-wrap gap-1.5">
                          {appliedFilters.titles.map((t, i) => {
                            const mt = t.matchType || t.match_type || "must";
                            return (
                              <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-50 border border-slate-200 text-slate-700">
                                <span className="font-medium">{t.value}</span>
                                <span className={`text-[10px] ${mt === "must" ? "text-indigo-600" : "text-slate-400"}`}>{mt}</span>
                                {t.years && t.years > 0 ? <span className="text-[10px] text-slate-500">≥{t.years}y</span> : null}
                                {t.recent ? <span className="text-[10px] text-emerald-600">recent</span> : null}
                              </span>
                            );
                          })}
                        </div>
                      </div>
                    )}
                    {appliedFilters.skills && appliedFilters.skills.length > 0 && (
                      <div>
                        <div className="text-[10px] font-semibold text-slate-500 uppercase mb-1">Skills</div>
                        <div className="flex flex-wrap gap-1.5">
                          {appliedFilters.skills.map((s, i) => {
                            const mt = s.matchType || s.match_type || "must";
                            return (
                              <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-50 border border-slate-200 text-slate-700">
                                <span className="font-medium">{s.value}</span>
                                <span className={`text-[10px] ${mt === "must" ? "text-indigo-600" : "text-slate-400"}`}>{mt}</span>
                                {s.years && s.years > 0 ? <span className="text-[10px] text-slate-500">≥{s.years}y</span> : null}
                                {s.recent ? <span className="text-[10px] text-emerald-600">recent</span> : null}
                              </span>
                            );
                          })}
                        </div>
                      </div>
                    )}
                    {appliedFilters.locations && appliedFilters.locations.length > 0 && (
                      <div>
                        <div className="text-[10px] font-semibold text-slate-500 uppercase mb-1">Locations</div>
                        <div className="flex flex-wrap gap-1.5">
                          {appliedFilters.locations.map((l, i) => (
                            <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-slate-50 border border-slate-200 text-slate-700">
                              <span className="font-medium">{l.value}</span>
                              {l.radius ? <span className="text-[10px] text-slate-500">{l.radius}</span> : null}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    {appliedFilters.companies && appliedFilters.companies.length > 0 && (
                      <div>
                        <div className="text-[10px] font-semibold text-slate-500 uppercase mb-1">Companies</div>
                        <div className="flex flex-wrap gap-1.5">
                          {appliedFilters.companies.map((c, i) => (
                            <span key={i} className="px-2 py-0.5 rounded bg-slate-50 border border-slate-200 text-slate-700 font-medium">
                              {c}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    {appliedFilters.keywords && appliedFilters.keywords.length > 0 && (
                      <div>
                        <div className="text-[10px] font-semibold text-slate-500 uppercase mb-1">Keywords</div>
                        <div className="flex flex-wrap gap-1.5">
                          {appliedFilters.keywords.map((k, i) => (
                            <span key={i} className="px-2 py-0.5 rounded bg-slate-50 border border-slate-200 text-slate-700">
                              {k}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Table Interface */}
      <div className="space-y-4">
        {/* Filter bar: search + status + source + min-score. All filter state
            feeds into the `filteredCandidates` useMemo above. */}
        <div className="flex flex-wrap items-center gap-2 bg-white px-3 py-2 rounded-xl border border-slate-200 shadow-sm mb-6">
          <div className="relative shrink-0 min-w-[260px] flex-1 max-w-[380px]">
            <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
              <Search className="h-4 w-4 text-slate-400" />
            </div>
            <Input
              placeholder="Search name, email, or location..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-9 pl-9 pr-3 w-full bg-slate-50 border-transparent focus:bg-white rounded-lg text-[12px] focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500 transition-all"
            />
          </div>

          <div className="flex items-center gap-1.5 bg-slate-50 rounded-lg px-3 h-9 border border-transparent focus-within:bg-white focus-within:border-indigo-500 shrink-0">
            <Filter className="w-3.5 h-3.5 text-slate-400" />
            <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap">Status</label>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
              className="text-[12px] font-semibold text-slate-800 bg-transparent focus:outline-none cursor-pointer pr-1 w-[90px]"
            >
              <option value="all">All</option>
              <option value="pass">Pass</option>
              <option value="fail">Fail</option>
              <option value="in_progress">In Progress</option>
              <option value="pending">Pending</option>
              <option value="n/a">N/A</option>
            </select>
          </div>

          <div className="flex items-center gap-1.5 bg-slate-50 rounded-lg px-3 h-9 border border-transparent focus-within:bg-white focus-within:border-indigo-500 shrink-0">
            <Filter className="w-3.5 h-3.5 text-slate-400" />
            <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap">Source</label>
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="text-[12px] font-semibold text-slate-800 bg-transparent focus:outline-none cursor-pointer pr-1 w-[110px]"
            >
              <option value="all">All</option>
              {availableSources.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className={`flex items-center gap-1.5 rounded-lg px-3 h-9 border transition-all cursor-pointer select-none shrink-0 ${activityFilter === "has_activity" ? "bg-indigo-50 border-indigo-400 text-indigo-700" : "bg-slate-50 border-transparent hover:bg-slate-100 text-slate-500"}`}
            onClick={() => setActivityFilter(activityFilter === "has_activity" ? "all" : "has_activity")}
            title="Show only candidates with activity history"
          >
            <Activity className="w-3.5 h-3.5" />
            <label className="text-[11px] font-semibold uppercase tracking-wider cursor-pointer whitespace-nowrap">Activity History</label>
          </div>

          <div className="flex items-center gap-1.5 bg-slate-50 rounded-lg px-3 h-9 border border-transparent focus-within:bg-white focus-within:border-indigo-500 shrink-0">
            <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap">Min score</label>
            <Input
              type="number"
              min={0}
              max={100}
              value={minScore}
              onChange={(e) => {
                const n = Number.parseInt(e.target.value, 10);
                setMinScore(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0);
              }}
              className="h-7 w-14 text-[12px] font-bold bg-white border-slate-200 rounded px-2 text-center"
            />
          </div>

          {hasActiveFilters && (
            <button
              onClick={clearFilters}
              className="h-9 px-3 text-[12px] font-bold text-slate-500 hover:text-slate-800 hover:bg-slate-100 rounded-lg flex items-center gap-1.5 transition-colors shrink-0"
            >
              <X className="w-3.5 h-3.5" /> Clear
            </button>
          )}

          <div className="ml-auto text-[12px] font-bold text-slate-500 px-2 text-right shrink-0 whitespace-nowrap">
            {(hasActiveFilters || isPartiallyLoaded) ? (
              <>
                {hasActiveFilters ? "Matching" : "Showing"} <span className="text-slate-900">{displayedCount}</span> of <span className="text-slate-900">{totalCandidates}</span>
                <span className="text-slate-500"> total candidates</span>
              </>
            ) : (
              <>
                Showing <span className="text-slate-900">{totalCandidates}</span>
                <span className="text-slate-500"> total candidates</span>
              </>
            )}
            {isPartiallyLoaded && (
              <div className="text-[11px] font-medium text-slate-400">Loaded {candidates.length} so far</div>
            )}
          </div>
        </div>

        {/* HTML Exact Replica Table */}
        <div className="bg-white rounded-2xl border border-slate-200/60 shadow-sm relative max-w-full">
          <div
            ref={tableScrollRef}
            className="overflow-x-auto overflow-y-auto rounded-2xl pb-2 scrollbar-thin scrollbar-thumb-slate-200 scrollbar-track-transparent"
            style={{ maxHeight: 'calc(100vh - 320px)' }}
          >
            <table className="table-fixed min-w-[1200px] w-full border-separate border-spacing-0">
              <TableHeader className="z-30 shadow-sm bg-slate-50">
                <TableRow className="bg-slate-50 border-b border-slate-200 hover:bg-slate-50 h-[50px] transition-colors">
                  <TableHead className="w-[50px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider border-r border-b border-slate-200 px-2">#</TableHead>
                  <TableHead className="w-[320px] sticky top-0 left-0 z-40 bg-slate-50 font-semibold text-slate-500 text-[12px] uppercase tracking-wider py-0 text-center border-b border-slate-200 after:absolute after:inset-y-0 after:right-0 after:w-[1px] after:bg-slate-200">
                    <div className="flex items-center justify-center w-full h-full group/header relative">
                      <button
                        onClick={() => toggleSort("name")}
                        className="flex items-center justify-center h-full px-3 cursor-pointer hover:bg-slate-100 transition-colors flex-1"
                      >
                        <span className="whitespace-nowrap">CANDIDATE NAME</span>
                        <div className="flex items-center gap-1 ml-2">
                          {sortField === "name"
                            ? (sortDir === "asc" ? <ChevronUp className="w-4 h-4 text-indigo-600" /> : <ChevronDown className="w-4 h-4 text-indigo-600" />)
                            : <ChevronsUpDown className="w-4 h-4 opacity-40" />}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setActiveFilterField(activeFilterField === "name" ? null : "name"); }}
                        className={`p-1 mr-2 rounded hover:bg-slate-200 transition-colors ${columnFilters["name"]?.value ? 'text-indigo-600' : 'text-slate-400'}`}
                        title="Filter Candidate Name"
                      >
                        <Filter className="w-3.5 h-3.5" />
                      </button>
                      {activeFilterField === "name" && (
                        <ColumnFilterPopup
                          field="name" label="CANDIDATE NAME"
                          onClose={() => setActiveFilterField(null)}
                          onApply={(f) => { setColumnFilters(p => ({ ...p, name: f })); setActiveFilterField(null); }}
                          onClear={() => { setColumnFilters(p => { const n = { ...p }; delete n.name; return n; }); setActiveFilterField(null); }}
                          currentFilter={columnFilters["name"]}
                        />
                      )}
                    </div>
                  </TableHead>
                  <TableHead className="w-[160px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider py-0 border-l border-b border-slate-200">
                    <div className="flex items-center justify-center w-full h-full group/header relative">
                      <button
                        onClick={() => toggleSort("source")}
                        className="flex items-center justify-center h-full px-4 cursor-pointer hover:bg-slate-100 transition-colors flex-1"
                      >
                        <span>SOURCE</span>
                        <div className="flex items-center gap-1 ml-2">
                          {sortField === "source"
                            ? (sortDir === "asc" ? <ChevronUp className="w-4 h-4 text-indigo-600" /> : <ChevronDown className="w-4 h-4 text-indigo-600" />)
                            : <ChevronsUpDown className="w-4 h-4 opacity-40" />}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setActiveFilterField(activeFilterField === "source" ? null : "source"); }}
                        className={`p-1 mr-1 rounded hover:bg-slate-200 transition-colors ${columnFilters["source"]?.value ? 'text-indigo-600' : 'text-slate-400'}`}
                        title="Filter Source"
                      >
                        <Filter className="w-3.5 h-3.5" />
                      </button>
                      {activeFilterField === "source" && (
                        <ColumnFilterPopup
                          field="source" label="SOURCE"
                          onClose={() => setActiveFilterField(null)}
                          onApply={(f) => { setColumnFilters(p => ({ ...p, source: f })); setActiveFilterField(null); }}
                          onClear={() => { setColumnFilters(p => { const n = { ...p }; delete n.source; return n; }); setActiveFilterField(null); }}
                          currentFilter={columnFilters["source"]}
                        />
                      )}
                    </div>
                  </TableHead>

                  <TableHead className="w-[260px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider py-0 border-l border-b border-slate-200">
                    <div className="flex items-center justify-center w-full h-full group/header relative">
                      <button
                        onClick={() => toggleSort("screening_score")}
                        className="flex items-center justify-center h-full px-4 cursor-pointer hover:bg-slate-100 transition-colors flex-1"
                      >
                        <span>RESUME SCREENING SCORE</span>
                        <div className="flex items-center gap-1 ml-2">
                          {sortField === "screening_score"
                            ? (sortDir === "asc" ? <ChevronUp className="w-4 h-4 text-indigo-600" /> : <ChevronDown className="w-4 h-4 text-indigo-600" />)
                            : <ChevronsUpDown className="w-4 h-4 opacity-40" />}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setActiveFilterField(activeFilterField === "screening_score" ? null : "screening_score"); }}
                        className={`p-1 mr-1 rounded hover:bg-slate-200 transition-colors ${columnFilters["screening_score"]?.value ? 'text-indigo-600' : 'text-slate-400'}`}
                        title="Filter Resume Screening Score"
                      >
                        <Filter className="w-3.5 h-3.5" />
                      </button>
                      {activeFilterField === "screening_score" && (
                        <ColumnFilterPopup
                          field="screening_score" label="SCREENING SCORE"
                          onClose={() => setActiveFilterField(null)}
                          onApply={(f) => { setColumnFilters(p => ({ ...p, screening_score: f })); setActiveFilterField(null); }}
                          onClear={() => { setColumnFilters(p => { const n = { ...p }; delete n.screening_score; return n; }); setActiveFilterField(null); }}
                          currentFilter={columnFilters["screening_score"]}
                        />
                      )}
                    </div>
                  </TableHead>
                  <TableHead className="w-[200px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider py-0 border-l border-b border-slate-200">
                    <div className="flex items-center justify-center w-full h-full group/header relative">
                      <button
                        onClick={() => toggleSort("engage_status")}
                        className="flex items-center justify-center h-full px-4 cursor-pointer hover:bg-slate-100 transition-colors flex-1"
                      >
                        <span>ENGAGE STATUS</span>
                        <div className="flex items-center gap-1 ml-2">
                          {sortField === "engage_status"
                            ? (sortDir === "asc" ? <ChevronUp className="w-4 h-4 text-indigo-600" /> : <ChevronDown className="w-4 h-4 text-indigo-600" />)
                            : <ChevronsUpDown className="w-4 h-4 opacity-40" />}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setActiveFilterField(activeFilterField === "engage_status" ? null : "engage_status"); }}
                        className={`p-1 mr-1 rounded hover:bg-slate-200 transition-colors ${columnFilters["engage_status"]?.value ? 'text-indigo-600' : 'text-slate-400'}`}
                        title="Filter Engage Status"
                      >
                        <Filter className="w-3.5 h-3.5" />
                      </button>
                      {activeFilterField === "engage_status" && (
                        <ColumnFilterPopup
                          field="engage_status" label="ENGAGE STATUS"
                          onClose={() => setActiveFilterField(null)}
                          onApply={(f) => { setColumnFilters(p => ({ ...p, engage_status: f })); setActiveFilterField(null); }}
                          onClear={() => { setColumnFilters(p => { const n = { ...p }; delete n.engage_status; return n; }); setActiveFilterField(null); }}
                          currentFilter={columnFilters["engage_status"]}
                        />
                      )}
                    </div>
                  </TableHead>

                  <TableHead className="w-[200px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider py-0 border-l border-b border-slate-200">
                    <div className="flex items-center justify-center w-full h-full group/header relative">
                      <button
                        onClick={() => toggleSort("engage_score")}
                        className="flex items-center justify-center h-full px-4 cursor-pointer hover:bg-slate-100 transition-colors flex-1"
                      >
                        <span>ENGAGE SCORE</span>
                        <div className="flex items-center gap-1 ml-2">
                          {sortField === "engage_score"
                            ? (sortDir === "asc" ? <ChevronUp className="w-4 h-4 text-indigo-600" /> : <ChevronDown className="w-4 h-4 text-indigo-600" />)
                            : <ChevronsUpDown className="w-4 h-4 opacity-40" />}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setActiveFilterField(activeFilterField === "engage_score" ? null : "engage_score"); }}
                        className={`p-1 mr-1 rounded hover:bg-slate-200 transition-colors ${columnFilters["engage_score"]?.value ? 'text-indigo-600' : 'text-slate-400'}`}
                        title="Filter Engage Score"
                      >
                        <Filter className="w-3.5 h-3.5" />
                      </button>
                      {activeFilterField === "engage_score" && (
                        <ColumnFilterPopup
                          field="engage_score" label="ENGAGE SCORE"
                          align="right"
                          onClose={() => setActiveFilterField(null)}
                          onApply={(f) => { setColumnFilters(p => ({ ...p, engage_score: f })); setActiveFilterField(null); }}
                          onClear={() => { setColumnFilters(p => { const n = { ...p }; delete n.engage_score; return n; }); setActiveFilterField(null); }}
                          currentFilter={columnFilters["engage_score"]}
                        />
                      )}
                    </div>
                  </TableHead>

                  <TableHead className="w-[220px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider py-0 border-l border-b border-slate-200">
                    <div className="flex items-center justify-center w-full h-full group/header relative">
                      <button
                        onClick={() => toggleSort("total_score")}
                        className="flex items-center justify-center h-full px-4 cursor-pointer hover:bg-slate-100 transition-colors flex-1"
                      >
                        <span>TOTAL FIT SCORE</span>
                        <div className="flex items-center gap-1 ml-2">
                          {sortField === "total_score"
                            ? (sortDir === "asc" ? <ChevronUp className="w-4 h-4 text-indigo-600" /> : <ChevronDown className="w-4 h-4 text-indigo-600" />)
                            : <ChevronsUpDown className="w-4 h-4 opacity-40" />}
                        </div>
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setActiveFilterField(activeFilterField === "total_score" ? null : "total_score"); }}
                        className={`p-1 mr-1 rounded hover:bg-slate-200 transition-colors ${columnFilters["total_score"]?.value ? 'text-indigo-600' : 'text-slate-400'}`}
                        title="Filter Total Fit Score"
                      >
                        <Filter className="w-3.5 h-3.5" />
                      </button>
                      {activeFilterField === "total_score" && (
                        <ColumnFilterPopup
                          field="total_score" label="FIT SCORE"
                          align="right"
                          onClose={() => setActiveFilterField(null)}
                          onApply={(f) => { setColumnFilters(p => ({ ...p, total_score: f })); setActiveFilterField(null); }}
                          onClear={() => { setColumnFilters(p => { const n = { ...p }; delete n.total_score; return n; }); setActiveFilterField(null); }}
                          currentFilter={columnFilters["total_score"]}
                        />
                      )}
                    </div>
                  </TableHead>

                  <TableHead className="w-[260px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider border-l border-b border-slate-200 py-0 px-2">
                    ACTIONS
                  </TableHead>
                  <TableHead className="w-[220px] sticky top-0 z-30 bg-slate-50 text-center font-semibold text-slate-500 text-[12px] uppercase tracking-wider border-l border-b border-slate-200 py-0 px-2">
                    CANDIDATE FEEDBACK
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className={isRefreshing ? "opacity-60 transition-opacity duration-300 pointer-events-none" : ""}>
                {isInitialLoading ? (
                  Array.from({ length: 8 }).map((_, i) => (
                    <TableRow key={i} className="h-20 bg-white">
                      <TableCell className="w-[50px] border-r border-slate-200/50 px-2 text-center"><Skeleton className="h-4 w-4 mx-auto" /></TableCell>
                      <TableCell className="w-[320px] sticky left-0 z-10 bg-white px-3 after:absolute after:inset-y-0 after:right-0 after:w-[1px] after:bg-slate-200"><Skeleton className="h-10 w-48 mx-auto" /></TableCell>
                      <TableCell className="w-[160px] border-l border-slate-100 text-center"><Skeleton className="h-6 w-20 mx-auto" /></TableCell>
                      <TableCell className="w-[260px] border-l border-slate-100 text-center"><Skeleton className="h-8 w-16 mx-auto" /></TableCell>
                      <TableCell className="w-[200px] border-l border-slate-100 text-center"><Skeleton className="h-6 w-24 mx-auto" /></TableCell>
                      <TableCell className="w-[200px] border-l border-slate-100 text-center"><Skeleton className="h-6 w-12 mx-auto" /></TableCell>
                      <TableCell className="w-[220px] border-l border-slate-100 text-center"><Skeleton className="h-6 w-12 mx-auto" /></TableCell>
                      <TableCell className="w-[260px] border-l border-slate-100 text-center"><Skeleton className="h-9 w-32 mx-auto" /></TableCell>
                      <TableCell className="w-[220px] border-l border-slate-100 text-center"><Skeleton className="h-9 w-32 mx-auto" /></TableCell>
                    </TableRow>
                  ))
                ) : (
                  filteredCandidates.map((candidate, idx) => {
                    const candidateKey = `${candidate.id || candidate.candidate_id || idx}`;
                    // Keep this in sync with the sort comparator's getScore so the
                    // displayed number always matches the sorted order.
                    const screeningScore = candidate.match_score ?? candidate.resume_match_percentage ?? 0;
                    const showEngageScore = hasFinalEngageOutcome(candidate);
                    const engageScore = showEngageScore ? (candidate.engage_score || 0) : 0;
                    const totalScore = showEngageScore ? Math.round((screeningScore + engageScore) / 2 * 10) / 10 : null;

                    return (
                      <TableRow key={`${candidateKey}-${idx}`} className="border-b border-slate-100 hover:bg-slate-50 transition-all duration-200 h-auto group leading-tight relative">
                        <TableCell className="w-[50px] border-r border-slate-200 py-2 px-2 align-middle text-center font-medium text-slate-500 text-[12px] group-hover:bg-slate-50 transition-colors">
                          {idx + 1}
                        </TableCell>
                        <TableCell className="sticky left-0 z-10 bg-white w-[320px] py-2 px-3 align-middle text-center group-hover:bg-slate-50 transition-colors after:absolute after:inset-y-0 after:right-0 after:w-[1px] after:bg-slate-200">
                          <Link
                            href={`/jobs/${jobId}/report?candidateId=${encodeURIComponent(candidate.candidate_id || candidate.id)}`}
                            className="text-[14px] font-bold text-indigo-600 hover:underline text-center w-full block mb-1"
                          >
                            {candidate.name}
                          </Link>
                          <span className="text-[12px] text-slate-500 block mb-0.5 text-center px-1 break-all whitespace-normal" title={candidate.email}>
                            <Mail className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.email || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <span className="text-[12px] text-slate-500 block mb-0.5 text-center">
                            <Phone className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.phone || <span className="font-normal opacity-50">—</span>}
                          </span>
                          {deriveInterviewId(candidate) && (
                            <button
                              type="button"
                              onClick={() => {
                                setSelectedCandidateForActivity({
                                  id: deriveInterviewId(candidate)!,
                                  name: candidate.name,
                                });
                                setIsActivityLogModalOpen(true);
                              }}
                              className="text-[12px] text-indigo-600 hover:bg-indigo-50 px-2 py-0.5 rounded-md inline-flex items-center justify-center gap-1 mt-1 font-bold border border-indigo-100 shadow-sm transition-colors"
                              title="View user activity history"
                            >
                              <Activity className="w-3.5 h-3.5" />
                              Activity History
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => openCandidateProfileUrl(candidate)}
                            className="text-[12px] text-indigo-600 hover:underline inline-flex items-center justify-center gap-1 mt-1 font-medium"
                            title={String(candidate.source || "").toLowerCase().includes("linkedin") ? "Open LinkedIn profile" : "Open JobDiva profile"}
                          >
                            <ExternalLink className="w-3.5 h-3.5" />
                            {String(candidate.source || "").toLowerCase().includes("linkedin") ? "LinkedIn URL" : "JobDiva URL"}
                          </button>
                          {needsContactEnrichment(candidate) && (
                            <div className="text-center mt-2">
                              {(() => {
                                const cid = String(candidate.candidate_id || candidate.id || "").trim();
                                const status = enrichStatusByCandidateId[cid];
                                const hoverStatus = status
                                  ? compactEnrichStatusMessage(status)
                                  : "Fetch missing phone/email from ZoomInfo";
                                return (
                                  <Button
                                    size="sm"
                                    className="h-6 px-2 bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white font-bold text-[10px] rounded-md shadow-sm"
                                    onClick={() => handleEnrichContact(candidate)}
                                    disabled={enrichingCandidateIds.has(String(candidate.candidate_id || candidate.id || ""))}
                                    title={hoverStatus}
                                  >
                                    {enrichingCandidateIds.has(String(candidate.candidate_id || candidate.id || "")) ? (
                                      <>
                                        <Loader2 className="w-3 h-3 mr-1 animate-spin" />
                                        Checking...
                                      </>
                                    ) : (
                                      <>
                                        <RefreshCw className="w-3 h-3 mr-1" />
                                        Get Contact
                                      </>
                                    )}
                                  </Button>
                                );
                              })()}
                            </div>
                          )}
                          <span className={`text-[12px] block text-center mt-1.5 ${availabilityPillClasses(deriveAvailability(candidate))}`}>
                            <Calendar className="w-3.5 h-3.5 inline mr-1 opacity-70" /> Available: {deriveAvailability(candidate) || <span className="font-normal opacity-50">—</span>}
                          </span>
                          {candidate.data?.open_to_relocation && (
                            <span className="text-[11px] inline-block mt-1 px-2 py-0.5 rounded-md bg-amber-50 text-amber-700 border border-amber-200 font-medium">
                              Open to Relocation
                            </span>
                          )}
                        </TableCell>

                        <TableCell className="text-center align-middle py-2 px-2 border-l border-slate-200">
                          <span className="text-[12px] font-semibold text-slate-700">
                            {normalizeSourceLabel(candidate.source)}
                          </span>
                        </TableCell>



                        <TableCell
                          className="text-center align-middle py-2 px-2 font-medium text-slate-900 text-[13px] border-l border-slate-200"
                          onMouseEnter={() => {
                            if (screeningScore > 0) setHoveredResumeScoreKey(candidateKey);
                          }}
                          onMouseLeave={() => setHoveredResumeScoreKey((prev) => (prev === candidateKey ? null : prev))}
                        >
                          <div className="relative flex items-center justify-center gap-1 w-full text-center">
                            {screeningScore > 0 ? (
                              <span className="inline-flex items-center justify-center">
                                <span className="font-bold text-slate-900 text-[14px] underline decoration-indigo-200 underline-offset-4">
                                  {screeningScore}/100
                                </span>
                              </span>
                            ) : (
                              <span className="font-normal opacity-40 italic text-slate-400 text-[13px]">
                                {String(candidate.source || "").toLowerCase().includes("applicant") ? "N/A" : "Pending"}
                              </span>
                            )}
                            {screeningScore > 0 ? (
                              <ResumeScreeningHoverCard
                                candidate={candidate}
                                open={hoveredResumeScoreKey === candidateKey}
                              />
                            ) : null}
                          </div>
                        </TableCell>

                        <TableCell className="text-center align-middle py-3 px-2 group-hover:bg-indigo-50/5 transition-colors border-l border-slate-200">
                          {(() => {
                            const rawStatus = String(candidate.engage_status || candidate.data?.engage_status || "").trim().toLowerCase();
                            // If in outreach phase, show the timeline
                            if (rawStatus === "initiated" || rawStatus === "sent" || rawStatus === "sms sent") {
                              return (
                                <div className="flex flex-col items-center gap-1 py-1">
                                  <div className="flex items-center gap-1.5 mb-1">
                                    <div className="w-2 h-2 rounded-full bg-yellow-500 animate-pulse" />
                                    <span className="text-[11px] font-bold text-yellow-600 tracking-wide">Initiated</span>
                                  </div>
                                  {(candidate.engage_created_at || candidate.data?.engage_created_at) && (
                                    <div className="flex flex-col items-center gap-1 px-3 py-1.5 rounded-lg bg-slate-50 border border-slate-100">
                                      <div className="text-[11px] text-emerald-600 flex items-center gap-1 font-semibold" title="Outreach initiated">
                                        <Mail className="w-3 h-3" /> {formatDate(candidate.engage_created_at || candidate.data?.engage_created_at)}
                                      </div>
                                      {(() => {
                                        const baseTime = candidate.engage_created_at || candidate.data?.engage_created_at;
                                        const phoneTime = new Date(new Date(baseTime).getTime() + 30 * 60000);
                                        const isActive = new Date() > phoneTime;
                                        return (
                                          <div
                                            className={`text-[11px] flex items-center gap-1 font-semibold ${isActive ? 'text-blue-600' : 'text-slate-400'}`}
                                            title={isActive ? "Follow-up triggered" : "Scheduled follow-up"}
                                          >
                                            <Phone className="w-3 h-3" /> {formatDate(phoneTime.toISOString())}
                                          </div>
                                        );
                                      })()}
                                    </div>
                                  )}
                                </div>
                              );
                            }
                            const { label, color } = normalizeInterviewStatus(candidate);
                            return (
                              <div className="flex justify-center items-center w-full">
                                <span
                                  className="px-3 py-1 rounded-full text-[11px] font-bold border"
                                  style={{ backgroundColor: `${color}08`, color, borderColor: `${color}30` }}
                                >
                                  {label}
                                </span>
                              </div>
                            );
                          })()}
                        </TableCell>



                        <TableCell
                          className="text-center align-middle py-3 px-2 font-medium text-slate-700 text-[13px] transition-colors border-l border-slate-200"
                          onMouseEnter={() => {
                            if (showEngageScore) setHoveredEngageScoreKey(candidateKey);
                          }}
                          onMouseLeave={() => setHoveredEngageScoreKey((prev) => (prev === candidateKey ? null : prev))}
                        >
                          {(() => {
                            const eScore = candidate.engage_score;
                            const eTotal = candidate.engage_total_score || 100;
                            const hardFilterDetails = candidate.engage_hard_filter_details || [];

                            if (showEngageScore && eScore !== undefined && eScore !== null) {
                              return (
                                <div className="relative flex items-center justify-center w-full">
                                  <span className="font-bold text-slate-900 text-[14px] underline decoration-indigo-200 underline-offset-4">
                                    {eScore}/{eTotal}
                                  </span>
                                  <HardFilterHoverCard
                                    details={hardFilterDetails}
                                    open={hoveredEngageScoreKey === candidateKey}
                                  />
                                </div>
                              );
                            }
                            return <span className="font-normal opacity-40 italic text-[13px]">Waiting</span>;
                          })()}
                        </TableCell>



                        <TableCell className="text-center font-bold text-slate-900 text-[14px] align-middle py-3 px-2 transition-colors border-l border-slate-200">
                          {totalScore !== null ? (
                            <span>{totalScore}/100</span>
                          ) : (
                            <span className="font-normal opacity-40 italic text-[13px]">Waiting</span>
                          )}
                        </TableCell>



                        <TableCell className="text-center pr-3 pl-3 border-l border-slate-200 py-3 align-middle transition-colors group-hover:bg-indigo-50/5">
                          <div className="flex flex-wrap items-center justify-center gap-2">

                            <Button
                              size="sm"
                              className="h-8 px-3 bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white font-bold text-[11px] rounded-md shadow-sm transition-all duration-200"
                              onClick={() => handleScreenClick(candidate)}
                              disabled={screeningCandidateIds.has(String(candidate.candidate_id || candidate.id || ""))}
                            >
                              <MessageSquare className="w-4 h-4 mr-1.5" />
                              Screen
                            </Button>
                            <Button
                              size="sm"
                              className="h-8 px-3 bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white font-bold text-[11px] rounded-md shadow-sm transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"

                              onClick={() => handleSmsCandidate(candidate)}
                              disabled={candidate.engage_status === "Initiated" || candidate.engage_status === "sent" || candidate.engage_status === "SMS Sent"}
                              title={(candidate.engage_status === "Initiated" || candidate.engage_status === "sent" || candidate.engage_status === "SMS Sent") ? "Outreach already initiated" : ""}
                            >
                              <Send className="w-3.5 h-3.5 mr-1" />
                              SMS
                            </Button>
                          </div>
                        </TableCell>

                        <TableCell className="text-center pr-4 pl-4 border-l border-slate-200 py-3 align-middle transition-colors group-hover:bg-indigo-50/5">
                          <div className="flex flex-col items-center gap-2">
                            <Select
                              value={feedbacks[candidate.id]?.startsWith("Reject") ? "Reject" : feedbacks[candidate.id] || undefined}
                              onValueChange={(val) => {
                                if (val === "Reject") {
                                  setActionCandidateId(candidate.id);
                                  setRejectReason("");
                                  setIntegrationModalOpen('reject');
                                } else if (val === "Submit") {
                                  setActionCandidateId(candidate.id);
                                  setIntegrationModalOpen('submit');
                                }
                              }}
                            >
                              <SelectTrigger className="w-[140px] h-8 text-[12px] font-semibold text-slate-700 bg-white border-slate-300 hover:border-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500">
                                <SelectValue placeholder="Select Action..." />
                              </SelectTrigger>
                              <SelectContent>
                                <SelectItem value="Submit" className="text-[12px] font-semibold cursor-pointer">Submit</SelectItem>
                                <SelectItem value="Reject" className="text-[12px] font-semibold cursor-pointer">Reject</SelectItem>
                              </SelectContent>
                            </Select>
                            {feedbacks[candidate.id] && (
                              <div className={`text-[9px] font-bold flex items-center justify-center gap-1 whitespace-nowrap ${feedbacks[candidate.id] === 'Submit' ? 'text-indigo-600' : 'text-rose-600'}`}>
                                {feedbacks[candidate.id] === 'Submit' ? <><Check className="w-3 h-3" /> Submitted</> : <><X className="w-3 h-3" /> Rejected</>}
                              </div>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </table>

            {/* Sentinel div — IntersectionObserver watches this to trigger next page load */}
            <div ref={loadMoreRef} className="h-1" aria-hidden="true" />

            {/* In-table loading indicator */}
            {isLoadingMore && (
              <div className="flex items-center justify-center gap-2.5 py-5 border-t border-slate-100 bg-white">
                <Loader2 className="w-4 h-4 animate-spin text-indigo-500" />
                <span className="text-[12px] font-semibold text-slate-500">Loading more candidates…</span>
              </div>
            )}

            {/* Manual load-more button as fallback when auto-scroll hasn't triggered */}
            {!isLoadingMore && hasMoreCandidates && (
              <div className="flex flex-col items-center gap-2 py-4 border-t border-slate-100 bg-white">
                <p className="text-[11px] text-slate-400">
                  Showing {candidates.length} of {candidateTotalCount} candidates
                </p>
                <button
                  onClick={loadMoreCandidates}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-lg border border-indigo-200 bg-indigo-50 text-indigo-700 text-[12px] font-bold hover:bg-indigo-100 transition-colors shadow-sm"
                >
                  <ChevronDown className="w-4 h-4" />
                  Load more
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Modals */}
      {selectedCandidate && (
        <CandidateDetailsModal
          isOpen={detailsModalOpen}
          onClose={() => setDetailsModalOpen(false)}
          candidateName={selectedCandidate.name}
          profileUrl={selectedCandidate.profile_url}
          imageUrl={selectedCandidate.image_url}
          jobTitle={selectedCandidate.headline}
          location={selectedCandidate.location}
          workLocation={selectedCandidate.work_location}
          experienceYears={selectedCandidate.data?.experience_years}
          matchScore={selectedCandidate.match_score}
          matchScoreDetails={selectedCandidate.data?.match_score_details}
          matchedSkills={selectedCandidate.data?.matched_skills}
          missingSkills={selectedCandidate.data?.missing_skills}
          explainability={selectedCandidate.data?.explainability}
          jobdivaCandidateId={selectedCandidate.jobdiva_candidate_id || selectedCandidate.data?.jobdiva_candidate_id}
          source={selectedCandidate.source}
        />
      )}

      {selectedCandidateForEmail && (
        <CandidateMessageModal
          candidateName={selectedCandidateForEmail.name}
          candidateEmail={selectedCandidateForEmail.email || "Email not available"}
          isOpen={messageModalOpen}
          onClose={() => {
            setMessageModalOpen(false);
            setSelectedCandidateForEmail(null);
          }}
        />
      )}

      <EngageWizardModal
        open={isScreenModalOpen}
        onClose={handleScreenModalClose}
        initialPayload={screenPayload}
        candidateIds={selectedScreenCandidateIds}
        onSend={async (payload) => {
          setScreenPayload(payload);
          await handleSendScreen(payload);
        }}
        loading={selectedScreenCandidateIds.some(id => screeningCandidateIds.has(id))}
        error={screenError}
        successData={screenApiResponse}
      />

      <MissingPhonesModal
        open={missingPhonesOpen}
        candidates={missingPhoneCandidates}
        onClose={() => {
          setMissingPhonesOpen(false);
          setPendingScreenCandidate(null);
        }}
        onAllProvided={async (phones) => {
          setMissingPhonesOpen(false);
          const cand = pendingScreenCandidate;
          setPendingScreenCandidate(null);
          if (!cand) return;
          const cid = String(cand.candidate_id || cand.id);
          const picked = phones[cid] || cand.phone || "";

          if (picked && picked !== cand.phone) {
            try {
              // Passing candidate_id in the body bypasses strict URL path decoders on QA
              await authFetch(`${API_BASE}/candidates/${encodeURIComponent(cid)}/phone`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  phone: picked,
                  jobdiva_id: cand.jobdiva_id || String(jobId || ""),
                  candidate_id: cid
                }),
              });
            } catch (err) {
              console.error("Failed to save phone number:", err);
            }
          }

          const next = { ...cand, phone: picked };
          setCandidates(prev => prev.map(c => String(c.candidate_id || c.id) === cid ? next : c));
          await runScreen(next);
        }}
        title="Phone number required"
        description="PAIR can only call candidates with a phone number on file. Add it below to continue."
        primaryLabel="Save & Screen"
        jobId={String(jobId || "")}
        jobDivaId={job?.jobdiva_id || String(jobId || "")}
      />

      {/* Integration Modals */}
      {integrationModalOpen && actionCandidateId && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg overflow-hidden border border-slate-200">
            {integrationModalOpen === 'submit' ? (
              <>
                <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
                    <ExternalLink className="w-5 h-5 text-indigo-600" />
                    Submit to JobDiva
                  </h3>
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600">×</button>
                </div>
                <div className="p-6 space-y-4">
                  <p className="text-sm text-slate-500">
                    This action will initiate an <strong className="text-slate-900 font-semibold">external submission in JobDiva</strong> for:
                  </p>
                  <div className="bg-slate-50 p-4 rounded-xl border border-slate-100 space-y-3 text-sm text-slate-700">
                    <div className="flex items-center gap-2.5">
                      <User className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Candidate:</strong> {candidates.find(c => c.id === actionCandidateId)?.name}</p>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Briefcase className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Job:</strong> {job?.title} ({job?.jobdiva_id || job?.job_id || jobId})</p>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Building2 className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Client:</strong> {job?.customer_name || "—"}</p>
                    </div>
                    <div className="flex items-center gap-2.5">
                      <Zap className="w-4 h-4 text-slate-400" />
                      <p><strong className="text-slate-900">Action:</strong> Create external submission record in JobDiva</p>
                    </div>
                  </div>
                </div>
                <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
                  <Button variant="outline" onClick={() => setIntegrationModalOpen(null)} className="font-semibold text-slate-600">Cancel</Button>
                  <Button
                    className="bg-indigo-600 hover:bg-indigo-700 text-white font-bold"
                    onClick={handleConfirmSubmit}
                    disabled={syncingCandidateId === actionCandidateId}
                  >
                    {syncingCandidateId === actionCandidateId ? 'Syncing...' : 'Confirm & Submit to JobDiva'}
                  </Button>
                </div>
              </>
            ) : (
              <>
                <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
                    <span className="w-5 h-5 rounded-full bg-rose-100 text-rose-600 flex items-center justify-center font-bold text-[11px]">✕</span>
                    Reject Candidate
                  </h3>
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600">×</button>
                </div>
                <div className="p-6 space-y-4">
                  <p className="text-sm text-slate-500">
                    Please provide a reason for rejecting <strong className="text-slate-900 font-semibold">{candidates.find(c => c.id === actionCandidateId)?.name}</strong>.
                  </p>
                  <div className="space-y-2">
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-widest">Rejection Reason</label>
                    <select
                      className="w-full h-11 px-3 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500/50"
                      value={rejectReason}
                      onChange={e => setRejectReason(e.target.value)}
                    >
                      <option value="" disabled>Select a reason...</option>
                      <option value="Skills do not meet requirements">Skills do not meet requirements</option>
                      <option value="Communication skills">Communication skills</option>
                      <option value="Domain experience mismatch">Domain experience mismatch</option>
                      <option value="More qualified candidates identified">More qualified candidates identified</option>
                      <option value="Overqualified for the role">Overqualified for the role</option>
                      <option value="Compensation expectations exceed budget">Compensation expectations exceed budget</option>
                      <option value="Not aligned with employment type (W2 / C2C / 1099)">Not aligned with employment type (W2 / C2C / 1099)</option>
                      <option value="Work authorization / visa constraints">Work authorization / visa constraints</option>
                      <option value="Not comfortable with background check / drug test">Not comfortable with background check / drug test</option>
                      <option value="Not local and not open to relocation">Not local and not open to relocation</option>
                      <option value="Open to remote only">Open to remote only</option>
                      <option value="Not available within required timeline">Not available within required timeline</option>
                      <option value="Accepted another offer">Accepted another offer</option>
                      <option value="Candidate withdrew interest">Candidate withdrew interest</option>
                      <option value="Career gap concern">Career gap concern</option>
                      <option value="Job Hopping (short-term engagements throughout or in the last 5-7 years)">Job Hopping (short-term engagements throughout or in the last 5-7 years)</option>
                      <option value="Fake candidate — Multiple profiles/resumes; misrepresentation of past experience">Fake candidate — Multiple profiles/resumes; misrepresentation of past experience</option>
                      <option value="Already submitted to same client / hiring manager by another vendor">Already submitted to same client / hiring manager by another vendor</option>
                      <option value="Previously rejected by client">Previously rejected by client</option>
                      <option value="Not eligible for rehire">Not eligible for rehire</option>
                      <option value="Past performance concern (Internal note as per past Pyramid client feedback)">Past performance concern (Internal note as per past Pyramid client feedback)</option>
                    </select>
                  </div>
                </div>
                <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
                  <Button variant="outline" onClick={() => setIntegrationModalOpen(null)} className="font-semibold text-slate-600">Cancel</Button>
                  <Button
                    variant="destructive"
                    onClick={handleConfirmReject}
                    disabled={!rejectReason || syncingCandidateId === actionCandidateId}
                    className="font-bold"
                  >
                    {syncingCandidateId === actionCandidateId ? 'Syncing...' : 'Confirm Rejection'}
                  </Button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
      {toast && (
        <div className="fixed right-4 top-4 z-[90]">
          <div
            className={`rounded-lg border px-3 py-2 text-[12px] font-semibold shadow-md transition-all ${toast.type === "success"
              ? "border-emerald-200 bg-emerald-50 text-emerald-700"
              : toast.type === "error"
                ? "border-rose-200 bg-rose-50 text-rose-700"
                : "border-slate-200 bg-white text-slate-700"
              }`}
          >
            {toast.message}
          </div>
        </div>
      )}
      {selectedCandidateForActivity && (
        <UserActivityLogModal
          isOpen={isActivityLogModalOpen}
          onClose={() => setIsActivityLogModalOpen(false)}
          interviewId={selectedCandidateForActivity.id}
          candidateName={selectedCandidateForActivity.name}
        />
      )}
    </div>
  );
}
