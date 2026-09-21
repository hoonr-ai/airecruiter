"use client";

import React, { memo, useEffect, useRef, useState } from "react";
import {
  Check,
  Mail,
  MessageSquare,
  Phone,
  Link2,
  Hourglass,
  Clock,
  PhoneOff,
  PhoneMissed,
  Voicemail,
  AlertTriangle,
  UserX,
  X,
  Minus,
} from "lucide-react";
import type { CandidateRow, CallOutcome, ActivityEvent } from "./types";
import {
  CHAIN_STOPS,
  CHANNEL_COLOR,
  OUTCOME_META,
  SCHEDULED_WAIT_EVENTS,
  TERMINAL_REASON_LABEL,
  TERMINAL_PHASES,
  isKnownPhase,
  phaseToStopIndex,
} from "./types";

const OUTCOME_ICON: Record<CallOutcome, React.ReactNode> = {
  answered: <Phone size={12} />,
  completed: <Check size={12} />,
  voicemail: <Voicemail size={12} />,
  no_answer: <PhoneMissed size={12} />,
  busy: <PhoneOff size={12} />,
  candidate_hangup: <UserX size={12} />,
  system_drop: <AlertTriangle size={12} />,
  failed: <X size={12} />,
  in_progress: <Phone size={12} />,
};

const TONE_BADGE: Record<string, string> = {
  good: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warning: "bg-amber-50 text-amber-700 border-amber-200",
  critical: "bg-rose-50 text-rose-700 border-rose-200",
  neutral: "bg-slate-50 text-slate-600 border-slate-200",
  info: "bg-indigo-50 text-indigo-700 border-indigo-200",
};

/** Small channel icon for Col 3 history logs */
const HistoryIcon: React.FC<{ type: string; subtype: string | null; failed?: boolean }> = ({
  type,
  subtype,
  failed,
}) => {
  const isWait = SCHEDULED_WAIT_EVENTS.has(type);
  const channel = isWait ? null : subtype ?? (type.includes("call") ? "call" : null);
  const color = channel ? CHANNEL_COLOR[channel] : undefined;
  let icon: React.ReactNode = <Minus size={11} />;
  if (isWait) icon = <Clock size={11} />;
  else if (channel === "email") icon = <Mail size={11} />;
  else if (channel === "sms") icon = <MessageSquare size={11} />;
  else if (channel === "call") icon = <Phone size={11} />;
  else if (type === "link_opened") icon = <Link2 size={11} />;
  else if (type.startsWith("interview_")) icon = <Check size={11} />;

  return (
    <span
      className="relative inline-flex h-6 w-6 items-center justify-center rounded-full border bg-white shadow-2xs transition-transform hover:scale-110"
      style={{ color: color ?? "#64748b", borderColor: color ?? "#e2e8f0" }}
      title={`${type}${subtype ? ` (${subtype})` : ""}${failed ? " — failed" : ""}`}
    >
      {icon}
      {failed && (
        <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full border border-white bg-rose-600" />
      )}
    </span>
  );
};

interface CandidateChainProps {
  candidate: CandidateRow;
}

