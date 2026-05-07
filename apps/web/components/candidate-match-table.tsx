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
import {
  Linkedin,
  ShieldCheck,
  Zap,
  ChevronUp,
  ChevronDown,
  Mail,
  Clock,
  MapPin,
  Calendar,
} from "lucide-react";

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
}

const POPOVER_WIDTH = 420;
const POPOVER_ESTIMATED_HEIGHT = 280;
const POPOVER_GAP = 8;

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

function getLocationStr(c: any): string {
  if (c.location) return String(c.location);
  if (c.city || c.state) {
    return `${c.city || ""}${c.city && c.state ? ", " : ""}${c.state || ""}`;
  }
  return "";
}

function getReceivedDate(c: any): Date | null {
  const raw =
    c.received ||
    c.received_date ||
    c.receivedDate ||
    c.last_modified ||
    c.lastModified;
  if (!raw) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatReceivedShort(d: Date | null): string {
  if (!d) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "2-digit" });
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

function getSourceBadge(source: string | undefined) {
  const src = String(source || "");
  const isLinkedIn = src.startsWith("LinkedIn");
  const isJobDivaTalent = src === "JobDiva-TalentSearch";
  const isJobDiva = src.toLowerCase().startsWith("jobdiva");
  const colors = isLinkedIn
    ? "bg-[#eff6ff] text-[#1d4ed8] border-[#bfdbfe]"
    : isJobDivaTalent
      ? "bg-[#fff7ed] text-[#c2410c] border-[#fed7aa]"
      : isJobDiva
        ? "bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]"
        : "bg-slate-50 text-slate-700 border-slate-200";
  const label = isLinkedIn
    ? "LinkedIn"
    : isJobDivaTalent
      ? "JobDiva"
      : src || "JobDiva";
  const Icon = isLinkedIn ? Linkedin : isJobDivaTalent ? Zap : ShieldCheck;
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
}: Props) {
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [hoverPos, setHoverPos] = useState<{ top: number; left: number; placement: "below" | "above" } | null>(null);
  const rowRefs = useRef<Map<string, HTMLTableRowElement | null>>(new Map());

  const computePosition = (rowEl: HTMLTableRowElement) => {
    const rect = rowEl.getBoundingClientRect();
    const left = Math.min(
      Math.max(8, rect.left),
      window.innerWidth - POPOVER_WIDTH - 8
    );
    const placeBelow = rect.bottom + POPOVER_GAP + POPOVER_ESTIMATED_HEIGHT < window.innerHeight;
    const top = placeBelow ? rect.bottom + POPOVER_GAP : rect.top - POPOVER_GAP - POPOVER_ESTIMATED_HEIGHT;
    setHoverPos({ top, left, placement: placeBelow ? "below" : "above" });
  };

  // Recompute on scroll/resize while a row is hovered.
  useLayoutEffect(() => {
    if (!hoveredId) return;
    const rowEl = rowRefs.current.get(hoveredId);
    if (!rowEl) return;
    computePosition(rowEl);
    const handler = () => {
      const el = rowRefs.current.get(hoveredId);
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
              const locationStr = getLocationStr(candidate);
              const receivedDate = getReceivedDate(candidate);
              const receivedShort = formatReceivedShort(receivedDate);
              const sourceBadge = getSourceBadge(candidate.source);
              const checked = selectedIds.has(id);
              const launchedKey = `${candidate.source ?? ''}:${id}`;
              const isAlreadyLaunched = !!disabledLaunchedKeys?.has(launchedKey);

              return (
                <TableRow
                  key={id}
                  ref={(el) => {
                    if (el) rowRefs.current.set(id, el);
                    else rowRefs.current.delete(id);
                  }}
                  className={`cursor-default transition-colors ${
                    isAlreadyLaunched
                      ? "bg-slate-50 opacity-60"
                      : "hover:bg-indigo-50/30"
                  }`}
                  onMouseEnter={(e) => {
                    setHoveredId(id);
                    computePosition(e.currentTarget as HTMLTableRowElement);
                  }}
                  onMouseLeave={() => {
                    setHoveredId((prev) => (prev === id ? null : prev));
                  }}
                >
                  <TableCell className="pl-4">
                    <Checkbox
                      className="w-4 h-4 rounded border-slate-300 data-[state=checked]:bg-purple-600 data-[state=checked]:border-purple-600"
                      checked={checked}
                      disabled={isAlreadyLaunched}
                      onCheckedChange={(v) => {
                        if (isAlreadyLaunched) return;
                        onToggleSelect(id, !!v);
                      }}
                      title={isAlreadyLaunched ? "Already launched on this job" : undefined}
                    />
                  </TableCell>
                  <TableCell className="max-w-[280px]">
                    <button
                      type="button"
                      className="text-left text-[13.5px] font-semibold text-slate-900 hover:text-indigo-600 truncate block max-w-full"
                      title={`${displayName} — view resume`}
                      onClick={() => onOpenResume(candidate)}
                    >
                      {displayName}
                    </button>
                    {(candidate.title || candidate.headline) && (
                      <div className="text-[11.5px] text-slate-500 truncate" title={candidate.title || candidate.headline}>
                        {candidate.title || candidate.headline}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <PhoneIndicator
                      candidateId={id}
                      jobdivaId={jobdivaId}
                      phone={candidate.phone}
                      persist={false}
                      onSaved={(normalised) => onPhoneSaved(id, normalised)}
                    />
                  </TableCell>
                  <TableCell className="text-center">
                    {matchScore != null && tone ? (
                      <button
                        type="button"
                        onClick={() => onOpenDetails(candidate)}
                        className="inline-flex items-center justify-center w-12 h-12 rounded-full font-extrabold text-[13px] hover:scale-105 transition-transform shadow-sm"
                        style={{ backgroundColor: tone.bg, color: tone.text, border: `2px solid ${tone.ring}` }}
                        title="View match score breakdown"
                      >
                        {matchScore}%
                      </button>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1 max-w-[260px]">
                      {topMatched.length === 0 ? (
                        <span className="text-slate-300">—</span>
                      ) : (
                        topMatched.map((skill, i) => (
                          <span
                            key={`${skill}-${i}`}
                            className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 text-[11px] font-semibold border border-slate-200 truncate max-w-[110px]"
                            title={skill}
                          >
                            {skill}
                          </span>
                        ))
                      )}
                      {moreMatchedCount > 0 && (
                        <span className="px-2 py-0.5 rounded-md bg-indigo-50 text-indigo-700 text-[11px] font-semibold border border-indigo-100">
                          +{moreMatchedCount}
                        </span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    {locationStr ? (
                      <span className="inline-flex items-center gap-1 text-slate-600 truncate max-w-[170px]" title={locationStr}>
                        <MapPin className="w-3 h-3 text-slate-400 shrink-0" />
                        <span className="truncate">{locationStr}</span>
                      </span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {receivedShort ? (
                      <span className="inline-flex items-center gap-1 text-slate-600">
                        <Calendar className="w-3 h-3 text-slate-400" />
                        {receivedShort}
                      </span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </TableCell>
                  <TableCell className="pr-4">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span
                        className={`px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center gap-1 border ${sourceBadge.colors}`}
                        title={candidate.source || ""}
                      >
                        <sourceBadge.Icon className="w-2.5 h-2.5" />
                        {sourceBadge.label}
                      </span>
                      {isAlreadyLaunched && (
                        <span
                          className="px-2 py-0.5 rounded-md text-[10px] font-extrabold uppercase tracking-wider inline-flex items-center border bg-amber-50 text-amber-700 border-amber-200"
                          title="This candidate was already launched on this job"
                        >
                          Already Launched
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
  const titleStr = String(candidate.title || candidate.current_title || candidate.headline || "").trim();
  const enhanced = (candidate.enhanced_info || {}) as Record<string, any>;
  const companyExp =
    (Array.isArray(candidate.company_experience) && candidate.company_experience.length > 0
      ? candidate.company_experience
      : Array.isArray(enhanced.company_experience)
        ? enhanced.company_experience
        : []) as Array<{ company?: string; title?: string }>;
  const companyStr = String(companyExp[0]?.company || "").trim();
  const titleAtCompany = titleStr && companyStr ? `${titleStr} @ ${companyStr}` : titleStr || companyStr;

  const yearsRaw =
    candidate.experience_years ?? candidate.years_experience ?? enhanced.years_of_experience;
  const yearsNum = typeof yearsRaw === "number" ? yearsRaw : Number(yearsRaw);
  const yearsStr = Number.isFinite(yearsNum) && yearsNum > 0 ? `${yearsNum}+ yrs experience` : "";

  const matched = getMatchedSkills(candidate);
  const missing = getMissingSkills(candidate).slice(0, 5);
  const explain = Array.isArray(candidate.explainability) ? candidate.explainability : [];
  const firstExplain = typeof explain[0] === "string" ? explain[0] : explain[0]?.text || "";

  const recentAvailabilityRaw = String(
    candidate.recent_availability ||
      candidate.recentAvailability ||
      candidate.availability_status ||
      candidate.available ||
      ""
  ).trim();
  const availabilityLower = recentAvailabilityRaw.toLowerCase();
  const availabilityChip = recentAvailabilityRaw
    ? availabilityLower.includes("available") || availabilityLower.includes("open")
      ? "bg-emerald-50 text-emerald-700 border-emerald-200"
      : availabilityLower.includes("placed") ||
          availabilityLower.includes("assignment") ||
          availabilityLower.includes("employed")
        ? "bg-slate-100 text-slate-600 border-slate-200"
        : "bg-amber-50 text-amber-700 border-amber-200"
    : "";

  return (
    <div
      className="pointer-events-none fixed z-[60] rounded-xl border border-slate-200 bg-white shadow-xl p-4"
      style={{ top, left, width: POPOVER_WIDTH }}
    >
      {titleAtCompany && (
        <div className="text-[12.5px] font-semibold text-slate-800 mb-1.5 truncate" title={titleAtCompany}>
          {titleAtCompany}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 mb-3 text-[11.5px] text-slate-600">
        {yearsStr && (
          <span className="inline-flex items-center gap-1">
            <Clock className="w-3 h-3 text-slate-400" />
            {yearsStr}
          </span>
        )}
        {candidate.email && (
          <span className="inline-flex items-center gap-1 truncate max-w-[200px]" title={candidate.email}>
            <Mail className="w-3 h-3 text-slate-400 shrink-0" />
            <span className="truncate">{candidate.email}</span>
          </span>
        )}
        {recentAvailabilityRaw && (
          <span className={`px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider border ${availabilityChip}`}>
            {recentAvailabilityRaw}
          </span>
        )}
      </div>

      {matched.length > 0 && (
        <div className="mb-3">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">
            Matched skills
          </div>
          <div className="flex flex-wrap gap-1">
            {matched.map((skill, i) => (
              <span
                key={`${skill}-${i}`}
                className="px-2 py-0.5 rounded-md bg-emerald-50 text-emerald-700 text-[11px] font-semibold border border-emerald-100"
              >
                {skill}
              </span>
            ))}
          </div>
        </div>
      )}

      {missing.length > 0 && (
        <div className="mb-3">
          <div className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">
            Top missing skills
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

      {firstExplain && (
        <div className="text-[11.5px] text-slate-600 leading-snug border-t border-slate-100 pt-2">
          {firstExplain}
        </div>
      )}
    </div>
  );
}
