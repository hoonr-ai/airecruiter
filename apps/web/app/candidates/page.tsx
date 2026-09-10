"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import Link from "next/link";
import { ArrowLeft, Search, Loader2, Phone, Check, X, ExternalLink, User, Briefcase, Zap, Activity, Calendar, Mail, Download, Filter, PhoneOff } from "lucide-react";
import { useClampedScoreInput } from "@/hooks/use-clamped-score";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { buildJobDivaCandidateUrl } from "@/lib/jobdiva";
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";
import { UserActivityLogModal } from "@/components/UserActivityLogModal";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const formatDate = (dateStr: string) => {
  if (!dateStr) return "—";
  try {
    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return dateStr;
    return date.toLocaleString('en-US', {
      timeZone: 'America/New_York',
      month: '2-digit',
      day: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
      timeZoneName: 'short'
    }).replace(",", "");
  } catch {
    return dateStr;
  }
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
      // no-op, could add toast
    }
    return;
  }

  const isJobDivaSource = source.startsWith("jobdiva") || !!candidate.jobdiva_id;
  const jobdivaCandidateId = String(
    candidate.jobdiva_candidate_id ||
    candidate.data?.jobdiva_candidate_id ||
    (isJobDivaSource ? candidateKey : "")
  ).trim();

  if (jobdivaCandidateId) {
    const url = buildJobDivaCandidateUrl(jobdivaCandidateId);
    if (url) {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
  }

  const existingProfileUrl =
    String(candidate.profile_url || "").trim() ||
    String(candidate.data?.profile_url || "").trim();

  if (existingProfileUrl) {
    const url = existingProfileUrl.startsWith('http://') || existingProfileUrl.startsWith('https://')
      ? existingProfileUrl
      : `https://${existingProfileUrl}`;
    window.open(url, "_blank", "noopener,noreferrer");
    return;
  }
};

const normalizeInterviewStatus = (raw: string | undefined | null): { label: string; color: string } => {
  const status = String(raw || "").trim().toLowerCase();
  if (!status) return { label: "Pending", color: "#64748b" };

  const pendingStates = new Set(["pending", "sent", "created", "queued", "scheduled", "started"]);
  if (pendingStates.has(status)) return { label: "Pending", color: "#64748b" };

  if (status === "in_progress" || status === "in-progress" || status === "inprogress" || status === "in progress") {
    return { label: "In Progress", color: "#f59e0b" };
  }

  if (status === "incomplete") {
    return { label: "Incomplete", color: "#64748b" };
  }

  if (status === "complete" || status === "completed" || status === "passed" || status === "pass") {
    return { label: "Pass", color: "#059669" };
  }

  if (status === "failed" || status === "fail" || status === "rejected") {
    return { label: "Fail", color: "#e11d48" };
  }

  const label = status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, " ");
  return { label, color: "#64748b" };
};

interface CandidateData {
  profile_url?: string;
  linkedin_url?: string;
  urls?: { linkedin?: string; linkedin_url?: string };
  resume_text?: string;
  experience_years?: number | string | null;
  company_experience?: Array<{ company?: string }>;
  matched_skills?: string[];
  missing_skills?: string[];
  explainability?: Array<string | { text?: string }>;
  feedback_type?: string;
  feedback_reason?: string;
  feedback_at?: string;
  jobdiva_candidate_id?: string;
  [key: string]: unknown;
}

interface HardFilterDetail {
  question?: string;
  status?: string;
  reason?: string;
}

interface Candidate {
  id: number;
  jobdiva_id: string | null;
  candidate_id: string;
  name: string;
  email: string;
  phone: string;
  source?: string;
  jobdiva_candidate_id?: string;
  match_score: number;
  engage_status: string;
  engage_interview_id: string;
  engage_created_at: string;
  engage_score: number;
  total_fit_score?: number | null;
  audit_payload?: { hard_filter_details?: HardFilterDetail[] };
  job_title: string;
  recruiter_emails?: string | string[];
  screening_level: string;
  attended_via: string;
  data?: CandidateData;
  location?: string;
  work_location?: string;
  profile_url?: string;
  image_url?: string;
  headline?: string;
  [key: string]: unknown;
}

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
  details?: HardFilterDetail[];
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



const SCREENING_LEVEL_STYLES: Record<string, string> = {
  "l0.5": "bg-gray-100 text-gray-600 border-gray-300",
  "l1":   "bg-blue-50 text-blue-700 border-blue-200",
  "l1.5": "bg-teal-50 text-teal-700 border-teal-200",
  "l2":   "bg-purple-50 text-purple-700 border-purple-200",
};

const getRecruiterEmailsArray = (emails: string | string[] | null | undefined): string[] => {
  if (!emails) return [];
  if (Array.isArray(emails)) return emails;
  if (typeof emails === "string") {
    try {
      const parsed = JSON.parse(emails);
      if (Array.isArray(parsed)) return parsed.map(String);
      if (typeof parsed === "string") return [parsed]; // Fix: handle JSON-quoted single strings without falling through
    } catch (e) {
      // ignore
    }
    return emails.split(",").map(s => s.trim()).filter(Boolean);
  }
  return [String(emails)];
};

