"use client";

import React, { useMemo, useState } from "react";
import { Briefcase, ChevronDown } from "lucide-react";
import type { JobBlockData } from "./types";
import { TERMINAL_PHASES } from "./types";
import { CandidateChain } from "./CandidateChain";

const PHASE_SEGMENTS: Array<{ keys: string[]; label: string; color: string }> = [
  { keys: ["contact_check"], label: "CC", color: "#C8BFEA" },
  { keys: ["phase1"], label: "P1", color: "#897ECF" },
  { keys: ["phase1_6hr"], label: "P2", color: "#6B5FD3" },
  { keys: ["phase2"], label: "P3", color: "#6B4FBB" },
  { keys: ["phase3"], label: "P4", color: "#2B2742" },
  { keys: ["phase1_extra", "phase1_6hr_extra", "phase2_extra", "high_score_extra"], label: "Extra", color: "#f59e0b" },
  { keys: ["pass", "completed"], label: "Passed", color: "#10b981" },
  { keys: ["pending"], label: "No response", color: "#94a3b8" },
  { keys: ["fail", "outreach_failed", "outreach_incomplete"], label: "Failed", color: "#ef4444" },
];

interface JobBlockProps {
  job: JobBlockData;
  defaultCollapsed?: boolean;
}

export const JobBlock: React.FC<JobBlockProps> = ({ job, defaultCollapsed = false }) => {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);

  const { segments, allPhases, terminal, total } = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const c of job.candidates) counts[c.phase] = (counts[c.phase] ?? 0) + 1;
    
    // Core pipeline phases to always track: CC, P1, P2, P3, P4, Extra
    const all = PHASE_SEGMENTS.map((s) => ({
      ...s,
      n: s.keys.reduce((acc, k) => acc + (counts[k] ?? 0), 0),
    }));

    // Only segments with n > 0 draw on the segmented progress bar
    const segs = all.filter((s) => s.n > 0);

    const term = job.candidates.filter(
      (c) => TERMINAL_PHASES.has(c.phase) || c.terminal_reason
    ).length;
    return { segments: segs, allPhases: all, terminal: term, total: job.candidates.length };
  }, [job.candidates]);

  return (
    <section className="rounded-xl border border-slate-200 bg-white shadow-xs overflow-hidden mb-4">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className="group flex w-full items-center gap-4 rounded-t-xl px-4 py-3 text-left transition-colors duration-200 hover:bg-slate-50/70"
        aria-expanded={!collapsed}
      >
        {/* Job Icon box */}
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-indigo-600 to-indigo-800 text-white shadow-xs">
          <Briefcase size={16} />
        </span>

        {/* Job Title and Diva ID */}
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="shrink-0 rounded border border-indigo-200 bg-indigo-50 px-1.5 font-mono text-[11px] font-semibold leading-[18px] tracking-wide text-indigo-700">
              {job.jobdiva_id ?? "—"}
            </span>
            <span className="truncate text-sm font-semibold leading-6 tracking-tight text-slate-900">
              {job.title ?? "Untitled job"}
            </span>
          </span>
          <span className="block truncate text-xs text-slate-500">
            {job.customer_name ?? "Unknown customer"}
          </span>
        </span>

        {/* Phase summary segmented bar with width scaled proportionally to candidate count */}
        <span className="hidden w-56 shrink-0 md:block">
          <span className="flex h-2.5 w-full gap-[2px] overflow-hidden rounded-full bg-slate-100 p-[1px]">
            {total === 0 || segments.length === 0 ? (
              <span className="h-full w-full bg-slate-200 rounded-full" />
            ) : (
              segments.map((s) => {
                const pct = total > 0 ? (s.n / total) * 100 : 0;
                return (
                  <span
                    key={s.label}
                    title={`${s.label}: ${s.n} (${Math.round(pct)}%)`}
                    className="h-full transition-all duration-700"
                    style={{
                      width: `${pct}%`,
                      background: s.color,
                    }}
                  />
                );
              })
            )}
          </span>
          <span className="mt-1 flex justify-between text-[10px] leading-3 text-slate-500">
            {(() => {
              // Always display core progression milestones in order: CC, P1, P2, P3, P4
              const coreLabels = ["CC", "P1", "P2", "P3", "P4"];
              const coreParts = coreLabels.map((lbl) => {
                const found = allPhases.find((p) => p.label === lbl);
                return `${lbl}: ${found ? found.n : 0}`;
              });

              // Also append any non-zero Extra or terminal segments
              const extraSegment = allPhases.find((p) => p.label === "Extra");
              if (extraSegment && extraSegment.n > 0) {
                coreParts.push(`Ex: ${extraSegment.n}`);
              }

              const fullText = coreParts.join(" · ");
              return (
                <span className="truncate" title={fullText}>
                  {fullText}
                </span>
              );
            })()}
          </span>
        </span>

        {/* Terminal counts */}
        <span className="shrink-0 text-right">
          <span className="block text-base font-semibold tabular-nums tracking-tight text-slate-900">
            {terminal}
            <span className="font-normal text-slate-400">/{total}</span>
          </span>
          <span className="block text-[10px] uppercase tracking-wider text-slate-400 font-medium">done</span>
        </span>

        {/* Expand toggle */}
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-transparent text-slate-400 transition-colors duration-200 group-hover:border-slate-200 group-hover:bg-white group-hover:text-slate-600">
          <ChevronDown
            size={16}
            className={`transition-transform duration-300 ${collapsed ? "-rotate-90" : ""}`}
          />
        </span>
      </button>

      {/* Candidate Chains Container */}
      {!collapsed && (
        <div className="lr-scroll max-h-[400px] overflow-y-auto border-t border-slate-100">
          {/* Table Header with explicit 4 Columns matching layout */}
          <div className="grid grid-cols-[150px_1fr_170px_150px] items-center gap-3 bg-slate-50/80 px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <div>PROFILE</div>
            <div className="px-1 text-center">PROGRESS PIPELINE</div>
            <div className="pl-1">HISTORY LOGS</div>
            <div className="text-right pr-2">ACTION / CTA</div>
          </div>
          <div className="divide-y divide-slate-100 px-1 py-1">
            {job.candidates.map((c) => (
              <CandidateChain key={c.interview_id} candidate={c} />
            ))}
            {job.candidates.length === 0 && (
              <div className="py-6 text-center text-xs text-slate-400">
                No candidates found in this job.
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
};

export default JobBlock;
