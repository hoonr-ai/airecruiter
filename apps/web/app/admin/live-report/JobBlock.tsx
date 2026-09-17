"use client";

import React, { useState } from "react";
import { ChevronDown, ChevronRight, Briefcase } from "lucide-react";
import type { JobBlockData } from "./types";
import { CandidateChain } from "./CandidateChain";

interface JobBlockProps {
  job: JobBlockData;
}

export const JobBlock: React.FC<JobBlockProps> = ({ job }) => {
  const [isCollapsed, setIsCollapsed] = useState<boolean>(false);

  const completedCount = job.candidates.filter(
    (c) => c.phase === "completed" || c.terminal_reason
  ).length;

  return (
    <div className="border border-slate-200 rounded-xl bg-white shadow-xs overflow-hidden mb-4">
      {/* Job Header */}
      <button
        type="button"
        onClick={() => setIsCollapsed((prev) => !prev)}
        className="w-full flex items-center justify-between p-4 bg-slate-50/70 hover:bg-slate-100/70 transition-colors text-left"
      >
        <div className="flex items-center gap-2.5">
          {isCollapsed ? (
            <ChevronRight className="h-4 w-4 text-slate-500" />
          ) : (
            <ChevronDown className="h-4 w-4 text-slate-500" />
          )}
          <Briefcase className="h-4 w-4 text-indigo-600" />
          <div>
            <span className="font-semibold text-sm text-slate-900">
              {job.title || "Job Title"}
            </span>
            {job.jobdiva_id && (
              <span className="ml-2 text-xs text-slate-400 font-mono">
                #{job.jobdiva_id}
              </span>
            )}
            {job.customer_name && (
              <span className="ml-2 text-xs text-slate-500 font-medium">
                • {job.customer_name}
              </span>
            )}
          </div>
        </div>

        {/* Progress pill */}
        <div className="flex items-center gap-2 text-xs">
          <span className="text-slate-500 font-medium">
            {completedCount} / {job.candidates.length} processed
          </span>
          <div className="w-20 bg-slate-200 rounded-full h-2 overflow-hidden">
            <div
              className="bg-indigo-600 h-full rounded-full transition-all"
              style={{
                width: `${
                  job.candidates.length > 0
                    ? (completedCount / job.candidates.length) * 100
                    : 0
                }%`,
              }}
            />
          </div>
        </div>
      </button>

      {/* Candidate Rows */}
      {!isCollapsed && (
        <div className="divide-y divide-slate-100 p-1">
          {job.candidates.map((c) => (
            <CandidateChain key={c.interview_id} candidate={c} />
          ))}
          {job.candidates.length === 0 && (
            <div className="py-4 text-center text-xs text-slate-400">
              No candidates found in this launch block.
            </div>
          )}
        </div>
      )}
    </div>
  );
};
