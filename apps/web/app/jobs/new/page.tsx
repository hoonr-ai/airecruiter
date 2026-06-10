"use client";

import { useState, useEffect, useEffectEvent, useCallback, useMemo, useRef, Suspense, type ReactNode } from "react";
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
  GripVertical,
  GraduationCap,
  UserCheck,
  Lightbulb,
  X,
  Box,
  Ban,
  Mail,
  MessageSquare,
  ExternalLink,
  Loader2,
  Briefcase,
  Clock,
  Phone,
  Calendar
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
import { PhoneIndicator } from "@/components/phone-indicator";
import { CandidateMatchTable, type CandidateMatchSortKey } from "@/components/candidate-match-table";
import { MissingContactsModal, type MissingContactCandidate } from "@/components/missing-contacts-modal";
import {
  LaunchPairProgressModal,
  initialLaunchProgress,
  type LaunchPairProgress,
  type LaunchBatchInfo,
  type LaunchFailedCandidate,
} from "@/components/launch-pair-progress-modal";
import { normalizePhone } from "@/lib/phone";
import { useEngagementFlow } from "@/hooks/use-engagement-flow";
import { API_BASE } from "@/lib/api";
import { trackEvent } from "@/lib/analytics";
import { logger } from "@/lib/logger";

const IS_QA_CURATE =
  typeof window !== "undefined" && window.location.hostname === "qacurate.hoonr.ai";
const LAUNCH_EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PLACEHOLDER_LAUNCH_EMAILS = new Set([
  "your-email@example.com",
  "email@example.com",
  "example@example.com",
  "test@example.com",
  "candidate@example.com",
  "noreply@example.com",
]);

function isValidLaunchEmail(value: string | null | undefined): boolean {
  const email = String(value || "").trim().toLowerCase();
  if (!email || !LAUNCH_EMAIL_RE.test(email)) return false;
  if (PLACEHOLDER_LAUNCH_EMAILS.has(email)) return false;
  if (email.endsWith("@example.com")) return false;
  if (email.endsWith("@noemail.pair.ai")) return false;
  // JobDiva auto-generates Auto_<id>@jobdiva.com for candidates with no real
  // email — dead addresses the backend blanks (engagement.py _is_placeholder_email).
  // Treat them as no-email here too so the launch gate matches the backend: a
  // synthetic-email-only candidate then routes to enrichment / manual entry
  // instead of slipping into the payload and failing the whole batch with a 400.
  if (email.endsWith("@jobdiva.com")) return false;
  return true;
}

function launchPhoneDigits(value: string | null | undefined): string {
  return String(value || "").replace(/\D/g, "");
}

function isValidLaunchPhone(value: string | null | undefined): boolean {
  return launchPhoneDigits(value).length >= 7;
}

// Defensive phone guard for streamed patches: the backend already makes
// CandidatesDetail/hydration phone patches upgrade-only (mobile-preferred), so
// here we only block the obvious downgrade — replacing a valid number with an
// empty/invalid one. Mobile-vs-home is decided server-side (slot/type known).
function betterPhoneUI(current: string | null | undefined, incoming: string | null | undefined): string {
  const cur = String(current || "").trim();
  const inc = String(incoming || "").trim();
  if (!inc) return cur;
  if (normalizePhone(inc) === null && normalizePhone(cur) !== null) return cur;
  return inc;
}

function getCandidateLaunchEmail(candidate: any): string {
  return String(
    candidate?.email ||
    candidate?.workEmail ||
    candidate?.personalEmail ||
    candidate?.enhanced_info?.email ||
    candidate?.enhanced_info?.workEmail ||
    candidate?.enhanced_info?.personalEmail ||
    candidate?.data?.email ||
    candidate?.data?.workEmail ||
    candidate?.data?.personalEmail ||
    candidate?.data?.enhanced_info?.email ||
    candidate?.data?.enhanced_info?.workEmail ||
    candidate?.data?.enhanced_info?.personalEmail ||
    candidate?.data?.zoominfo_contact_enrichment?.workEmail ||
    candidate?.data?.zoominfo_contact_enrichment?.personalEmail ||
    ""
  ).trim().toLowerCase();
}

function getCandidateLaunchPhone(candidate: any): string {
  return String(
    candidate?.phone ||
    candidate?.workPhone ||
    candidate?.mobilePhone ||
    candidate?.enhanced_info?.phone ||
    candidate?.enhanced_info?.workPhone ||
    candidate?.enhanced_info?.mobilePhone ||
    candidate?.data?.phone ||
    candidate?.data?.workPhone ||
    candidate?.data?.mobilePhone ||
    candidate?.data?.enhanced_info?.phone ||
    candidate?.data?.enhanced_info?.workPhone ||
    candidate?.data?.enhanced_info?.mobilePhone ||
    candidate?.data?.zoominfo_contact_enrichment?.mobilePhone ||
    candidate?.data?.zoominfo_contact_enrichment?.workPhone ||
    ""
  ).trim();
}

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

// Robust remote-job detection: catches "Remote", "Remote / W2", "Fully Remote",
// AND the JobDiva quirk where city is literally "REMOTE" with location_type empty.
function isRemoteJob(jd: { location_type?: string | null; city?: string | null } | null | undefined): boolean {
  if (!jd) return false;
  if ((jd.location_type || "").toLowerCase().includes("remote")) return true;
  return (jd.city || "").trim().toUpperCase() === "REMOTE";
}

const CANADIAN_PROVINCES = new Set([
  "ON", "BC", "QC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU",
]);

// monitored_jobs has no country column. Derive from state/province code so
// the JD copy and screening intro can say "based in {country}" without a
// schema migration. US is the default; only Canadian provinces flip it.
function deriveCountry(stateCode: string | null | undefined): string {
  return CANADIAN_PROVINCES.has((stateCode || "").trim().toUpperCase()) ? "Canada" : "United States";
}

// Provenance chip text for rubric items (titles, skills, education, domain).
// Items extracted by the AI keep the existing "Hoonr-Curate" label; items
// added manually by the recruiter via the "Add" buttons get "Recruiter".
function sourceLabel(source: string | null | undefined): string {
  return (source || "").toLowerCase() === "recruiter" ? "Recruiter" : "Hoonr-Curate";
}
function isRecruiterSource(source: string | null | undefined): boolean {
  return (source || "").toLowerCase() === "recruiter";
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

// Native HTML5 drag-reorder. Each call site gets its own dragIdx ref so two
// independent reorderable lists on the same screen don't see each other's
// drags (e.g. Step 3 skills + Step 4 questions when both are visible mid-jump).
function useDragReorder(onMove: (from: number, to: number) => void) {
  const dragIdxRef = useRef<number | null>(null);
  const onDragStart = (idx: number) => (e: React.DragEvent) => {
    dragIdxRef.current = idx;
    e.dataTransfer.effectAllowed = "move";
  };
  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  };
  const onDrop = (idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    const from = dragIdxRef.current;
    dragIdxRef.current = null;
    if (from === null || from === idx) return;
    onMove(from, idx);
  };
  const onDragEnd = () => { dragIdxRef.current = null; };
  return { onDragStart, onDragOver, onDrop, onDragEnd };
}

