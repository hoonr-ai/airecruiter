"use client";

import React, { useMemo } from "react";
import { ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface ResumeModalProps {
  isOpen: boolean;
  onClose: () => void;
  candidateName: string;
  resumeText: string;
  keywords?: string[];
  similarKeywords?: string[];
  // For JobDiva-sourced candidates, render a "View on JobDiva" link in the
  // header that deep-links to the candidate record. Same URL format as
  // CandidateDetailsModal and the rankings list row.
  jobdivaCandidateId?: string;
  source?: string;
}

type Tier = "primary" | "similar";

const escapeRegex = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

interface KeywordIndex {
  regex: RegExp;
  tierByLower: Map<string, Tier>;
}

function buildKeywordIndex(
  primary: string[] | undefined,
  similar: string[] | undefined
): KeywordIndex | null {
  const tierByLower = new Map<string, Tier>();
  const cleanList = (list: string[] | undefined): string[] =>
    Array.from(
      new Set(
        (list || [])
          .map((k) => (k || "").trim())
          .filter((k) => k.length >= 2 && k.length <= 60)
      )
    );

  cleanList(primary).forEach((k) => tierByLower.set(k.toLowerCase(), "primary"));
  cleanList(similar).forEach((k) => {
    const lower = k.toLowerCase();
    if (!tierByLower.has(lower)) tierByLower.set(lower, "similar");
  });

  if (tierByLower.size === 0) return null;

  const all = Array.from(tierByLower.keys()).sort((a, b) => b.length - a.length);
  const patterns = all.map((k) => {
    const esc = escapeRegex(k);
    const wrap = /^\w.*\w$|^\w$/.test(k);
    return wrap ? `\\b${esc}\\b` : esc;
  });
  return {
    regex: new RegExp(`(${patterns.join("|")})`, "gi"),
    tierByLower,
  };
}

function highlightLine(line: string, idx: KeywordIndex | null): React.ReactNode {
  if (!idx) return line;
  const { regex, tierByLower } = idx;
  const parts: React.ReactNode[] = [];
  let last = 0;
  regex.lastIndex = 0;
  for (const m of line.matchAll(regex)) {
    const start = m.index ?? 0;
    if (start > last) parts.push(line.slice(last, start));
    const tier = tierByLower.get(m[0].toLowerCase()) ?? "primary";
    const cls =
      tier === "similar"
        ? "bg-sky-100 text-sky-900 rounded px-0.5 font-semibold"
        : "bg-yellow-200 text-slate-900 rounded px-0.5 font-semibold";
    parts.push(
      <mark key={`${start}-${m[0]}`} className={cls}>
        {m[0]}
      </mark>
    );
    last = start + m[0].length;
  }
  if (parts.length === 0) return line;
  if (last < line.length) parts.push(line.slice(last));
  return <>{parts}</>;
}

export function ResumeModal({
  isOpen,
  onClose,
  candidateName,
  resumeText,
  keywords,
  similarKeywords,
  jobdivaCandidateId,
  source,
}: ResumeModalProps) {
  const showJobDivaLink =
    !!jobdivaCandidateId &&
    typeof source === "string" &&
    source.toLowerCase().startsWith("jobdiva");
  const jobDivaUrl = showJobDivaLink
    ? `https://www1.jobdiva.com/employers/myreports/viewcandidate2_real.jsp?docids=-1&candidateid=${encodeURIComponent(jobdivaCandidateId!)}`
    : null;
  const index = useMemo(
    () => buildKeywordIndex(keywords, similarKeywords),
    [keywords, similarKeywords]
  );

  const { matchedPrimary, matchedSimilar } = useMemo(() => {
    if (!index || !resumeText) return { matchedPrimary: [] as string[], matchedSimilar: [] as string[] };
    index.regex.lastIndex = 0;
    const primary = new Set<string>();
    const similar = new Set<string>();
    for (const m of resumeText.matchAll(index.regex)) {
      const lower = m[0].toLowerCase();
      const tier = index.tierByLower.get(lower) ?? "primary";
      (tier === "similar" ? similar : primary).add(lower);
    }
    return {
      matchedPrimary: Array.from(primary).slice(0, 30),
      matchedSimilar: Array.from(similar).slice(0, 30),
    };
  }, [index, resumeText]);

  const formatResumeText = (text: string) => {
    if (
      !text ||
      text.trim() === "" ||
      text === "Resume content unavailable." ||
      text === "null" ||
      text.toLowerCase().includes("resume not available") ||
      text.toLowerCase().includes("content unavailable")
    ) {
      return (
        <div className="text-slate-500 italic text-center py-8">
          <p className="mb-4">Resume content is not available for this candidate.</p>
          <p className="text-sm">This may occur if:</p>
          <ul className="text-sm mt-2 space-y-1">
            <li>• The candidate hasn&apos;t uploaded a resume</li>
            <li>• Resume access is restricted</li>
            <li>• Data sync is still in progress</li>
          </ul>
        </div>
      );
    }

    const lines = text.split("\n");
    const formattedContent = [];

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line) continue;

      const isHeader =
        /^[A-Z\s]{3,}$/.test(line) ||
        /^(SUMMARY|EXPERIENCE|EDUCATION|SKILLS|OBJECTIVE|QUALIFICATION|WORK|EMPLOYMENT|PROJECTS|CERTIFICATIONS|ACHIEVEMENTS)/i.test(line);

      if (isHeader) {
        formattedContent.push(
          <h3 key={i} className="text-lg font-bold text-slate-900 mt-6 mb-3 border-b border-slate-200 pb-2">
            {highlightLine(line, index)}
          </h3>
        );
      } else if (line.includes("@") && line.includes(".")) {
        formattedContent.push(
          <p key={i} className="text-sm text-blue-600 mb-2">
            {highlightLine(line, index)}
          </p>
        );
      } else if (/^\(\d{3}\)|\d{3}-\d{3}-\d{4}/.test(line)) {
        formattedContent.push(
          <p key={i} className="text-sm text-slate-600 mb-2 font-medium">
            {highlightLine(line, index)}
          </p>
        );
      } else if (line.startsWith("•") || line.startsWith("-") || line.startsWith("*")) {
        formattedContent.push(
          <p key={i} className="text-sm text-slate-700 mb-1 ml-4">
            {highlightLine(line, index)}
          </p>
        );
      } else {
        formattedContent.push(
          <p key={i} className="text-sm text-slate-700 mb-2 leading-relaxed">
            {highlightLine(line, index)}
          </p>
        );
      }
    }

    return <div className="space-y-1">{formattedContent}</div>;
  };

  const totalMatched = matchedPrimary.length + matchedSimilar.length;

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent
        className="max-w-4xl max-h-[90vh] overflow-hidden"
        onOpenAutoFocus={(e) => e.preventDefault()}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DialogHeader className="border-b border-slate-200 pb-4 mb-4">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <DialogTitle className="text-xl font-bold text-slate-900">
              Resume: {candidateName}
            </DialogTitle>
            {jobDivaUrl && (
              <a
                href={jobDivaUrl}
                target="_blank"
                rel="noopener noreferrer"
                title="Open candidate record in JobDiva"
                className="inline-flex items-center gap-1.5 text-[12px] font-semibold text-[#c2410c] bg-[#fff7ed] hover:bg-[#ffedd5] border border-[#fed7aa] px-3 py-1.5 rounded-lg transition-colors"
              >
                <ExternalLink className="w-3.5 h-3.5" />
                View on JobDiva
              </a>
            )}
          </div>
          {totalMatched > 0 && (
            <div className="flex flex-col gap-1.5 mt-2">
              {matchedPrimary.length > 0 && (
                <div className="flex items-start gap-2 flex-wrap">
                  <span className="text-[11px] font-bold text-slate-500 uppercase tracking-wider mt-1">
                    Match ({matchedPrimary.length}):
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {matchedPrimary.map((kw) => (
                      <span
                        key={`p-${kw}`}
                        className="px-2 py-0.5 rounded-md bg-yellow-100 border border-yellow-200 text-[11px] font-semibold text-yellow-900"
                      >
                        {kw}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {matchedSimilar.length > 0 && (
                <div className="flex items-start gap-2 flex-wrap">
                  <span className="text-[11px] font-bold text-slate-500 uppercase tracking-wider mt-1">
                    Similar ({matchedSimilar.length}):
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {matchedSimilar.map((kw) => (
                      <span
                        key={`s-${kw}`}
                        className="px-2 py-0.5 rounded-md bg-sky-100 border border-sky-200 text-[11px] font-semibold text-sky-900"
                      >
                        {kw}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </DialogHeader>

        <div className="overflow-y-auto max-h-[70vh] pr-2">
          <div className="bg-white rounded-lg p-6 border border-slate-200">
            {formatResumeText(resumeText)}
          </div>
        </div>

        <div className="border-t border-slate-200 pt-4 mt-4">
          <div className="flex justify-end">
            <Button onClick={onClose} variant="outline">
              Close
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
