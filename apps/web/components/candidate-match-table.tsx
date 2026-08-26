"use client";

/* eslint-disable @typescript-eslint/no-explicit-any -- candidate objects flow through this view as loosely typed records, matching the surrounding sourcing flow. */

import { useLayoutEffect, useRef, useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Checkbox } from "@/components/ui/checkbox";
import { PhoneIndicator } from "@/components/phone-indicator";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Linkedin,
  ShieldCheck,
  Zap,
  ChevronUp,
  ChevronDown,
  Mail,
  Clock,
  MapPin,
  Briefcase,
  Calendar,
  Users,
  GraduationCap,
  Sparkles,
} from "lucide-react";
import { getCandidateLocations } from "@/lib/candidate-location";

export type CandidateMatchSortKey =
  | "match"
  | "name"
  | "lastActive"
  | "location"
  | "source";

interface Props {
  candidates: any[];
  selectedIds: Set<string>;
  onToggleSelect: (id: string, checked: boolean) => void;
  onOpenDetails: (candidate: any) => void;
  onOpenResume: (candidate: any) => void;
  onPhoneSaved: (id: string, normalisedPhone: string) => void;
  jobdivaId: string;
  sortKey: CandidateMatchSortKey;
  sortDir: "asc" | "desc";
  onSortChange: (key: CandidateMatchSortKey, dir: "asc" | "desc") => void;
  // Set of "${source}:${candidate_id}" keys for candidates already launched
  // on this job. Rows matching are rendered disabled with a badge so the
  // recruiter can't re-launch them.
  disabledLaunchedKeys?: Set<string>;
  // Set of "${source}:${candidate_id}" keys for candidates whose phone is on
  // the Do-Not-Contact list. Rows matching are rendered red and locked out
  // of selection so they cannot be launched.
  dncKeys?: Set<string>;
}

const POPOVER_WIDTH = 540;
const POPOVER_ESTIMATED_HEIGHT = 480;
const POPOVER_GAP = 8;

function formatFollowerCount(n: number | null | undefined): string {
  // Always append the unit so a chip rendered as "👤 226" can't be misread
  // as "in 226 other pipelines" or "226 connections". Users icon + bare
  // number was too ambiguous in the wild.
  if (n == null || !Number.isFinite(Number(n))) return "";
  const v = Number(n);
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1).replace(/\.0$/, "")}M followers`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(1).replace(/\.0$/, "")}k followers`;
  return `${v} followers`;
}

