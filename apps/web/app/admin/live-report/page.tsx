"use client";

import React, { useState, useEffect, useCallback, useReducer, useRef } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Radio,
  RefreshCw,
  Search,
  Server,
  Users,
  X,
  ChevronDown,
} from "lucide-react";
import { api, isNotFoundError, LIVE_REPORT_PROD_ONLY_MESSAGE } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { useLiveReportStream } from "@/hooks/use-live-report-stream";
import { JobBlock } from "./JobBlock";
import type { LaunchListItem, HealthData, TerminalReason } from "./types";

interface FeedItem {
  id: number;
  ts: string;
  text: string;
  critical: boolean;
  tone?: "good" | "critical" | "neutral";
}

export default function LiveReportPage() {
  const { isAdmin, isTeamLead, isLoading: isRoleLoading } = useUserRole();

  const [launches, setLaunches] = useState<LaunchListItem[]>([]);
  const [selectedBulkId, setSelectedBulkId] = useState<string | null>(null);
  const [revealPii, setRevealPii] = useState<boolean>(false);
  const [health, setHealth] = useState<HealthData | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [launchesError, setLaunchesError] = useState<string | null>(null);
  const feedIdCounterRef = useRef<number>(0);

  // Push into feed
  const pushFeed = useCallback(
    (text: string, critical = false, tone?: "good" | "critical" | "neutral") => {
      feedIdCounterRef.current += 1;
      const currentId = feedIdCounterRef.current;
      const resolvedTone = tone || (critical ? "critical" : "neutral");
      setFeed((prev) => [
        {
          id: currentId,
          ts: new Date().toLocaleTimeString(),
          text,
          critical,
          tone: resolvedTone,
        },
        ...prev.slice(0, 49), // retain up to 50 entries
      ]);
    },
    []
  );

  // Self-healing SSE stream with auto-reconciling snapshot
  const activityEventRef = useRef<((event: any) => void) | null>(null);

  const {
    snapshot,
    setSnapshot,
    isLoading: isSnapshotLoading,
    isConnected,
    error: snapshotError,
    refreshSnapshot,
  } = useLiveReportStream({
    bulkId: selectedBulkId,
    revealPii,
    onActivityEvent: (evt) => activityEventRef.current?.(evt),
  });

  // Handle real-time activity events arriving via SSE
  const handleActivityEvent = useCallback(
    (event: {
      interviewId: number;
      type: string;
      subtype: string | null;
      phase: string | null;
      status: string | null;
      terminalReason?: string | null;
    }) => {
      const isFailed = event.status === "failed";
      const isPassed =
        event.status === "passed" ||
        event.phase === "pass" ||
        event.type === "evaluation_completed" ||
        event.type === "interview_completed";
      const tone: "good" | "critical" | "neutral" = isFailed
        ? "critical"
        : isPassed
        ? "good"
        : "neutral";

      pushFeed(
        `#${event.interviewId} ${event.type}${event.subtype ? ` (${event.subtype})` : ""} [${event.phase || "phase"}]`,
        isFailed,
        tone
      );

      // Mutate snapshot candidate in real-time
      setSnapshot((prev) => {
        if (!prev || !prev.jobs) return prev;
        const updatedJobs = prev.jobs.map((job) => ({
          ...job,
          candidates: job.candidates.map((cand) => {
            if (cand.interview_id === event.interviewId) {
              const updatedPhase = event.phase || cand.phase;
              const isDone = updatedPhase === "completed" || event.type === "evaluation_completed";
              let terminalReason: TerminalReason | null | undefined = cand.terminal_reason;
              if (event.terminalReason) {
                terminalReason = event.terminalReason as TerminalReason;
              } else if (event.status === "failed") {
                terminalReason = "outreach_failed";
              } else if (event.status === "passed") {
                terminalReason = "passed";
              }

              return {
                ...cand,
                phase: updatedPhase,
                outreach_status: event.status || cand.outreach_status,
                call_outcome: isDone ? "completed" : cand.call_outcome,
                terminal_reason: terminalReason,
              };
            }
            return cand;
          }),
        }));
        return { ...prev, jobs: updatedJobs };
      });
    },
    [pushFeed, setSnapshot]
  );

  useEffect(() => {
    activityEventRef.current = handleActivityEvent;
  }, [handleActivityEvent]);

  const [searchLaunchTerm, setSearchLaunchTerm] = useState<string>("");
  const [debouncedSearch, setDebouncedSearch] = useState<string>("");
  const [isDropdownOpen, setIsDropdownOpen] = useState<boolean>(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = setTimeout(() => {
      setDebouncedSearch(searchLaunchTerm);
    }, 300);
    return () => clearTimeout(handler);
  }, [searchLaunchTerm]);

  // Close searchable dropdown on click outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsDropdownOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Clean, recruiter-friendly event text formatter using full phase names
  const formatRecruiterEvent = useCallback((event: {
    interviewId?: number;
    name?: string;
    type: string;
    subtype?: string | null;
    phase?: string | null;
    status?: string | null;
  }): string | null => {
    const p = (event.phase || "").toLowerCase();
    let phaseName = "Outreach";
    if (p === "phase1") phaseName = "Phase 1";
    else if (p === "phase1_6hr" || p.includes("6hr")) phaseName = "Phase 2";
    else if (p === "phase2") phaseName = "Phase 3";
    else if (p === "phase3") phaseName = "Phase 4";
    else if (p === "phase1_extra") phaseName = "Extra 1";
    else if (p === "phase1_6hr_extra") phaseName = "Extra 2";
    else if (p === "phase2_extra") phaseName = "Extra 3";
    else if (p === "contact_check") phaseName = "Contact Check";
    else if (p === "pass") phaseName = "Passed";
    else if (p === "fail") phaseName = "Failed";

    const t = (event.type || "").toLowerCase();
    const st = (event.subtype || "").toLowerCase();

    // Hide internal noise from recruiters
    if (t.includes("stt_tts") || t.includes("phase_transition") || t.includes("teams_alert")) {
      return null;
    }

    let action = t;
    if (t.includes("interview_started")) action = "Interview Started";
    else if (t.includes("interview_completed") || t.includes("evaluation_completed")) action = "Passed Interview";
    else if (t.includes("interview_partial")) action = "Interview Partial";
    else if (t.includes("voice_pipeline")) {
      action = st.includes("hangup") || event.status?.includes("hangup") ? "Call Ended (Hangup)" : "Voice Call Completed";
    } else if (t.includes("email_sms") || (t.includes("email") && t.includes("sms"))) {
      action = "Email & SMS Sent";
    } else if (t.includes("email")) action = "Email Sent";
    else if (t.includes("sms")) action = "SMS Sent";
    else if (t.includes("call")) action = "Call Placed";

    const subject = event.name || (event.interviewId ? `#${event.interviewId}` : "Candidate");
    const statusText = event.status === "failed" ? " [Failed]" : event.status === "passed" ? " [Passed]" : "";
    return `${subject} (${phaseName}): ${action}${statusText}`;
  }, []);

  // Fetch launch list and health stats
  const fetchLaunchesAndHealth = useCallback(async (searchQuery?: string) => {
    try {
      const [launchesData, healthData] = await Promise.allSettled([
        api.liveReport.getLaunches({ search: searchQuery, limit: 100 }),
        api.liveReport.getHealth(),
      ]);

      if (launchesData.status === "fulfilled") {
        setLaunchesError(null);
        const list = Array.isArray(launchesData.value)
          ? launchesData.value
          : launchesData.value?.launches || [];
        setLaunches(list);
        if (list.length > 0) {
          setSelectedBulkId((prev) => {
            if (prev && list.some((l: any) => l.bulk_id === prev)) return prev;
            return list[0].bulk_id;
          });
        }
      } else {
        console.error("Failed to load live report launches:", launchesData.reason);
        if (isNotFoundError(launchesData.reason)) {
          setLaunchesError(LIVE_REPORT_PROD_ONLY_MESSAGE);
        } else {
          setLaunchesError("Unable to load launches. Please check API connection and retry.");
        }
      }

      if (healthData.status === "fulfilled") {
        setHealth(healthData.value);
      }
    } catch (err: any) {
      console.error("Failed to load live report initial data:", err);
      if (isNotFoundError(err)) {
        setLaunchesError(LIVE_REPORT_PROD_ONLY_MESSAGE);
      } else {
        setLaunchesError("Failed to communicate with the analytics service.");
      }
    }
  }, []);

  useEffect(() => {
    fetchLaunchesAndHealth(debouncedSearch);
  }, [fetchLaunchesAndHealth, debouncedSearch]);

  // Reset feed when user switches launches
  useEffect(() => {
    setFeed([]);
  }, [selectedBulkId]);

  // Seed the Live Activity Feed with recent candidate events when snapshot loads
  useEffect(() => {
    if (!snapshot || !snapshot.jobs) return;
    const allRecentEvents: Array<{
      id: number;
      ts: string;
      rawTs: string;
      text: string;
      critical: boolean;
    }> = [];

    for (const job of snapshot.jobs) {
      for (const cand of job.candidates) {
        for (const evt of cand.events || []) {
          const formatted = formatRecruiterEvent({
            interviewId: cand.interview_id,
            name: cand.name,
            type: evt.type,
            subtype: evt.subtype,
            phase: evt.phase,
            status: evt.status,
          });
          if (!formatted) continue;

          const isFailed = evt.status === "failed";
          const isPassed =
            evt.status === "passed" ||
            evt.phase === "pass" ||
            evt.type === "evaluation_completed" ||
            evt.type === "interview_completed" ||
            cand.phase === "pass" && (evt.type.includes("evaluation") || evt.type.includes("interview"));
          const tone: "good" | "critical" | "neutral" = isFailed
            ? "critical"
            : isPassed
            ? "good"
            : "neutral";
          const d = evt.ts ? new Date(evt.ts) : new Date();
          const tsStr = !Number.isNaN(d.getTime())
            ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
            : "—";

          feedIdCounterRef.current += 1;
          allRecentEvents.push({
            id: feedIdCounterRef.current,
            rawTs: evt.ts || "",
            ts: tsStr,
            text: formatted,
            critical: isFailed,
            tone,
          });
        }
      }
    }

    // Sort newest first, keep top 30
    allRecentEvents.sort((a, b) => b.rawTs.localeCompare(a.rawTs));
    if (allRecentEvents.length > 0) {
      setFeed((prev) => {
        if (prev.length === 0) {
          return allRecentEvents.slice(0, 30).map(({ rawTs, ...item }) => item);
        }
        // Merge missing events from snapshot if any arrived while feed was active
        const existingTexts = new Set(prev.map((p) => p.text));
        const newItems = allRecentEvents
          .filter((item) => !existingTexts.has(item.text))
          .map(({ rawTs, ...item }) => item);
        if (newItems.length === 0) return prev;
        return [...newItems, ...prev].slice(0, 50);
      });
    }
  }, [snapshot, formatRecruiterEvent]);

  if (isRoleLoading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-slate-400 text-sm">Loading Live Report...</div>
      </div>
    );
  }

  const selectedLaunch = launches.find((l) => l.bulk_id === selectedBulkId);


  return (
    <div className="p-6 w-full max-w-[1600px] mx-auto space-y-6">
      {/* Launch Load Error Alert */}
      {launchesError && (
        <div className="flex items-center justify-between gap-2 p-3 rounded-lg border border-red-200 bg-red-50 text-xs text-red-800">
          <div className="flex items-center gap-2">
            <span className="font-semibold">Launch Load Error:</span>
            <span>{launchesError}</span>
          </div>
          <button
            type="button"
            onClick={() => fetchLaunchesAndHealth()}
            className="px-2.5 py-1 rounded bg-red-600 hover:bg-red-700 text-white font-medium transition-colors"
          >
            Retry
          </button>
        </div>
      )}

      {/* Top Header & Launch Selector */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-200 pb-5">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-bold tracking-tight text-slate-900">
              Live Launch Monitor
            </h1>
            <div
              className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${
                isConnected
                  ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                  : "bg-amber-50 text-amber-700 border-amber-200"
              }`}
            >
              <Radio
                className={`h-3 w-3 ${isConnected ? "animate-pulse text-emerald-600" : "text-amber-500"}`}
              />
              <span>{isConnected ? "Live Stream Active" : "Reconnecting..."}</span>
            </div>
          </div>
          <p className="text-xs text-slate-500 mt-1">
            Zero-latency telemetry from PairBot voice agent fleet.
          </p>
        </div>

        {/* Controls */}
        <div className="flex flex-wrap items-center gap-3">
          {/* Unified Searchable Launch Combobox */}
          <div className="relative w-80 sm:w-96" ref={dropdownRef}>
            <div
              className={`flex items-center gap-2 border rounded-lg px-2.5 py-1.5 bg-white shadow-xs cursor-text transition-all ${
                isDropdownOpen
                  ? "border-indigo-500 ring-2 ring-indigo-100"
                  : "border-slate-300 hover:border-slate-400"
              }`}
              onClick={() => setIsDropdownOpen(true)}
            >
              <Search className="h-3.5 w-3.5 text-slate-400 shrink-0" />
              <input
                type="text"
                role="combobox"
                aria-expanded={isDropdownOpen}
                aria-haspopup="listbox"
                value={searchLaunchTerm}
                onFocus={() => setIsDropdownOpen(true)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    setIsDropdownOpen(false);
                  }
                }}
                onChange={(e) => {
                  setSearchLaunchTerm(e.target.value);
                  setIsDropdownOpen(true);
                }}
                placeholder={
                  selectedLaunch
                    ? `${selectedLaunch.titles?.[0] || selectedLaunch.jobdiva_ids?.[0] || selectedLaunch.bulk_id.slice(0, 8)} (${selectedLaunch.total_candidates} candidates)`
                    : "Search job name, JobDiva ID, bulk ID..."
                }
                className="w-full text-xs text-slate-800 placeholder:text-slate-500 bg-transparent focus:outline-hidden"
              />
              {searchLaunchTerm && (
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setSearchLaunchTerm("");
                  }}
                  className="text-slate-400 hover:text-slate-600 p-0.5 rounded"
                  title="Clear search"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
              <ChevronDown
                className={`h-3.5 w-3.5 text-slate-400 shrink-0 cursor-pointer transition-transform ${
                  isDropdownOpen ? "rotate-180" : ""
                }`}
                onClick={(e) => {
                  e.stopPropagation();
                  setIsDropdownOpen((prev) => !prev);
                }}
              />
            </div>

            {/* Floating Dropdown Results */}
            {isDropdownOpen && (
              <div
                role="listbox"
                className="absolute left-0 right-0 top-full mt-1 bg-white border border-slate-200 rounded-xl shadow-xl z-50 max-h-72 overflow-y-auto divide-y divide-slate-100 animate-in fade-in zoom-in-95 duration-100"
              >
                {launches.length > 0 ? (
                  launches.map((l) => {
                    const isSelected = l.bulk_id === selectedBulkId;
                    const jobTitle = l.titles?.[0] || "Untitled Job";
                    const jobDivaId = l.jobdiva_ids?.[0];
                    return (
                      <button
                        key={l.bulk_id}
                        type="button"
                        onClick={() => {
                          setSelectedBulkId(l.bulk_id);
                          setIsDropdownOpen(false);
                          setSearchLaunchTerm("");
                        }}
                        className={`w-full text-left px-3 py-2.5 transition-colors flex items-center justify-between gap-3 text-xs ${
                          isSelected
                            ? "bg-indigo-50/70 text-indigo-950 font-medium"
                            : "hover:bg-slate-50 text-slate-800"
                        }`}
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="font-semibold truncate text-slate-900">
                              {jobTitle}
                            </span>
                            {jobDivaId && (
                              <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 shrink-0">
                                #{jobDivaId}
                              </span>
                            )}
                          </div>
                          <div className="text-[11px] text-slate-500 mt-0.5 flex items-center gap-2">
                            <span className="capitalize">{l.state || "active"}</span>
                            <span>•</span>
                            <span>{l.total_candidates} candidates</span>
                            <span>•</span>
                            <span className="font-mono text-[10px] text-slate-400">
                              ID: {l.bulk_id.slice(0, 8)}
                            </span>
                          </div>
                        </div>
                        {isSelected && (
                          <div className="h-2 w-2 rounded-full bg-indigo-600 shrink-0" />
                        )}
                      </button>
                    );
                  })
                ) : (
                  <div className="p-4 text-center text-xs text-slate-400">
                    {launchesError || "No matching launches found"}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Reveal PII Button */}
          <button
            type="button"
            onClick={() => setRevealPii((prev) => !prev)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors shadow-xs"
          >
            {revealPii ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            <span>{revealPii ? "Mask PII" : "Reveal PII"}</span>
          </button>

          {/* Manual Refresh */}
          <button
            type="button"
            onClick={() => refreshSnapshot()}
            disabled={isSnapshotLoading}
            className="p-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-colors shadow-xs"
            title="Force refresh snapshot"
          >
            <RefreshCw className={`h-4 w-4 ${isSnapshotLoading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* System Health Strip (Restricted to Admins and Team Leads; hidden from recruiters) */}
      {(isAdmin || isTeamLead) && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
            <Database className="h-5 w-5 text-indigo-600" />
            <div>
              <div className="text-[11px] text-slate-400 font-medium">DB Connection Pool</div>
              <div className="text-sm font-semibold text-slate-800">
                {health?.db_pool ? `${health.db_pool.used} / ${health.db_pool.max} (${health.db_pool.percent}%)` : "—"}
              </div>
            </div>
          </div>

          <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
            <Server className="h-5 w-5 text-emerald-600" />
            <div>
              <div className="text-[11px] text-slate-400 font-medium">Agent Workers Fleet</div>
              <div className="text-sm font-semibold text-slate-800">
                {health?.agent_fleet ? `${health.agent_fleet.active_workers} Active • ${health.agent_fleet.idle_workers} Idle` : "—"}
              </div>
            </div>
          </div>

          <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
            <Activity className="h-5 w-5 text-blue-600" />
            <div>
              <div className="text-[11px] text-slate-400 font-medium">Queue Processing</div>
              <div className="text-sm font-semibold text-slate-800">
                {health?.outreach_queue ? `${health.outreach_queue.pending} Pending / ${health.outreach_queue.processing} In-flight` : "—"}
              </div>
            </div>
          </div>

          <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
            <CheckCircle2 className="h-5 w-5 text-indigo-600" />
            <div>
              <div className="text-[11px] text-slate-400 font-medium">Telemetry Protocol</div>
              <div className="text-sm font-semibold text-slate-800">SSE Stream + Claim-Check</div>
            </div>
          </div>
        </div>
      )}

      {/* Main Grid: Job Blocks on Left, Event Feed on Right */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        {/* Job Blocks (3 columns) */}
        <div className="xl:col-span-3 space-y-4">
          <h2 className="text-sm font-semibold text-slate-900 flex items-center justify-between">
            <span>Active Campaign Job Blocks</span>
            {snapshot?.jobs && (
              <span className="text-xs text-slate-400 font-normal">
                {snapshot.jobs.length} job(s) in batch
              </span>
            )}
          </h2>

          {isSnapshotLoading && !snapshot && (
            <div className="py-12 text-center text-xs text-slate-400">
              Loading launch snapshot...
            </div>
          )}

          {snapshotError && (
            <div className="p-4 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700">
              {snapshotError}
            </div>
          )}

          {snapshot?.jobs?.map((job, idx) => (
            <JobBlock key={job.bulk_jd_id || job.jobdiva_id || `job-${idx}`} job={job} />
          ))}

          {snapshot && (!snapshot.jobs || snapshot.jobs.length === 0) && (
            <div className="py-12 border border-dashed border-slate-200 rounded-xl text-center text-xs text-slate-400">
              No jobs or candidates recorded for this launch.
            </div>
          )}
        </div>

        {/* Event Feed & Anomalies (1 column) */}
        <div className="xl:col-span-1 space-y-4">
          {/* Anomalies Banner if any */}
          {snapshot?.anomalies && snapshot.anomalies.length > 0 && (
            <div className="border border-amber-200 rounded-xl bg-amber-50/70 p-3.5">
              <div className="flex items-center gap-2 text-xs font-semibold text-amber-900 mb-2">
                <AlertTriangle className="h-4 w-4 text-amber-600" />
                <span>Detected Anomalies ({snapshot.anomalies.length})</span>
              </div>
              <ul className="space-y-1.5 text-xs text-amber-800">
                {snapshot.anomalies.map((a, i) => (
                  <li key={i} className="flex items-start gap-1.5">
                    <span>•</span>
                    <span>
                      {a.kind === "stuck_candidate" &&
                        `Candidate #${a.interview_id} stuck in ${a.phase} for ${a.minutes_since_event}m`}
                      {a.kind === "call_failure" &&
                        `Call failure on #${a.interview_id}: ${a.outcome}`}
                      {a.kind === "handoff_expired" &&
                        `Handoff expired on #${a.interview_id}`}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Real-time Activity Feed */}
          <div className="border border-slate-200 rounded-xl bg-white shadow-xs overflow-hidden flex flex-col h-[520px]">
            <div className="p-3.5 border-b border-slate-100 flex items-center justify-between bg-slate-50/50">
              <span className="text-xs font-semibold text-slate-800">Live Activity Feed</span>
              <span className="text-[10px] text-slate-400 font-mono">Streamed</span>
            </div>

            <div className="flex-1 overflow-y-auto p-3 space-y-2 text-xs divide-y divide-slate-50">
              {feed.map((item) => {
                const isPassed = item.tone === "good" || item.text.includes("[Passed]") || item.text.includes("Passed");
                const isFailed = item.critical || item.tone === "critical" || item.text.includes("[Failed]") || item.text.includes("Failed");
                const textColor = isFailed
                  ? "text-rose-600 font-medium"
                  : isPassed
                  ? "text-emerald-600 font-medium"
                  : "text-slate-700";

                return (
                  <div key={item.id} className="pt-2 first:pt-0 flex items-start justify-between gap-2">
                    <span className={textColor}>
                      {item.text}
                    </span>
                    <span className="text-[10px] text-slate-400 shrink-0 font-mono">{item.ts}</span>
                  </div>
                );
              })}

              {feed.length === 0 && (
                <div className="h-full flex items-center justify-center text-slate-400 text-xs">
                  Listening for stream activity...
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
