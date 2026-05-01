"use client";

import { useState, useEffect, useMemo } from "react";
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
  X
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
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";
import { CandidateMessageModal } from "@/components/candidate-message-modal";
import { EngageWizardModal } from "@/components/EngageWizardModal";
import { MissingPhonesModal, type MissingPhoneCandidate } from "@/components/missing-phones-modal";
import { API_BASE } from "@/lib/api";
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

interface JobDetails {
  job_id: string;
  jobdiva_id?: string;
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
  candidate_id?: string;
  engage_interview_id?: string;
  name: string;
  email: string;
  phone?: string;
  location?: string;
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
  engage_completed_at?: string;
  engage_created_at?: string;
  availability?: string;
  created_at: string;
  data?: any;
}

type EnrichStatus = { type: "info" | "error" | "success"; message: string };
type ToastState = { type: "info" | "error" | "success"; message: string } | null;

export default function CandidateRankingsPage() {
  const { jobId } = useParams();
  const router = useRouter();
  const engagement = useEngagementFlow();

  const [job, setJob] = useState<JobDetails | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
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
        const response = await fetch(`${API_BASE}/jobs/${jobId}/candidates/${actionCandidateId}/feedback`, {
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
        const response = await fetch(`${API_BASE}/jobs/${jobId}/candidates/${actionCandidateId}/feedback`, {
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
  type StatusFilter = "all" | "done" | "pending";
  type SortField = "index" | "name" | "screening_score" | "engage_score" | "total_score";
  type SortDir = "asc" | "desc";

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [sourceFilter, setSourceFilter] = useState<string>("all");
  const [minScore, setMinScore] = useState<number>(0);
  const [sortField, setSortField] = useState<SortField>("index");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  // Resume-matching completion status for filter + table labels.
  const deriveStatus = (c: Candidate): "done" | "pending" => {
    const fromData = String(c.data?.resume_matching_status || "").toLowerCase();
    if (fromData === "done") return "done";
    const s = c.match_score ?? c.resume_match_percentage ?? 0;
    return s > 0 ? "done" : "pending";
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
      "in_progress",
      "in-progress",
      "inprogress",
      "started",
    ]);
    if (pendingStates.has(raw)) {
      return { label: "Pending", color: "#64748b" };
    }

    if (raw.includes("complete")) {
      return { label: "Completed", color: "#059669" };
    }

    const label = raw.charAt(0).toUpperCase() + raw.slice(1);
    return { label, color: "#64748b" };
  };

  const normalizeHardFilterStatus = (c: Candidate): { label: string; color: string } => {
    const raw = String(c.engage_hard_filter_status || c.data?.engage_hard_filter_status || "").trim().toLowerCase();
    if (!raw) {
      return { label: "—", color: "#94a3b8" };
    }

    if (raw.includes("fail")) {
      return { label: "Failed", color: "#dc2626" };
    }
    if (raw.includes("pass")) {
      return { label: "Passed", color: "#059669" };
    }

    const label = raw.charAt(0).toUpperCase() + raw.slice(1);
    return { label, color: "#64748b" };
  };

  const syncInterviewDetails = async (rows: Candidate[]): Promise<Candidate[]> => {
    const interviewIds = Array.from(
      new Set(
        rows
          .map((c) => deriveInterviewId(c))
          .filter((id): id is string => Boolean(id))
          .map((id) => Number.parseInt(id, 10))
          .filter((n) => Number.isFinite(n) && n > 0)
      )
    );

    if (!interviewIds.length) return rows;

    try {
      const res = await fetch(`${API_BASE}/api/v1/engagement/interviews/details-sync`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interview_ids: interviewIds }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || !Array.isArray(payload?.results)) {
        return rows;
      }

      const detailByInterviewId = new Map<string, Record<string, unknown>>();
      for (const item of payload.results) {
        const iid = String(item?.interview_id || "").trim();
        if (!iid || !item?.success) continue;
        detailByInterviewId.set(iid, item as Record<string, unknown>);
      }

      return rows.map((c) => {
        const iid = deriveInterviewId(c);
        if (!iid) return c;

        const detail = detailByInterviewId.get(String(Number.parseInt(iid, 10)) || iid) || detailByInterviewId.get(iid);
        if (!detail) return c;

        const interview = (detail.detail as Record<string, unknown> | undefined)?.interview as Record<string, unknown> | undefined;
        const nextStatus = String(interview?.status || detail.status || c.engage_status || "").trim();
        const scoreRaw = interview?.overall_score ?? detail.overall_score;
        const score = Number(scoreRaw);
        const completedAt = interview?.completed_at || detail.completed_at || c.data?.engage_completed_at || null;

        return {
          ...c,
          engage_status: nextStatus || c.engage_status,
          engage_score: Number.isFinite(score) ? score : c.engage_score,
          data: {
            ...(c.data || {}),
            engage_interview_id: iid,
            ...(nextStatus ? { engage_status: nextStatus } : {}),
            ...(Number.isFinite(score) ? { engage_score: score } : {}),
            ...(completedAt ? { engage_completed_at: completedAt } : {}),
          },
        };
      });
    } catch (error) {
      console.warn("Failed to sync interview details", error);
      return rows;
    }
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
        const hay = `${c.name || ""} ${c.email || ""} ${c.headline || ""} ${c.location || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      // Status
      if (statusFilter !== "all" && deriveStatus(c) !== statusFilter) return false;
      // Source
      if (sourceFilter !== "all" && c.source !== sourceFilter) return false;
      // Min score
      const score = c.match_score ?? c.resume_match_percentage ?? 0;
      if (score < minScore) return false;
      return true;
    });

    if (sortField !== "index") {
      const dir = sortDir === "asc" ? 1 : -1;
      rows = [...rows].sort((a, b) => {
        const getScore = (c: Candidate) => c.match_score ?? c.resume_match_percentage ?? 0;
        const getEngage = (c: Candidate) => c.engage_score ?? 0;
        switch (sortField) {
          case "name":
            return dir * (a.name || "").localeCompare(b.name || "");
          case "screening_score":
            return dir * (getScore(a) - getScore(b));
          case "engage_score":
            return dir * (getEngage(a) - getEngage(b));
          case "total_score":
            return dir * ((getScore(a) + getEngage(a)) - (getScore(b) + getEngage(b)));
          default:
            return 0;
        }
      });
    }
    return rows;
  }, [candidates, searchQuery, statusFilter, sourceFilter, minScore, sortField, sortDir]);

  const toggleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDir(prev => (prev === "asc" ? "desc" : "asc"));
    } else {
      setSortField(field);
      setSortDir(field === "name" ? "asc" : "desc");
    }
  };

  const clearFilters = () => {
    setSearchQuery("");
    setStatusFilter("all");
    setSourceFilter("all");
    setMinScore(0);
  };

  // Modal states
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null);

  // Rank-list actions (Email / Screen / SMS)
  const [messageModalOpen, setMessageModalOpen] = useState(false);
  const [selectedCandidateForEmail, setSelectedCandidateForEmail] = useState<Candidate | null>(null);

  const [isScreenModalOpen, setIsScreenModalOpen] = useState(false);
  const [screenPayload, setScreenPayload] = useState<string>("");
  const [screenLoading, setScreenLoading] = useState(false);
  const [screenError, setScreenError] = useState<string | null>(null);
  const [selectedScreenCandidateIds, setSelectedScreenCandidateIds] = useState<string[]>([]);
  const [screenApiResponse, setScreenApiResponse] = useState<any>(null);
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

    const existingJobDivaUrl =
      String(candidate.profile_url || "").trim() ||
      String(candidate.data?.profile_url || "").trim() ||
      String(candidateProfileUrls[candidateKey] || "").trim();

    if (existingJobDivaUrl) {
      window.open(existingJobDivaUrl, "_blank", "noopener,noreferrer");
      return;
    }

    try {
      const res = await fetch(`${API_BASE}/candidates/${encodeURIComponent(candidateKey)}/profile-url`);
      if (!res.ok) {
        pushToast("JobDiva profile URL not available", "info");
        return;
      }
      const payload = await res.json().catch(() => ({}));
      const url = String(payload?.profile_url || "").trim();
      if (!url) {
        pushToast("JobDiva profile URL not available", "info");
        return;
      }

      setCandidateProfileUrls(prev => ({ ...prev, [candidateKey]: url }));
      window.open(url, "_blank", "noopener,noreferrer");
    } catch {
      pushToast("Failed to fetch profile URL", "error");
    }
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
      const res = await fetch(`${API_BASE}/candidates/enrich-contact`, {
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
    setScreenLoading(true);
    setScreenError(null);
    try {
      const data = await engagement.generatePayload({
        candidateIds: [candidate.candidate_id || String(candidate.id)],
        jobId: candidate.jobdiva_id || String(jobId || ""),
      });
      setScreenPayload(data.payload);
      setSelectedScreenCandidateIds([candidate.candidate_id || String(candidate.id)]);
      setIsScreenModalOpen(true);
    } catch (err: any) {
      setScreenError(err?.message || "Failed to generate screening payload");
    } finally {
      setScreenLoading(false);
    }
  };

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
    setScreenLoading(true);
    setScreenError(null);
    setScreenApiResponse(null);
    const payloadToSend = payloadOverride ?? screenPayload;
    try {
      const data = await engagement.sendBulkInterview({
        payload: payloadToSend,
        realCandidateIds: selectedScreenCandidateIds,
      });
      setScreenApiResponse(data);
      if (data.success) {
        // Optimistically update status to Initiated for all selected candidates
        setCandidates(prev => prev.map(c => {
          const cid = String(c.candidate_id || c.id || "");
          if (selectedScreenCandidateIds.includes(cid)) {
            return {
              ...c,
              engage_status: "Initiated",
              engage_created_at: new Date().toISOString()
            };
          }
          return c;
        }));

        setTimeout(() => {
          setIsScreenModalOpen(false);
          fetchData();
        }, 1200);
      } else {
        setScreenError(data.message || "Screen API returned an error");
      }
    } catch (err: any) {
      setScreenError(err?.message || "Screen call failed");
    } finally {
      setScreenLoading(false);
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
      const res = await fetch(
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

  const fetchData = async () => {
    setIsLoading(true);
    try {
      const apiBase = API_BASE;

      // Fetch job details
      const jobRes = await fetch(`${apiBase}/jobs/${jobId}/monitored-data`);
      const jobData = await jobRes.json();

      // Handle both { data: { ... } } and flat { ... } structures
      const data = jobData.data || jobData;

      if (data) {
        setJob({
          job_id: jobId as string,
          jobdiva_id: data.jobdiva_id,
          title: data.title || data.enhanced_title || `Job ${jobId}`,
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
        const critRes = await fetch(`${apiBase}/api/jobs/${jobId}/criteria`);
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

      // Fetch candidates
      const candRes = await fetch(`${apiBase}/jobs/${jobId}/candidates`);
      const candData = await candRes.json();
      if (candData.status === "success" && Array.isArray(candData.candidates)) {
        // Initialize feedbacks state from persisted data
        const initialFeedbacks: Record<string, string> = {};
        candData.candidates.forEach((c: any) => {
          if (c.data?.feedback_type) {
            initialFeedbacks[c.id] = c.data.feedback_type;
          }
        });
        setFeedbacks(initialFeedbacks);

        // Deduplicate candidates by their JobDiva candidate ID.
        // We keep the first occurrence since they are sorted by created_at DESC from the backend.
        const seen = new Set();
        const uniqueCandidates = candData.candidates.filter((c: any) => {
          const candidateIdKey = String(c.candidate_id || "").trim();
          const emailKey = String(c.email || "").trim().toLowerCase();
          const nameKey = String(c.name || "").trim().toLowerCase();
          const dedupKey =
            candidateIdKey
              ? `cid:${candidateIdKey}`
              : emailKey
                ? `email:${emailKey}`
                : nameKey
                  ? `name:${nameKey}`
                  : `row:${String(c.id || "").trim()}`;
          if (!dedupKey) return true;
          if (seen.has(dedupKey)) return false;
          seen.add(dedupKey);
          return true;
        });

        const getSourcePriority = (source: string) => {
          const s = (source || "").toLowerCase();
          if (s.includes('applicants')) return 1;
          if (s.includes('linkedin')) return 2;
          if (s.includes('talentsearch') || s.includes('talent_search')) return 3;
          return 4;
        };

        const sorted = uniqueCandidates.sort((a: any, b: any) => {
          // 1. Primary sort by source priority
          const prioA = getSourcePriority(a.source);
          const prioB = getSourcePriority(b.source);
          if (prioA !== prioB) return prioA - prioB;

          // 2. Secondary sort by match percentage
          const totalA = (a.match_score || a.resume_match_percentage || 0);
          const totalB = (b.match_score || b.resume_match_percentage || 0);
          return totalB - totalA;
        });
        const synced = await syncInterviewDetails(sorted as Candidate[]);
        setCandidates(synced);

        // EXTRA FALLBACK: If job title is still Unknown, borrow from candidates
        setJob(prev => {
          if (!prev || prev.title === `Job ${jobId}`) {
            const firstCand = synced[0];
            const recoveredTitle = firstCand?.headline || firstCand?.job_title || `Job ${jobId}`;
            return {
              ...(prev || {}),
              job_id: jobId as string,
              title: recoveredTitle,
            };
          }
          return prev;
        });
      }
    } catch (error) {
      console.error("Error fetching ranking data:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const openDetails = (candidate: Candidate) => {
    setSelectedCandidate(candidate);
    setDetailsModalOpen(true);
  };

  const isInitialLoading = isLoading && !job && candidates.length === 0;
  const isRefreshing = isLoading && !isInitialLoading;

  return (
    <div className="max-w-[1600px] mx-auto px-2 space-y-4 pb-10">
      {/* Top Navigation */}
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

      {/* Rankings Page Header matching the exact HTML vibe */}
      <div className="bg-white rounded-[14px] border border-slate-200 p-4 flex flex-row items-center justify-between shadow-[0_2px_10px_rgba(0,0,0,0.02)]">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-3">
            {isInitialLoading ? (
              <Skeleton className="h-7 w-64 rounded bg-slate-100" />
            ) : (
              <>
                <Medal className="w-[24px] h-[24px] text-indigo-600" />
                <h2 className="text-[24px] font-bold text-slate-900 m-0 leading-none flex items-center gap-1.5">
                  {job?.title}
                  <span className="text-slate-500 font-medium text-[16px]">
                    ({job?.jobdiva_id || job?.job_id || jobId}) <span className="text-indigo-600 text-[14px] ml-1">🔗</span>
                  </span>
                </h2>
              </>
            )}
          </div>
          <div className="text-[14px] text-slate-500 font-medium mt-0.5">Candidate Rank List</div>
        </div>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-8 py-1 px-4 border-r border-slate-100">
            <div className="space-y-1.5 text-[14px] text-slate-600">
              {isInitialLoading ? (
                <>
                  <Skeleton className="h-4 w-40 bg-slate-100" />
                  <Skeleton className="h-4 w-48 bg-slate-100" />
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Total Candidates Sourced: <strong className="text-slate-900 ml-1">{candidates.length}</strong>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Resume Shortlisted Candidates: <strong className="text-slate-900 ml-1">{candidates.filter(c => c.match_score >= 70).length}</strong>
                  </div>
                </>
              )}
            </div>
            <div className="space-y-1.5 text-[14px] text-slate-600">
              {isInitialLoading ? (
                <>
                  <Skeleton className="h-4 w-40 bg-slate-100" />
                  <Skeleton className="h-4 w-32 bg-slate-100" />
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Max. Allowed Submittals: <strong className="text-slate-900 ml-1">{job?.max_allowed_submittals ?? 0}</strong>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Openings: <strong className="text-slate-900 ml-1">{job?.openings ?? 0}</strong>
                  </div>
                </>
              )}
            </div>
          </div>
          <Button variant="outline" className="w-[36px] h-[36px] p-0 flex items-center justify-center text-slate-500 hover:text-slate-800" onClick={fetchData} disabled={isLoading}>
            <RefreshCw className={`w-[16px] h-[16px] ${isRefreshing ? "animate-spin" : ""}`} />
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
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative flex-1 min-w-[260px] h-[40px]">
            <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
              <Search className="h-4 w-4 text-slate-400" />
            </div>
            <Input
              placeholder="Search name, email, headline, or location…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-full pl-10 pr-6 bg-white border-slate-200 rounded-[8px] shadow-sm text-[14px] focus:ring-indigo-500/20 focus:border-indigo-500/50"
            />
          </div>

          <div className="flex items-center gap-2 h-[40px] bg-white border border-slate-200 rounded-[8px] px-3 shadow-sm">
            <Filter className="w-3.5 h-3.5 text-slate-400" />
            <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide">Status</label>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
              className="text-[13px] font-medium text-slate-700 bg-transparent focus:outline-none cursor-pointer"
            >
              <option value="all">All</option>
              <option value="done">Done</option>
              <option value="pending">Pending</option>
            </select>
          </div>

          <div className="flex items-center gap-2 h-[40px] bg-white border border-slate-200 rounded-[8px] px-3 shadow-sm">
            <Filter className="w-3.5 h-3.5 text-slate-400" />
            <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide">Source</label>
            <select
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              className="text-[13px] font-medium text-slate-700 bg-transparent focus:outline-none cursor-pointer max-w-[180px]"
            >
              <option value="all">All</option>
              {availableSources.map(s => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2 h-[40px] bg-white border border-slate-200 rounded-[8px] px-3 shadow-sm">
            <Filter className="w-3.5 h-3.5 text-slate-400" />
            <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide">Min score</label>
            <Input
              type="number"
              min={0}
              max={100}
              value={minScore}
              onChange={(e) => {
                const n = Number.parseInt(e.target.value, 10);
                setMinScore(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0);
              }}
              className="h-7 w-16 text-[13px] font-medium border-slate-200 px-2"
            />
          </div>

          {(searchQuery || statusFilter !== "all" || sourceFilter !== "all" || minScore > 0) && (
            <button
              onClick={clearFilters}
              className="h-[40px] px-3 text-[13px] font-semibold text-slate-500 hover:text-slate-700 flex items-center gap-1"
            >
              <X className="w-3.5 h-3.5" /> Clear
            </button>
          )}

          <div className="ml-auto text-[12.5px] font-semibold text-slate-500 px-2">
            Showing <span className="text-slate-900">{filteredCandidates.length}</span> of <span className="text-slate-900">{candidates.length}</span>
          </div>
        </div>

        {/* HTML Exact Replica Table */}
        <div className="bg-white rounded-2xl border border-slate-200/60 shadow-sm overflow-hidden relative max-w-full">
          <div className="overflow-x-auto pb-2 scrollbar-thin scrollbar-thumb-slate-200 scrollbar-track-transparent">
            <Table className="table-fixed min-w-[1200px] w-full border-collapse">
              <TableHeader>
                <TableRow className="bg-slate-50/80 border-b border-slate-200 hover:bg-slate-50/80 h-[42px] transition-colors">
                  <TableHead className="w-[44px] sticky left-0 z-10 bg-white text-center font-bold text-slate-900 text-[11px] uppercase tracking-wider border-r border-[#e2e8f0] py-1 px-1">#</TableHead>
                  <TableHead className="w-[160px] sticky left-[44px] z-10 bg-white font-bold text-slate-900 text-[9.5px] uppercase tracking-wide border-r border-slate-200 py-0">
                    <button
                      onClick={() => toggleSort("name")}
                      className="flex items-center justify-between w-full h-full px-1.5 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[10px]" />
                      <span className="whitespace-nowrap flex-1 text-center">CANDIDATE</span>
                      <div className="w-[10px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "name"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[90px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">SOURCE</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[100px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">RESUME STATUS</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[90px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <button
                      onClick={() => toggleSort("screening_score")}
                      className="flex items-center justify-between w-full h-full px-1 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">MATCH SCORE</span>
                      <div className="w-[10px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "screening_score"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                   <TableHead className="w-[120px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">SCREEN STATUS</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[100px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">HARD FILTER</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[90px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <button
                      onClick={() => toggleSort("engage_score")}
                      className="flex items-center justify-between w-full h-full px-1 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">SCREEN SCORE</span>
                      <div className="w-[10px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "engage_score"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[125px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">SCREEN COMPLETED AT</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[100px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <button
                      onClick={() => toggleSort("total_score")}
                      className="flex items-center justify-between w-full h-full px-1 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">TOTAL FIT SCORE</span>
                      <div className="w-[10px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "total_score"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[90px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">JOB CONFIG</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[240px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide border-l border-slate-200 py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">ACTIONS</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[160px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide border-l border-slate-200 py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[10px]" />
                      <span className="flex-1 text-center leading-tight">FEEDBACK</span>
                      <div className="w-[10px]" />
                    </div>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className={isRefreshing ? "opacity-60 transition-opacity duration-300 pointer-events-none" : ""}>
                {isInitialLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <TableRow key={i} className="h-20">
                      <TableCell className="w-[44px] sticky left-0 z-10 bg-white border-r border-slate-200/50 px-1"><Skeleton className="h-4 w-4 mx-auto" /></TableCell>
                      <TableCell className="w-[160px] sticky left-[44px] z-10 bg-white border-r border-slate-200/50 px-1"><Skeleton className="h-12 w-40 mx-auto" /></TableCell>
                      <TableCell className="pl-6"><Skeleton className="h-8 w-24 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-8 w-20 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-32 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-6 w-12 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-6 w-16 mx-auto" /></TableCell>
                      <TableCell className="sticky right-0 bg-white z-20 border-l border-slate-200/50 pr-10"><Skeleton className="h-9 w-32 mx-auto" /></TableCell>
                    </TableRow>
                  ))
                ) : (
                  filteredCandidates.map((candidate, idx) => {
                    const screeningScore = candidate.match_score || 0;
                    const engageScore = candidate.engage_score || 0;
                    const totalScore = screeningScore + engageScore;

                    return (
                      <TableRow key={`${candidate.id || candidate.candidate_id}-${idx}`} className="border-b border-slate-100 hover:bg-indigo-50/30 transition-all duration-200 h-auto group leading-tight relative">
                        <TableCell className="w-[44px] sticky left-0 z-10 bg-white border-r border-[#e2e8f0] py-1 px-1 align-middle text-center">
                          <div className="absolute inset-0 flex items-center justify-center">{idx + 1}</div>
                        </TableCell>
                        <TableCell className="sticky left-[44px] z-10 bg-white border-r border-[#e2e8f0] w-[160px] py-1 px-1 align-middle text-center">
                          <Link
                            href={`/jobs/${jobId}/candidates/report?candidateId=${encodeURIComponent(candidate.candidate_id || candidate.id)}`}
                            className="text-[14px] font-bold text-indigo-600 hover:underline text-center w-full block mb-0.5"
                          >
                            {candidate.name}
                          </Link>
                          <span className="text-[11px] text-[#64748b] block mb-0 text-center">
                            <Mail className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.email || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <span className="text-[11px] text-[#64748b] block mb-0 text-center">
                            <Phone className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.phone || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <button
                            type="button"
                            onClick={() => openCandidateProfileUrl(candidate)}
                            className="text-[11px] text-[#6366f1] hover:underline inline-flex items-center justify-center gap-1 mt-0.5"
                            title={String(candidate.source || "").toLowerCase().includes("linkedin") ? "Open LinkedIn profile" : "Open JobDiva profile"}
                          >
                            <ExternalLink className="w-3 h-3" />
                            {String(candidate.source || "").toLowerCase().includes("linkedin") ? "LinkedIn URL" : "JobDiva URL"}
                          </button>
                          {needsContactEnrichment(candidate) && (
                            <div className="text-center mt-1">
                              {(() => {
                                const cid = String(candidate.candidate_id || candidate.id || "").trim();
                                const status = enrichStatusByCandidateId[cid];
                                const hoverStatus = status
                                  ? compactEnrichStatusMessage(status)
                                  : "Fetch missing phone/email from ZoomInfo";
                                return (
                                  <Button
                                    size="sm"
                                    className="h-5 px-1.5 bg-white border border-[#6366f1]/30 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[9px] rounded-md shadow-sm"
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
                          <span className={`text-[11px] block text-center mt-0.5 ${availabilityPillClasses(deriveAvailability(candidate))}`}>
                            <Calendar className="w-3.5 h-3.5 inline mr-1 opacity-70" /> Available: {deriveAvailability(candidate) || <span className="font-normal opacity-50">—</span>}
                          </span>
                          {candidate.data?.open_to_relocation && (
                            <span className="text-[10px] inline-block mt-0.5 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 border border-amber-200 font-medium">
                              Open to Relocation
                            </span>
                          )}
                        </TableCell>

                        <TableCell className="text-center align-middle py-1">
                          <span className="text-[11px] font-semibold text-slate-700">
                            {normalizeSourceLabel(candidate.source)}
                          </span>
                        </TableCell>

                        <TableCell className="text-center align-middle py-1">
                          <div className="flex items-center justify-center gap-1.5">
                            {(() => {
                              const statusFromData = String(candidate.data?.resume_matching_status || "").toLowerCase();
                              if (statusFromData === "done" || screeningScore > 0) {
                                return <span className="font-medium text-[12px] text-emerald-600">Completed</span>;
                              }
                              const cid = String(candidate.candidate_id || candidate.id || "");
                              const isRefreshing = refreshingResumeMatchIds.has(cid);
                              return (
                                <>
                                  <span className="font-medium text-[12px] italic text-slate-400">Pending</span>
                                  <button
                                    type="button"
                                    className="inline-flex items-center text-slate-500 hover:text-indigo-600"
                                    title="Re-run resume matching"
                                    onClick={() => handleRefreshResumeMatch(candidate)}
                                    disabled={isRefreshing}
                                  >
                                    <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? "animate-spin" : ""}`} />
                                  </button>
                                </>
                              );
                            })()}
                          </div>
                        </TableCell>

                        <TableCell className="text-center align-middle py-1 font-medium text-[#0f172a] text-[12px]">
                          <div className="flex items-center justify-center gap-1.5 w-full text-center">

                            {screeningScore > 0 ? (
                              <span
                                className="font-bold text-slate-700"
                              >
                                {screeningScore}
                              </span>
                            ) : (
                              <span className="font-normal opacity-40 italic text-slate-400">Pending</span>
                            )}
                          </div>
                        </TableCell>

                        <TableCell className="text-center align-middle py-2 group-hover:bg-indigo-50/5 transition-colors">
                          {(() => {
                            const rawStatus = String(candidate.engage_status || candidate.data?.engage_status || "").trim().toLowerCase();
                            // If in outreach phase, show the timeline
                            if (rawStatus === "initiated" || rawStatus === "sent" || rawStatus === "sms sent") {
                                return (
                                  <div className="flex flex-col items-center gap-1 py-1">
                                    <div className="flex items-center gap-1 mb-1">
                                      <div className="w-1.5 h-1.5 rounded-full bg-yellow-500 animate-pulse" />
                                      <span className="text-[10px] font-bold text-yellow-600 tracking-wide">Initiated</span>
                                    </div>
                                    {(candidate.engage_created_at || candidate.data?.engage_created_at) && (
                                      <div className="flex flex-col items-center gap-0.5 px-2 py-1 rounded-lg bg-slate-50 border border-slate-100">
                                        <div className="text-[10px] text-emerald-600 flex items-center gap-1 font-semibold" title="Outreach initiated">
                                          <Mail className="w-2.5 h-2.5" /> {formatDate(candidate.engage_created_at || candidate.data?.engage_created_at)}
                                        </div>
                                        {(() => {
                                          const baseTime = candidate.engage_created_at || candidate.data?.engage_created_at;
                                          const phoneTime = new Date(new Date(baseTime).getTime() + 30 * 60000);
                                          const isActive = new Date() > phoneTime;
                                          return (
                                            <div
                                              className={`text-[10px] flex items-center gap-1 font-semibold ${isActive ? 'text-blue-600' : 'text-slate-400'}`}
                                              title={isActive ? "Follow-up triggered" : "Scheduled follow-up"}
                                            >
                                              <Phone className="w-2.5 h-2.5" /> {formatDate(phoneTime.toISOString())}
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
                                  className="px-2.5 py-0.5 rounded-full text-[10px] font-bold border" 
                                  style={{ backgroundColor: `${color}08`, color, borderColor: `${color}30` }}
                                >
                                  {label}
                                </span>
                              </div>
                            );
                          })()}
                        </TableCell>

                        <TableCell className="text-center align-middle py-2 group-hover:bg-indigo-50/5 transition-colors">
                          {(() => {
                            const { label, color } = normalizeHardFilterStatus(candidate);
                            return (
                              <div className="flex justify-center items-center w-full">
                                <span 
                                  className="px-2.5 py-0.5 rounded-full text-[10px] font-bold border" 
                                  style={{ backgroundColor: `${color}08`, color, borderColor: `${color}30` }}
                                >
                                  {label}
                                </span>
                              </div>
                            );
                          })()}
                        </TableCell>

                        <TableCell className="text-center align-middle py-2 font-medium text-slate-700 text-[12px] group-hover:bg-indigo-50/5 transition-colors">
                          {(() => {
                            const cScore = candidate.engage_candidate_score;
                            const tScore = candidate.engage_total_score || candidate.engage_score;
                            if (cScore !== undefined && tScore !== undefined) {
                              return (
                                <span 
                                  className="text-center w-full font-bold text-slate-900 bg-slate-50/50 px-2 py-1 rounded border border-slate-100 inline-block mx-auto"
                                >
                                  {cScore}<span className="text-slate-400 font-normal mx-0.5">/</span>{tScore}
                                </span>
                              );
                            }
                            if (tScore) {
                              return (
                                <span 
                                  className="text-center w-full font-bold text-slate-900"
                                >
                                  {tScore}
                                </span>
                              );
                            }
                            return <span className="font-normal opacity-40 italic">Waiting</span>;
                          })()}
                        </TableCell>

                        <TableCell className="text-center font-medium text-slate-600 text-[11px] align-middle py-1 group-hover:bg-indigo-50/5 transition-colors">
                          {candidate.engage_completed_at || candidate.data?.engage_completed_at ? formatDate(candidate.engage_completed_at || candidate.data.engage_completed_at) : <span className="font-normal opacity-30 italic">—</span>}
                        </TableCell>

                        <TableCell className="text-center font-bold text-indigo-700 text-[13px] align-middle py-2 bg-indigo-50/10 group-hover:bg-indigo-50/30 transition-colors">
                          {totalScore ? (
                            <span 
                              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-indigo-50 border border-indigo-100 shadow-sm"
                            >
                              {totalScore}
                            </span>
                          ) : (
                            <span className="font-normal opacity-30 italic">—</span>
                          )}
                        </TableCell>

                        <TableCell className="text-center align-middle py-1 font-medium text-slate-700 text-[11px]">
                          <div className="flex items-center justify-center gap-1.5 w-full text-center">
                            {candidate.data?.config_version || <span className="font-normal opacity-50">—</span>}
                          </div>
                        </TableCell>

                        <TableCell className="text-center pr-4 pl-4 border-l border-[#e2e8f0] py-2 align-middle transition-colors group-hover:bg-indigo-50/5">
                          <div className="flex items-center justify-center gap-3">
                            <Button
                              size="sm"
                              className="h-7 px-2.5 bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white font-bold text-[9px] rounded-md shadow-sm transition-all duration-200"
                              onClick={() => handleEmailCandidate(candidate)}
                            >
                              <Mail className="w-3.5 h-3.5 mr-1" />
                              Email
                            </Button>
                            <Button
                              size="sm"
                              className="h-7 px-2.5 bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white font-bold text-[9px] rounded-md shadow-sm transition-all duration-200"
                              onClick={() => handleScreenClick(candidate)}
                              disabled={screenLoading}
                            >
                              <MessageSquare className="w-3.5 h-3.5 mr-1" />
                              Screen
                            </Button>
                            <Button
                              size="sm"
                              className="h-7 px-2.5 bg-white border border-indigo-200 text-indigo-600 hover:bg-indigo-600 hover:text-white font-bold text-[9px] rounded-md shadow-sm transition-all duration-200 disabled:opacity-50 disabled:cursor-not-allowed"

                              onClick={() => handleSmsCandidate(candidate)}
                              disabled={candidate.engage_status === "Initiated" || candidate.engage_status === "sent" || candidate.engage_status === "SMS Sent"}
                              title={(candidate.engage_status === "Initiated" || candidate.engage_status === "sent" || candidate.engage_status === "SMS Sent") ? "Outreach already initiated" : ""}
                            >
                              <Send className="w-3 h-3 mr-0.5" />
                              SMS
                            </Button>
                          </div>
                        </TableCell>

                        <TableCell className="text-center pr-4 pl-4 border-l border-[#e2e8f0] py-2 align-middle transition-colors group-hover:bg-indigo-50/5">
                          <div className="flex flex-col items-center gap-1.5">
                            <select 
                              className="w-full text-[11px] font-medium text-[#334155] bg-white border border-[#cbd5e1] rounded h-7 px-1 focus:outline-none focus:ring-1 focus:ring-indigo-500"
                              value={feedbacks[candidate.id]?.startsWith("Reject") ? "Reject" : feedbacks[candidate.id] || ""}
                              onChange={(e) => {
                                const val = e.target.value;
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
                              <option value="" disabled>Select Action...</option>
                              <option value="Submit">Submit</option>
                              <option value="Reject">Reject</option>
                            </select>
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
            </Table>
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
          experienceYears={selectedCandidate.data?.experience_years}
          matchScore={selectedCandidate.match_score}
          matchScoreDetails={selectedCandidate.data?.match_score_details}
          matchedSkills={selectedCandidate.data?.matched_skills}
          missingSkills={selectedCandidate.data?.missing_skills}
          explainability={selectedCandidate.data?.explainability}
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
        onClose={() => setIsScreenModalOpen(false)}
        initialPayload={screenPayload}
        candidateIds={selectedScreenCandidateIds}
        onSend={async (payload) => {
          setScreenPayload(payload);
          await handleSendScreen(payload);
        }}
        loading={screenLoading}
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
          const next = { ...cand, phone: picked };
          setCandidates(prev => prev.map(c => String(c.candidate_id || c.id) === cid ? next : c));
          await runScreen(next);
        }}
        title="Phone number required"
        description="PAIR can only call candidates with a phone number on file. Add it below to continue."
        primaryLabel="Save & Screen"
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
    </div>
  );
}