export const CandidateChain: React.FC<CandidateChainProps> = memo(({ candidate }) => {
  const reached = phaseToStopIndex(candidate.phase);
  const isTerminal = TERMINAL_PHASES.has(candidate.phase);
  const passed = candidate.phase === "pass" || candidate.terminal_reason === "passed";
  const restedNoResponse = candidate.phase === "pending";
  const failedTerminal =
    isTerminal && !passed && !restedNoResponse && candidate.phase !== "completed";
  const outcome = candidate.call_outcome ? OUTCOME_META[candidate.call_outcome] : null;
  const unknownPhase = !isKnownPhase(candidate.phase);
  const idle = candidate.idle_minutes;
  const stuckDetail =
    candidate.events?.length === 0
      ? "no events"
      : idle == null
      ? "no recent events"
      : idle >= 120
      ? `idle ${Math.floor(idle / 60)}h`
      : `idle ${idle}m`;

  const neverPlaced = (candidate.handoff_expired ?? 0) > 0;

  const reasonLabel = candidate.terminal_reason
    ? TERMINAL_REASON_LABEL[candidate.terminal_reason]
    : null;
  const scoreLabel =
    candidate.overall_score != null
      ? `score ${Math.round(candidate.overall_score)}${reasonLabel ? ` · ${reasonLabel}` : ""}`
      : null;

  // Check if candidate completed the interview (passed, completed, or call completed)
  const isInterviewCompleted =
    passed ||
    candidate.phase === "completed" ||
    candidate.terminal_reason === "passed" ||
    candidate.call_outcome === "completed" ||
    (candidate.events || []).some(
      (e) =>
        e.type === "interview_completed" ||
        e.type === "call_completed" ||
        e.status === "completed" && (e.type.includes("interview") || e.type.includes("call"))
    );

  // The specific phase node where the interview was completed
  const completedStopIndex = isInterviewCompleted ? reached : -1;

  const nextAttempt = candidate.next_attempt_at
    ? new Date(candidate.next_attempt_at)
    : null;
  const nextAttemptLabel =
    nextAttempt && !Number.isNaN(nextAttempt.getTime())
      ? nextAttempt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : null;

  // Track pop animation when node completes
  const prevReached = useRef(reached);
  const [popIndex, setPopIndex] = useState<number | null>(null);
  useEffect(() => {
    if (reached > prevReached.current) {
      setPopIndex(reached);
      const t = setTimeout(() => setPopIndex(null), 500);
      prevReached.current = reached;
      return () => clearTimeout(t);
    }
    prevReached.current = reached;
  }, [reached]);

  const events = candidate.events || [];

  /**
   * Selective node coloring:
   * Only color/fill circles that got updates (or is active).
   * Leave uncontacted/un-updated phases in between as uncolored empty circles with their number.
   */
  const phaseHasEvents = (stopKey: string, stopIndex: number): boolean => {
    const count = candidate.event_counts_by_phase?.[stopKey] ?? 0;
    if (count > 0) return true;
    return events.some((e) => e.phase === stopKey);
  };

  // Col 3 distinct event list (Email, Phone, SMS) + total counter
  const totalEvents = candidate.event_count ?? events.length;

  return (
    <div
      className={`grid h-16 grid-cols-[150px_1fr_170px_150px] items-center gap-3 px-4 py-2 border-b border-slate-100 last:border-b-0 transition-colors duration-200 hover:bg-slate-50/70 ${
        candidate.is_stuck ? "lr-row-stuck bg-amber-50/15" : "bg-white"
      }`}
    >
      {/* ── COL 1: PROFILE ─────────────────────────────────────────────── */}
      <div className="min-w-0 flex flex-col justify-center">
        <p className="truncate text-sm font-semibold tracking-tight text-slate-900">
          {candidate.name}
        </p>
        <p className="truncate text-xs text-slate-500 mt-0.5">
          {candidate.is_stuck ? (
            <span className="inline-flex items-center gap-1 text-amber-700 font-medium">
              <Hourglass size={11} className="text-amber-600 shrink-0" /> stuck · {stuckDetail}
            </span>
          ) : candidate.awaiting_retry || nextAttemptLabel ? (
            <span
              className="inline-flex items-center gap-1 text-slate-600"
              title={
                candidate.next_attempt_at
                  ? `Next scheduled attempt: ${new Date(candidate.next_attempt_at).toLocaleString()}`
                  : "Waiting on a scheduled attempt"
              }
            >
              <Clock size={11} className="text-slate-400 shrink-0" /> waiting{nextAttemptLabel ? ` · next ${nextAttemptLabel}` : ""}
            </span>
          ) : unknownPhase ? (
            <span className="text-slate-400" title={`Unrecognized phase: ${candidate.phase}`}>
              unknown phase · {candidate.phase}
            </span>
          ) : (
            candidate.outreach_status ?? "—"
          )}
        </p>
      </div>

      {/* ── COL 2: PROGRESS PIPELINE (Horizontal Stepper Track 1 to 6) ── */}
      <div className="flex items-center w-full min-w-[240px] px-2">
        {CHAIN_STOPS.map((stop, i) => {
          const hasUpdates = phaseHasEvents(stop.key, i);
          const isCurrentActive = i === reached && !isTerminal;
          const isResultStop = i === CHAIN_STOPS.length - 1;

          // Only color nodes that received updates or is active
          const isNodeDone = hasUpdates && i <= reached;

          // Check if this specific node is where the candidate completed the interview
          const isCompletedNode = i === completedStopIndex;

          let nodeCls =
            "relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 transition-all duration-300";
          if (isCompletedNode) {
            nodeCls += " border-emerald-600 bg-emerald-600 text-white shadow-2xs ring-2 ring-emerald-200";
          } else if (isNodeDone) {
            nodeCls += " lr-node-done border-indigo-700 bg-indigo-600 text-white shadow-2xs";
          } else if (isCurrentActive) {
            nodeCls += " lr-node-active border-indigo-600 bg-indigo-50 text-indigo-700 ring-2 ring-indigo-200";
          } else {
            nodeCls += " border-slate-200 bg-white text-slate-400";
          }

          if (popIndex === i) nodeCls += " lr-node-pop";

          const prevHasUpdates = i > 0 ? phaseHasEvents(CHAIN_STOPS[i - 1].key, i - 1) : false;
          const connectorFilled = (prevHasUpdates || hasUpdates) && i <= reached;

          return (
            <React.Fragment key={stop.key}>
              {/* Connector line between steps */}
              {i > 0 && (
                <div className="relative flex-1 self-center">
                  <div className="h-[2px] w-full bg-slate-200 rounded-full overflow-hidden">
                    <div
                      className={`h-full transition-all duration-500 ${
                        connectorFilled ? "bg-indigo-600" : "bg-slate-200"
                      }`}
                    />
                  </div>
                </div>
              )}

              {/* Node Circle */}
              <div
                className="relative flex flex-col items-center"
                title={`${stop.label} (${stop.short})`}
              >
                <div className={nodeCls}>
                  {i === completedStopIndex ? (
                    <Check size={13} strokeWidth={2.5} />
                  ) : (
                    <span
                      className={`text-[10px] font-semibold tracking-tight ${
                        isNodeDone ? "text-white" : ""
                      }`}
                    >
                      {stop.short}
                    </span>
                  )}
                </div>
              </div>
            </React.Fragment>
          );
        })}
      </div>

      {/* ── COL 3: HISTORY LOGS (Sub-div with scroll actions annotated by phase) ── */}
      <div className="flex items-center overflow-x-auto lr-scroll gap-1.5 justify-start pl-1 max-w-full py-1">
        {events.length > 0 ? (
          events.map((evt, idx) => {
            const isEmail = evt.type.includes("email") || evt.subtype === "email";
            const isCall = evt.type.includes("call") || evt.subtype === "call";
            const isSms = evt.type.includes("sms") || evt.subtype === "sms";

            let phaseTag = "OUT";
            if (evt.phase === "phase1") phaseTag = "P1";
            else if (evt.phase === "contact_check") phaseTag = "CC";
            else if (evt.phase === "phase1_6hr") phaseTag = "P2";
            else if (evt.phase === "phase2") phaseTag = "P3";
            else if (evt.phase === "phase3") phaseTag = "P4";
            else if (evt.phase === "phase1_extra") phaseTag = "Ex1";
            else if (evt.phase === "phase1_6hr_extra") phaseTag = "Ex2";
            else if (evt.phase === "phase2_extra") phaseTag = "Ex3";
            else if (evt.phase === "completed") phaseTag = "END";
            else if (evt.phase) phaseTag = "EVT";

            return (
              <span
                key={idx}
                className="inline-flex shrink-0 items-center gap-1 rounded-md border border-slate-200 bg-slate-50/90 px-1.5 py-0.5 text-[10px] font-medium text-slate-700 shadow-2xs transition-transform hover:scale-105"
                title={`${evt.phase ?? "Outreach"}: ${evt.type} (${evt.status ?? "done"})`}
              >
                <span className="font-bold text-indigo-600 text-[9px]">{phaseTag}</span>
                {isEmail && <Mail size={11} className="text-purple-600 shrink-0" />}
                {isCall && <Phone size={11} className="text-indigo-600 shrink-0" />}
                {isSms && <MessageSquare size={11} className="text-teal-600 shrink-0" />}
                {!isEmail && !isCall && !isSms && <Check size={11} className="text-slate-500 shrink-0" />}
              </span>
            );
          })
        ) : (
          <div className="flex items-center gap-1">
            <span
              className="relative inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-200 bg-slate-50 text-slate-400 shadow-2xs"
              title="Email Outreach"
            >
              <Mail size={11} />
            </span>
            <span
              className="relative inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-200 bg-slate-50 text-slate-400 shadow-2xs"
              title="Call Outreach"
            >
              <Phone size={11} />
            </span>
            <span
              className="relative inline-flex h-6 w-6 items-center justify-center rounded-full border border-slate-200 bg-slate-50 text-slate-400 shadow-2xs"
              title="SMS Outreach"
            >
              <MessageSquare size={11} />
            </span>
          </div>
        )}
      </div>

      {/* ── COL 4: ACTION / CTA (Badge / Voicemail Button) ───────────── */}
      <div className="flex items-center justify-end pr-1">
        {outcome ? (
          <span
            className={`inline-flex shrink-0 whitespace-nowrap items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-semibold shadow-2xs transition-colors ${
              TONE_BADGE[outcome.tone] || TONE_BADGE.neutral
            }`}
          >
            {OUTCOME_ICON[candidate.call_outcome as CallOutcome]}
            {outcome.label}
          </span>
        ) : neverPlaced ? (
          <span
            className="inline-flex shrink-0 whitespace-nowrap items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium border-rose-200 bg-rose-50 text-rose-700 shadow-2xs"
            title={`${candidate.handoff_expired} call handoff(s) expired`}
          >
            <PhoneMissed size={12} />
            Never placed
          </span>
        ) : (
          <span className="text-xs text-slate-400 whitespace-nowrap">no call yet</span>
        )}
      </div>
    </div>
  );
});

CandidateChain.displayName = "CandidateChain";
export default CandidateChain;