export default function GlobalCandidatesPage() {
  const CANDIDATE_PAGE_SIZE = 50;

  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [offset, setOffset] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [isFetchingMore, setIsFetchingMore] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchInput, setSearchInput] = useState("");

  // Filters
  const [filterStatus, setFilterStatus] = useState("");
  const [filterFeedback, setFilterFeedback] = useState("");
  const [filterSource, setFilterSource] = useState("all");
  const [filterMinScore, handleFilterMinScoreChange, setFilterMinScore] = useClampedScoreInput("");
  const [availableSources, setAvailableSources] = useState<string[]>([]);
  const [exportStartDate, setExportStartDate] = useState("");
  const [exportEndDate, setExportEndDate] = useState("");
  const [isExporting, setIsExporting] = useState(false);
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  // Fetch global filter options on mount
  useEffect(() => {
    api.candidates.getFilterOptions().then(res => {
      if (res.status === "success" && res.sources) {
        const filtered = res.sources.filter((s: string) => 
          s.toLowerCase() !== "upload resume" && 
          s.toLowerCase() !== "upload-resume"
        );
        setAvailableSources(filtered.sort());
      }
    }).catch(console.error);
  }, []);

  const normalizeSourceLabel = (source: string | null | undefined): string => {
    const raw = String(source || "").trim();
    const s = raw.toLowerCase();
    if (!s) return "Unknown";
    if (s.includes("applicant")) return "Job-Diva Applicant";
    if (s.includes("talentsearch") || s.includes("talent_search")) return "Job-Diva Candidate";
    if (s.includes("linkedin")) return "LinkedIn";
    return raw;
  };

  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null);
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);

  // Feedback states
  const [feedbacks, setFeedbacks] = useState<Record<string, string>>({});
  const [feedbackReasons, setFeedbackReasons] = useState<Record<string, string>>({});
  const [feedbackTimes, setFeedbackTimes] = useState<Record<string, string>>({});
  const [actionCandidateId, setActionCandidateId] = useState<number | null>(null);
  const [integrationModalOpen, setIntegrationModalOpen] = useState<'submit' | 'reject' | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [syncingCandidateId, setSyncingCandidateId] = useState<number | null>(null);

  // Activity states
  const [isActivityLogModalOpen, setIsActivityLogModalOpen] = useState(false);
  const [selectedCandidateForActivity, setSelectedCandidateForActivity] = useState<{ id: string, name: string } | null>(null);

  // Hover states
  const [hoveredScoreCandidateId, setHoveredScoreCandidateId] = useState<string | null>(null);
  const [hoveredEngageCandidateId, setHoveredEngageCandidateId] = useState<string | null>(null);

  const resetPagination = () => {
    setOffset(0);
    setIsLoading(true);
  };

  const handleConfirmSubmit = async () => {
    if (actionCandidateId) {
      setSyncingCandidateId(actionCandidateId);
      const submittedAt = new Date().toISOString();
      try {
        const c = candidates.find(cand => cand.id === actionCandidateId);
        if (!c?.jobdiva_id) throw new Error("No job ID found");
        await api.candidates.feedback(c.jobdiva_id, String(actionCandidateId), { feedback_type: 'Submit' });
        setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Submit' }));
        setFeedbackTimes(prev => ({ ...prev, [actionCandidateId]: submittedAt }));
      } catch (error) {
        console.error('Error syncing submission:', error);
      } finally {
        setSyncingCandidateId(null);
        setIntegrationModalOpen(null);
        setActionCandidateId(null);
      }
    }
  };

  const handleConfirmReject = async () => {
    const trimmedReason = rejectReason?.trim() || "";
    if (actionCandidateId && trimmedReason) {
      setSyncingCandidateId(actionCandidateId);
      const rejectedAt = new Date().toISOString();
      try {
        const c = candidates.find(cand => cand.id === actionCandidateId);
        if (!c?.jobdiva_id) throw new Error("No job ID found");
        await api.candidates.feedback(c.jobdiva_id, String(actionCandidateId), {
          feedback_type: 'Reject',
          reason: trimmedReason
        });
        setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Reject' }));
        setFeedbackReasons(prev => ({ ...prev, [actionCandidateId]: trimmedReason }));
        setFeedbackTimes(prev => ({ ...prev, [actionCandidateId]: rejectedAt }));
      } catch (error) {
        console.error('Error syncing rejection:', error);
      } finally {
        setSyncingCandidateId(null);
        setIntegrationModalOpen(null);
        setActionCandidateId(null);
        setRejectReason('');
      }
    }
  };

  const handleMarkUnreachable = async (candidateId: number, jobDivaId: string | null) => {
    setSyncingCandidateId(candidateId);
    try {
      if (!jobDivaId) throw new Error("No job ID found");
      await api.candidates.feedback(jobDivaId, String(candidateId), { feedback_type: 'Unreachable' });
      setFeedbacks(prev => ({ ...prev, [candidateId]: 'Unreachable' }));
      setFeedbackTimes(prev => ({ ...prev, [candidateId]: new Date().toISOString() }));
      setFeedbackReasons(prev => {
        const next = { ...prev };
        delete next[candidateId];
        return next;
      });
    } catch (error) {
      console.error('Error marking unreachable:', error);
    } finally {
      setSyncingCandidateId(null);
    }
  };

  const fetchIdRef = useRef(0);

  const fetchCandidates = useCallback(async (currentOffset: number, search: string, status: string, feedback: string, source: string, minScore: number | "", startDate: string, endDate: string, replace: boolean = false) => {
    fetchIdRef.current += 1;
    const currentFetchId = fetchIdRef.current;

    if (replace) {
      setFeedbacks({});
      setFeedbackReasons({});
      setFeedbackTimes({});
    }

    try {
      const query = new URLSearchParams({
        limit: String(CANDIDATE_PAGE_SIZE),
        offset: String(currentOffset),
      });
      if (search) {
        query.append("search", search);
      }
      if (status) {
        query.append("status", status);
      }
      if (feedback) {
        query.append("feedback", feedback);
      }
      if (source && source !== "all") {
        query.append("source", source);
      }
      if (minScore !== "") {
        query.append("min_score", String(minScore));
      }
      if (startDate) {
        query.append("start_date", startDate);
      }
      if (endDate) {
        query.append("end_date", endDate);
      }

      const candData = await api.candidates.getAllLaunched(query.toString());
      if (currentFetchId !== fetchIdRef.current) return;

      if (candData.status === "success" && Array.isArray(candData.candidates)) {
        const pageFeedbacks: Record<string, string> = {};
        const pageFeedbackReasons: Record<string, string> = {};
        const pageFeedbackTimes: Record<string, string> = {};
        candData.candidates.forEach((c: Candidate) => {
          if (c.data?.feedback_type) pageFeedbacks[c.id] = c.data.feedback_type;
          if (c.data?.feedback_reason) pageFeedbackReasons[c.id] = c.data.feedback_reason;
          if (c.data?.feedback_at) pageFeedbackTimes[c.id] = c.data.feedback_at;
        });
        setFeedbacks(prev => ({ ...prev, ...pageFeedbacks }));
        setFeedbackReasons(prev => ({ ...prev, ...pageFeedbackReasons }));
        setFeedbackTimes(prev => ({ ...prev, ...pageFeedbackTimes }));

        if (replace) {
          setCandidates(candData.candidates);
        } else {
          setCandidates(prev => {
            const newDict = new Map(prev.map(c => [c.candidate_id, c]));
            candData.candidates.forEach((c: Candidate) => newDict.set(c.candidate_id, c));
            return Array.from(newDict.values());
          });
        }
        setTotalCount(candData.total || 0);
      }
    } catch (error) {
      console.error("Failed to fetch launched candidates", error);
    }
  }, []);

  useEffect(() => {
    let timeout: NodeJS.Timeout;
    if (searchInput !== searchQuery) {
      timeout = setTimeout(() => {
        setSearchQuery(searchInput);
        setOffset(0);
        setIsLoading(true);
      }, 500);
    }
    return () => clearTimeout(timeout);
  }, [searchInput, searchQuery]);

  useEffect(() => {
    const request = window.setTimeout(() => {
      void fetchCandidates(0, searchQuery, filterStatus, filterFeedback, filterSource, filterMinScore, exportStartDate, exportEndDate, true)
        .finally(() => setIsLoading(false));
    }, 0);
    return () => window.clearTimeout(request);
  }, [searchQuery, filterStatus, filterFeedback, filterSource, filterMinScore, exportStartDate, exportEndDate, fetchCandidates]);

  const loadMore = async () => {
    if (isFetchingMore) return;
    const nextOffset = offset + CANDIDATE_PAGE_SIZE;
    if (nextOffset >= totalCount) return;

    setIsFetchingMore(true);
    setOffset(nextOffset);
    await fetchCandidates(nextOffset, searchQuery, filterStatus, filterFeedback, filterSource, filterMinScore, exportStartDate, exportEndDate, false);
    setIsFetchingMore(false);
  };

  const hasMore = candidates.length < totalCount;

  const handleExport = () => {
    if (!candidates || candidates.length === 0) return;

    const escapeCsvField = (field: unknown) => {
      if (field === null || field === undefined) return "";
      let str = String(field);
      if (/^[=+\-@]/.test(str)) {
        str = "'" + str;
      }
      if (str.includes(",") || str.includes('"') || str.includes("\n")) {
        return `"${str.replace(/"/g, '""')}"`;
      }
      return str;
    };

    const headers = [
      "JobDiva ID",
      "Candidate Name",
      "Email",
      "Phone",
      "Source",
      "Launched Date",
      "Resume Screening Score",
      "Engage Status",
      "Engage Score",
      "Total Fit Score",
      "Recruiter Email"
    ];

    const generateRows = (cands: any[]) => cands.map((c) => {
      const resumeScore = Math.round(c.match_score || 0);
      const engageScoreStr = c.engage_score !== null && c.engage_score !== undefined ? `${c.engage_score}` : "Waiting";
      const totalFitScoreStr = c.total_fit_score !== null && c.total_fit_score !== undefined ? `${c.total_fit_score}` : "Waiting";

      const statusInfo = normalizeInterviewStatus(c.engage_status);

      return [
        escapeCsvField(c.jobdiva_id || ""),
        escapeCsvField(c.name || "Unknown"),
        escapeCsvField(c.email || ""),
        escapeCsvField(c.phone || ""),
        escapeCsvField(normalizeSourceLabel(c.source)),
        escapeCsvField(c.engage_created_at ? formatDate(c.engage_created_at) : ""),
        escapeCsvField(resumeScore > 0 ? resumeScore : "N/A"),
        escapeCsvField(statusInfo.label),
        escapeCsvField(engageScoreStr),
        escapeCsvField(totalFitScoreStr),
        escapeCsvField(getRecruiterEmailsArray(c.recruiter_emails).join(", ") || "N/A")
      ].join(",");
    });

    const rows = generateRows(candidates);
    const csvContent = [headers.join(","), ...rows].join("\n");
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute(
      "download",
      `Master_Candidate_Pool_${new Date().toISOString().split("T")[0]}.csv`
    );
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleExportWithDateRange = async () => {
    if (exportStartDate && exportEndDate && exportStartDate > exportEndDate) {
      setToast({ message: "Start Date cannot be after End Date.", type: "error" });
      return;
    }

    setIsExporting(true);
    try {
      const allExportCandidates: Candidate[] = [];
      let currentOffset = 0;
      const CHUNK_SIZE = 500;
      let hasMore = true;

      while (hasMore) {
        const query = new URLSearchParams({
          limit: String(CHUNK_SIZE),
          offset: String(currentOffset),
        });
        if (searchQuery) query.append("search", searchQuery);
        if (filterStatus) query.append("status", filterStatus);
        if (filterFeedback) query.append("feedback", filterFeedback);
        if (filterSource && filterSource !== "all") query.append("source", filterSource);
        if (filterMinScore !== "") query.append("min_score", String(filterMinScore));
        if (exportStartDate) query.append("start_date", exportStartDate);
        if (exportEndDate) query.append("end_date", exportEndDate);

        const candData = await api.candidates.getAllLaunched(query.toString());
        if (candData.status === "success" && Array.isArray(candData.candidates) && candData.candidates.length > 0) {
          allExportCandidates.push(...candData.candidates);
          currentOffset += CHUNK_SIZE;
          if (allExportCandidates.length >= (candData.total || 0)) {
            hasMore = false;
          }
        } else {
          hasMore = false;
        }
      }

      if (allExportCandidates.length > 0) {
        
        const escapeCsvField = (field: unknown) => {
          if (field === null || field === undefined) return "";
          let str = String(field);
          if (/^[=+\-@]/.test(str)) {
            str = "'" + str;
          }
          if (str.includes(",") || str.includes('"') || str.includes("\n")) {
            return `"${str.replace(/"/g, '""')}"`;
          }
          return str;
        };

        const headers = [
          "JobDiva ID",
          "Candidate Name",
          "Email",
          "Phone",
          "Source",
          "Launched Date",
          "Resume Screening Score",
          "Engage Status",
          "Engage Score",
          "Total Fit Score",
          "Recruiter Email"
        ];

        const generateRows = (cands: Candidate[]) => cands.map((c) => {
          const resumeScore = Math.round(c.match_score || 0);
          const engageScoreStr = c.engage_score !== null && c.engage_score !== undefined ? `${c.engage_score}` : "Waiting";
          const totalFitScoreStr = c.total_fit_score !== null && c.total_fit_score !== undefined ? `${c.total_fit_score}` : "Waiting";

          const statusInfo = normalizeInterviewStatus(c.engage_status);

          return [
            escapeCsvField(c.jobdiva_id || ""),
            escapeCsvField(c.name || "Unknown"),
            escapeCsvField(c.email || ""),
            escapeCsvField(c.phone || ""),
            escapeCsvField(normalizeSourceLabel(c.source)),
            escapeCsvField(c.engage_created_at ? formatDate(c.engage_created_at) : ""),
            escapeCsvField(resumeScore > 0 ? resumeScore : "N/A"),
            escapeCsvField(statusInfo.label),
            escapeCsvField(engageScoreStr),
            escapeCsvField(totalFitScoreStr),
            escapeCsvField(getRecruiterEmailsArray(c.recruiter_emails).join(", ") || "N/A")
          ].join(",");
        });

        const rows = generateRows(allExportCandidates);
        const csvContent = [headers.join(","), ...rows].join("\n");
        const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.setAttribute("href", url);
        link.setAttribute(
          "download",
          `Master_Candidate_Pool_${exportStartDate || "all"}_to_${exportEndDate || "all"}.csv`
        );
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      } else {
        setToast({ message: "No candidates found for the selected date range.", type: "error" });
      }
    } catch (error) {
      console.error("Export failed:", error);
      setToast({ message: "Failed to export data.", type: "error" });
    } finally {
      setIsExporting(false);
    }
  };

  return (
    <div className="space-y-6 w-full max-w-[1600px] mx-auto px-2 pb-10">
      <div className="flex items-center justify-between mt-2">
        <div className="flex items-center gap-4">
          <Link
            href="/"
            className="rounded-full h-10 w-10 inline-flex items-center justify-center hover:bg-slate-100 transition-colors"
            aria-label="Back to jobs"
          >
            <ArrowLeft className="h-5 w-5 text-slate-400" />
          </Link>
          <div>
            <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Master Candidate Pool</h1>
            <p className="text-slate-500 text-[14px]">Viewing all candidates launched through PAIR.</p>
          </div>
        </div>
      </div>

      <div className="bg-white rounded-2xl border border-slate-200 shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)] flex flex-col h-[calc(100vh-180px)] min-h-[600px]">

        {/* Toolbar */}
        <div className="p-4 border-b border-slate-100 flex flex-col gap-4 bg-slate-50/50 rounded-t-2xl">

          {/* Row 2: Search and Actions */}
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 w-full">
            <div className="relative shrink-0 min-w-[200px] flex-1 max-w-[460px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-400" />
              <Input
                placeholder="Search by Name, Email, Phone, Job Title, or JobDiva ID..."
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                className="pl-9 w-full h-9 text-[13px] bg-white border-slate-200 focus-visible:ring-indigo-500/20 focus-visible:border-indigo-500 transition-all rounded-lg shadow-sm"
              />
            </div>

            <div className="flex items-center gap-4 shrink-0">
              <div className="flex items-center gap-2 bg-white rounded-lg border border-slate-200 px-2 h-9 shadow-sm">
                <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider pl-1 pr-1">Launched From</label>
                <input
                  type="date"
                  value={exportStartDate}
                  onChange={(e) => setExportStartDate(e.target.value)}
                  className="h-7 text-[12px] bg-transparent focus:outline-none w-[110px]"
                />
                <span className="text-slate-300 text-[11px] uppercase font-bold px-1">to</span>
                <input
                  type="date"
                  value={exportEndDate}
                  onChange={(e) => setExportEndDate(e.target.value)}
                  className="h-7 text-[12px] bg-transparent focus:outline-none w-[110px]"
                />
              </div>

            {totalCount > 0 && !isLoading && (
              <span className="text-[13px] font-medium text-slate-500 whitespace-nowrap">
                Showing {candidates.length} of {totalCount}
              </span>
            )}
            {candidates.length > 0 && (
              <Button
                variant="outline"
                size="sm"
                onClick={handleExportWithDateRange}
                disabled={isExporting}
                className="h-9 px-3 flex items-center gap-2 border-slate-200 bg-white hover:bg-slate-50 text-slate-600 shadow-sm transition-colors rounded-lg font-medium text-[12.5px]"
                title="Export candidates matching current filters and date range"
              >
                {isExporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                {isExporting ? "Exporting..." : "Export CSV"}
              </Button>
            )}
          </div>
        </div>

          {/* Row 2: Filters */}
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4 w-full">
            <div className="flex items-center justify-between gap-2 bg-white rounded-lg px-3 h-10 border border-slate-200 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-500/20 transition-all shadow-sm w-full">
              <Filter className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap shrink-0">Status</label>
              <select
                value={filterStatus}
                onChange={(e) => {
                  resetPagination();
                  setFilterStatus(e.target.value);
                }}
                className="text-[12px] font-semibold text-slate-800 bg-transparent focus:outline-none cursor-pointer pr-1 flex-1 text-right"
              >
                <option value="">All</option>
                <option value="pass">Pass</option>
                <option value="fail">Fail</option>
                <option value="in progress">In Progress</option>
                <option value="pending">Pending</option>
              </select>
            </div>

            <div className="flex items-center justify-between gap-2 bg-white rounded-lg px-3 h-10 border border-slate-200 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-500/20 transition-all shadow-sm w-full">
              <Filter className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap shrink-0">Feedback</label>
              <select
                value={filterFeedback}
                onChange={(e) => {
                  resetPagination();
                  setFilterFeedback(e.target.value);
                }}
                className="text-[12px] font-semibold text-slate-800 bg-transparent focus:outline-none cursor-pointer pr-1 flex-1 text-right"
              >
                <option value="">All</option>
                <option value="No Feedback">No Feedback</option>
                <option value="Submit">Submitted</option>
                <option value="Reject">Rejected</option>
                <option value="Unreachable">Unreachable</option>
              </select>
            </div>

            <div className="flex items-center justify-between gap-2 bg-white rounded-lg px-3 h-10 border border-slate-200 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-500/20 transition-all shadow-sm w-full">
              <Filter className="w-3.5 h-3.5 text-slate-400 shrink-0" />
              <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap shrink-0">Source</label>
              <select
                value={filterSource}
                onChange={(e) => {
                  resetPagination();
                  setFilterSource(e.target.value);
                }}
                className="text-[12px] font-semibold text-slate-800 bg-transparent focus:outline-none cursor-pointer pr-1 flex-1 text-right"
              >
                <option value="all">All</option>
                {availableSources.map(s => (
                  <option key={s} value={s}>{normalizeSourceLabel(s)}</option>
                ))}
              </select>
            </div>

            <div className="flex items-center justify-between gap-2 bg-white rounded-lg px-3 h-10 border border-slate-200 focus-within:border-indigo-500 focus-within:ring-2 focus-within:ring-indigo-500/20 transition-all shadow-sm w-full">
              <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider whitespace-nowrap shrink-0">Min Resume Score</label>
              <Input
                type="number"
                min={0}
                max={100}
                value={filterMinScore}
                onChange={(e) => {
                  resetPagination();
                  handleFilterMinScoreChange(e.target.value);
                }}
                className="h-7 w-full max-w-[80px] ml-auto text-[12px] font-bold bg-slate-50/50 border-slate-200 rounded px-2 text-center focus-visible:ring-indigo-500/20 focus-visible:border-indigo-500"
              />
            </div>
          </div>
        </div>

        {/* Table Area */}
        <div className="flex-1 relative min-h-0 overflow-auto scrollbar-thin scrollbar-thumb-slate-200">
          <Table
            containerClassName="overflow-visible"
            className="min-w-[1750px] border-separate border-spacing-0"
          >
            <TableHeader className="bg-slate-50 sticky top-0 z-40 shadow-sm">
              <TableRow className="border-b-slate-200 hover:bg-transparent h-[50px]">
                <TableHead className="w-[50px] min-w-[50px] max-w-[50px] sticky left-0 z-30 bg-slate-50 text-center font-bold text-slate-500 text-[12px] uppercase tracking-wider px-2">#</TableHead>
                <TableHead className="w-[120px] min-w-[120px] max-w-[120px] sticky left-[50px] z-30 bg-slate-50 text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">JOBDIVA ID</TableHead>
                <TableHead className="w-[200px] min-w-[200px] max-w-[200px] sticky left-[170px] z-30 bg-slate-50 text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">JOB TITLE</TableHead>
                <TableHead className="w-[300px] min-w-[300px] max-w-[300px] sticky left-[370px] z-30 bg-slate-50 text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200 shadow-[2px_0_5px_-2px_rgba(0,0,0,0.05)]">CANDIDATE NAME</TableHead>
                <TableHead className="w-[300px] min-w-[300px] max-w-[300px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RECRUITER EMAIL</TableHead>
                <TableHead className="w-[220px] min-w-[220px] max-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">SOURCE</TableHead>
                <TableHead className="w-[220px] min-w-[220px] max-w-[220px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">LAUNCHED DATE</TableHead>
                <TableHead className="w-[180px] min-w-[180px] max-w-[180px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">SCREENING LEVEL</TableHead>
                <TableHead className="w-[240px] min-w-[240px] max-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">RESUME SCREENING SCORE</TableHead>
                <TableHead className="w-[240px] min-w-[240px] max-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">ENGAGE STATUS</TableHead>
                <TableHead className="w-[240px] min-w-[240px] max-w-[240px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">ENGAGE SCORE</TableHead>
                <TableHead className="w-[260px] min-w-[260px] max-w-[260px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">TOTAL FIT SCORE</TableHead>
                <TableHead className="w-[260px] min-w-[260px] max-w-[260px] text-center text-[12px] font-bold text-slate-500 uppercase tracking-wider border-l border-slate-200">CANDIDATE FEEDBACK</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                Array.from({ length: 10 }).map((_, i) => (
                  <TableRow key={`skel-${i}`}>
                    <TableCell className="border-b border-slate-200 text-center sticky left-0 z-10 bg-white"><Skeleton className="h-4 w-6 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center sticky left-[50px] z-10 bg-white border-l border-slate-200"><Skeleton className="h-4 w-16 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center sticky left-[170px] z-10 bg-white border-l border-slate-200"><Skeleton className="h-4 w-32 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 sticky left-[370px] z-10 bg-white border-l border-slate-200 shadow-[2px_0_5px_-2px_rgba(0,0,0,0.05)]">
                      <div className="space-y-2">
                        <Skeleton className="h-4 w-40 mx-auto" />
                        <Skeleton className="h-3 w-32 mx-auto" />
                      </div>
                    </TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-16 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-24 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-6 w-12 mx-auto rounded-full" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-6 w-24 mx-auto rounded-full" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-8 w-24 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-32 mx-auto" /></TableCell>
                  </TableRow>
                ))
              ) : candidates.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={11} className="h-48 text-center">
                    <div className="text-slate-400 text-[14px]">No candidates found.</div>
                  </TableCell>
                </TableRow>
              ) : (
                candidates.map((c, i) => {
                  const statusInfo = normalizeInterviewStatus(c.engage_status);
                  const resumeScore = Math.round(c.match_score || 0);
                  const parsedRecruiterEmails = getRecruiterEmailsArray(c.recruiter_emails); // Cache parsed emails once per row

                  return (
                    <TableRow key={c.candidate_id} className="group hover:bg-slate-50 transition-colors cursor-default h-[60px] border-b border-slate-200">
                      <TableCell className="border-b border-slate-200 text-center text-[13px] font-medium text-slate-400 sticky left-0 z-10 bg-white group-hover:bg-slate-50 transition-colors">
                        {i + 1}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-semibold text-slate-700 text-[12px] sticky left-[50px] z-10 bg-white group-hover:bg-slate-50 transition-colors border-l border-slate-200">
                        {c.jobdiva_id || "—"}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center text-[13px] font-medium text-slate-700 sticky left-[170px] z-10 bg-white group-hover:bg-slate-50 transition-colors border-l border-slate-200 px-3">
                        <span className="whitespace-normal break-words leading-tight" title={c.job_title}>
                          {c.job_title || "Unknown Job"}
                        </span>
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center sticky left-[370px] z-10 bg-white group-hover:bg-slate-50 transition-colors border-l border-slate-200 shadow-[2px_0_5px_-2px_rgba(0,0,0,0.05)]">
                        <div className="flex flex-col gap-1 items-center justify-center">
                          <button
                            onClick={() => {
                              setSelectedCandidate(c);
                              setDetailsModalOpen(true);
                            }}
                            className="text-[14px] font-bold text-indigo-600 hover:text-indigo-700 hover:underline transition-colors text-center whitespace-normal leading-tight"
                          >
                            {c.name || "Unknown"}
                          </button>
                          <span className="text-[12px] text-slate-500 block mb-0.5 text-center px-1 break-all whitespace-normal" title={c.email}>
                            <Mail className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {c.email || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <span className="text-[12px] text-slate-500 block mb-0.5 text-center">
                            <Phone className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {c.phone || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <div className="flex items-center justify-center gap-3 mt-1">
                            {c.engage_interview_id && (
                              <button
                                type="button"
                                onClick={() => {
                                  setSelectedCandidateForActivity({
                                    id: c.engage_interview_id,
                                    name: c.name,
                                  });
                                  setIsActivityLogModalOpen(true);
                                }}
                                className="text-[12px] text-indigo-600 hover:bg-indigo-50 px-2 py-0.5 rounded-md inline-flex items-center justify-center gap-1 font-bold border border-indigo-100 shadow-sm transition-colors"
                                title="View user activity history"
                              >
                                <Activity className="w-3.5 h-3.5" />
                                Activity History
                              </button>
                            )}
                            <button
                              type="button"
                              onClick={() => openCandidateProfileUrl(c)}
                              className="text-[12px] text-indigo-600 hover:underline inline-flex items-center justify-center gap-1 font-medium"
                              title={String(c.source || "").toLowerCase().includes("linkedin") ? "Open LinkedIn profile" : "Open JobDiva profile"}
                            >
                              <ExternalLink className="w-3.5 h-3.5" />
                              {String(c.source || "").toLowerCase().includes("linkedin") ? "LinkedIn URL" : "JobDiva URL"}
                            </button>
                          </div>
                        </div>
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-medium text-slate-700 text-[13px] border-l border-slate-200 min-w-[300px] max-w-[300px] px-3 align-middle py-4">
                        {parsedRecruiterEmails.length > 0 ? (
                          <div className="flex flex-col items-center justify-center w-full gap-1.5">
                            {parsedRecruiterEmails.map((email) => (
                              <span key={email} className="block whitespace-nowrap leading-relaxed w-full text-center max-w-full overflow-hidden text-ellipsis font-semibold" title={email}>
                                {email}
                              </span>
                            ))}
                          </div>
                        ) : (
                          <div className="flex items-center justify-center h-full w-full">
                            <span className="text-slate-400 italic text-[11px]">N/A</span>
                          </div>
                        )}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-semibold text-slate-700 text-[12px]">
                        {normalizeSourceLabel(c.source)}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-medium text-slate-600 text-[12px]">
                        {c.engage_created_at ? formatDate(c.engage_created_at) : <span className="text-slate-400 italic">N/A</span>}
                      </TableCell>
                      
                      <TableCell className="border-b border-slate-200 text-center font-medium border-l border-slate-200">
                        {c.screening_level ? (
                          <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-bold border uppercase ${SCREENING_LEVEL_STYLES[c.screening_level.toLowerCase()] ?? "bg-gray-100 text-gray-600 border-gray-300"}`}>
                            {c.screening_level}
                          </span>
                        ) : (
                          <span className="text-slate-400 text-xs italic">N/A</span>
                        )}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-medium text-slate-900 text-[13px]">
                        {resumeScore > 0 ? (
                          <div
                            className="relative group/score inline-block w-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 rounded"
                            onMouseEnter={() => setHoveredScoreCandidateId(c.candidate_id)}
                            onMouseLeave={() => setHoveredScoreCandidateId(null)}
                            onFocus={() => setHoveredScoreCandidateId(c.candidate_id)}
                            onBlur={() => setHoveredScoreCandidateId(null)}
                            tabIndex={0}
                            aria-label="View Resume Screening Details"
                            role="button"
                          >
                            <span className="font-bold text-slate-900 text-[14px] underline decoration-indigo-200 underline-offset-4 cursor-help">
                              {resumeScore}/100
                            </span>
                            <ResumeScreeningHoverCard
                              candidate={c}
                              open={hoveredScoreCandidateId === c.candidate_id}
                            />
                          </div>
                        ) : (
                          <span className="font-normal opacity-40 italic text-slate-400 text-[13px]">N/A</span>
                        )}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center py-3 border-l border-slate-200">
                        <div className="flex justify-center items-center w-full">
                          <span
                            className="px-3 py-1 rounded-full text-[11px] font-bold border"
                            style={{ backgroundColor: `${statusInfo.color}08`, color: statusInfo.color, borderColor: `${statusInfo.color}30` }}
                          >
                            {statusInfo.label}
                          </span>
                        </div>
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-medium text-slate-700 text-[13px] border-l border-slate-200">
                        {c.engage_score !== null && c.engage_score !== undefined ? (
                          <div
                            className="relative group/engage inline-block w-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 rounded"
                            onMouseEnter={() => setHoveredEngageCandidateId(c.candidate_id)}
                            onMouseLeave={() => setHoveredEngageCandidateId(null)}
                            onFocus={() => setHoveredEngageCandidateId(c.candidate_id)}
                            onBlur={() => setHoveredEngageCandidateId(null)}
                            tabIndex={0}
                            aria-label="View Hard Filter Details"
                            role="button"
                          >
                            <span className="font-bold text-slate-900 text-[14px] underline decoration-indigo-200 underline-offset-4 cursor-help">
                              {c.engage_score}/100
                            </span>
                            <HardFilterHoverCard
                              details={c.audit_payload?.hard_filter_details}
                              open={hoveredEngageCandidateId === c.candidate_id}
                            />
                          </div>
                        ) : (
                          <span className="font-normal opacity-40 italic text-[13px]">Waiting</span>
                        )}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center font-bold text-slate-900 text-[14px] border-l border-slate-200">
                        {c.total_fit_score !== null && c.total_fit_score !== undefined ? (
                          <span>{c.total_fit_score}/100</span>
                        ) : (
                          <span className="font-normal opacity-40 italic text-[13px]">Waiting</span>
                        )}
                      </TableCell>

                      <TableCell className="border-b border-slate-200 text-center border-l border-slate-200 py-4 align-middle transition-colors group-hover:bg-indigo-50/5">
                        <div className="flex flex-col items-center justify-center gap-1.5 min-h-[64px]">
                          <Select
                            disabled={syncingCandidateId === c.id}
                            value={feedbacks[c.id]?.startsWith("Reject") ? "Reject" : feedbacks[c.id] || undefined}
                            onValueChange={(val) => {
                              if (val === "Reject") {
                                setActionCandidateId(c.id);
                                setRejectReason("");
                                setIntegrationModalOpen('reject');
                              } else if (val === "Submit") {
                                setActionCandidateId(c.id);
                                setIntegrationModalOpen('submit');
                              } else if (val === "Unreachable") {
                                handleMarkUnreachable(c.id, c.jobdiva_id);
                              }
                            }}
                          >
                            <SelectTrigger className="w-[140px] h-8 text-[12px] font-semibold text-slate-700 bg-white border-slate-300 hover:border-slate-400 focus:ring-2 focus:ring-indigo-500/20 focus:border-indigo-500">
                              <SelectValue placeholder="Select Action..." />
                            </SelectTrigger>
                            <SelectContent className="bg-white border-slate-200 shadow-xl">
                              <SelectItem value="Submit" className="text-[12px] font-semibold text-slate-700 focus:bg-indigo-50 focus:text-indigo-700">Submit</SelectItem>
                              <SelectItem value="Reject" className="text-[12px] font-semibold text-slate-700 focus:bg-red-50 focus:text-red-700">Reject</SelectItem>
                              <SelectItem value="Unreachable" className="text-[12px] font-semibold text-slate-700 focus:bg-orange-50 focus:text-orange-700">Unreachable</SelectItem>
                            </SelectContent>
                          </Select>
                          {feedbacks[c.id] && (
                            <div className="flex flex-col items-center gap-1 mt-1.5">
                              <div className={`text-[12px] font-bold flex items-center justify-center gap-1 whitespace-nowrap ${feedbacks[c.id] === 'Submit' ? 'text-indigo-600' : feedbacks[c.id] === 'Reject' ? 'text-rose-600' : 'text-slate-500'}`}>
                                {feedbacks[c.id] === 'Submit' ? <><Check className="w-3 h-3" /> Submitted</> : 
                                 feedbacks[c.id] === 'Reject' ? <><X className="w-3 h-3" /> Rejected</> : 
                                 <><PhoneOff className="w-3 h-3" /> Unreachable</>}
                              </div>
                              {feedbackReasons[c.id] && (
                                <div className="max-w-[160px] max-h-[80px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-200 pr-1 text-xs text-slate-600 font-medium text-center leading-snug whitespace-normal break-words">
                                  {feedbackReasons[c.id]}
                                </div>
                              )}
                              {feedbackTimes[c.id] && (() => {
                                const d = new Date(feedbackTimes[c.id]);
                                if (isNaN(d.getTime())) return null;
                                return (
                                  <div className="text-[10px] text-slate-400 font-medium text-center whitespace-nowrap" title={feedbackTimes[c.id]}>
                                    {formatDate(feedbackTimes[c.id])}
                                  </div>
                                );
                              })()}
                            </div>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
              {isFetchingMore && Array.from({ length: 3 }).map((_, i) => (
                  <TableRow key={`skel-more-${i}`}>
                    <TableCell className="border-b border-slate-200 text-center sticky left-0 z-10 bg-white"><Skeleton className="h-4 w-6 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center sticky left-[50px] z-10 bg-white border-l border-slate-200"><Skeleton className="h-4 w-16 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center sticky left-[170px] z-10 bg-white border-l border-slate-200"><Skeleton className="h-4 w-32 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 sticky left-[370px] z-10 bg-white border-l border-slate-200 shadow-[2px_0_5px_-2px_rgba(0,0,0,0.05)]">
                      <div className="space-y-2">
                        <Skeleton className="h-4 w-40 mx-auto" />
                        <Skeleton className="h-3 w-32 mx-auto" />
                      </div>
                    </TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-16 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-24 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-6 w-12 mx-auto rounded-full" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-6 w-24 mx-auto rounded-full" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                    <TableCell className="border-b border-slate-200 text-center border-l border-slate-200"><Skeleton className="h-8 w-24 mx-auto" /></TableCell>
                  </TableRow>
              ))}
            </TableBody>
          </Table>

          {hasMore && !isLoading && !isFetchingMore && (
            <div className="p-6 flex justify-center border-t border-slate-100 pb-16">
              <button
                onClick={loadMore}
                className="h-10 px-8 rounded-full bg-white border border-indigo-200 text-[13px] font-semibold text-indigo-600 hover:bg-indigo-50 transition-all shadow-sm flex items-center gap-2"
              >
                Load More
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Toast Notification */}
      {toast && (
        <div className={`fixed top-4 right-4 z-50 rounded-lg p-4 text-white ${toast.type === 'success' ? 'bg-green-500' : 'bg-red-500'} shadow-lg transition-all duration-300 transform translate-y-0 opacity-100`}>
          {toast.message}
        </div>
      )}

      {/* Modals */}
      {selectedCandidate && (
        <CandidateDetailsModal
          isOpen={detailsModalOpen}
          onClose={() => setDetailsModalOpen(false)}
          candidateName={selectedCandidate.name}
          profileUrl={selectedCandidate.profile_url}
          imageUrl={selectedCandidate.image_url}
          jobTitle={selectedCandidate.job_title}
          location={selectedCandidate.location}
          workLocation={selectedCandidate.work_location}
          experienceYears={selectedCandidate.data?.experience_years}
          matchScore={selectedCandidate.match_score}
        />
      )}

      {/* Activity Log Modal */}
      {selectedCandidateForActivity && (
        <UserActivityLogModal
          isOpen={isActivityLogModalOpen}
          onClose={() => setIsActivityLogModalOpen(false)}
          interviewId={selectedCandidateForActivity.id}
          candidateName={selectedCandidateForActivity.name}
        />
      )}

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
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600" aria-label="Close">×</button>
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
                      <p><strong className="text-slate-900">Job:</strong> {candidates.find(c => c.id === actionCandidateId)?.job_title}</p>
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
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600" aria-label="Close">×</button>
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
                      <option value="Candidate does not want to work with the same client">Candidate does not want to work with the same client</option>
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
    </div>
  );
}
