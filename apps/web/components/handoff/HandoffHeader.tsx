"use client";

import { Clock } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ScoreBadge } from "@/components/ScoreBadge";
import { hardFilterLabel } from "@/components/handoff/HandoffInfoTabs";
import type { HandoffContext } from "@/lib/api";

/** Candidate · role · AI score · timer, with page actions on the right. */
export function HandoffHeader({
  context,
  fallbackName,
  timerLabel,
  timer,
  children,
}: {
  context: HandoffContext | null | undefined;
  fallbackName?: string | null;
  timerLabel?: string;
  timer?: string | null;
  children?: React.ReactNode;
}) {
  const name = context?.candidate_name || fallbackName || "Candidate";
  const score = typeof context?.overall_score === "number" ? context.overall_score : null;
  const hf = hardFilterLabel(context?.hard_filter);
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-bold text-slate-900 truncate">{name}</h1>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-600">
          {context?.role_position && <span>{context.role_position}</span>}
          {score !== null && (
            <span className="inline-flex items-center gap-1">
              AI score <ScoreBadge score={score} />
            </span>
          )}
          {hf && (
            <Badge variant="outline" className={hf.ok ? "text-emerald-700" : "text-red-700"}>
              {hf.text}
            </Badge>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {timer && (
          <span className="inline-flex items-center gap-1.5 text-sm font-medium text-slate-700 tabular-nums">
            <Clock className="w-4 h-4 text-slate-400" aria-hidden="true" />
            {timerLabel && <span className="text-slate-500 font-normal">{timerLabel}</span>}
            {timer}
          </span>
        )}
        {children}
      </div>
    </div>
  );
}
