"use client";

import { useState, useEffect, useEffectEvent, useRef, Suspense } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import {
  History,
  Plus,
  Search,
  Linkedin,
  Filter,
  Globe,
  MapPin,
  Rocket,
  ShieldCheck,
  Zap,
  Star,
  Building2,
  PawPrint,
  LayoutGrid,
  Check,
  CheckCircle2,
  ChevronRight,
  ChevronLeft,
  Timer,
  Users,
  ArrowRight,
  Clipboard,
  Wand2,
  FileText,
  RotateCcw,
  Sparkles,
  Info,
  Save,
  Megaphone,
  Eye,
  Type,
  ArrowLeft,
  FileInput,
  CloudDownload,
  Settings,
  ListChecks,
  ChevronUp,
  ChevronDown,
  GraduationCap,
  UserCheck,
  Lightbulb,
  X,
  Box,
  Ban,
  Mail,
  MessageSquare,
  ExternalLink,
  Loader2
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { CandidateMessageModal } from "@/components/candidate-message-modal";
import { ResumeModal } from "@/components/ResumeModal";
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";
import { PasteResumeModal } from "@/components/jobs/PasteResumeModal";
import { BulkUploadSection } from "@/components/jobs/BulkUploadSection";
import { API_BASE } from "@/lib/api";
import { logger } from "@/lib/logger";

// Utility function to clean location_type values and filter out employment terms
function cleanLocationType(locationType: string | null | undefined): string {
  if (!locationType) return "";

  const employmentTerms = [
    "direct placement", "contract", "full-time", "part-time",
    "w2", "1099", "c2c", "corp to corp", "open", "pending",
    "temporary", "permanent", "temp to perm", "fulltime", "parttime",
    "consultant", "consulting", "employee", "contractor"
  ];

  const cleanType = locationType.toLowerCase().trim();

  // If the location type contains any employment terms, return empty string
  if (employmentTerms.some(term => cleanType.includes(term))) {
    return "";
  }

  // Return the original value if it's clean
  return locationType.trim();
}

type Step = 1 | 2 | 3 | 4 | 5;
type ScreeningLevel = "L1" | "L1.5" | "L2";
type EmploymentType = "W2" | "1099" | "C2C" | "Full-Time";
type ScreenQuestion = {
  id: number;
  question_text: string;
  pass_criteria: string;
  is_default: boolean;
  category: string;
  order_index: number;
  // 4.3: onsite/hybrid arrangement must knock candidates out automatically
  // when they say no. Persisted per-question so non-default recruiter-authored
  // questions can also be marked as hard filters.
  is_hard_filter?: boolean;
};

// F2: availability screening question needs a date-aware control, not free text.
// The default question is generated with category "default" (not a dedicated
// category), so we detect it via a phrase regex on the question text.
const AVAILABILITY_RE = /earliest availability|available by|start (a )?new role/i;
const isAvailabilityQuestion = (q: Pick<ScreenQuestion, "question_text">) =>
  AVAILABILITY_RE.test(q.question_text ?? "");

// Parse an existing `pass_criteria` into either {mode:'asap'} or {mode:'date',iso}.
// Falls back to 'asap' when the string isn't recognizable so the UI never renders
// an invalid date.
function parseAvailabilityCriteria(s: string): { mode: "asap" | "date"; iso?: string } {
  const raw = (s ?? "").trim();
  if (!raw) return { mode: "asap" };
  if (/ASAP/i.test(raw)) return { mode: "asap" };
  const m = raw.match(/by\s+(.+?)\s*$/i);
  if (!m) return { mode: "asap" };
  const d = new Date(m[1]);
  if (isNaN(+d)) return { mode: "asap" };
  // Normalize to ISO yyyy-mm-dd for <input type="date">.
  return { mode: "date", iso: d.toISOString().slice(0, 10) };
}

function formatAvailabilityCriteria(v: { mode: "asap" | "date"; iso?: string }): string {
  if (v.mode === "asap" || !v.iso) return "Must be available ASAP";
  // Build a human-friendly "Mar 09, 2026" style string.
  // Use UTC to avoid off-by-one from the picker's local-time parse.
  const d = new Date(`${v.iso}T00:00:00Z`);
  if (isNaN(+d)) return "Must be available ASAP";
  const formatted = d.toLocaleDateString("en-US", {
    month: "short",
    day: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  });
  return `Must be available by ${formatted}`;
}

const STEP_LABELS = {
  1: "Intake",
  2: "Publish",
  3: "Establish Rubric",
  4: "Set Filters",
  5: "Source"
};

const STEP_DESCRIPTIONS: Record<Step, string> = {
  1: "Enter a JobDiva Job ID to get started.",
  2: "Review your Hoonr-Curate-enhanced job posting and select where to publish externally.",
  3: "Define evaluation criteria and rubric for candidate assessment.",
  4: "Configure filters and requirements for candidate matching.",
  5: "Launch sourcing and begin candidate collection."
};

// Stable handle tying a rubric item to its Step-4 resume_match filter.
// Replaces the earlier "value.split('—')[0]" fragility: Step-5 sourcing
// derivation now matches by this key rather than by re-parsing the
// user-visible filter string.
const rubricKeyFor = (category: string, baseValue: string): string =>
  `${category}|${(baseValue || "").trim()}`;

const getCandidateDisplayName = (candidate: {
  name?: string;
  firstName?: string;
  lastName?: string;
  title?: string;
  source?: string;
}) => {
  const normalize = (value?: string) => {
    const cleaned = (value || "").replace(/\s+/g, " ").trim();
    if (!cleaned) return "";
    const lowered = cleaned.toLowerCase();
    if (["linkedin candidate", "professional candidate", "unknown candidate", "unknown"].includes(lowered)) {
      return "";
    }
    return cleaned;
  };

  const fullName = normalize(candidate.name);
  if (fullName) return fullName;

  const composed = normalize([candidate.firstName, candidate.lastName].filter(Boolean).join(" "));
  if (composed) return composed;

  const title = normalize(candidate.title);
  if (title) return title;

  return candidate.source === "LinkedIn" ? "LinkedIn profile" : "Unnamed candidate";
};

export default function NewJobPage() {
  return (
    <Suspense fallback={<div>Loading...</div>}>
      <NewJobPageContent />
    </Suspense>
  );
}

function NewJobPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [currentStep, setCurrentStepState] = useState<Step>(1);
  // Track the highest step the user has ever reached so the pipeline/stepper
  // at the top allows jumping back to any step they've visited, not just
  // current-1 and current+1. Without this, stepping backward from step 4 to
  // step 1 forced the user to click Next three more times to return.
  const [maxStepReached, setMaxStepReached] = useState<Step>(1);
  const setCurrentStep = (next: Step | ((prev: Step) => Step)) => {
    setCurrentStepState(prev => {
      const resolved = typeof next === "function" ? (next as (p: Step) => Step)(prev) : next;
      setMaxStepReached(current => (resolved > current ? resolved : current));
      return resolved;
    });
  };
  const [numericJobId, setNumericJobId] = useState("");
  const [jobdivaId, setJobdivaId] = useState("");
  const [jobData, setJobData] = useState<any>(null);
  const [isFetching, setIsFetching] = useState(false);
  const [isFetched, setIsFetched] = useState(false);

  // External (non-JobDiva) requirement flow
  const [isExternal, setIsExternal] = useState(false);
  const [extTitle, setExtTitle] = useState("");
  const [extCustomer, setExtCustomer] = useState("");
  const [extDescription, setExtDescription] = useState("");
  const [isCreatingExternal, setIsCreatingExternal] = useState(false);

  // Paste-resume modal (sourced candidates step)
  const [pasteResumeOpen, setPasteResumeOpen] = useState(false);
  const [pasteName, setPasteName] = useState("");
  const [pasteEmail, setPasteEmail] = useState("");
  const [pasteResumeText, setPasteResumeText] = useState("");
  const [isSavingPasteResume, setIsSavingPasteResume] = useState(false);

  // Bulk resume upload state
  const [bulkFiles, setBulkFiles] = useState<File[]>([]);
  const [isUploadingBulk, setIsUploadingBulk] = useState(false);
  const [bulkProgress, setBulkProgress] = useState<{ processed: number; failed: number; total: number } | null>(null);
  const bulkFileInputRef = useRef<HTMLInputElement | null>(null);
  const [recruiterNotes, setRecruiterNotes] = useState("");
  const [selectedEmpTypes, setSelectedEmpTypes] = useState<EmploymentType[]>([]);
  const [recruiterEmails, setRecruiterEmails] = useState<string[]>([]);
  const [emailInput, setEmailInput] = useState("");
  const [emailError, setEmailError] = useState(false);
  const [isInputInvalid, setIsInputInvalid] = useState(false);
  const [emailErrorMessage, setEmailErrorMessage] = useState("");

  // Email modal state
  const [selectedCandidateForEmail, setSelectedCandidateForEmail] = useState<any>(null);
  const [messageModalOpen, setMessageModalOpen] = useState(false);

  // Function to fetch candidate resume if not available - only real JobDiva resumes
  const fetchCandidateResume = async (candidateId: string) => {
    try {
      const response = await fetch(`${API_BASE}/candidates/${candidateId}/resume`);
      const data = await response.json();

      // Check if the API returned an error or no real resume
      if (data.status === "error" || !data.resume_text) {
        console.log(`⚠️ No real resume available for candidate ${candidateId}: ${data.message}`);
        return null; // Return null instead of fake content
      }

      // Verify it's not auto-generated content
      if (data.resume_text.includes("Professional experience details available upon request") ||
        data.resume_text.includes("Experienced professional with a strong background")) {
        console.log(`⚠️ Auto-generated content detected for candidate ${candidateId} - rejecting`);
        return null;
      }

      return data.resume_text;
    } catch (error) {
      console.error("Error fetching resume:", error);
      return null; // Return null on error instead of fake message
    }
  };

  // Enhanced resume viewing handler - only show REAL JobDiva resumes
  const handleViewResume = async (candidate: any) => {
    let resumeText = candidate.resume_text || candidate.resumeText || candidate.data?.resume_text;

    // Check if this is a fake auto-generated resume
    if (resumeText && (
      resumeText.includes("Professional experience details available upon request") ||
      resumeText.includes("Experienced professional with a strong background") ||
      resumeText.includes("Contact information and detailed work history available upon request")
    )) {
      console.log(`⚠️ Detected auto-generated resume for ${candidate.firstName} ${candidate.lastName} - skipping`);
      alert("This candidate's resume is not available from JobDiva. Only real resumes from JobDiva are displayed.");
      return;
    }

    // If no resume text available, try to fetch it from JobDiva API
    if (!resumeText || resumeText.trim() === "") {
      console.log(`🔍 Fetching real resume for candidate: ${candidate.firstName} ${candidate.lastName}`);
      resumeText = await fetchCandidateResume(candidate.id || candidate.candidateId || candidate.candidate_id);

      // If fetchCandidateResume returns null, no real resume is available
      if (!resumeText) {
        console.log(`⚠️ No real resume available for ${candidate.firstName} ${candidate.lastName}`);
        alert("This candidate's resume is not available from JobDiva API. Only real resumes from JobDiva are displayed.");
        return;
      }
    }

    // Only proceed with real resume content
    if (resumeText && resumeText.trim().length > 50) {
      setSelectedCandidateForResume({
        name: `${candidate.firstName} ${candidate.lastName}`,
        resumeText: resumeText
      });
      setResumeModalOpen(true);
    } else {
      alert("This candidate's resume is not available from JobDiva. Only real resumes from JobDiva are displayed.");
    }
  };
  const [selectedCandidateForResume, setSelectedCandidateForResume] = useState<any>(null);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const [selectedCandidateForDetails, setSelectedCandidateForDetails] = useState<any>(null);
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [jobTitle, setJobTitle] = useState("");
  const [enhancedTitle, setEnhancedTitle] = useState("");
  const [jobPosting, setJobPosting] = useState("");
  const [isGeneratingJD, setIsGeneratingJD] = useState(false);
  const [isEnhancingTitle, setIsEnhancingTitle] = useState(false);
  const [isEditingJD, setIsEditingJD] = useState(false);
  const [selectedJobBoards, setSelectedJobBoards] = useState<string[]>([]);
  const [screeningLevel, setScreeningLevel] = useState<ScreeningLevel>("L1.5");
  const [toast, setToast] = useState<{ message: string; type: "success" | "info" | "error"} | null>(null);
  const [pageSubtitle, setPageSubtitle] = useState(STEP_DESCRIPTIONS[1]);
  const [rubricData, setRubricData] = useState<any>(null);
  const [isGeneratingRubric, setIsGeneratingRubric] = useState(false);
  const [workAuthorization, setWorkAuthorization] = useState("");

  // Step 4 - Set Filters state
  const [resumeMatchFilters, setResumeMatchFilters] = useState<Array<{
    id: number;
    category: string;
    value: string;
    active: boolean;
    ai: boolean;
    fromRubric: boolean;
    // Stable handle for rubric-derived items. Lets Step-5 sourcing derivation
    // match up to Step-4 filters without string-parsing the user-facing value
    // (which carries formatted suffixes like "— 3+ yrs, Similar match").
    rubricKey?: string;
    // Per-filter weightage (default 1.0) applied inside the backend scoring
    // ratio. Clamped to [0.1, 5] at the input layer.
    weight?: number;
  }>>([]);
  const [filterIdCounter, setFilterIdCounter] = useState(1);
  // Step 4 - Phone Screen state
  const [botIntroduction, setBotIntroduction] = useState("");
  const [screenQuestions, setScreenQuestions] = useState<ScreenQuestion[]>([]);
  const [questionIdCounter, setQuestionIdCounter] = useState(1);

  // Step 5 - Sourcing state
  // Recruiter QA 5.1 / 5.2: the "JobDiva Applicants" toggle was misleading —
  // applicants auto-enroll via jobdiva_applicant_auto_sync. It's off the
  // switchboard now. Only JobDiva Talent Search is pre-ticked; the recruiter
  // opts in to LinkedIn/Dice/Exa explicitly.
  const [searchSources, setSearchSources] = useState({
    jobdiva: true,
    linkedin: false,
    dice: false,
    exa: false,
  });
  // 5.6: JobDiva Talent Search freshness window. Default 90 days — recent
  // enough to weed out stale resumes while still surfacing passive candidates.
  // 0 / null means "Any" (no freshness filter).
  const [recentDaysFilter, setRecentDaysFilter] = useState<number>(90);
  // 5.10: opt-in override to include JobDiva Talent Search candidates that
  // don't have an attached resume. Off by default — recruiters repeatedly
  // complained about "Resume not available" results poisoning the list.
  const [includeNoResume, setIncludeNoResume] = useState<boolean>(false);
  // 5.8: cached JobDiva profile URLs per candidate id. Populated on-demand
  // when the recruiter clicks a candidate name — Talent Search doesn't
  // return PROFILEURL so we enrich lazily.
  const [candidateProfileUrls, setCandidateProfileUrls] = useState<Record<string, string>>({});
  const [sourceTitles, setSourceTitles] = useState<Array<{
    id: number;
    value: string;
    matchType: 'must' | 'can' | 'exclude';
    years: number;
    recent: boolean;
    similarCount: string;
    similarTitles: string[];
    selectedSimilarTitles?: string[];
    similarExpanded?: boolean;
    fromRubric?: boolean;
  }>>([]);
  const [sourceSkills, setSourceSkills] = useState<Array<{
    id: number;
    value: string;
    matchType: 'must' | 'can' | 'exclude';
    years: number;
    recent: boolean;
    similarCount: string;
    similarSkills: string[];
    selectedSimilarSkills?: string[];
    similarExpanded?: boolean;
    fromRubric?: boolean;
  }>>([]);
  const [sourceLocations, setSourceLocations] = useState<Array<{
    id: number;
    value: string;
    radius: string;
  }>>([]);
  const [hasSeededSourceLocation, setHasSeededSourceLocation] = useState(false);
  const [sourceCompanies, setSourceCompanies] = useState<string[]>([]);
  const [sourceKeywords, setSourceKeywords] = useState<string[]>([]);
  const [sourceTitleInput, setSourceTitleInput] = useState("");
  const [sourceSkillInput, setSourceSkillInput] = useState("");
  const [sourceLocationInput, setSourceLocationInput] = useState("");
  const [sourceLocationRadius, setSourceLocationRadius] = useState("Within 25 mi");
  const [sourceCompanyInput, setSourceCompanyInput] = useState("");
  const [sourceKeywordInput, setSourceKeywordInput] = useState("");
  const [isSearching, setIsSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const [booleanStringOpen, setBooleanStringOpen] = useState(false);
  const [generatedBoolean, setGeneratedBoolean] = useState("");
  const [isRefreshingBoolean, setIsRefreshingBoolean] = useState(false);
  const [booleanUserEdited, setBooleanUserEdited] = useState(false);
  const [booleanAttempts, setBooleanAttempts] = useState<{ query: string; label: string }[]>([]);
  const MAX_BOOLEAN_ATTEMPTS = 4;
  const QUALIFIED_SCORE_THRESHOLD = 70;
  const QUALIFIED_TARGET_COUNT = 50;
  const [candidates, setCandidates] = useState<any[]>([]);
  const seenCandidateIdsRef = useRef<Set<string>>(new Set());
  const searchAbortRef = useRef<AbortController | null>(null);
  // Fires handleEnhanceJob() exactly once per session when the user first lands on
  // Step 2 without an existing AI JD. Prevents a re-fire after a user wipe and
  // re-entry — recruiter intent (blank) must be respected once the flag is set.
  const hasAutoGeneratedJDRef = useRef(false);

  // 5.8: lazily fetch the JobDiva profile URL for a Talent Search candidate.
  // Talent Search doesn't return PROFILEURL, so we hit the backend enrichment
  // endpoint on first click and cache the result.
  const fetchAndOpenProfileUrl = async (candidate: any) => {
    const candId = String(candidate?.candidate_id || candidate?.id || "").trim();
    // Prefer any URL already on the candidate (applicants path returns one).
    const existing = candidate?.profile_url || candidateProfileUrls[candId] || "";
    if (existing) {
      window.open(existing, "_blank", "noopener,noreferrer");
      return true;
    }
    if (!candId) return false;
    try {
      const apiUrl = API_BASE;
      const res = await fetch(`${apiUrl}/candidates/${encodeURIComponent(candId)}/profile-url`);
      if (!res.ok) return false;
      const data = await res.json();
      const url = (data?.profile_url || "").trim();
      if (url) {
        setCandidateProfileUrls(prev => ({ ...prev, [candId]: url }));
        window.open(url, "_blank", "noopener,noreferrer");
        return true;
      }
    } catch (e) {
      console.warn("profile-url fetch failed", e);
    }
    return false;
  };
  const [selectedCandidates, setSelectedCandidates] = useState<Set<string>>(new Set());
  const [searchStatus, setSearchStatus] = useState("Fetching applicants...");

  // Pagination state
  const [currentPage, setCurrentPage] = useState(1);
  const [candidatesPerPage, setCandidatesPerPage] = useState(10);
  const [sourceFilter, setSourceFilter] = useState<"all" | "jobdiva" | "linkedin-unipile" | "linkedin-exa" | "dice" | "upload-resume">("all");

  const matchesSourceFilter = (cand: any) => {
    const src = String(cand.source || "").toLowerCase();
    switch (sourceFilter) {
      case "all": return true;
      case "jobdiva": return src.startsWith("jobdiva");
      case "linkedin-unipile": return src === "linkedin-unipile" || src === "linkedin";
      case "linkedin-exa": return src === "linkedin-exa";
      case "dice": return src === "dice";
      case "upload-resume": return src === "upload-resume";
      default: return true;
    }
  };
  const sourceCounts = candidates.reduce((acc: Record<string, number>, c) => {
    const s = String(c.source || "").toLowerCase();
    if (s.startsWith("jobdiva")) acc["jobdiva"] = (acc["jobdiva"] || 0) + 1;
    else if (s === "linkedin-unipile" || s === "linkedin") acc["linkedin-unipile"] = (acc["linkedin-unipile"] || 0) + 1;
    else if (s === "linkedin-exa") acc["linkedin-exa"] = (acc["linkedin-exa"] || 0) + 1;
    else if (s === "dice") acc["dice"] = (acc["dice"] || 0) + 1;
    else if (s === "upload-resume") acc["upload-resume"] = (acc["upload-resume"] || 0) + 1;
    return acc;
  }, {});

  const sortedCandidates = [...candidates]
    .filter(matchesSourceFilter)
    .sort((a, b) => {
      const scoreA = a.match_score || 0;
      const scoreB = b.match_score || 0;
      return scoreB - scoreA;
    });

  const totalPages = Math.max(1, Math.ceil(sortedCandidates.length / candidatesPerPage));
  const paginatedCandidates = sortedCandidates.slice(
    (currentPage - 1) * candidatesPerPage,
    currentPage * candidatesPerPage
  );

  const visiblePages = (() => {
    if (totalPages <= 5) return Array.from({length: totalPages}, (_, i) => i + 1);
    if (currentPage <= 3) return [1, 2, 3, 4, "...", totalPages];
    if (currentPage >= totalPages - 2) return [1, "...", totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
    return [1, "...", currentPage - 1, currentPage, currentPage + 1, "...", totalPages];
  })();

  // Resume modal state
  const [selectedCandidate, setSelectedCandidate] = useState<any>(null);
  const [candidateResume, setCandidateResume] = useState<string>("");
  const [isLoadingResume, setIsLoadingResume] = useState(false);
  const [showResumeModal, setShowResumeModal] = useState(false);

  // Resume Setup load state. Gates the wizard shell so the user sees a full-page
  // loader instead of a flash-of-empty-form while we hydrate from /jobs/{id}/draft.
  const [isLoadingDraft, setIsLoadingDraft] = useState(false);

  useEffect(() => {
    const jobIdFromUrl = searchParams.get("jobId");
    if (jobIdFromUrl) {
      if (jobIdFromUrl.includes("-")) {
        setJobdivaId(jobIdFromUrl);
      } else {
        setNumericJobId(jobIdFromUrl);
      }
      setIsLoadingDraft(true);
      loadJobDraft(jobIdFromUrl).finally(() => setIsLoadingDraft(false));
    }
  }, [searchParams]);

  useEffect(() => {
    setHasSeededSourceLocation(false);
  }, [numericJobId, jobdivaId]);

  const showToast = (message: string, type: "success" | "info" | "error" = "success") => {
    setToast({ message, type });
    setTimeout(() => setToast(null), 3000);
  };

  const loadJobDraft = async (jobIdToLoad: string) => {
    try {
      const apiUrl = API_BASE;

      // 1. Fetch the basic draft info from monitored_jobs
      const draftResponse = await fetch(`${apiUrl}/jobs/${jobIdToLoad}/draft`);
      if (!draftResponse.ok) {
        console.error("Draft fetch HTTP error:", draftResponse.status);
        return false;
      }
      const draftResult = await draftResponse.json();

      // Backend returns HTTP 200 with status:error when not found
      if (draftResult.status === "error" || !draftResult.data) {
        console.error("Draft not found:", draftResult.message);
        return false;
      }

      const draft = draftResult.data;

      // 2. Hydrate `jobData` from the draft payload when possible.
      // The `/jobs/{id}/draft` endpoint now embeds a `job_details` block mirroring
      // what `/jobs/fetch` returns, sourced from monitored_jobs. For the Resume
      // Setup flow this avoids a second JobDiva round-trip (≈2-3s lag + the
      // refetch dialog) and lets us paint the wizard in a single render.
      const embeddedDetails = draft.job_details;
      const hasEmbeddedDetails = embeddedDetails && (embeddedDetails.title || embeddedDetails.customer_name);

      if (hasEmbeddedDetails) {
        setJobData(embeddedDetails);
        if (embeddedDetails.jobdiva_id) {
          setJobdivaId(embeddedDetails.jobdiva_id);
        }
        if (embeddedDetails.is_external || (embeddedDetails.jobdiva_id || "").startsWith("EXT-")) {
          setIsExternal(true);
        }
      } else {
        // Cold path: no persisted job_details yet (e.g. the user pasted a
        // JobDiva ID but hasn't saved the job). Fall back to the old JobDiva
        // fetch so the first-time flow still works.
        const detailsResponse = await fetch(`${apiUrl}/jobs/fetch`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ job_id: jobIdToLoad.trim() })
        });

        if (detailsResponse.ok) {
          const details = await detailsResponse.json();
          setJobData(details);
          if (details.jobdiva_id) {
            setJobdivaId(details.jobdiva_id);
          }
          if (details.is_external || (details.jobdiva_id || "").startsWith("EXT-")) {
            setIsExternal(true);
          }
        }
      }

      // 2. Restore specialized data for later steps (Rubric, Filters, etc.)
      // Always check for existing rubric regardless of current step to prevent redundant AI generation
      try {
        const rubricRes = await fetch(`${apiUrl}/api/v1/ai-generation/jobs/${jobIdToLoad}/rubric`);
        if (rubricRes.ok) {
          const rData = await rubricRes.json();
          // Only pre-load if it's an actual populated rubric, not an empty shell
          if (rData.titles?.length > 0 || rData.skills?.length > 0) {
            setRubricData(applyTitleRequiredSafetyNet(rData));
            // Restore screen questions if they exist in the rubric
            if (rData.screen_questions?.length) {
              setScreenQuestions(rData.screen_questions.map((q: any, i: number) => ({ ...q, id: i + 1 })));
              setQuestionIdCounter(rData.screen_questions.length + 1);
            }
            if (rData.bot_introduction) {
              setBotIntroduction(rData.bot_introduction);
            }
            console.log("✅ Existing rubric detected and pre-loaded from database.");
          } else {
            console.log("⚠️ Rubric endpoint returned empty lists, ignoring.");
          }
        }
      } catch (e) {
        console.error("No existing rubric found or failed to load:", e);
      }

      // 4. Restore form state (Draft values overlay JobDiva values)
      if (draft.title !== undefined && draft.title !== null) setJobTitle(draft.title || "");
      if (draft.enhanced_title !== undefined && draft.enhanced_title !== null) setEnhancedTitle(draft.enhanced_title || "");
      if (draft.ai_description !== undefined && draft.ai_description !== null) setJobPosting(draft.ai_description || "");
      if (draft.recruiter_notes !== undefined && draft.recruiter_notes !== null) setRecruiterNotes(draft.recruiter_notes || "");
      if (draft.selected_employment_types?.length) setSelectedEmpTypes(draft.selected_employment_types);
      if (draft.recruiter_emails?.length) setRecruiterEmails(draft.recruiter_emails);
      if (draft.screening_level) setScreeningLevel(draft.screening_level);
      if (draft.selected_job_boards?.length) setSelectedJobBoards(draft.selected_job_boards);
      if (draft.work_authorization) setWorkAuthorization(draft.work_authorization);
      if (draft.bot_introduction) setBotIntroduction(draft.bot_introduction);

      // Restore resume match filters if they exist
      if (draft.resume_match_filters && draft.resume_match_filters.length > 0) {
        // Backfill weight=1 for legacy drafts that pre-date the per-filter
        // weightage control. New drafts persist the user-set weight.
        const normalized = draft.resume_match_filters.map((f: any) => ({
          ...f,
          weight: typeof f.weight === 'number' && isFinite(f.weight) ? f.weight : 1,
        }));
        setResumeMatchFilters(normalized);
        const maxId = Math.max(...draft.resume_match_filters.map((f: any) => f.id));
        setFilterIdCounter(maxId + 1);
        console.log(`✅ Restored ${draft.resume_match_filters.length} resume match filters from database`);
      }

      // Restore sourcing filters if they exist
      if (draft.sourcing_filters) {
        const sf = draft.sourcing_filters;
        if (sf.sources) {
          // Strip the retired jobdiva_hotlist flag from persisted drafts so
          // saved jobs don't resurrect the removed checkbox.
          const { jobdiva_hotlist: _removed, ...cleanSources } = sf.sources as Record<string, boolean>;
          setSearchSources(prev => ({ ...prev, ...cleanSources }));
        }
        if (sf.titles) setSourceTitles(sf.titles);
        if (sf.skills) setSourceSkills(sf.skills);
        if (sf.locations) setSourceLocations(sf.locations);
        if (sf.companies) setSourceCompanies(sf.companies);
        if (sf.keywords) setSourceKeywords(sf.keywords);
        console.log('✅ Restored sourcing filters from database');
      }

      // 5. Navigate to the saved step
      if (draft.current_step) {
        const savedStep = draft.current_step as Step;
        setCurrentStep(savedStep);
        // Treat the saved step as previously-reached so the pipeline allows
        // hopping back to it (and any earlier step) without re-clicking Next.
        setMaxStepReached(prev => (savedStep > prev ? savedStep : prev));
        setPageSubtitle(STEP_DESCRIPTIONS[savedStep]);
        setIsFetched(true);
        setNumericJobId(jobIdToLoad);
      }

      return true;
    } catch (error) {
      console.error("Failed to load draft:", error);
    }
    return false;
  };

  const handleCreateExternal = async () => {
    if (!extTitle.trim()) {
      showToast("Please enter a job title", "info");
      return;
    }
    if (!extDescription.trim()) {
      showToast("Please paste the job description", "info");
      return;
    }
    setIsCreatingExternal(true);
    try {
      const apiUrl = API_BASE;
      const createRes = await fetch(`${apiUrl}/jobs/external/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: extTitle.trim(),
          description: extDescription.trim(),
          customer_name: extCustomer.trim() || "External",
        }),
      });
      if (!createRes.ok) {
        showToast("Failed to create external requirement", "error");
        return;
      }
      const created = await createRes.json();
      const newJobId = String(created.job_id);
      const newRef = String(created.jobdiva_id);

      setNumericJobId(newJobId);
      setJobdivaId(newRef);
      setJobTitle(extTitle.trim());
      setEnhancedTitle(extTitle.trim());
      setJobPosting(extDescription.trim());
      setJobData({
        id: newJobId,
        jobdiva_id: newRef,
        title: extTitle.trim(),
        customer_name: extCustomer.trim() || "External",
        description: extDescription.trim(),
        ai_description: extDescription.trim(),
        is_external: true,
      });
      setIsFetched(true);
      showToast("External requirement created. Extracting rubric…", "success");

      // Fire rubric extraction in the background — same endpoint JobDiva flow uses.
      try {
        const rubricRes = await fetch(`${apiUrl}/api/v1/ai-generation/jobs/generate-rubric`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            jobId: newJobId,
            jobdivaId: newRef,
            jobTitle: extTitle.trim(),
            enhancedJobTitle: extTitle.trim(),
            jobDescription: extDescription.trim(),
            jobNotes: "",
            customerName: extCustomer.trim() || "External",
            originalDescription: extDescription.trim(),
          }),
        });
        if (rubricRes.ok) {
          const rubric = await rubricRes.json();
          if (rubric && (rubric.titles?.length || rubric.skills?.length)) {
            setRubricData(applyTitleRequiredSafetyNet(rubric, extTitle));
            showToast("Rubric ready", "success");
          }
        }
      } catch (err) {
        console.error("External rubric extraction failed:", err);
      }
    } catch (err) {
      console.error("External create failed:", err);
      showToast("Failed to create external requirement", "error");
    } finally {
      setIsCreatingExternal(false);
    }
  };

  const handleSubmitPasteResume = async () => {
    if (!pasteName.trim() || !pasteResumeText.trim()) {
      showToast("Name and resume text are required", "info");
      return;
    }
    const jobRef = numericJobId || jobdivaId;
    if (!jobRef) {
      showToast("No job context found", "error");
      return;
    }
    setIsSavingPasteResume(true);
    try {
      const apiUrl = API_BASE;
      const res = await fetch(`${apiUrl}/jobs/${encodeURIComponent(jobRef)}/manual-candidate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: pasteName.trim(),
          email: pasteEmail.trim(),
          resume_text: pasteResumeText,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(err.detail || "Failed to save resume", "error");
        return;
      }
      const result = await res.json();
      const cand = result.candidate;
      // Prepend so the user sees it immediately
      setCandidates((prev: any[]) => [{
        ...cand,
        id: cand.candidate_id,
        full_name: cand.name,
      }, ...prev]);
      setPasteResumeOpen(false);
      setPasteName("");
      setPasteEmail("");
      setPasteResumeText("");
      showToast(`Saved ${cand.name} (score ${cand.match_score ?? "—"})`, "success");
    } catch (err) {
      console.error("Paste resume failed:", err);
      showToast("Failed to save resume", "error");
    } finally {
      setIsSavingPasteResume(false);
    }
  };

  const handleBulkUpload = async () => {
    if (!bulkFiles.length) {
      showToast("Select one or more resume files first", "info");
      return;
    }
    const jobRef = numericJobId || jobdivaId;
    if (!jobRef) {
      showToast("No job context found", "error");
      return;
    }
    setIsUploadingBulk(true);
    setBulkProgress({ processed: 0, failed: 0, total: bulkFiles.length });
    try {
      const apiUrl = API_BASE;
      const formData = new FormData();
      bulkFiles.forEach(f => formData.append("files", f));
      const res = await fetch(`${apiUrl}/jobs/${encodeURIComponent(jobRef)}/bulk-resumes`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showToast(err.detail || "Bulk upload failed", "error");
        return;
      }
      const result = await res.json();
      const newCands = (result.candidates || []).map((c: any) => ({
        ...c,
        id: c.candidate_id,
        full_name: c.name,
      }));
      setCandidates((prev: any[]) => [...newCands, ...prev]);
      setBulkProgress({ processed: result.processed_count || 0, failed: result.failed_count || 0, total: bulkFiles.length });
      setBulkFiles([]);
      if (bulkFileInputRef.current) bulkFileInputRef.current.value = "";
      const msg = result.failed_count
        ? `Processed ${result.processed_count}, failed ${result.failed_count}`
        : `Processed ${result.processed_count} resume${result.processed_count === 1 ? "" : "s"}`;
      showToast(msg, result.failed_count ? "info" : "success");
    } catch (err) {
      console.error("Bulk upload failed:", err);
      showToast("Bulk upload failed", "error");
    } finally {
      setIsUploadingBulk(false);
    }
  };

  const handleFetchJob = async () => {
    const isValidJobDivaId = (id: string) => id.trim().includes("-");

    if (!isValidJobDivaId(jobdivaId)) {
      showToast("Please enter a valid JobDiva Reference code (e.g., 26-06182)", "info");
      return;
    }

    const searchId = jobdivaId.trim();

    setIsFetching(true);
    setIsFetched(false);

    // RESET all states before new fetch to prevent stale data
    setJobTitle("");
    setEnhancedTitle("");
    setJobPosting("");
    setRecruiterNotes("");
    setSelectedEmpTypes([]);
    setRecruiterEmails([]);
    setSelectedEmpTypes([]);

    try {
      const apiUrl = API_BASE;
      const response = await fetch(`${apiUrl}/jobs/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: searchId })
      });

      if (!response.ok) {
        showToast("Job not found. Check the ID.", "info");
        return;
      }

      const data = await response.json();

      // Completeness Check: Ensure the job has at least a title
      if (!data.title) {
        showToast("Job not found or incomplete data from JobDiva.", "info");
        return;
      }

      setJobData(data); // Store the full data object from backend

      if (data.id) {
        console.log(`🔄 Identifier Resolved: Syncing internal numericJobId to Numeric PK '${data.id}'`);
        setNumericJobId(data.id.toString());
      }
      if (data.jobdiva_id) {
        console.log(`🔄 Ref Code Resolved: Setting UI jobdivaId to '${data.jobdiva_id}'`);
        setJobdivaId(data.jobdiva_id.toString());
      }

      const displayData = {
        title: data.title,
        customer_name: data.customer_name || data.customer,
        location: `${data.city || ""}, ${data.state || ""}`.trim() || "Remote",
        openings: data.openings || "1",
        type: data.employment_type || "Full-Time",
        rate: data.pay_rate || "Market Rate",
        startDate: data.start_date || "ASAP",
        postedDate: data.posted_date || "Recently posted",
        description: data.description
      };

      // Auto-populate intake form fields from JobDiva data
      console.log("Auto-populating intake form with JobDiva data...", data);

      // 1. Job Title and Description
      setJobTitle(data.title || "");
      setEnhancedTitle(data.enhanced_title || data.title || "");

      // Strict Check for AI Description
      // If JobDiva result has "" or null for ai_description, then setJobPosting to ""
      // We no longer fall back to data.description to respect clearing intentionality
      if (data.ai_description !== undefined && data.ai_description !== null) {
        setJobPosting(data.ai_description);
      } else {
        setJobPosting("");
      }


      // 2. Employment Type - auto-select from JobDiva OR restore previously selected types
      if (data.selected_employment_types && Array.isArray(data.selected_employment_types) && data.selected_employment_types.length > 0) {
        console.log("Restoring previously selected employment types:", data.selected_employment_types);
        setSelectedEmpTypes(data.selected_employment_types as EmploymentType[]);
      } else if (data.employment_type) {
        const empType = data.employment_type as EmploymentType;
        if (["W2", "1099", "C2C", "Full-Time"].includes(empType)) {
          setSelectedEmpTypes([empType]);
          showToast(`Employment type set to: ${empType}`, "info");
        }
      }

      // 3. Recruiter Notes - populate from JobDiva job_notes or local recruiter_notes if available
      const notes = data.recruiter_notes !== undefined ? data.recruiter_notes : data.job_notes;
      setRecruiterNotes(notes || "");
      if (notes) {
        showToast("Recruiter notes populated", "info");
      }

      // 4. Recruiter Emails - auto-populate from local database OR JobDiva recruiter_emails
      if (data.recruiter_emails && Array.isArray(data.recruiter_emails) && data.recruiter_emails.length > 0) {
        const validEmails = data.recruiter_emails.filter((email: string) =>
          email && typeof email === 'string' && /^\S+@\S+\.\S+$/.test(email.trim())
        );
        if (validEmails.length > 0) {
          setRecruiterEmails(validEmails);
          showToast(`${validEmails.length} recruiter email(s) populated`, "info");
        }
      }

      // 5. Set default screening level from database OR to L1.5 (recommended)
      setScreeningLevel(data.screening_level || "L1.5");

      // 6. Set Work Authorization from JobDiva
      if (data.work_authorization) {
        setWorkAuthorization(data.work_authorization);
      }

      // 7. Publish To (Job Boards) - auto-populate from local database
      if (data.selected_job_boards && Array.isArray(data.selected_job_boards) && data.selected_job_boards.length > 0) {
        setSelectedJobBoards(data.selected_job_boards);
        showToast(`Restored ${data.selected_job_boards.length} job board selection(s)`, "info");
      }

      setIsFetched(true);

      // FORCE: Always stay on step 1 for newly imported jobs to follow normal workflow
      setCurrentStep(1);
      setPageSubtitle(`${displayData.title} · ${displayData.customer_name}`);
      showToast("Job intake form auto-populated from JobDiva.", "success");
    } catch (error: any) {
      console.error("Error fetching job:", error);
      showToast(error.message === "Job not found or incomplete data from JobDiva." ? "Job not found. Check the ID." : "Failed to fetch job. Use format: 26-06182", "info");
    } finally {
      setIsFetching(false);
    }
  };

  const handleEnhanceJob = async (titleOverride?: string, descOverride?: string, notesOverride?: string) => {
    setIsGeneratingJD(true);
    try {
      const response = await fetch(`${API_BASE}/api/v1/ai-generation/jobs/${numericJobId || jobdivaId || 'new'}/generate-description`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: titleOverride || jobTitle,
          jobDescription: descOverride || jobData?.description || jobPosting,
          jobNotes: notesOverride === undefined ? recruiterNotes : notesOverride,
          workAuthorization: selectedEmpTypes.join(", "),
          // Forward rubric-derived context so the backend prompt can include
          // required YoE and Education/Certs without paraphrasing them away.
          yearsOfExperience: typeof rubricData?.total_years === "number"
            ? rubricData.total_years
            : (parseInt(rubricData?.total_years, 10) || null),
          education: Array.isArray(rubricData?.education) ? rubricData.education : [],
          certifications: Array.isArray(rubricData?.certifications) ? rubricData.certifications : [],
        })
      });

      if (!response.ok) {
        // Surface the backend's structured detail so recruiters see *why* the
        // call failed (e.g. "OPENAI_API_KEY is not set") instead of a generic
        // "AI enhancement failed" toast. Prior code swallowed `errorText` into
        // console.error and threw a constant string — making QA triage in
        // live deploys blind.
        const raw = await response.text();
        let detail = raw;
        try { detail = JSON.parse(raw).detail ?? raw; } catch { /* not JSON — keep raw */ }
        logger.error("ai_jd.enhance.failed", {
          status: response.status,
          jobId: numericJobId || jobdivaId || 'new',
          detail,
        });
        throw new Error(detail || `Failed to generate JD (${response.status})`);
      }

      const data = await response.json();
      setJobPosting(data.description);

      showToast("AI Job Description enriched!", "success");
    } catch (error) {
      const message = (error as Error)?.message ?? "unknown error";
      logger.error("ai_jd.enhance.exception", { message });
      showToast(`JD generation failed: ${message}`, "info");
    } finally {
      setIsGeneratingJD(false);
    }
  };

  // 2.1 Auto-generate the AI Job Description the first time the user lands on
  // Step 2 without one. Recruiters shouldn't have to click Regenerate to see the
  // initial draft — the persisted-draft loader already skips this by populating
  // `jobPosting`, so re-entering an existing job is a no-op.
  useEffect(() => {
    if (currentStep !== 2) return;
    if (jobPosting && jobPosting.trim().length > 0) return;
    if (hasAutoGeneratedJDRef.current) return;
    if (isGeneratingJD) return;
    if (!jobTitle && !jobData?.description) return;
    // Wait for JobDiva hydration to finish before the first auto-fire, so the
    // generator sees recruiter notes + rubric (education, total_years) that
    // arrive asynchronously. If the user typed the job manually (no jobdivaId
    // and no pending fetch), skip this guard.
    const awaitingJobDivaImport = Boolean(jobdivaId) && !isFetched && isFetching;
    if (awaitingJobDivaImport) return;
    hasAutoGeneratedJDRef.current = true;
    handleEnhanceJob();
    // Intentionally depend only on the trigger inputs; `handleEnhanceJob` is
    // stable enough for this guarded single-fire.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStep, jobPosting, isGeneratingJD, jobTitle, jobData?.description, isFetched, isFetching, jobdivaId, recruiterNotes]);

  const handleEnhanceTitle = async () => {
    if (!jobTitle) return;
    setIsEnhancingTitle(true);
    try {
      const apiUrl = API_BASE;
      const res = await fetch(`${apiUrl}/api/v1/ai-generation/jobs/generate-title`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: jobTitle, // Always use original title as base for enhancement
          enhancedTitle: enhancedTitle, // Pass current enhanced title just in case 
          jobNotes: recruiterNotes,
          jobDescription: jobPosting
        })
      });
      if (res.ok) {
        const data = await res.json();
        setEnhancedTitle(data.title);

        showToast("Title enhanced by Hoonr-Curate.", "success");
      } else {
        const err = await res.text();
        console.error("Title enhance failed:", err);
        showToast("Failed to enhance title.", "info");
      }
    } catch (e) {
      console.error(e);
      showToast("Failed to enhance title.", "info");
    } finally {
      setIsEnhancingTitle(false);
    }
  };

  const handleAddEmail = () => {
    const trimmed = emailInput.trim();
    if (trimmed && /^\S+@\S+\.\S+$/.test(trimmed) && !recruiterEmails.includes(trimmed)) {
      setRecruiterEmails([...recruiterEmails, trimmed]);
      setEmailInput("");
      setEmailError(false);
      setIsInputInvalid(false);
      setEmailErrorMessage("");
    } else if (trimmed && !/^\S+@\S+\.\S+$/.test(trimmed)) {
      setIsInputInvalid(true);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === "," || e.key === ";") {
      e.preventDefault();
      handleAddEmail();
    }
  };

  const removeEmail = (email: string) => {
    setRecruiterEmails(recruiterEmails.filter(e => e !== email));
  };

  const toggleEmpType = (type: EmploymentType) => {
    setSelectedEmpTypes(prev => {
      const newTypes = prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type];
      return newTypes;
    });
  };

  const toggleJobBoard = (board: string) => {
    setSelectedJobBoards(prev => {
      const newSelection = prev.includes(board) ? prev.filter(b => b !== board) : [...prev, board];
      return newSelection;
    });
  };

  const saveJobDraft = async (stepData: {
    currentStep: number,
    saveType?: string,
    skipToast?: boolean
  }) => {
    if (!jobData || (!numericJobId && !jobdivaId)) {
      showToast("Job data not available for saving.", "info");
      return false;
    }

    try {
      const apiUrl = API_BASE;
      // Use the new endpoint that saves directly to monitored_jobs
      const response = await fetch(`${apiUrl}/jobs/${numericJobId || jobdivaId}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: numericJobId || jobdivaId,
          jobdiva_id: jobdivaId || jobData?.jobdiva_id || jobData?.id?.toString(),
          user_session: "default", // Add user session parameter required by API
          current_step: stepData.currentStep,
          title: jobTitle,
          customer_name: jobData?.customer_name || jobData?.customer || "",
          enhanced_title: enhancedTitle,
          ai_description: jobPosting,
          recruiter_notes: recruiterNotes,
          work_authorization: workAuthorization || jobData?.work_authorization || "",
          selected_employment_types: selectedEmpTypes,
          recruiter_emails: recruiterEmails,
          screening_level: screeningLevel,
          selected_job_boards: selectedJobBoards,
          rubric: {
            ...getNormalizedRubricPayload(),
            screen_questions: screenQuestions
          }, // 🔥 SEND FULL RUBRIC DATA + Screen Questions
          bot_introduction: botIntroduction,
          resume_match_filters: resumeMatchFilters.map(f => ({
            id: f.id,
            category: f.category,
            value: f.value,
            active: f.active,
            ai: f.ai,
            fromRubric: f.fromRubric
          })),
          sourcing_filters: {
            sources: searchSources,
            titles: sourceTitles,
            skills: sourceSkills,
            locations: sourceLocations,
            companies: sourceCompanies,
            keywords: sourceKeywords
          },
          step1_completed: stepData.currentStep >= 1,
          step2_completed: stepData.currentStep >= 2,
          step3_completed: stepData.currentStep >= 3,
          is_auto_saved: stepData.saveType === "auto"
        })
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        const errorMessage = errorData?.detail || errorData?.message || `Save failed (HTTP ${response.status})`;
        console.error("API Error Response:", errorData);
        throw new Error(errorMessage);
      }

      const result = await response.json();
      if (!stepData.skipToast) {
        showToast(stepData.saveType === "auto" ? "Auto-saved to monitored jobs" : "Saved to monitored jobs successfully", "success");
      }
      return true;
    } catch (error) {
      console.error("Error saving job to monitored jobs:", error);
      if (!stepData.skipToast) {
        const errorMsg = error instanceof Error ? error.message : "Failed to save. Please try again.";
        showToast(errorMsg, "error");
      }
      return false;
    }
  };

  const StepIndicator = () => (
    <div className="flex items-start mb-8 relative">
      {Object.entries(STEP_LABELS).map(([step, label], index) => {
        const stepNumber = parseInt(step) as Step;
        const isActive = stepNumber === currentStep;
        const isCompleted = stepNumber < currentStep;
        // A step is clickable when the user has already reached it before
        // (anywhere <= maxStepReached) OR it's the immediate next step and the
        // current step is unlocked (jobData present). This lets users bounce
        // back and forth in the pipeline without re-clicking Next on every
        // intermediate step.
        const isClickable =
          stepNumber <= maxStepReached ||
          (stepNumber === currentStep + 1 && !!jobData);
        const isLast = index === Object.keys(STEP_LABELS).length - 1;

        return (
          <div key={step} className="flex-1 flex flex-col items-center relative z-10">
            <div
              className={`flex flex-col items-center w-full ${isClickable ? "cursor-pointer" : "cursor-not-allowed"}`}
              onClick={() => isClickable && setCurrentStep(stepNumber)}
            >
              <div className="relative flex items-center justify-center w-full mb-3">
                {/* Connector Line — pinned perfectly between bubbles */}
                {!isLast && (
                  <div
                    className={`absolute top-1/2 left-[calc(50%+18px)] right-[-50%] h-[2.5px] -translate-y-1/2 -z-10 transition-colors duration-300 ${isCompleted ? "bg-[#10b981]" : "bg-slate-200"}`}
                  />
                )}

                <div className={`
                  w-7 h-7 rounded-full flex items-center justify-center text-[13px] font-bold transition-all duration-300 relative z-10
                  ${isActive ? "bg-primary text-white shadow-[0_0_0_6px_rgba(99,102,241,0.12)]" : ""}
                  ${isCompleted ? "bg-[#10b981] text-white" : ""}
                  ${!isActive && !isCompleted ? "bg-slate-200 text-slate-500" : ""}
                `}>
                  {isCompleted ? <Check className="w-4 h-4 stroke-[3]" /> : stepNumber}
                </div>
              </div>
              <span className={`text-[12px] font-medium transition-colors duration-200 whitespace-nowrap text-center
                ${isActive ? "text-primary" : ""}
                ${isCompleted ? "text-[#10b981]" : ""}
                ${!isActive && !isCompleted ? "text-slate-400" : ""}
              `}>
                {label}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );

  // Helper component to format AI-generated postings with rich text rendering
  const AIPostingJobDescription = ({ text }: { text: string }) => {
    const renderInline = (content: string) => {
      // Parse [text](url), **bold** and *italic*
      const parts = content.split(/(\[.*?\]\(.*?\)+|\*\*.*?\*\*|\*(?!\*).*?\*(?!\*))/g);
      return parts.map((part, i) => {
        if (part.startsWith('[') && part.includes('](') && part.endsWith(')')) {
          const match = part.match(/\[(.*?)\]\((.*?)\)/);
          if (match) {
            return (
              <a key={i} href={match[2]} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
                {match[1]}
              </a>
            );
          }
        } else if (part.startsWith('**') && part.endsWith('**')) {
          return <strong key={i} className="font-semibold text-slate-900">{part.slice(2, -2)}</strong>;
        } else if (part.startsWith('*') && part.endsWith('*')) {
          return <em key={i} className="italic text-slate-800">{part.slice(1, -1)}</em>;
        }
        return <span key={i}>{part}</span>;
      });
    };

    const formatLines = (rawText: string) => {
      if (!rawText) return null;
      return rawText.split('\n').map((line, index) => {
        const trimmedLine = line.trim();
        if (!trimmedLine) return <div key={index} className="h-2" />;

        // Header check: starts with bold all caps or is just an all caps line
        const isHeader = /^\*\*[A-Z\s]+\*\*$/.test(trimmedLine) || /^[A-Z\s]{3,25}$/.test(trimmedLine);
        if (isHeader) {
          const title = trimmedLine.replace(/\*\*/g, '').trim();
          return (
            <div key={index} className="text-[15px] font-semibold text-slate-900 mt-5 mb-2 first:mt-0 uppercase tracking-tight">
              {title}
            </div>
          );
        }

        // Bullet points
        if (trimmedLine.startsWith('•') || trimmedLine.startsWith('-')) {
          const content = trimmedLine.replace(/^[•-]\s*/, '').trim();
          return (
            <div key={index} className="flex gap-2.5 ml-1 my-1.5 items-start">
              <span className="text-slate-400 mt-1">•</span>
              <div className="flex-1">{renderInline(content)}</div>
            </div>
          );
        }

        return (
          <div key={index} className="mb-2 text-slate-600 leading-relaxed">
            {renderInline(trimmedLine)}
          </div>
        );
      });
    };

    return <div className="text-[13.5px] font-normal">{formatLines(text)}</div>;
  };

  const intakeStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      {/* Card Header — reference style: no heavy background, very subtle gradient */}
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100"
        style={{ background: "linear-gradient(135deg, #f5f3ff 0%, #ffffff 60%)" }}>
        <FileInput className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[20px] font-semibold text-slate-900 leading-tight tracking-tight">Intake</h2>
          <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">Fetch job details from JobDiva, then add any additional context for Hoonr-Curate.</p>
        </div>
      </div>

      <div className="p-7 space-y-7">
        {/* Source toggle: JobDiva vs External */}
        {!isFetched && (
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => setIsExternal(false)}
              className={`px-3 py-1.5 rounded-full text-[12px] font-semibold border transition-colors ${!isExternal ? 'bg-[#6366f1] text-white border-[#6366f1]' : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'}`}
            >
              JobDiva Requirement
            </button>
            <button
              type="button"
              onClick={() => setIsExternal(true)}
              className={`px-3 py-1.5 rounded-full text-[12px] font-semibold border transition-colors ${isExternal ? 'bg-[#6366f1] text-white border-[#6366f1]' : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'}`}
            >
              External Requirement
            </button>
          </div>
        )}

        {!isExternal ? (
          <div>
            <label className="block text-[14px] font-medium text-slate-900 mb-3">JobDiva Job ID</label>
            <div className="flex items-center gap-3">
              <Input
                placeholder="e.g. 26-08025"
                value={jobdivaId}
                onChange={(e) => setJobdivaId(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && jobdivaId.trim().includes("-") && handleFetchJob()}
                className="max-w-[180px] h-[36px] bg-white border-slate-200 text-[13px]"
              />
              <button
                onClick={handleFetchJob}
                disabled={!jobdivaId.trim().includes("-") || isFetching}
                className={`h-[36px] px-3.5 rounded-lg flex items-center gap-2 text-[13px] font-medium transition-all text-white disabled:opacity-50 disabled:cursor-not-allowed ${isFetched ? "bg-[#16a34a]" : "bg-primary hover:bg-[#5b21b6]"}`}
              >
                {isFetching ? (
                  <>
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    Fetching...
                  </>
                ) : isFetched ? (
                  <>
                    <Check className="w-4 h-4" />
                    Fetched
                  </>
                ) : (
                  <>
                    <CloudDownload className="w-4 h-4" />
                    Fetch from JobDiva
                  </>
                )}
              </button>
            </div>
          </div>
        ) : !isFetched ? (
          <div className="space-y-5">
            <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-[13px] text-amber-800 leading-relaxed">
              <strong className="font-semibold">External Requirement</strong> — not linked to JobDiva. Paste the job description; Hoonr-Curate will extract skills and rubric. JobDiva-specific fields (applicant list, UDFs) will be skipped.
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-[13px] font-medium text-slate-900 mb-2">Job Title *</label>
                <Input
                  placeholder="e.g. AI Agent Engineer"
                  value={extTitle}
                  onChange={(e) => setExtTitle(e.target.value)}
                  className="h-[36px] bg-white border-slate-200 text-[13px]"
                />
              </div>
              <div>
                <label className="block text-[13px] font-medium text-slate-900 mb-2">Customer</label>
                <Input
                  placeholder="e.g. Accenture"
                  value={extCustomer}
                  onChange={(e) => setExtCustomer(e.target.value)}
                  className="h-[36px] bg-white border-slate-200 text-[13px]"
                />
              </div>
            </div>
            <div>
              <label className="block text-[13px] font-medium text-slate-900 mb-2">Job Description *</label>
              <Textarea
                placeholder="Paste the full JD (responsibilities, required skills, preferred experience, etc.)"
                value={extDescription}
                onChange={(e) => setExtDescription(e.target.value)}
                rows={10}
                className="bg-white border-slate-200 text-[13px] leading-relaxed"
              />
              <p className="text-[11px] text-slate-500 mt-2">Hoonr-Curate will extract the rubric (titles, skills, education) from this text.</p>
            </div>
            <div>
              <button
                onClick={handleCreateExternal}
                disabled={isCreatingExternal || !extTitle.trim() || !extDescription.trim()}
                className="h-[36px] px-4 rounded-lg flex items-center gap-2 text-[13px] font-medium transition-all text-white disabled:opacity-50 disabled:cursor-not-allowed bg-primary hover:bg-[#5b21b6]"
              >
                {isCreatingExternal ? (
                  <>
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                    Creating requirement…
                  </>
                ) : (
                  <>
                    <Sparkles className="w-4 h-4" />
                    Create External Requirement
                  </>
                )}
              </button>
            </div>
          </div>
        ) : (
          <div className="bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3 text-[13px] text-emerald-800 flex items-center gap-2">
            <Check className="w-4 h-4" />
            External requirement <strong>{jobdivaId}</strong> created. You can now proceed through the rubric and sourcing steps.
          </div>
        )}

        {jobData && (
          <>
            {/* Data Grid — 3 column, bordered box, reference spec */}
            <div className="border-t border-slate-100 pt-6">
              <div
                className="grid grid-cols-3 gap-y-5 gap-x-6 p-5 rounded-lg mb-6"
                style={{ background: "#f8fafc", border: "1px solid #e2e8f0" }}
              >
                {[
                  // Row 1 — Identity
                  { label: "Job Title", value: jobData.title || "—" },
                  { label: "Customer", value: jobData.customer_name || jobData.customer || "—" },
                  { label: "Status", value: jobData.status || "—" },
                  // Row 2 — Contract Terms
                  { label: "Priority", value: (!jobData.priority || jobData.priority === "[null]") ? "—" : jobData.priority },
                  { label: "Program Duration", value: (!jobData.program_duration && !jobData.duration) || jobData.program_duration === "[null]" || jobData.duration === "[null]" ? "—" : (jobData.program_duration || jobData.duration) },
                  {
                    label: "Max Allowed Submittals",
                    value: (!jobData.max_allowed_submittals && !jobData.max_submittals) || jobData.max_allowed_submittals === "[null]" || jobData.max_submittals === "[null]" || Number.isNaN(Number.parseInt(jobData.max_allowed_submittals ?? jobData.max_submittals, 10))
                      ? "—"
                      : Number.parseInt(jobData.max_allowed_submittals ?? jobData.max_submittals, 10).toString()
                  },
                  // Row 3 — Compensation & Slots
                  { label: "Employment Type", value: jobData.employment_type || "—" },
                  { label: "Pay Rate", value: (!jobData.pay_rate || jobData.pay_rate === "[null]") ? "—" : jobData.pay_rate },
                  { label: "Openings", value: jobData.openings || "—" },
                  // Row 4 — Where & When
                  {
                    label: "Location",
                    value: [
                      `${jobData.city || ""}, ${jobData.state || ""}`.trim(),
                      jobData.zip_code || jobData.zip ? (jobData.zip_code || jobData.zip) : null,
                      cleanLocationType(jobData.location_type) ? `(${cleanLocationType(jobData.location_type)})` : null
                    ].filter(Boolean).join(" ") || "—"
                  },
                  { label: "Job Start Date", value: jobData.start_date || "—" },
                  { label: "Job Posted Date", value: jobData.posted_date || "—" },
                ].map(({ label, value }) => (
                  <div key={label} className="flex flex-col gap-1">
                    <span className="text-[11px] font-semibold uppercase tracking-[0.06em] text-slate-400">{label}</span>
                    <span className="text-[14px] font-medium text-slate-900" title={value?.toString()}>{value}</span>
                  </div>
                ))}
              </div>

              {/* Job Description */}
              <div className="mb-5">
                <label className="block text-[14px] font-medium text-slate-900 mb-2">
                  Job Description{" "}
                  <span className="text-slate-500 font-normal ml-1">— pulled from JobDiva</span>
                </label>
                <div
                  className="rounded-md p-4 text-[13px] text-slate-900 leading-[1.75] max-h-[180px] overflow-y-auto whitespace-pre-wrap"
                  style={{ background: "#f8fafc", border: "1px solid #e2e8f0" }}
                >
                  {jobData.description}
                </div>
              </div>

              {/* Recruiter Notes */}
              <div className="mb-10">
                <label className="flex flex-col gap-1 mb-2">
                  <div className="flex items-center gap-1.5 text-[14px] font-medium text-slate-900">
                    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 text-primary"><path d="M17.414 2.586a2 2 0 00-2.828 0L7 10.172V13h2.828l7.586-7.586a2 2 0 000-2.828z" /><path fillRule="evenodd" d="M2 6a2 2 0 012-2h4a1 1 0 010 2H4v10h10v-4a1 1 0 112 0v4a2 2 0 01-2 2H4a2 2 0 01-2-2V6z" clipRule="evenodd" /></svg>
                    Recruiter Notes
                  </div>
                  <div className="flex items-start gap-1.5 px-2 py-1.5 bg-amber-50 border border-amber-100 rounded-md">
                    <Info className="w-3.5 h-3.5 text-amber-600 mt-0.5 flex-shrink-0" />
                    <span className="text-[12px] font-medium text-amber-700 leading-tight">
                      Whatever you write here will be used to generate the AI Job Description for external posting. Please be cautious of what you include.
                    </span>
                  </div>
                </label>
                <Textarea
                  placeholder="e.g. Client strongly prefers fintech background. Must be local to Atlanta metro — no relocation. W2 only, no C2C. Ideally someone with NetSuite over SAP. Start date is flexible but ASAP preferred..."
                  value={recruiterNotes}
                  onChange={(e) => {
                    setRecruiterNotes(e.target.value);
                  }}
                  rows={3}
                  className="text-[14px] border-slate-200 resize-y min-h-[100px]"
                />
              </div>

              {/* Employment Type */}
              <div className="mb-5">
                <label className="block text-[14px] font-medium text-slate-900 mb-1">
                  Employment Type <span className="text-red-500">*</span>
                </label>
                <p className="text-[13px] text-slate-500 mb-3">Select all that apply for this role.</p>
                <div className="flex flex-wrap gap-2">
                  {(["W2", "1099", "C2C", "Full-Time"] as EmploymentType[]).map(type => (
                    <button
                      key={type}
                      onClick={() => toggleEmpType(type)}
                      className={`px-4 py-1.5 rounded-full border text-[13px] font-medium transition-all cursor-pointer ${selectedEmpTypes.includes(type)
                        ? "bg-primary border-primary text-white"
                        : "bg-white border-slate-300 text-slate-500 hover:border-primary hover:text-primary"
                        }`}
                    >
                      {type}
                    </button>
                  ))}
                </div>
              </div>


              <div className="border-t border-slate-100 my-6" />

              {/* Hoonr-Curate Setup Section */}
              <div className="flex items-center gap-2 mb-5">
                <Settings className="w-5 h-5 text-slate-700 flex-shrink-0" />
                <span className="text-[14px] font-bold text-slate-900">Hoonr-Curate Setup</span>
                <span className="text-[12px] text-slate-500 font-normal">Configure your screening before proceeding</span>
              </div>

              <div className="mb-7">
                <label className="block text-[14px] font-medium text-slate-900 mb-2">
                  Recruiter Email(s) <span className="text-red-500">*</span>
                </label>
                <div
                  className={`flex flex-wrap items-center gap-1.5 border rounded-lg px-2.5 py-1.5 min-h-[44px] max-w-[480px] bg-white cursor-text transition-colors ${emailError || isInputInvalid ? 'border-red-400' : 'border-slate-200 focus-within:border-primary'}`}
                  onClick={() => document.getElementById('recruiter-email-input')?.focus()}
                >
                  {recruiterEmails.map(email => (
                    <span key={email} className="inline-flex items-center gap-1.5 bg-[#eff6ff] text-[#2563eb] text-[12px] font-medium px-3 py-1 rounded-full border border-[#bfdbfe]">
                      {email}
                      <button onClick={(e) => { e.stopPropagation(); removeEmail(email); }} className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-7 h-7 flex items-center justify-center rounded-md transition-all duration-200" title="Remove">
                        <X className="w-4 h-4" />
                      </button>
                    </span>
                  ))}
                  <input
                    id="recruiter-email-input"
                    type="text"
                    placeholder="you@pyramidci.com"
                    value={emailInput}
                    onChange={(e) => {
                      const val = e.target.value;
                      setEmailInput(val);
                      if (val) {
                        const trimmed = val.trim();
                        if (!trimmed.includes("@")) {
                          setIsInputInvalid(true);
                          setEmailErrorMessage("The @ symbol is missing.");
                        } else {
                          const atParts = trimmed.split("@");
                          const domain = atParts[1];
                          if (!domain || domain.trim() === "") {
                            setIsInputInvalid(true);
                            setEmailErrorMessage("Domain name is missing.");
                          } else {
                            const domainParts = domain.split(".");
                            const tld = domainParts[domainParts.length - 1];
                            const domainBody = domainParts.slice(0, -1).join('.');
                            if (domainParts.length < 2 || !domainBody || !/^[a-zA-Z]{2,6}$/.test(tld)) {
                              setIsInputInvalid(true);
                              setEmailErrorMessage("Suffix is missing or invalid (e.g. .com, .org).");
                            } else {
                              setIsInputInvalid(false);
                              setEmailErrorMessage("");
                            }
                          }
                        }
                      } else {
                        setIsInputInvalid(false);
                        setEmailErrorMessage("");
                      }
                    }}
                    onKeyDown={handleKeyPress}
                    onBlur={handleAddEmail}
                    className="flex-1 min-w-[200px] border-none outline-none text-[14px] bg-transparent py-1 placeholder:text-slate-400"
                  />
                  {emailInput && (
                    <span className="flex items-center gap-1.5 ml-auto text-[10px] font-bold uppercase tracking-wider pr-1">
                      {!isInputInvalid ? (
                        <>
                          <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />
                          <span className="text-green-600">Valid</span>
                        </>
                      ) : (
                        <>
                          <span className="text-red-500">Invalid</span>
                        </>
                      )}
                    </span>
                  )}
                </div>
                {isInputInvalid && <p className="text-[11px] text-red-500 mt-1">{emailErrorMessage}</p>}
                <p className="text-[12px] text-slate-500 mt-1.5">Press comma, semicolon, or Enter to add. You'll receive notifications for this job.</p>
              </div>

              {/* Screening Level */}
              <div>
                <label className="block text-[14px] font-medium text-slate-900 mb-1">Screening Level</label>
                <p className="text-[13px] text-slate-500 mb-4">How deeply should Hoonr-Curate screen each candidate?</p>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {/* L1 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L1" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => {
                      setScreeningLevel("L1");
                    }}
                  >
                    <div className="flex items-center gap-2 mb-3 flex-wrap">
                      <span className="inline-flex items-center justify-center px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide bg-[#ede9fe] text-[#5b21b6]">L1</span>
                      <span className="font-semibold text-[14px] text-slate-900">Basic Screen</span>
                    </div>
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <p className="flex items-start gap-1.5 text-slate-500"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><circle cx="12" cy="12" r="10" strokeWidth="2" /><polyline points="12 6 12 12 16 14" strokeWidth="2" /></svg> ~4–8 min call</p>
                      <p className="flex items-start gap-1.5 text-slate-500 leading-snug"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> Availability, location, work authorization, compensation, 1–2 skills-fit questions</p>
                      <p className="flex items-start gap-1.5 text-[#166534] font-medium"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg> Higher volume of candidates collected</p>
                      <p className="flex items-start gap-1.5 text-[#6b7280]"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM7 9a1 1 0 000 2h6a1 1 0 100-2H7z" clipRule="evenodd" /></svg> Less qualifying detail per candidate</p>
                    </div>
                  </div>

                  {/* L1.5 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L1.5" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => {
                      setScreeningLevel("L1.5");
                    }}
                  >
                    <div className="flex items-center gap-2 mb-3 flex-wrap">
                      <span className="inline-flex items-center justify-center px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide bg-[#ede9fe] text-[#5b21b6]">L1.5</span>
                      <span className="font-semibold text-[14px] text-slate-900">Standard Screen</span>
                      <span className="text-[11px] bg-[#dcfce7] text-[#166534] px-2 py-0.5 rounded-full font-semibold">Recommended</span>
                    </div>
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <p className="flex items-start gap-1.5 text-slate-500"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><circle cx="12" cy="12" r="10" strokeWidth="2" /><polyline points="12 6 12 12 16 14" strokeWidth="2" /></svg> ~8–12 min call</p>
                      <p className="flex items-start gap-1.5 text-slate-500 leading-snug"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> All L1 questions + 1–2 more skills-fit questions + probing</p>
                      <p className="flex items-start gap-1.5 text-[#166534] font-medium"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg> Best balance of depth and candidate volume</p>
                      <p className="flex items-start gap-1.5 text-[#6b7280]"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM7 9a1 1 0 000 2h6a1 1 0 100-2H7z" clipRule="evenodd" /></svg> Moderate drop-off vs. L1</p>
                    </div>
                  </div>

                  {/* L2 */}
                  <div
                    className={`flex-1 border-2 rounded-[10px] p-4 cursor-pointer transition-all ${screeningLevel === "L2" ? "border-primary bg-[#f5f3ff]" : "border-slate-200 hover:border-primary"}`}
                    onClick={() => {
                      setScreeningLevel("L2");
                    }}
                  >
                    <div className="flex items-center gap-2 mb-3 flex-wrap">
                      <span className="inline-flex items-center justify-center px-2.5 py-0.5 rounded-full text-[11px] font-bold tracking-wide bg-[#dcfce7] text-[#166534]">L2</span>
                      <span className="font-semibold text-[14px] text-slate-900">Deep Screen</span>
                    </div>
                    <div className="flex flex-col gap-1.5 text-[12px]">
                      <p className="flex items-start gap-1.5 text-slate-500"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><circle cx="12" cy="12" r="10" strokeWidth="2" /><polyline points="12 6 12 12 16 14" strokeWidth="2" /></svg> ~12–16 min call</p>
                      <p className="flex items-start gap-1.5 text-slate-500 leading-snug"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg> All L1.5 topics + 1–2 more skills/cultural fit questions</p>
                      <p className="flex items-start gap-1.5 text-[#166534] font-medium"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg> Richest candidate profiles, highest fit accuracy</p>
                      <p className="flex items-start gap-1.5 text-[#6b7280]"><svg className="w-3 h-3 mt-0.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM7 9a1 1 0 000 2h6a1 1 0 100-2H7z" clipRule="evenodd" /></svg> Fewest completions — best for niche or senior roles</p>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );

  const publishStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100"
        style={{ background: "linear-gradient(135deg, #f5f3ff 0%, #ffffff 60%)" }}>
        <Megaphone className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[20px] font-medium text-slate-900 leading-tight tracking-tight">Publish</h2>
          <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">Review your Hoonr-Curate-enhanced job posting and select where to publish externally.</p>
        </div>
      </div>
      <div className="p-7">
        <div className="flex flex-col lg:flex-row gap-6 items-start">
          <div className="flex-1 w-full relative">
            {/* Job Title Section */}
            <div className="mb-6">
              <label className="block text-[14px] font-bold text-slate-900 mb-2 ml-1">Job Title</label>
              <div className="flex items-center gap-3">
                <Input
                  value={enhancedTitle}
                  onChange={(e) => {
                    setEnhancedTitle(e.target.value);
                  }}
                  placeholder="Enhanced Job Title"
                  className="h-10 text-[14px] border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white"
                />
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleEnhanceTitle}
                  disabled={isEnhancingTitle}
                  className="h-10 px-4 flex items-center gap-2 border-slate-200 bg-white text-slate-900 hover:text-black shadow-sm text-[13px] font-bold rounded-lg disabled:opacity-50"
                >
                  <Sparkles className={`w-3.5 h-3.5 text-slate-900 ${isEnhancingTitle ? 'animate-pulse' : ''}`} />
                  {isEnhancingTitle ? 'Enhancing...' : 'Enhance'}
                </Button>
              </div>
              <p className="text-[11.5px] text-slate-400 mt-2 ml-1 font-normal italic">
                Pre-filled from JobDiva. Edit or enhance for external posting.
              </p>
            </div>

            <div className="flex items-center justify-between mb-3 mt-8">
              <div className="bg-[#eef2ff] text-[#4f46e5] flex items-center gap-1.5 px-4 py-1.5 rounded-full text-[12.5px] font-medium border border-[#ddd6fe]">
                <Sparkles className="w-3.5 h-3.5" />
                Hoonr-Curate-Enhanced Job Posting
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => handleEnhanceJob()}
                disabled={isGeneratingJD}
                className="h-9 px-4 flex items-center gap-2 border-slate-200 bg-white text-slate-900 shadow-sm text-[13px] font-bold rounded-xl hover:bg-slate-50 transition-all disabled:opacity-50"
              >
                <RotateCcw className={`w-3.5 h-3.5 text-slate-900 ${isGeneratingJD ? 'animate-spin' : ''}`} />
                {isGeneratingJD ? 'Regenerating...' : 'Regenerate'}
              </Button>
            </div>

            {isEditingJD ? (
              <div className="relative group">
                <textarea
                  autoFocus
                  value={jobPosting}
                  onChange={(e) => {
                    setJobPosting(e.target.value);
                  }}
                  onBlur={() => {
                    setIsEditingJD(false);
                  }}
                  className="w-full bg-white border-2 border-primary/40 rounded-lg p-7 h-[500px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-200 text-[13.5px] font-normal leading-relaxed text-slate-900 focus-visible:outline-none focus:ring-4 focus:ring-primary/10 transition-all resize-none"
                  placeholder="Edit Markdown here..."
                />
                <div className="absolute top-4 right-4 bg-primary text-white text-[11px] font-bold px-3 py-1.5 rounded-md shadow-md pointer-events-none animate-in fade-in duration-200">
                  Click outside to save & preview
                </div>
              </div>
            ) : (
              <div
                onClick={() => setIsEditingJD(true)}
                title="Click to edit job description"
                className="bg-slate-50/50 border border-slate-200 rounded-lg p-7 h-[500px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-200 text-[13.5px] font-normal leading-relaxed text-slate-900 cursor-text hover:border-primary/40 hover:bg-white transition-colors group relative flex items-center justify-center text-center"
              >
                {jobPosting ? (
                  <>
                    <div className="absolute top-4 right-4 bg-slate-200 text-slate-600 text-[11px] font-bold px-3 py-1.5 rounded-md shadow-sm opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none">
                      Click anywhere to edit
                    </div>
                    <div className="w-full h-full text-left">
                      <AIPostingJobDescription text={jobPosting} />
                    </div>
                  </>
                ) : (
                  <div className="flex flex-col items-center gap-4 max-w-sm px-6">
                    <div className="w-16 h-16 bg-white rounded-full shadow-sm flex items-center justify-center border border-slate-100">
                      <Sparkles className="w-8 h-8 text-primary/40" />
                    </div>
                    <div>
                      <h4 className="text-[17px] font-bold text-slate-900">No AI Description Yet</h4>
                      <p className="text-[14px] text-slate-500 mt-2 leading-relaxed">
                        This job doesn't have an AI-enhanced description. Click the
                        <strong> "Regenerate"</strong> button above to generate one now.
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      className="mt-2 border-primary/20 hover:bg-white hover:text-primary hover:border-primary/40"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleEnhanceJob();
                      }}
                    >
                      Generate AI JD
                    </Button>
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="w-full lg:w-[240px] flex-shrink-0">
            <label className="block text-[15px] font-bold text-slate-900 mb-4 ml-1">Publish To</label>
            <div className="flex flex-col border border-slate-200 rounded-2xl bg-[#F8FAFC] p-2 shadow-sm">
              {[
                { name: "LinkedIn", icon: <Linkedin className="w-4 h-4 text-[#0A66C2]" /> },
                { name: "Indeed", icon: <Search className="w-4 h-4 text-[#2164f3]" /> },
                { name: "Dice", icon: <LayoutGrid className="w-4 h-4 text-[#1565c0]" /> },
                { name: "Monster", icon: <PawPrint className="w-4 h-4 text-[#6d1f7e]" /> },
                { name: "CareerBuilder", icon: <Building2 className="w-4 h-4 text-[#00a4bd]" /> },
              ].map(board => (
                <label key={board.name} className="flex items-center gap-3 p-2.5 hover:bg-white hover:shadow-sm cursor-pointer transition-all rounded-xl group/item">
                  <Checkbox
                    checked={selectedJobBoards.includes(board.name)}
                    onCheckedChange={() => toggleJobBoard(board.name)}
                    className="w-[18px] h-[18px] rounded-md border-slate-300 data-[state=checked]:bg-[#4f46e5] data-[state=checked]:border-[#4f46e5] text-white transition-all"
                  />
                  <div className="flex items-center gap-3">
                    <div className="transition-transform group-hover/item:scale-110 duration-200">
                      {board.icon}
                    </div>
                    <span className="text-[14px] font-medium text-slate-700 group-hover/item:text-slate-900 transition-colors">
                      {board.name}
                    </span>
                  </div>
                </label>
              ))}
            </div>
            <div className="flex items-start gap-2 mt-5 px-1">
              <Info className="w-4 h-4 text-slate-400 mt-0.5 flex-shrink-0" />
              <p className="text-[12px] text-slate-500 leading-snug font-medium">
                Job posting team will receive your request to post after you Launch Hoonr-Curate.
              </p>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  const normalizeTitle = (value: string | null | undefined) =>
    (value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "");

  const getPrimaryJobTitle = () => normalizeTitle(jobData?.title || jobTitle);

  // 3.1 Belt-and-suspenders: if a skill's `value` appears (word-boundary,
  // case-insensitive) inside the job title, force it to Required. The backend
  // prompt already nudges this, but an LLM regression shouldn't downgrade a
  // title-named skill to Preferred and silently change sourcing weights.
  const applyTitleRequiredSafetyNet = (rubric: any, titleHint?: string): any => {
    if (!rubric || !Array.isArray(rubric.skills) || rubric.skills.length === 0) {
      return rubric;
    }
    const haystack = (titleHint || jobData?.title || jobTitle || "").toLowerCase();
    if (!haystack.trim()) return rubric;

    const tokenize = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const titleTokens = new Set(tokenize(haystack).split(/\s+/).filter(Boolean));

    const patched = rubric.skills.map((s: any) => {
      const val = String(s?.value || "").toLowerCase();
      if (!val) return s;
      // Substring AND token match — catches "databricks" in "Databricks
      // Data Engineer" without false-positiving on a loose "data" token
      // leaking through.
      const tokens = tokenize(val).split(/\s+/).filter(Boolean);
      const allTokensHit =
        tokens.length > 0 && tokens.every((t) => titleTokens.has(t));
      const phraseHit = haystack.includes(val);
      if (allTokensHit || phraseHit) {
        return {
          ...s,
          required: "Required",
          importance: "required",
          is_required: true,
          fromTitle: true,
        };
      }
      return s;
    });
    return { ...rubric, skills: patched };
  };

  const isRubricItemRequired = (item: any) => {
    if (!item) return false;

    if (typeof item.is_required === "boolean") {
      return item.is_required;
    }

    const rawRequired = String(
      item.required ?? item.priority ?? item.importance ?? item.matchType ?? ""
    )
      .trim()
      .toLowerCase();

    return ["required", "must", "must have", "mandatory", "hard"].includes(rawRequired);
  };

  const isDirectResumeTitle = (titleObj: any) => {
    const primaryJobTitle = getPrimaryJobTitle();
    if (!primaryJobTitle) return false;

    return normalizeTitle(titleObj?.value) === primaryJobTitle;
  };

  const getNormalizedTitleItem = (titleItem: any) => {
    if (isDirectResumeTitle(titleItem)) {
      return {
        ...titleItem,
        required: "Required",
        matchType: "Similar",
      };
    }

    return {
      ...titleItem,
      required: isRubricItemRequired(titleItem) ? "Required" : "Preferred",
      matchType: "Similar",
    };
  };

  const getNormalizedSkillItem = (skillItem: any) => ({
    ...skillItem,
    required: isRubricItemRequired(skillItem) ? "Required" : "Preferred",
    matchType: "Similar",
  });

  const getNormalizedRubricPayload = () => {
    if (!rubricData) return rubricData;

    return {
      ...rubricData,
      titles: (rubricData.titles || []).map((title: any) => getNormalizedTitleItem(title)),
      skills: (rubricData.skills || []).map((skill: any) => getNormalizedSkillItem(skill)),
      soft_skills: (rubricData.soft_skills || []).map((skill: any) => getNormalizedSkillItem(skill)),
    };
  };

  const updateRubricItem = (category: string, index: number, field: string, value: any) => {
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      const updated = { ...prev };
      updated[category] = [...updated[category]];
      if (category === "titles") {
        const nextTitle = getNormalizedTitleItem({
          ...updated[category][index],
          [field]: value,
        });
        updated[category][index] = nextTitle;
      } else if (category === "skills") {
        updated[category][index] = getNormalizedSkillItem({
          ...updated[category][index],
          [field]: value,
        });
      } else {
        updated[category][index] = { ...updated[category][index], [field]: value };
      }
      return updated;
    });
  };

  const moveRubricItem = (category: string, from: number, to: number) => {
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      const updated = { ...prev };
      const items = [...updated[category]];
      const [moved] = items.splice(from, 1);
      items.splice(to, 0, moved);
      updated[category] = items;
      return updated;
    });
  };

  const removeRubricItem = (category: string, index: number) => {
    console.log(`🗑️ Removing ${category} at index ${index}`);
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      return {
        ...prev,
        [category]: prev[category].filter((_: any, i: number) => i !== index)
      };
    });
  };

  const addRubricItem = (category: string, newItem: any) => {
    setRubricData((prev: any) => {
      if (!prev) return prev;
      const updated = { ...prev };
      if (!updated[category]) updated[category] = [];
      // For titles, always set source to 'Hoonr-Curate' and remove any other source
      if (category === 'titles') {
        const pairTitle = getNormalizedTitleItem({
          ...newItem,
          required: 'Preferred',
          matchType: 'Similar',
          source: 'Hoonr-Curate',
        });
        updated[category] = [...updated[category], pairTitle];
      } else if (category === "skills") {
        updated[category] = [...updated[category], getNormalizedSkillItem({
          ...newItem,
          matchType: "Similar",
        })];
      } else {
        updated[category] = [...updated[category], newItem];
      }
      return updated;
    });
  };

  const establishRubricStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100" style={{ background: "linear-gradient(135deg, #f5f3ff 0%, #ffffff 60%)" }}>
        <ListChecks className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[21px] font-medium text-slate-900 leading-tight tracking-tight">Establish Rubric</h2>
          <p className="text-slate-500 text-[15px] mt-1 leading-relaxed">Hoonr-Curate-extracted rubric items from the job description. These become the rubric by which candidates are graded. Edit freely.</p>
        </div>
      </div>

      {isGeneratingRubric ? (
        <div className="p-20 flex flex-col items-center justify-center gap-4">
          <div className="w-8 h-8 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
          <p className="text-[15px] font-medium text-slate-600 animate-pulse">Extracting criteria from Hoonr-Curate Job Description...</p>
        </div>
      ) : rubricData ? (
        <div className="p-7 space-y-7">

          {/* Titles */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Clipboard className="w-4 h-4 text-slate-900 flex-shrink-0" />
              <h3 className="text-[14px] font-bold text-slate-800">Titles</h3>
              <span className="text-[12px] font-normal text-slate-500">Job title for sourcing & resume matching · 5 max</span>
            </div>

            {/* Column Headers */}
            <div className="flex items-center gap-2.5 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-1">
              <div className="flex-1 min-w-0">Job Title</div>
              <div className="w-[110px] flex-shrink-0 flex items-center justify-center">
                Min. Years
              </div>
              <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                Recent
              </div>
              <div className="w-[170px] flex-shrink-0 flex items-center justify-center">
                Match Type
              </div>
              <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                Required / Preferred
              </div>
              <div className="w-[70px] flex-shrink-0"></div>
              <div className="w-[36px] flex-shrink-0"></div>
            </div>

            <div className="space-y-0">
              {rubricData.titles?.map((rawTitle: any, idx: number) => {
                const title = getNormalizedTitleItem(rawTitle);

                return (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={title.value}
                      onChange={(e) => updateRubricItem('titles', idx, 'value', e.target.value)}
                      className="flex-1 min-w-0 text-[13px] font-normal text-slate-700 bg-transparent border border-transparent rounded px-2 py-1.5 outline-none focus:border-slate-200 focus:bg-white transition-all"
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap">Hoonr-Curate</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0 flex items-center gap-1.5">
                    <input
                      type="number"
                      min={0}
                      value={title.minYears}
                      onChange={(e) => updateRubricItem('titles', idx, 'minYears', Math.max(0, parseInt(e.target.value) || 0))}
                      className="w-12 border border-slate-200 rounded px-1.5 py-1 text-[13px] text-center outline-none focus:border-[#818cf8]"
                    />
                    <span className="text-[12px] text-slate-500">{title.minYears === 0 ? '—' : 'yrs'}</span>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                    <Checkbox checked={title.recent} onCheckedChange={(checked) => updateRubricItem('titles', idx, 'recent', !!checked)} className="border-slate-300 rounded-[4px] data-[state=checked]:bg-[#6d28d9] data-[state=checked]:border-[#6d28d9] text-white w-[16px] h-[16px] hover:border-[#6d28d9] transition-all" />
                  </div>
                  <div className="w-[170px] flex-shrink-0">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[118px] bg-white cursor-pointer select-none">
                      <button
                        onClick={() => updateRubricItem('titles', idx, 'matchType', 'Exact')}
                        className={`flex-1 py-[3px] rounded-full transition-all ${title.matchType === 'Exact' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}
                      >
                        Exact
                      </button>
                      <button
                        onClick={() => updateRubricItem('titles', idx, 'matchType', 'Similar')}
                        className={`flex-1 py-[3px] rounded-full transition-all ${title.matchType === 'Similar' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}
                      >
                        Similar
                      </button>
                    </div>
                  </div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none">
                      <button
                        onClick={() => updateRubricItem('titles', idx, 'required', 'Required')}
                        className={`flex-1 py-[3px] rounded-full transition-all ${title.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}
                      >
                        Required
                      </button>
                      <button
                        onClick={() => updateRubricItem('titles', idx, 'required', 'Preferred')}
                        disabled={isDirectResumeTitle(title)}
                        className={`flex-1 py-[3px] rounded-full transition-all ${title.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'} ${isDirectResumeTitle(title) ? 'opacity-40 cursor-not-allowed' : ''}`}
                      >
                        Preferred
                      </button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex flex-col gap-1 items-center">
                    <button
                      disabled={idx === 0}
                      onClick={() => moveRubricItem('titles', idx, idx - 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      disabled={idx === (rubricData.titles?.length - 1)}
                      onClick={() => moveRubricItem('titles', idx, idx + 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronDown className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('titles', idx);
                      }}
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              )})}

              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={(rubricData.titles?.length || 0) >= 5}
                  onClick={() => addRubricItem('titles', { value: '', minYears: 0, recent: false, matchType: 'Similar', required: 'Preferred', source: 'Hoonr-Curate' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Title
                </Button>
                <span className={`ml-3 text-[13.5px] font-medium ${(rubricData.titles?.length || 0) >= 5 ? 'text-rose-600' : 'text-slate-500'}`}>
                  {(rubricData.titles?.length || 0)} / 5
                </span>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Skills */}
          <section>
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <Wand2 className="w-4 h-4 text-slate-900 flex-shrink-0" />
                <h3 className="text-[14px] font-bold text-slate-800">Skills</h3>
                <span className="text-[12px] font-normal text-slate-500">Top 8 · ordered by importance</span>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => showToast("No new suggestions — list is full or already complete.", "info")}
                className="border-slate-200 text-[#1e293b] bg-white hover:bg-slate-50 font-medium text-[13px] rounded-[7px] shadow-none h-[28px] px-2.5 border transition-all"
              >
                <Wand2 className="w-3 h-3 mr-1 text-[#7e22ce]" />
                Suggest More
              </Button>
            </div>

            {/* Column Headers */}
            <div className="flex items-center gap-2.5 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-1">
              <div className="flex-1 min-w-0">Hard Skill</div>
              <div className="w-[110px] flex-shrink-0 flex items-center justify-center">
                Min. Years
              </div>
              <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                Recent
              </div>
              <div className="w-[170px] flex-shrink-0 flex items-center justify-center">
                Match Type
              </div>
              <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                Required / Preferred
              </div>
              <div className="w-[106px] flex-shrink-0 flex items-center justify-center">
                Actions
              </div>
            </div>
            <div className="space-y-0">
              {rubricData.skills?.map((skill: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={skill.value}
                      onChange={(e) => updateRubricItem('skills', idx, 'value', e.target.value)}
                      className="flex-1 min-w-0 text-[13px] font-normal text-slate-700 bg-transparent border border-transparent rounded px-2 py-1.5 outline-none focus:border-slate-200 focus:bg-white transition-all"
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap">Hoonr-Curate</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0 flex items-center gap-1.5">
                    <input
                      type="number"
                      min={0}
                      value={skill.minYears}
                      onChange={(e) => updateRubricItem('skills', idx, 'minYears', Math.max(0, parseInt(e.target.value) || 0))}
                      className="w-12 border border-slate-200 rounded px-1.5 py-1 text-[13px] text-center outline-none focus:border-[#818cf8]"
                    />
                    <span className="text-[12px] text-slate-500">{skill.minYears === 0 ? '—' : 'yrs'}</span>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex items-center justify-center">
                    <Checkbox checked={skill.recent} onCheckedChange={(checked) => updateRubricItem('skills', idx, 'recent', !!checked)} className="border-slate-300 rounded-[4px] data-[state=checked]:bg-[#6d28d9] data-[state=checked]:border-[#6d28d9] text-white w-[16px] h-[16px] hover:border-[#6d28d9] transition-all" />
                  </div>
                  <div className="w-[170px] flex-shrink-0">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[118px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('skills', idx, 'matchType', 'Exact')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.matchType === 'Exact' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Exact</button>
                      <button onClick={() => updateRubricItem('skills', idx, 'matchType', 'Similar')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.matchType === 'Similar' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Similar</button>
                    </div>
                  </div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('skills', idx, 'required', 'Required')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('skills', idx, 'required', 'Preferred')} className={`flex-1 py-[3px] rounded-full transition-all ${skill.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0 flex flex-col gap-1 items-center">
                    <button
                      disabled={idx === 0}
                      onClick={() => moveRubricItem('skills', idx, idx - 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      disabled={idx === (rubricData.skills?.length - 1)}
                      onClick={() => moveRubricItem('skills', idx, idx + 1)}
                      className="w-[22px] h-[22px] flex items-center justify-center border border-slate-200 rounded-[4px] bg-white text-slate-400 hover:text-slate-600 hover:bg-slate-50 transition-all disabled:opacity-20 disabled:pointer-events-none"
                    >
                      <ChevronDown className="w-3.5 h-3.5" />
                    </button>
                  </div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('skills', idx);
                      }}
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="ml-1 mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={(rubricData.skills?.length || 0) >= 8}
                  onClick={() => addRubricItem('skills', { value: '', minYears: 0, recent: false, matchType: 'Similar', required: 'Preferred', source: 'Hoonr-Curate' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Skill
                </Button>
                <span className={`ml-3 text-[13.5px] font-medium ${(rubricData.skills?.length || 0) >= 8 ? 'text-rose-600' : 'text-slate-500'}`}>
                  {(rubricData.skills?.length || 0)} / 8
                </span>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Education & Certificates */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <div className="flex items-center gap-2">
                <GraduationCap className="w-4 h-4 text-slate-900" />
                <h3 className="text-[14px] font-bold text-slate-800">Education & Certificates</h3>
              </div>
              <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> Hoonr-Curate detected
              </span>
            </div>

            <div className="space-y-0">
              {rubricData.education?.map((edu: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <select
                      value={edu.degree}
                      onChange={(e) => updateRubricItem('education', idx, 'degree', e.target.value)}
                      className="h-[34px] w-[220px] bg-slate-50 border border-slate-200 rounded-lg text-slate-700 text-[13px] px-2 font-medium outline-none cursor-pointer flex-shrink-0 hover:border-slate-300 transition-all shadow-sm"
                    >
                      <option value="No requirement">No requirement</option>
                      <option value="High School / GED">High School / GED</option>
                      <option value="Associate's degree">Associate's degree</option>
                      <option value="Bachelor's degree">Bachelor's degree</option>
                      <option value="Master's degree">Master's degree</option>
                      <option value="PhD or equivalent">PhD or equivalent</option>
                      <option value="Certification / License">Certification / License</option>
                    </select>
                    <span className="text-slate-400 font-medium text-[11.5px] whitespace-nowrap flex-shrink-0 px-1">in / as</span>
                    <Input
                      value={edu.field}
                      onChange={(e) => updateRubricItem('education', idx, 'field', e.target.value)}
                      className="w-[260px] flex-shrink-0 h-[34px] text-[13px] font-medium text-slate-700 bg-white border-slate-200"
                      placeholder="Field of study"
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight whitespace-nowrap ml-1 uppercase">Hoonr-Curate</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[170px] flex-shrink-0"></div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none shadow-sm">
                      <button onClick={() => updateRubricItem('education', idx, 'required', 'Required')} className={`flex-1 py-[2.5px] rounded-full transition-all ${edu.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('education', idx, 'required', 'Preferred')} className={`flex-1 py-[2.5px] rounded-full transition-all ${edu.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('education', idx);
                      }}
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('education', { degree: "Bachelor's degree", field: '', required: 'Preferred' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Education / Certificate
                </Button>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Domain (rendered as "Industry Experience" — the data key stays
              `domain` throughout the rubric to avoid a cross-codebase rename). */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Building2 className="w-4 h-4 text-slate-900" />
              <h3 className="text-[14px] font-bold text-slate-800">Industry Experience</h3>
              <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> Detected in JD
              </span>
            </div>

            <div className="space-y-0">
              {rubricData.domain?.map((dom: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <Input
                      value={dom.value}
                      onChange={(e) => updateRubricItem('domain', idx, 'value', e.target.value)}
                      className="flex-1 h-[34px] text-[13px] font-medium text-slate-700 bg-white border-slate-200"
                    />
                    <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight whitespace-nowrap ml-2 uppercase">Hoonr-Curate</span>
                  </div>
                  <div className="w-[110px] flex-shrink-0"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[170px] flex-shrink-0"></div>
                  <div className="w-[180px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none">
                      <button onClick={() => updateRubricItem('domain', idx, 'required', 'Required')} className={`flex-1 py-[2px] rounded-full transition-all ${dom.required === 'Required' ? 'bg-[#dcfce7] text-[#166534]' : 'text-slate-400'}`}>Required</button>
                      <button onClick={() => updateRubricItem('domain', idx, 'required', 'Preferred')} className={`flex-1 py-[2px] rounded-full transition-all ${dom.required === 'Preferred' ? 'bg-[#ede9fe] text-[#6d28d9]' : 'text-slate-400'}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('domain', idx);
                      }}
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('domain', { value: '', required: 'Preferred' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Industry
                </Button>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Customer Requirements */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <UserCheck className="w-4 h-4 text-slate-900 flex-shrink-0" />
              <h3 className="text-[14px] font-bold text-slate-800">Customer Requirements</h3>
              <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full flex items-center gap-1">
                <Sparkles className="w-3 h-3" /> Hoonr-Curate generated
              </span>
            </div>

            <div className="space-y-0">
              {rubricData.customer_requirements?.map((req: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <select
                      className="h-[34px] w-[190px] bg-slate-50 border border-slate-200 rounded-lg text-slate-700 text-[13px] px-2 font-medium outline-none cursor-pointer flex-shrink-0"
                      value={req.type}
                      onChange={(e) => updateRubricItem('customer_requirements', idx, 'type', e.target.value)}
                    >
                      <option value="Must not be employed by">Must not be employed by</option>
                      <option value="Currently employed by">Currently employed by</option>
                      <option value="Previously employed by">Previously employed by</option>
                    </select>
                    <Input
                      value={req.value}
                      onChange={(e) => updateRubricItem('customer_requirements', idx, 'value', e.target.value)}
                      className="w-[350px] flex-shrink-0 h-[34px] text-[13px] font-medium text-slate-700 bg-[#fffafb] border-[#fecaca] focus:border-rose-300 focus:ring-0"
                      placeholder="Company name"
                    />
                  </div>
                  <div className="w-[110px] flex-shrink-0"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[170px] flex-shrink-0"></div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center"></div>
                  <div className="w-[70px] flex-shrink-0"></div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('customer_requirements', idx);
                      }}
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('customer_requirements', { type: 'Must not be employed by', value: '' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Requirement
                </Button>
              </div>
            </div>
          </section>

          <div className="mb-7"></div>

          {/* Other Requirements */}
          <section>
            <div className="flex items-center gap-2 mb-4">
              <Lightbulb className="w-4 h-4 text-slate-900" />
              <h3 className="text-[14px] font-bold text-slate-800">Other Requirements</h3>
              <span className="text-[12px] text-slate-500 font-normal">Location constraints, shift requirements, work authorization, etc.</span>
            </div>

            <div className="space-y-0">
              {rubricData.other_requirements?.map((req: any, idx: number) => (
                <div key={idx} className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0">
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={req.value}
                      onChange={(e) => updateRubricItem('other_requirements', idx, 'value', e.target.value)}
                      className="flex-1 text-[13px] font-medium text-slate-700 bg-transparent border-none outline-none focus:ring-0 placeholder:text-slate-400 py-1"
                      placeholder="Requirement..."
                    />
                  </div>
                  <div className="w-[190px] flex-shrink-0 flex items-center justify-center">
                    <div className="border border-slate-200 rounded-full p-[1.5px] flex items-center text-[11px] font-medium w-[135px] bg-white cursor-pointer select-none shadow-sm">
                      <button onClick={() => updateRubricItem("other_requirements", idx, "required", "Required")} className={`flex-1 py-[2.5px] rounded-full transition-all ${req.required === "Required" ? "bg-[#dcfce7] text-[#166534]" : "text-slate-400"}`}>Required</button>
                      <button onClick={() => updateRubricItem("other_requirements", idx, "required", "Preferred")} className={`flex-1 py-[2.5px] rounded-full transition-all ${req.required === "Preferred" ? "bg-[#ede9fe] text-[#6d28d9]" : "text-slate-400"}`}>Preferred</button>
                    </div>
                  </div>
                  <div className="w-[36px] flex-shrink-0 text-center">
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeRubricItem('other_requirements', idx);
                      }}
                      className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => addRubricItem('other_requirements', { value: '', required: 'Preferred' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Requirement
                </Button>
              </div>
            </div>
          </section>

        </div>
      ) : null}
    </div>
  );

  // Filter management functions
  const toggleResumeFilter = (id: number, active: boolean) => {
    setResumeMatchFilters(prev =>
      prev.map(filter =>
        filter.id === id ? { ...filter, active } : filter
      )
    );
  };

  const updateResumeFilter = (id: number, value: string) => {
    setResumeMatchFilters(prev =>
      prev.map(filter =>
        filter.id === id ? { ...filter, value } : filter
      )
    );
  };

  const deleteResumeFilter = (id: number) => {
    setResumeMatchFilters(prev => prev.filter(filter => filter.id !== id));
  };

  const addResumeFilter = () => {
    // Inline-editable draft row. User fills category + value directly inside
    // the filter card (no native prompt). Manual filters (ai=false,
    // fromRubric=false) render the category as an editable <input>.
    setResumeMatchFilters(prev => [
      ...prev,
      {
        id: filterIdCounter,
        category: 'Custom',
        value: '',
        active: true,
        ai: false,
        fromRubric: false,
        weight: 1
      }
    ]);
    setFilterIdCounter(prev => prev + 1);
  };

  const updateResumeFilterCategory = (id: number, category: string) => {
    setResumeMatchFilters(prev =>
      prev.map(filter => (filter.id === id ? { ...filter, category } : filter))
    );
  };

  // Initialize filters from rubric data when moving to step 4
  const initializeFiltersFromRubric = () => {
    if (!rubricData) return;

    const manualFilters = resumeMatchFilters.filter(filter => !filter.fromRubric);
    const filters: Array<{
      id: number;
      category: string;
      value: string;
      active: boolean;
      ai: boolean;
      fromRubric: boolean;
      rubricKey?: string;
      weight?: number;
    }> = [];

    let idCounter = 1;

    // Preserve user's active/inactive preferences AND custom weight for
    // existing filters across rubric re-inits. Key on the stable rubricKey
    // (when present) or derive one from the filter's base value.
    const existingFilterPrefs = new Map<string, { active: boolean; weight: number }>();
    resumeMatchFilters.forEach(f => {
      const key = f.rubricKey || rubricKeyFor(f.category, f.value.split("—")[0]);
      existingFilterPrefs.set(key, {
        active: f.active,
        weight: typeof f.weight === 'number' && isFinite(f.weight) ? f.weight : 1,
      });
    });

    const pushRubricFilter = (
      category: string,
      baseValue: string,
      displayValue: string,
      defaultActive: boolean
    ) => {
      const key = rubricKeyFor(category, baseValue);
      const existing = existingFilterPrefs.get(key);
      const active = existing ? existing.active : defaultActive;
      const weight = existing ? existing.weight : 1;
      filters.push({
        id: idCounter++,
        category,
        value: displayValue,
        active,
        ai: true,
        fromRubric: true,
        rubricKey: key,
        weight,
      });
    };

    // 1. Titles
    // Preserve Required vs Preferred flag set on Step 3 — previously every
    // title was hard-coded as "Required Title" + active=true, which made
    // Preferred titles appear as hard filters on Step 4. Now the category
    // pill and the default On/Off state both track the rubric's
    // `title.required` value, mirroring how skills are handled below.
    if (rubricData.titles) {
      rubricData.titles.forEach((title: any) => {
        const isRequired = title.required === "Required";
        const category = isRequired ? "Required Title" : "Preferred Title";
        pushRubricFilter(
          category,
          title.value || "",
          `${title.value} — ${title.minYears}+ yrs, ${title.matchType} match`,
          isRequired
        );
      });
    }

    // 2. Skills
    if (rubricData.skills) {
      rubricData.skills.forEach((skill: any) => {
        const category = skill.required === "Required" ? "Required Skill" : "Preferred Skill";
        pushRubricFilter(
          category,
          skill.value || "",
          `${skill.value} — ${skill.minYears}+ yrs, ${skill.matchType} match`,
          skill.required === "Required"
        );
      });
    }

    // 3. Education
    if (rubricData.education) {
      rubricData.education.forEach((edu: any) => {
        const display = `${edu.degree}${edu.field ? ` in ${edu.field}` : ""}`;
        pushRubricFilter("Education", display, display, edu.required === "Required");
      });
    }

    // 4. Domain Experience
    if (rubricData.domain) {
      rubricData.domain.forEach((dom: any) => {
        pushRubricFilter("Domain", dom.value || "", dom.value || "", dom.required === "Required");
      });
    }

    // 5. Customer Requirements
    if (rubricData.customer_requirements) {
      rubricData.customer_requirements.forEach((req: any) => {
        if (!req.value) return;
        const display = `${req.type}: ${req.value}`;
        pushRubricFilter("Customer Req.", display, display, true);
      });
    }

    // 6. Other Requirements
    if (rubricData.other_requirements) {
      rubricData.other_requirements.forEach((req: any) => {
        if (!req.value) return;
        pushRubricFilter("Requirement", req.value, req.value, req.required === "Required");
      });
    }

    const nextFilters = [
      ...filters,
      ...manualFilters.map(filter => ({
        ...filter,
        id: idCounter++
      }))
    ];

    setResumeMatchFilters(nextFilters);
    setFilterIdCounter(idCounter);
  };


  // 4.4: Tracks whether the recruiter has manually added/deleted/edited a
  // question. If so, the Step-4 sync effect stops overwriting the list — only
  // an explicit "Regenerate" button can rewrite it.
  const userHasEditedQuestionsRef = useRef(false);

  const initializeScreenQuestionsFromRubric = async (opts: { force?: boolean } = {}) => {
    if (!jobData) return;
    // Respect recruiter edits. Sync-effect re-fires (from level / rubric
    // changes) MUST NOT clobber handcrafted questions — an explicit
    // `force: true` (from a user-initiated Regenerate) is the only escape.
    if (userHasEditedQuestionsRef.current && !opts.force) return;

    const addressParts = [jobData.address1, jobData.city, jobData.state].filter(Boolean);
    const addressStr = addressParts.join(", ");
    const location = `${jobData.city || ""}, ${jobData.state || ""}`.trim().replace(/^, |, $/g, "");
    const arrangement = (jobData.location_type || "").toLowerCase();
    const isRemote = arrangement === "remote";
    const arrangementLabel = arrangement === "hybrid" ? "a hybrid" : "an onsite";

    let idCounter = 1;
    const questions: ScreenQuestion[] = [];
    const customQuestions = screenQuestions.filter(
      question => question.category !== "default" && question.category !== "role-specific"
    );

    // 1. Bot Introduction
    const introTitle = (enhancedTitle || jobTitle || "role").trim();
    const intro = `Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a ${introTitle} in ${location || "your area"}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`;
    setBotIntroduction(prev => (prev && prev.trim().length > 0 ? prev : intro));

    // 2. Default Questions — note the onsite/hybrid question is now
    // arrangement-aware, address-aware, and marked as a hard filter so the
    // downstream screening flow can disqualify automatically.
    const defaultQs: Array<{ text: string; criteria: string; is_hard_filter?: boolean }> = [
      { text: "Are you open to exploring new job opportunities?", criteria: "Must be open to new job opportunities" },
      { text: "What is your current or most recent role and key responsibilities?", criteria: "" },
      { text: "What is your current location?", criteria: "" },
    ];
    if (!isRemote) {
      defaultQs.push({
        text: `This role follows ${arrangementLabel} work arrangement based in ${addressStr || location || "the job location"}. Are you open to working in this setup?`,
        criteria: `Must be open to ${arrangementLabel} work arrangement`,
        is_hard_filter: true,
      });
    }
    defaultQs.push(
      { text: "What is your earliest availability to start a new role?", criteria: `Must be available by ${jobData.start_date || 'ASAP'}` },
      { text: "What is your current compensation and expected compensation?", criteria: "" },
      { text: "Are you authorized to work in the United States?", criteria: "" },
      { text: "Will you now or in the future require visa sponsorship to continue working in the United States?", criteria: "" },
    );

    defaultQs.forEach((q, index) => {
      questions.push({
        id: idCounter++,
        question_text: q.text,
        pass_criteria: q.criteria,
        is_default: true,
        category: "default",
        order_index: index,
        is_hard_filter: !!q.is_hard_filter,
      });
    });

    // 3. Role-Specific Questions — prefer the backend LLM-backed generator
    // which produces depth-probing, seniority-aware questions. Fall back to
    // the legacy per-skill template only if the endpoint fails, so we
    // never leave the recruiter empty-handed.
    let roleSpecific: ScreenQuestion[] = [];
    try {
      const apiUrl = API_BASE;
      const jobRef = numericJobId || jobdivaId || "new";
      const res = await fetch(`${apiUrl}/api/v1/ai-generation/jobs/${jobRef}/screening-questions/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: (enhancedTitle || jobTitle || "").trim(),
          rubric: rubricData || {},
          screeningLevel: screeningLevel,
          customerName: jobData?.customer_name || "",
          workArrangement: jobData?.location_type || "",
          address: addressStr,
          totalYears: rubricData?.total_years || null,
        }),
      });
      if (res.ok) {
        const payload = await res.json();
        const raw = Array.isArray(payload?.questions) ? payload.questions : [];
        // Front-matter (intro, arrangement, total-years) is already owned by
        // the frontend above — keep only role-specific questions to avoid
        // duplicates.
        raw
          .filter((q: any) => {
            const cat = String(q?.category || "").toLowerCase();
            return cat !== "default" && cat !== "work-arrangement" && cat !== "intro";
          })
          .forEach((q: any) => {
            roleSpecific.push({
              id: idCounter++,
              question_text: q.question_text || "",
              pass_criteria: q.pass_criteria || "",
              is_default: false,
              category: "role-specific",
              order_index: questions.length + roleSpecific.length,
              is_hard_filter: false,
            });
          });
      }
    } catch (e) {
      console.warn("screening-questions/generate failed, using template fallback", e);
    }

    // Fallback path: legacy per-skill template so Step 4 is never empty.
    if (roleSpecific.length === 0 && rubricData?.skills) {
      rubricData.skills.forEach((skill: any) => {
        if (questions.length + roleSpecific.length >= 12) return;
        roleSpecific.push({
          id: idCounter++,
          question_text: `Can you describe your experience with ${skill.value}? We're looking for ${skill.minYears || 3}+ years of experience.`,
          pass_criteria: `Must have ${skill.minYears || 3}+ yrs of ${skill.value} experience`,
          is_default: false,
          category: "role-specific",
          order_index: questions.length + roleSpecific.length,
          is_hard_filter: false,
        });
      });
    }

    roleSpecific.forEach(q => questions.push(q));

    const remainingSlots = Math.max(0, 14 - questions.length);
    const mergedQuestions = [
      ...questions,
      ...customQuestions.slice(0, remainingSlots).map((question, index) => ({
        ...question,
        id: idCounter + index,
        order_index: questions.length + index,
      })),
    ];

    setScreenQuestions(mergedQuestions);
    setQuestionIdCounter(mergedQuestions.length + 1);
  };

  const initializeSourceFromRubric = () => {
    if (!rubricData) return;

    const getRubricDrivenMatchType = (item: any, existingMatchType?: 'must' | 'can' | 'exclude') => {
      if (existingMatchType === 'exclude') return 'exclude';
      return isRubricItemRequired(item) ? "must" : "can";
    };

    // Use the stable rubricKey planted in Step 4 rather than re-parsing the
    // formatted filter value. If no rubric filter is active yet (initial
    // page load), fall back to including every rubric item.
    const activeRubricFilterKeys = new Set(
      resumeMatchFilters
        .filter(filter => filter.fromRubric && filter.active && filter.rubricKey)
        .map(filter => filter.rubricKey as string)
    );

    const shouldIncludeRubricItem = (category: string, value: string) => {
      if (activeRubricFilterKeys.size === 0) return true;
      return activeRubricFilterKeys.has(rubricKeyFor(category, value));
    };

    // 1. Titles
    if (rubricData.titles) {
      setSourceTitles(prev => {
        const existingByValue = new Map(prev.map(title => [title.value, title]));
        const manualTitles = prev.filter(title => !title.fromRubric);
        const rubricTitles = rubricData.titles
          .filter((title: any) => shouldIncludeRubricItem("Required Title", title.value || ""))
          .map((title: any, index: number) => {
          const existing = existingByValue.get(title.value || "");

          return {
            id: existing?.id ?? index + 1,
            value: title.value || "",
            matchType: getRubricDrivenMatchType(title, existing?.matchType),
            years: title.minYears || 0,
            recent: existing?.recent ?? !!title.recent,
            similarCount: `${(title.similar_titles || []).length}/${(title.similar_titles || []).length} similar`,
            similarTitles: title.similar_titles || [],
            selectedSimilarTitles: existing?.selectedSimilarTitles?.filter((item: string) =>
              (title.similar_titles || []).includes(item)
            ) ?? (title.similar_titles || []),
            similarExpanded: existing?.similarExpanded ?? false,
            fromRubric: true
          };
        });

        return [...rubricTitles, ...manualTitles];
      });
    }

    // 2. Skills
    if (rubricData.skills) {
      setSourceSkills(prev => {
        const existingByValue = new Map(prev.map(skill => [skill.value, skill]));
        const manualSkills = prev.filter(skill => !skill.fromRubric);
        const rubricSkills = rubricData.skills
          .filter((skill: any) => shouldIncludeRubricItem(
            isRubricItemRequired(skill) ? "Required Skill" : "Preferred Skill",
            skill.value || ""
          ))
          .map((skill: any, index: number) => {
          const existing = existingByValue.get(skill.value || "");

          return {
            id: existing?.id ?? index + 1001,
            value: skill.value || "",
            matchType: getRubricDrivenMatchType(skill, existing?.matchType),
            years: skill.minYears || 0,
            recent: existing?.recent ?? !!skill.recent,
            similarCount: `${(skill.similar_skills || []).length}/${(skill.similar_skills || []).length} similar`,
            similarSkills: skill.similar_skills || [],
            selectedSimilarSkills: existing?.selectedSimilarSkills?.filter((item: string) =>
              (skill.similar_skills || []).includes(item)
            ) ?? (skill.similar_skills || []),
            similarExpanded: existing?.similarExpanded ?? false,
            fromRubric: true
          };
        });

        return [...rubricSkills, ...manualSkills];
      });
    }

    // 3. Locations
    if (!hasSeededSourceLocation) {
      setHasSeededSourceLocation(true);
      if (jobData && sourceLocations.length === 0) {
        const loc = `${jobData.city || ""}, ${jobData.state || ""}`.trim().replace(/^, |, $/g, "");
        if (loc) {
          setSourceLocations([{
            id: 1,
            value: loc,
            radius: "within 25 mi"
          }]);
        }
      }
    }

    // 4. Keywords
    // Don't auto-populate sourceKeywords anymore
  };

  const syncStepFourData = useEffectEvent(() => {
    if (!rubricData) return;

    initializeFiltersFromRubric();
    initializeScreenQuestionsFromRubric();
  });

  // 5.3: once sourcing criteria have been derived from the rubric for a job,
  // don't re-derive them. Re-runs on every dep-list change caused visible
  // drift ("first picked 2 skills, later 3, later 5+1 title"). An explicit
  // Reset button (or user-forced regenerate) is the only way to recompute.
  const sourcingCriteriaInitializedRef = useRef(false);

  const syncStepFiveData = useEffectEvent(() => {
    if (!rubricData) return;
    if (sourcingCriteriaInitializedRef.current) return;

    initializeSourceFromRubric();
    sourcingCriteriaInitializedRef.current = true;
  });

  useEffect(() => {
    if (!rubricData?.titles?.length) return;

    const normalizedTitles = rubricData.titles.map((title: any) => getNormalizedTitleItem(title));
    const hasChanges = normalizedTitles.some((title: any, index: number) => {
      const currentTitle = rubricData.titles[index];
      return (
        title.required !== currentTitle.required ||
        title.matchType !== currentTitle.matchType
      );
    });

    if (!hasChanges) return;

    setRubricData((prev: any) => {
      if (!prev?.titles) return prev;
      return {
        ...prev,
        titles: prev.titles.map((title: any) => getNormalizedTitleItem(title)),
      };
    });
  }, [rubricData?.titles, jobData?.title, jobTitle]);

  useEffect(() => {
    if (!rubricData?.skills?.length) return;

    const normalizedSkills = rubricData.skills.map((skill: any) => getNormalizedSkillItem(skill));
    const hasChanges = normalizedSkills.some((skill: any, index: number) => {
      const currentSkill = rubricData.skills[index];
      return skill.matchType !== currentSkill.matchType;
    });

    if (!hasChanges) return;

    setRubricData((prev: any) => {
      if (!prev?.skills) return prev;
      return {
        ...prev,
        skills: prev.skills.map((skill: any) => getNormalizedSkillItem(skill)),
      };
    });
  }, [rubricData?.skills]);

  // Inject the Step 1 work-authorization value (e.g. "W2 only", "US Citizen /
  // GC") into Step 3's "Other Requirements" list so recruiters don't have to
  // re-enter it. We only inject once per rubric+workAuth pair — if the user
  // deletes the item we don't re-add it on the same rubric. Tracked via a
  // ref so state churn in other rubric fields doesn't re-trigger injection.
  const injectedWorkAuthRef = useRef<string | null>(null);
  useEffect(() => {
    if (!rubricData) return;
    const authValue = (workAuthorization || jobData?.work_authorization || "").trim();
    if (!authValue) return;
    // Don't re-inject the same value we already inserted on this rubric.
    if (injectedWorkAuthRef.current === authValue) return;

    setRubricData((prev: any) => {
      if (!prev) return prev;
      const existing: any[] = Array.isArray(prev.other_requirements) ? prev.other_requirements : [];
      const already = existing.some(
        (item: any) => typeof item?.value === "string" &&
          item.value.trim().toLowerCase() === authValue.toLowerCase()
      );
      if (already) {
        injectedWorkAuthRef.current = authValue;
        return prev;
      }
      injectedWorkAuthRef.current = authValue;
      return {
        ...prev,
        other_requirements: [
          { value: authValue, required: "Required", source: "Step1" },
          ...existing,
        ],
      };
    });
  }, [rubricData, workAuthorization, jobData?.work_authorization]);

  useEffect(() => {
    if (currentStep !== 4) return;

    syncStepFourData();
    // `screeningLevel` is a dep so flipping Light/Medium/Intensive on Step 1
    // re-derives the role-specific question set to match the new depth. User
    // edits are protected by `userHasEditedQuestionsRef` inside the initializer.
  }, [currentStep, rubricData, jobData, screenQuestions.length, screeningLevel]);

  useEffect(() => {
    if (currentStep !== 5) return;

    syncStepFiveData();
  }, [currentStep, rubricData, jobData, resumeMatchFilters]);

  const addSourceTitle = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue) return;
    setSourceTitles(prev => [
      ...prev,
      {
        id: Date.now(),
        value: cleanValue,
        matchType: "must",
        years: 0,
        recent: false,
        similarCount: "0/0 similar",
        similarTitles: [],
        selectedSimilarTitles: [],
        similarExpanded: false,
        fromRubric: false
      }
    ]);
    setSourceTitleInput("");
    setGeneratedBoolean("");
  };

  const addSourceSkill = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue) return;
    setSourceSkills(prev => [
      ...prev,
      {
        id: Date.now(),
        value: cleanValue,
        matchType: "can",
        years: 0,
        recent: false,
        similarCount: "0/0 similar",
        similarSkills: [],
        selectedSimilarSkills: [],
        similarExpanded: false,
        fromRubric: false
      }
    ]);
    setSourceSkillInput("");
    setGeneratedBoolean("");
  };

  const addSourceLocation = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue) return;
    setSourceLocations(prev => [
      ...prev,
      {
        id: Date.now(),
        value: cleanValue,
        radius: sourceLocationRadius.toLowerCase()
      }
    ]);
    setSourceLocationInput("");
    setGeneratedBoolean("");
  };

  const addSourceCompany = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue || sourceCompanies.includes(cleanValue)) return;
    setSourceCompanies(prev => [...prev, cleanValue]);
    setSourceCompanyInput("");
    setGeneratedBoolean("");
  };

  const addSourceKeyword = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue || sourceKeywords.includes(cleanValue)) return;
    setSourceKeywords(prev => [...prev, cleanValue]);
    setSourceKeywordInput("");
    setGeneratedBoolean("");
  };

  const buildGeneratedBooleanString = () => {
    const quote = (value: string) => `"${value.replace(/"/g, '\\"')}"`;
    const normalizeTerm = (value: string) =>
      value
        .toLowerCase()
        .replace(/^must be local to\s*/i, "")
        .replace(/\s*metro$/i, "")
        .replace(/^must not be employed by:\s*/i, "")
        .replace(/["()]/g, "")
        .replace(/\s+within\s+\d+\s+mi$/i, "")
        .replace(/\s+recent$/i, "")
        .replace(/\s+over\s+\d+\s+years?$/i, "")
        .trim();
    const normalizeResumeFilterValue = (value: string) =>
      value
        .replace(/^Must not be employed by:\s*/i, "")
        .replace(/^Must be local to\s*/i, "")
        .replace(/^(must have|must include|must be|can have|preferred|nice to have)\s*:?\s*/i, "")
        .replace(/\s*metro$/i, "")
        .trim();
    const sourceTermKeys = new Set<string>();
    const addSourceKey = (value: string) => {
      const key = normalizeTerm(value);
      if (key) sourceTermKeys.add(key);
    };
    const criterionGroup = (value: string, similar: string[] = [], years = 0, recent = false) => {
      addSourceKey(value);
      similar.forEach(addSourceKey);
      const terms = [value, ...similar].map(term => term.trim()).filter(Boolean).map(quote);
      const base = terms.length > 1 ? `(${terms.join(" OR ")})` : terms[0];
      if (!base) return "";
      const experienceClause = years > 0 ? ` AND "${years}+ years"` : "";
      const recentClause = recent ? " AND recent" : "";
      return `${base}${recentClause}${experienceClause}`;
    };

    const must: string[] = [];
    const can: string[] = [];
    const exclude: string[] = [];
    const seenMust = new Set<string>();
    const seenCan = new Set<string>();
    const seenExclude = new Set<string>();
    const addUnique = (bucket: string[], seen: Set<string>, clause: string, keyValue = clause) => {
      const key = normalizeTerm(keyValue);
      if (!clause || !key || seen.has(key)) return;
      seen.add(key);
      bucket.push(clause);
    };

    sourceTitles.forEach(title => {
      const group = criterionGroup(title.value, title.selectedSimilarTitles || [], title.years, title.recent);
      if (!group) return;
      if (title.matchType === "exclude") addUnique(exclude, seenExclude, group, title.value);
      else if (title.matchType === "can") addUnique(can, seenCan, group, title.value);
      else addUnique(must, seenMust, group, title.value);
    });

    sourceSkills.forEach(skill => {
      const group = criterionGroup(skill.value, skill.selectedSimilarSkills || [], skill.years, skill.recent);
      if (!group) return;
      if (skill.matchType === "exclude") addUnique(exclude, seenExclude, group, skill.value);
      else if (skill.matchType === "can") addUnique(can, seenCan, group, skill.value);
      else addUnique(must, seenMust, group, skill.value);
    });

    sourceKeywords.filter(Boolean).forEach(keyword => {
      addSourceKey(keyword);
      addUnique(must, seenMust, quote(keyword), keyword);
    });
    sourceCompanies.filter(Boolean).forEach(company => {
      addSourceKey(company);
      addUnique(must, seenMust, quote(company), company);
    });
    sourceLocations
      .filter(location => location.value)
      .forEach(location => {
        addSourceKey(location.value);
        addUnique(must, seenMust, `${quote(location.value)} ${location.radius}`, location.value);
      });

    const parts = [...must];
    if (can.length) parts.push(`(${can.join(" OR ")})`);
    let booleanString = parts.length ? parts.join(" AND ") : (isValidBoolean(jobTitle) ? jobTitle : quote(jobTitle || "Role"));
    if (exclude.length) booleanString += ` NOT (${exclude.join(" OR ")})`;
    return booleanString;
  };

  const isValidBoolean = (str: string) => {
    if (!str) return false;
    return str.includes(" AND ") || str.includes(" OR ") || str.includes(" NOT ") || (str.includes('"') && str.length > 5);
  };

  const resolvedGeneratedBoolean = generatedBoolean || buildGeneratedBooleanString();

  useEffect(() => {
    if (booleanUserEdited) return;
    setIsRefreshingBoolean(true);
    const timeoutId = window.setTimeout(() => {
      setGeneratedBoolean(buildGeneratedBooleanString());
      setIsRefreshingBoolean(false);
    }, 150);

    return () => window.clearTimeout(timeoutId);
  }, [sourceTitles, sourceSkills, sourceLocations, sourceCompanies, sourceKeywords, resumeMatchFilters, jobTitle, booleanUserEdited]);

  // Item F: mirror the boolean-string relaxation into the structured search
  // payload. Tier 1/2 widen radius — also bump `within_miles` so LinkedIn /
  // Dice / Exa (which don't read the boolean's `within N mi`) benefit too.
  // Tier 3 drops NOT(...) from the boolean — deactivate exclude-category
  // resume_match_filters so scoring doesn't penalize the same candidates
  // whose "excluded" terms we just allowed through at sourcing time.
  const relaxStructuralOverrides = (
    tier: number,
    baseWithinMiles: number,
    currentFilters: typeof resumeMatchFilters
  ): { withinMilesOverride?: number; resumeMatchFiltersOverride?: typeof resumeMatchFilters } => {
    if (tier === 1) {
      return { withinMilesOverride: Math.max(50, baseWithinMiles * 2) };
    }
    if (tier === 2) {
      return { withinMilesOverride: Math.max(100, baseWithinMiles * 2) };
    }
    // tier >= 3: also deactivate Exclude-category filters for scoring.
    return {
      withinMilesOverride: Math.max(100, baseWithinMiles * 2),
      resumeMatchFiltersOverride: currentFilters.map(f =>
        (f.category || "").toLowerCase().includes("exclude")
          ? { ...f, active: false }
          : f
      ),
    };
  };

  const relaxBooleanString = (input: string, tier: number): { query: string; label: string } => {
    let query = input;
    let label = "";
    if (tier === 1) {
      query = query.replace(/within\s+(\d+)\s+mi/gi, (_m, n) => `within ${Math.max(50, Number(n) * 2)} mi`);
      query = query.replace(/\s+AND\s+"\d+\+\s*years?"/gi, "");
      label = "Widened radius · dropped year thresholds";
    } else if (tier === 2) {
      query = query.replace(/within\s+(\d+)\s+mi/gi, (_m, n) => `within ${Math.max(100, Number(n) * 2)} mi`);
      query = query.replace(/\(([^()]+?)\)/g, (_m, inner) => {
        const parts = String(inner).split(/\s+AND\s+/i).map((p: string) => p.trim()).filter(Boolean);
        return parts.length > 1 ? `(${parts.join(" OR ")})` : `(${inner})`;
      });
      query = query.replace(/\s+AND\s+recent/gi, "");
      label = "Radius widened further · required clauses OR-joined";
    } else {
      query = query.replace(/\s+NOT\s+\([^)]*\)/gi, "");
      const andParts = query.split(/\s+AND\s+/i).map(p => p.trim()).filter(Boolean);
      const locationPart = andParts.find(p => /within\s+\d+\s+mi/i.test(p));
      const rolePart = andParts.find(p => !/within\s+\d+\s+mi/i.test(p) && !/"\d+\+\s*years?"/i.test(p));
      const keep = [rolePart, locationPart].filter(Boolean) as string[];
      query = keep.length ? keep.join(" AND ") : andParts[0] || query;
      label = "Kept only role + location";
    }
    return { query: query.replace(/\s+/g, " ").trim(), label };
  };

  const countQualified = (list: any[]) =>
    list.filter(c => (c.match_score || 0) >= QUALIFIED_SCORE_THRESHOLD).length;

  const buildSearchPayload = (booleanString: string, overrides?: { withinMilesOverride?: number; resumeMatchFiltersOverride?: typeof resumeMatchFilters }) => {
    const titleCriteria = sourceTitles.map(t => ({
      value: t.value || "Title",
      match_type: t.matchType || "must",
      years: t.years || 0,
      recent: t.recent || false,
      similar_terms: t.selectedSimilarTitles || []
    }));
    const skillCriteria = sourceSkills.map(s => ({
      value: s.value || "Skill",
      match_type: s.matchType || "must",
      years: s.years || 0,
      recent: s.recent || false,
      similar_terms: s.selectedSimilarSkills || []
    }));
    // Degrade gracefully: if nothing was configured, inject the job title as
    // a preferred title so the search isn't totally empty. Backend sources
    // that only accept a flat skills list (LinkedIn/Dice/Exa) derive their
    // list from title_criteria + skill_criteria server-side.
    if (titleCriteria.length === 0 && skillCriteria.length === 0 && jobTitle) {
      titleCriteria.push({
        value: jobTitle,
        match_type: "can",
        years: 0,
        recent: false,
        similar_terms: []
      });
    }
    const primaryLocation = sourceLocations[0];
    const parsedRadius = primaryLocation?.radius?.match(/(\d+)/)?.[1]
      ? Number(primaryLocation.radius.match(/(\d+)/)?.[1])
      : 25;
    const withinMiles = overrides?.withinMilesOverride ?? parsedRadius;
    const activeResumeFilters = (overrides?.resumeMatchFiltersOverride ?? resumeMatchFilters)
      .filter(f => f.active)
      .map(f => ({
        category: f.category,
        value: f.value,
        active: f.active,
        weight: typeof f.weight === 'number' && isFinite(f.weight) ? f.weight : 1,
      }));
    const selectedSourcesArray = Object.keys(searchSources)
      .filter(k => (searchSources as any)[k])
      .map(k => {
        // `jobdiva_applicants` was removed as a toggle (5.1). Applicants
        // still land via the auto-sync path; they're just not gated by a
        // recruiter checkbox on Step 5 anymore.
        if (k === 'jobdiva') return 'JobDiva';
        if (k === 'linkedin') return 'LinkedIn';
        if (k === 'dice') return 'Dice';
        if (k === 'exa') return 'Exa';
        return k;
      });
    return {
      job_id: numericJobId || jobdivaId,
      title_criteria: titleCriteria,
      skill_criteria: skillCriteria,
      keywords: sourceKeywords,
      companies: sourceCompanies,
      resume_match_filters: activeResumeFilters,
      location: primaryLocation?.value || "",
      within_miles: withinMiles,
      sources: selectedSourcesArray,
      boolean_string: booleanString,
      // 5.6 / 5.10 plumbing — backend honors these in
      // jobdiva_service.search_candidates. `recent_days: 0` means Any.
      recent_days: recentDaysFilter > 0 ? recentDaysFilter : null,
      require_resume: !includeNoResume,
      page: 1,
      page_size: 100
    };
  };

  const runSearchStream = async (
    booleanString: string,
    mode: "replace" | "append",
    overrides?: { withinMilesOverride?: number; resumeMatchFiltersOverride?: typeof resumeMatchFilters }
  ): Promise<any[]> => {
    const apiUrl = API_BASE;
    const payload = buildSearchPayload(booleanString, overrides);
    const controller = new AbortController();
    searchAbortRef.current = controller;
    let response: Response;
    try {
      response = await fetch(`${apiUrl}/candidates/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
    } catch (e: any) {
      if (e?.name === "AbortError") {
        if (mode === "replace") setCandidates([]);
        return [];
      }
      throw e;
    }
    if (!response.ok || !response.body) {
      console.error("Search failed:", response.status);
      if (mode === "replace") setCandidates([]);
      return [];
    }
    if (mode === "replace") {
      setCandidates([]);
      setCurrentPage(1);
      seenCandidateIdsRef.current = new Set<string>();
    }
    const seenIds = seenCandidateIdsRef.current;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let runList: any[] = [];
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            if (event.type === "candidate") {
              const id = String(event.data.candidate_id || event.data.id || "");
              if (id && seenIds.has(id)) continue;
              if (id) seenIds.add(id);
              runList.push(event.data);
              setCandidates(prev => [...prev, event.data]);
            } else if (event.type === "stage") {
              setSearchStatus(event.data);
            } else if (event.type === "summary") {
              console.log("Search stream complete:", event.data);
            } else if (event.type === "error") {
              console.error("Stream error:", event.message);
            }
          } catch (e) {
            console.error("Failed to parse stream line:", line, e);
          }
        }
      }
    } catch (e: any) {
      if (e?.name === "AbortError" || controller.signal.aborted) {
        console.log("Search stream aborted by user");
      } else {
        throw e;
      }
    }
    return runList;
  };

  const handleStopSearch = () => {
    if (searchAbortRef.current) {
      searchAbortRef.current.abort();
    }
    setIsSearching(false);
    setSearchStatus("Search stopped");
  };

  const handleRunSearch = async () => {
    setIsSearching(true);
    setHasSearched(true);
    try {
      const initial = resolvedGeneratedBoolean;
      setGeneratedBoolean(initial);
      const attempts: { query: string; label: string }[] = [{ query: initial, label: "Hoonr-Curate generated" }];
      setBooleanAttempts(attempts);
      setSearchStatus("Searching candidates...");
      const firstRun = await runSearchStream(initial, "replace");
      let accumulated = [...firstRun];

      const baseWithinMiles = (() => {
        const m = sourceLocations[0]?.radius?.match(/(\d+)/)?.[1];
        return m ? Number(m) : 25;
      })();
      let currentAttempts = attempts;
      while (currentAttempts.length < MAX_BOOLEAN_ATTEMPTS) {
        if (searchAbortRef.current?.signal.aborted) break;
        const qualified = countQualified(accumulated);
        if (qualified >= QUALIFIED_TARGET_COUNT) break;
        const tier = currentAttempts.length; // 1, 2, 3 as attempts grow
        const relaxed = relaxBooleanString(currentAttempts[currentAttempts.length - 1].query, tier);
        if (relaxed.query === currentAttempts[currentAttempts.length - 1].query) break;
        const structuralOverrides = relaxStructuralOverrides(tier, baseWithinMiles, resumeMatchFilters);
        currentAttempts = [...currentAttempts, { query: relaxed.query, label: relaxed.label }];
        setBooleanAttempts(currentAttempts);
        setGeneratedBoolean(relaxed.query);
        setSearchStatus(`Only ${qualified}/${QUALIFIED_TARGET_COUNT} strong matches — relaxing boolean (attempt ${currentAttempts.length}/${MAX_BOOLEAN_ATTEMPTS})...`);
        const nextRun = await runSearchStream(relaxed.query, "append", structuralOverrides);
        accumulated = [...accumulated, ...nextRun];
      }
    } catch (error) {
      console.error("Failed to search candidates:", error);
    } finally {
      setIsSearching(false);
    }
  };

  const handleExtendBoolean = async () => {
    if (isSearching) return;
    if (booleanAttempts.length >= MAX_BOOLEAN_ATTEMPTS) return;
    const base = resolvedGeneratedBoolean;
    const tier = Math.max(1, booleanAttempts.length);
    const relaxed = relaxBooleanString(base, tier);
    const nextAttempts = booleanAttempts.length
      ? [...booleanAttempts, { query: relaxed.query, label: relaxed.label }]
      : [{ query: base, label: "Hoonr-Curate generated" }, { query: relaxed.query, label: relaxed.label }];
    setBooleanAttempts(nextAttempts);
    setGeneratedBoolean(relaxed.query);
    setBooleanUserEdited(true);
    setIsSearching(true);
    setHasSearched(true);
    try {
      const baseWithinMiles = (() => {
        const m = sourceLocations[0]?.radius?.match(/(\d+)/)?.[1];
        return m ? Number(m) : 25;
      })();
      const structuralOverrides = relaxStructuralOverrides(tier, baseWithinMiles, resumeMatchFilters);
      setSearchStatus(`Extending search with more lenient boolean (attempt ${nextAttempts.length}/${MAX_BOOLEAN_ATTEMPTS})...`);
      await runSearchStream(relaxed.query, "append", structuralOverrides);
    } finally {
      setIsSearching(false);
    }
  };

  const addScreenQuestion = () => {
    const newQuestion: ScreenQuestion = {
      id: questionIdCounter,
      question_text: "",
      pass_criteria: "",
      is_default: false,
      category: "other",
      order_index: screenQuestions.length,
      is_hard_filter: false,
    };
    userHasEditedQuestionsRef.current = true;
    setScreenQuestions([...screenQuestions, newQuestion]);
    setQuestionIdCounter(questionIdCounter + 1);
  };

  const updateScreenQuestion = (id: number, field: keyof ScreenQuestion, value: any) => {
    userHasEditedQuestionsRef.current = true;
    setScreenQuestions(prev => prev.map(q => q.id === id ? { ...q, [field]: value } : q));
  };

  const deleteScreenQuestion = (id: number) => {
    userHasEditedQuestionsRef.current = true;
    setScreenQuestions(prev => prev.filter(q => q.id !== id));
  };

  const setFiltersStep = (
    <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
      <div className="flex flex-row items-start gap-4 px-7 py-6 border-b border-slate-100"
        style={{ background: "linear-gradient(135deg, #f5f3ff 0%, #ffffff 60%)" }}>
        <Filter className="w-[22px] h-[22px] text-primary mt-0.5 flex-shrink-0" />
        <div>
          <h2 className="text-[20px] font-medium text-slate-900 leading-tight tracking-tight">Set Filters</h2>
          <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">Each rubric item from Establish Rubric is evaluated here. Toggle, edit, or add filters for resume matching and the Hoonr-Curate phone screen.</p>
        </div>
      </div>

      <div className="p-7 space-y-7">
        {/* Resume Match Section */}
        <section>
          <div className="flex items-center gap-2 mb-4">
            <FileText className="w-4 h-4 text-slate-900 flex-shrink-0" />
            <h3 className="text-[14px] font-bold text-slate-800">Resume Match</h3>
            <span className="text-[12px] font-normal text-slate-500">Hard filters applied during resume screening</span>
            <span className="ml-auto bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
              <Sparkles className="w-3 h-3 inline mr-1" />
              Hoonr-Curate pre-filled
            </span>
          </div>

          {/* Filter Header */}
          <div className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-2">
            <div className="w-[44px] flex-shrink-0"></div>
            <div className="w-[110px] flex-shrink-0">Category</div>
            <div className="flex-1">Value</div>
            <div className="w-[220px] flex-shrink-0"></div>
          </div>

          {/* Active Filters */}
          {resumeMatchFilters.filter(f => f.active).length > 0 && (
            <>
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-500 py-2">
                <div className="w-2 h-2 bg-green-500 rounded-full"></div>
                <span>Active ({resumeMatchFilters.filter(f => f.active).length})</span>
              </div>
              {resumeMatchFilters.filter(f => f.active).map((filter) => (
                <div key={filter.id} className="flex items-center gap-3 py-3 border-b border-slate-100 last:border-b-0">
                  <button
                    onClick={() => toggleResumeFilter(filter.id, false)}
                    className="w-10 h-7 rounded bg-green-100 border border-green-300 text-green-600 text-[11px] font-bold flex items-center justify-center transition-all hover:bg-green-200"
                    title="Disable"
                  >
                    On
                  </button>
                  {filter.ai || filter.fromRubric ? (
                    <span className="w-[110px] flex-shrink-0 bg-slate-100 text-slate-600 text-[11px] font-semibold px-3 py-1 rounded-full text-center">
                      {filter.category}
                    </span>
                  ) : (
                    <input
                      type="text"
                      value={filter.category}
                      onChange={(e) => updateResumeFilterCategory(filter.id, e.target.value)}
                      placeholder="Category"
                      className="w-[110px] flex-shrink-0 bg-slate-50 border border-slate-200 text-slate-700 text-[11px] font-semibold px-3 py-1 rounded-full text-center outline-none focus:border-[#6366f1] focus:ring-1 focus:ring-[#6366f1]/30"
                    />
                  )}
                  <div className="flex-1 min-w-0">
                    <input
                      type="text"
                      value={filter.value}
                      onChange={(e) => updateResumeFilter(filter.id, e.target.value)}
                      placeholder={filter.ai || filter.fromRubric ? "" : "Enter value..."}
                      className="w-full text-[13px] bg-transparent border-none outline-none text-slate-900 font-medium"
                    />
                  </div>
                  <div className="w-[220px] flex-shrink-0 flex items-center justify-end gap-2">
                    {filter.ai && (
                      <span className="bg-[#ede9fe] text-[#6d28d9] text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        Hoonr-Curate
                      </span>
                    )}
                    {filter.fromRubric && (
                      <span className="bg-slate-100 text-slate-600 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        from rubric
                      </span>
                    )}
                    <button
                      onClick={() => deleteResumeFilter(filter.id)}
                      className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all ml-2"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          {/* Inactive Filters */}
          {resumeMatchFilters.filter(f => !f.active).length > 0 && (
            <>
              {resumeMatchFilters.filter(f => f.active).length > 0 && (
                <div className="h-px bg-slate-200 my-4"></div>
              )}
              <div className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-slate-400 py-2">
                <div className="w-2 h-2 bg-slate-400 rounded-full"></div>
                <span>Off ({resumeMatchFilters.filter(f => !f.active).length})</span>
              </div>
              {resumeMatchFilters.filter(f => !f.active).map((filter) => (
                <div key={filter.id} className="flex items-center gap-3 py-3 border-b border-slate-100 last:border-b-0 opacity-70">
                  <button
                    onClick={() => toggleResumeFilter(filter.id, true)}
                    className="w-10 h-7 rounded bg-slate-100 border border-slate-300 text-slate-400 text-[11px] font-bold flex items-center justify-center transition-all hover:border-primary hover:text-primary"
                    title="Enable"
                  >
                    Off
                  </button>
                  {filter.ai || filter.fromRubric ? (
                    <span className="w-[110px] flex-shrink-0 bg-slate-50 text-slate-400 text-[11px] font-semibold px-3 py-1 rounded-full text-center">
                      {filter.category}
                    </span>
                  ) : (
                    <input
                      type="text"
                      value={filter.category}
                      onChange={(e) => updateResumeFilterCategory(filter.id, e.target.value)}
                      placeholder="Category"
                      className="w-[110px] flex-shrink-0 bg-slate-50 border border-slate-200 text-slate-400 text-[11px] font-semibold px-3 py-1 rounded-full text-center outline-none focus:border-[#6366f1] focus:ring-1 focus:ring-[#6366f1]/30"
                    />
                  )}
                  <div className="flex-1 min-w-0">
                    <input
                      type="text"
                      value={filter.value}
                      onChange={(e) => updateResumeFilter(filter.id, e.target.value)}
                      className="w-full text-[13px] bg-transparent border-none outline-none text-slate-500 font-medium"
                    />
                  </div>
                  <div className="w-[220px] flex-shrink-0 flex items-center justify-end gap-2">
                    {filter.ai && (
                      <span className="bg-slate-100 text-slate-400 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        Hoonr-Curate
                      </span>
                    )}
                    {filter.fromRubric && (
                      <span className="bg-slate-50 text-slate-400 text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0">
                        from rubric
                      </span>
                    )}
                    <button
                      onClick={() => deleteResumeFilter(filter.id)}
                      className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all ml-2"
                      title="Remove"
                    >
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}
            </>
          )}

          {/* No filters state */}
          {resumeMatchFilters.length === 0 && (
            <p className="text-[13px] text-slate-400 py-4">No filters set.</p>
          )}

          {/* Add Filter Button */}
          <Button
            variant="outline"
            size="sm"
            onClick={addResumeFilter}
            className="mt-3 border-slate-200 text-slate-600 bg-white hover:bg-slate-50 font-medium text-[13px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
          >
            <Plus className="w-3.5 h-3.5 mr-1.5" />
            Add Resume Filter
          </Button>
        </section>

        <div className="h-px bg-slate-100 my-2"></div>

        {/* Screen Section */}
        <section className="pt-2">
          <div className="flex items-center gap-2 mb-4">
            <Users className="w-4 h-4 text-slate-900 flex-shrink-0" />
            <h3 className="text-[14px] font-bold text-slate-800">Screen</h3>
            <span className="text-[12px] font-normal text-slate-500">Questions asked during Hoonr-Curate phone screen</span>
            <span className="ml-auto text-slate-400 text-[11px] font-bold">
              {screenQuestions.length} / 12 questions
            </span>
          </div>

          {/* Bot Introduction */}
          <div className="bg-[#f5f3ff] rounded-xl border border-[#ddd6fe] p-5 mb-6 relative">
            <div className="flex items-center gap-2 mb-2">
              <div className="w-5 h-5 bg-[#6d28d9] rounded flex items-center justify-center">
                <Users className="w-3 h-3 text-white" />
              </div>
              <span className="text-[12px] font-bold text-slate-800">Bot Introduction</span>
              <span className="text-[11px] text-slate-400 font-normal">— what Nova says at the start of each call. Variables in {"{{brackets}}"} are filled at runtime.</span>
            </div>
            <textarea
              value={botIntroduction}
              onChange={(e) => setBotIntroduction(e.target.value)}
              className="w-full bg-transparent border-none outline-none text-[13px] text-slate-600 leading-relaxed resize-none h-24"
              placeholder="Enter bot introduction..."
            />
          </div>

          {/* Questions Table */}
          <div className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-2">
            <div className="w-8 flex-shrink-0">#</div>
            <div className="flex-1">Question</div>
            <div className="flex-1">Pass Criteria <span className="text-[10px] font-normal lowercase">(blank = informational only)</span></div>
            <div className="w-10 flex-shrink-0"></div>
          </div>

          {screenQuestions.map((q, index) => (
            <div key={q.id} className="flex items-start gap-3 py-3 border-b border-slate-100 last:border-b-0 group">
              <div className="w-8 h-8 rounded-full bg-[#6366f1] text-white flex items-center justify-center text-[12px] font-bold flex-shrink-0 mt-0.5">
                {index + 1}
              </div>

              <div className="flex-1 min-w-0">
                {q.is_hard_filter && (
                  <div className="inline-flex items-center gap-1 bg-rose-50 text-rose-700 border border-rose-200 text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded-full mb-1">
                    Hard filter
                  </div>
                )}
                <textarea
                  value={q.question_text}
                  onChange={(e) => updateScreenQuestion(q.id, 'question_text', e.target.value)}
                  className="w-full text-[13px] bg-transparent border-none outline-none text-slate-900 font-medium resize-none"
                  rows={2}
                />
              </div>

              <div className="flex-1 min-w-0 border-l border-slate-100 pl-3">
                {isAvailabilityQuestion(q) ? (() => {
                  // F2: availability question renders a date picker + ASAP
                  // toggle so recruiters don't have to free-type a date and
                  // can't end up with a stale baked-in value.
                  const parsed = parseAvailabilityCriteria(q.pass_criteria);
                  const isASAP = parsed.mode === "asap";
                  return (
                    <div className="flex items-center gap-2 flex-wrap">
                      <input
                        type="date"
                        value={parsed.iso ?? ""}
                        disabled={isASAP}
                        onChange={(e) => {
                          const iso = e.target.value;
                          updateScreenQuestion(
                            q.id,
                            "pass_criteria",
                            formatAvailabilityCriteria(iso ? { mode: "date", iso } : { mode: "asap" })
                          );
                        }}
                        className={`text-[13px] bg-white border border-slate-200 rounded-md px-2 py-1 font-medium focus:outline-none focus:ring-2 focus:ring-[#6366f1]/30 focus:border-[#6366f1] ${isASAP ? "text-slate-400" : "text-[#4f46e5]"}`}
                      />
                      <label className="inline-flex items-center gap-1.5 text-[12px] text-slate-600 cursor-pointer select-none">
                        <input
                          type="checkbox"
                          checked={isASAP}
                          onChange={(e) => {
                            if (e.target.checked) {
                              updateScreenQuestion(q.id, "pass_criteria", formatAvailabilityCriteria({ mode: "asap" }));
                            } else {
                              // Uncheck → default to today so the picker has a sensible value.
                              const today = new Date().toISOString().slice(0, 10);
                              updateScreenQuestion(
                                q.id,
                                "pass_criteria",
                                formatAvailabilityCriteria({ mode: "date", iso: today })
                              );
                            }
                          }}
                          className="w-3.5 h-3.5 rounded border-slate-300 text-[#6366f1] focus:ring-[#6366f1]/30"
                        />
                        ASAP
                      </label>
                    </div>
                  );
                })() : (
                  <input
                    type="text"
                    value={q.pass_criteria}
                    onChange={(e) => updateScreenQuestion(q.id, 'pass_criteria', e.target.value)}
                    className={`w-full text-[13px] bg-transparent border-none outline-none font-medium ${q.pass_criteria ? 'text-[#4f46e5]' : 'text-slate-300 italic'}`}
                    placeholder="No hard filter"
                  />
                )}
              </div>

              <div className="w-10 flex-shrink-0 flex flex-col items-end gap-2 pr-1">
                {q.category === 'role-specific' && (
                  <span className="bg-[#f0fdf4] text-[#166534] text-[9px] font-bold px-1.5 py-0.5 rounded border border-[#bbf7d0] whitespace-nowrap mb-1">
                    role-specific
                  </span>
                )}
                <button
                  onClick={() => deleteScreenQuestion(q.id)}
                  className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all opacity-0 group-hover:opacity-100"
                  title="Remove"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}

          {/* Add Question Button */}
          <Button
            variant="outline"
            size="sm"
            onClick={addScreenQuestion}
            className="mt-3 border-slate-200 text-slate-600 bg-white hover:bg-slate-50 font-medium text-[13px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
          >
            <Plus className="w-3.5 h-3.5 mr-1.5" />
            Add Question
          </Button>
        </section>
      </div>
    </div>
  );

  const sourceStep = (
    <div className="space-y-6">
      <div className="border border-slate-200 rounded-xl shadow-md overflow-hidden bg-white mb-6">
        {/* Step 5 Header - Aligned with Step 4 Style */}
        <div className="flex flex-row items-start gap-5 px-8 py-6 border-b border-slate-100"
          style={{ background: "linear-gradient(135deg, #f5f3ff 0%, #ffffff 60%)" }}>
          <div className="w-11 h-11 flex items-center justify-center mt-0.5 flex-shrink-0">
            <Search className="w-5 h-5 text-[#6366f1]" strokeWidth={3} />
          </div>
          <div className="flex-1 text-left">
            <h2 className="text-[20px] font-medium text-slate-900 leading-tight tracking-tight mb-1">Source</h2>
            <p className="text-slate-500 text-[14px] mt-1 leading-relaxed">
              Build your candidate search using structured filters. Hoonr-Curate generates the Boolean string, searches JobDiva applicants first, then uses JobDiva Talent Search if fewer than 3 applicants match.
            </p>
          </div>
        </div>

        <div className="p-8">
          {/* Inner Content Box - Exact Screenshot Structure */}
          <div className="border border-slate-200 rounded-2xl bg-white shadow-sm overflow-hidden p-7 space-y-8">
            <div className="space-y-8">
              {/* 5.5: Top row now shows only the Hoonr-Curate badge. Run/Stop
                  buttons live below the Boolean string so the recruiter can
                  inspect + edit the query before kicking off the search. */}
              <div className="flex items-center justify-between mb-2">
                <div className="bg-[#ede9fe] text-[#6366f1] text-[11px] font-bold px-3 py-1 rounded-lg border border-[#ddd6fe] flex items-center gap-2">
                  <Sparkles className="w-3.5 h-3.5" /> Hoonr-Curate Pre-filled from Rubric
                </div>
              </div>

              <section>
                <div className="flex items-center gap-3 mb-5">
                  <Globe className="w-4 h-4 text-slate-400" />
                  <span className="text-[11px] font-bold uppercase tracking-widest text-slate-400">Search Sources:</span>
                  <div className="flex items-center gap-5 ml-1">
                    {[
                      // 5.1: JobDiva Applicants toggle removed — applicants
                      // auto-enroll via jobdiva_applicant_auto_sync regardless
                      // of this switchboard. Exposing it here implied they
                      // were a gated source, which they aren't.
                      { id: 'jobdiva', label: 'JobDiva', icon: <ShieldCheck className="w-4 h-4 text-[#6366f1]" />, disabled: false },
                      { id: 'linkedin', label: 'LinkedIn', icon: <Linkedin className="w-4 h-4 text-[#0A66C2] fill-[#0A66C2]" />, disabled: false },
                      { id: 'dice', label: 'Dice', icon: <Box className="w-4 h-4 text-slate-700" />, disabled: false },
                      { id: 'exa', label: 'Exa', icon: <Search className="w-4 h-4 text-pink-500" />, disabled: false }
                    ].map(source => (
                      <label key={source.id} className={`flex items-center gap-2 ${source.disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer group'}`} title={source.disabled ? "Integration coming soon" : ""}>
                        <Checkbox
                          checked={source.disabled ? false : (searchSources as any)[source.id]}
                          onCheckedChange={(checked) => !source.disabled && setSearchSources(prev => ({ ...prev, [source.id]: !!checked }))}
                          className={`w-4.5 h-4.5 rounded border-slate-300 data-[state=checked]:bg-[#6366f1] data-[state=checked]:border-[#6366f1] ${source.disabled ? 'opacity-50' : ''}`}
                          disabled={source.disabled}
                        />
                        <div className={`flex items-center gap-1.5 ${source.disabled ? 'opacity-60' : 'opacity-80 group-hover:opacity-100 transition-opacity'}`}>
                          {source.icon}
                          <span className="text-[13px] font-bold text-slate-700">{source.label}</span>
                        </div>
                      </label>
                    ))}
                  </div>
                </div>
                {/* 5.6 Recent-availability dropdown + 5.10 include-no-resume
                    toggle. Both scope JobDiva Talent Search only — other
                    sources ignore them server-side. */}
                <div className="flex items-center gap-6 flex-wrap">
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-bold uppercase tracking-widest text-slate-400">Recent Availability:</span>
                    <select
                      value={recentDaysFilter}
                      onChange={(e) => setRecentDaysFilter(Number(e.target.value))}
                      className="h-8 px-2 text-[12px] font-medium text-slate-700 bg-white border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-[#6366f1]/30"
                    >
                      <option value={30}>Last 30 days</option>
                      <option value={60}>Last 60 days</option>
                      <option value={90}>Last 90 days</option>
                      <option value={180}>Last 180 days</option>
                      <option value={0}>Any</option>
                    </select>
                  </div>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <Checkbox
                      checked={includeNoResume}
                      onCheckedChange={(checked) => setIncludeNoResume(!!checked)}
                      className="w-4 h-4 rounded border-slate-300 data-[state=checked]:bg-[#6366f1] data-[state=checked]:border-[#6366f1]"
                    />
                    <span className="text-[12px] font-medium text-slate-600">Include candidates without resumes</span>
                  </label>
                </div>
              </section>

              <section>
                <div className="flex items-center gap-3 mb-4">
                  <Clipboard className="w-4 h-4 text-slate-400" />
                  <h3 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Job Titles</h3>
                  <span className="bg-[#ede9fe] text-[#6366f1] text-[11px] font-bold px-2.5 py-0.5 rounded-full border border-[#ddd6fe]">{sourceTitles.length} added</span>
                </div>

                <div className="space-y-3 mb-3">
                  {sourceTitles.map((title) => (
                    <div key={title.id} className="flex flex-col gap-1">
                      <div className="flex items-center gap-3 p-1 pl-2.5 rounded-xl border border-[#ddd6fe] bg-white shadow-sm group">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <div className={`flex items-center justify-between px-2.5 h-8 min-w-[125px] rounded-xl text-[12px] font-bold cursor-pointer transition-all ${title.matchType === 'must' ? 'bg-[#f5f3ff] text-[#6366f1] border border-[#ddd6fe]' :
                              title.matchType === 'exclude' ? 'bg-[#fef2f2] text-[#dc2626] border border-[#fee2e2]' :
                                'bg-[#f0fdf4] text-[#16a34a] border border-[#dcfce7]'
                              }`}>
                              {title.matchType === 'must' ? 'Must have' : title.matchType === 'exclude' ? 'Must not have' : 'Can have'}
                              <ChevronDown className="w-4 h-4 opacity-50 ml-1" />
                            </div>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="start" className="w-[150px] p-1.5 rounded-xl border-slate-200 shadow-lg">
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]" onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'must' } : t))}>
                              Must have
                            </DropdownMenuItem>
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]" onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'can' } : t))}>
                              Can have
                            </DropdownMenuItem>
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px] text-red-600" onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'exclude' } : t))}>
                              Must not have
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                        <span className="flex-1 text-[13px] font-bold text-slate-800 px-1">{title.value}</span>

                        <div className="flex items-center h-8 bg-white border border-slate-200 rounded-lg overflow-hidden ml-auto shadow-sm">
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, years: Math.max(0, t.years - 1) } : t))}>-</button>
                          <span className="px-2 h-full flex items-center justify-center text-[11px] font-bold text-slate-700 min-w-[58px] text-center border-x border-slate-100">{title.years === 0 ? 'Any exp' : `${title.years}+ yr${title.years > 1 ? 's' : ''}`}</span>
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, years: t.years + 1 } : t))}>+</button>
                        </div>

                        <button
                          className={`flex items-center gap-1.5 px-2.5 h-8 rounded-xl text-[11px] font-bold transition-all border shadow-sm ${title.recent ? 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]' : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                            }`}
                          onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, recent: !t.recent } : t))}
                        >
                          <History className={`w-3.5 h-3.5 ${title.recent ? 'text-[#6366f1]' : 'text-slate-400'}`} />
                          Recent
                        </button>

                        {/* Similar button */}
                        {(title.similarTitles || []).length > 0 && (
                          <button
                            className={`flex items-center gap-1.5 px-2.5 h-8 rounded-lg text-[11px] font-bold transition-all border ${title.similarExpanded ? 'bg-[#ede9fe] text-[#6366f1] border-[#ddd6fe]' : 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe] hover:bg-[#ede9fe]'
                              }`}
                            onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, similarExpanded: !t.similarExpanded } : t))}
                          >
                            {title.selectedSimilarTitles?.length || 0}/{title.similarTitles.length} similar
                            <ChevronDown className={`w-3.5 h-3.5 opacity-60 transition-transform ${title.similarExpanded ? 'rotate-180' : ''}`} />
                          </button>
                        )}

                        <button
                          className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                          onClick={() => setSourceTitles(prev => prev.filter(t => t.id !== title.id))}
                        >
                          <X className="w-4 h-4" />
                        </button>
                      </div>

                      {/* Inline similar titles panel */}
                      {title.similarExpanded && (title.similarTitles || []).length > 0 && (
                        <div className="mx-1 mb-1 rounded-xl border border-[#ddd6fe] bg-[#f5f3ff] px-4 py-3">
                          <div className="flex items-center justify-between mb-3">
                            <span className="text-[12px] font-bold text-[#6366f1]">
                              {title.selectedSimilarTitles?.length || 0}/{title.similarTitles.length} similar titles also included
                            </span>
                            <button
                              className="text-[11px] font-bold text-slate-500 hover:text-[#6366f1] transition-colors"
                              onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, selectedSimilarTitles: t.selectedSimilarTitles?.length === t.similarTitles.length ? [] : t.similarTitles } : t))}
                            >
                              {(title.selectedSimilarTitles?.length || 0) === title.similarTitles.length ? 'Deselect all' : 'Select all'}
                            </button>
                          </div>
                          <div className="grid grid-cols-2 gap-x-6 gap-y-2">
                            {title.similarTitles.map((st, i) => (
                              <label key={i} className="flex items-center gap-2 cursor-pointer group">
                                <div
                                  className={`w-4 h-4 rounded flex items-center justify-center flex-shrink-0 border transition-all ${(title.selectedSimilarTitles || []).includes(st)
                                      ? 'bg-[#6366f1] border-[#6366f1]'
                                      : 'bg-white border-slate-300 group-hover:border-[#6366f1]'
                                    }`}
                                  onClick={() => setSourceTitles(prev => prev.map(t => t.id === title.id ? {
                                    ...t,
                                    selectedSimilarTitles: (t.selectedSimilarTitles || []).includes(st)
                                      ? (t.selectedSimilarTitles || []).filter(x => x !== st)
                                      : [...(t.selectedSimilarTitles || []), st]
                                  } : t))}
                                >
                                  {(title.selectedSimilarTitles || []).includes(st) && (
                                    <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 12 12"><path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
                                  )}
                                </div>
                                <span className="text-[12px] font-medium text-slate-700">{st}</span>
                              </label>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                <div className="relative">
                  <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                  <Input
                    value={sourceTitleInput}
                    onChange={(e) => setSourceTitleInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addSourceTitle(sourceTitleInput);
                    }}
                    onBlur={() => addSourceTitle(sourceTitleInput)}
                    placeholder="Search job titles..."
                    className="h-11 pl-11 text-[13px] border-slate-200 focus:border-[#6366f1]/30 focus:ring-0 bg-[#f5f3ff] rounded-xl font-medium text-slate-600 placeholder:text-slate-400"
                  />
                </div>
              </section>

              <div className="border-t border-slate-100" />

              <section>
                <div className="flex items-center gap-3 mb-4">
                  <Zap className="w-4 h-4 text-slate-400" />
                  <h3 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Skills & Experience</h3>
                  <span className="bg-[#ede9fe] text-[#6366f1] text-[11px] font-bold px-2.5 py-0.5 rounded-full border border-[#ddd6fe]">{sourceSkills.length} added</span>
                </div>

                <div className="space-y-3 mb-3">
                  {sourceSkills.map((skill) => (
                    <div key={skill.id} className="flex flex-col gap-1">
                      <div className="flex items-center gap-3 p-1 pl-2.5 rounded-xl border border-slate-200 bg-white group hover:border-[#6366f1]/30 transition-all shadow-sm">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <div className={`flex items-center justify-between px-2.5 h-8 min-w-[125px] rounded-xl text-[12px] font-bold cursor-pointer transition-all ${skill.matchType === 'must' ? 'bg-[#f5f3ff] text-[#6366f1] border border-[#ddd6fe]' :
                              skill.matchType === 'exclude' ? 'bg-[#fef2f2] text-[#dc2626] border border-[#fee2e2]' :
                                'bg-[#f0fdf4] text-[#16a34a] border border-[#dcfce7]'
                              }`}>
                              {skill.matchType === 'must' ? 'Must have' : skill.matchType === 'exclude' ? 'Must not have' : 'Can have'}
                              <ChevronDown className="w-4 h-4 opacity-50 ml-1" />
                            </div>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="start" className="w-[150px] p-1.5 rounded-xl border-slate-200 shadow-lg">
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]" onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'must' } : s))}>
                              Must have
                            </DropdownMenuItem>
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]" onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'can' } : s))}>
                              Can have
                            </DropdownMenuItem>
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px] text-red-600" onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'exclude' } : s))}>
                              Must not have
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                        <span className="flex-1 text-[13px] font-bold text-slate-800 px-1">{skill.value}</span>

                        <div className="flex items-center h-8 bg-white border border-slate-200 rounded-lg overflow-hidden ml-auto shadow-sm">
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, years: Math.max(0, s.years - 1) } : s))}>-</button>
                          <span className="px-2 h-full flex items-center justify-center text-[11px] font-bold text-slate-700 min-w-[58px] text-center border-x border-slate-100">{skill.years === 0 ? 'Any exp' : `${skill.years}+ yr${skill.years > 1 ? 's' : ''}`}</span>
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, years: s.years + 1 } : s))}>+</button>
                        </div>

                        <button
                          className={`flex items-center gap-1.5 px-2.5 h-8 rounded-xl text-[11px] font-bold transition-all border shadow-sm ${skill.recent ? 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]' : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                            }`}
                          onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, recent: !s.recent } : s))}
                        >
                          <History className={`w-3.5 h-3.5 ${skill.recent ? 'text-[#6366f1]' : 'text-slate-400'}`} />
                          Recent
                        </button>

                        {/* Similar button */}
                        {(skill.similarSkills || []).length > 0 && (
                          <button
                            className={`flex items-center gap-1.5 px-2.5 h-8 rounded-lg text-[11px] font-bold transition-all border ${skill.similarExpanded ? 'bg-[#ede9fe] text-[#6366f1] border-[#ddd6fe]' : 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe] hover:bg-[#ede9fe]'
                              }`}
                            onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, similarExpanded: !s.similarExpanded } : s))}
                          >
                            {skill.selectedSimilarSkills?.length || 0}/{skill.similarSkills.length} similar
                            <ChevronDown className={`w-3.5 h-3.5 opacity-60 transition-transform ${skill.similarExpanded ? 'rotate-180' : ''}`} />
                          </button>
                        )}

                        <button
                          className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                          onClick={() => setSourceSkills(prev => prev.filter(s => s.id !== skill.id))}
                        >
                          <X className="w-4 h-4" />
                        </button>
                      </div>

                      {/* Inline similar skills panel */}
                      {skill.similarExpanded && (skill.similarSkills || []).length > 0 && (
                        <div className="mx-1 mb-1 rounded-xl border border-[#ddd6fe] bg-[#f5f3ff] px-4 py-3">
                          <div className="flex items-center justify-between mb-3">
                            <span className="text-[12px] font-bold text-[#6366f1]">
                              {skill.selectedSimilarSkills?.length || 0}/{skill.similarSkills.length} similar skills also included
                            </span>
                            <button
                              className="text-[11px] font-bold text-slate-500 hover:text-[#6366f1] transition-colors"
                              onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, selectedSimilarSkills: s.selectedSimilarSkills?.length === s.similarSkills.length ? [] : s.similarSkills } : s))}
                            >
                              {(skill.selectedSimilarSkills?.length || 0) === skill.similarSkills.length ? 'Deselect all' : 'Select all'}
                            </button>
                          </div>
                          <div className="grid grid-cols-2 gap-x-6 gap-y-2">
                            {skill.similarSkills.map((ss, i) => (
                              <label key={i} className="flex items-center gap-2 cursor-pointer group">
                                <div
                                  className={`w-4 h-4 rounded flex items-center justify-center flex-shrink-0 border transition-all ${(skill.selectedSimilarSkills || []).includes(ss)
                                      ? 'bg-[#6366f1] border-[#6366f1]'
                                      : 'bg-white border-slate-300 group-hover:border-[#6366f1]'
                                    }`}
                                  onClick={() => setSourceSkills(prev => prev.map(s => s.id === skill.id ? {
                                    ...s,
                                    selectedSimilarSkills: (s.selectedSimilarSkills || []).includes(ss)
                                      ? (s.selectedSimilarSkills || []).filter(x => x !== ss)
                                      : [...(s.selectedSimilarSkills || []), ss]
                                  } : s))}
                                >
                                  {(skill.selectedSimilarSkills || []).includes(ss) && (
                                    <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 12 12"><path d="M2 6l3 3 5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /></svg>
                                  )}
                                </div>
                                <span className="text-[12px] font-medium text-slate-700">{ss}</span>
                              </label>
                            ))}
                          </div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                <div className="relative">
                  <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                  <Input
                    value={sourceSkillInput}
                    onChange={(e) => setSourceSkillInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addSourceSkill(sourceSkillInput);
                    }}
                    onBlur={() => addSourceSkill(sourceSkillInput)}
                    placeholder="Search skills..."
                    className="h-11 pl-11 text-[13px] border-slate-200 focus:border-[#6366f1]/30 focus:ring-0 bg-[#f5f3ff] rounded-xl font-medium text-slate-600 placeholder:text-slate-400"
                  />
                </div>
              </section>

              <div className="border-t border-slate-100" />

              <section>
                <div className="flex items-center gap-3 mb-4">
                  <MapPin className="w-4 h-4 text-slate-400" />
                  <h3 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Locations</h3>
                </div>

                <div className="space-y-4">
                  <div className="flex flex-col gap-3">
                    {sourceLocations.map((loc) => (
                      <div key={loc.id} className="flex items-center justify-between p-2.5 pl-3.5 rounded-xl border border-[#ddd6fe] bg-[#f5f3ff]">
                        <div className="flex items-center gap-3">
                          <MapPin className="w-4.5 h-4.5 text-[#6366f1]" />
                          <span className="text-[13px] font-bold text-slate-800 tracking-tight">{loc.value}</span>
                        </div>
                        <div className="flex items-center gap-3">
                          <div className="px-4 h-8 bg-white border border-[#ddd6fe] rounded-lg text-[#6366f1] text-[11px] font-bold flex items-center justify-center min-w-[110px]">
                            {loc.radius}
                          </div>
                          <button
                            className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-8 h-8 flex items-center justify-center rounded-lg transition-all duration-200"
                            onClick={() => setSourceLocations(prev => prev.filter(l => l.id !== loc.id))}
                          >
                            <X className="w-4 h-4" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="flex items-center gap-4">
                    <div className="relative flex-1">
                      <MapPin className="absolute left-4 top-1/2 -translate-y-1/2 w-4.5 h-4.5 text-slate-300" />
                      <Input
                        value={sourceLocationInput}
                        onChange={(e) => setSourceLocationInput(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            addSourceLocation(sourceLocationInput);
                          }
                        }}
                        placeholder="City, state, or zip code..."
                        className="h-11 pl-11 text-[13px] border-slate-200 focus:border-[#6366f1]/30 focus:ring-0 bg-[#f5f3ff] rounded-xl font-medium"
                      />
                    </div>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <div className="flex items-center justify-between px-4 h-11 min-w-[120px] border border-slate-200 rounded-xl text-slate-800 text-[13px] font-bold cursor-pointer hover:bg-slate-50 transition-colors">
                          {sourceLocationRadius}
                          <ChevronDown className="w-4 h-4 text-slate-400" />
                        </div>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-[150px] p-1.5 rounded-xl border-slate-200 shadow-lg">
                        {["Within 10 mi", "Within 25 mi", "Within 50 mi", "Within 100 mi", "Exact location"].map(radius => (
                          <DropdownMenuItem
                            key={radius}
                            className={`rounded-lg py-2 cursor-pointer font-bold text-[13px] ${sourceLocationRadius === radius ? "bg-slate-50 flex items-center justify-between" : ""}`}
                            onClick={() => {
                              setSourceLocationRadius(radius);
                              setGeneratedBoolean("");
                            }}
                          >
                            {radius}
                            {sourceLocationRadius === radius && <Check className="w-3.5 h-3.5 text-[#6366f1]" />}
                          </DropdownMenuItem>
                        ))}
                      </DropdownMenuContent>
                    </DropdownMenu>
                    <Button
                      type="button"
                      onClick={() => addSourceLocation(sourceLocationInput)}
                      disabled={!sourceLocationInput.trim()}
                      className="h-11 px-4 bg-[#6366f1] hover:bg-[#4f46e5] text-white text-[13px] font-bold rounded-xl transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <Plus className="w-4 h-4 mr-1" />
                      Add
                    </Button>
                  </div>
                </div>
              </section>

              <div className="border-t border-slate-100" />

              <section>
                <div className="flex items-center gap-3 mb-4">
                  <Clipboard className="w-4 h-4 text-slate-400" />
                  <h3 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Companies</h3>
                </div>
                <div className="relative">
                  <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-300" />
                  <Input
                    value={sourceCompanyInput}
                    onChange={(e) => setSourceCompanyInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") addSourceCompany(sourceCompanyInput);
                    }}
                    onBlur={() => addSourceCompany(sourceCompanyInput)}
                    placeholder="Search companies..."
                    className="h-11 pl-11 text-[13px] border-slate-200 focus:border-[#6366f1]/30 focus:ring-0 bg-[#f5f3ff] rounded-xl font-medium"
                  />
                </div>
                {sourceCompanies.length > 0 && (
                  <div className="flex flex-wrap gap-2.5 mt-3">
                    {sourceCompanies.map((company) => (
                      <div key={company} className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-slate-200 bg-white text-[12.5px] font-bold text-slate-700 shadow-sm">
                        {company}
                        <button
                          className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-5 h-5 flex items-center justify-center rounded-md transition-all duration-200"
                          onClick={() => {
                            setSourceCompanies(prev => prev.filter(item => item !== company));
                            setGeneratedBoolean("");
                          }}
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </section>

              <div className="border-t border-slate-100" />

              <section>
                <div className="flex items-center gap-3 mb-4">
                  <Type className="w-4 h-4 text-slate-400" />
                  <h3 className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Keywords</h3>
                </div>

                <div className="space-y-4">
                  <div className="flex flex-wrap gap-2.5">
                    {sourceKeywords.map((tag) => (
                      <div key={tag} className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-slate-200 bg-white text-[12.5px] font-bold text-slate-700 shadow-sm">
                        {tag}
                        <button
                          className="text-slate-400 hover:text-rose-500 hover:bg-rose-50 w-5 h-5 flex items-center justify-center rounded-md transition-all duration-200"
                          onClick={() => setSourceKeywords(prev => prev.filter(t => t !== tag))}
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="relative">
                    <Search className="absolute left-4 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-300" />
                    <Input
                      value={sourceKeywordInput}
                      onChange={(e) => setSourceKeywordInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") addSourceKeyword(sourceKeywordInput);
                      }}
                      onBlur={() => addSourceKeyword(sourceKeywordInput)}
                      placeholder="Profile keywords or phrases..."
                      className="h-11 pl-11 text-[13px] border-slate-200 focus:border-[#6366f1]/30 focus:ring-0 bg-[#f5f3ff] rounded-xl placeholder:italic font-medium"
                    />
                  </div>

                  <div className="bg-[#f5f3ff] border border-[#ddd6fe] rounded-xl overflow-hidden mt-3">
                    <button
                      className="w-full flex items-center gap-4 px-6 py-3.5 h-12 hover:bg-[#ede9fe] transition-colors"
                      onClick={async () => {
                        const nextState = !booleanStringOpen;
                        setBooleanStringOpen(nextState);

                        // Auto-save when expanding the boolean string view to feed the agent
                        if (nextState) {
                          setIsRefreshingBoolean(true);
                          try {
                            await saveJobDraft({ currentStep, saveType: "auto", skipToast: true });
                            setGeneratedBoolean(buildGeneratedBooleanString());
                          } finally {
                            setIsRefreshingBoolean(false);
                          }
                        }
                      }}
                    >
                      <FileText className="w-4.5 h-4.5 text-[#6366f1]" />
                      <span className="text-[13px] font-bold text-slate-700 flex-1 text-left flex items-center gap-2">
                        <code className="text-[#6366f1] text-lg lg:text-base font-mono font-bold leading-none">&lt;/&gt;</code> View generated boolean string
                      </span>
                      <ChevronDown className={`w-4.5 h-4.5 text-slate-400 transition-transform duration-300 ${booleanStringOpen ? 'rotate-180' : ''}`} />
                    </button>
                    {booleanStringOpen && (
                      <div className="px-6 pb-6 pt-1 animate-in fade-in slide-in-from-top-1">
                        {!isRefreshingBoolean ? (
                          <div className="p-4 bg-white border border-slate-200 rounded-xl shadow-inner">
                            <div className="flex items-center justify-between mb-2 gap-2 flex-wrap">
                              <div className="flex items-center gap-2 flex-wrap">
                                <span className="text-[11px] font-bold uppercase tracking-widest text-[#5b21b6] bg-[#f5f3ff] px-2.5 py-0.5 rounded-full border border-[#ddd6fe]">
                                  {booleanUserEdited ? "Edited" : "Hoonr-Curate Generated"}
                                </span>
                                {booleanAttempts.length > 0 && (
                                  <span className="text-[11px] font-bold uppercase tracking-widest text-slate-500 bg-slate-50 px-2.5 py-0.5 rounded-full border border-slate-200">
                                    Attempt {booleanAttempts.length}/{MAX_BOOLEAN_ATTEMPTS}
                                  </span>
                                )}
                                {booleanAttempts.length > 1 && (
                                  <span className="text-[11px] font-medium text-slate-500">
                                    {booleanAttempts[booleanAttempts.length - 1].label}
                                  </span>
                                )}
                              </div>
                              <div className="flex items-center gap-2">
                                {booleanUserEdited && (
                                  <button
                                    onClick={() => {
                                      setBooleanUserEdited(false);
                                      setGeneratedBoolean(buildGeneratedBooleanString());
                                    }}
                                    className="text-[11px] font-bold text-slate-500 hover:text-[#6366f1] px-2.5 py-1 rounded-md border border-slate-200 bg-white hover:bg-slate-50 transition-colors"
                                  >
                                    Reset
                                  </button>
                                )}
                                <button
                                  onClick={handleExtendBoolean}
                                  disabled={isSearching || booleanAttempts.length >= MAX_BOOLEAN_ATTEMPTS}
                                  className="text-[11px] font-bold text-[#6366f1] hover:text-white hover:bg-[#6366f1] px-2.5 py-1 rounded-md border border-[#ddd6fe] bg-[#f5f3ff] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                                  title="Relax the boolean string and search again, appending new candidates"
                                >
                                  Make more lenient
                                </button>
                              </div>
                            </div>
                            <textarea
                              value={resolvedGeneratedBoolean}
                              onChange={(e) => {
                                setBooleanUserEdited(true);
                                setGeneratedBoolean(e.target.value);
                              }}
                              rows={Math.min(8, Math.max(2, resolvedGeneratedBoolean.split("\n").length))}
                              className="w-full resize-y text-[13px] font-mono font-medium text-slate-700 leading-relaxed tracking-tight bg-slate-50 border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#6366f1]/30 focus:border-[#6366f1]"
                              spellCheck={false}
                            />
                            {/* Relaxation history: show every attempted boolean
                                as a read-only card under the live textarea so
                                the recruiter can audit what was widened and
                                when. Only renders once auto/manual relaxation
                                has actually produced >1 attempt. */}
                            {booleanAttempts.length > 1 && (
                              <div className="mt-4 space-y-2">
                                <p className="text-[11px] font-bold uppercase tracking-widest text-slate-500">
                                  Relaxation history · {booleanAttempts.length} attempt{booleanAttempts.length === 1 ? "" : "s"}
                                </p>
                                {booleanAttempts.map((attempt, idx) => {
                                  const isOriginal = idx === 0;
                                  const isCurrent = idx === booleanAttempts.length - 1;
                                  return (
                                    <div
                                      key={`${idx}-${attempt.query.slice(0, 24)}`}
                                      className={`p-3 rounded-lg border ${
                                        isCurrent
                                          ? "bg-[#f5f3ff] border-[#ddd6fe]"
                                          : "bg-slate-50 border-slate-200"
                                      }`}
                                    >
                                      <div className="flex items-center justify-between gap-2 mb-1.5 flex-wrap">
                                        <div className="flex items-center gap-2 flex-wrap">
                                          <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-0.5 rounded-full border ${
                                            isCurrent
                                              ? "text-[#5b21b6] bg-white border-[#ddd6fe]"
                                              : "text-slate-500 bg-white border-slate-200"
                                          }`}>
                                            Attempt {idx + 1}
                                          </span>
                                          <span className="text-[11px] font-bold text-slate-600">
                                            {isOriginal ? "Original" : attempt.label}
                                          </span>
                                          {isCurrent && (
                                            <span className="text-[10px] font-bold uppercase tracking-widest text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-200">
                                              Active
                                            </span>
                                          )}
                                        </div>
                                        <button
                                          type="button"
                                          onClick={() => {
                                            navigator.clipboard?.writeText(attempt.query).catch(() => {});
                                          }}
                                          className="text-[10px] font-bold text-slate-500 hover:text-[#6366f1] px-2 py-0.5 rounded-md border border-slate-200 bg-white hover:bg-slate-50 transition-colors"
                                        >
                                          Copy
                                        </button>
                                      </div>
                                      <pre className="text-[12px] font-mono font-medium text-slate-700 leading-relaxed whitespace-pre-wrap break-words">
                                        {attempt.query}
                                      </pre>
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                            {/* 5.5: Run/Stop moved here so the recruiter reviews
                                the Boolean before kicking off a search. */}
                            <div className="flex items-center justify-end gap-2 mt-4">
                              {isSearching && (
                                <Button
                                  className="bg-white hover:bg-rose-50 text-rose-600 border border-rose-200 font-bold h-9 px-4 rounded-lg flex items-center gap-2 shadow-sm transition-all active:scale-95 text-[13.5px] flex-shrink-0"
                                  onClick={handleStopSearch}
                                >
                                  <Ban className="w-4 h-4" />
                                  Stop Search
                                </Button>
                              )}
                              <Button
                                className="bg-[#6366f1] hover:bg-[#4f46e5] text-white font-bold h-9 px-4 rounded-lg flex items-center gap-2 shadow-sm transition-all active:scale-95 text-[13.5px] flex-shrink-0"
                                onClick={handleRunSearch}
                                disabled={isSearching}
                              >
                                {isSearching ? (
                                  <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                                ) : (
                                  <Rocket className="w-4 h-4 fill-white" />
                                )}
                                Run Search
                              </Button>
                            </div>
                          </div>
                        ) : (
                          <div className="p-4 bg-white border border-[#ddd6fe] rounded-xl overflow-x-auto shadow-inner flex items-center justify-center py-6 gap-3">
                            <span className="w-5 h-5 border-2 border-slate-200 border-t-[#6366f1] rounded-full animate-spin" />
                            <div className="flex flex-col">
                              <p className="text-[13px] font-bold text-slate-700">Refreshing Boolean string...</p>
                              <p className="text-[12px] font-medium text-slate-500">Based on Page 5 sourcing filters only</p>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </section>
            </div>

            {/* Sourced Candidates Section */}
            <div className="border-t border-slate-200 pt-8 mt-10">
              <div className="flex items-center justify-between mb-8">
                <div>
                  <h4 className="text-[15px] font-bold text-slate-900 mb-1 flex items-center gap-2">
                    Sourced Candidates
                    {isSearching && (
                      <span className="flex h-2 w-2 relative">
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#6366f1] opacity-75"></span>
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-[#6366f1]"></span>
                      </span>
                    )}
                  </h4>
                  <p className={`text-slate-500 text-[13px] font-medium tracking-tight transition-all ${isSearching ? 'animate-pulse text-[#6366f1]' : ''}`}>
                    {hasSearched ? (
                        isSearching ? `Sourcing candidates... ${candidates.length} found so far` : `${candidates.length} candidates found${sourceFilter !== "all" ? ` · showing ${sortedCandidates.length}` : ""}`
                    ) : 'Run a search to find candidates.'}
                  </p>
                  {candidates.length > 0 && (
                    <div className="flex items-center gap-1.5 mt-3 flex-wrap">
                      {([
                        { id: "all", label: "All", count: candidates.length },
                        { id: "jobdiva", label: "JobDiva", count: sourceCounts["jobdiva"] || 0 },
                        { id: "linkedin-unipile", label: "LinkedIn-Unipile", count: sourceCounts["linkedin-unipile"] || 0 },
                        { id: "linkedin-exa", label: "LinkedIn-Exa", count: sourceCounts["linkedin-exa"] || 0 },
                        { id: "dice", label: "Dice", count: sourceCounts["dice"] || 0 },
                        { id: "upload-resume", label: "Upload-Resume", count: sourceCounts["upload-resume"] || 0 }
                      ] as const).map(pill => {
                        if (pill.id !== "all" && pill.count === 0) return null;
                        const active = sourceFilter === pill.id;
                        return (
                          <button
                            key={pill.id}
                            onClick={() => { setSourceFilter(pill.id as any); setCurrentPage(1); }}
                            className={`px-2.5 py-1 rounded-full text-[11px] font-bold uppercase tracking-wider border transition-colors ${active ? 'bg-[#6366f1] text-white border-[#6366f1]' : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'}`}
                          >
                            {pill.label} <span className={`ml-1 font-medium ${active ? 'text-white/80' : 'text-slate-400'}`}>{pill.count}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
                {candidates.length > 0 && (
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      className="h-8 px-4 text-[13px] font-bold border-slate-200 text-slate-700 bg-white shadow-sm flex items-center gap-2 hover:bg-slate-50"
                      onClick={() => {
                        const first150 = candidates.slice(0, 150);
                        const first150Ids = new Set(first150.map(c => c.candidate_id || c.id));

                        // Check if all first 150 are already selected
                        const allFirst150Selected = first150.every(c => selectedCandidates.has(c.candidate_id || c.id));

                        if (allFirst150Selected) {
                          // Deselect all first 150
                          setSelectedCandidates(prev => {
                            const next = new Set(prev);
                            first150.forEach(c => {
                              const id = c.candidate_id || c.id;
                              next.delete(id);
                            });
                            return next;
                          });
                        } else {
                          // Select all first 150
                          setSelectedCandidates(prev => {
                            const next = new Set(prev);
                            first150.forEach(c => {
                              const id = c.candidate_id || c.id;
                              next.add(id);
                            });
                            return next;
                          });
                        }
                      }}
                    >
                      <Star className="w-3.5 h-3.5 fill-slate-700" />
                      {(() => {
                        const first150 = candidates.slice(0, 150);
                        const allFirst150Selected = first150.every(c => selectedCandidates.has(c.candidate_id || c.id));
                        return allFirst150Selected ? 'Deselect Best 150' : 'Select Best 150';
                      })()
                      }
                    </Button>
                    <Button
                      variant="outline"
                      className="h-8 px-4 text-[13px] font-bold border-slate-200 text-slate-700 bg-white"
                      onClick={() => {
                        const allIds = candidates.map(c => c.candidate_id || c.id);
                        const allSelected = allIds.every(id => selectedCandidates.has(id));

                        if (allSelected) {
                          // Deselect all
                          setSelectedCandidates(new Set());
                        } else {
                          // Select all
                          setSelectedCandidates(new Set(allIds));
                        }
                      }}
                    >
                      {(() => {
                        const allIds = candidates.map(c => c.candidate_id || c.id);
                        const allSelected = allIds.every(id => selectedCandidates.has(id));
                        return allSelected ? 'Deselect All' : 'Select All';
                      })()
                      }
                    </Button>
                  </div>
                )}
                {isExternal && (
                  <Button
                    variant="outline"
                    className="h-8 px-4 text-[13px] font-bold border-[#6366f1] text-[#6366f1] bg-white shadow-sm flex items-center gap-2 hover:bg-[#f5f3ff]"
                    onClick={() => setPasteResumeOpen(true)}
                  >
                    <FileText className="w-3.5 h-3.5" />
                    Add via Resume
                  </Button>
                )}
              </div>

              {hasSearched ? (
                <>
                  {isSearching && (
                    <div className="mb-6 p-4 bg-[#f5f3ff]/50 backdrop-blur-sm border border-[#ddd6fe] rounded-2xl flex items-center justify-between shadow-sm animate-in fade-in slide-in-from-top-2 duration-300">
                      <div className="flex items-center gap-4">
                        <div className="relative flex items-center justify-center">
                          <div className="w-8 h-8 border-3 border-[#ddd6fe] border-t-[#6366f1] rounded-full animate-spin" />
                          <Search className="w-3.5 h-3.5 text-[#6366f1] absolute" />
                        </div>
                        <div>
                          <p className="text-[13px] font-bold text-slate-800 leading-tight">{searchStatus}</p>
                          <p className="text-[11px] font-medium text-slate-500 mt-0.5">Live sourcing in progress. Candidates appearing in real-time...</p>
                        </div>
                      </div>
                      <div className="px-3 py-1 bg-[#6366f1] text-white text-[10px] font-black rounded-lg uppercase tracking-tighter shadow-sm animate-pulse">
                        Live Search
                      </div>
                    </div>
                  )}

                  {candidates.length > 0 ? (
                    <div className="space-y-4">
                    {paginatedCandidates.map((candidate, idx) => {
                      // Select random badges to show matching elements
                      const badgeOptions = [
                        sourceTitles[0]?.value,
                        sourceSkills[0]?.value ? `${sourceSkills[0]?.value} certified` : null,
                        sourceSkills[1]?.value,
                        sourceLocations[0]?.value ? `Local to ${sourceLocations[0].value}` : null
                      ].filter(Boolean);

                      return (
                        <div key={`${candidate.candidate_id || candidate.id}-${idx}`} className="p-5 border border-slate-200 rounded-xl bg-white shadow-sm hover:border-purple-200 hover:shadow-md transition-all flex items-center gap-4">
                          <Checkbox
                            className="w-4.5 h-4.5 rounded border-slate-300 data-[state=checked]:bg-purple-600 data-[state=checked]:border-purple-600"
                            checked={selectedCandidates.has(candidate.candidate_id || candidate.id)}
                            onCheckedChange={(checked) => {
                              setSelectedCandidates(prev => {
                                const next = new Set(prev);
                                const id = candidate.candidate_id || candidate.id;
                                if (checked) next.add(id);
                                else next.delete(id);
                                return next;
                              });
                            }}
                          />
                        <div className="flex-1 min-w-0">
                          {(() => {
                            const displayName = getCandidateDisplayName(candidate);
                            return (
                            <div className="flex items-center justify-between gap-4">
                              <div className="flex items-center gap-3 min-w-0">
                                <a
                                  href={candidate.source?.startsWith('LinkedIn') ? candidate.profile_url || '#' : '#'}
                                  target={candidate.source?.startsWith('LinkedIn') ? "_blank" : undefined}
                                  rel={candidate.source?.startsWith('LinkedIn') ? "noopener noreferrer" : undefined}
                                  className={`text-[17px] font-bold text-slate-900 flex items-center gap-3 transition-colors group/name ${
                                    candidate.source?.startsWith('LinkedIn') ? 'hover:text-[#1d4ed8]' : 
                                    candidate.source === 'JobDiva-TalentSearch' ? 'hover:text-[#c2410c]' : 
                                    'hover:text-[#6366f1]'
                                  }`}
                                  onClick={async (e) => {
                                    if (candidate.source?.startsWith('LinkedIn')) return;
                                    e.preventDefault();
                                    // 5.8: prefer JobDiva profile URL when
                                    // available (opens in new tab); fall back
                                    // to the resume modal so the name is
                                    // never a dead click.
                                    const opened = await fetchAndOpenProfileUrl(candidate);
                                    if (!opened) handleViewResume(candidate);
                                  }}
                                >
                                   <span className="flex items-center gap-2">
                                     <span className={`text-[17px] font-bold text-slate-900 transition-colors ${
                                       candidate.source?.startsWith('LinkedIn') ? 'group-hover/name:text-[#1d4ed8]' : 
                                       candidate.source === 'JobDiva-TalentSearch' ? 'group-hover/name:text-[#c2410c]' : 
                                       'group-hover/name:text-[#6366f1]'
                                     }`}>
                                       {displayName}
                                     </span>
                                     <span 
                                       className={`h-7 w-7 flex items-center justify-center border border-slate-200 bg-white text-slate-400 rounded-lg shadow-sm transition-all ${
                                         candidate.source?.startsWith('LinkedIn') 
                                           ? 'group-hover/name:border-[#bfdbfe] group-hover/name:bg-[#eff6ff] group-hover/name:text-[#1d4ed8]' : 
                                         candidate.source === 'JobDiva-TalentSearch' 
                                           ? 'group-hover/name:border-[#fed7aa] group-hover/name:bg-[#fff7ed] group-hover/name:text-[#c2410c]' : 
                                         'group-hover/name:border-[#c7d2fe] group-hover/name:bg-[#f5f3ff] group-hover/name:text-[#6366f1]'
                                       }`}
                                       title={candidate.source?.startsWith('LinkedIn') ? "View LinkedIn Profile" : "Click to view resume"}
                                     >
                                       <ExternalLink className="w-3.5 h-3.5" />
                                     </span>
                                   </span>
                                </a>
                                <span className={`px-2.5 py-0.5 rounded-lg text-[11px] font-extrabold uppercase tracking-wider flex items-center gap-1.5 shadow-sm h-fit border ${candidate.source?.startsWith('LinkedIn')
                                    ? 'bg-[#eff6ff] text-[#1d4ed8] border-[#bfdbfe]'
                                    : candidate.source === 'JobDiva-TalentSearch'
                                      ? 'bg-[#fff7ed] text-[#c2410c] border-[#fed7aa]'
                                      : 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]'
                                  }`}>
                                  {candidate.source?.startsWith('LinkedIn') ? <Linkedin className="w-3 h-3 fill-current" /> : candidate.source === 'JobDiva-TalentSearch' ? <Zap className="w-3 h-3 fill-current" /> : <ShieldCheck className="w-3 h-3" />}
                                  {candidate.source || "JobDiva"}
                                </span>
                              </div>

                              <div className="flex items-center gap-3 shrink-0">
                                {candidate.match_score !== undefined && (
                                  <span className={`px-2.5 py-0.5 rounded-lg text-[11px] font-extrabold uppercase tracking-wider flex items-center shadow-sm h-fit border ${
                                    candidate.match_score >= 80 ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 
                                    candidate.match_score >= 60 ? 'bg-amber-50 text-amber-700 border-amber-200' : 
                                    'bg-rose-50 text-rose-700 border-rose-200'
                                  }`}>
                                    {candidate.match_score}% Match
                                  </span>
                                )}
                                {!candidate.source?.startsWith('LinkedIn') && (
                                  <Button
                                    size="sm"
                                    className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2"
                                    onClick={() => handleViewResume({ ...candidate, firstName: displayName.split(" ")[0] || displayName, lastName: displayName.split(" ").slice(1).join(" ") })}
                                    title="Open candidate resume"
                                  >
                                    <FileText className="w-3.5 h-3.5" />
                                    Resume
                                    <ExternalLink className="w-3 h-3 opacity-70" />
                                  </Button>
                                )}
                                <Button
                                  size="sm"
                                  className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                                  onClick={() => {
                                    setSelectedCandidateForDetails({
                                      name: displayName,
                                      profileUrl: candidate.profile_url,
                                      imageUrl: candidate.image_url,
                                      jobTitle: candidate.title || candidate.headline || "",
                                      location: candidate.location || (candidate.city ? `${candidate.city}, ${candidate.state}` : ""),
                                      experienceYears: candidate.experience_years || candidate.yearsExtracted || candidate.enhanced_info?.years_of_experience || null,
                                      tags: badgeOptions,
                                      matchScore: candidate.match_score,
                                      missingSkills: candidate.missing_skills,
                                      explainability: candidate.explainability,
                                      matchScoreDetails: candidate.match_score_details,
                                      matchedSkills: candidate.matched_skills,
                                    });
                                    setDetailsModalOpen(true);
                                  }}
                                >
                                  <Eye className="w-3.5 h-3.5" />
                                  View
                                </Button>
                              </div>
                            </div>
                            );
                          })()}
                          {/* 5.7: availability pill + abstract + location row.
                              Fields populated by jobdiva.py Talent Search
                              mapper. All three are optional — only render the
                              strip if at least one is present. */}
                          {(() => {
                            const availability = candidate.availability_status || candidate.available;
                            const abstract = candidate.abstract || "";
                            const locationStr = candidate.location || (candidate.city || candidate.state ? `${candidate.city || ""}${candidate.city && candidate.state ? ", " : ""}${candidate.state || ""}` : "");
                            if (!availability && !abstract && !locationStr) return null;
                            const availabilityColor =
                              String(availability || "").toLowerCase().includes("available") ? "bg-emerald-50 text-emerald-700 border-emerald-200" :
                              String(availability || "").toLowerCase().includes("placed") ? "bg-slate-100 text-slate-600 border-slate-200" :
                              "bg-amber-50 text-amber-700 border-amber-200";
                            return (
                              <div className="flex items-center gap-3 mt-2 text-[12px] text-slate-600">
                                {availability && (
                                  <span className={`px-2 py-0.5 rounded-full text-[10.5px] font-bold uppercase tracking-wider border ${availabilityColor}`}>
                                    {availability}
                                  </span>
                                )}
                                {locationStr && (
                                  <span className="inline-flex items-center gap-1 text-slate-500">
                                    <MapPin className="w-3 h-3" />
                                    {locationStr}
                                  </span>
                                )}
                                {abstract && (
                                  <span className="text-slate-500 truncate" title={abstract}>
                                    {abstract.length > 90 ? `${abstract.slice(0, 90).trimEnd()}…` : abstract}
                                  </span>
                                )}
                              </div>
                            );
                          })()}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                ) : isSearching ? (
                  <div className="flex flex-col items-center justify-center p-20 bg-slate-50/50 rounded-2xl border border-dashed border-slate-200 animate-pulse mt-4">
                    <div className="flex flex-col items-center gap-3">
                      <div className="w-12 h-12 border-4 border-slate-200 border-t-[#6366f1] rounded-full animate-spin mb-2" />
                      <p className="text-slate-600 text-sm font-bold animate-pulse">{searchStatus}</p>
                      <p className="text-slate-400 text-[12px] font-medium italic">Retrieving candidate records associated with Job ID {numericJobId || jobdivaId}...</p>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col items-center justify-center p-20 bg-slate-50/50 rounded-2xl border border-dashed border-slate-200 animate-in fade-in zoom-in duration-500">
                    <div className="w-16 h-16 rounded-full bg-slate-100 flex items-center justify-center mb-6 shadow-inner">
                      <Users className="w-8 h-8 text-slate-300" />
                    </div>
                    <p className="text-slate-600 text-base font-bold">No candidates found with the current filters.</p>
                    <p className="text-slate-400 text-[13px] mt-2 font-medium">Try broadening your criteria or adding more titles/skills.</p>
                  </div>
                )}

                {/* Pagination Controls */}
                {/* Pagination Controls */}
                {candidates.length > 0 && (
                    <div className="mt-8 flex items-center justify-between bg-white/70 backdrop-blur-xl p-3.5 px-5 rounded-2xl border border-slate-200/60 shadow-[0_8px_30px_rgb(0,0,0,0.04)] animate-in fade-in slide-in-from-bottom-2 duration-500 sticky bottom-6 z-10">
                      
                      {/* Context & Rows Selection */}
                      <div className="flex items-center gap-4">
                        <div className="flex items-center gap-2 text-[13px]">
                          <span className="text-slate-500 font-medium">Showing</span>
                          <span className="font-bold text-slate-800">
                            {(currentPage - 1) * candidatesPerPage + 1}-{Math.min(currentPage * candidatesPerPage, candidates.length)}
                          </span>
                          <span className="text-slate-500 font-medium">
                            of {candidates.length} {isSearching ? <span className="italic text-slate-400 font-normal ml-0.5">(sourcing...)</span> : 'candidates'}
                          </span>
                        </div>
                        
                        <div className="h-4 w-[1px] bg-slate-200/80"></div>
                        
                        <select
                          value={candidatesPerPage}
                          onChange={(e) => {
                            setCandidatesPerPage(Number(e.target.value));
                            setCurrentPage(1);
                          }}
                          className="bg-transparent text-[13px] font-bold text-slate-600 outline-none cursor-pointer border hover:bg-white/50 border-transparent hover:border-slate-200 rounded-md py-1 px-2 transition-all appearance-none pr-6 relative"
                          style={{ backgroundImage: `url("data:image/svg+xml;charset=UTF-8,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%2364748b' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3e%3cpolyline points='6 9 12 15 18 9'%3e%3c/polyline%3e%3c/svg%3e")`, backgroundRepeat: 'no-repeat', backgroundPosition: 'right 6px center', backgroundSize: '12px' }}
                        >
                          <option value={10}>10 / page</option>
                          <option value={20}>20 / page</option>
                          <option value={50}>50 / page</option>
                        </select>
                      </div>

                      {/* Numbered Pagination & Prev/Next */}
                      <div className="flex items-center gap-1.5" key={`pagination-${currentPage}-${totalPages}`}>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={currentPage === 1}
                          onClick={() => setCurrentPage(prev => Math.max(1, prev - 1))}
                          className="h-8 px-2.5 rounded-lg text-slate-500 font-bold hover:bg-slate-100 disabled:opacity-30 transition-all flex items-center justify-center"
                        >
                          <ChevronLeft className="w-4 h-4 shrink-0" />
                          <span className="sr-only">Previous</span>
                        </Button>
                        
                        <div className="flex items-center gap-1 mx-0.5">
                          {visiblePages.map((pageNum, idx) => (
                            pageNum === "..." ? (
                              <span key={`ellipsis-${idx}`} className="w-8 h-8 flex items-center justify-center text-slate-400 font-bold text-[14px]">
                                ...
                              </span>
                            ) : (
                              <button
                                key={`page-${pageNum}`}
                                disabled={currentPage === pageNum}
                                onClick={() => setCurrentPage(pageNum as number)}
                                className={`w-8 h-8 rounded-lg flex items-center justify-center font-bold text-[13px] transition-all duration-200 ${
                                  currentPage === pageNum 
                                    ? 'bg-[#6366f1] text-white shadow-md transform scale-105 cursor-default' 
                                    : 'text-slate-600 hover:bg-slate-100/80 cursor-pointer'
                                }`}
                              >
                                {pageNum}
                              </button>
                            )
                          ))}
                        </div>

                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={currentPage === totalPages}
                          onClick={() => setCurrentPage(prev => Math.min(totalPages, prev + 1))}
                          className="h-8 px-2.5 rounded-lg text-slate-500 font-bold hover:bg-slate-100 disabled:opacity-30 transition-all flex items-center justify-center"
                        >
                          <ChevronRight className="w-4 h-4 shrink-0" />
                          <span className="sr-only">Next</span>
                        </Button>
                      </div>
                    </div>
                )}
              </>
            ) : (
              <div className="h-4 flex items-center justify-center opacity-0 mt-4">
              </div>
            )}

            {/* Bulk Resume Upload */}
            <BulkUploadSection
              jobRef={numericJobId || jobdivaId}
              bulkFiles={bulkFiles}
              onBulkFilesChange={setBulkFiles}
              onClearProgress={() => setBulkProgress(null)}
              isUploadingBulk={isUploadingBulk}
              bulkProgress={bulkProgress}
              bulkFileInputRef={bulkFileInputRef}
              onUpload={handleBulkUpload}
            />
            </div>

            {/* Launch Footer */}
            <div className="border-t border-slate-200 pt-6 mt-2 flex items-center justify-between">
              <span className="text-[13px] font-medium text-slate-400">
                {hasSearched && !isSearching ? `${selectedCandidates.size} candidates selected` : ''}
              </span>
              <Button
                className={`h-[42px] px-5 text-white font-bold text-[14px] rounded-xl flex items-center gap-2 shadow-md transition-all group ${candidates.length > 0 && selectedCandidates.size > 0 ? "bg-[#6366f1] hover:bg-[#4f46e5] hover:translate-y-[-1px] active:translate-y-[0px] active:scale-[0.98]" : "bg-slate-300 cursor-not-allowed"}`}
                onClick={async () => {
                  if (selectedCandidates.size === 0) return;

                  try {
                    // Prepare candidates payload only for SELECTED candidates
                    const candidatesPayload = candidates
                      .filter(c => selectedCandidates.has(c.candidate_id || c.id))
                      .map(c => {
                        // Ensure name is never null or undefined for Pydantic validation
                        const displayName = getCandidateDisplayName(c);
                        
                        // Ensure skills is always a list
                        let skillList = [];
                        if (Array.isArray(c.skills)) {
                          skillList = c.skills;
                        } else if (typeof c.skills === 'string' && c.skills.trim()) {
                          try {
                            const parsed = JSON.parse(c.skills);
                            skillList = Array.isArray(parsed) ? parsed : [c.skills];
                          } catch (e) {
                            skillList = [c.skills];
                          }
                        }

                        return {
                          candidate_id: String(c.candidate_id || c.id || "unknown"),
                          name: displayName || "Unnamed Candidate",
                          email: c.email || null,
                          phone: c.phone || null,
                          skills: skillList,
                          experience_years: c.yearsExtracted || c.experience_years || 0,
                          source: c.source || "JobDiva-Applicants",
                          headline: c.title || c.headline || "",
                          location: c.location || "",
                          profile_url: c.profile_url || null,
                          image_url: c.image_url || null,
                          resume_text: c.resume_text || c.resumeText || "",
                          resume_id: String(c.resumeId || c.resume_id || ""),
                          is_selected: true,
                          education: Array.isArray(c.education || c.candidate_education) ? (c.education || c.candidate_education) : [],
                          certifications: Array.isArray(c.certifications || c.candidate_certification) ? (c.certifications || c.candidate_certification) : [],
                          company_experience: Array.isArray(c.company_experience || c.enhanced_info?.company_experience) ? (c.company_experience || c.enhanced_info?.company_experience) : [],
                          urls: (c.urls && typeof c.urls === 'object' && !Array.isArray(c.urls)) ? c.urls : (c.enhanced_info?.urls || {}),
                          match_score: typeof c.match_score === 'number' ? c.match_score : 0,
                          enhanced_info: (c.enhanced_info && typeof c.enhanced_info === 'object' && !Array.isArray(c.enhanced_info)) ? c.enhanced_info : null
                        };
                      });

                    const selectedCount = candidatesPayload.filter(c => c.is_selected).length;
                    console.log(`🚀 Launching Hoonr-Curate with ${selectedCount} selected candidates out of ${candidatesPayload.length} total`);

                    const apiUrl = API_BASE;
                    const response = await fetch(`${apiUrl}/candidates/save`, {
                      method: "POST",
                      headers: { "Content-Type": "application/json" },
                      body: JSON.stringify({
                        // Always send the ALPHANUMERIC jobdiva_id (e.g. '26-05172'), not the numeric job_id PK.
                        jobdiva_id: jobdivaId || jobData?.jobdiva_id || numericJobId,
                        candidates: candidatesPayload
                      })
                    });

                    const result = await response.json();

                    if (response.ok && result.status === 'success') {
                      const saved = result.saved_count || selectedCount;
                      showToast(`${saved} candidates saved to Master Pool — redirecting...`, "success");
                      setTimeout(() => {
                        router.push(`/candidates`);
                      }, 1500);
                    } else {
                      console.error('Save failed details:', JSON.stringify(result, null, 2));
                      const errorMsg = result.detail 
                        ? (Array.isArray(result.detail) ? JSON.stringify(result.detail) : result.detail)
                        : (result.message || 'Unknown error');
                      showToast(`Error saving candidates: ${errorMsg}`, "error");
                    }
                  } catch (e) {
                    console.error("Failed to save candidates:", e);
                    showToast("Failed to save candidates. Please try again.", "error");
                  }
                }}
                disabled={!hasSearched || isSearching || selectedCandidates.size === 0}
              >
                <Rocket className="w-4 h-4 fill-white" />
                Launch PAIR
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  const renderStepContent = () => {
    switch (currentStep) {
      case 1: return intakeStep;
      case 2: return publishStep;
      case 3: return establishRubricStep;
      case 4: return setFiltersStep;
      case 5: return sourceStep;
      default: return null;
    }
  };

  // Full-page loader while we hydrate a saved draft. Prevents the flash-of-
  // empty-form that recruiters see on Resume Setup while /jobs/{id}/draft
  // (plus rubric / screen questions) resolve.
  if (isLoadingDraft) {
    return (
      <div className="min-h-[calc(100vh-4rem)] flex items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-slate-500">
          <Loader2 className="w-8 h-8 animate-spin text-indigo-600" />
          <div className="text-[15px] font-medium">Loading draft…</div>
        </div>
      </div>
    );
  }

  return (
    <div className="p-8 max-w-7xl mx-auto animate-in fade-in slide-in-from-bottom-4 duration-500">
      {/* Breadcrumb */}
      <div className="mb-5">
        <Link href="/jobs" className="text-slate-500 hover:text-slate-700 text-[15px] flex items-center gap-2 transition-colors font-medium">
          <ArrowLeft className="w-4 h-4" />
          Back to Jobs
        </Link>
      </div>

      {/* Page Header */}
      <div className="mb-7">
        <h1 className="text-[32px] font-bold text-slate-900 leading-tight">New Job</h1>
        <p className="text-slate-500 text-[16px] font-medium mt-1">
          {(() => {
            const title = jobData?.title || jobTitle;
            const customer = jobData?.customer_name || jobData?.customer || "";
            if (!title && !customer) return "Enter a JobDiva Job ID to get started.";
            if (title && customer) return `${title} · ${customer}`;
            return title || customer;
          })()}
        </p>
      </div>

      {/* Step Indicator */}
      <StepIndicator />

      {/* Step Content */}
      <div className="mt-8">
        {renderStepContent()}
      </div>

      {/* Wizard Navigation — Back | Save & Exit … Next */}
      <div className="flex items-center justify-between pt-10 border-t border-slate-200 mt-12 mb-20 px-4">
        <div className="flex items-center gap-4">
          {currentStep > 1 && (
            <button
              onClick={() => setCurrentStep((currentStep - 1) as Step)}
              className="flex items-center gap-2.5 px-6 py-2.5 bg-white border border-slate-200 rounded-xl font-bold text-slate-700 hover:bg-slate-50 transition-all active:scale-95 shadow-sm"
            >
              <ArrowLeft className="w-4.5 h-4.5" />
              Back
            </button>
          )}

          <Button
            variant="outline"
            className="h-[44px] px-6 bg-white border-slate-200 flex items-center gap-2.5 shadow-sm text-[15px] font-bold text-slate-700 transition-all rounded-xl active:scale-95 hover:bg-slate-50"
            onClick={async () => {
              const saved = await saveJobDraft({ currentStep, saveType: "manual" });
              if (saved) {
                router.push("/");
              }
            }}
          >
            <Save className="w-4.5 h-4.5 text-slate-400" />
            Save & Exit
          </Button>
        </div>

        <div className="flex items-center gap-3">
          {currentStep < 5 && (
            <Button
              className="h-[44px] px-8 bg-[#6366f1] hover:bg-[#4f46e5] flex items-center gap-2 shadow-lg shadow-indigo-100 text-[15px] font-bold text-white transition-all rounded-xl active:scale-95"
              onClick={async () => {
                if (currentStep === 1) {
                  if (!jobData) {
                    showToast("Fetch a job first before saving.", "info");
                    return;
                  }
                  if (recruiterEmails.length === 0) {
                    setEmailError(true);
                    showToast("Recruiter Email is required.", "info");
                    return;
                  }
                  if (selectedEmpTypes.length === 0) {
                    showToast("Employment Type is required.", "info");
                    return;
                  }

                  const saved = await saveJobDraft({ currentStep: 1, skipToast: true });
                  if (!saved) {
                    showToast("Failed to save Step 1 data. Please try again.", "info");
                    return;
                  }
                  setCurrentStep(2);
                } else if (currentStep === 2) {
                  const saved = await saveJobDraft({ currentStep: 2, skipToast: true });
                  if (!saved) {
                    showToast("Failed to save Step 2 data. Please try again.", "info");
                    return;
                  }

                  if (!rubricData || (rubricData.titles?.length === 0 && rubricData.skills?.length === 0)) {
                    setIsGeneratingRubric(true);
                    setCurrentStep(3);
                    try {
                      const apiUrl = API_BASE;
                      const res = await fetch(`${apiUrl}/api/v1/ai-generation/jobs/generate-rubric`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                          jobId: numericJobId || jobdivaId,
                          jobdivaId: jobdivaId,
                          jobTitle: jobData?.title || jobTitle,
                          enhancedJobTitle: enhancedTitle || "",
                          jobDescription: jobPosting,
                          jobNotes: recruiterNotes,
                          originalDescription: jobData?.description || "",
                          customerName: jobData?.customer_name || jobData?.customer || "",
                          requiredDegree: jobData?.required_degree || "",
                          jobCity: jobData?.city || "",
                          jobState: jobData?.state || "",
                          locationType: jobData?.location_type || ""
                        })
                      });
                      if (res.ok) {
                        const data = await res.json();
                        setRubricData(applyTitleRequiredSafetyNet(data));
                        showToast("Step 2 saved and rubric generated!", "success");
                      } else {
                        throw new Error("API failed");
                      }
                    } catch (e) {
                      console.error(e);
                      showToast("Failed to generate rubric.", "info");
                      setRubricData(null);
                    } finally {
                      setIsGeneratingRubric(false);
                    }
                    return;
                  }
                } else if (currentStep === 3) {
                  const saved = await saveJobDraft({ currentStep: 3, skipToast: true });
                  if (!saved) return;
                } else if (currentStep === 4) {
                  const saved = await saveJobDraft({ currentStep: 4, skipToast: true });
                  if (!saved) return;
                  // Only derive sourcing criteria on first entry (5.3). The
                  // sync effect below won't override it on subsequent visits.
                  if (!sourcingCriteriaInitializedRef.current) {
                    initializeSourceFromRubric();
                    sourcingCriteriaInitializedRef.current = true;
                  }
                }

                if (currentStep < 5) setCurrentStep((currentStep + 1) as Step);
              }}
              disabled={(currentStep === 1 && !jobData) || isGeneratingJD || isSearching}
            >
              {isGeneratingJD ? (
                <>
                  <span className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin mr-2" />
                  Enriching...
                </>
              ) : (
                <>
                  Next
                  <ArrowRight className="w-5 h-5 ml-1.5" />
                </>
              )}
            </Button>
          )}
        </div>
      </div>

      {/* Toast Notification */}
      {toast && (
        <div
          className={`fixed bottom-8 right-8 flex items-center gap-2.5 px-5 py-3 rounded-lg text-[14px] font-medium text-white shadow-xl z-50 transition-all duration-300 transform translate-y-0 opacity-100 ${toast.type === "success" ? "bg-[#166534]" : "bg-primary"}`}
        >
          {toast.type === "success" ? (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5 flex-shrink-0 font-bold"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z" clipRule="evenodd" /></svg>
          ) : (
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-5 h-5 flex-shrink-0 font-bold"><path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clipRule="evenodd" /></svg>
          )}
          {toast.message}
        </div>
      )}

      {/* Email Modal */}
      {selectedCandidateForEmail && (
        <CandidateMessageModal
          candidateName={selectedCandidateForEmail.name}
          candidateEmail={selectedCandidateForEmail.email}
          isOpen={messageModalOpen}
          onClose={() => {
            setMessageModalOpen(false);
            setSelectedCandidateForEmail(null);
          }}
        />
      )}

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

      {/* Paste Resume Modal (External requirement) */}
      <PasteResumeModal
        open={pasteResumeOpen}
        onClose={() => setPasteResumeOpen(false)}
        name={pasteName}
        onNameChange={setPasteName}
        email={pasteEmail}
        onEmailChange={setPasteEmail}
        resumeText={pasteResumeText}
        onResumeTextChange={setPasteResumeText}
        isSaving={isSavingPasteResume}
        onSubmit={handleSubmitPasteResume}
      />

      {selectedCandidateForDetails && (
        <CandidateDetailsModal
          isOpen={detailsModalOpen}
          candidateName={selectedCandidateForDetails.name}
          profileUrl={selectedCandidateForDetails.profileUrl}
          imageUrl={selectedCandidateForDetails.imageUrl}
          jobTitle={selectedCandidateForDetails.jobTitle}
          location={selectedCandidateForDetails.location}
          experienceYears={selectedCandidateForDetails.experienceYears}
          tags={selectedCandidateForDetails.tags}
          matchScore={selectedCandidateForDetails.matchScore}
          missingSkills={selectedCandidateForDetails.missingSkills}
          matchedSkills={selectedCandidateForDetails.matchedSkills}
          matchScoreDetails={selectedCandidateForDetails.matchScoreDetails}
          explainability={selectedCandidateForDetails.explainability}
          onClose={() => {
            setDetailsModalOpen(false);
            setSelectedCandidateForDetails(null);
          }}
        />
      )}
    </div>
  );
};
