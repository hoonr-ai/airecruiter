"use client";

import { useState, useEffect, useMemo } from "react";
import { useParams, useRouter } from "next/navigation";
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
  X,
  MessageSquare,
  Send,
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

interface Candidate {
  id: number;
  jobdiva_id?: string;
  candidate_id?: string;
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
  engage_status?: string;
  engage_completed_at?: string;
  availability?: string;
  created_at: string;
  data?: any;
}

type EnrichStatus = { type: "info" | "error" | "success"; message: string };
type ToastState = { type: "info" | "error" | "success"; message: string } | null;
type ToastState = { type: "info" | "error" | "success"; message: string } | null;

export default function CandidateRankingsPage() {
  const { jobId } = useParams();
  const router = useRouter();
  const engagement = useEngagementFlow();

  const [job, setJob] = useState<JobDetails | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);

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
      d.availability_status ||
      d.available ||
      d.availability ||
      c.availability ||
      null
    );
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
      }

      // Fetch candidates
      const candRes = await fetch(`${apiBase}/jobs/${jobId}/candidates`);
      const candData = await candRes.json();
      if (candData.status === "success" && Array.isArray(candData.candidates)) {
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
        setCandidates(sorted);

        // EXTRA FALLBACK: If job title is still Unknown, borrow from candidates
        setJob(prev => {
          if (!prev || prev.title === `Job ${jobId}`) {
            const firstCand = sorted[0];
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
        <div className="bg-white rounded-[12px] border border-slate-200 shadow-sm overflow-hidden relative max-w-full">
          <div className="overflow-x-auto pb-1">
            <Table className="table-fixed min-w-[1120px] w-full">
              <TableHeader>
                <TableRow className="bg-white border-b border-slate-200 hover:bg-white h-[34px]">
                  <TableHead className="w-[44px] text-center font-bold text-slate-900 text-[11px] uppercase tracking-wider border-r border-[#e2e8f0] py-1 px-1">#</TableHead>
                  {(() => {
                    // Helper that turns a column header into a sortable button.
                    // Shows an active arrow when that column is the current sort.
                    const SortIcon = ({ field }: { field: SortField }) => {
                      if (sortField !== field) {
                        return <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />;
                      }
                      return sortDir === "asc"
                        ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" />
                        : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />;
                    };
                    return null; // just a hoist trick; the component is used inline below
                  })()}
                  <TableHead className="w-[180px] font-bold text-slate-900 text-[10px] uppercase tracking-wide border-r border-slate-200 py-0">
                    <button
                      onClick={() => toggleSort("name")}
                      className="flex items-center justify-between w-full h-full px-1.5 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[20px]" />
                      <span className="whitespace-nowrap flex-1 text-center">CANDIDATE NAME</span>
                      <div className="w-[20px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "name"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[118px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">RESUME MATCHING STATUS</span>
                      <div className="w-[20px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[108px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <button
                      onClick={() => toggleSort("screening_score")}
                      className="flex items-center justify-between w-full h-full px-1 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">RESUME MATCHING SCORE</span>
                      <div className="w-[20px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "screening_score"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[98px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">SCREEN STATUS</span>
                      <div className="w-[20px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[98px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <button
                      onClick={() => toggleSort("engage_score")}
                      className="flex items-center justify-between w-full h-full px-1 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">SCREEN SCORE</span>
                      <div className="w-[20px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "engage_score"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[128px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">SCREEN COMPLETED AT</span>
                      <div className="w-[20px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[88px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <button
                      onClick={() => toggleSort("total_score")}
                      className="flex items-center justify-between w-full h-full px-1 cursor-pointer hover:bg-slate-50 transition-colors"
                    >
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">TOTAL FIT SCORE</span>
                      <div className="w-[20px] flex items-center justify-end gap-1 px-0.5">
                        {sortField === "total_score"
                          ? (sortDir === "asc" ? <ChevronUp className="w-3.5 h-3.5 text-indigo-600" /> : <ChevronDown className="w-3.5 h-3.5 text-indigo-600" />)
                          : <ChevronsUpDown className="w-3.5 h-3.5 opacity-40" />}
                      </div>
                    </button>
                  </TableHead>
                  <TableHead className="w-[78px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">JOB CONFIG</span>
                      <div className="w-[20px]" />
                    </div>
                  </TableHead>
                  <TableHead className="w-[150px] text-center font-bold text-slate-900 text-[9.5px] uppercase tracking-wide border-l border-slate-200 py-0">
                    <div className="flex items-center justify-between w-full h-full px-1">
                      <div className="w-[20px]" />
                      <span className="flex-1 text-center leading-tight">ACTIONS</span>
                      <div className="w-[20px]" />
                    </div>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className={isRefreshing ? "opacity-60 transition-opacity duration-300 pointer-events-none" : ""}>
                {isInitialLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <TableRow key={i} className="h-20">
                      <TableCell className="pl-4"><Skeleton className="h-4 w-4 mx-auto" /></TableCell>
                      <TableCell className="sticky left-0 bg-white z-20 border-r border-slate-200/50"><Skeleton className="h-12 w-64" /></TableCell>
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
                      <TableRow key={`${candidate.id || candidate.candidate_id}-${idx}`} className="border-b border-[#e2e8f0] hover:bg-slate-50/80 transition-colors h-auto group leading-tight">
                        <TableCell className="relative text-center font-semibold text-slate-500 text-[11px] border-r border-[#e2e8f0] w-[44px] p-0 align-middle">
                          <div className="absolute inset-0 flex items-center justify-center">{idx + 1}</div>
                        </TableCell>
                        <TableCell className="border-r border-[#e2e8f0] w-[180px] py-1 px-1 align-middle text-center">
                          <button
                            onClick={() => openDetails(candidate)}
                            className="text-[14px] font-bold text-indigo-600 hover:underline text-center w-full block mb-0.5"
                          >
                            {candidate.name}
                          </button>
                          <span className="text-[11px] text-[#64748b] block mb-0 text-center">
                            <Mail className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.email || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <span className="text-[11px] text-[#64748b] block mb-0 text-center">
                            <Phone className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.phone || <span className="font-normal opacity-50">—</span>}
                          </span>
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
                        </TableCell>

                        <TableCell className="text-center align-middle py-1">
                          <div className="flex items-center justify-center gap-1.5">
                            {(() => {
                              const statusFromData = String(candidate.data?.resume_matching_status || "").toLowerCase();
                              if (statusFromData === "done") {
                                return <span className="font-medium text-[12px] text-emerald-600">Done</span>;
                              }
                              if (screeningScore > 0) {
                                return <span className="font-medium text-[12px] text-emerald-600">Done</span>;
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
                              <button
                                onClick={() => openDetails(candidate)}
                                className="font-semibold text-indigo-600 hover:underline"
                                title="View detailed resume matching breakdown"
                              >
                                {screeningScore}
                              </button>
                            ) : (
                              <span className="font-normal opacity-50">—</span>
                            )}
                          </div>
                        </TableCell>

                        <TableCell className="text-center align-middle py-1">
                          <span className="font-medium text-[13px]" style={{ color: candidate.engage_status?.toLowerCase().includes("pass") ? '#059669' : '#64748b' }}>
                            {candidate.engage_status || "Pending"}
                          </span>
                        </TableCell>

                        <TableCell className="text-center align-middle py-1 font-medium text-slate-700 text-[12px]">
                          {engageScore > 0 ? (
                            <div className="flex items-center justify-center gap-1.5 w-full text-center">
                              {engageScore}
                            </div>
                          ) : (
                            <span className="font-normal opacity-50">—</span>
                          )}
                        </TableCell>

                        <TableCell className="text-center font-medium text-slate-700 text-[11px] align-middle py-1">
                          {candidate.data?.engage_completed_at ? formatDate(candidate.data.engage_completed_at) : <span className="font-normal opacity-50">—</span>}
                        </TableCell>

                        <TableCell className="text-center font-medium text-slate-700 text-[12px] align-middle py-1">
                          {totalScore || <span className="font-normal opacity-50">—</span>}
                        </TableCell>

                        <TableCell className="text-center align-middle py-1 font-medium text-slate-700 text-[11px]">
                          <div className="flex items-center justify-center gap-1.5 w-full text-center">
                            {candidate.data?.config_version || <span className="font-normal opacity-50">—</span>}
                          </div>
                        </TableCell>

                        <TableCell className="text-center pr-0.5 pl-0.5 border-l border-[#e2e8f0] py-1 align-middle transition-colors group-hover:bg-slate-50/80">
                          <div className="flex items-center justify-center gap-0.5">
                            <Button
                              size="sm"
                              className="h-6 px-1 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[8.5px] rounded-md shadow-sm"
                              onClick={() => handleEmailCandidate(candidate)}
                            >
                              <Mail className="w-3 h-3 mr-0.5" />
                              Email
                            </Button>
                            <Button
                              size="sm"
                              className="h-6 px-1 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[8.5px] rounded-md shadow-sm"
                              onClick={() => handleScreenClick(candidate)}
                              disabled={screenLoading}
                            >
                              <MessageSquare className="w-3 h-3 mr-0.5" />
                              Screen
                            </Button>
                            <Button
                              size="sm"
                              className="h-6 px-1 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[8.5px] rounded-md shadow-sm"
                              onClick={() => handleSmsCandidate(candidate)}
                            >
                              <Send className="w-3 h-3 mr-0.5" />
                              SMS
                            </Button>
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
      {toast && (
        <div className="fixed right-4 top-4 z-[90]">
          <div
            className={`rounded-lg border px-3 py-2 text-[12px] font-semibold shadow-md transition-all ${
              toast.type === "success"
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