// Parse an existing `pass_criteria` into either {mode:'asap'} or {mode:'date',iso}.
// Falls back to 'asap' when the string isn't recognizable so the UI never renders
// an invalid date.
function parseAvailabilityCriteria(s: string): { mode: "asap" | "date"; iso?: string } {
  const raw = (s ?? "").trim();
  if (!raw) return { mode: "asap" };
  if (/ASAP/i.test(raw)) return { mode: "asap" };
  const m = raw.match(/by\s+(.+?)\s*$/i);
  if (!m) return { mode: "asap" };
  // F2: parse in UTC to avoid local timezone shifts during ISO conversion
  const d = new Date(m[1] + " UTC");
  if (isNaN(+d)) return { mode: "asap" };
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

// Minimal client-side mirror of `detect_role_family` in
// apps/api/services/screening_question_generator.py. Used only to pick the
// right Step-4 fallback templates when the backend generator can't be
// reached — the API itself remains the source of truth for normal flows.
const _IT_TITLE_KEYWORDS = [
  "engineer", "developer", "architect", "devops", "sre", "site reliability",
  "data engineer", "data scientist", "machine learning", "ai engineer",
  "cloud", "programmer", "full stack", "backend", "frontend", "qa automation",
  "platform", "security engineer", "database", "etl", "analytics engineer",
  "ios", "android", "sdet",
];
const _IT_SKILL_KEYWORDS = [
  "react", "angular", "vue", "next.js", "tailwind", "typescript", "javascript",
  "html5", "css3", "java", "spring", "kotlin", "swift", "node", ".net", "c#",
  "golang", "go ", "ruby", "microservice", "fastapi", "django", "flask",
  "kubernetes", "k8s", "terraform", "helm", "jenkins", "github actions",
  "kafka", "airflow", "snowflake", "databricks", "spark", "dbt", "redshift",
  "bigquery", "python", "selenium", "cypress", "playwright",
];
function isLikelyItRole(
  title: string,
  skills: Array<{ value?: string; name?: string }> = []
): boolean {
  const t = (title || "").toLowerCase();
  const skillBlob = skills
    .map(s => (s.value || s.name || "").toLowerCase())
    .join(" ");
  const haystack = ` ${t} ${skillBlob} `;
  if (_IT_TITLE_KEYWORDS.some(k => t.includes(k))) return true;
  return _IT_SKILL_KEYWORDS.some(k => haystack.includes(k));
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

type StepSnapshot = Record<string, unknown>;

const truncateForTelemetry = (value: unknown, max = 220): string | number | boolean | null => {
  if (value === null || value === undefined) return null;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value.length > max ? `${value.slice(0, max)}…` : value;
  const serialized = JSON.stringify(value);
  if (serialized.length > max) return `${serialized.slice(0, max)}…`;
  return serialized;
};

const diffSnapshots = (before: StepSnapshot = {}, after: StepSnapshot = {}) => {
  const keys = Array.from(new Set([...Object.keys(before), ...Object.keys(after)]));
  const changes: Array<{ field: string; before: unknown; after: unknown }> = [];

  for (const key of keys) {
    const prev = before[key];
    const next = after[key];
    if (JSON.stringify(prev) === JSON.stringify(next)) continue;
    changes.push({
      field: key,
      before: truncateForTelemetry(prev),
      after: truncateForTelemetry(next),
    });
  }

  return changes;
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

// Extracted UI dedupe logic using email OR phone
// Client-side cross-source de-dup. POLICY: a JobDiva row is never the dropped
// side of a collision — when the same person appears from JobDiva and another
// source we MERGE the best info into one surviving row and keep JobDiva's
// identity (so it stays Launch-PAIR-actionable). Matches only on STRONG
// identity (real email / phone+name / LinkedIn URL) so two different people
// sharing a common name+city are never falsely merged.
const isPlaceholderEmailUI = (e?: string) => {
  const n = String(e || "").trim().toLowerCase();
  if (!n || !n.includes("@")) return true;
  const domain = n.split("@").pop() || "";
  if (domain === "jobdiva.com") return true;
  if (n.endsWith("@noemail.pair.ai")) return true;
  return false;
};
const candIsJobDivaUI = (c: any) => {
  const s = String(c?.source || "").toLowerCase();
  if (s.startsWith("jobdiva")) return true;
  const srcs = Array.isArray(c?.sources) ? c.sources : [];
  if (srcs.some((x: any) => String(x || "").toLowerCase().startsWith("jobdiva"))) return true;
  return Boolean(String(c?.jobdiva_candidate_id || c?.jobdiva_id || c?.data?.jobdiva_candidate_id || "").trim());
};
const deduplicateCandidatesUI = (candidatesList: any[]) => {
  const normalizeEmail = (e?: string) => String(e || "").trim().toLowerCase();
  const normalizePhoneValue = (p?: string) => {
    const digits = String(p || "").replace(/\D/g, "");
    if (digits.length < 7) return "";
    // A shared/placeholder line (e.g. 000-000-0000, 555-555-5555) must not
    // collapse a whole agency's candidates into one row.
    if (new Set(digits.split("")).size < 4) return "";
    return digits.slice(-10);
  };
  const getName = (c: any) => {
    const f = String(c.firstName || "").toLowerCase().trim();
    const l = String(c.lastName || "").toLowerCase().trim();
    const full = `${f} ${l}`.trim();
    return full || String(c.name || "").toLowerCase().trim();
  };
  const getEmail = (c: any) => {
    const e = normalizeEmail(c.email || c.data?.email || c.enhanced_info?.email);
    return e && !isPlaceholderEmailUI(e) ? e : "";
  };
  const getPhone = (c: any) => normalizePhoneValue(c.phone || c.data?.phone || c.enhanced_info?.phone);
  const getLinkedin = (c: any) => {
    const u = String(c.profile_url || c.data?.urls?.linkedin || c.linkedin_url || "").trim().toLowerCase();
    return u.includes("linkedin.com") ? u.split("?")[0].replace(/\/+$/, "") : "";
  };

  const keysOf = (c: any): string[] => {
    const keys: string[] = [];
    const email = getEmail(c);
    if (email) keys.push(`email:${email}`);
    const phone = getPhone(c);
    const name = getName(c);
    if (phone && name && name.includes(" ")) keys.push(`phone-name:${phone}|${name}`);
    const li = getLinkedin(c);
    if (li) keys.push(`linkedin:${li}`);
    return keys;
  };

  const mergeBestOf = (dst: any, src: any) => {
    const srcList = (c: any) => {
      const out: string[] = [];
      if (Array.isArray(c.sources)) out.push(...c.sources.filter(Boolean).map(String));
      if (c.source) out.push(String(c.source));
      return out;
    };
    const merged = Array.from(new Set([...srcList(dst), ...srcList(src)]));
    if (merged.length) dst.sources = merged;
    for (const f of ["phone", "location", "city", "state", "title", "headline",
      "resume_id", "profile_url", "linkedin_url", "image_url", "experience_years",
      "jobdiva_candidate_id", "jobdiva_id"]) {
      if (!dst[f] && src[f]) dst[f] = src[f];
    }
    const dEmail = String(dst.email || "");
    const sEmail = String(src.email || "");
    if (sEmail && sEmail !== dEmail && (!dEmail || (isPlaceholderEmailUI(dEmail) && !isPlaceholderEmailUI(sEmail)))) {
      dst.email = sEmail;
    }
    const badResume = (r: any) => { const s = String(r || ""); return !s.trim() || s.includes("Resume content unavailable"); };
    const dR = String(dst.resume_text || "");
    const sR = String(src.resume_text || "");
    if (sR && !badResume(sR) && (badResume(dR) || sR.length > dR.length)) dst.resume_text = src.resume_text;
    return dst;
  };

  const getPrio = (s?: string) => {
    const l = String(s || "").toLowerCase();
    if (l.includes("applicant")) return 1;
    if (l.includes("talentsearch")) return 2;
    return 3;
  };

  let uniqueResults: any[] = [];

  for (const cand of candidatesList) {
    const cKeys = keysOf(cand);
    if (cKeys.length === 0) {
      uniqueResults.push(cand);
      continue;
    }

    // Find ALL existing rows that share a strong identity key with cand.
    const matchIndices: number[] = [];
    for (let i = 0; i < uniqueResults.length; i++) {
      const eKeys = keysOf(uniqueResults[i]);
      if (cKeys.some(k => eKeys.includes(k))) matchIndices.push(i);
    }

    if (matchIndices.length === 0) {
      uniqueResults.push(cand);
      continue;
    }

    // Same person across rows: pick ONE survivor, then fold everyone else's
    // best info into it. JobDiva-bearing record always wins the survivor slot
    // (never dropped); otherwise prefer has-both-contacts, then source
    // priority, then match_score.
    const competitors = [cand, ...matchIndices.map(i => uniqueResults[i])];
    let winner = competitors[0];
    for (let i = 1; i < competitors.length; i++) {
      const comp = competitors[i];
      const wJd = candIsJobDivaUI(winner);
      const cJd = candIsJobDivaUI(comp);
      if (cJd !== wJd) { if (cJd) winner = comp; continue; }
      const wBoth = Boolean(getEmail(winner)) && Boolean(getPhone(winner));
      const cBoth = Boolean(getEmail(comp)) && Boolean(getPhone(comp));
      if (cBoth !== wBoth) { if (cBoth) winner = comp; continue; }
      const wPrio = getPrio(winner.source);
      const cPrio = getPrio(comp.source);
      if (cPrio !== wPrio) { if (cPrio < wPrio) winner = comp; continue; }
      if (Number(comp.match_score || 0) > Number(winner.match_score || 0)) winner = comp;
    }

    // Clone the survivor so we never mutate an object still held in React state,
    // then absorb best-of from every other competitor.
    const survivor = { ...winner };
    for (const comp of competitors) {
      if (comp !== winner) mergeBestOf(survivor, comp);
    }

    uniqueResults = uniqueResults.filter((_, idx) => !matchIndices.includes(idx));
    uniqueResults.push(survivor);
  }
  return uniqueResults;
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

export default function NewJobPage() {
  return (
    <Suspense fallback={<div>Loading...</div>}>
      <NewJobPageContent />
    </Suspense>
  );
}

type WizardMode = 'edit' | 'source' | 'view';

function NewJobPageContent() {
  const router = useRouter();
  const engagement = useEngagementFlow();
  const searchParams = useSearchParams();
  const [currentStep, setCurrentStepState] = useState<Step>(1);
  // Track the highest step the user has ever reached so the pipeline/stepper
  // at the top allows jumping back to any step they've visited, not just
  // current-1 and current+1. Without this, stepping backward from step 4 to
  // step 1 forced the user to click Next three more times to return.
  const [maxStepReached, setMaxStepReached] = useState<Step>(1);
  // Mode controls whether Steps 1-4 are editable. 'edit' (default) for
  // Unpublished resume; 'source' for Active jobs sourcing another batch
  // (Steps 1-4 read-only, Step 5 actionable); 'view' for Inactive jobs
  // (everything read-only, Launch PAIR disabled).
  const [wizardMode, setWizardMode] = useState<WizardMode>('edit');
  const isReadOnly = wizardMode !== 'edit';
  const isViewOnly = wizardMode === 'view';
  // Already-launched candidate keys for Step 5 (only fetched in source/view modes).
  const [launchedCandidateKeys, setLaunchedCandidateKeys] = useState<Set<string>>(new Set());
  // Bare candidate_ids (no source prefix) for already-launched people. Used as a
  // resilient fallback so a candidate re-sourced under a slightly different
  // `source` string still gets hidden once they're launched / in the rank list.
  const [launchedCandidateIds, setLaunchedCandidateIds] = useState<Set<string>>(new Set());
  // DNC (Do Not Contact) phone set, fetched once at mount. Used to flag and
  // skip candidates whose phone matches a Zoom DNC entry.
  const [dncPhones, setDncPhones] = useState<Set<string>>(new Set());
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
      const { primary, similar } = collectResumeHighlightKeywords(candidate);
      setSelectedCandidateForResume({
        name: `${candidate.firstName} ${candidate.lastName}`,
        resumeText: resumeText,
        keywords: primary,
        similarKeywords: similar,
        jobdivaCandidateId: String(
          candidate.jobdiva_candidate_id ||
            candidate.data?.jobdiva_candidate_id ||
            candidate.candidate_id ||
            candidate.candidateId ||
            candidate.id ||
            ""
        ).trim() || undefined,
        source: candidate.source,
      });
      setResumeModalOpen(true);
    } else {
      alert("This candidate's resume is not available from JobDiva. Only real resumes from JobDiva are displayed.");
    }
  };

  // Build keyword lists for the resume highlight overlay. Returns two
  // tiers: 'primary' (yellow) for direct matches the user typed and the
  // rubric confirmed; 'similar' (light blue) for the auto-suggested
  // similar titles/skills the recruiter selected. Excludes any criteria
  // flagged as 'exclude'.
  const collectResumeHighlightKeywords = (candidate: any): { primary: string[]; similar: string[] } => {
    const primary = new Set<string>();
    const similar = new Set<string>();
    const addTo = (set: Set<string>, val: unknown) => {
      if (typeof val !== "string") return;
      const trimmed = val.trim();
      if (trimmed.length >= 2 && trimmed.length <= 60) set.add(trimmed);
    };

    if (Array.isArray(candidate?.matched_skills)) {
      candidate.matched_skills.forEach((s: any) => addTo(primary, typeof s === "string" ? s : s?.name));
    }
    if (Array.isArray(candidate?.matched_titles)) {
      candidate.matched_titles.forEach((s: any) => addTo(primary, typeof s === "string" ? s : s?.name));
    }

    sourceTitles.forEach((t) => {
      if (t.matchType === "exclude") return;
      addTo(primary, t.value);
      (t.selectedSimilarTitles || []).forEach((s) => addTo(similar, s));
    });
    sourceSkills.forEach((s) => {
      if (s.matchType === "exclude") return;
      addTo(primary, s.value);
      (s.selectedSimilarSkills || []).forEach((sim) => addTo(similar, sim));
    });
    sourceKeywords.forEach((k) => addTo(primary, k));

    // If a term ended up in both buckets, primary wins.
    primary.forEach((p) => similar.delete(p));

    return { primary: Array.from(primary), similar: Array.from(similar) };
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
  const [toast, setToast] = useState<{ message: string; type: "success" | "info" | "error" } | null>(null);
  const [pageSubtitle, setPageSubtitle] = useState(STEP_DESCRIPTIONS[1]);
  const [rubricData, setRubricData] = useState<any>(null);
  const [isGeneratingRubric, setIsGeneratingRubric] = useState(false);
  // Covers the entire Step-2 → Step-3 advance (draft save + rubric fetch) so
  // the Next button stays in a loading state continuously. `isGeneratingRubric`
  // alone misses the draft-save gap and makes the button look dead for ~1s.
  const [isAdvancingStep, setIsAdvancingStep] = useState(false);
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
    // OR-group bucket. Only meaningful when matchType === 'can'. Items in
    // the same group are OR'd together; different groups are AND'd. Existing
    // 'can' items default to group 1, preserving legacy single-bucket behavior.
    orGroup?: number;
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
    orGroup?: number;
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
  const [sourceLocationMiles, setSourceLocationMiles] = useState<number>(25);
  const [sourceCompanyInput, setSourceCompanyInput] = useState("");
  const [sourceKeywordInput, setSourceKeywordInput] = useState("");
  // PR-B: top-level minimum years of experience floor for sourcing.
  // null = no floor; an integer 0..40 enforces a hard minimum that the
  // backend applies pre-LLM (cheap regex over headline / resume snippet)
  // and post-LLM (parsed years_of_experience).
  const [minExperienceYears, setMinExperienceYears] = useState<number | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [isEnrichingContacts, setIsEnrichingContacts] = useState(false);
  const [missingContactsOpen, setMissingContactsOpen] = useState(false);
  // Realtime progress for the batched Launch PAIR flow (enrichment + per-batch
  // save/engage). Batches of 5 keep individual payloads small enough for the
  // backend; the modal surfaces per-batch status so the recruiter can see
  // what's happening on long runs.
  const LAUNCH_BATCH_SIZE = 5;
  // Bounded concurrency for the Launch PAIR contact-enrichment pass. Each
  // candidate's enrich-contact call runs the ZoomInfo→Apollo→Exa chain
  // server-side; doing them one-at-a-time made the modal crawl for minutes, so
  // we overlap up to N at once. Keep modest to stay under provider rate limits.
  const LAUNCH_ENRICH_CONCURRENCY = 6;
  const [launchProgress, setLaunchProgress] = useState<LaunchPairProgress>(initialLaunchProgress);
  const [missingContactCandidates, setMissingContactCandidates] = useState<MissingContactCandidate[]>([]);
  const [missingContactsReviewMode, setMissingContactsReviewMode] = useState(false);
  const [pendingLaunchOverrides, setPendingLaunchOverrides] = useState<Record<string, { phone?: string; email?: string }>>({});
  // QA-only safety toggle. When ON (default), Launch PAIR opens the manual
  // mobile/email override modal for every candidate (current QA behavior).
  // When OFF, Launch PAIR behaves exactly like production (auto-enrich +
  // launch for everyone). Has no effect outside QA (gated by IS_QA_CURATE).
  const [qaOverrideEnabled, setQaOverrideEnabled] = useState(true);
  const [readyLaunchedPendingRedirect, setReadyLaunchedPendingRedirect] = useState(false);
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
  // `true` when the current `candidates` list was restored from localStorage
  // rather than a fresh stream. Used to surface a small "Restored from last
  // run" caption so recruiters know results are stale until re-run.
  const [restoredFromCache, setRestoredFromCache] = useState(false);
  // True when the most recent JobDiva search hit a job whose JobDiva-side
  // AI matcher (Search Agent) criteria isn't configured. Surfaced as a
  // small amber banner above the results list nudging the recruiter to
  // open JobDiva and set criteria once for sharper matches.
  const [jobdivaCriteriaUnconfigured, setJobdivaCriteriaUnconfigured] = useState(false);
  const [showJobdivaSkillsModal, setShowJobdivaSkillsModal] = useState(false);
  const [skillsCopied, setSkillsCopied] = useState(false);
  const [isCheckingJobdivaCriteria, setIsCheckingJobdivaCriteria] = useState(false);
  const [hasCheckedJobdivaCriteria, setHasCheckedJobdivaCriteria] = useState(false);
  const seenCandidateIdsRef = useRef<Set<string>>(new Set());
  // Candidate ids whose detail lookup failed during the current search run
  // (JobDiva 429 / no resume). They're kept and scored from the JobAgent
  // skills; size drives the one summary toast fired when the run completes.
  const detailFailedIdsRef = useRef<Set<string>>(new Set());
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
  const [lastSearchRuntimeSec, setLastSearchRuntimeSec] = useState<number | null>(null);
  const [lastSearchRunsExecuted, setLastSearchRunsExecuted] = useState<number | null>(null);

  // Pagination state
  const [currentPage, setCurrentPage] = useState(1);
  const [candidatesPerPage, setCandidatesPerPage] = useState(20);
  // "Select Best N" — recruiter-tunable count for the bulk-select shortcut.
  // Persists across selection changes so re-clicking the button uses the
  // same N. The text input is the source of truth; the button reads it.
  const [selectBestN, setSelectBestN] = useState<number>(100);
  const [selectBestInput, setSelectBestInput] = useState<string>("100");
  const [sourceFilter, setSourceFilter] = useState<"all" | "jobdiva" | "linkedin-unipile" | "linkedin-exa" | "dice" | "upload-resume">("all");
  const [locationFilter, setLocationFilter] = useState<Set<string>>(new Set());
  const [minScore, setMinScore] = useState<number>(0);
  const [candidateSearchQuery, setCandidateSearchQuery] = useState<string>("");
  const [sortKey, setSortKey] = useState<CandidateMatchSortKey>("match");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [locationFilterOpen, setLocationFilterOpen] = useState(false);
  const locationFilterRef = useRef<HTMLDivElement | null>(null);
  const [locationOptionSearch, setLocationOptionSearch] = useState("");

  useEffect(() => {
    if (!locationFilterOpen) return;
    const onDown = (e: MouseEvent) => {
      if (locationFilterRef.current && !locationFilterRef.current.contains(e.target as Node)) {
        setLocationFilterOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [locationFilterOpen]);

  const currentWithinMiles = useMemo(() => {
    const radius = sourceLocations[0]?.radius;
    const parsed = typeof radius === "string" ? Number(radius.match(/(\d+)/)?.[1] ?? 25) : 25;
    return Math.min(100, Math.max(1, parsed));
  }, [sourceLocations]);

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

  const totalCandidatesCount = candidates.length;

  const getJobdivaSkills = () => {
    const seen = new Set<string>();
    const ordered: string[] = [];

    const append = (raw: unknown) => {
      const value = String(raw || "").trim();
      if (!value) return;
      const key = value.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      ordered.push(value);
    };

    // Prefer Step-5 sourced skills (recruiter-edited), excluding explicit "must not have".
    sourceSkills
      .filter((skill) => skill.matchType !== "exclude")
      .forEach((skill) => {
        append(skill.value);
        (skill.selectedSimilarSkills || []).forEach(append);
      });

    // Fallback: if source skills are empty, use rubric skills.
    if (ordered.length === 0) {
      (rubricData?.skills || []).forEach((skill: any) => append(skill?.value));
    }

    return ordered;
  };

  const jobdivaSkillsToUse = getJobdivaSkills();
  const toAgentToken = (value: unknown) =>
    String(value || "")
      .trim()
      .replace(/[()]/g, "")
      .toUpperCase();

  const parseLocationAgentToken = (rawLocation: string) => {
    const value = String(rawLocation || "").trim();
    if (!value) return "US";

    const parts = value.split(",").map((p) => p.trim()).filter(Boolean);
    const tail = parts.length > 0 ? parts[parts.length - 1] : value;
    const stateMatch = tail.match(/\b([A-Za-z]{2})\b/);
    if (stateMatch?.[1]) return `${stateMatch[1].toUpperCase()}-US`;

    if (/\b(united states|usa|us)\b/i.test(value)) return "US";
    return "US";
  };

  const buildJobdivaAgentString = () => {
    const groups: string[] = [];

    // JobDiva agent input parses each parenthesised group as an exact phrase
    // when wrapped in quotes — without the quotes "PROGRAM MANAGEMENT"
    // tokenises as two separate words. Matches the boolean string format
    // sent through the API (`("TERM")` per PR #159).
    const wrapTerm = (term: string) => `"${term}"`;

    const nonExcludedSkills = sourceSkills.filter((skill) => skill.matchType !== "exclude");
    if (nonExcludedSkills.length > 0) {
      nonExcludedSkills.forEach((skill) => {
        const terms = [skill.value, ...(skill.selectedSimilarSkills || [])]
          .map(toAgentToken)
          .filter(Boolean);

        const uniqueTerms = Array.from(new Set(terms));
        if (uniqueTerms.length === 0) return;

        groups.push(
          uniqueTerms.length === 1
            ? `(${wrapTerm(uniqueTerms[0])})`
            : `(${uniqueTerms.map(wrapTerm).join(" OR ")})`
        );
      });
    } else {
      jobdivaSkillsToUse.forEach((skill) => {
        const token = toAgentToken(skill);
        if (token) groups.push(`(${wrapTerm(token)})`);
      });
    }

    const locationToken = parseLocationAgentToken(sourceLocations[0]?.value || "");
    if (groups.length === 0) return `IN (${locationToken})`;
    return `${groups.join(" AND ")}, IN (${locationToken})`;
  };

  const jobdivaAgentString = buildJobdivaAgentString();
  const jobdivaSkillsCopyText = jobdivaAgentString;
  const jobdivaJobEditUrl = (jobdivaId || numericJobId)
    ? `https://www1.jobdiva.com/employers/myjobs/vieweditjobform.jsp?lstjobs=1&jobid=${encodeURIComponent(jobdivaId || numericJobId)}`
    : "";

  const candidateLocationOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const c of candidates) {
      const loc =
        (c as any).location ||
        ((c as any).city
          ? `${(c as any).city}${(c as any).state ? `, ${(c as any).state}` : ""}`
          : "");
      if (!loc) continue;
      counts.set(loc, (counts.get(loc) ?? 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);
  }, [candidates]);

  const getCandidateLocationStr = (c: any): string => {
    if (c.location) return String(c.location);
    if (c.city || c.state) {
      return `${c.city || ""}${c.city && c.state ? ", " : ""}${c.state || ""}`;
    }
    return "";
  };

  const getCandidateLastActiveDate = (c: any): Date | null => {
    const raw =
      c.available || c.DATEAVAILABLE || c.received || c.received_date || c.receivedDate || c.last_modified || c.lastModified;
    if (!raw) return null;
    const d = new Date(raw);
    return Number.isNaN(d.getTime()) ? null : d;
  };

  const getCandidateMatchScore = (c: any): number => {
    const score = Number(c?.match_score);
    return Number.isFinite(score) ? score : 0;
  };

  // A *genuine* hard-filter fail is a numeric 0% (hard-veto / exclusion rule).
  // Candidates we simply couldn't score — JobDiva detail/résumé lookup failed
  // (detail_failed) or the row never finished scoring — have NO numeric score
  // and render as "N/A". They must NOT be treated as a 0% drop at Launch PAIR;
  // only a real numeric 0 is skipped. (getCandidateMatchScore coerces a missing
  // score to 0, so it can't be used for the launch gate.)
  const isHardFilterZero = (c: any): boolean =>
    typeof c?.match_score === "number" && c.match_score === 0;

  const sortedCandidates = useMemo(() => {
    const trimmedQuery = candidateSearchQuery.trim().toLowerCase();
    const sourcePriority = (c: any) => {
      const source = String(c.source || "").toLowerCase();
      if (source.includes("applicant")) return 1;
      if (source.includes("linkedin")) return 2;
      if (source.includes("talentsearch")) return 3;
      return 4;
    };

    const filtered = candidates.filter((c: any) => {
      const candId = c.candidate_id || c.jobdiva_candidate_id || c.id;
      const key = `${c.source ?? ''}:${candId}`;
      // Hide anyone already launched (now in sourced_candidates / the rank
      // list). Match on the composite source:id key, falling back to the bare
      // candidate_id so source-string drift between sourcing runs can't let a
      // launched candidate re-surface.
      if (launchedCandidateKeys.has(key) || launchedCandidateIds.has(String(candId))) return false;
      if (!matchesSourceFilter(c)) return false;
      // Progressive rows (agent_result / details_loaded) bypass score &
      // location filters so they stay visible while shimmering. Once the
      // scored patch lands they fall back into the normal filter pipeline.
      const stage = String(c?._stage || "");
      const awaitingScore = stage === "agent_result" || stage === "details_loaded";
      const awaitingDetails = stage === "agent_result";
      // Candidates we couldn't score (detail_failed → N/A) are exempt from the
      // min-score filter — a failed detail lookup must not hide a JobDiva row.
      if (minScore > 0 && !awaitingScore && !c?.detail_failed) {
        const score = getCandidateMatchScore(c);
        if (score < minScore) return false;
      }
      if (locationFilter.size > 0 && !awaitingDetails) {
        const loc = getCandidateLocationStr(c);
        if (!loc || !locationFilter.has(loc)) return false;
      }
      if (trimmedQuery) {
        const haystack = [
          c.name,
          c.firstName,
          c.lastName,
          c.email,
          c.phone,
          c.title,
          c.headline,
        ]
          .map((v) => String(v || "").toLowerCase())
          .join(" ");
        if (!haystack.includes(trimmedQuery)) return false;
      }
      return true;
    });

    const dirMul = sortDir === "asc" ? 1 : -1;
    const cmp = (a: any, b: any) => {
      switch (sortKey) {
        case "match": {
          // JobDiva rows carry api_rank (recency for Applicants, JobAgent
          // rank for Talent Search). When both sides have it, JobDiva's
          // own ranking wins — match_score is a lenient/rough signal for
          // those rows, not the sort key. Falls through to score sort for
          // non-JobDiva pairs (no api_rank) and ties.
          const rankA = typeof a.api_rank === "number" ? a.api_rank : null;
          const rankB = typeof b.api_rank === "number" ? b.api_rank : null;
          if (rankA !== null && rankB !== null && rankA !== rankB) {
            return rankA - rankB;
          }

          const scoreA = getCandidateMatchScore(a);
          const scoreB = getCandidateMatchScore(b);
          if (scoreA !== scoreB) return (scoreA - scoreB) * dirMul;

          const prioA = sourcePriority(a);
          const prioB = sourcePriority(b);
          if (prioA !== prioB) return prioA - prioB;
          return 0;
        }
        case "name": {
          const nameA = String(a.name || `${a.firstName || ""} ${a.lastName || ""}`).trim().toLowerCase();
          const nameB = String(b.name || `${b.firstName || ""} ${b.lastName || ""}`).trim().toLowerCase();
          return nameA.localeCompare(nameB) * dirMul;
        }
        case "lastActive": {
          const dA = getCandidateLastActiveDate(a)?.getTime() ?? 0;
          const dB = getCandidateLastActiveDate(b)?.getTime() ?? 0;
          return (dA - dB) * dirMul;
        }
        case "location": {
          const locA = getCandidateLocationStr(a).toLowerCase();
          const locB = getCandidateLocationStr(b).toLowerCase();
          return locA.localeCompare(locB) * dirMul;
        }
        case "source": {
          const srcA = String(a.source || "").toLowerCase();
          const srcB = String(b.source || "").toLowerCase();
          return srcA.localeCompare(srcB) * dirMul;
        }
        default:
          return 0;
      }
    };

    return [...filtered].sort(cmp);
  }, [candidates, sourceFilter, minScore, locationFilter, candidateSearchQuery, sortKey, sortDir, launchedCandidateKeys, launchedCandidateIds]);

  const totalPages = Math.max(1, Math.ceil(sortedCandidates.length / candidatesPerPage));
  const paginatedCandidates = sortedCandidates.slice(
    (currentPage - 1) * candidatesPerPage,
    currentPage * candidatesPerPage
  );
  const qualityScorecard = hasSearched ? collectCandidateQualityStats(candidates) : null;
  const topMatchedPreview = (qualityScorecard?.top_matched_skills || []).slice(0, 2).map((item: any) => item.term).filter(Boolean);
  const topMissingPreview = (qualityScorecard?.top_missing_skills || []).slice(0, 2).map((item: any) => item.term).filter(Boolean);

  const visiblePages = (() => {
    if (totalPages <= 5) return Array.from({ length: totalPages }, (_, i) => i + 1);
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
  const stepEntrySnapshotRef = useRef<Partial<Record<Step, StepSnapshot>>>({});
  const stepStartMsRef = useRef<number>(Date.now());

  const getStepSnapshot = (step: Step): StepSnapshot => {
    switch (step) {
      case 1:
        return {
          isExternal,
          jobdivaId,
          numericJobId,
          hasJobData: !!jobData,
          jobTitle,
          recruiterNotes,
          selectedEmpTypes,
          recruiterEmails,
          screeningLevel,
          workAuthorization,
          selectedJobBoards,
        };
      case 2:
        return {
          enhancedTitle,
          postingLength: (jobPosting || "").length,
          postingPreview: truncateForTelemetry(jobPosting, 180),
          selectedJobBoards,
        };
      case 3:
        return {
          titleCount: rubricData?.titles?.length || 0,
          skillCount: rubricData?.skills?.length || 0,
          educationCount: rubricData?.education?.length || 0,
          domainCount: rubricData?.domain?.length || 0,
          customerRequirementsCount: rubricData?.customer_requirements?.length || 0,
          otherRequirementsCount: rubricData?.other_requirements?.length || 0,
          titleSignature: (rubricData?.titles || []).map((t: any) => `${t.value}|${t.required}|${t.minYears}|${t.matchType}`).slice(0, 20),
          skillSignature: (rubricData?.skills || []).map((s: any) => `${s.value}|${s.required}|${s.minYears}|${s.matchType}`).slice(0, 20),
        };
      case 4:
        return {
          resumeFiltersCount: resumeMatchFilters.length,
          activeResumeFiltersCount: resumeMatchFilters.filter(f => f.active).length,
          resumeFiltersSignature: resumeMatchFilters.map(f => `${f.category}|${f.value}|${f.active}`).slice(0, 40),
          screenQuestionCount: screenQuestions.length,
          screenQuestionsSignature: screenQuestions.map(q => `${q.question_text}|${q.pass_criteria}`).slice(0, 40),
          botIntroPreview: truncateForTelemetry(botIntroduction, 180),
        };
      case 5:
        return {
          searchSources,
          recentDaysFilter,
          includeNoResume,
          sourceTitlesCount: sourceTitles.length,
          sourceSkillsCount: sourceSkills.length,
          sourceLocationsCount: sourceLocations.length,
          sourceCompaniesCount: sourceCompanies.length,
          sourceKeywordsCount: sourceKeywords.length,
          sourceFilter,
          booleanQuery: truncateForTelemetry(resolvedGeneratedBoolean, 260),
        };
      default:
        return {};
    }
  };

  const trackStepStart = (step: Step) => {
    const snapshot = getStepSnapshot(step);
    stepEntrySnapshotRef.current[step] = snapshot;
    stepStartMsRef.current = Date.now();

    trackEvent("job_wizard_step_started", {
      step,
      step_label: STEP_LABELS[step],
      job_ref: (jobdivaId || numericJobId || "new").toString(),
      state: snapshot,
    });
  };

  const trackStepAdvance = (fromStep: Step, toStep: Step, context?: Record<string, unknown>) => {
    const before = stepEntrySnapshotRef.current[fromStep] || {};
    const after = getStepSnapshot(fromStep);
    const changes = diffSnapshots(before, after).slice(0, 80);

    trackEvent("job_wizard_step_completed", {
      from_step: fromStep,
      from_step_label: STEP_LABELS[fromStep],
      to_step: toStep,
      to_step_label: STEP_LABELS[toStep],
      duration_ms: Date.now() - stepStartMsRef.current,
      job_ref: (jobdivaId || numericJobId || "new").toString(),
      changes_count: changes.length,
      changed_fields: changes.map(c => c.field),
      changes,
      ...(context || {}),
    });
  };

  useEffect(() => {
    const jobIdFromUrl = searchParams.get("jobId");
    const modeParam = searchParams.get("mode");
    const stepParam = searchParams.get("step");

    if (modeParam === 'source') {
      setWizardMode('source');
    } else if (modeParam === 'view') {
      setWizardMode('view');
    }

    if (stepParam) {
      const parsed = parseInt(stepParam, 10);
      if (!Number.isNaN(parsed) && parsed >= 1 && parsed <= 5) {
        setCurrentStep(parsed as Step);
      }
    }

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

  // Pull the (candidate_id, source) keys for everyone already launched (i.e.
  // saved into sourced_candidates / showing in the rank list) so Step 5 hides
  // those rows. Runs in every mode: after the first launch in edit mode and on
  // every revisit in source mode, just-launched people must drop off the list
  // so the recruiter can keep launching PAIR for the remaining candidates.
  const refreshLaunchedKeys = useCallback(async () => {
    const ref = jobdivaId || numericJobId;
    if (!ref) return;
    try {
      const res = await fetch(`${API_BASE}/jobs/${ref}/launched-candidate-keys`);
      if (!res.ok) return;
      const json = await res.json();
      const keys = new Set<string>();
      const ids = new Set<string>();
      for (const item of json?.launched ?? []) {
        if (item?.candidate_id) {
          keys.add(`${item.source ?? ''}:${item.candidate_id}`);
          ids.add(String(item.candidate_id));
        }
      }
      setLaunchedCandidateKeys(keys);
      setLaunchedCandidateIds(ids);
    } catch (err) {
      console.warn('Failed to load launched candidate keys', err);
    }
  }, [jobdivaId, numericJobId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (cancelled) return;
      await refreshLaunchedKeys();
    })();
    return () => { cancelled = true; };
  }, [refreshLaunchedKeys]);

  // Fetch the DNC phone list once. Cached server-side; the small payload
  // (~95 phones) is fine to ship in full.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/dnc/keys`);
        if (!res.ok) return;
        const json = await res.json();
        if (cancelled) return;
        const phones = new Set<string>(Array.isArray(json?.phones) ? json.phones : []);
        setDncPhones(phones);
      } catch (err) {
        console.warn("Failed to load DNC keys", err);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Compute "${source}:${candidate_id}" keys for any candidate whose phone
  // is on the DNC list. Recomputed whenever candidates load, phones get
  // enriched, or the DNC list arrives.
  const dncCandidateKeys = useMemo(() => {
    if (dncPhones.size === 0) return new Set<string>();
    const keys = new Set<string>();
    for (const c of candidates) {
      const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
      if (!id) continue;
      const np = normalizePhone(c.phone);
      if (np && dncPhones.has(np)) {
        keys.add(`${c.source ?? ''}:${id}`);
      }
    }
    return keys;
  }, [candidates, dncPhones]);

  useEffect(() => {
    trackStepStart(currentStep);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStep]);

  // Open-to-Work polling for Exa-sourced LinkedIn candidates.
  // Mirrors the Hoonrai/Revelio frontend polling pattern: every 5s POST any
  // LinkedIn URL we haven't yet resolved to /candidates/open-to-work-statuses
  // and patch candidates whose status flips from "PENDING" to true/false.
  // Stops automatically once nothing is pending.
  useEffect(() => {
    const pendingUrls: string[] = [];
    for (const c of candidates) {
      const url = (c as any).profile_url || "";
      const otw = (c as any).open_to_work;
      if (!url || !String(url).toLowerCase().includes("linkedin.com")) continue;
      if (otw === true || otw === false) continue;
      pendingUrls.push(String(url));
    }
    if (pendingUrls.length === 0) return;

    let cancelled = false;
    const poll = async () => {
      try {
        const resp = await fetch(`${API_BASE}/candidates/open-to-work-statuses`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ links: pendingUrls }),
        });
        if (!resp.ok) return;
        const json = await resp.json();
        const cache = (json && json.openToWorkStatusCache) || {};
        const resolved: Record<string, boolean> = {};
        for (const u of pendingUrls) {
          const v = cache[u];
          if (v === true || v === false) resolved[u] = v;
        }
        if (cancelled || Object.keys(resolved).length === 0) return;
        setCandidates((prev: any[]) =>
          prev.map((c) => {
            const u = (c as any).profile_url || "";
            if (u && Object.prototype.hasOwnProperty.call(resolved, u)) {
              return { ...c, open_to_work: resolved[u] };
            }
            return c;
          })
        );
      } catch {
        // network blip — next interval will retry
      }
    };

    // Immediate first poll, then every 5s. Hoonrai uses 5s; matches actor latency.
    // Cap at 24 polls (~2 min) so a stuck backend never spins the chip forever.
    let attempts = 0;
    const MAX_ATTEMPTS = 24;
    poll();
    attempts += 1;
    const intervalId = setInterval(() => {
      if (attempts >= MAX_ATTEMPTS) {
        clearInterval(intervalId);
        setCandidates((prev: any[]) =>
          prev.map((c) => {
            const u = (c as any).profile_url || "";
            const otw = (c as any).open_to_work;
            if (!u || !String(u).toLowerCase().includes("linkedin.com")) return c;
            if (otw === true || otw === false) return c;
            return { ...c, open_to_work: false };
          })
        );
        return;
      }
      attempts += 1;
      poll();
    }, 5000);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
    };
  }, [candidates]);

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
            // Seed the rubric-fingerprint refs from the loaded rubric so a
            // Step 3 → 4 / 4 → 5 transition without edits doesn't think the
            // rubric "changed since last regeneration" and clobber the saved
            // questions/sourcing.
            const seededKey = computeRubricQuestionsKey(rData);
            lastQuestionsRubricKeyRef.current = seededKey;
            lastSourcingRubricKeyRef.current = `${seededKey}::[]`;
            // Restore screen questions if they exist in the rubric. Treat the
            // saved list as recruiter-curated so the Step-4 sync effect won't
            // re-add defaults the recruiter explicitly deleted; the regen
            // escape hatches (level change, explicit Regenerate, or rubric
            // change on Next) still work.
            if (rData.screen_questions?.length) {
              setScreenQuestions(rData.screen_questions.map((q: any, i: number) => ({ ...q, id: i + 1 })));
              setQuestionIdCounter(rData.screen_questions.length + 1);
              userHasEditedQuestionsRef.current = true;
              lastGeneratedLevelRef.current = draft.screening_level ?? screeningLevel;
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
        // Refresh sourcing fingerprint now that filters are loaded — pairs
        // with the rubric-key seed above so Step-4 → Step-5 only triggers
        // a sourcing refresh when something actually changed since this load.
        if (lastSourcingRubricKeyRef.current) {
          const seededKey = lastSourcingRubricKeyRef.current.split("::")[0] || "";
          lastSourcingRubricKeyRef.current = `${seededKey}::${JSON.stringify(
            normalized.filter((f: any) => f?.active).map((f: any) => `${f?.category ?? ""}|${f?.value ?? ""}`).sort()
          )}`;
        }
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
        if (typeof sf.recentDaysFilter === "number") setRecentDaysFilter(sf.recentDaysFilter);
        if (typeof sf.includeNoResume === "boolean") setIncludeNoResume(sf.includeNoResume);
        if (sf.minExperienceYears === null || typeof sf.minExperienceYears === "number") {
          setMinExperienceYears(sf.minExperienceYears);
        }
        if (typeof sf.sourceLocationMiles === "number") setSourceLocationMiles(sf.sourceLocationMiles);
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

    trackEvent("job_wizard_step1_job_search_started", {
      step: 1,
      job_ref_input: searchId,
    });

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

      trackEvent("job_wizard_step1_job_search_success", {
        step: 1,
        job_ref_input: searchId,
        resolved_job_id: data.id?.toString?.() || "",
        resolved_jobdiva_id: data.jobdiva_id?.toString?.() || "",
        title: truncateForTelemetry(data.title),
      });
    } catch (error: any) {
      console.error("Error fetching job:", error);
      showToast(error.message === "Job not found or incomplete data from JobDiva." ? "Job not found. Check the ID." : "Failed to fetch job. Use format: 26-06182", "info");
      trackEvent("job_wizard_step1_job_search_failed", {
        step: 1,
        job_ref_input: searchId,
        error: truncateForTelemetry(error?.message || String(error)),
      });
    } finally {
      setIsFetching(false);
    }
  };

  const handleEnhanceJob = async (titleOverride?: string, descOverride?: string, notesOverride?: string) => {
    setIsGeneratingJD(true);
    trackEvent("job_wizard_step2_jd_regenerate_requested", {
      step: 2,
      job_ref: (numericJobId || jobdivaId || "new").toString(),
      has_title_override: !!titleOverride,
      has_description_override: !!descOverride,
      has_notes_override: notesOverride !== undefined,
    });
    try {
      const response = await fetch(`${API_BASE}/api/v1/ai-generation/jobs/${numericJobId || jobdivaId || 'new'}/generate-description`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: titleOverride || jobTitle,
          jobDescription: descOverride || jobData?.description || jobPosting,
          jobNotes: notesOverride === undefined ? recruiterNotes : notesOverride,
          workAuthorization: selectedEmpTypes.join(", "),
          payRate: jobData?.pay_rate || "",
          // Forward rubric-derived context so the backend prompt can include
          // required YoE and Education/Certs without paraphrasing them away.
          yearsOfExperience: typeof rubricData?.total_years === "number"
            ? rubricData.total_years
            : (parseInt(rubricData?.total_years, 10) || null),
          education: Array.isArray(rubricData?.education) ? rubricData.education : [],
          certifications: Array.isArray(rubricData?.certifications) ? rubricData.certifications : [],
          // Remote-job context: backend uses these to inject the "This is a
          // remote position based in {country}" disclaimer and suppress
          // city/state in the body when the role is fully remote.
          workArrangement: jobData?.location_type || "",
          country: deriveCountry(jobData?.state),
          city: jobData?.city || "",
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
      trackEvent("job_wizard_step2_jd_regenerate_success", {
        step: 2,
        generated_length: (data?.description || "").length,
      });
    } catch (error) {
      const message = (error as Error)?.message ?? "unknown error";
      logger.error("ai_jd.enhance.exception", { message });
      showToast(`JD generation failed: ${message}`, "info");
      trackEvent("job_wizard_step2_jd_regenerate_failed", {
        step: 2,
        error: truncateForTelemetry(message),
      });
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
    trackEvent("job_wizard_step2_title_enhance_requested", {
      step: 2,
      title: truncateForTelemetry(jobTitle),
    });
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
        const nextTitle = (data?.title || "").trim();
        setEnhancedTitle(nextTitle);
        if (nextTitle) {
          await handleEnhanceJob(nextTitle);
        }

        showToast("Title enhanced by Hoonr-Curate.", "success");
        trackEvent("job_wizard_step2_title_enhance_success", {
          step: 2,
          title: truncateForTelemetry(data?.title),
        });
      } else {
        const err = await res.text();
        console.error("Title enhance failed:", err);
        showToast("Failed to enhance title.", "info");
        trackEvent("job_wizard_step2_title_enhance_failed", {
          step: 2,
          error: truncateForTelemetry(err),
        });
      }
    } catch (e) {
      console.error(e);
      showToast("Failed to enhance title.", "info");
      trackEvent("job_wizard_step2_title_enhance_failed", {
        step: 2,
        error: truncateForTelemetry((e as Error)?.message || String(e)),
      });
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
      trackEvent("job_wizard_step1_employment_type_changed", {
        step: 1,
        changed_type: type,
        selected_types: newTypes,
      });
      return newTypes;
    });
  };

  const toggleJobBoard = (board: string) => {
    setSelectedJobBoards(prev => {
      const newSelection = prev.includes(board) ? prev.filter(b => b !== board) : [...prev, board];
      trackEvent("job_wizard_step2_publish_targets_changed", {
        step: 2,
        changed_board: board,
        selected_boards: newSelection,
      });
      return newSelection;
    });
  };

  const saveJobDraft = async (stepData: {
    currentStep: number,
    saveType?: string,
    skipToast?: boolean
  }) => {
    if (isReadOnly) {
      // Source / view mode: Steps 1-4 are read-only, so skip the draft save
      // entirely. Falsely returning true keeps the Next button flow intact
      // (it gates step transitions on save success) without mutating the
      // saved job.
      return true;
    }
    if (!jobData || (!numericJobId && !jobdivaId)) {
      showToast("Job data not available for saving.", "info");
      return false;
    }

    // Bound the save fetch — the backend save now caps its transaction at
    // 10s (lock_timeout=2s, statement_timeout=10s in save_job_draft), so 20s
    // gives the server a comfortable window to either succeed or return a
    // 500 with a real error. Without this, a hung backend (e.g. row-lock
    // contention pre-fix) left the user staring at a silent spinner.
    const saveController = new AbortController();
    const saveTimeoutId = setTimeout(() => saveController.abort(), 20000);
    try {
      const apiUrl = API_BASE;
      // Use the new endpoint that saves directly to monitored_jobs
      const response = await fetch(`${apiUrl}/jobs/${numericJobId || jobdivaId}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: saveController.signal,
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
            keywords: sourceKeywords,
            recentDaysFilter,
            includeNoResume,
            minExperienceYears,
            sourceLocationMiles,
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
        const isAbort = error instanceof DOMException && error.name === "AbortError";
        const errorMsg = isAbort
          ? "Save timed out — please retry."
          : error instanceof Error ? error.message : "Failed to save. Please try again.";
        showToast(errorMsg, "error");
      }
      return false;
    } finally {
      clearTimeout(saveTimeoutId);
    }
  };

  // Step 5 has no Next button (the wizard hops straight to Launch PAIR), so
  // recruiter edits to titles / skills / filter knobs never persist unless
  // they happen to hit Save & Exit or toggle the boolean panel. Debounce a
  // silent auto-save whenever Step 5 sourcing state changes so reloads
  // restore everything.
  // step5DirtyRef tracks whether the user has made edits that the 1.5s
  // debounce hasn't flushed yet — if they click a different step indicator
  // within that window the cleanup below would clearTimeout, dropping the
  // edit. The currentStep-keyed effect further down catches that case.
  const step5DirtyRef = useRef(false);
  useEffect(() => {
    if (currentStep !== 5) return;
    if (isReadOnly) return;
    if (!jobData) return;
    step5DirtyRef.current = true;
    const handle = setTimeout(async () => {
      const ok = await saveJobDraft({ currentStep: 5, saveType: "auto", skipToast: true });
      if (ok) step5DirtyRef.current = false;
    }, 1500);
    return () => clearTimeout(handle);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    currentStep,
    isReadOnly,
    jobData,
    searchSources,
    sourceTitles,
    sourceSkills,
    sourceLocations,
    sourceCompanies,
    sourceKeywords,
    recentDaysFilter,
    includeNoResume,
    minExperienceYears,
    sourceLocationMiles,
  ]);

  // Flush a pending Step 5 save when the user navigates away from Step 5
  // (e.g., clicks the Step 4 indicator before the 1.5s debounce fires).
  useEffect(() => {
    if (currentStep === 5) return;
    if (!step5DirtyRef.current) return;
    if (isReadOnly) return;
    if (!jobData) return;
    step5DirtyRef.current = false;
    saveJobDraft({ currentStep: 5, saveType: "auto", skipToast: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentStep]);

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
              onClick={async () => {
                if (!isClickable) return;
                const fromStep = currentStep;
                if (stepNumber === fromStep) return;
                trackEvent("job_wizard_step_jumped", {
                  from_step: fromStep,
                  to_step: stepNumber,
                  from_step_label: STEP_LABELS[fromStep],
                  to_step_label: STEP_LABELS[stepNumber],
                });
                // Persist whatever the recruiter changed on the current step
                // before jumping. The Next button does this; jumping via the
                // step indicator used to skip the save, so deletions/edits
                // could silently revert on reload.
                if (jobData && (numericJobId || jobdivaId)) {
                  await saveJobDraft({ currentStep: stepNumber, skipToast: true });
                }
                setCurrentStep(stepNumber);
              }}
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

  // Preserve user-selected matchType (Exact/Similar). Previously this was
  // hardcoded to "Similar" on every normalization pass, which silently reverted
  // clicks on the Exact toggle — the state update landed, the normalizer
  // overwrote it, and the UI snapped back. Default to "Similar" only when no
  // prior value exists.
  const normalizeMatchType = (value: any): "Exact" | "Similar" =>
    value === "Exact" ? "Exact" : "Similar";

  const getNormalizedTitleItem = (titleItem: any) => {
    if (isDirectResumeTitle(titleItem)) {
      return {
        ...titleItem,
        required: "Required",
        matchType: normalizeMatchType(titleItem.matchType),
      };
    }

    return {
      ...titleItem,
      required: isRubricItemRequired(titleItem) ? "Required" : "Preferred",
      matchType: normalizeMatchType(titleItem.matchType),
    };
  };

  const getNormalizedSkillItem = (skillItem: any) => ({
    ...skillItem,
    required: isRubricItemRequired(skillItem) ? "Required" : "Preferred",
    matchType: normalizeMatchType(skillItem.matchType),
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
    trackEvent("job_wizard_step3_rubric_item_changed", {
      step: 3,
      category,
      index,
      field,
      value: truncateForTelemetry(value, 180),
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
    trackEvent("job_wizard_step3_rubric_item_reordered", {
      step: 3,
      category,
      from_index: from,
      to_index: to,
    });
  };

  const removeRubricItem = (category: string, index: number) => {
    console.log(`🗑️ Removing ${category} at index ${index}`);
    const itemToRemove = rubricData?.[category]?.[index];
    setRubricData((prev: any) => {
      if (!prev || !prev[category]) return prev;
      return {
        ...prev,
        [category]: prev[category].filter((_: any, i: number) => i !== index)
      };
    });
    trackEvent("job_wizard_step3_rubric_item_removed", {
      step: 3,
      category,
      index,
      value: truncateForTelemetry(itemToRemove?.value ?? itemToRemove?.field ?? itemToRemove, 180),
    });
  };

  const addRubricItem = (category: string, newItem: any) => {
    setRubricData((prev: any) => {
      if (!prev) return prev;
      const updated = { ...prev };
      if (!updated[category]) updated[category] = [];
      // Titles preserve the source from the call site (AI extractor passes
      // 'Hoonr-Curate'; the manual "Add Title" button passes 'Recruiter') so
      // the chip label can reflect provenance.
      if (category === 'titles') {
        const pairTitle = getNormalizedTitleItem({
          ...newItem,
          required: 'Preferred',
          matchType: 'Similar',
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
    trackEvent("job_wizard_step3_rubric_item_added", {
      step: 3,
      category,
      value: truncateForTelemetry(newItem?.value ?? newItem?.field ?? newItem, 180),
    });
  };

  const skillsDrag = useDragReorder((from, to) => moveRubricItem("skills", from, to));

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
                      <span className={`text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap ${isRecruiterSource(title.source) ? "bg-slate-100 text-slate-700" : "bg-[#ede9fe] text-[#6d28d9]"}`}>
                        {sourceLabel(title.source)}
                      </span>
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
                )
              })}

              <div className="mt-3">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={(rubricData.titles?.length || 0) >= 5}
                  onClick={() => addRubricItem('titles', { value: '', minYears: 0, recent: false, matchType: 'Similar', required: 'Preferred', source: 'Recruiter' })}
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
                <span className="text-[12px] font-normal text-slate-500">Ordered by importance</span>
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
              <div className="w-[24px] flex-shrink-0"></div>
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
              <div className="w-[36px] flex-shrink-0 flex items-center justify-center">
                Actions
              </div>
            </div>
            <div className="space-y-0">
              {rubricData.skills?.map((skill: any, idx: number) => (
                <div
                  key={idx}
                  className="flex items-center gap-2.5 py-2 border-b border-slate-200 last:border-b-0"
                  onDragOver={skillsDrag.onDragOver}
                  onDrop={skillsDrag.onDrop(idx)}
                  onDragEnd={skillsDrag.onDragEnd}
                >
                  <button
                    type="button"
                    draggable
                    onDragStart={skillsDrag.onDragStart(idx)}
                    onDragEnd={skillsDrag.onDragEnd}
                    className="w-[24px] flex-shrink-0 flex items-center justify-center text-slate-300 hover:text-slate-600 cursor-grab active:cursor-grabbing"
                    title="Drag to reorder"
                    aria-label="Drag to reorder skill"
                  >
                    <GripVertical className="w-4 h-4" />
                  </button>
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <input
                      type="text"
                      value={skill.value}
                      onChange={(e) => updateRubricItem('skills', idx, 'value', e.target.value)}
                      className="flex-1 min-w-0 text-[13px] font-normal text-slate-700 bg-transparent border border-transparent rounded px-2 py-1.5 outline-none focus:border-slate-200 focus:bg-white transition-all"
                    />
                    <span className={`text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight flex-shrink-0 whitespace-nowrap ${isRecruiterSource(skill.source) ? "bg-slate-100 text-slate-700" : "bg-[#ede9fe] text-[#6d28d9]"}`}>
                      {sourceLabel(skill.source)}
                    </span>
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
                  onClick={() => addRubricItem('skills', { value: '', minYears: 0, recent: false, matchType: 'Similar', required: 'Preferred', source: 'Recruiter' })}
                  className="border-slate-200 text-[#334155] bg-white hover:bg-slate-50 font-medium text-[13.5px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
                >
                  <Plus className="w-3.5 h-3.5 mr-1.5 text-slate-500" />
                  Add Skill
                </Button>
                <span className="ml-3 text-[13.5px] font-medium text-slate-500">
                  {(rubricData.skills?.length || 0)} skill{(rubricData.skills?.length || 0) === 1 ? '' : 's'}
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
                    <span className={`text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight whitespace-nowrap ml-1 uppercase ${isRecruiterSource(edu.source) ? "bg-slate-100 text-slate-700" : "bg-[#ede9fe] text-[#6d28d9]"}`}>
                      {sourceLabel(edu.source)}
                    </span>
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
                  onClick={() => addRubricItem('education', { degree: "Bachelor's degree", field: '', required: 'Preferred', source: 'Recruiter' })}
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
                    <span className={`text-[10.5px] font-bold px-2 py-0.5 rounded-full tracking-tight whitespace-nowrap ml-2 uppercase ${isRecruiterSource(dom.source) ? "bg-slate-100 text-slate-700" : "bg-[#ede9fe] text-[#6d28d9]"}`}>
                      {sourceLabel(dom.source)}
                    </span>
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
                  onClick={() => addRubricItem('domain', { value: '', required: 'Preferred', source: 'Recruiter' })}
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
              <span className="text-[12px] text-slate-500 font-normal">Location constraints, shift requirements, etc.</span>
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
    const target = resumeMatchFilters.find(f => f.id === id);
    setResumeMatchFilters(prev =>
      prev.map(filter =>
        filter.id === id ? { ...filter, active } : filter
      )
    );
    trackEvent("job_wizard_step4_resume_filter_toggled", {
      step: 4,
      filter_id: id,
      filter_category: target?.category || "",
      filter_value: truncateForTelemetry(target?.value),
      active,
    });
  };

  const updateResumeFilter = (id: number, value: string) => {
    setResumeMatchFilters(prev =>
      prev.map(filter =>
        filter.id === id ? { ...filter, value } : filter
      )
    );
    trackEvent("job_wizard_step4_resume_filter_value_changed", {
      step: 4,
      filter_id: id,
      value: truncateForTelemetry(value),
    });
  };

  const deleteResumeFilter = (id: number) => {
    const target = resumeMatchFilters.find(f => f.id === id);
    setResumeMatchFilters(prev => prev.filter(filter => filter.id !== id));
    trackEvent("job_wizard_step4_resume_filter_removed", {
      step: 4,
      filter_id: id,
      filter_category: target?.category || "",
      filter_value: truncateForTelemetry(target?.value),
    });
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
    trackEvent("job_wizard_step4_resume_filter_added", {
      step: 4,
      initial_category: "Custom",
    });
  };

  const updateResumeFilterCategory = (id: number, category: string) => {
    setResumeMatchFilters(prev =>
      prev.map(filter => (filter.id === id ? { ...filter, category } : filter))
    );
    trackEvent("job_wizard_step4_resume_filter_category_changed", {
      step: 4,
      filter_id: id,
      category: truncateForTelemetry(category),
    });
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
  // Records the screeningLevel the current question set was generated against.
  // If the recruiter bumps the level on Step 1 (e.g. Light → Intensive), the
  // sync effect is allowed to regenerate even if the list has been edited —
  // raising depth should pull in additional role-specific questions, and the
  // `customQuestions` pass-through below preserves hand-crafted entries.
  const lastGeneratedLevelRef = useRef<string | null>(null);

  const initializeScreenQuestionsFromRubric = async (opts: { force?: boolean } = {}) => {
    if (!jobData) return;
    // Source / view mode: Steps 1-4 are frozen and the saved question set is
    // the audit trail of what Alex actually asked. Never recompute defaults
    // here, even when called with `force: true` from the Step 3→4 path.
    if (isReadOnly) return;
    // Respect recruiter edits, UNLESS the screening level has changed since
    // the last generation (recruiter dialed depth up/down on Step 1 — they
    // expect the question set to track). `force: true` from the explicit
    // Regenerate button is the other escape hatch.
    const levelChanged = lastGeneratedLevelRef.current !== null
      && lastGeneratedLevelRef.current !== screeningLevel;
    if (userHasEditedQuestionsRef.current && !opts.force && !levelChanged) return;

    const addressParts = [jobData.address1, jobData.city, jobData.state].filter(Boolean);
    const addressStr = addressParts.join(", ");
    const location = `${jobData.city || ""}, ${jobData.state || ""}`.trim().replace(/^, |, $/g, "");
    const arrangement = (jobData.location_type || "").toLowerCase();
    const isRemote = isRemoteJob(jobData);
    const country = deriveCountry(jobData.state);
    const arrangementLabel = arrangement.includes("hybrid") ? "a hybrid" : "an onsite";

    let idCounter = 1;
    const questions: ScreenQuestion[] = [];
    const targetRoleSpecificCount =
      screeningLevel === "L1" ? 3 : screeningLevel === "L2" ? 7 : 5;
    const customQuestions = screenQuestions.filter(
      question => question.category !== "default" && question.category !== "role-specific"
    );
    const isIt = isLikelyItRole(
      enhancedTitle || jobTitle || "",
      (rubricData?.skills as Array<{ value?: string; name?: string }>) || []
    );

    // 1. Bot Introduction
    const introTitle = (enhancedTitle || jobTitle || "role").trim();
    const intro = isRemote
      ? `Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a remote ${introTitle} based in ${country}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`
      : `Hi {{candidate name}}, I'm Alex, a virtual recruiter with Pyramid Consulting. We are helping our client recruit for a ${introTitle} in ${location || "your area"}, and you seem to be a good fit for the role. Please note that conversation may be recorded for verification and quality purposes. Do you have about 8-12 minutes to begin the preliminary evaluation process for this role?`;
    setBotIntroduction(prev => (prev && prev.trim().length > 0 ? prev : intro));

    // 2. Default Questions — arrangement-aware, address-aware. The onsite/hybrid
    // question is a preference check, not a hard filter; recruiters can flip
    // it to a hard filter manually if disqualification should be automatic.
    const availabilityDate = jobData.start_date || 'ASAP';
    const defaultQs: Array<{ text: string; criteria: string; is_hard_filter?: boolean }> = [
      { text: "Are you open to exploring new job opportunities?", criteria: "Must be open to new job opportunities" },
      { text: "What is your current or most recent role and key responsibilities?", criteria: "" },
      { text: "What is your current location?", criteria: "" },
    ];
    if (!isRemote) {
      defaultQs.push({
        text: `This role follows ${arrangementLabel} work arrangement based in ${addressStr || location || "the job location"}. Are you open to working in this setup?`,
        criteria: `Must be open to ${arrangementLabel} work arrangement`,
      });
    }
    const availabilityText = "What is your earliest availability to start a new role?";

    defaultQs.push(
      { text: availabilityText, criteria: "" },
      { text: "What is your expected compensation for this role?", criteria: "" },
      { text: "Which types of working arrangements are you open to and eligible for? Select all that apply: W2 Employee, Subcontractor to Pyramid through your current employer, Independent Contractor", criteria: "" },
      { text: "Are you authorized to work indefinitely for any employer in the United States?", criteria: "" },
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
    const roleSpecific: ScreenQuestion[] = [];
    try {
      const apiUrl = API_BASE;
      const jobRef = numericJobId || jobdivaId || "new";
      const levelForApi = screeningLevel === "L1" ? "light" : screeningLevel === "L2" ? "intensive" : "medium";
      const res = await fetch(`${apiUrl}/api/v1/ai-generation/jobs/${jobRef}/screening-questions/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobTitle: (enhancedTitle || jobTitle || "").trim(),
          rubric: rubricData || {},
          screeningLevel: levelForApi,
          customerName: jobData?.customer_name || "",
          workArrangement: jobData?.location_type || "",
          // Plumbed so the backend can detect the JobDiva-import quirk where
          // city is literally "REMOTE" with location_type empty, and skip the
          // onsite work-arrangement question accordingly.
          city: jobData?.city || "",
          address: addressStr,
          totalYears: rubricData?.total_years ?? 0,
        }),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        console.warn(
          `screening-questions/generate ${res.status}; using template fallback`,
          text.slice(0, 500),
        );
      } else {
        const payload = await res.json();
        const raw = Array.isArray(payload?.questions) ? payload.questions : [];
        // Front-matter (intro, arrangement, total-years) is already owned by
        // the frontend above — keep only role-specific questions to avoid
        // duplicates.
        raw
          .filter((q: any) => {
            const cat = String(q?.category || "").toLowerCase();
            return cat !== "default" && cat !== "work-arrangement" && cat !== "intro" && cat !== "logistics";
          })
          .slice(0, targetRoleSpecificCount)
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

    // Backend is the source-of-truth for role-aware questions. If that call
    // fails, keep a minimal neutral fallback so Step 4 is never empty, while
    // avoiding technical-only wording that misfits non-IT roles.
    if (roleSpecific.length === 0 && rubricData?.skills) {
      rubricData.skills.forEach((skill: any) => {
        if (roleSpecific.length >= targetRoleSpecificCount) return;
        const skillName = skill.value || (isIt ? "this technology" : "this responsibility");
        const promptVariant = roleSpecific.length % 4;

        let questionText = "";
        let passCriteria = "";

        if (isIt) {
          if (promptVariant === 0) {
            questionText = `Walk through one concrete implementation choice you made with ${skillName} — what specific alternative did you reject, and what technical trade-off (latency, consistency, throughput, cost) drove the decision?`;
            passCriteria = `Candidate identifies a specific ${skillName} implementation choice, names the rejected alternative, and articulates a concrete technical trade-off.`;
          } else if (promptVariant === 1) {
            questionText = `In a ${skillName}-based system, what specific failure signal — a metric, log line, or error class — has actually led you to a root cause, and which exact configuration or code change prevented recurrence?`;
            passCriteria = `Candidate names a concrete ${skillName} signal, root cause, and the precise configuration knob, code path, or design change that fixed it.`;
          } else if (promptVariant === 2) {
            questionText = `Name one specific configuration knob, API method, or version-pinned behavior in ${skillName} you've personally tuned, and what observable behavior changed as a result.`;
            passCriteria = `Candidate names a real ${skillName} flag/API/syntax detail and ties it to a concrete, verifiable behavior change — not a generic 'we used it for X'.`;
          } else {
            questionText = `Pick one place where ${skillName} interacts with another part of your stack — what concrete contract, schema, or interface did you design or change, and what failure mode were you guarding against?`;
            passCriteria = `Candidate points to a specific ${skillName} integration boundary, names the contract/schema, and identifies the concrete failure mode the design defends against.`;
          }
        } else if (promptVariant === 0) {
          questionText = `Tell me about a recent situation where ${skillName} directly influenced the final outcome. What decision mattered most?`;
          passCriteria = `Candidate provides a concrete ${skillName} example, explains the decision made, and ties it to a measurable outcome.`;
        } else if (promptVariant === 1) {
          questionText = `Describe a challenging issue related to ${skillName}. How did you identify the cause and prevent it from happening again?`;
          passCriteria = `Candidate walks through concrete diagnosis steps for ${skillName} and a practical prevention action.`;
        } else if (promptVariant === 2) {
          questionText = `When priorities conflicted around ${skillName}, how did you balance speed, quality, and stakeholder expectations?`;
          passCriteria = `Candidate explains trade-offs around ${skillName} and shows clear prioritization with stakeholder alignment.`;
        } else {
          questionText = `What does strong execution in ${skillName} look like in your role, and can you share one example?`;
          passCriteria = `Candidate defines practical execution standards for ${skillName} and supports them with a specific real-world example.`;
        }

        roleSpecific.push({
          id: idCounter++,
          question_text: questionText,
          pass_criteria: passCriteria,
          is_default: false,
          category: "role-specific",
          order_index: questions.length + roleSpecific.length,
          is_hard_filter: false,
        });
      });

      // If we still don't have enough questions (few rubric skills), top up
      // to the exact level target with generic but concrete prompts.
      while (roleSpecific.length < targetRoleSpecificCount) {
        roleSpecific.push({
          id: idCounter++,
          question_text: isIt
            ? `Name one specific configuration knob, API method, or version-pinned behavior in your stack you've personally tuned, and what observable behavior changed as a result.`
            : `Share a recent project example where you solved a non-trivial problem under constraints. What factors shaped your decision?`,
          pass_criteria: isIt
            ? `Candidate names a real flag/API/syntax detail and ties it to a concrete, verifiable behavior change — not a generic 'we used it for X'.`
            : `Candidate gives a concrete situation, explains constraints and decision rationale, and describes the result.`,
          is_default: false,
          category: "role-specific",
          order_index: questions.length + roleSpecific.length,
          is_hard_filter: false,
        });
      }
    }

    // Belt-and-suspenders: keep role-specific count deterministic by level.
    if (roleSpecific.length > targetRoleSpecificCount) {
      roleSpecific.splice(targetRoleSpecificCount);
    }
    while (roleSpecific.length < targetRoleSpecificCount) {
      roleSpecific.push({
        id: idCounter++,
        question_text: isIt
          ? "Name one specific configuration knob, API method, or version-pinned behavior in your stack you've personally tuned, and what observable behavior changed as a result."
          : "Share a recent project example where you solved a non-trivial problem under constraints. What factors shaped your decision?",
        pass_criteria: isIt
          ? "Candidate names a real flag/API/syntax detail and ties it to a concrete, verifiable behavior change — not a generic 'we used it for X'."
          : "Candidate gives a concrete situation, explains constraints and decision rationale, and describes the result.",
        is_default: false,
        category: "role-specific",
        order_index: questions.length + roleSpecific.length,
        is_hard_filter: false,
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
    lastGeneratedLevelRef.current = screeningLevel;
  };

  const initializeSourceFromRubric = () => {
    if (!rubricData) return;

    const getRubricDrivenMatchType = (item: any, existingMatchType?: 'must' | 'can' | 'exclude') => {
      // Preserve ANY user-edited matchType. Previously only 'exclude' was
      // honored, so a recruiter who toggled a Preferred-rubric skill from
      // "OR" → "AND" (must) on Step 5 would silently see it flip back to
      // "OR" the next time syncStepFiveData ran (e.g. after a page reload
      // restored sf.titles/sf.skills and re-ran initializeSourceFromRubric).
      // Explicit re-seeding from rubric still happens via the Step 4 → 5
      // Next button when the rubric fingerprint changes (line ~8217).
      if (existingMatchType) return existingMatchType;
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

    // Per-category prefix index so a missing category (e.g. Step 4 has no
    // "Required Title" filters but does have "Required Skill" ones) lets all
    // titles through instead of filtering them to zero.
    const activeRubricCategories = new Set(
      Array.from(activeRubricFilterKeys).map(key => key.split("|")[0])
    );

    const shouldIncludeRubricItem = (category: string, value: string) => {
      if (activeRubricFilterKeys.size === 0) return true;
      if (!activeRubricCategories.has(category)) return true;
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
              orGroup: existing?.orGroup,
              years: title.minYears || 0,
              recent: existing?.recent ?? !!title.recent,
              similarCount: `${(title.similar_titles || []).length}/${(title.similar_titles || []).length} similar`,
              similarTitles: title.similar_titles || [],
              selectedSimilarTitles: existing?.selectedSimilarTitles?.filter((item: string) =>
                (title.similar_titles || []).includes(item)
              ) ?? (title.similar_titles || []),
              // Default expanded so recruiters see the related titles the
              // taxonomy is searching for without having to click "Similar".
              similarExpanded: existing?.similarExpanded ?? true,
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
              orGroup: existing?.orGroup,
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
        // Format: "City, State Zip" (e.g. "Tempe, AZ 85281"). Including the
        // zip narrows sourcing-provider matches that mishandle short state
        // codes alone. Falls back to "City, State" when the zip is missing.
        const city = (jobData.city || "").trim();
        const state = (jobData.state || "").trim();
        const zip = (jobData.zip_code || "").trim();
        const cityState = [city, state].filter(Boolean).join(", ");
        const loc = [cityState, zip].filter(Boolean).join(" ");
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
    // In source / view mode the Step-4 form is frozen — re-initializing
    // filters and questions from the rubric would clobber the saved set
    // that Alex used for the historical screens.
    if (isReadOnly) return;

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
    // View mode: Step 5 is frozen too, so skip the seed. Source mode is
    // the whole reason recruiters re-enter Step 5, so it still runs (the
    // ref below prevents repeat init within a single session).
    if (isViewOnly) return;
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

  // Fingerprint of the JD text the current rubric was generated against.
  // When the recruiter edits the AI JD on Step 2 and re-clicks Next, we
  // compare the new JD against this fingerprint — if it differs, the rubric
  // (including Min Experience / `total_years`) is regenerated. If the JD is
  // unchanged (Step 3 → back → Step 2 → Next without edits), the existing
  // rubric + any recruiter edits to it are preserved.
  const lastRubricJdRef = useRef<string>("");

  // Fingerprint of the rubric (titles+skills) used to generate the current
  // Step-4 question set. Step-3 → Step-4 Next compares it; if it changed
  // (skill renamed, added, removed, requirement flipped) we force-regenerate
  // role-specific questions while preserving recruiter custom questions.
  // Same fingerprint, plus filter signature, gates Step-4 → Step-5 sourcing
  // refresh below.
  const lastQuestionsRubricKeyRef = useRef<string>("");
  const lastSourcingRubricKeyRef = useRef<string>("");
  const computeRubricQuestionsKey = (rubric: any): string => {
    if (!rubric) return "";
    const titles = (rubric.titles || []).map((t: any) => `${t?.value ?? ""}|${t?.minYears ?? 0}|${t?.required ?? ""}`);
    const skills = (rubric.skills || []).map((s: any) => `${s?.value ?? ""}|${s?.minYears ?? 0}|${s?.required ?? ""}`);
    return JSON.stringify({ titles, skills, total_years: rubric.total_years ?? null });
  };
  const computeSourcingRubricKey = (rubric: any, filters: any[]): string => {
    const filterSig = (filters || [])
      .filter(f => f?.active)
      .map(f => `${f?.category ?? ""}|${f?.value ?? ""}`)
      .sort();
    return `${computeRubricQuestionsKey(rubric)}::${JSON.stringify(filterSig)}`;
  };

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

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (currentStep !== 5) return;

    const jobRef = String(jobdivaId || numericJobId || "draft").trim();
    const seenKey = `step5:agent-modal-seen:${jobRef}`;
    const alreadySeen = window.sessionStorage.getItem(seenKey) === "1";
    if (alreadySeen) return;

    setShowJobdivaSkillsModal(true);
    setSkillsCopied(false);
    window.sessionStorage.setItem(seenKey, "1");
  }, [currentStep, jobdivaId, numericJobId]);

  useEffect(() => {
    setHasCheckedJobdivaCriteria(false);
    setJobdivaCriteriaUnconfigured(false);
  }, [jobdivaId, numericJobId]);

  useEffect(() => {
    if (currentStep === 5) return;
    setHasCheckedJobdivaCriteria(false);
    setIsCheckingJobdivaCriteria(false);
  }, [currentStep]);

  useEffect(() => {
    if (currentStep !== 5) return;
    if (!searchSources.jobdiva) return;
    if (hasCheckedJobdivaCriteria) return;

    const jobRef = String(jobdivaId || numericJobId || "").trim();
    if (!jobRef) return;

    let cancelled = false;

    const checkJobdivaCriteria = async () => {
      setIsCheckingJobdivaCriteria(true);
      try {
        const res = await fetch(`${API_BASE}/candidates/jobdiva/${encodeURIComponent(jobRef)}/criteria-status`);
        if (!res.ok) throw new Error(`criteria status check failed (${res.status})`);

        const data = await res.json();
        const unconfigured = Boolean(data?.criteria_unconfigured);
        if (cancelled) return;

        setJobdivaCriteriaUnconfigured(unconfigured);
        setHasCheckedJobdivaCriteria(true);

        if (unconfigured) {
          setShowJobdivaSkillsModal(true);
          setSkillsCopied(false);
        }
      } catch (e) {
        if (!cancelled) {
          console.warn("JobDiva criteria pre-check failed", e);
          setHasCheckedJobdivaCriteria(true);
        }
      } finally {
        if (!cancelled) setIsCheckingJobdivaCriteria(false);
      }
    };

    checkJobdivaCriteria();

    return () => {
      cancelled = true;
    };
  }, [currentStep, searchSources.jobdiva, jobdivaId, numericJobId, hasCheckedJobdivaCriteria]);

  const addSourceTitle = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue) return;
    const normalized = cleanValue.toLowerCase();
    if (sourceTitles.some(t => t.value.trim().toLowerCase() === normalized)) {
      setSourceTitleInput("");
      return;
    }
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
    trackEvent("job_wizard_step5_source_title_added", {
      step: 5,
      value: truncateForTelemetry(cleanValue),
    });
  };

  const addSourceSkill = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue) return;
    const normalized = cleanValue.toLowerCase();
    if (sourceSkills.some(s => s.value.trim().toLowerCase() === normalized)) {
      setSourceSkillInput("");
      return;
    }
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
    trackEvent("job_wizard_step5_source_skill_added", {
      step: 5,
      value: truncateForTelemetry(cleanValue),
    });
  };

  const addSourceLocation = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue) return;
    setSourceLocations(prev => [
      ...prev,
      {
        id: Date.now(),
        value: cleanValue,
        radius: `within ${sourceLocationMiles} mi`
      }
    ]);
    setSourceLocationInput("");
    setGeneratedBoolean("");
    trackEvent("job_wizard_step5_source_location_added", {
      step: 5,
      value: truncateForTelemetry(cleanValue),
      radius_miles: sourceLocationMiles,
    });
  };

  const addSourceCompany = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue || sourceCompanies.includes(cleanValue)) return;
    setSourceCompanies(prev => [...prev, cleanValue]);
    setSourceCompanyInput("");
    setGeneratedBoolean("");
    trackEvent("job_wizard_step5_source_company_added", {
      step: 5,
      value: truncateForTelemetry(cleanValue),
    });
  };

  const addSourceKeyword = (value: string) => {
    const cleanValue = value.trim();
    if (!cleanValue || sourceKeywords.includes(cleanValue)) return;
    setSourceKeywords(prev => [...prev, cleanValue]);
    setSourceKeywordInput("");
    setGeneratedBoolean("");
    trackEvent("job_wizard_step5_source_keyword_added", {
      step: 5,
      value: truncateForTelemetry(cleanValue),
    });
  };

  const buildGeneratedBooleanString = () => {
    // JobDiva's Talent Search parser speaks a different dialect than the
    // generic "X" AND "N+ years" form used for LinkedIn/Dice/Exa: it wants
    // uppercase quoted terms and `"TERM" OVER N YRS` for experience clauses
    // (see apps/api/services/jobdiva_boolean_translator.py). When JobDiva is
    // the active source we render the string in its native syntax so the
    // recruiter sees what JobDiva will actually run — no more "Databricks
    // AND 5+ years" looking correct in the UI but silently getting rewritten
    // by the backend translator.
    const isJobDiva = !!searchSources.jobdiva;
    const quote = (value: string) => {
      const body = (isJobDiva ? value.toUpperCase() : value).replace(/"/g, '\\"');
      return `"${body}"`;
    };
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
      // JobDiva's Talent Search parser requires every skill/title term to be
      // wrapped in parens — bare `(SKILL)` fails recognition, `("SKILL")`
      // works. Always wrap for JobDiva even when the term has no similars.
      const base = isJobDiva || terms.length > 1
        ? `(${terms.join(" OR ")})`
        : terms[0];
      if (!base) return "";
      const experienceClause = years > 0
        ? (isJobDiva ? ` OVER ${years} YRS` : ` AND "${years}+ years"`)
        : "";
      const recentClause = recent ? " AND recent" : "";
      return `${base}${recentClause}${experienceClause}`;
    };

    const must: string[] = [];
    // OR-groups keyed by group id (1, 2, 3, ...). Items within a group are
    // OR'd; groups are AND'd into the must chain. Keeping the legacy `can`
    // bucket as group 1 means existing items still produce `(A OR B)`.
    const orGroups = new Map<number, string[]>();
    const orGroupSeen = new Map<number, Set<string>>();
    const exclude: string[] = [];
    const seenMust = new Set<string>();
    const seenExclude = new Set<string>();
    const addUnique = (bucket: string[], seen: Set<string>, clause: string, keyValue = clause) => {
      const key = normalizeTerm(keyValue);
      if (!clause || !key || seen.has(key)) return;
      seen.add(key);
      bucket.push(clause);
    };
    const addToOrGroup = (groupId: number, clause: string, keyValue: string) => {
      const gid = groupId > 0 ? groupId : 1;
      if (!orGroups.has(gid)) {
        orGroups.set(gid, []);
        orGroupSeen.set(gid, new Set());
      }
      addUnique(orGroups.get(gid)!, orGroupSeen.get(gid)!, clause, keyValue);
    };

    // Boolean string sent to JobDiva uses only the top 2 titles. Adding
    // every rubric-derived title would over-narrow the JobDiva search
    // (5 ANDed title clauses ≈ 0 results). The remaining titles still flow
    // through `title_criteria` in the API payload below, where they feed
    // the in-app title-boost scoring without affecting what JobDiva returns.
    // Exclude titles are always emitted (they only narrow the JobDiva search).
    const includedTitles = sourceTitles.filter(t => t.matchType !== "exclude").slice(0, 2);
    const excludeTitles = sourceTitles.filter(t => t.matchType === "exclude");
    [...includedTitles, ...excludeTitles].forEach(title => {
      const group = criterionGroup(title.value, title.selectedSimilarTitles || [], title.years, title.recent);
      if (!group) return;
      if (title.matchType === "exclude") addUnique(exclude, seenExclude, group, title.value);
      else if (title.matchType === "can") addToOrGroup(title.orGroup ?? 1, group, title.value);
      else addUnique(must, seenMust, group, title.value);
    });

    sourceSkills.forEach(skill => {
      const group = criterionGroup(skill.value, skill.selectedSimilarSkills || [], skill.years, skill.recent);
      if (!group) return;
      if (skill.matchType === "exclude") addUnique(exclude, seenExclude, group, skill.value);
      else if (skill.matchType === "can") addToOrGroup(skill.orGroup ?? 1, group, skill.value);
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
    // Multiple sourcing locations are always alternatives — a candidate in
    // any of them satisfies the location criterion — so OR them together
    // inside a single clause instead of pushing each into `must` (which
    // would AND them and reject every candidate that isn't in all).
    const locationClauses: string[] = [];
    const seenLocations = new Set<string>();
    sourceLocations
      .filter(location => location.value)
      .forEach(location => {
        const key = normalizeTerm(location.value);
        if (!key || seenLocations.has(key)) return;
        seenLocations.add(key);
        addSourceKey(location.value);
        locationClauses.push(`${quote(location.value)} ${location.radius}`);
      });
    if (locationClauses.length === 1) {
      must.push(locationClauses[0]);
    } else if (locationClauses.length > 1) {
      must.push(`(${locationClauses.join(" OR ")})`);
    }

    const parts = [...must];
    // Render OR-groups in ascending group-id order so the string is stable
    // across re-renders. Singleton groups are flattened (no parens) — `(A)`
    // and `A` are equivalent in Boolean syntax but the parens look odd.
    const sortedGroupIds = Array.from(orGroups.keys()).sort((a, b) => a - b);
    sortedGroupIds.forEach(gid => {
      const items = orGroups.get(gid) || [];
      if (items.length === 0) return;
      parts.push(items.length === 1 ? items[0] : `(${items.join(" OR ")})`);
    });
    let booleanString = parts.length ? parts.join(" AND ") : (isValidBoolean(jobTitle) ? jobTitle : quote(jobTitle || "Role"));
    if (exclude.length) booleanString += ` NOT (${exclude.join(" OR ")})`;
    return booleanString;
  };

  // Returns the next OR-group id available across all source* state. Used by
  // the "+ New OR group" menu item to mint a fresh bucket without colliding
  // with an existing one.
  const nextOrGroupId = (): number => {
    let maxId = 0;
    sourceTitles.forEach(t => {
      if (t.matchType === "can" && (t.orGroup ?? 1) > maxId) maxId = t.orGroup ?? 1;
    });
    sourceSkills.forEach(s => {
      if (s.matchType === "can" && (s.orGroup ?? 1) > maxId) maxId = s.orGroup ?? 1;
    });
    return maxId + 1 >= 1 ? maxId + 1 : 1;
  };
  const existingOrGroupIds = (): number[] => {
    const ids = new Set<number>();
    sourceTitles.forEach(t => { if (t.matchType === "can") ids.add(t.orGroup ?? 1); });
    sourceSkills.forEach(s => { if (s.matchType === "can") ids.add(s.orGroup ?? 1); });
    return Array.from(ids).sort((a, b) => a - b);
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

  const relaxStructuralOverrides = (
    tier: number,
    currentFilters: typeof resumeMatchFilters,
    currentTitles: typeof sourceTitles,
    currentSkills: typeof sourceSkills,
    currentCompanies: typeof sourceCompanies
  ): {
    resumeMatchFiltersOverride?: typeof resumeMatchFilters;
    titleCriteriaOverride?: typeof sourceTitles;
    skillCriteriaOverride?: typeof sourceSkills;
    companiesOverride?: typeof sourceCompanies;
  } => {
    const zeroedTitles = currentTitles.map(t => ({ ...t, years: 0, recent: false }));
    const zeroedSkills = currentSkills.map(s => ({ ...s, years: 0, recent: false }));

    if (tier === 1) {
      const skillsRelaxed = (() => {
        const mustIdxs = zeroedSkills
          .map((s, i) => (s.matchType === "must" ? i : -1))
          .filter(i => i >= 0);
        const demoteCount = Math.ceil(mustIdxs.length / 2);
        const demoteSet = new Set(mustIdxs.slice(0, demoteCount));
        return zeroedSkills.map((s, i) =>
          demoteSet.has(i) ? { ...s, matchType: "can" as const } : s
        );
      })();
      return {
        titleCriteriaOverride: zeroedTitles,
        skillCriteriaOverride: skillsRelaxed,
      };
    }

    const titlesAllCan = zeroedTitles.map(t =>
      t.matchType === "must" ? { ...t, matchType: "can" as const } : t
    );
    const skillsAllCan = zeroedSkills.map(s =>
      s.matchType === "must" ? { ...s, matchType: "can" as const } : s
    );
    const filtersDeactivated = currentFilters.map(f =>
      (f.category || "").toLowerCase().includes("exclude") ? { ...f, active: false } : f
    );

    return {
      titleCriteriaOverride: titlesAllCan,
      skillCriteriaOverride: skillsAllCan,
      companiesOverride: [],
      resumeMatchFiltersOverride: filtersDeactivated,
    };
  };

  const relaxBooleanString = (input: string, tier: number): { query: string; label: string } => {
    const original = String(input || "").replace(/\s+/g, " ").trim();
    let query = original;
    let label = "";

    const isLocationClause = (part: string) => {
      const p = String(part || "").toLowerCase();
      return p.includes("within") && p.includes("mi");
    };

    const splitByAnd = (value: string) =>
      value.split(/\s+AND\s+/i).map(v => v.trim()).filter(Boolean);

    if (tier === 1) {
      query = query.replace(/\s+AND\s+"\d+\+\s*years?"/gi, "");
      query = query.replace(/\s+OVER\s+\d+\s+YRS\b/gi, "");
      const parts = splitByAnd(query);
      const locationParts = parts.filter(isLocationClause);
      const nonLocation = parts.filter(p => !isLocationClause(p));
      if (nonLocation.length > 1) {
        query = `(${nonLocation.join(" OR ")})${locationParts.length ? ` AND ${locationParts.join(" AND ")}` : ""}`;
      }
      label = "Relaxed must clauses by intelligence · kept location radius";
    } else if (tier === 2) {
      query = query.replace(/\(([^()]+?)\)/g, (_m, inner) => {
        const parts = String(inner).split(/\s+AND\s+/i).map((p: string) => p.trim()).filter(Boolean);
        return parts.length > 1 ? `(${parts.join(" OR ")})` : `(${inner})`;
      });
      query = query.replace(/\s+AND\s+recent/gi, "");
      query = query.replace(/\s+OVER\s+\d+\s+YRS\b/gi, "");
      query = query.replace(/\s+AND\s+"\d+\+\s*years?"/gi, "");
      label = "Further relaxed required clauses · kept location radius";
    } else {
      query = query.replace(/\s+NOT\s+\([^)]*\)/gi, "");
      const andParts = query.split(/\s+AND\s+/i).map(p => p.trim()).filter(Boolean);
      const locationPart = andParts.find(p => /within\s+\d+\s+mi/i.test(p));
      const rolePart = andParts.find(p => !/within\s+\d+\s+mi/i.test(p) && !/"\d+\+\s*years?"/i.test(p));
      const keep = [rolePart, locationPart].filter(Boolean) as string[];
      query = keep.length ? keep.join(" AND ") : andParts[0] || query;
      label = "Broadest recovery mode (role + location only)";
    }

    query = query.replace(/\s+/g, " ").trim();

    // Safety net: if tier transform produced no effective change, make one
    // deterministic loosening so retries are meaningfully different.
    if (query === original) {
      const withoutYears = query.replace(/\s+OVER\s+\d+\s+YRS\b/gi, "").replace(/\s+AND\s+"\d+\+\s*years?"/gi, "").trim();
      if (withoutYears && withoutYears !== query) {
        query = withoutYears;
        label = label || "Dropped year thresholds";
      } else if (/\s+AND\s+/i.test(query)) {
        const parts = query.split(/\s+AND\s+/i).map(p => p.trim()).filter(Boolean);
        query = parts.slice(0, -1).join(" AND ") || query;
        label = label || "Broadened query scope";
      }
      query = query.replace(/\s+/g, " ").trim();
    }

    return { query, label };
  };

  const countQualified = (list: any[]) =>
    list.filter(c => (c.match_score || 0) >= QUALIFIED_SCORE_THRESHOLD).length;

  function summarizeTopTerms(values: string[], limit = 10) {
    const counts = new Map<string, number>();
    values
      .map(v => String(v || "").trim())
      .filter(Boolean)
      .forEach(v => counts.set(v, (counts.get(v) || 0) + 1));

    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, limit)
      .map(([term, count]) => ({ term: truncateForTelemetry(term, 80), count }));
  }

  function collectCandidateQualityStats(list: any[]) {
    const total = list.length;
    const scoreList = list
      .map(c => Number(c?.match_score))
      .filter(score => Number.isFinite(score));

    const tier90 = scoreList.filter(score => score >= 90).length;
    const tier80 = scoreList.filter(score => score >= 80).length;
    const tier70 = scoreList.filter(score => score >= 70).length;

    const sourceCounts = list.reduce((acc: Record<string, number>, c: any) => {
      const source = String(c?.source || "unknown");
      acc[source] = (acc[source] || 0) + 1;
      return acc;
    }, {});

    const matchedSkills = list.flatMap((c: any) =>
      Array.isArray(c?.matched_skills)
        ? c.matched_skills.map((s: any) => String(s || "")).filter(Boolean)
        : []
    );

    const missingSkills = list.flatMap((c: any) =>
      Array.isArray(c?.missing_skills)
        ? c.missing_skills.map((s: any) => String(s || "")).filter(Boolean)
        : []
    );

    return {
      total_results: total,
      scored_results: scoreList.length,
      average_match_score: scoreList.length
        ? Number((scoreList.reduce((sum, score) => sum + score, 0) / scoreList.length).toFixed(2))
        : null,
      quality_tier_counts: {
        gte_90: tier90,
        gte_80: tier80,
        gte_70: tier70,
        lt_70: Math.max(0, scoreList.length - tier70),
      },
      quality_tier_pct: {
        gte_90: total ? Number(((tier90 / total) * 100).toFixed(2)) : 0,
        gte_80: total ? Number(((tier80 / total) * 100).toFixed(2)) : 0,
        gte_70: total ? Number(((tier70 / total) * 100).toFixed(2)) : 0,
      },
      source_counts: sourceCounts,
      top_matched_skills: summarizeTopTerms(matchedSkills, 12),
      top_missing_skills: summarizeTopTerms(missingSkills, 12),
    };
  }

  const buildStep5FilterContext = () => ({
    search_sources: Object.keys(searchSources).filter(k => (searchSources as any)[k]),
    recent_days: recentDaysFilter,
    include_no_resume: includeNoResume,
    active_resume_filters_count: resumeMatchFilters.filter(f => f.active).length,
    active_resume_filters: resumeMatchFilters
      .filter(f => f.active)
      .slice(0, 40)
      .map(f => ({
        category: truncateForTelemetry(f.category, 60),
        value: truncateForTelemetry(f.value, 120),
        weight: f.weight,
      })),
    source_criteria: {
      titles: sourceTitles.slice(0, 20).map(t => ({ value: truncateForTelemetry(t.value, 100), match_type: t.matchType, years: t.years, recent: t.recent })),
      skills: sourceSkills.slice(0, 20).map(s => ({ value: truncateForTelemetry(s.value, 100), match_type: s.matchType, years: s.years, recent: s.recent })),
      locations: sourceLocations.slice(0, 10).map(l => ({ value: truncateForTelemetry(l.value, 80), radius: l.radius })),
      companies: sourceCompanies.slice(0, 20).map(c => truncateForTelemetry(c, 80)),
      keywords: sourceKeywords.slice(0, 30).map(k => truncateForTelemetry(k, 80)),
    },
  });

  const buildSearchPayload = (
    booleanString: string,
    overrides?: {
      resumeMatchFiltersOverride?: typeof resumeMatchFilters;
      titleCriteriaOverride?: typeof sourceTitles;
      skillCriteriaOverride?: typeof sourceSkills;
      companiesOverride?: typeof sourceCompanies;
    }
  ) => {
    const effectiveTitles = overrides?.titleCriteriaOverride ?? sourceTitles;
    const effectiveSkills = overrides?.skillCriteriaOverride ?? sourceSkills;

    const titleCriteria = effectiveTitles.map(t => ({
      value: t.value || "Title",
      match_type: t.matchType || "must",
      years: t.years || 0,
      recent: t.recent || false,
      similar_terms: t.selectedSimilarTitles || []
    }));
    const skillCriteria = effectiveSkills.map(s => ({
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
    const withinMiles = Math.min(100, Math.max(1, parsedRadius));
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
      companies: overrides?.companiesOverride ?? sourceCompanies,
      resume_match_filters: activeResumeFilters,
      location: primaryLocation?.value || "",
      within_miles: withinMiles,
      sources: selectedSourcesArray,
      boolean_string: booleanString,
      // 5.6 / 5.10 plumbing — backend honors these in
      // jobdiva_service.search_candidates. `recent_days: 0` means Any.
      recent_days: recentDaysFilter > 0 ? recentDaysFilter : null,
      require_resume: !includeNoResume,
      // PR-B: top-level YOE floor. `undefined` means no constraint;
      // backend applies as both pre-LLM regex gate and post-LLM hard
      // filter against the parsed years_of_experience.
      min_experience_years:
        typeof minExperienceYears === "number" && minExperienceYears > 0
          ? minExperienceYears
          : undefined,
      // Hiring client / account name. Powers the "Same client / industry"
      // scoring dimension and the currently-employed-by-client veto. The
      // backend falls back to monitored_jobs.customer_name when this is
      // omitted, and filters placeholder values ("External"/"Unknown").
      client_name: (jobData?.customer_name || jobData?.customer || "").trim() || undefined,
      page: 1,
      page_size: 100
    };
  };

  const runSearchStream = async (
    booleanString: string,
    mode: "replace" | "append",
    overrides?: {
      resumeMatchFiltersOverride?: typeof resumeMatchFilters;
      titleCriteriaOverride?: typeof sourceTitles;
      skillCriteriaOverride?: typeof sourceSkills;
      companiesOverride?: typeof sourceCompanies;
    }
  ): Promise<any[]> => {
    const apiUrl = API_BASE;
    const payload = buildSearchPayload(booleanString, overrides);

    const mapStageToStatus = (stage: string) => {
      const raw = String(stage || "").toLowerCase();
      // Exa deep-search messages have their own informative shape
      // ("warming up", "running (N seeds)", "done — X enriched, Y new",
      // "skipped: <reason>") — pass them through unchanged so the
      // recruiter can see Pass B progress in the status bar instead of
      // collapsing it into a generic "Searching at Exa portal..." line.
      if (raw.includes("exa deep-search")) {
        return stage;
      }
      if (raw.includes("jobdiva applicants")) {
        return "Searching at JobDiva portal (Applicants)...";
      }
      if (raw.includes("jobdiva talent")) {
        return "Searching at JobDiva portal (Talent Search)...";
      }
      if (raw.includes("linkedin")) {
        return "Searching at LinkedIn portal...";
      }
      if (raw.includes("exa")) {
        return "Searching at Exa portal...";
      }
      if (raw.includes("dice")) {
        return "Searching at Dice portal...";
      }
      return stage;
    };

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
    let activePortal = "source portal";
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
              setCandidates(prev => deduplicateCandidatesUI([...prev, event.data]));

              const foundCount = runList.length;
              if (foundCount === 1) {
                setSearchStatus(`Found 1 profile from ${activePortal}. Matching resumes against the rubric...`);
              } else if (foundCount % 5 === 0) {
                setSearchStatus(`Found ${foundCount} profiles from ${activePortal}. Matching resumes against the rubric...`);
              }
            } else if (event.type === "candidate_detail") {
              // Two flavors share this event:
              //   1. Background CandidatesDetail hydration patches that fill
              //      email/phone/linkedin_url/resume_text into a thin row
              //      streamed earlier.
              //   2. Progressive Step-5 enrichment stages: `stage` is one of
              //      "jobdiva_details" | "scored" | "dropped". The row was
              //      emitted at the agent_result stage with shimmer cells; the
              //      patches replace those cells as data lands. `dropped`
              //      removes the row outright (filter failure, no resume,
              //      cross-source dup, etc.).
              const targetId = String(event.candidate_id || "");
              const stage = String(event.stage || "");
              if (!targetId) continue;

              if (stage === "dropped") {
                // Remove the matching row by id, and forget it from the seen
                // set so a re-run can re-add it. Drop reason is in patch
                // (_drop_reason) — surfaced via console for debugging.
                const drop_reason = (event.patch && (event.patch as any)._drop_reason) || "unknown";
                if (process.env.NODE_ENV !== "production") {
                  console.debug(`[Step-5] dropped candidate ${targetId} reason=${drop_reason}`);
                }
                runList = runList.filter(r => String(r.candidate_id || r.id || "") !== targetId);
                seenIds.delete(targetId);
                setCandidates(prev => prev.filter(c => (
                  String(c.candidate_id || c.jobdiva_candidate_id || c.id || "") !== targetId
                )));
                continue;
              }

              const patch = (event.patch && typeof event.patch === "object")
                ? event.patch
                : {};
              if (Object.keys(patch).length === 0) continue;
              // Detail-lookup failures (JobDiva 429 / no resume) are kept and
              // scored from the JobAgent skills; track them for one summary toast
              // when the run completes.
              if (
                (patch as any).detail_failed === true ||
                ["kept_no_resume", "error"].includes(String((patch as any).enhanced_info_status || ""))
              ) {
                detailFailedIdsRef.current.add(targetId);
              }
              // Merge a patch into a candidate row, guarding phone against a
              // downgrade (an incoming patch must never replace a valid number
              // with an empty/invalid one).
              const applyPatch = (c: any) => {
                const merged = { ...c, ...patch };
                if (Object.prototype.hasOwnProperty.call(patch, "phone")) {
                  merged.phone = betterPhoneUI(c.phone, (patch as any).phone);
                }
                return merged;
              };
              // Update local runList copy used elsewhere in this run.
              for (let i = 0; i < runList.length; i++) {
                if (String(runList[i].candidate_id || runList[i].id || "") === targetId) {
                  runList[i] = applyPatch(runList[i]);
                  break;
                }
              }
              setCandidates(prev => deduplicateCandidatesUI(prev.map(c => (
                String(c.candidate_id || c.jobdiva_candidate_id || c.id || "") === targetId
                  ? applyPatch(c)
                  : c
              ))));
            } else if (event.type === "stage") {
              const rawStage = String(event.data || "");
              const mapped = mapStageToStatus(rawStage);
              if (mapped.toLowerCase().includes("portal")) {
                const portalPart = mapped.replace(/^Searching at\s*/i, "").replace(/\.\.\.$/, "").trim();
                if (portalPart) activePortal = portalPart;
              }
              setSearchStatus(mapped);
            } else if (event.type === "summary") {
              setSearchStatus(`Found ${runList.length} profiles. Finalizing shortlist and quality scoring...`);
              console.log("Search stream complete:", event.data);
              const summary = event.data?.summary || event.data || {};
              const unconfigured = Boolean(summary?.jobdiva_criteria_unconfigured);
              setJobdivaCriteriaUnconfigured(unconfigured);
              if (unconfigured) {
                // Pre-check may miss if JobDiva returns a non-standard error
                // shape. If the actual search summary confirms criteria are
                // unconfigured, still surface the recruiter guidance modal.
                setShowJobdivaSkillsModal(true);
                setSkillsCopied(false);
              }
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

  // Bucket key for persisted search results. Falls back to "draft" for jobs
  // that haven't been assigned a numeric/JobDiva id yet so wizard work survives
  // a reload before the first save.
  const sourcingResultsKey = `sourcing:results:${numericJobId || jobdivaId || "draft"}`;
  // Sourcing results expire after 24h: recruiters who come back the next day
  // should see a fresh search, not stale candidates from the prior session.
  const SOURCING_RESULTS_TTL_MS = 24 * 60 * 60 * 1000;

  // One-shot sweep on mount: drop any sourcing:results:* entries older than
  // the TTL across all jobs. Lazy per-key expiry (below) handles the active
  // job; this stops abandoned jobs from sitting in localStorage forever.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const now = Date.now();
      const stale: string[] = [];
      for (let i = 0; i < window.localStorage.length; i++) {
        const k = window.localStorage.key(i);
        if (!k || !k.startsWith("sourcing:results:")) continue;
        try {
          const parsed = JSON.parse(window.localStorage.getItem(k) || "null");
          const savedAt = Number(parsed?.savedAt);
          if (!savedAt || now - savedAt > SOURCING_RESULTS_TTL_MS) {
            stale.push(k);
          }
        } catch {
          stale.push(k);
        }
      }
      for (const k of stale) window.localStorage.removeItem(k);
    } catch {
      /* localStorage unavailable — ignore */
    }
  }, []);

  // Persist results once a search completes. Runs on the transition from
  // `isSearching: true → false` (and also when `candidates` changes while idle).
  // Skipped while streaming to avoid ~N writes per search.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (isSearching) return;
    if (!hasSearched) return;
    if (candidates.length === 0) return;
    try {
      const trimmed = candidates.slice(0, 100);
      window.localStorage.setItem(
        sourcingResultsKey,
        JSON.stringify({ candidates: trimmed, savedAt: Date.now() })
      );
    } catch {
      /* quota / unavailable — swallow, results remain in-memory */
    }
  }, [isSearching, hasSearched, candidates, sourcingResultsKey]);

  // Restore last-run results when the recruiter lands on Step 5 with nothing
  // in memory (e.g. after a reload). Gated to one-shot via the hasSearched
  // check. Clears `restoredFromCache` as soon as a fresh search starts.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (currentStep !== 5) return;
    if (hasSearched) return;
    if (candidates.length > 0) return;
    try {
      const raw = window.localStorage.getItem(sourcingResultsKey);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      const savedAt = Number(parsed?.savedAt);
      if (!savedAt || Date.now() - savedAt > SOURCING_RESULTS_TTL_MS) {
        window.localStorage.removeItem(sourcingResultsKey);
        return;
      }
      const cached = Array.isArray(parsed?.candidates) ? parsed.candidates : [];
      if (cached.length === 0) return;
      const dedupedCached = deduplicateCandidatesUI(cached);
      setCandidates(dedupedCached);
      setHasSearched(true);
      setRestoredFromCache(true);
      const seen = seenCandidateIdsRef.current;
      for (const c of dedupedCached) {
        const id = String(c?.candidate_id || c?.id || "");
        if (id) seen.add(id);
      }
    } catch {
      /* corrupt or unparseable cache — ignore */
    }
  }, [currentStep, sourcingResultsKey]);

  const handleRunSearch = async () => {
    const searchStartMs = Date.now();
    let accumulated: any[] = [];
    let runBreakdown: Array<Record<string, unknown>> = [];
    let currentAttempts: { query: string; label: string }[] = [];

    setIsSearching(true);
    setHasSearched(true);
    setRestoredFromCache(false);
    detailFailedIdsRef.current = new Set<string>();
    trackEvent("job_wizard_step5_candidate_search_started", {
      step: 5,
      query: truncateForTelemetry(resolvedGeneratedBoolean, 260),
      sources: Object.keys(searchSources).filter(k => (searchSources as any)[k]),
      recent_days: recentDaysFilter,
      include_no_resume: includeNoResume,
    });
    try {
      const initial = resolvedGeneratedBoolean;
      setGeneratedBoolean(initial);
      const attempts: { query: string; label: string }[] = [{ query: initial, label: "Hoonr-Curate generated" }];
      setBooleanAttempts(attempts);
      currentAttempts = attempts;
      setSearchStatus("Connecting to source portals...");

      const firstRunStartMs = Date.now();
      const firstRun = await runSearchStream(initial, "replace");
      accumulated = [...firstRun];
      const firstQuality = collectCandidateQualityStats(firstRun);
      runBreakdown = [
        {
          attempt: 1,
          label: "Hoonr-Curate generated",
          query: truncateForTelemetry(initial, 260),
          duration_seconds: Number(((Date.now() - firstRunStartMs) / 1000).toFixed(2)),
          results_count: firstRun.length,
          quality_tier_counts: firstQuality.quality_tier_counts,
          average_match_score: firstQuality.average_match_score,
        },
      ];

      while (currentAttempts.length < MAX_BOOLEAN_ATTEMPTS) {
        if (searchAbortRef.current?.signal.aborted) break;
        const qualified = countQualified(accumulated);
        if (qualified >= QUALIFIED_TARGET_COUNT) break;
        const tier = currentAttempts.length; // 1, 2, 3 as attempts grow
        const relaxed = relaxBooleanString(currentAttempts[currentAttempts.length - 1].query, tier);
        if (relaxed.query === currentAttempts[currentAttempts.length - 1].query) break;
        const structuralOverrides = relaxStructuralOverrides(
          tier,
          resumeMatchFilters,
          sourceTitles,
          sourceSkills,
          sourceCompanies
        );
        currentAttempts = [...currentAttempts, { query: relaxed.query, label: relaxed.label }];
        setBooleanAttempts(currentAttempts);
        setGeneratedBoolean(relaxed.query);
        setSearchStatus(`Only ${qualified}/${QUALIFIED_TARGET_COUNT} strong matches — relaxing boolean (attempt ${currentAttempts.length}/${MAX_BOOLEAN_ATTEMPTS})...`);

        const relaxedRunStartMs = Date.now();
        const nextRun = await runSearchStream(relaxed.query, "append", structuralOverrides);
        const relaxedQuality = collectCandidateQualityStats(nextRun);
        runBreakdown.push({
          attempt: currentAttempts.length,
          label: relaxed.label,
          query: truncateForTelemetry(relaxed.query, 260),
          duration_seconds: Number(((Date.now() - relaxedRunStartMs) / 1000).toFixed(2)),
          results_count: nextRun.length,
          quality_tier_counts: relaxedQuality.quality_tier_counts,
          average_match_score: relaxedQuality.average_match_score,
        });
        accumulated = [...accumulated, ...nextRun];
      }
    } catch (error) {
      console.error("Failed to search candidates:", error);
      trackEvent("job_wizard_step5_candidate_search_failed", {
        step: 5,
        error: truncateForTelemetry((error as Error)?.message || String(error)),
      });
    } finally {
      setIsSearching(false);
      const detailFailedCount = detailFailedIdsRef.current.size;
      if (detailFailedCount > 0) {
        showToast(
          `Couldn't score ${detailFailedCount} candidate${detailFailedCount === 1 ? "" : "s"} — JobDiva details were unavailable (e.g. rate limit / no résumé). They're shown as N/A and remain launchable.`,
          "info",
        );
      }
      const runtimeSeconds = Number(((Date.now() - searchStartMs) / 1000).toFixed(2));
      setLastSearchRuntimeSec(runtimeSeconds);
      setLastSearchRunsExecuted(runBreakdown.length || 1);
      const overallQuality = collectCandidateQualityStats(accumulated);
      trackEvent("job_wizard_step5_candidate_search_finished", {
        step: 5,
        candidates_found: accumulated.length,
        runtime_seconds: runtimeSeconds,
        runs_executed: runBreakdown.length,
        runs: runBreakdown,
        boolean_attempts: currentAttempts.map((attempt, idx) => ({
          attempt: idx + 1,
          label: attempt.label,
          query: truncateForTelemetry(attempt.query, 260),
        })),
        quality: overallQuality,
        ...buildStep5FilterContext(),
      });
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
    const runStartMs = Date.now();
    let runResults: any[] = [];
    trackEvent("job_wizard_step5_boolean_relaxed", {
      step: 5,
      previous_query: truncateForTelemetry(base, 220),
      relaxed_query: truncateForTelemetry(relaxed.query, 220),
      reason: relaxed.label,
      attempt: nextAttempts.length,
    });
    try {
      const structuralOverrides = relaxStructuralOverrides(
        tier,
        resumeMatchFilters,
        sourceTitles,
        sourceSkills,
        sourceCompanies
      );
      setSearchStatus(`Extending search with more lenient boolean (attempt ${nextAttempts.length}/${MAX_BOOLEAN_ATTEMPTS})...`);
      runResults = await runSearchStream(relaxed.query, "append", structuralOverrides);
    } finally {
      setIsSearching(false);
      const runtimeSeconds = Number(((Date.now() - runStartMs) / 1000).toFixed(2));
      setLastSearchRuntimeSec(runtimeSeconds);
      setLastSearchRunsExecuted(1);
      const runQuality = collectCandidateQualityStats(runResults);
      trackEvent("job_wizard_step5_boolean_relaxed_finished", {
        step: 5,
        attempt: nextAttempts.length,
        relaxed_query: truncateForTelemetry(relaxed.query, 260),
        reason: relaxed.label,
        runtime_seconds: runtimeSeconds,
        results_count: runResults.length,
        quality: runQuality,
        boolean_attempts: nextAttempts.map((attempt, idx) => ({
          attempt: idx + 1,
          label: attempt.label,
          query: truncateForTelemetry(attempt.query, 260),
        })),
        ...buildStep5FilterContext(),
      });
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
    trackEvent("job_wizard_step4_screen_question_added", {
      step: 4,
      question_id: newQuestion.id,
      total_questions: screenQuestions.length + 1,
    });
  };

  const updateScreenQuestion = (id: number, field: keyof ScreenQuestion, value: any) => {
    userHasEditedQuestionsRef.current = true;
    setScreenQuestions(prev => prev.map(q => {
      if (q.id === id) {
        return { ...q, [field]: value };
      }
      return q;
    }));
    trackEvent("job_wizard_step4_screen_question_changed", {
      step: 4,
      question_id: id,
      field,
      value: truncateForTelemetry(value, 180),
    });
  };

  const deleteScreenQuestion = (id: number) => {
    userHasEditedQuestionsRef.current = true;
    setScreenQuestions(prev => prev.filter(q => q.id !== id));
    trackEvent("job_wizard_step4_screen_question_removed", {
      step: 4,
      question_id: id,
    });
  };

  // Reorder among the non-default block (questions 8+). Default rows stay
  // pinned at the top in their generated order — recruiters move only the
  // role-specific + custom rows below them.
  const moveScreenQuestion = (from: number, to: number) => {
    setScreenQuestions(prev => {
      if (from === to || from < 0 || to < 0) return prev;
      if (from >= prev.length || to >= prev.length) return prev;
      const next = [...prev];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next.map((q, i) => ({ ...q, order_index: i }));
    });
    userHasEditedQuestionsRef.current = true;
    trackEvent("job_wizard_step4_screen_question_reordered", {
      step: 4,
      from_index: from,
      to_index: to,
    });
  };

  const questionsDrag = useDragReorder((from, to) => moveScreenQuestion(from, to));

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
            <span className="text-[12px] font-normal text-slate-500">Hard filters applied during resume matching</span>
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
              <span className="text-[11px] text-slate-400 font-normal">— what Alex says at the start of each call. Variables in {"{{brackets}}"} are filled at runtime.</span>
            </div>
            <textarea
              value={botIntroduction}
              onChange={(e) => setBotIntroduction(e.target.value)}
              onBlur={(e) => {
                trackEvent("job_wizard_step4_bot_introduction_saved", {
                  step: 4,
                  length: e.target.value.length,
                  preview: truncateForTelemetry(e.target.value, 220),
                });
              }}
              className="w-full bg-transparent border-none outline-none text-[13px] text-slate-600 leading-relaxed resize-none h-24"
              placeholder="Enter bot introduction..."
            />
          </div>

          {/* Questions Table */}
          <div className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-2">
            <div className="w-5 flex-shrink-0"></div>
            <div className="w-8 flex-shrink-0">#</div>
            <div className="flex-1">Question</div>
            <div className="flex-1">Pass Criteria <span className="text-[10px] font-normal lowercase">(blank = informational only)</span></div>
            <div className="w-10 flex-shrink-0"></div>
          </div>

          {screenQuestions.map((q, index) => {
            return (
            <div
              key={q.id}
              className="flex items-start gap-3 py-3 border-b border-slate-100 last:border-b-0 group"
              onDragOver={questionsDrag.onDragOver}
              onDrop={questionsDrag.onDrop(index)}
              onDragEnd={questionsDrag.onDragEnd}
            >
              <button
                type="button"
                draggable
                onDragStart={questionsDrag.onDragStart(index)}
                onDragEnd={questionsDrag.onDragEnd}
                className="w-5 flex-shrink-0 flex items-center justify-center text-slate-300 hover:text-slate-600 cursor-grab active:cursor-grabbing mt-1.5"
                title="Drag to reorder"
                aria-label="Drag to reorder question"
              >
                <GripVertical className="w-4 h-4" />
              </button>
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
                  className="w-full text-[13px] bg-transparent border-none outline-none text-slate-900 font-medium resize-none whitespace-pre-wrap break-words"
                  rows={3}
                />
              </div>

              <div className="flex-1 min-w-0 border-l border-slate-100 pl-3">
                <textarea
                  value={q.pass_criteria}
                  onChange={(e) => updateScreenQuestion(q.id, 'pass_criteria', e.target.value)}
                  rows={2}
                  className={`w-full text-[13px] bg-transparent border-none outline-none font-medium resize-none whitespace-pre-wrap break-words ${q.pass_criteria ? 'text-[#4f46e5]' : 'text-slate-300 italic'}`}
                  placeholder="No hard filter"
                />
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
            );
          })}

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

  // Launch PAIR consumes selected candidates (with optional contact overrides
  // from enrichment) and persists them to sourced_candidates.
  //
  // The full selection is split into batches of LAUNCH_BATCH_SIZE before
  // hitting /candidates/save + the engagement endpoints — large bulk
  // payloads (hundreds of resumes + full enhanced_info blobs) were
  // timing out / OOMing the backend. Each batch's save/engage status is
  // streamed into launchProgress so the modal can show realtime state.
  const runLaunchPair = async (
    contactOverrides?: Record<string, { phone?: string; email?: string }>,
    launchIdsOverride?: Set<string>,
    options?: { skipRedirect?: boolean },
  ): Promise<{ success: boolean; savedCount: number }> => {
    const launchIds = launchIdsOverride ?? selectedCandidates;
    if (launchIds.size === 0) return { success: false, savedCount: 0 };

    const effective = contactOverrides
      ? candidates.map(c => {
        const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
        const override = contactOverrides[id];
        return override
          ? {
            ...c,
            phone: override.phone || c.phone,
            email: override.email || c.email,
          }
          : c;
      })
      : candidates;

    if (contactOverrides) {
      setCandidates(effective);
    }

    // Final DNC safety net: drop any selected candidate whose normalized
    // phone is on the DNC list, even if React state hasn't yet flushed
    // the auto-deselect from handleLaunchPairClick. The backend repeats
    // this check at /candidates/save — defense in depth.
    const candidatesPayload = effective
      .filter(c => launchIds.has(c.candidate_id || c.jobdiva_candidate_id || c.id))
      // Hard filter fail safety net: a *genuine* 0% candidate (hard-veto /
      // exclusion) must never reach /candidates/save, even via the second
      // MissingContactsModal pass. Candidates we couldn't score (detail_failed
      // / unscored → N/A) have no numeric score and are kept launchable.
      .filter(c => !isHardFilterZero(c))
      .filter(c => {
        if (dncPhones.size === 0) return true;
        const np = normalizePhone(c.phone);
        return !(np && dncPhones.has(np));
      })
      .map(c => {
        const displayName = getCandidateDisplayName(c);
        // Send the SAME contact the launch gate used to mark this candidate
        // launchable. getCandidateLaunchEmail/Phone read nested fields
        // (workEmail/personalEmail/enhanced_info.*/data.*/zoominfo_contact_enrichment.*);
        // sending only top-level c.email/c.phone dropped nested-only contact to
        // null, which the backend then rejected — failing the whole batch with a
        // 400. Guard with the same validity checks so we never persist a
        // synthetic/placeholder address.
        const launchEmail = getCandidateLaunchEmail(c);
        const launchPhone = getCandidateLaunchPhone(c);
        let skillList: any[] = [];
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
          candidate_id: String(c.candidate_id || c.jobdiva_candidate_id || c.id || "unknown"),
          name: displayName || "Unnamed Candidate",
          email: isValidLaunchEmail(launchEmail) ? launchEmail : null,
          phone: isValidLaunchPhone(launchPhone) ? launchPhone : null,
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
          // Send null (not 0) when unscored so the backend re-scores at save
          // instead of locking a placeholder 0% into the rank list.
          match_score: typeof c.match_score === 'number' ? c.match_score : null,
          detail_failed: !!c.detail_failed,
          matched_skills: Array.isArray(c.matched_skills) ? c.matched_skills : [],
          missing_skills: Array.isArray(c.missing_skills) ? c.missing_skills : [],
          match_score_details: (c.match_score_details && typeof c.match_score_details === 'object' && !Array.isArray(c.match_score_details)) ? c.match_score_details : {},
          explainability: Array.isArray(c.explainability) ? c.explainability : [],
          enhanced_info: (c.enhanced_info && typeof c.enhanced_info === 'object' && !Array.isArray(c.enhanced_info)) ? c.enhanced_info : null
        };
      });

    if (candidatesPayload.length === 0) {
      return { success: false, savedCount: 0 };
    }

    const jobdivaIdForSave = jobdivaId || jobData?.jobdiva_id || numericJobId;
    const jobIdForEngage = (jobdivaId || jobData?.jobdiva_id || numericJobId || "").toString().trim();

    // Slice into fixed-size batches; the modal already shows per-batch progress.
    const batches: typeof candidatesPayload[] = [];
    for (let i = 0; i < candidatesPayload.length; i += LAUNCH_BATCH_SIZE) {
      batches.push(candidatesPayload.slice(i, i + LAUNCH_BATCH_SIZE));
    }

    const initialBatchInfo: LaunchBatchInfo[] = batches.map((batch, idx) => ({
      index: idx,
      size: batch.length,
      status: "pending",
      savedCount: 0,
      dncSkipped: 0,
      engageSent: 0,
      alreadySent: 0,
    }));

    setLaunchProgress(prev => ({
      ...prev,
      open: true,
      phase: "launching",
      totalCandidates: candidatesPayload.length,
      batchSize: LAUNCH_BATCH_SIZE,
      batches: initialBatchInfo,
      currentBatchIndex: 0,
      totalSaved: 0,
      totalEngaged: 0,
      totalFailedBatches: 0,
      failedCandidates: [],
      jobIdForRelaunch: jobdivaIdForSave ? String(jobdivaIdForSave) : undefined,
    }));

    const updateBatch = (idx: number, patch: Partial<LaunchBatchInfo>) => {
      setLaunchProgress(prev => ({
        ...prev,
        batches: prev.batches.map(b => (b.index === idx ? { ...b, ...patch } : b)),
      }));
    };

    // Collect candidates from any batch that fails (save or engage) so the
    // modal can offer a CSV export for manual re-launch via the API.
    const failedLaunchCandidates: LaunchFailedCandidate[] = [];
    const recordFailedBatch = (
      batch: typeof candidatesPayload[number][],
      stage: "save" | "engage",
      errorMessage: string,
      batchIndex: number,
    ) => {
      for (const c of batch) {
        const skillNames = Array.isArray(c.skills)
          ? c.skills
              .map((s: any) =>
                typeof s === "string" ? s : s?.name || s?.skill || "",
              )
              .filter(Boolean)
              .join("; ")
          : "";
        failedLaunchCandidates.push({
          candidate_id: c.candidate_id,
          name: c.name,
          email: c.email,
          phone: c.phone,
          source: c.source,
          headline: c.headline,
          location: c.location,
          experience_years: c.experience_years,
          match_score: c.match_score,
          skills: skillNames,
          matched_skills: Array.isArray(c.matched_skills)
            ? c.matched_skills.join("; ")
            : "",
          resume_id: c.resume_id,
          profile_url: c.profile_url,
          batch_index: batchIndex,
          failure_stage: stage,
          error_message: errorMessage,
        });
      }
    };

    console.log(`🚀 Launching Hoonr-Curate with ${candidatesPayload.length} candidates in ${batches.length} batch(es) of ${LAUNCH_BATCH_SIZE}`);

    let totalSaved = 0;
    let totalEngaged = 0;
    let totalDncSkipped = 0;
    let totalFailedBatches = 0;
    let engageFailureMessage: string | null = null;
    let skippedCandidateNames: string[] = [];

    for (let i = 0; i < batches.length; i++) {
      const batch = batches[i];
      const batchIds = batch.map(c => c.candidate_id);

      setLaunchProgress(prev => ({ ...prev, currentBatchIndex: i }));
      updateBatch(i, { status: "saving" });

      // ── Save batch ────────────────────────────────────────────────────
      let saveOk = false;
      let batchSavedCount = 0;
      let batchDncSkipped = 0;
      try {
        const response = await fetch(`${API_BASE}/candidates/save`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            jobdiva_id: jobdivaIdForSave,
            candidates: batch,
          }),
        });
        const result = await response.json();
        if (response.ok && result.status === 'success') {
          saveOk = true;
          batchSavedCount = Number(result.saved_count) || batch.length;
          batchDncSkipped = Number(result?.dnc_skipped_count || 0);
        } else {
          console.error(`Batch ${i + 1} save failed:`, JSON.stringify(result, null, 2));
          const errorMsg = result.detail
            ? (Array.isArray(result.detail) ? JSON.stringify(result.detail) : result.detail)
            : (result.message || 'Unknown error');
          updateBatch(i, { status: "failed", errorMessage: `Save failed: ${errorMsg}` });
          totalFailedBatches += 1;
          recordFailedBatch(batch, "save", String(errorMsg), i);
        }
      } catch (e) {
        console.error(`Batch ${i + 1} save threw:`, e);
        const errMsg = e instanceof Error ? e.message : "Unknown error";
        updateBatch(i, {
          status: "failed",
          errorMessage: `Save failed: ${errMsg}`,
        });
        totalFailedBatches += 1;
        recordFailedBatch(batch, "save", errMsg, i);
      }

      if (!saveOk) {
        continue; // skip engage for this batch; move on
      }

      totalSaved += batchSavedCount;
      totalDncSkipped += batchDncSkipped;
      updateBatch(i, {
        status: "engaging",
        savedCount: batchSavedCount,
        dncSkipped: batchDncSkipped,
      });

      // ── Engage batch ─────────────────────────────────────────────────
      let batchEngageSent = 0;
      let batchAlreadySent = 0;
      let batchEngageError: string | null = null;
      try {
        const engageData = await engagement.generatePayload({
          candidateIds: batchIds,
          jobId: jobIdForEngage,
        });
        if (engageData?.payload) {
          const engageRes = await engagement.sendBulkInterview({
            payload: engageData.payload,
            realCandidateIds: batchIds,
            isInitialLaunch: wizardMode !== 'source',
            notifyRecruiters: true,
            sendJobPostingEmail: i === batches.length - 1 && totalFailedBatches === 0,
          });
          if (engageRes.success) {
            batchEngageSent = Array.isArray(engageRes.data) ? engageRes.data.length : batchIds.length;
            batchAlreadySent = Array.isArray(engageRes.skipped_already_sent)
              ? engageRes.skipped_already_sent.length
              : 0;

            if (Array.isArray(engageRes.skipped_already_sent) && engageRes.skipped_already_sent.length > 0) {
              const skippedNames = engageRes.skipped_already_sent.map((id: string) => {
                const c = candidates.find(cand => (cand.candidate_id || cand.id) === id);
                if (c) {
                  return c.name || [c.firstName, c.lastName].filter(Boolean).join(" ") || c.email || id;
                }
                return id;
              }).filter(Boolean);
              skippedCandidateNames.push(...skippedNames);
            }
            
            if (engageRes.bulk_id && batchEngageSent > 0) {
              updateBatch(i, { status: "engaging", message: "Waiting for background processing..." });
              try {
                await new Promise<void>((resolve, reject) => {
                  const eventSource = new EventSource(`${API_BASE}/api/v1/engagement/engage/bulk-status/stream?bulk_id=${engageRes.bulk_id}`);
                  eventSource.onmessage = (event) => {
                    try {
                      const data = JSON.parse(event.data);
                      if (data.status === "completed") {
                        eventSource.close();
                        resolve();
                      } else if (data.status === "error") {
                        eventSource.close();
                        reject(new Error(data.message || "Background processing failed"));
                      } else if (data.status === "processing") {
                        updateBatch(i, { status: "engaging", message: `Processing ${data.pending} candidates...` });
                      }
                    } catch (e) {
                      eventSource.close();
                      reject(e);
                    }
                  };
                  eventSource.onerror = (error) => {
                    eventSource.close();
                    reject(new Error("Lost connection to background processing status stream"));
                  };
                });
              } catch (streamErr) {
                console.warn(`Batch ${i + 1} SSE stream error:`, streamErr);
              }
            }
          } else {
            batchEngageError = engageRes.message || "PAIR rejected the batch";
          }
        } else {
          batchEngageError = "Engagement payload missing";
        }
      } catch (engageErr) {
        batchEngageError = engageErr instanceof Error ? engageErr.message : "Engagement call failed";
        console.warn(`Batch ${i + 1} engage failed:`, engageErr);
      }

      if (batchEngageError) {
        updateBatch(i, {
          status: "failed",
          engageSent: batchEngageSent,
          alreadySent: batchAlreadySent,
          errorMessage: `Engage failed: ${batchEngageError}`,
        });
        totalFailedBatches += 1;
        engageFailureMessage = batchEngageError;
        recordFailedBatch(batch, "engage", batchEngageError, i);
      } else {
        totalEngaged += batchEngageSent;
        updateBatch(i, {
          status: "completed",
          engageSent: batchEngageSent,
          alreadySent: batchAlreadySent,
        });
      }

      setLaunchProgress(prev => ({
        ...prev,
        totalSaved,
        totalEngaged,
        totalFailedBatches,
      }));
    }

    if (skippedCandidateNames.length > 0) {
      showToast(
        `Skipped ${skippedCandidateNames.join(", ")} (Already Launched)`,
        "info"
      );
    }

    if (totalDncSkipped > 0) {
      showToast(
        `${totalDncSkipped} candidate${totalDncSkipped === 1 ? "" : "s"} blocked at save (Do Not Contact)`,
        "info",
      );
    }

    const success = totalSaved > 0 && totalFailedBatches === 0;
    const partial = totalSaved > 0 && totalFailedBatches > 0;

    if (success) {
      showToast(`Launched PAIR for ${totalSaved} candidate${totalSaved === 1 ? "" : "s"}`, "success");
    } else if (partial) {
      showToast(
        `Launched ${totalSaved} · ${totalFailedBatches} batch${totalFailedBatches === 1 ? "" : "es"} failed${engageFailureMessage ? ` (${engageFailureMessage})` : ""}`,
        "info",
      );
    } else {
      showToast(
        `Launch PAIR failed${engageFailureMessage ? `: ${engageFailureMessage}` : ""}`,
        "error",
      );
    }

    setLaunchProgress(prev => ({
      ...prev,
      phase: totalFailedBatches === 0 ? "completed" : (totalSaved > 0 ? "completed" : "failed"),
      totalSaved,
      totalEngaged,
      totalFailedBatches,
      failedCandidates: failedLaunchCandidates,
      finalMessage: engageFailureMessage ?? undefined,
    }));

    // After a successful launch, redirect to this job's rank list. The
    // just-launched candidates now live there; to launch PAIR for the
    // remaining candidates the recruiter re-opens the job in source mode
    // ("Source Candidates"), where already-launched people are filtered out
    // of Step 5 so only the remaining ones can be selected and launched.
    if (totalSaved > 0 && !options?.skipRedirect) {
      setTimeout(() => {
        setLaunchProgress(initialLaunchProgress);
        if (jobIdForEngage) {
          router.push(`/jobs/${encodeURIComponent(jobIdForEngage)}/rankings`);
        } else {
          router.push(`/`);
        }
      }, 1500);
    }

    return { success: totalSaved > 0, savedCount: totalSaved };
  };

  // Entry point wired to Launch PAIR. Before save, auto-enrich selected
  // candidates missing phone via ZoomInfo using LinkedIn URL.
  const handleLaunchPairClick = async () => {
    if (!hasSearched) {
      showToast("Run Search first to source candidates.", "info");
      return;
    }
    if (selectedCandidates.size === 0) {
      showToast("Select at least one candidate before launching PAIR.", "info");
      return;
    }

    // Hard filter fail: candidates with a genuine numeric 0% (hard-veto /
    // exclusion) are never launched, even when selected. Candidates we couldn't
    // score (detail_failed / unscored → N/A) are NOT skipped here. Compute the
    // skip set once and thread it through the flow below WITHOUT touching the
    // table selection — they are simply excluded from the launch payload and
    // reported on the completion screen.
    const hardFilterSkipIds = new Set<string>();
    const hardFilterSkippedNames: string[] = [];
    for (const c of candidates) {
      const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
      if (!id || !selectedCandidates.has(id)) continue;
      if (isHardFilterZero(c)) {
        hardFilterSkipIds.add(id);
        hardFilterSkippedNames.push(getCandidateDisplayName(c) || c.name || "Unnamed");
      }
    }

    trackEvent("job_wizard_step5_launch_pair_clicked", {
      step: 5,
      selected_candidates_count: selectedCandidates.size,
      hard_filter_skipped: hardFilterSkipIds.size,
    });

    // Persist the latest Step-5 sourcing state before launching so titles /
    // skills / filter tweaks made just-now survive the launch round-trip.
    await saveJobDraft({ currentStep: 5, saveType: "auto", skipToast: true });

    setIsEnrichingContacts(true);
    try {
      if (IS_QA_CURATE && qaOverrideEnabled) {
        // QA mode with Override toggle ON: skip ZoomInfo auto-enrichment and
        // the immediate launch path. Open the contact modal for EVERY selected
        // candidate so QA can review and override mobile / email before
        // anything fires. With Override OFF, fall through to the production
        // path below (auto-enrich + launch for everyone).
        const reviewList: MissingContactCandidate[] = [];
        const launchJobdivaId = jobdivaId || jobData?.jobdiva_id || numericJobId || undefined;
        for (const c of candidates) {
          const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
          if (!id || !selectedCandidates.has(id) || hardFilterSkipIds.has(id)) continue;
          const currentPhone = getCandidateLaunchPhone(c);
          const currentEmail = getCandidateLaunchEmail(c);
          reviewList.push({
            candidate_id: id,
            name: getCandidateDisplayName(c) || c.name || "Unnamed",
            headline: c.title || c.headline || "",
            location: c.location || "",
            source: c.source || "",
            jobdiva_id: launchJobdivaId ? String(launchJobdivaId) : undefined,
            needsPhone: true,
            needsEmail: true,
            currentPhone,
            currentEmail,
          });
        }
        if (reviewList.length === 0) {
          showToast("No candidates available to launch.", "info");
          return;
        }
        setPendingLaunchOverrides({});
        setMissingContactCandidates(reviewList);
        setMissingContactsReviewMode(true);
        setMissingContactsOpen(true);
        return;
      }

      const candidatesMissingContact = candidates.filter(c => {
        const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
        if (!selectedCandidates.has(id)) return false;
        if (hardFilterSkipIds.has(String(id || "").trim())) return false;
        const phone = getCandidateLaunchPhone(c);
        const email = getCandidateLaunchEmail(c);
        return !isValidLaunchPhone(phone) || !isValidLaunchEmail(email);
      });

      // Open the progress modal upfront so the recruiter sees enrichment
      // streaming. runLaunchPair will flip phase to "launching" and fill in
      // the per-batch list once enrichment is done.
      setLaunchProgress({
        ...initialLaunchProgress,
        open: true,
        phase: candidatesMissingContact.length > 0 ? "enriching" : "launching",
        totalCandidates: Math.max(0, selectedCandidates.size - hardFilterSkipIds.size),
        batchSize: LAUNCH_BATCH_SIZE,
        enrichTotal: candidatesMissingContact.length,
        hardFilterSkipped: hardFilterSkipIds.size,
        hardFilterSkippedNames,
      });

      const contactOverrides: Record<string, { phone?: string; email?: string }> = {};
      let enrichedCount = 0;
      let enrichedMobileCount = 0;
      let enrichedWorkPhoneCount = 0;
      let missingLinkedInCount = 0;
      let enrichFailedCount = 0;
      let noContactFoundCount = 0;

      const enrichOne = async (c: (typeof candidatesMissingContact)[number]) => {
        const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
        if (!id) return;

        const linkedinUrlCandidates = [
          c.profile_url,
          c.linkedin_url,
          c.urls?.linkedin,
          c.urls?.linkedin_url,
          c.data?.urls?.linkedin,
          c.data?.urls?.linkedin_url,
          extractLinkedInFromText(c.resume_text || c.resumeText || c.data?.resume_text),
        ].map((v: any) => String(v || "").trim()).filter(Boolean);

        const linkedinUrl = linkedinUrlCandidates.find((u: string) => looksLikeLinkedInProfile(u)) || "";

        // Is the candidate ALREADY reachable on their existing contact? PAIR
        // launches on phone OR email, so a candidate who already has one is
        // launchable even if enrichment adds nothing new — count them as
        // "already reachable", never as a "missing LinkedIn"/"no contact" miss.
        const launchableNow =
          isValidLaunchPhone(getCandidateLaunchPhone(c)) ||
          isValidLaunchEmail(getCandidateLaunchEmail(c));

        if (!linkedinUrl) {
          if (launchableNow) {
            setLaunchProgress(prev => ({
              ...prev,
              enrichDone: prev.enrichDone + 1,
              enrichAlreadyReachable: prev.enrichAlreadyReachable + 1,
            }));
          } else {
            missingLinkedInCount += 1;
            setLaunchProgress(prev => ({
              ...prev,
              enrichDone: prev.enrichDone + 1,
              enrichMissingLinkedIn: prev.enrichMissingLinkedIn + 1,
            }));
          }
          return;
        }

        try {
          const res = await fetch(`${API_BASE}/candidates/enrich-contact`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              candidate_id: id,
              jobdiva_id: jobdivaId || jobData?.jobdiva_id || numericJobId || undefined,
              source: c.source || undefined,
              linkedin_url: linkedinUrl,
            }),
          });

          if (!res.ok) {
            enrichFailedCount += 1;
            setLaunchProgress(prev => ({
              ...prev,
              enrichDone: prev.enrichDone + 1,
              enrichFailed: prev.enrichFailed + 1,
            }));
            return;
          }
          const enriched = await res.json();
          const nextPhone = enriched?.phone || enriched?.mobilePhone || enriched?.workPhone || "";
          const nextEmail = enriched?.email || "";
          const phoneSource = String(enriched?.phone_source || "").trim();
          if (nextPhone || nextEmail) {
            contactOverrides[id] = {
              phone: nextPhone || undefined,
              email: nextEmail || undefined,
            };
            enrichedCount += 1;
            if (phoneSource === "mobilePhone") {
              enrichedMobileCount += 1;
            } else if (phoneSource === "workPhone") {
              enrichedWorkPhoneCount += 1;
            }
            setLaunchProgress(prev => ({
              ...prev,
              enrichDone: prev.enrichDone + 1,
              enrichSucceeded: prev.enrichSucceeded + 1,
            }));
          } else if (launchableNow) {
            // Enrichment found nothing new, but the candidate already has a
            // usable phone or email — still launchable, not a miss.
            setLaunchProgress(prev => ({
              ...prev,
              enrichDone: prev.enrichDone + 1,
              enrichAlreadyReachable: prev.enrichAlreadyReachable + 1,
            }));
          } else {
            noContactFoundCount += 1;
            setLaunchProgress(prev => ({
              ...prev,
              enrichDone: prev.enrichDone + 1,
              enrichNoContact: prev.enrichNoContact + 1,
            }));
          }
        } catch {
          // Best-effort enrichment; keep launch flow moving.
          enrichFailedCount += 1;
          setLaunchProgress(prev => ({
            ...prev,
            enrichDone: prev.enrichDone + 1,
            enrichFailed: prev.enrichFailed + 1,
          }));
        }
      };

      // Bounded concurrency pool instead of one-at-a-time. "Missing LinkedIn"
      // candidates resolve instantly; the win is overlapping the network-bound
      // calls. Counters/contactOverrides mutate synchronously between awaits
      // (atomic in single-threaded JS) and setLaunchProgress uses the functional
      // updater, so concurrent updates are safe. Pool drains via a shared cursor.
      let enrichCursor = 0;
      const enrichWorker = async () => {
        while (enrichCursor < candidatesMissingContact.length) {
          await enrichOne(candidatesMissingContact[enrichCursor++]);
        }
      };
      await Promise.all(
        Array.from(
          { length: Math.min(LAUNCH_ENRICH_CONCURRENCY, candidatesMissingContact.length) },
          enrichWorker,
        ),
      );

      if (enrichedCount > 0) {
        setCandidates(prev => prev.map(c => {
          const cid = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
          const override = contactOverrides[cid];
          if (!override) return c;
          return {
            ...c,
            phone: override.phone || c.phone,
            email: override.email || c.email,
          };
        }));
        const parts = [
          `${enrichedCount} candidate${enrichedCount === 1 ? "" : "s"}`,
          enrichedMobileCount > 0 ? `${enrichedMobileCount} mobile` : "",
          enrichedWorkPhoneCount > 0 ? `${enrichedWorkPhoneCount} work phone` : "",
        ].filter(Boolean);
        showToast(`ZoomInfo enriched: ${parts.join(" · ")}.`, "success");
      }

      const unresolvedMissing = candidatesMissingContact.filter(c => {
        const cid = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
        const overridePhone = contactOverrides[cid]?.phone || getCandidateLaunchPhone(c);
        const overrideEmail = contactOverrides[cid]?.email || getCandidateLaunchEmail(c);
        // Launchable with EITHER a phone or an email — only count candidates
        // left with neither after enrichment.
        return !isValidLaunchPhone(overridePhone) && !isValidLaunchEmail(overrideEmail);
      }).length;

      if (unresolvedMissing > 0) {
        showToast(`${unresolvedMissing} selected candidate${unresolvedMissing === 1 ? "" : "s"} still missing both phone and email after enrichment.`, "info");
      }

      if (missingLinkedInCount > 0 || enrichFailedCount > 0 || noContactFoundCount > 0) {
        const bits = [
          missingLinkedInCount > 0 ? `${missingLinkedInCount} missing LinkedIn URL` : "",
          noContactFoundCount > 0 ? `${noContactFoundCount} still missing phone & email` : "",
          enrichFailedCount > 0 ? `${enrichFailedCount} enrichment call failed` : "",
        ].filter(Boolean);
        showToast(`Enrichment summary: ${bits.join(" · ")}`, "info");
      }

      // DNC re-check after enrichment: a candidate without a phone in search
      // results may now have one via ZoomInfo, and that phone may match the
      // DNC list. Auto-deselect any matches and surface a toast so the user
      // sees why the count dropped before the POST fires.
      const dncDropped = new Set<string>();
      if (dncPhones.size > 0) {
        for (const c of candidates) {
          const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
          if (!id || !selectedCandidates.has(id)) continue;
          const overridePhone = contactOverrides[id]?.phone;
          const phoneToCheck = overridePhone || c.phone;
          const np = normalizePhone(phoneToCheck);
          if (np && dncPhones.has(np)) {
            dncDropped.add(id);
          }
        }
        if (dncDropped.size > 0) {
          setSelectedCandidates(prev => {
            const next = new Set(prev);
            for (const id of dncDropped) next.delete(id);
            return next;
          });
          showToast(
            `${dncDropped.size} candidate${dncDropped.size === 1 ? "" : "s"} skipped — Do Not Contact list match`,
            "info",
          );
        }
      }

      // Partition selected candidates into Ready (has a usable phone OR a real
      // email) and Needs-info (missing both). Ready candidates launch
      // immediately; the Needs-info group opens MissingContactsModal so the
      // recruiter can fill in details and launch them in a second pass.
      const readyIds = new Set<string>();
      const needsInfo: MissingContactCandidate[] = [];
      const launchJobdivaId = jobdivaId || jobData?.jobdiva_id || numericJobId || undefined;
      const emailToCandidateIds = new Map<string, string[]>();
      const phoneToCandidateIds = new Map<string, string[]>();
      for (const c of candidates) {
        const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
        if (!id || !selectedCandidates.has(id) || dncDropped.has(id) || hardFilterSkipIds.has(id)) continue;
        const overrideEmail = contactOverrides[id]?.email;
        const overridePhone = contactOverrides[id]?.phone;
        const effectiveEmail = String(overrideEmail || getCandidateLaunchEmail(c)).trim().toLowerCase();
        const effectivePhone = overridePhone || getCandidateLaunchPhone(c);
        if (isValidLaunchEmail(effectiveEmail)) {
          const ids = emailToCandidateIds.get(effectiveEmail) || [];
          ids.push(id);
          emailToCandidateIds.set(effectiveEmail, ids);
        }
        if (isValidLaunchPhone(effectivePhone)) {
          const normalizedPhone = launchPhoneDigits(effectivePhone);
          const ids = phoneToCandidateIds.get(normalizedPhone) || [];
          ids.push(id);
          phoneToCandidateIds.set(normalizedPhone, ids);
        }
      }
      const duplicateEmailIds = new Set<string>();
      const duplicatePhoneIds = new Set<string>();
      for (const ids of emailToCandidateIds.values()) {
        if (ids.length < 2) continue;
        for (const id of ids) duplicateEmailIds.add(id);
      }
      for (const ids of phoneToCandidateIds.values()) {
        if (ids.length < 2) continue;
        for (const id of ids) duplicatePhoneIds.add(id);
      }
      for (const c of candidates) {
        const id = String(c.candidate_id || c.jobdiva_candidate_id || c.id || "").trim();
        if (!id || !selectedCandidates.has(id) || dncDropped.has(id) || hardFilterSkipIds.has(id)) continue;
        const overridePhone = contactOverrides[id]?.phone;
        const overrideEmail = contactOverrides[id]?.email;
        const effectivePhone = overridePhone || getCandidateLaunchPhone(c);
        const effectiveEmail = String(overrideEmail || getCandidateLaunchEmail(c)).trim().toLowerCase();
        const phoneOK = isValidLaunchPhone(effectivePhone) && !duplicatePhoneIds.has(id);
        const emailOK = isValidLaunchEmail(effectiveEmail) && !duplicateEmailIds.has(id);
        // PAIR can reach a candidate by phone OR email, so EITHER one is enough
        // to launch. A candidate with just a phone, or just an email, is
        // launchable; only those missing both get routed to the contact modal.
        if (phoneOK || emailOK) {
          readyIds.add(id);
        } else {
          needsInfo.push({
            candidate_id: id,
            name: getCandidateDisplayName(c) || c.name || "Unnamed",
            headline: c.title || c.headline || "",
            location: c.location || "",
            source: c.source || "",
            jobdiva_id: launchJobdivaId ? String(launchJobdivaId) : undefined,
            needsPhone: !phoneOK,
            needsEmail: !emailOK,
            currentPhone: effectivePhone || "",
            currentEmail: effectiveEmail || "",
          });
        }
      }

      if (duplicateEmailIds.size > 0) {
        showToast(
          `${duplicateEmailIds.size} selected candidate${duplicateEmailIds.size === 1 ? "" : "s"} share duplicate email addresses. PAIR needs a unique real email per candidate.`,
          "info",
        );
      }
      if (duplicatePhoneIds.size > 0) {
        showToast(
          `${duplicatePhoneIds.size} selected candidate${duplicatePhoneIds.size === 1 ? "" : "s"} share duplicate phone numbers. PAIR needs a unique real phone per candidate.`,
          "info",
        );
      }

      const hasReady = readyIds.size > 0;
      const hasNeeds = needsInfo.length > 0;

      if (hasReady) {
        const result = await runLaunchPair(contactOverrides, readyIds, {
          skipRedirect: hasNeeds,
        });
        setReadyLaunchedPendingRedirect(hasNeeds && result.success);
        // If a needs-info pass follows, hand the screen off to
        // MissingContactsModal — close the progress modal so the recruiter
        // can fill in details for the rest. A second runLaunchPair call
        // will re-open it.
        if (hasNeeds) {
          setLaunchProgress(initialLaunchProgress);
        }
      } else if (!hasNeeds && hardFilterSkipIds.size > 0) {
        setReadyLaunchedPendingRedirect(false);
        // Nothing to launch, but the only reason is hard-filter skips. Keep the
        // progress modal open in a completed state so the recruiter sees which
        // candidates were skipped (and why) instead of a bare toast.
        setLaunchProgress(prev => ({
          ...prev,
          open: true,
          phase: "completed",
          totalCandidates: 0,
          enrichTotal: 0,
          batches: [],
          finalMessage: "No candidates launched — all selected candidates failed the hard filter (0% match).",
        }));
      } else {
        setReadyLaunchedPendingRedirect(false);
        // No ready candidates: nothing for runLaunchPair to do, so drop
        // the progress modal we opened upfront.
        setLaunchProgress(initialLaunchProgress);
      }

      if (hasNeeds) {
        setPendingLaunchOverrides(contactOverrides);
        setMissingContactCandidates(needsInfo);
        setMissingContactsReviewMode(false);
        setMissingContactsOpen(true);
      } else if (!hasReady && hardFilterSkipIds.size > 0) {
        showToast(
          `${hardFilterSkipIds.size} candidate${hardFilterSkipIds.size === 1 ? "" : "s"} skipped — hard filter failed (0% match).`,
          "info",
        );
      } else if (!hasReady) {
        showToast("No candidates available to launch.", "info");
      }
    } finally {
      setIsEnrichingContacts(false);
    }
  };

  const handleMissingContactsProvided = async (
    newContacts: Record<string, { phone?: string; email?: string }>,
  ) => {
    setMissingContactsOpen(false);
    const mergedOverrides: Record<string, { phone?: string; email?: string }> = {
      ...pendingLaunchOverrides,
    };
    for (const [id, vals] of Object.entries(newContacts)) {
      mergedOverrides[id] = {
        phone: vals.phone || pendingLaunchOverrides[id]?.phone,
        email: vals.email || pendingLaunchOverrides[id]?.email,
      };
    }
    const launchIds = new Set<string>(Object.keys(newContacts));
    await runLaunchPair(mergedOverrides, launchIds, { skipRedirect: false });
    setReadyLaunchedPendingRedirect(false);
    setPendingLaunchOverrides({});
    setMissingContactCandidates([]);
    setMissingContactsReviewMode(false);
  };

  const handleMissingContactsClose = () => {
    setMissingContactsOpen(false);
    setMissingContactCandidates([]);
    setMissingContactsReviewMode(false);
    setPendingLaunchOverrides({});
    if (readyLaunchedPendingRedirect) {
      const jobIdForEngage = (jobdivaId || jobData?.jobdiva_id || numericJobId || "").toString().trim();
      setTimeout(() => {
        if (jobIdForEngage) {
          router.push(`/jobs/${encodeURIComponent(jobIdForEngage)}/rankings`);
        } else {
          router.push(`/`);
        }
      }, 200);
      setReadyLaunchedPendingRedirect(false);
    }
  };

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
              Build your candidate search using structured filters. Hoonr-Curate generates the Boolean string and runs JobDiva Talent Search. JobDiva applicants are synced automatically and shown on the rank-list page with source as Job-Diva Applicant.
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
                      { id: 'jobdiva', label: 'JobDiva Talent', icon: <ShieldCheck className="w-4 h-4 text-[#6366f1]" />, disabled: false },
                      { id: 'linkedin', label: 'LinkedIn', icon: <Linkedin className="w-4 h-4 text-[#0A66C2] fill-[#0A66C2]" />, disabled: false },
                      { id: 'dice', label: 'Dice', icon: <Box className="w-4 h-4 text-slate-700" />, disabled: false },
                      { id: 'exa', label: 'Exa', icon: <Search className="w-4 h-4 text-pink-500" />, disabled: false }
                    ].map(source => (
                      <label key={source.id} className={`flex items-center gap-2 ${source.disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer group'}`} title={source.disabled ? "Integration coming soon" : ""}>
                        <Checkbox
                          checked={source.disabled ? false : (searchSources as any)[source.id]}
                          onCheckedChange={(checked) => {
                            if (source.disabled) return;
                            const enabled = !!checked;
                            setSearchSources(prev => ({ ...prev, [source.id]: enabled }));
                            trackEvent("job_wizard_step5_source_toggled", {
                              step: 5,
                              source: source.id,
                              enabled,
                            });
                          }}
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
                      onChange={(e) => {
                        const value = Number(e.target.value);
                        setRecentDaysFilter(value);
                        trackEvent("job_wizard_step5_recent_days_changed", {
                          step: 5,
                          recent_days: value,
                        });
                      }}
                      className="h-8 px-2 text-[12px] font-medium text-slate-700 bg-white border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-[#6366f1]/30"
                    >
                      <option value={30}>Last 30 days</option>
                      <option value={60}>Last 60 days</option>
                      <option value={90}>Last 90 days</option>
                      <option value={180}>Last 180 days</option>
                      <option value={0}>Any</option>
                    </select>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[11px] font-bold uppercase tracking-widest text-slate-400">Min YOE:</span>
                    <input
                      type="number"
                      min={0}
                      max={40}
                      step={1}
                      value={minExperienceYears ?? ""}
                      onChange={(e) => {
                        const raw = e.target.value;
                        if (raw === "") {
                          setMinExperienceYears(null);
                          return;
                        }
                        const parsed = parseInt(raw, 10);
                        const clamped = Number.isFinite(parsed)
                          ? Math.max(0, Math.min(40, parsed))
                          : null;
                        setMinExperienceYears(clamped);
                      }}
                      placeholder="any"
                      className="w-20 h-8 px-2 text-[12px] font-medium text-slate-700 bg-white border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-[#6366f1]/30"
                      title="Drops candidates whose resume confidently shows fewer years of experience. Leave blank for no floor."
                    />
                  </div>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <Checkbox
                      checked={includeNoResume}
                      onCheckedChange={(checked) => {
                        const enabled = !!checked;
                        setIncludeNoResume(enabled);
                        trackEvent("job_wizard_step5_include_no_resume_toggled", {
                          step: 5,
                          enabled,
                        });
                      }}
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
                              {title.matchType === 'must' ? 'Must have' : title.matchType === 'exclude' ? 'Must not have' : `Can have · Group ${title.orGroup ?? 1}`}
                              <ChevronDown className="w-4 h-4 opacity-50 ml-1" />
                            </div>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="start" className="w-[180px] p-1.5 rounded-xl border-slate-200 shadow-lg">
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]" onClick={() => {
                              setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'must' } : t));
                              trackEvent("job_wizard_step5_source_title_match_type_changed", {
                                step: 5,
                                title: truncateForTelemetry(title.value, 100),
                                match_type: "must",
                              });
                            }}>
                              Must have
                            </DropdownMenuItem>
                            {existingOrGroupIds().map(gid => (
                              <DropdownMenuItem
                                key={`title-or-${gid}`}
                                className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]"
                                onClick={() => {
                                  setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'can', orGroup: gid } : t));
                                  trackEvent("job_wizard_step5_source_title_match_type_changed", {
                                    step: 5,
                                    title: truncateForTelemetry(title.value, 100),
                                    match_type: "can",
                                    or_group: gid,
                                  });
                                }}
                              >
                                Can have · Group {gid}
                              </DropdownMenuItem>
                            ))}
                            <DropdownMenuItem
                              className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px] text-[#16a34a]"
                              onClick={() => {
                                const newGid = nextOrGroupId();
                                setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'can', orGroup: newGid } : t));
                                trackEvent("job_wizard_step5_source_title_match_type_changed", {
                                  step: 5,
                                  title: truncateForTelemetry(title.value, 100),
                                  match_type: "can",
                                  or_group: newGid,
                                  new_group: true,
                                });
                              }}
                            >
                              + New OR group
                            </DropdownMenuItem>
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px] text-red-600" onClick={() => {
                              setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, matchType: 'exclude' } : t));
                              trackEvent("job_wizard_step5_source_title_match_type_changed", {
                                step: 5,
                                title: truncateForTelemetry(title.value, 100),
                                match_type: "exclude",
                              });
                            }}>
                              Must not have
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                        <span className="flex-1 text-[13px] font-bold text-slate-800 px-1">{title.value}</span>

                        <div className="flex items-center h-8 bg-white border border-slate-200 rounded-lg overflow-hidden ml-auto shadow-sm">
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => {
                            const nextYears = Math.max(0, title.years - 1);
                            setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, years: nextYears } : t));
                            trackEvent("job_wizard_step5_source_title_years_changed", {
                              step: 5,
                              title: truncateForTelemetry(title.value, 100),
                              years: nextYears,
                            });
                          }}>-</button>
                          <span className="px-2 h-full flex items-center justify-center text-[11px] font-bold text-slate-700 min-w-[58px] text-center border-x border-slate-100">{title.years === 0 ? 'Any exp' : `${title.years}+ yr${title.years > 1 ? 's' : ''}`}</span>
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => {
                            const nextYears = title.years + 1;
                            setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, years: nextYears } : t));
                            trackEvent("job_wizard_step5_source_title_years_changed", {
                              step: 5,
                              title: truncateForTelemetry(title.value, 100),
                              years: nextYears,
                            });
                          }}>+</button>
                        </div>

                        <button
                          className={`flex items-center gap-1.5 px-2.5 h-8 rounded-xl text-[11px] font-bold transition-all border shadow-sm ${title.recent ? 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]' : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                            }`}
                          onClick={() => {
                            const nextRecent = !title.recent;
                            setSourceTitles(prev => prev.map(t => t.id === title.id ? { ...t, recent: nextRecent } : t));
                            trackEvent("job_wizard_step5_source_title_recent_toggled", {
                              step: 5,
                              title: truncateForTelemetry(title.value, 100),
                              recent: nextRecent,
                            });
                          }}
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
                          onClick={() => {
                            setSourceTitles(prev => prev.filter(t => t.id !== title.id));
                            trackEvent("job_wizard_step5_source_title_removed", {
                              step: 5,
                              title: truncateForTelemetry(title.value, 100),
                            });
                          }}
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
                              {skill.matchType === 'must' ? 'Must have' : skill.matchType === 'exclude' ? 'Must not have' : `Can have · Group ${skill.orGroup ?? 1}`}
                              <ChevronDown className="w-4 h-4 opacity-50 ml-1" />
                            </div>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="start" className="w-[180px] p-1.5 rounded-xl border-slate-200 shadow-lg">
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]" onClick={() => {
                              setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'must' } : s));
                              trackEvent("job_wizard_step5_source_skill_match_type_changed", {
                                step: 5,
                                skill: truncateForTelemetry(skill.value, 100),
                                match_type: "must",
                              });
                            }}>
                              Must have
                            </DropdownMenuItem>
                            {existingOrGroupIds().map(gid => (
                              <DropdownMenuItem
                                key={`skill-or-${gid}`}
                                className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px]"
                                onClick={() => {
                                  setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'can', orGroup: gid } : s));
                                  trackEvent("job_wizard_step5_source_skill_match_type_changed", {
                                    step: 5,
                                    skill: truncateForTelemetry(skill.value, 100),
                                    match_type: "can",
                                    or_group: gid,
                                  });
                                }}
                              >
                                Can have · Group {gid}
                              </DropdownMenuItem>
                            ))}
                            <DropdownMenuItem
                              className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px] text-[#16a34a]"
                              onClick={() => {
                                const newGid = nextOrGroupId();
                                setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'can', orGroup: newGid } : s));
                                trackEvent("job_wizard_step5_source_skill_match_type_changed", {
                                  step: 5,
                                  skill: truncateForTelemetry(skill.value, 100),
                                  match_type: "can",
                                  or_group: newGid,
                                  new_group: true,
                                });
                              }}
                            >
                              + New OR group
                            </DropdownMenuItem>
                            <DropdownMenuItem className="flex items-center gap-2 rounded-lg py-2 cursor-pointer font-bold text-[12px] text-red-600" onClick={() => {
                              setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, matchType: 'exclude' } : s));
                              trackEvent("job_wizard_step5_source_skill_match_type_changed", {
                                step: 5,
                                skill: truncateForTelemetry(skill.value, 100),
                                match_type: "exclude",
                              });
                            }}>
                              Must not have
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                        <span className="flex-1 text-[13px] font-bold text-slate-800 px-1">{skill.value}</span>

                        <div className="flex items-center h-8 bg-white border border-slate-200 rounded-lg overflow-hidden ml-auto shadow-sm">
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => {
                            const nextYears = Math.max(0, skill.years - 1);
                            setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, years: nextYears } : s));
                            trackEvent("job_wizard_step5_source_skill_years_changed", {
                              step: 5,
                              skill: truncateForTelemetry(skill.value, 100),
                              years: nextYears,
                            });
                          }}>-</button>
                          <span className="px-2 h-full flex items-center justify-center text-[11px] font-bold text-slate-700 min-w-[58px] text-center border-x border-slate-100">{skill.years === 0 ? 'Any exp' : `${skill.years}+ yr${skill.years > 1 ? 's' : ''}`}</span>
                          <button className="w-8 h-full flex items-center justify-center hover:bg-slate-50 transition-colors text-slate-400 font-bold text-[14px]" onClick={() => {
                            const nextYears = skill.years + 1;
                            setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, years: nextYears } : s));
                            trackEvent("job_wizard_step5_source_skill_years_changed", {
                              step: 5,
                              skill: truncateForTelemetry(skill.value, 100),
                              years: nextYears,
                            });
                          }}>+</button>
                        </div>

                        <button
                          className={`flex items-center gap-1.5 px-2.5 h-8 rounded-xl text-[11px] font-bold transition-all border shadow-sm ${skill.recent ? 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]' : 'bg-white text-slate-500 border-slate-200 hover:bg-slate-50'
                            }`}
                          onClick={() => {
                            const nextRecent = !skill.recent;
                            setSourceSkills(prev => prev.map(s => s.id === skill.id ? { ...s, recent: nextRecent } : s));
                            trackEvent("job_wizard_step5_source_skill_recent_toggled", {
                              step: 5,
                              skill: truncateForTelemetry(skill.value, 100),
                              recent: nextRecent,
                            });
                          }}
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
                          onClick={() => {
                            setSourceSkills(prev => prev.filter(s => s.id !== skill.id));
                            trackEvent("job_wizard_step5_source_skill_removed", {
                              step: 5,
                              skill: truncateForTelemetry(skill.value, 100),
                            });
                          }}
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
                            onClick={() => {
                              setSourceLocations(prev => prev.filter(l => l.id !== loc.id));
                              trackEvent("job_wizard_step5_source_location_removed", {
                                step: 5,
                                value: truncateForTelemetry(loc.value, 100),
                              });
                            }}
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
                    <div className="flex items-center gap-1.5 px-3 h-11 border border-slate-200 rounded-xl bg-white">
                      <span className="text-[12px] font-bold text-slate-500 uppercase tracking-wider">Within</span>
                      <Input
                        type="number"
                        min={1}
                        max={100}
                        step={5}
                        value={sourceLocationMiles}
                        onChange={(e) => {
                          const next = Number(e.target.value);
                          if (Number.isFinite(next)) {
                            setSourceLocationMiles(next);
                          }
                        }}
                        onBlur={() => {
                          setSourceLocationMiles((prev) => {
                            const clamped = Math.min(100, Math.max(1, Math.round(prev || 25)));
                            if (clamped !== prev) {
                              trackEvent("job_wizard_step5_location_radius_changed", {
                                step: 5,
                                radius_miles: clamped,
                              });
                            }
                            return clamped;
                          });
                          setGeneratedBoolean("");
                        }}
                        className="h-7 w-14 px-1 text-center text-[13px] font-bold border-0 focus:ring-0 focus-visible:ring-0 shadow-none p-0"
                        aria-label="Search radius in miles"
                      />
                      <span className="text-[12px] font-bold text-slate-500 uppercase tracking-wider">mi</span>
                    </div>
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
                            trackEvent("job_wizard_step5_source_company_removed", {
                              step: 5,
                              value: truncateForTelemetry(company, 100),
                            });
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
                          onClick={() => {
                            setSourceKeywords(prev => prev.filter(t => t !== tag));
                            trackEvent("job_wizard_step5_source_keyword_removed", {
                              step: 5,
                              value: truncateForTelemetry(tag, 100),
                            });
                          }}
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
                        trackEvent("job_wizard_step5_boolean_panel_toggled", {
                          step: 5,
                          opened: nextState,
                        });

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
                                      trackEvent("job_wizard_step5_boolean_reset", {
                                        step: 5,
                                      });
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
                                trackEvent("job_wizard_step5_boolean_edited", {
                                  step: 5,
                                  length: e.target.value.length,
                                });
                              }}
                              onBlur={(e) => {
                                trackEvent("job_wizard_step5_boolean_edit_saved", {
                                  step: 5,
                                  query: truncateForTelemetry(e.target.value, 320),
                                });
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
                                      className={`p-3 rounded-lg border ${isCurrent
                                        ? "bg-[#f5f3ff] border-[#ddd6fe]"
                                        : "bg-slate-50 border-slate-200"
                                        }`}
                                    >
                                      <div className="flex items-center justify-between gap-2 mb-1.5 flex-wrap">
                                        <div className="flex items-center gap-2 flex-wrap">
                                          <span className={`text-[10px] font-bold uppercase tracking-widest px-2 py-0.5 rounded-full border ${isCurrent
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
                                            navigator.clipboard?.writeText(attempt.query).catch(() => { });
                                            trackEvent("job_wizard_step5_boolean_history_copied", {
                                              step: 5,
                                              attempt: idx + 1,
                                              query: truncateForTelemetry(attempt.query, 260),
                                            });
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
                    {/* Run/Stop live OUTSIDE the "View boolean string" collapsible
                        so recruiters can always see the primary action. The
                        collapsible above is for reviewing/editing the string. */}
                    <div className="flex items-center justify-end gap-2 px-6 pb-6 pt-2">
                      {/* Recruiter-triggered re-open of the auto-show modal. The
                          modal auto-opens once per session (sessionStorage gate)
                          on Step 5 entry, so once dismissed there was no path
                          back to it. Same outline/slate look as Save & Exit so
                          it doesn't compete visually with the primary purple
                          "Run Search" CTA. */}
                      <Button
                        variant="outline"
                        className="bg-white border-slate-200 hover:bg-slate-50 text-slate-700 font-bold h-9 px-4 rounded-lg flex items-center gap-2 shadow-sm transition-all active:scale-95 text-[13.5px] flex-shrink-0"
                        onClick={() => {
                          setShowJobdivaSkillsModal(true);
                          setSkillsCopied(false);
                          trackEvent("job_wizard_step5_view_agent_string_clicked", { step: 5 });
                        }}
                      >
                        <FileText className="w-4 h-4 text-slate-400" />
                        View JobDiva Search Agent string
                      </Button>
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
                      isSearching ? searchStatus : `${candidates.length} candidates found${sourceFilter !== "all" ? ` · showing ${sortedCandidates.length}` : ""}`
                    ) : 'Run a search to find candidates.'}
                  </p>
                  {hasSearched && !isSearching && candidates.length > 0 && qualityScorecard && (
                    <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                      <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-[#ede9fe] text-[#5b21b6] text-[10.5px] font-bold uppercase tracking-wider border border-[#ddd6fe]">
                        {qualityScorecard.quality_tier_counts.gte_70} / {qualityScorecard.total_results} ≥ 70%
                      </span>
                      <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-emerald-50 text-emerald-700 text-[10.5px] font-bold uppercase tracking-wider border border-emerald-200">
                        Avg {qualityScorecard.average_match_score ?? "—"}%
                      </span>
                      <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-white text-slate-600 text-[10.5px] font-bold uppercase tracking-wider border border-slate-200">
                        {qualityScorecard.quality_tier_counts.gte_80} ≥ 80%
                      </span>
                      <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-white text-slate-600 text-[10.5px] font-bold uppercase tracking-wider border border-slate-200">
                        {qualityScorecard.quality_tier_counts.gte_90} ≥ 90%
                      </span>
                      {lastSearchRuntimeSec !== null && (
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-white text-slate-600 text-[10.5px] font-bold uppercase tracking-wider border border-slate-200">
                          {lastSearchRuntimeSec}s{lastSearchRunsExecuted ? ` · ${lastSearchRunsExecuted} run${lastSearchRunsExecuted === 1 ? "" : "s"}` : ""}
                        </span>
                      )}
                      {topMatchedPreview.length > 0 && (
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-sky-50 text-sky-700 text-[10.5px] font-semibold border border-sky-200">
                          Top matched: {topMatchedPreview.join(", ")}
                        </span>
                      )}
                      {topMissingPreview.length > 0 && (
                        <span className="inline-flex items-center px-2.5 py-1 rounded-full bg-amber-50 text-amber-700 text-[10.5px] font-semibold border border-amber-200">
                          Top missing: {topMissingPreview.join(", ")}
                        </span>
                      )}
                    </div>
                  )}
                  {restoredFromCache && !isSearching && (
                    <p className="text-[11.5px] font-medium text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-2 py-1 mt-2 inline-block">
                      Restored from last run · Re-run to refresh
                    </p>
                  )}
                  {isCheckingJobdivaCriteria && !isSearching && searchSources.jobdiva && (
                    <p className="text-[11.5px] font-medium text-slate-600 bg-slate-50 border border-slate-200 rounded-md px-2 py-1 mt-2 inline-flex items-center gap-1.5">
                      <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      Checking JobDiva AI matcher criteria...
                    </p>
                  )}
                  {candidates.length > 0 && (
                    <div className="flex items-center gap-1.5 mt-3 flex-wrap">
                      {([
                        { id: "all", label: "All", count: totalCandidatesCount },
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
                            className={`px-2.5 py-1 rounded-full text-[11px] font-bold uppercase tracking-wider border transition-colors ${active
                              ? 'bg-[#6366f1] text-white border-[#6366f1]'
                              : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'}`}
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
                        const n = Math.max(1, selectBestN);
                        const firstN = candidates
                          .filter(c => {
                            const key = `${c.source ?? ''}:${c.candidate_id || c.jobdiva_candidate_id || c.id}`;
                            return !launchedCandidateKeys.has(key) && !launchedCandidateIds.has(String(c.candidate_id || c.jobdiva_candidate_id || c.id)) && !dncCandidateKeys.has(key);
                          })
                          .slice(0, n);

                        const allFirstNSelected = firstN.length > 0 && firstN.every(c => selectedCandidates.has(c.candidate_id || c.jobdiva_candidate_id || c.id));

                        if (allFirstNSelected) {
                          setSelectedCandidates(prev => {
                            const next = new Set(prev);
                            firstN.forEach(c => {
                              const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
                              next.delete(id);
                            });
                            return next;
                          });
                        } else {
                          setSelectedCandidates(prev => {
                            const next = new Set(prev);
                            firstN.forEach(c => {
                              const id = c.candidate_id || c.jobdiva_candidate_id || c.id;
                              next.add(id);
                            });
                            return next;
                          });
                        }
                      }}
                    >
                      <Star className="w-3.5 h-3.5 fill-slate-700" />
                      {(() => {
                        const n = Math.max(1, selectBestN);
                        const firstN = candidates
                          .filter(c => {
                            const key = `${c.source ?? ''}:${c.candidate_id || c.jobdiva_candidate_id || c.id}`;
                            return !launchedCandidateKeys.has(key) && !launchedCandidateIds.has(String(c.candidate_id || c.jobdiva_candidate_id || c.id)) && !dncCandidateKeys.has(key);
                          })
                          .slice(0, n);
                        const allFirstNSelected = firstN.length > 0 && firstN.every(c => selectedCandidates.has(c.candidate_id || c.jobdiva_candidate_id || c.id));
                        return allFirstNSelected ? 'Deselect Best' : 'Select Best';
                      })()
                      }
                    </Button>
                    <input
                      type="number"
                      min={1}
                      value={selectBestInput}
                      onChange={(e) => {
                        const raw = e.target.value;
                        setSelectBestInput(raw);
                        const parsed = parseInt(raw, 10);
                        if (!isNaN(parsed) && parsed > 0) {
                          setSelectBestN(parsed);
                        }
                      }}
                      onBlur={() => {
                        const parsed = parseInt(selectBestInput, 10);
                        if (isNaN(parsed) || parsed <= 0) {
                          setSelectBestN(100);
                          setSelectBestInput("100");
                        } else {
                          setSelectBestInput(String(parsed));
                        }
                      }}
                      aria-label="Number of best candidates to select"
                      className="h-8 w-16 px-2 text-[13px] font-bold text-slate-700 border border-slate-200 rounded-md bg-white shadow-sm focus:outline-none focus:ring-2 focus:ring-[#6366f1]/40 focus:border-[#6366f1]"
                    />
                    <Button
                      variant="outline"
                      className="h-8 px-4 text-[13px] font-bold border-slate-200 text-slate-700 bg-white"
                      onClick={() => {
                        const eligible = candidates.filter(c => {
                          const key = `${c.source ?? ''}:${c.candidate_id || c.jobdiva_candidate_id || c.id}`;
                          return !launchedCandidateKeys.has(key) && !launchedCandidateIds.has(String(c.candidate_id || c.jobdiva_candidate_id || c.id)) && !dncCandidateKeys.has(key);
                        });
                        const allIds = eligible.map(c => c.candidate_id || c.jobdiva_candidate_id || c.id);
                        const allSelected = allIds.length > 0 && allIds.every(id => selectedCandidates.has(id));

                        if (allSelected) {
                          // Deselect all
                          setSelectedCandidates(new Set());
                        } else {
                          // Select all (skipping already-launched and DNC)
                          setSelectedCandidates(new Set(allIds));
                        }
                      }}
                    >
                      {(() => {
                        const eligible = candidates.filter(c => {
                          const key = `${c.source ?? ''}:${c.candidate_id || c.jobdiva_candidate_id || c.id}`;
                          return !launchedCandidateKeys.has(key) && !launchedCandidateIds.has(String(c.candidate_id || c.jobdiva_candidate_id || c.id)) && !dncCandidateKeys.has(key);
                        });
                        const allIds = eligible.map(c => c.candidate_id || c.jobdiva_candidate_id || c.id);
                        const allSelected = allIds.length > 0 && allIds.every(id => selectedCandidates.has(id));
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

                  {candidates.length > 0 && (
                    <div className="mb-4 flex flex-wrap items-center gap-2 bg-white p-3 rounded-xl border border-slate-200 shadow-sm">
                      <div className="relative w-[280px] shrink-0">
                        <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
                          <Search className="h-4 w-4 text-slate-400" />
                        </div>
                        <Input
                          placeholder="Search name, email, phone..."
                          value={candidateSearchQuery}
                          onChange={(e) => { setCandidateSearchQuery(e.target.value); setCurrentPage(1); }}
                          className="h-9 pl-9 pr-3 w-full bg-slate-50 border-transparent focus:bg-white rounded-lg text-[13px]"
                        />
                      </div>

                      <div className="relative" ref={locationFilterRef}>
                        <button
                          type="button"
                          onClick={() => setLocationFilterOpen((v) => !v)}
                          className={`h-9 px-3 inline-flex items-center gap-2 rounded-lg border text-[13px] font-medium transition-colors ${
                            locationFilter.size > 0
                              ? "bg-indigo-50 border-indigo-200 text-indigo-700"
                              : "bg-slate-50 border-transparent text-slate-700 hover:bg-white hover:border-slate-200"
                          }`}
                        >
                          <MapPin className="w-3.5 h-3.5" />
                          Location
                          {locationFilter.size > 0 && (
                            <span className="px-1.5 py-0.5 rounded-full bg-indigo-600 text-white text-[10px] font-bold">
                              {locationFilter.size}
                            </span>
                          )}
                          <ChevronDown className="w-3.5 h-3.5 opacity-60" />
                        </button>
                        {locationFilterOpen && (
                          <div className="absolute z-30 mt-1 left-0 w-[300px] max-h-[340px] flex flex-col bg-white rounded-lg border border-slate-200 shadow-lg">
                            <div className="p-2 border-b border-slate-100">
                              <Input
                                placeholder="Filter locations..."
                                value={locationOptionSearch}
                                onChange={(e) => setLocationOptionSearch(e.target.value)}
                                className="h-8 text-[12.5px]"
                              />
                            </div>
                            <div className="overflow-y-auto p-1">
                              {candidateLocationOptions.length === 0 ? (
                                <div className="px-3 py-4 text-[12px] text-slate-400 text-center">No locations found.</div>
                              ) : (
                                candidateLocationOptions
                                  .filter(([loc]) =>
                                    loc.toLowerCase().includes(locationOptionSearch.trim().toLowerCase())
                                  )
                                  .map(([loc, count]) => {
                                    const checked = locationFilter.has(loc);
                                    return (
                                      <label
                                        key={loc}
                                        className="flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-slate-50 cursor-pointer text-[12.5px]"
                                      >
                                        <Checkbox
                                          checked={checked}
                                          onCheckedChange={(v) => {
                                            setLocationFilter((prev) => {
                                              const next = new Set(prev);
                                              if (v) next.add(loc);
                                              else next.delete(loc);
                                              return next;
                                            });
                                            setCurrentPage(1);
                                          }}
                                          className="w-3.5 h-3.5"
                                        />
                                        <span className="flex-1 truncate text-slate-700" title={loc}>{loc}</span>
                                        <span className="text-[11px] text-slate-400">{count}</span>
                                      </label>
                                    );
                                  })
                              )}
                            </div>
                            {locationFilter.size > 0 && (
                              <div className="border-t border-slate-100 p-2">
                                <button
                                  type="button"
                                  onClick={() => { setLocationFilter(new Set()); setCurrentPage(1); }}
                                  className="text-[12px] font-semibold text-indigo-600 hover:text-indigo-800"
                                >
                                  Clear selection
                                </button>
                              </div>
                            )}
                          </div>
                        )}
                      </div>

                      <div className="flex items-center gap-1.5 h-9 px-3 rounded-lg bg-slate-50 border border-transparent">
                        <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Min match</label>
                        <input
                          type="number"
                          min={0}
                          max={100}
                          value={minScore}
                          onChange={(e) => {
                            const n = Math.max(0, Math.min(100, Number(e.target.value) || 0));
                            setMinScore(n);
                            setCurrentPage(1);
                          }}
                          className="w-14 bg-transparent text-[13px] font-semibold text-slate-800 focus:outline-none"
                        />
                        <span className="text-[11px] text-slate-400">%</span>
                      </div>

                      <div className="flex items-center gap-1.5 h-9 px-3 rounded-lg bg-slate-50 border border-transparent">
                        <label className="text-[11px] font-semibold text-slate-500 uppercase tracking-wider">Sort</label>
                        <select
                          value={`${sortKey}:${sortDir}`}
                          onChange={(e) => {
                            const [k, d] = e.target.value.split(":") as [CandidateMatchSortKey, "asc" | "desc"];
                            setSortKey(k);
                            setSortDir(d);
                            setCurrentPage(1);
                          }}
                          className="bg-transparent text-[13px] font-semibold text-slate-700 focus:outline-none cursor-pointer"
                        >
                          <option value="match:desc">Match score (high → low)</option>
                          <option value="match:asc">Match score (low → high)</option>
                          <option value="name:asc">Name (A → Z)</option>
                          <option value="name:desc">Name (Z → A)</option>
                          <option value="lastActive:desc">Last active (newest)</option>
                          <option value="lastActive:asc">Last active (oldest)</option>
                          <option value="location:asc">Location (A → Z)</option>
                          <option value="source:asc">Source (A → Z)</option>
                        </select>
                      </div>

                      <div className="ml-auto text-[12px] text-slate-500">
                        {sortedCandidates.length} of {candidates.length}
                      </div>
                    </div>
                  )}

                  {candidates.length > 0 ? (
                    <CandidateMatchTable
                      candidates={paginatedCandidates}
                      selectedIds={selectedCandidates}
                      disabledLaunchedKeys={launchedCandidateKeys}
                      dncKeys={dncCandidateKeys}
                      onToggleSelect={(id, checked) => {
                        setSelectedCandidates((prev) => {
                          const next = new Set(prev);
                          if (checked) next.add(id);
                          else next.delete(id);
                          return next;
                        });
                      }}
                      onOpenDetails={(candidate) => {
                        setSelectedCandidateForDetails({
                          name: getCandidateDisplayName(candidate),
                          profileUrl: candidate.profile_url,
                          imageUrl: candidate.image_url,
                          jobTitle: candidate.title || candidate.headline || "",
                          location:
                            candidate.location ||
                            (candidate.city ? `${candidate.city}, ${candidate.state}` : ""),
                          experienceYears:
                            candidate.experience_years ||
                            candidate.yearsExtracted ||
                            candidate.enhanced_info?.years_of_experience ||
                            null,
                          tags: [
                            sourceTitles[0]?.value,
                            sourceSkills[0]?.value ? `${sourceSkills[0]?.value} certified` : null,
                            sourceSkills[1]?.value,
                          ].filter(Boolean),
                          matchScore: candidate.match_score,
                          missingSkills: candidate.missing_skills,
                          explainability: candidate.explainability,
                          matchScoreDetails: candidate.match_score_details,
                          matchedSkills: candidate.matched_skills,
                          jobdivaCandidateId:
                            candidate.jobdiva_candidate_id ?? candidate.data?.jobdiva_candidate_id,
                          source: candidate.source,
                        });
                        setDetailsModalOpen(true);
                      }}
                      onOpenResume={async (candidate) => {
                        const isLinkedIn = !!candidate.source?.startsWith("LinkedIn");
                        if (isLinkedIn && candidate.profile_url) {
                          window.open(candidate.profile_url, "_blank", "noopener,noreferrer");
                          return;
                        }
                        const opened = await fetchAndOpenProfileUrl(candidate);
                        if (!opened) {
                          const displayName = getCandidateDisplayName(candidate);
                          handleViewResume({
                            ...candidate,
                            firstName: displayName.split(" ")[0] || displayName,
                            lastName: displayName.split(" ").slice(1).join(" "),
                          });
                        }
                      }}
                      onPhoneSaved={(id, normalised) => {
                        setCandidates((prev) =>
                          prev.map((c) =>
                            (c.candidate_id || c.jobdiva_candidate_id || c.id) === id ? { ...c, phone: normalised } : c
                          )
                        );
                      }}
                      jobdivaId={jobdivaId || jobData?.jobdiva_id || String(numericJobId || "")}
                      sortKey={sortKey}
                      sortDir={sortDir}
                      onSortChange={(k, d) => {
                        setSortKey(k);
                        setSortDir(d);
                        setCurrentPage(1);
                      }}
                    />
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

{/* Pagination Controls */ }
{/* Pagination Controls */ }
{
  candidates.length > 0 && (
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
                className={`w-8 h-8 rounded-lg flex items-center justify-center font-bold text-[13px] transition-all duration-200 ${currentPage === pageNum
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
  )
}
                </>
              ) : (
  <div className="h-4 flex items-center justify-center opacity-0 mt-4">
  </div>
)}

{/* Bulk resume upload lives in the Tira chatbot now — removed from
                Step 5 to keep sourcing focused on the boolean-string workflow. */}
            </div>

  {/* Launch Footer */ }
  < div className = "border-t border-slate-200 pt-6 mt-2 flex items-center justify-between" >
              <span className="text-[13px] font-medium text-slate-400">
                {hasSearched && !isSearching ? `${selectedCandidates.size} candidates selected` : ''}
              </span>
              <div className="flex flex-col items-end gap-2">
                {IS_QA_CURATE && (
                  <button
                    type="button"
                    onClick={() => setQaOverrideEnabled(v => !v)}
                    className="flex items-center gap-2 select-none"
                    title={qaOverrideEnabled
                      ? "Override ON — manual mobile/email entry modal for every candidate (QA behavior)"
                      : "Override OFF — launches for everyone like production"}
                  >
                    <span className="text-[12px] font-semibold text-slate-600">Override</span>
                    <span
                      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${qaOverrideEnabled ? "bg-[#6366f1]" : "bg-slate-300"}`}
                    >
                      <span
                        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${qaOverrideEnabled ? "translate-x-[18px]" : "translate-x-0.5"}`}
                      />
                    </span>
                    <span className={`text-[11px] font-bold ${qaOverrideEnabled ? "text-[#6366f1]" : "text-slate-400"}`}>
                      {qaOverrideEnabled ? "ON" : "OFF"}
                    </span>
                  </button>
                )}
                <Button
                  className="h-[42px] px-5 text-white font-bold text-[14px] rounded-xl flex items-center gap-2 shadow-md transition-all group bg-[#6366f1] hover:bg-[#4f46e5] hover:translate-y-[-1px] active:translate-y-[0px] active:scale-[0.98] disabled:bg-slate-300 disabled:cursor-not-allowed disabled:hover:translate-y-0"
                  onClick={handleLaunchPairClick}
                  disabled={isSearching || isEnrichingContacts || isViewOnly || launchProgress.open}
                  title={isViewOnly ? "Job activity has been stopped" : undefined}
                >
                  {isEnrichingContacts ? (
                    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  ) : (
                    <Rocket className="w-4 h-4 fill-white" />
                  )}
                  {isEnrichingContacts ? "Enriching Contacts..." : "Launch PAIR"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

const renderStepContent = () => {
  let content: ReactNode = null;
  switch (currentStep) {
    case 1: content = intakeStep; break;
    case 2: content = publishStep; break;
    case 3: content = establishRubricStep; break;
    case 4: content = setFiltersStep; break;
    case 5: content = sourceStep; break;
    default: content = null;
  }
  // Steps 1-4 lock down to read-only when the wizard is opened in source
  // (Active job, re-launching) or view (Inactive job) mode. Step 5 stays
  // interactive in source mode; in view mode the Launch PAIR button itself
  // is gated so we still wrap the step to also freeze its filters/search.
  const shouldFreeze = isReadOnly && (currentStep !== 5 || isViewOnly);
  if (shouldFreeze) {
    return (
      <fieldset disabled className="contents">
        {content}
      </fieldset>
    );
  }
  return content;
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
          const title = enhancedTitle || jobData?.enhanced_title || jobData?.title || jobTitle;
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
            onClick={() => {
              const toStep = (currentStep - 1) as Step;
              trackEvent("job_wizard_step_back_clicked", {
                from_step: currentStep,
                to_step: toStep,
                from_step_label: STEP_LABELS[currentStep],
                to_step_label: STEP_LABELS[toStep],
              });
              setCurrentStep(toStep);
            }}
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
              // Per-step loading: every Next click flips isAdvancingStep on
              // for the duration of its own save (+ rubric fetch on step 2),
              // so the button shows "Preparing..." while something is
              // actually in flight. Passing currentStep+1 to saveJobDraft
              // records the step the user is navigating TO, so Resume Setup
              // lands them back where they were — not one step behind.
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

                setIsAdvancingStep(true);
                try {
                  const saved = await saveJobDraft({ currentStep: 2, skipToast: true });
                  if (!saved) {
                    showToast("Failed to save Step 1 data. Please try again.", "info");
                    return;
                  }
                  trackStepAdvance(1, 2, { via: "next_button" });
                  setCurrentStep(2);
                } finally {
                  setIsAdvancingStep(false);
                }
                return;
              } else if (currentStep === 2) {
                if (isReadOnly) {
                  trackStepAdvance(2, 3, { via: "next_button", read_only: true });
                  setCurrentStep(3);
                  return;
                }
                setIsAdvancingStep(true);
                try {
                  const saved = await saveJobDraft({ currentStep: 3, skipToast: true });
                  if (!saved) {
                    showToast("Failed to save Step 2 data. Please try again.", "info");
                    return;
                  }

                  // Regenerate rubric when (a) none exists yet, or (b) the
                  // JD text has been edited since the last rubric was
                  // generated. Otherwise preserve the existing rubric +
                  // recruiter edits on Step 3.
                  const jdFingerprint = (jobPosting || "").trim();
                  const rubricIsEmpty = !rubricData || (rubricData.titles?.length === 0 && rubricData.skills?.length === 0);
                  const jdChanged = jdFingerprint !== "" && jdFingerprint !== lastRubricJdRef.current;
                  if (rubricIsEmpty || jdChanged) {
                    setIsGeneratingRubric(true);
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
                        lastRubricJdRef.current = jdFingerprint;
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
                  }
                  trackStepAdvance(2, 3, {
                    via: "next_button",
                    rubric_regenerated: rubricIsEmpty || jdChanged,
                  });
                  setCurrentStep(3);
                  return;
                } finally {
                  setIsAdvancingStep(false);
                }
              } else if (currentStep === 3) {
                if (isReadOnly) {
                  trackStepAdvance(3, 4, { via: "next_button", read_only: true });
                  setCurrentStep(4);
                  return;
                }
                setIsAdvancingStep(true);
                try {
                  const saved = await saveJobDraft({ currentStep: 4, skipToast: true });
                  if (!saved) return;
                  // If the rubric (titles/skills/total_years) has been
                  // edited since the current Step-4 question set was
                  // generated, force-regenerate role-specific questions so
                  // Step 4 reflects the recruiter's Step-3 changes. Custom
                  // ("other") questions are preserved by the initializer.
                  const nextKey = computeRubricQuestionsKey(rubricData);
                  if (nextKey && nextKey !== lastQuestionsRubricKeyRef.current) {
                    await initializeScreenQuestionsFromRubric({ force: true });
                    lastQuestionsRubricKeyRef.current = nextKey;
                  }
                  trackStepAdvance(3, 4, { via: "next_button" });
                } finally {
                  setIsAdvancingStep(false);
                }
              } else if (currentStep === 4) {
                if (isViewOnly) {
                  trackStepAdvance(4, 5, { via: "next_button", read_only: true });
                  setCurrentStep(5);
                  return;
                }
                setIsAdvancingStep(true);
                try {
                  const saved = await saveJobDraft({ currentStep: 5, skipToast: true });
                  if (!saved) return;
                  const nextSourcingKey = computeSourcingRubricKey(rubricData, resumeMatchFilters);
                  // First entry: derive sourcing criteria from rubric.
                  // Subsequent entries: only refresh when the rubric or
                  // active filter set has actually changed since the last
                  // sourcing init, so recruiter customisations on Step 5
                  // aren't clobbered by no-op revisits.
                  if (!sourcingCriteriaInitializedRef.current) {
                    initializeSourceFromRubric();
                    sourcingCriteriaInitializedRef.current = true;
                  } else if (nextSourcingKey && nextSourcingKey !== lastSourcingRubricKeyRef.current) {
                    initializeSourceFromRubric();
                  }
                  lastSourcingRubricKeyRef.current = nextSourcingKey;
                  trackStepAdvance(4, 5, { via: "next_button" });
                } finally {
                  setIsAdvancingStep(false);
                }
              }

              if (currentStep < 5) setCurrentStep((currentStep + 1) as Step);
            }}
            disabled={(currentStep === 1 && !jobData) || isGeneratingJD || isSearching || isAdvancingStep || isGeneratingRubric}
          >
            {isGeneratingJD ? (
              <>
                <span className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin mr-2" />
                Enriching...
              </>
            ) : (isAdvancingStep || isGeneratingRubric) ? (
              <>
                <span className="w-5 h-5 border-2 border-white/30 border-t-white rounded-full animate-spin mr-2" />
                Preparing...
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

    {showJobdivaSkillsModal && (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
        <div className="w-full max-w-2xl rounded-xl bg-white border border-slate-200 shadow-2xl overflow-hidden">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <div>
              <h3 className="text-[16px] font-bold text-slate-900">JobDiva Search Agent string</h3>
              <p className="text-[12px] text-slate-500 mt-0.5">Use this Boolean-ready string in JobDiva Search Agent, including location format.</p>
            </div>
            <button
              type="button"
              onClick={() => setShowJobdivaSkillsModal(false)}
              className="w-8 h-8 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-100 flex items-center justify-center"
              aria-label="Close skills modal"
            >
              <X className="w-4 h-4" />
            </button>
          </div>

          <div className="px-5 py-4 space-y-3">
            <div className={`text-[12px] rounded-lg px-3 py-2 border ${jobdivaCriteriaUnconfigured ? "text-amber-800 bg-amber-50 border-amber-200" : "text-indigo-800 bg-indigo-50 border-indigo-200"}`}>
              {jobdivaCriteriaUnconfigured
                ? "JobDiva AI matcher isn’t configured for this job yet. We’ll still search, but quality improves once criteria are saved in JobDiva."
                : "Tip: copy this string into JobDiva Search Agent criteria for stronger portal-side matching."}
            </div>
            <div className="text-[12px] text-slate-600">
              {jobdivaSkillsToUse.length} skill{jobdivaSkillsToUse.length === 1 ? "" : "s"} formatted in Boolean form with location suffix: <span className="font-semibold">(HADOOP) AND (SPARK OR PYSPARK), IN (NC-US)</span>
            </div>
            <textarea
              value={jobdivaSkillsCopyText}
              readOnly
              rows={8}
              className="w-full rounded-lg border border-slate-200 bg-slate-50 text-[13px] text-slate-800 font-medium px-3 py-2 outline-none resize-y"
            />
            <div className="flex items-center justify-end gap-2">
              {jobdivaJobEditUrl ? (
                <a
                  href={jobdivaJobEditUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="h-9 px-3 border border-slate-200 rounded-md inline-flex items-center gap-1.5 text-[12.5px] font-bold text-slate-700 hover:bg-slate-50"
                >
                  Open JobDiva Job
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              ) : null}
              <Button
                variant="outline"
                className="h-9 px-3 text-[12.5px] font-bold"
                onClick={() => setShowJobdivaSkillsModal(false)}
              >
                Close
              </Button>
              <Button
                className="h-9 px-3 bg-[#6366f1] hover:bg-[#4f46e5] text-white text-[12.5px] font-bold flex items-center gap-1.5"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(jobdivaSkillsCopyText);
                    setSkillsCopied(true);
                    setTimeout(() => setSkillsCopied(false), 1800);
                  } catch {
                    showToast("Copy failed. Please copy manually.", "info");
                  }
                }}
              >
                <Clipboard className="w-3.5 h-3.5" />
                {skillsCopied ? "Copied" : "Copy agent string"}
              </Button>
            </div>
          </div>
        </div>
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
        keywords={selectedCandidateForResume.keywords}
        similarKeywords={selectedCandidateForResume.similarKeywords}
        jobdivaCandidateId={selectedCandidateForResume.jobdivaCandidateId}
        source={selectedCandidateForResume.source}
        isOpen={resumeModalOpen}
        onClose={() => {
          setResumeModalOpen(false);
          setSelectedCandidateForResume(null);
        }}
      />
    )}

    <MissingContactsModal
      open={missingContactsOpen}
      candidates={missingContactCandidates}
      onClose={handleMissingContactsClose}
      onAllProvided={handleMissingContactsProvided}
      title={
        missingContactsReviewMode
          ? "Review candidate contacts before launch"
          : undefined
      }
      description={
        missingContactsReviewMode
          ? "PAIR is gated in this environment — confirm or override the mobile number and email for each candidate before launching."
          : undefined
      }
      primaryLabel={
        missingContactsReviewMode ? "Confirm & launch PAIR" : undefined
      }
      // Normal flow: let the recruiter launch whoever now has a phone/email and
      // skip the rest. QA review-gate mode keeps confirm-all (no partial).
      allowPartial={!missingContactsReviewMode}
      jobId={numericJobId || jobData?.jobdiva_id?.toString() || undefined}
      jobDivaId={jobdivaId || jobData?.jobdiva_id?.toString() || undefined}
    />

    <LaunchPairProgressModal
      progress={launchProgress}
      onClose={() => setLaunchProgress(initialLaunchProgress)}
    />

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
        jobdivaCandidateId={selectedCandidateForDetails.jobdivaCandidateId}
        source={selectedCandidateForDetails.source}
        onClose={() => {
          setDetailsModalOpen(false);
          setSelectedCandidateForDetails(null);
        }}
      />
    )}
  </div>
);
};
