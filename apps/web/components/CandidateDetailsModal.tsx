"use client";

import { Button } from "@/components/ui/button";
import { 
  Linkedin, 
  Check, 
  MapPin, 
  Briefcase,
  Sparkles,
  Link,
  TrendingUp,
  AlertCircle,
  Clock
} from "lucide-react";
import { 
  Dialog, 
  DialogContent, 
  DialogTitle,
  DialogDescription
} from "@/components/ui/dialog";

import { memo } from "react";

interface CandidateDetailsModalProps {
  isOpen: boolean;
  onClose: () => void;
  candidateName: string;
  profileUrl?: string;
  imageUrl?: string;
  jobTitle?: string;
  location?: string;
  experienceYears?: number | string | null;
  tags?: string[];
  matchScore?: number;
  missingSkills?: string[];
  matchedSkills?: string[];
  matchScoreDetails?: Record<string, any>;
  explainability?: string[];
}

/** Title-case a string: "cloud security engineer" → "Cloud Security Engineer" */
function toTitleCase(str: string): string {
  if (!str) return "";
  return str
    .toLowerCase()
    .split(" ")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

/** Proper location format: "atlanta, ga" → "Atlanta, GA" */
function formatLocation(loc: string): string {
  if (!loc) return "";
  const parts = loc.split(",").map((p) => p.trim());
  if (parts.length >= 2) {
    const city = toTitleCase(parts[0]);
    const stateZip = parts.slice(1).join(", ").toUpperCase().trim();
    return `${city}, ${stateZip}`;
  }
  return toTitleCase(loc);
}

function ScoreRing({ score }: { score: number }) {
  const radius = 44;
  const stroke = 7;
  const normalizedRadius = radius - stroke / 2;
  const circumference = 2 * Math.PI * normalizedRadius;
  const progress = Math.min(Math.max(score, 0), 100) / 100;
  const strokeDashoffset = circumference * (1 - progress);

  const color =
    score >= 85 ? "#10b981" : score >= 65 ? "#f59e0b" : "#f43f5e";
  const bgColor =
    score >= 85 ? "#d1fae5" : score >= 65 ? "#fef3c7" : "#ffe4e6";
  const label =
    score >= 85 ? "Excellent" : score >= 65 ? "Good Fit" : "Low Match";

  return (
    <div className="flex flex-col items-center gap-1">
      <div className="relative" style={{ width: radius * 2, height: radius * 2 }}>
        <svg width={radius * 2} height={radius * 2} style={{ transform: "rotate(-90deg)" }}>
          <circle
            cx={radius}
            cy={radius}
            r={normalizedRadius}
            fill="none"
            stroke={bgColor}
            strokeWidth={stroke}
          />
          <circle
            cx={radius}
            cy={radius}
            r={normalizedRadius}
            fill="none"
            stroke={color}
            strokeWidth={stroke}
            strokeDasharray={circumference}
            strokeDashoffset={strokeDashoffset}
            strokeLinecap="round"
            style={{ transition: "stroke-dashoffset 0.8s cubic-bezier(0.4,0,0.2,1)" }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-[22px] font-black leading-none" style={{ color }}>{score}</span>
          <span className="text-[9px] font-bold text-slate-400 uppercase tracking-widest mt-0.5">/ 100</span>
        </div>
      </div>
      <span className="text-[10px] font-bold uppercase tracking-widest" style={{ color }}>{label}</span>
    </div>
  );
}

function CategoryBar({ label, score, weight }: { label: string; score: number; weight: number }) {
  const pct = weight > 0 ? Math.round((score / weight) * 100) : 0;
  const clampedPct = Math.min(pct, 100);
  const barColor =
    clampedPct >= 75 ? "bg-emerald-500" : clampedPct >= 45 ? "bg-amber-400" : "bg-rose-400";
  const textColor =
    clampedPct >= 75 ? "text-emerald-600" : clampedPct >= 45 ? "text-amber-600" : "text-rose-500";

  return (
    <div className="flex items-center gap-3">
      <span className="text-[12px] font-semibold text-slate-600 w-32 shrink-0 truncate">{label}</span>
      <div className="flex-1 h-[7px] bg-slate-100 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full ${barColor} transition-all duration-700 ease-out`}
          style={{ width: `${clampedPct}%` }}
        />
      </div>
      <span className={`text-[12px] font-bold w-10 text-right tabular-nums ${textColor}`}>{clampedPct}%</span>
    </div>
  );
}

function CandidateDetailsModalBase({
  isOpen,
  onClose,
  candidateName,
  profileUrl,
  imageUrl,
  jobTitle,
  location,
  experienceYears,
  tags,
  matchScore = 0,
  missingSkills,
  matchedSkills,
  matchScoreDetails,
  explainability,
}: CandidateDetailsModalProps) {
  const isLinkedIn = profileUrl?.includes("linkedin.com");

  const formattedTitle = toTitleCase(jobTitle || "");
  const formattedLocation = formatLocation(location || "");
  const formattedYears = experienceYears
    ? `${experienceYears}${typeof experienceYears === "number" ? "+ yrs" : ""}`
    : null;

  const scoreEntries = matchScoreDetails
    ? Object.entries(matchScoreDetails).filter(([, d]: [string, any]) => d?.weight > 0)
    : [];

  const topMatches = (matchedSkills || []).slice(0, 8);
  const topMissing = (missingSkills || []).slice(0, 8);
  const summary = explainability?.[0];
  const initials = candidateName
    .split(" ")
    .map((n) => n[0])
    .join("")
    .substring(0, 2)
    .toUpperCase();

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-xl bg-white rounded-[20px] shadow-2xl border border-slate-100 p-0 overflow-hidden flex flex-col max-h-[88vh]">
        <div className="sr-only">
          <DialogTitle>Candidate Match Report — {candidateName}</DialogTitle>
          <DialogDescription>AI-generated candidate match report for {candidateName}.</DialogDescription>
        </div>

        {/* ── Header ── */}
        <div className="px-6 pt-6 pb-5 flex items-start gap-4 border-b border-slate-100 bg-white">
          {/* Avatar */}
          <div className="relative shrink-0">
            {imageUrl ? (
              <img
                src={imageUrl}
                alt={candidateName}
                className="w-[52px] h-[52px] rounded-full object-cover ring-2 ring-white shadow"
              />
            ) : (
              <div className="w-[52px] h-[52px] rounded-full bg-gradient-to-br from-indigo-100 to-purple-100 flex items-center justify-center text-sm font-bold text-indigo-600 ring-2 ring-white shadow">
                {initials}
              </div>
            )}
            {isLinkedIn && (
              <div className="absolute -bottom-0.5 -right-0.5 w-[18px] h-[18px] bg-[#0077B5] rounded-full flex items-center justify-center border-2 border-white">
                <Linkedin className="w-2.5 h-2.5 text-white fill-current" />
              </div>
            )}
          </div>

          {/* Name + meta */}
          <div className="flex-1 min-w-0 pt-0.5">
            <h2 className="text-[18px] font-bold text-slate-900 leading-tight truncate">{candidateName}</h2>
            <div className="flex items-center gap-2 mt-1.5 text-[12px] text-slate-500 font-medium flex-wrap">
              {formattedTitle && (
                <span className="flex items-center gap-1.5 bg-slate-50 px-2 py-0.5 rounded-md">
                  <Briefcase className="w-3 h-3 text-slate-400" />
                  {formattedTitle}
                </span>
              )}
              {formattedLocation && (
                <span className="flex items-center gap-1.5 bg-slate-50 px-2 py-0.5 rounded-md">
                  <MapPin className="w-3 h-3 text-slate-400" />
                  {formattedLocation}
                </span>
              )}
              {formattedYears && (
                <span className="flex items-center gap-1.5 bg-slate-50 px-2 py-0.5 rounded-md">
                  <Clock className="w-3 h-3 text-slate-400" />
                  {formattedYears}
                </span>
              )}
            </div>
          </div>

          {/* Score ring */}
          <div className="shrink-0 ml-1">
            <ScoreRing score={matchScore} />
          </div>
        </div>

        {/* ── Scrollable body ── */}
        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5 bg-[#fafafa]">

          {/* AI Summary */}
          {summary && (
            <div className="flex items-start gap-2.5 bg-indigo-50/80 border border-indigo-100 rounded-xl px-4 py-3">
              <Sparkles className="w-3.5 h-3.5 text-indigo-500 mt-0.5 shrink-0" />
              <p className="text-[12.5px] text-slate-700 leading-relaxed font-medium">{summary}</p>
            </div>
          )}

          {/* Score breakdown */}
          {scoreEntries.length > 0 && (
            <div className="bg-white rounded-xl border border-slate-100 shadow-sm px-5 py-4 space-y-3">
              <div className="flex items-center gap-2 mb-1">
                <TrendingUp className="w-3.5 h-3.5 text-slate-400" />
                <span className="text-[11px] font-bold text-slate-400 uppercase tracking-widest">Score Breakdown</span>
              </div>
              {scoreEntries.map(([cat, data]: [string, any], i) => (
                <CategoryBar key={i} label={cat} score={data.score ?? 0} weight={data.weight ?? 1} />
              ))}
            </div>
          )}

          {/* Skill audit — two columns */}
          {(topMatches.length > 0 || topMissing.length > 0) && (
            <div className="grid grid-cols-2 gap-3">
              {/* Matched */}
              <div className="bg-white rounded-xl border border-slate-100 shadow-sm p-4">
                <div className="flex items-center gap-1.5 mb-3">
                  <div className="w-4 h-4 rounded-full bg-emerald-100 flex items-center justify-center">
                    <Check className="w-2.5 h-2.5 text-emerald-600" />
                  </div>
                  <span className="text-[10px] font-bold text-emerald-600 uppercase tracking-widest">Matched</span>
                </div>
                <ul className="space-y-1.5">
                  {topMatches.length > 0 ? topMatches.map((s, i) => (
                    <li key={i} className="text-[12px] text-slate-700 font-medium leading-snug flex items-start gap-1.5">
                      <span className="text-emerald-400 mt-0.5 shrink-0">•</span>
                      <span>{s}</span>
                    </li>
                  )) : (
                    <li className="text-[11px] text-slate-400 italic">None recorded</li>
                  )}
                </ul>
              </div>

              {/* Missing */}
              <div className="bg-white rounded-xl border border-slate-100 shadow-sm p-4">
                <div className="flex items-center gap-1.5 mb-3">
                  <div className="w-4 h-4 rounded-full bg-rose-100 flex items-center justify-center">
                    <AlertCircle className="w-2.5 h-2.5 text-rose-500" />
                  </div>
                  <span className="text-[10px] font-bold text-rose-500 uppercase tracking-widest">Gaps</span>
                </div>
                <ul className="space-y-1.5">
                  {topMissing.length > 0 ? topMissing.map((s, i) => (
                    <li key={i} className="text-[12px] text-slate-700 font-medium leading-snug flex items-start gap-1.5">
                      <span className="text-rose-300 mt-0.5 shrink-0">•</span>
                      <span>{s}</span>
                    </li>
                  )) : (
                    <li className="text-[11px] text-slate-500 font-medium">Meets all requirements ✓</li>
                  )}
                </ul>
              </div>
            </div>
          )}

          {/* Keyword chips */}
          {tags && tags.length > 0 && (
            <div className="flex flex-wrap gap-1.5 pt-1">
              {tags.slice(0, 12).map((tag, i) => (
                <span
                  key={i}
                  className="px-2.5 py-1 rounded-md bg-white border border-slate-200 text-[11px] font-semibold text-slate-500 shadow-sm"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* ── Footer ── */}
        <div className="px-5 py-3.5 bg-white border-t border-slate-100 flex items-center justify-between">
          {profileUrl ? (
            <a
              href={profileUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-indigo-600 bg-indigo-50 hover:bg-indigo-100 px-3.5 py-2 rounded-lg transition-colors"
            >
              <Link className="w-3.5 h-3.5" />
              View Profile
            </a>
          ) : <div />}

          <Button
            onClick={onClose}
            variant="ghost"
            className="text-slate-400 hover:text-slate-700 text-[13px] font-semibold px-5 rounded-lg h-9"
          >
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export const CandidateDetailsModal = memo(CandidateDetailsModalBase);
