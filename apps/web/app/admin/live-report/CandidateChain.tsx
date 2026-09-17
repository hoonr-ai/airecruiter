"use client";

import React, { memo } from "react";
import {
  Phone,
  Check,
  Voicemail,
  PhoneMissed,
  PhoneOff,
  UserX,
  AlertTriangle,
  X,
  Clock,
  Mail,
  MessageSquare,
} from "lucide-react";
import type { CandidateRow, CallOutcome } from "./types";
import { CHAIN_STOPS, phaseToStopIndex, TERMINAL_REASON_LABEL } from "./types";

interface CandidateChainProps {
  candidate: CandidateRow;
}

const OUTCOME_ICONS: Record<CallOutcome, React.ReactNode> = {
  answered: <Phone className="h-3 w-3 text-emerald-600" />,
  completed: <Check className="h-3 w-3 text-emerald-600" />,
  voicemail: <Voicemail className="h-3 w-3 text-amber-600" />,
  no_answer: <PhoneMissed className="h-3 w-3 text-slate-500" />,
  busy: <PhoneOff className="h-3 w-3 text-amber-600" />,
  candidate_hangup: <UserX className="h-3 w-3 text-rose-500" />,
  system_drop: <AlertTriangle className="h-3 w-3 text-rose-600" />,
  failed: <X className="h-3 w-3 text-rose-600" />,
  in_progress: <Phone className="h-3 w-3 text-indigo-600 animate-pulse" />,
};

export const CandidateChain: React.FC<CandidateChainProps> = memo(({ candidate }) => {
  const currentStopIndex = phaseToStopIndex(candidate.phase);
  const isTerminal = currentStopIndex === 6;

  return (
    <div className="flex items-center justify-between py-2 px-3 border-b border-slate-100 last:border-b-0 hover:bg-slate-50/70 transition-colors text-xs">
      {/* Candidate Identifier */}
      <div className="w-44 shrink-0 flex flex-col justify-center">
        <div className="flex items-center gap-1.5 font-medium text-slate-800 truncate">
          <span>{candidate.name}</span>
          {candidate.is_stuck && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-800 border border-amber-300">
              Stuck {candidate.idle_minutes ? `(${candidate.idle_minutes}m)` : ""}
            </span>
          )}
          {candidate.awaiting_retry && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-50 text-blue-700 border border-blue-200">
              Next try soon
            </span>
          )}
        </div>
        <div className="text-[11px] text-slate-400">#{candidate.interview_id}</div>
      </div>

      {/* Animated Node Chain (Stops) */}
      <div className="flex-1 max-w-xl mx-4 flex items-center justify-between relative">
        {CHAIN_STOPS.map((stop, idx) => {
          const isPassed = idx < currentStopIndex;
          const isCurrent = idx === currentStopIndex;

          return (
            <React.Fragment key={stop.key}>
              <div className="flex flex-col items-center z-10">
                <div
                  className={`w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-semibold border transition-all ${
                    isPassed
                      ? "bg-indigo-600 border-indigo-700 text-white"
                      : isCurrent
                      ? "bg-white border-indigo-600 text-indigo-600 ring-4 ring-indigo-100 animate-pulse"
                      : "bg-slate-100 border-slate-200 text-slate-400"
                  }`}
                >
                  {isPassed ? <Check className="h-3 w-3" /> : stop.short}
                </div>
                <span className="text-[9px] mt-1 text-slate-500">{stop.short}</span>
              </div>
              {idx < CHAIN_STOPS.length - 1 && (
                <div
                  className={`flex-1 h-0.5 mx-1 transition-all ${
                    idx < currentStopIndex ? "bg-indigo-500" : "bg-slate-200"
                  }`}
                />
              )}
            </React.Fragment>
          );
        })}
      </div>

      {/* Outcome & Counters */}
      <div className="w-56 shrink-0 flex items-center justify-end gap-3">
        {/* Comms counts */}
        <div className="flex items-center gap-2 text-[11px] text-slate-500">
          {(candidate.call_attempts ?? 0) > 0 && (
            <span className="flex items-center gap-0.5" title="Call attempts">
              <Phone className="h-3 w-3 text-slate-400" />
              {candidate.call_attempts}
            </span>
          )}
          {(candidate.sms_sent ?? 0) > 0 && (
            <span className="flex items-center gap-0.5" title="SMS sent">
              <MessageSquare className="h-3 w-3 text-slate-400" />
              {candidate.sms_sent}
            </span>
          )}
        </div>

        {/* Outcome Badge */}
        {candidate.call_outcome && (
          <div className="flex items-center gap-1 px-2 py-0.5 rounded border border-slate-200 bg-white text-[11px] font-medium shadow-xs">
            {OUTCOME_ICONS[candidate.call_outcome] || null}
            <span className="capitalize">{candidate.call_outcome.replace("_", " ")}</span>
          </div>
        )}

        {/* Terminal Badge if finished */}
        {candidate.terminal_reason && (
          <span
            className={`px-2 py-0.5 rounded text-[11px] font-medium border ${
              candidate.terminal_reason === "passed"
                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                : "bg-slate-100 text-slate-700 border-slate-200"
            }`}
          >
            {TERMINAL_REASON_LABEL[candidate.terminal_reason] || candidate.terminal_reason}
          </span>
        )}
      </div>
    </div>
  );
});

CandidateChain.displayName = "CandidateChain";