function getInitials(name: string): string {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function getCandidateId(c: any): string {
  return String(c.candidate_id || c.id || "");
}

function getDisplayName(c: any): string {
  const normalize = (v: any) => {
    const s = String(v || "").replace(/\s+/g, " ").trim();
    if (!s) return "";
    if (
      ["linkedin candidate", "professional candidate", "unknown candidate", "unknown"].includes(
        s.toLowerCase()
      )
    ) {
      return "";
    }
    return s;
  };
  return (
    normalize(c.name) ||
    normalize([c.firstName, c.lastName].filter(Boolean).join(" ")) ||
    normalize(c.title) ||
    (c.source === "LinkedIn" ? "LinkedIn profile" : "Unnamed candidate")
  );
}

function getLastActiveDate(c: any): Date | null {
  const raw =
    c.available ||
    c.DATEAVAILABLE ||
    c.received ||
    c.received_date ||
    c.receivedDate ||
    c.last_modified ||
    c.lastModified;
  if (!raw) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatLastActiveShort(d: Date | null): string {
  if (!d) return "";
  return d.toLocaleDateString(undefined, { month: "2-digit", day: "2-digit", year: "numeric" });
}

function getMatchTone(score: number | null) {
  if (score == null) return null;
  if (score >= 80) return { ring: "#2563eb", bg: "#dbeafe", text: "#1d4ed8" };
  if (score >= 60) return { ring: "#d97706", bg: "#fef3c7", text: "#b45309" };
  return { ring: "#e11d48", bg: "#ffe4e6", text: "#be123c" };
}

function getMatchedSkills(c: any): string[] {
  const raw = Array.isArray(c.matched_skills) ? c.matched_skills : [];
  return raw
    .map((s: any) => (typeof s === "string" ? s : s?.name))
    .filter((s: any) => typeof s === "string" && s.trim().length > 0);
}

function getMissingSkills(c: any): string[] {
  const raw = Array.isArray(c.missing_skills) ? c.missing_skills : [];
  return raw
    .map((s: any) => (typeof s === "string" ? s : s?.name))
    .filter((s: any) => typeof s === "string" && s.trim().length > 0);
}

// Progressive sourcing stages. Backend emits "agent_result" first (name +
// source only), then patches in "details_loaded" (resume / profile fields)
// and finally "scored" (match_score + matched_skills). External / cached
// sources still emit the full row in one shot — those candidates have no
// `_stage` field and are treated as fully loaded.
function awaitingDetails(c: any): boolean {
  return String(c?._stage || "") === "agent_result";
}

function awaitingScore(c: any): boolean {
  const stage = String(c?._stage || "");
  return stage === "agent_result" || stage === "details_loaded";
}

function getSourceBadge(source: string | undefined, sources?: string[]) {
  const src = String(source || "");
  // A candidate may have been surfaced by both the keyword-driven Exa pass
  // and the Exa Research deep-search pass; the backend merges them into a
  // single `sources` array. Pick the most informative single label here.
  const srcList = Array.isArray(sources) ? sources.filter(Boolean) : [];
  const hasExa = srcList.includes("LinkedIn-Exa") || src === "LinkedIn-Exa";
  const hasDeep = srcList.includes("LinkedIn-DeepSearch") || src === "LinkedIn-DeepSearch";
  const isBothExa = hasExa && hasDeep;
  const isDeepOnly = !hasExa && hasDeep;
  const isLinkedIn = src.startsWith("LinkedIn") || hasExa || hasDeep;
  const isJobDivaTalent = src === "JobDiva-TalentSearch";
  const isJobAgent = src === "JobDiva-JobAgent";
  const isJobDiva = src.toLowerCase().startsWith("jobdiva");
  const colors = isBothExa
    ? "bg-[#eef2ff] text-[#4338ca] border-[#c7d2fe]"
    : isDeepOnly
      ? "bg-[#f5f3ff] text-[#7c3aed] border-[#ddd6fe]"
      : isLinkedIn
        ? "bg-[#eff6ff] text-[#1d4ed8] border-[#bfdbfe]"
        : isJobDivaTalent || isJobAgent
          ? "bg-[#fff7ed] text-[#c2410c] border-[#fed7aa]"
          : isJobDiva
            ? "bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]"
            : "bg-slate-50 text-slate-700 border-slate-200";
  const label = isBothExa
    ? "LinkedIn + Deep"
    : isDeepOnly
      ? "LinkedIn Deep"
      : isLinkedIn
        ? "LinkedIn"
        : isJobAgent
          ? "JobDiva Agent"
          : isJobDivaTalent
            ? "JobDiva"
            : src || "JobDiva";
  const Icon = isLinkedIn ? Linkedin : isJobDivaTalent || isJobAgent ? Zap : ShieldCheck;
  return { colors, label, Icon, isLinkedIn };
}

function SortHeader({
  label,
  columnKey,
  activeKey,
  activeDir,
  onSortChange,
  align = "left",
  className = "",
}: {
  label: string;
  columnKey: CandidateMatchSortKey;
  activeKey: CandidateMatchSortKey;
  activeDir: "asc" | "desc";
  onSortChange: (key: CandidateMatchSortKey, dir: "asc" | "desc") => void;
  align?: "left" | "center" | "right";
  className?: string;
}) {
  const active = activeKey === columnKey;
  const handleClick = () => {
    if (!active) {
      const defaultDir = columnKey === "match" || columnKey === "lastActive" ? "desc" : "asc";
      onSortChange(columnKey, defaultDir);
    } else {
      onSortChange(columnKey, activeDir === "asc" ? "desc" : "asc");
    }
  };
  return (
    <button
      type="button"
      onClick={handleClick}
      className={`inline-flex items-center gap-1 select-none ${
        align === "center" ? "justify-center w-full" : align === "right" ? "justify-end w-full" : ""
      } ${active ? "text-slate-900" : "text-slate-500 hover:text-slate-700"} ${className}`}
    >
      <span className="text-[11px] font-semibold uppercase tracking-wider">{label}</span>
      {active ? (
        activeDir === "asc" ? (
          <ChevronUp className="w-3 h-3" />
        ) : (
          <ChevronDown className="w-3 h-3" />
        )
      ) : (
        <span className="w-3 h-3 inline-block opacity-30">
          <ChevronUp className="w-3 h-3" />
        </span>
      )}
    </button>
  );
}

export function CandidateMatchTable({
  candidates,
  selectedIds,
  onToggleSelect,
  onOpenDetails,
  onOpenResume,
  onPhoneSaved,
  jobdivaId,
  sortKey,
  sortDir,
  onSortChange,
  disabledLaunchedKeys,
  dncKeys,
}: Props) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [hoverPos, setHoverPos] = useState<{ top: number; left: number; placement: "below" | "above" } | null>(null);
  const scoreRefs = useRef<Map<string, HTMLElement | null>>(new Map());

  const computePosition = (scoreEl: HTMLElement) => {
    const rect = scoreEl.getBoundingClientRect();
    const left = Math.min(
      Math.max(8, rect.left),
      window.innerWidth - POPOVER_WIDTH - 8
    );
    const placeBelow = rect.bottom + POPOVER_GAP + POPOVER_ESTIMATED_HEIGHT < window.innerHeight;
    const top = placeBelow ? rect.bottom + POPOVER_GAP : rect.top - POPOVER_GAP - POPOVER_ESTIMATED_HEIGHT;
    setHoverPos({ top, left, placement: placeBelow ? "below" : "above" });
  };

  // Recompute on scroll/resize while the score is hovered.
  useLayoutEffect(() => {
    if (!hoveredId) return;
    const scoreEl = scoreRefs.current.get(hoveredId);
    if (!scoreEl) return;
    computePosition(scoreEl);
    const handler = () => {
      const el = scoreRefs.current.get(hoveredId);
      if (el) computePosition(el);
    };
    window.addEventListener("scroll", handler, { passive: true, capture: true });
    window.addEventListener("resize", handler);
    return () => {
      window.removeEventListener("scroll", handler, { capture: true } as any);
      window.removeEventListener("resize", handler);
    };
  }, [hoveredId]);

  const hoveredCandidate = hoveredId ? candidates.find((c) => getCandidateId(c) === hoveredId) : null;

  return (
    <div className="relative">
      <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
        <Table className="text-[13px]">
          <TableHeader className="bg-slate-50">
            <TableRow className="hover:bg-slate-50">
              <TableHead className="w-10 pl-4">
                <span className="sr-only">Select</span>
              </TableHead>
              <TableHead>
                <SortHeader
                  label="Name"
                  columnKey="name"
                  activeKey={sortKey}
                  activeDir={sortDir}
                  onSortChange={onSortChange}
                />
              </TableHead>
              <TableHead className="w-[150px]">
                <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  Phone
                </span>
              </TableHead>
              <TableHead className="w-[110px] text-center">
                <SortHeader
                  label="Match"
                  columnKey="match"
                  activeKey={sortKey}
                  activeDir={sortDir}
                  onSortChange={onSortChange}
                  align="center"
                />
              </TableHead>
              <TableHead>
                <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  Skills matched
                </span>
              </TableHead>
              <TableHead className="w-[180px]">
                <SortHeader
                  label="Location"
                  columnKey="location"
                  activeKey={sortKey}
                  activeDir={sortDir}
                  onSortChange={onSortChange}
                />
              </TableHead>
              <TableHead className="w-[110px]">
                <SortHeader
                  label="Last Active"
                  columnKey="lastActive"
                  activeKey={sortKey}
                  activeDir={sortDir}
                  onSortChange={onSortChange}
                />
              </TableHead>
              <TableHead className="w-[110px] pr-4">
                <SortHeader
                  label="Source"
                  columnKey="source"
                  activeKey={sortKey}
                  activeDir={sortDir}
                  onSortChange={onSortChange}
                />
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {candidates.map((candidate) => {
              const id = getCandidateId(candidate);
              const displayName = getDisplayName(candidate);
              const matched = getMatchedSkills(candidate);
              const topMatched = matched.slice(0, 3);
              const moreMatchedCount = Math.max(0, matched.length - 3);
              const matchScore =
                typeof candidate.match_score === "number" ? candidate.match_score : null;
              const tone = getMatchTone(matchScore);
              const { home: homeLocation, work: workLocation } = getCandidateLocations(candidate);
              const lastActiveDate = getLastActiveDate(candidate);
              const lastActiveShort = formatLastActiveShort(lastActiveDate);
              const sourceBadge = getSourceBadge(candidate.source, candidate.sources);
              const checked = selectedIds.has(id);
              const launchedKey = `${candidate.source ?? ''}:${id}`;
              const isAlreadyLaunched = !!disabledLaunchedKeys?.has(launchedKey);
              const isDnc = !!dncKeys?.has(launchedKey);
              // Backend-stamped flag: current/last employer is on the
              // no-contact company list. Display-only row — greyed out,
              // unselectable, every action disabled.
              const isNoContact = candidate.no_contact === true;
              const noContactTitle =
                candidate.no_contact_reason ||
                "Employer is on the no-contact company list — no actions can be taken";

              return (
                <TableRow
                  key={id}
                  className={`cursor-default transition-colors ${
                    isNoContact
                      ? "bg-slate-100/80 opacity-50"
                      : isDnc
                        ? "bg-rose-50 opacity-80"
                        : isAlreadyLaunched
                          ? "bg-slate-50 opacity-60"
                          : "hover:bg-indigo-50/30"
                  }`}
                >
                  <TableCell className="pl-4">
                    <Checkbox
                      className="w-4 h-4 rounded border-slate-300 data-[state=checked]:bg-purple-600 data-[state=checked]:border-purple-600"
                      checked={checked && !isDnc && !isNoContact}
                      disabled={isAlreadyLaunched || isDnc || isNoContact}
                      onCheckedChange={(v) => {
                        if (isAlreadyLaunched || isDnc || isNoContact) return;
                        onToggleSelect(id, !!v);
                      }}
                      title={
                        isNoContact
                          ? noContactTitle
                          : isDnc
                            ? "Phone is on the Do Not Contact list"
                            : isAlreadyLaunched
                              ? "Already launched on this job"
                              : undefined
                      }
                    />
                  </TableCell>
                  <TableCell className="max-w-[280px]">
                    {isNoContact ? (
                      <span
                        className="text-left text-[13.5px] font-semibold text-slate-500 truncate block max-w-full"
                        title={noContactTitle}
                      >
                        {displayName}
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="text-left text-[13.5px] font-semibold text-slate-900 hover:text-indigo-600 truncate block max-w-full"
                        title={`${displayName} — view resume`}
                        onClick={() => onOpenResume(candidate)}
                      >
                        {displayName}
                      </button>
                    )}
                    {candidate.title || candidate.headline ? (
                      <div className="text-[11.5px] text-slate-500 truncate" title={candidate.title || candidate.headline}>
                        {candidate.title || candidate.headline}
                      </div>
                    ) : awaitingDetails(candidate) ? (
                      <Skeleton className="h-3 w-32 mt-1" data-testid="shimmer-title" />
                    ) : null}
                  </TableCell>
                  <TableCell>
                    {candidate.phone ? (
                      <PhoneIndicator
                        candidateId={id}
                        jobdivaId={jobdivaId}
                        phone={candidate.phone}
                        persist={false}
                        onSaved={(normalised) => onPhoneSaved(id, normalised)}
                        linkedinUrl={candidate.profile_url}
                        source={candidate.source}
                        disabled={isNoContact}
                      />
                    ) : awaitingDetails(candidate) ? (
                      <Skeleton className="h-4 w-24" data-testid="shimmer-phone" />
                    ) : (
                      <PhoneIndicator
                        candidateId={id}
                        jobdivaId={jobdivaId}
                        phone={candidate.phone}
                        persist={false}
                        onSaved={(normalised) => onPhoneSaved(id, normalised)}
                        linkedinUrl={candidate.profile_url}
                        source={candidate.source}
                        disabled={isNoContact}
                      />
                    )}
                  </TableCell>
                  <TableCell
                    className="text-center"
                    ref={(el) => {
                      if (el) scoreRefs.current.set(id, el);
                      else scoreRefs.current.delete(id);
                    }}
                    onMouseEnter={(e) => {
                      if (matchScore != null && tone) {
                        setHoveredId(id);
                        computePosition(e.currentTarget);
                      }
                    }}
                    onMouseLeave={() => {
                      setHoveredId((prev) => (prev === id ? null : prev));
                    }}
                  >
                    {isNoContact ? (
                      // No-contact rows are never scored (and the details
                      // popup is an action, so it stays unreachable).
                      <span
                        className="inline-flex items-center justify-center w-12 h-12 rounded-full font-bold text-[12px] bg-slate-100 text-slate-400 border-2 border-slate-200"
                        title={noContactTitle}
                      >
                        —
                      </span>
                    ) : matchScore != null && tone ? (
                      <button
                        type="button"
                        onClick={() => onOpenDetails(candidate)}
                        className="inline-flex items-center justify-center w-12 h-12 rounded-full font-extrabold text-[13px] hover:scale-105 transition-transform shadow-sm"
                        style={{ backgroundColor: tone.bg, color: tone.text, border: `2px solid ${tone.ring}` }}
                        title="View match score breakdown"
                      >
                        {matchScore}%
                      </button>
                    ) : candidate.detail_failed ? (
                      // Candidate-details (résumé / detail API) failed, so we
                      // couldn't score this person. Show a neutral grey "N/A"
                      // rather than a misleading red 0% — they stay launchable.
                      <span
                        className="inline-flex items-center justify-center w-12 h-12 rounded-full font-bold text-[12px] bg-slate-100 text-slate-500 border-2 border-slate-200"
                        title="Couldn't score — JobDiva candidate details were unavailable (e.g. rate limit / no résumé). Still launchable."
                      >
                        N/A
                      </span>
                    ) : awaitingScore(candidate) ? (
                      <Skeleton
                        className="inline-block w-12 h-12 rounded-full"
                        data-testid="shimmer-score"
                      />
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {topMatched.length > 0 ? (
                      <div className="flex flex-wrap gap-1 max-w-[260px]">
                        {topMatched.map((skill, i) => (
                          <span
                            key={`${skill}-${i}`}
                            className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 text-[11px] font-semibold border border-slate-200 truncate max-w-[110px]"
                            title={skill}
                          >
                            {skill}
                          </span>
                        ))}
                        {moreMatchedCount > 0 && (
                          <span className="px-2 py-0.5 rounded-md bg-indigo-50 text-indigo-700 text-[11px] font-semibold border border-indigo-100">
                            +{moreMatchedCount}
                          </span>
                        )}
                      </div>
                    ) : awaitingScore(candidate) ? (
                      <div className="flex flex-wrap gap-1 max-w-[260px]" data-testid="shimmer-skills">
                        <Skeleton className="h-5 w-16 rounded-md" />
                        <Skeleton className="h-5 w-20 rounded-md" />
                        <Skeleton className="h-5 w-14 rounded-md" />
                      </div>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {homeLocation || workLocation ? (
                      <div className="flex flex-col gap-0.5 max-w-[170px]">
                        {homeLocation && (
                          <span
                            className="inline-flex items-center gap-1 text-slate-600 truncate"
                            title={`Location: ${homeLocation}`}
                          >
                            <MapPin className="w-3 h-3 text-slate-400 shrink-0" />
                            <span className="truncate">{homeLocation}</span>
                          </span>
                        )}
                        {workLocation && (
                          <span
                            className="inline-flex items-center gap-1 text-slate-500 truncate"
                            title={`Works in: ${workLocation}`}
                          >
                            <Briefcase className="w-3 h-3 text-slate-400 shrink-0" />
                            <span className="truncate">
                              <span className="text-slate-400">Works in: </span>
                              {workLocation}
                            </span>
                          </span>
                        )}
                        {candidate.location_out_of_radius &&
                          typeof candidate.distance_miles === "number" &&
                          candidate.distance_miles > 0 &&
                          candidate.distance_miles < 9000 && (
                            <span
                              className="inline-flex items-center w-fit px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 text-[10px] font-semibold border border-amber-200"
                              title="Outside the search radius (straight-line distance from the job location)"
                            >
                              ~{Math.round(candidate.distance_miles)} mi away
                            </span>
                          )}
                      </div>
                    ) : awaitingDetails(candidate) ? (
                      <Skeleton className="h-4 w-28" data-testid="shimmer-location" />
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {lastActiveShort ? (
                      <span className="inline-flex items-center gap-1 text-slate-600">
                        <Calendar className="w-3 h-3 text-slate-400" />
                        {lastActiveShort}
                      </span>
                    ) : awaitingDetails(candidate) ? (
                      <Skeleton className="h-4 w-20" data-testid="shimmer-last-active" />
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell className="pr-4">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span
                        className={`px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center gap-1 border ${sourceBadge.colors}`}
                        title={
                          Array.isArray(candidate.sources) && candidate.sources.length > 1
                            ? `Found via: ${candidate.sources.join(" + ")}`
                            : candidate.source || ""
                        }
                      >
                        <sourceBadge.Icon className="w-2.5 h-2.5" />
                        {sourceBadge.label}
                      </span>
                      {/* Standalone Deep chip — fires when Pass B contributed
                          to this row (overlapping with Pass A or as a
                          deep-only find). Visible signal that the recruiter
                          is looking at enriched data, not just a Pass A hit. */}
                      {(Array.isArray(candidate.sources)
                        ? candidate.sources.includes("LinkedIn-DeepSearch")
                        : candidate.source === "LinkedIn-DeepSearch") && (
                        <span
                          className="px-1.5 py-0.5 rounded-md text-[9.5px] font-extrabold uppercase tracking-wider inline-flex items-center gap-0.5 border bg-violet-50 text-violet-700 border-violet-200"
                          title="Enriched via Exa Agent deep-search"
                        >
                          ✦ Deep
                        </span>
                      )}
                      {isNoContact && (
                        <span
                          className="px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center border bg-slate-200 text-slate-600 border-slate-300"
                          title={noContactTitle}
                        >
                          No Contact
                        </span>
                      )}
                      {isDnc && (
                        <span
                          className="px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center border bg-rose-100 text-rose-700 border-rose-300"
                          title="Phone is on the Do Not Contact list — candidate will be skipped at Launch PAIR"
                        >
                          DNC
                        </span>
                      )}
                      {isAlreadyLaunched && (
                        <span
                          className="px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center border bg-amber-50 text-amber-700 border-amber-200"
                          title="This candidate was already launched on this job"
                        >
                          Already Launched
                        </span>
                      )}
                      {(candidate.detail_failed === true ||
                        ["kept_no_resume", "error"].includes(String(candidate.enhanced_info_status || ""))) && (
                        <span
                          className="px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center border bg-amber-50 text-amber-700 border-amber-200"
                          title="JobDiva candidate details were unavailable (e.g. rate limit / no résumé), so this candidate couldn't be scored. Shown as N/A — still launchable."
                        >
                          Limited data
                        </span>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>

      {hoveredCandidate && hoverPos && (
        <HoverDetailsCard
          candidate={hoveredCandidate}
          top={hoverPos.top}
          left={hoverPos.left}
        />
      )}
    </div>
  );
}

function HoverDetailsCard({
  candidate,
  top,
  left,
}: {
  candidate: any;
  top: number;
  left: number;
}) {
  // Header data
  const displayName = getDisplayName(candidate);
  const titleStr = String(
    candidate.title || candidate.current_title || candidate.headline || ""
  ).trim();
  const enhanced = (candidate.enhanced_info || {}) as Record<string, any>;
  const initials = getInitials(displayName);

  // Location + years
  const { home: homeLoc, work: workLoc } = getCandidateLocations(candidate);
  const locationStr = homeLoc || workLoc || String(candidate.location || "").trim();
  const yearsRaw =
    candidate.experience_years ?? candidate.years_experience ?? enhanced.years_of_experience;
  const yearsNum = typeof yearsRaw === "number" ? yearsRaw : Number(yearsRaw);
  const yearsStr = Number.isFinite(yearsNum) && yearsNum > 0 ? `${yearsNum} yrs` : "";

  // Experience: prefer Exa's structured `exa_recent_companies` (Pass B has
  // dates + titles), fall back to enhanced_info.company_experience from
  // resume parsing. Both arrays may be present at the same time after Pass B
  // lands — prefer Exa because its dates are cleaner.
  const exaCompaniesRaw = Array.isArray(candidate.exa_recent_companies)
    ? candidate.exa_recent_companies
    : [];
  const exaCompanies = exaCompaniesRaw.filter((c: any) => c && (c.company || c.title));
  const enhancedExp = Array.isArray(enhanced.company_experience)
    ? enhanced.company_experience
    : Array.isArray(candidate.company_experience)
      ? candidate.company_experience
      : [];
  const useExaExp = exaCompanies.length > 0;
  const experienceRows = (useExaExp ? exaCompanies : enhancedExp).slice(0, 3);

  // Skills
  const matched = getMatchedSkills(candidate);
  const missing = getMissingSkills(candidate).slice(0, 4);

  // Education (best-effort — schemas vary across sources)
  const educationRaw =
    candidate.education || enhanced.education || enhanced.education_summary || "";
  const educationStr = Array.isArray(educationRaw)
    ? educationRaw
        .slice(0, 1)
        .map((e: any) =>
          typeof e === "string"
            ? e
            : [e?.degree, e?.school || e?.institution].filter(Boolean).join(", ") +
              (e?.year || e?.graduation_year ? ` · ${e.year || e.graduation_year}` : "")
        )
        .filter(Boolean)[0] || ""
    : String(educationRaw || "").trim();

  // Exa Research API enrichment
  const exaFitRationale = String(candidate.exa_fit_rationale || "").trim();
  const exaLastActivity = String(candidate.exa_last_activity || "").trim();
  const exaFollowerStr = formatFollowerCount(candidate.exa_follower_count);

  // Explain fallback when Exa rationale isn't there yet
  const explain = Array.isArray(candidate.explainability) ? candidate.explainability : [];
  const firstExplain = typeof explain[0] === "string" ? explain[0] : explain[0]?.text || "";

  // Status chips
  const recentAvailabilityRaw = String(
    candidate.recent_availability ||
      candidate.recentAvailability ||
      candidate.availability_status ||
      candidate.available ||
      ""
  ).trim();
  const availabilityLower = recentAvailabilityRaw.toLowerCase();

  // Tri-state Open-to-Work specifically for LinkedIn `open_to_work` (the
  // Apify-backed signal from services/apify_open_to_work.py). When the value
  // is explicitly true/false we trust it; when it's undefined AND the
  // candidate has a LinkedIn profile_url, treat it as "checking" since the
  // /candidates/open-to-work-statuses poller will resolve it shortly. For
  // anything else, fall through to the legacy heuristic that folds in
  // open_to_relocation + recent availability text.
  const profileUrlStr = String(candidate.profile_url || "").toLowerCase();
  const hasLinkedInUrl = profileUrlStr.includes("linkedin.com");
  const otwExplicit: boolean | null =
    candidate.open_to_work === true
      ? true
      : candidate.open_to_work === false
      ? false
      : null;
  const otwState: "yes" | "no" | "checking" | "legacy_yes" | "none" =
    otwExplicit === true
      ? "yes"
      : otwExplicit === false
      ? "no"
      : hasLinkedInUrl
      ? "checking"
      : !!candidate.open_to_relocation ||
        availabilityLower.includes("open") ||
        availabilityLower.includes("available")
      ? "legacy_yes"
      : "none";
  const isOpenToWork = otwState === "yes" || otwState === "legacy_yes";

  const matchScore =
    typeof candidate.match_score === "number" ? candidate.match_score : null;
  const matchTone = getMatchTone(matchScore);

  const sourceBadge = getSourceBadge(candidate.source, candidate.sources);
  const hasDeepEnrichment =
    Array.isArray(candidate.sources) && candidate.sources.includes("LinkedIn-DeepSearch");

  return (
    <div
      className="pointer-events-none fixed z-[60] rounded-xl border border-slate-200 bg-white shadow-xl"
      style={{ top, left, width: POPOVER_WIDTH }}
    >
      {/* HEADER — avatar + name + source on the same row, title + location below */}
      <div className="p-4 pb-3 border-b border-slate-100">
        <div className="flex items-start gap-3">
          <div className="w-11 h-11 rounded-full bg-indigo-100 text-indigo-700 flex items-center justify-center font-bold text-[13px] shrink-0">
            {initials}
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[14px] font-bold text-slate-900 truncate max-w-[300px]" title={displayName}>
                {displayName}
              </span>
              {hasDeepEnrichment && (
                <span
                  className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-md text-[9.5px] font-extrabold uppercase tracking-wider border bg-violet-50 text-violet-700 border-violet-200"
                  title="Enriched via Exa Agent deep-search"
                >
                  <Sparkles className="w-2.5 h-2.5" />
                  Deep
                </span>
              )}
              <span
                className={`ml-auto px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center gap-1 border shrink-0 ${sourceBadge.colors}`}
                title={Array.isArray(candidate.sources) && candidate.sources.length > 1
                  ? `Found via: ${candidate.sources.join(" + ")}`
                  : sourceBadge.label}
              >
                <sourceBadge.Icon className="w-2.5 h-2.5" />
                {sourceBadge.label}
              </span>
            </div>
            {titleStr && (
              <div className="text-[12.5px] text-slate-700 truncate mt-0.5" title={titleStr}>
                {titleStr}
              </div>
            )}
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11.5px] text-slate-500 mt-1">
              {locationStr && (
                <span className="inline-flex items-center gap-1">
                  <MapPin className="w-3 h-3" />
                  {locationStr}
                </span>
              )}
              {yearsStr && (
                <>
                  {locationStr && <span className="text-slate-300">·</span>}
                  <span className="inline-flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {yearsStr}
                  </span>
                </>
              )}
              {matchScore != null && matchTone && (
                <>
                  <span className="text-slate-300">·</span>
                  <span
                    className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold"
                    style={{ backgroundColor: matchTone.bg, color: matchTone.text }}
                  >
                    {matchScore}% match
                  </span>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Status chip row — Open to work / Email / Follower count / Last activity */}
        {(otwState !== "none" || candidate.email || exaFollowerStr || exaLastActivity || recentAvailabilityRaw) && (
          <div className="flex flex-wrap items-center gap-1.5 mt-3">
            {(otwState === "yes" || otwState === "legacy_yes") && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-emerald-50 text-emerald-700 border-emerald-200">
                ✓ Open to work
              </span>
            )}
            {otwState === "no" && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-slate-50 text-slate-500 border-slate-200"
                title="LinkedIn #OpenToWork badge not set on this profile (checked via Apify)"
              >
                — Not open to work
              </span>
            )}
            {otwState === "checking" && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-amber-50 text-amber-700 border-amber-200"
                title="Checking LinkedIn #OpenToWork status via Apify…"
              >
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                Checking OTW…
              </span>
            )}
            {recentAvailabilityRaw && !isOpenToWork && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-amber-50 text-amber-700 border-amber-200">
                {recentAvailabilityRaw}
              </span>
            )}
            {candidate.email && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-slate-50 text-slate-700 border-slate-200 max-w-[200px]"
                title={candidate.email}
              >
                <Mail className="w-3 h-3" />
                <span className="truncate">{candidate.email}</span>
              </span>
            )}
            {exaFollowerStr && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-indigo-50 text-indigo-700 border-indigo-200"
                title="LinkedIn follower count (Exa Agent)"
              >
                <Users className="w-3 h-3" />
                {exaFollowerStr}
              </span>
            )}
            {exaLastActivity && (
              <span
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border bg-violet-50 text-violet-700 border-violet-200"
                title="LinkedIn last activity (Exa Agent)"
              >
                <Clock className="w-3 h-3" />
                {exaLastActivity}
              </span>
            )}
          </div>
        )}
      </div>

      {/* BODY */}
      <div className="p-4 pt-3 space-y-3">
        {/* EXPERIENCE timeline */}
        {experienceRows.length > 0 && (
          <div>
            <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5 flex items-center gap-1">
              <Briefcase className="w-3 h-3" />
              Experience
            </div>
            <div className="flex flex-col gap-1">
              {experienceRows.map((c: any, i: number) => {
                const title = String(c?.title || c?.job_title || "").trim();
                const company = String(c?.company || c?.company_name || "").trim();
                const start = String(c?.start || c?.start_date || "").trim();
                const end = String(c?.end || c?.end_date || "").trim();
                const lhs = title && company
                  ? <><span>{title}</span> <span className="text-slate-400">at</span> <span className="font-semibold">{company}</span></>
                  : <span className="font-semibold">{title || company || "(role)"}</span>;
                const rhs = [start, end].filter(Boolean).join(" – ");
                return (
                  <div
                    key={`${title}-${company}-${i}`}
                    className="text-[12px] text-slate-700 truncate"
                    title={`${title} at ${company}${rhs ? ` · ${rhs}` : ""}`}
                  >
                    {lhs}
                    {rhs && <span className="text-slate-400"> · {rhs}</span>}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* SKILLS — matched (emerald) + missing (rose) */}
        {(matched.length > 0 || missing.length > 0) && (
          <div className="grid grid-cols-2 gap-3">
            {matched.length > 0 && (
              <div>
                <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">
                  Skills matched ({matched.length})
                </div>
                <div className="flex flex-wrap gap-1">
                  {matched.slice(0, 6).map((skill, i) => (
                    <span
                      key={`${skill}-${i}`}
                      className="px-2 py-0.5 rounded-md bg-emerald-50 text-emerald-700 text-[11px] font-semibold border border-emerald-100"
                    >
                      {skill}
                    </span>
                  ))}
                  {matched.length > 6 && (
                    <span className="px-2 py-0.5 rounded-md bg-emerald-100 text-emerald-700 text-[11px] font-semibold">
                      +{matched.length - 6}
                    </span>
                  )}
                </div>
              </div>
            )}
            {missing.length > 0 && (
              <div>
                <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">
                  Missing
                </div>
                <div className="flex flex-wrap gap-1">
                  {missing.map((skill, i) => (
                    <span
                      key={`${skill}-${i}`}
                      className="px-2 py-0.5 rounded-md bg-rose-50 text-rose-700 text-[11px] font-semibold border border-rose-100"
                    >
                      {skill}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* EDUCATION */}
        {educationStr && (
          <div className="text-[11.5px] text-slate-700 inline-flex items-start gap-1">
            <GraduationCap className="w-3.5 h-3.5 text-slate-400 mt-0.5 shrink-0" />
            <span>{educationStr}</span>
          </div>
        )}

        {/* FIT RATIONALE — Exa preferred, fall back to legacy explainability */}
        {(exaFitRationale || firstExplain) && (
          <div className="text-[12px] text-slate-600 leading-snug border-t border-slate-100 pt-2.5">
            <div className="text-[10px] font-bold uppercase tracking-wider text-violet-600 mb-1 inline-flex items-center gap-1">
              <Sparkles className="w-3 h-3" />
              {exaFitRationale ? "Fit rationale (Exa Agent)" : "Why this matches"}
            </div>
            {exaFitRationale || firstExplain}
          </div>
        )}
      </div>
    </div>
  );
}
