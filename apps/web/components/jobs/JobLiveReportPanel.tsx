"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  Radio,
  RefreshCw,
  Eye,
  EyeOff,
  AlertTriangle,
} from "lucide-react";
import { api, authFetch } from "@/lib/api";
import { JobBlock } from "@/app/admin/live-report/JobBlock";
import type { LaunchListItem, JobBlockData, Snapshot, CandidateRow, Anomaly } from "@/app/admin/live-report/types";

interface FeedItem {
  id: number;
  ts: string;
  text: string;
  critical: boolean;
  tone?: "good" | "critical" | "neutral";
}

interface JobLiveReportPanelProps {
  jobId: string;
  jobdivaId?: string;
  initialTitle?: string;
  hideHeader?: boolean;
}

export function JobLiveReportPanel({
  jobId,
  jobdivaId,
  initialTitle,
  hideHeader = false,
}: JobLiveReportPanelProps) {
  const [jobTitle, setJobTitle] = useState<string>(initialTitle || "");
  const [resolvedJobDivaId, setResolvedJobDivaId] = useState<string>(jobdivaId || "");
  const [launches, setLaunches] = useState<LaunchListItem[]>([]);
  const [aggregatedSnapshot, setAggregatedSnapshot] = useState<Snapshot | null>(null);
  const [revealPii, setRevealPii] = useState<boolean>(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isInitializing, setIsInitializing] = useState<boolean>(true);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
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
        ...prev.slice(0, 49),
      ]);
    },
    []
  );

  const handleActivityEvent = useCallback(
    (event: {
      interviewId: number;
      type: string;
      subtype: string | null;
      phase: string | null;
      status: string | null;
      terminalReason?: string | null;
      name?: string;
    }) => {
      const isFailed = event.status === "failed";
      const isPassed =
        event.status === "passed" ||
        event.phase === "pass" ||
        event.terminalReason === "passed";
      const tone: "good" | "critical" | "neutral" = isFailed
        ? "critical"
        : isPassed
        ? "good"
        : "neutral";

      const subject = event.name || `#${event.interviewId}`;
      const action = event.type.replace(/_/g, " ");
      const statusText = isFailed ? " [Failed]" : isPassed ? " [Passed]" : "";
      pushFeed(`${subject}: ${action}${statusText}`, isFailed, tone);

      // Mutate candidate in real-time in aggregatedSnapshot
      setAggregatedSnapshot((prev) => {
        if (!prev || !prev.jobs) return prev;
        const updatedJobs = prev.jobs.map((job) => ({
          ...job,
          candidates: job.candidates.map((cand) => {
            if (cand.interview_id === event.interviewId) {
              const updatedPhase = event.phase || cand.phase;
              const isDone = updatedPhase === "completed" || event.type === "evaluation_completed";
              return {
                ...cand,
                phase: updatedPhase,
                outreach_status: event.status || cand.outreach_status,
                call_outcome: isDone ? "completed" : cand.call_outcome,
                terminal_reason: (event.terminalReason as any) || (event.status === "failed" ? "outreach_failed" : event.status === "passed" ? "passed" : cand.terminal_reason),
              };
            }
            return cand;
          }),
        }));
        return { ...prev, jobs: updatedJobs };
      });
    },
    [pushFeed]
  );

  const activityEventRef = useRef(handleActivityEvent);
  activityEventRef.current = handleActivityEvent;

  // Aggregate helper across multiple launch snapshots
  const mergeSnapshots = useCallback((snapshots: Snapshot[], jdId: string): Snapshot => {
    const candidateMap = new Map<number, CandidateRow>();
    const anomalyList: Anomaly[] = [];
    const seenAnomalyKeys = new Set<string>();

    for (const snap of snapshots) {
      if (!snap) continue;
      for (const a of snap.anomalies || []) {
        const k = `${a.kind}_${a.interview_id}`;
        if (!seenAnomalyKeys.has(k)) {
          seenAnomalyKeys.add(k);
          anomalyList.push(a);
        }
      }

      for (const j of snap.jobs || []) {
        for (const c of j.candidates || []) {
          if (!candidateMap.has(c.interview_id)) {
            candidateMap.set(c.interview_id, c);
          } else {
            // Merge events or keep latest non-empty status
            const existing = candidateMap.get(c.interview_id)!;
            candidateMap.set(c.interview_id, {
              ...existing,
              ...c,
              events: [...(existing.events || []), ...(c.events || [])].filter(
                (evt, idx, arr) => arr.findIndex((x) => x.ts === evt.ts && x.type === evt.type) === idx
              ),
            });
          }
        }
      }
    }

    const aggregatedCandidates = Array.from(candidateMap.values());
    const primaryTitle = snapshots[0]?.jobs?.[0]?.title || jobTitle || "Untitled job";
    const primaryCustomer = snapshots[0]?.jobs?.[0]?.customer_name || null;

    return {
      bulk_id: snapshots.map((s) => s.bulk_id).join(","),
      created_at: snapshots[0]?.created_at || null,
      state: snapshots.some((s) => s.state === "live") ? "live" : "archived",
      jobs: [
        {
          bulk_jd_id: null,
          jobdiva_id: jdId,
          title: primaryTitle,
          customer_name: primaryCustomer,
          candidates: aggregatedCandidates,
        },
      ],
      anomalies: anomalyList,
    };
  }, [jobTitle]);

  // Load and aggregate all launches for this job
  const fetchAllLaunchSnapshots = useCallback(
    async (launchList: LaunchListItem[], jdId: string, mask: boolean) => {
      if (!launchList || launchList.length === 0) {
        setAggregatedSnapshot(null);
        return;
      }
      setIsRefreshing(true);
      try {
        const snapshotPromises = launchList.map((l) =>
          api.liveReport.getSnapshot(l.bulk_id, mask).catch((e: any) => {
            console.warn(`Failed to fetch snapshot for launch ${l.bulk_id}:`, e);
            return null;
          })
        );
        const results = await Promise.all(snapshotPromises);
        const validSnapshots: Snapshot[] = results.filter((s): s is Snapshot => Boolean(s));
        if (validSnapshots.length > 0) {
          setAggregatedSnapshot(mergeSnapshots(validSnapshots, jdId));
        } else {
          setAggregatedSnapshot(null);
        }
        setError(null);
      } catch (err: any) {
        console.error("Failed to aggregate launches for job live report:", err);
        setError(err?.message || "Failed to load live report data");
      } finally {
        setIsRefreshing(false);
      }
    },
    [mergeSnapshots]
  );

  // Initialize job and fetch all launches
  useEffect(() => {
    let isCancelled = false;

    async function initJobData() {
      if (!jobId) return;
      setIsInitializing(true);
      setError(null);

      try {
        let jdId = jobdivaId || resolvedJobDivaId || jobId;
        if (!jdId || jdId === jobId) {
          try {
            const jobRes = await api.jobs.getMonitoredData(jobId);
            const jobData = jobRes?.data || jobRes;
            if (jobData?.jobdiva_id) {
              jdId = String(jobData.jobdiva_id).trim();
            }
            if (jobData?.enhanced_title || jobData?.title) {
              setJobTitle(jobData.enhanced_title || jobData.title);
            }
          } catch (e) {
            console.warn("Could not fetch monitored job details, falling back to jobId:", e);
          }
        }

        if (isCancelled) return;
        setResolvedJobDivaId(jdId);

        // Fetch all launches for this jobdiva_id
        const launchData = await api.liveReport.getLaunches({
          jobdiva_id: jdId,
          limit: 100,
        });

        if (isCancelled) return;
        let launchList: LaunchListItem[] = launchData?.launches || [];

        if (launchList.length === 0 && jdId !== jobId) {
          const fallback = await api.liveReport.getLaunches({
            jobdiva_id: jobId,
            limit: 100,
          });
          if (fallback?.launches?.length > 0) {
            launchList = fallback.launches;
          }
        }

        if (isCancelled) return;
        setLaunches(launchList);

        if (launchList.length > 0) {
          await fetchAllLaunchSnapshots(launchList, jdId, revealPii);
        } else {
          setAggregatedSnapshot(null);
        }
      } catch (err: any) {
        if (!isCancelled) {
          console.error("Failed to initialize job live report:", err);
          setError(err?.message || "Failed to load live launches for this job");
        }
      } finally {
        if (!isCancelled) setIsInitializing(false);
      }
    }

    initJobData();
    return () => {
      isCancelled = true;
    };
  }, [jobId, jobdivaId, revealPii, fetchAllLaunchSnapshots]);

  // Connect SSE streams for all launches
  const [streamConnectedCount, setStreamConnectedCount] = useState<number>(0);
  const isConnected = streamConnectedCount > 0;

  useEffect(() => {
    if (!launches || launches.length === 0 || typeof window === "undefined") {
      setStreamConnectedCount(0);
      return;
    }

    const abortControllers: AbortController[] = [];

    launches.forEach((launch) => {
      const ac = new AbortController();
      abortControllers.push(ac);

      const streamUrl = api.liveReport.streamUrl(launch.bulk_id);
      authFetch(streamUrl, {
        signal: ac.signal,
        headers: { Accept: "text/event-stream" },
      })
        .then(async (response) => {
          if (!response.ok || !response.body) return;
          setStreamConnectedCount((prev) => prev + 1);

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";

          const handleRawChunk = (rawEvent: string) => {
            const trimmed = rawEvent.trim();
            if (!trimmed || trimmed.startsWith(":")) return;
            const lines = trimmed.split("\n");
            const dataLines = lines
              .filter((l) => l.startsWith("data:"))
              .map((l) => l.slice(5).trim());
            if (dataLines.length === 0) return;
            const jsonStr = dataLines.join("");
            try {
              const payload = JSON.parse(jsonStr);
              if (payload.type === "connected") return;
              const interviewId = payload.interview_id ?? payload.interviewId;
              const eventType = payload.event_type ?? payload.type;
              if (interviewId && eventType && eventType !== "connected") {
                activityEventRef.current({
                  interviewId: Number(interviewId),
                  type: eventType,
                  subtype: payload.subtype ?? null,
                  phase: payload.phase ?? null,
                  status: payload.status ?? null,
                  terminalReason: payload.terminal_reason ?? payload.terminalReason ?? null,
                });
              }
            } catch (err) {
              // Ignore unparseable SSE
            }
          };

          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let sepIdx: number;
            while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
              const raw = buffer.slice(0, sepIdx);
              buffer = buffer.slice(sepIdx + 2);
              handleRawChunk(raw);
            }
          }
          if (buffer.trim()) handleRawChunk(buffer);
        })
        .catch(() => {
          // Expected when aborted or connection ends
        })
        .finally(() => {
          setStreamConnectedCount((prev) => Math.max(0, prev - 1));
        });
    });

    return () => {
      abortControllers.forEach((ac) => ac.abort());
    };
  }, [launches]);

  // Tab visibility reconciliation
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible" && launches.length > 0) {
        fetchAllLaunchSnapshots(launches, resolvedJobDivaId || jobId, revealPii);
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, [launches, resolvedJobDivaId, jobId, revealPii, fetchAllLaunchSnapshots]);

  // Scoped Job from aggregatedSnapshot
  const scopedJob: JobBlockData | null = useMemo(() => {
    if (!aggregatedSnapshot?.jobs || aggregatedSnapshot.jobs.length === 0) return null;
    return aggregatedSnapshot.jobs[0];
  }, [aggregatedSnapshot]);

  // Scoped Anomalies
  const scopedAnomalies = useMemo(() => {
    return aggregatedSnapshot?.anomalies || [];
  }, [aggregatedSnapshot]);

  // Memoize candidate name resolution map
  const candidateNameMap = useMemo(() => {
    const map = new Map<number, string>();
    for (const c of scopedJob?.candidates || []) {
      if (c.interview_id && c.name) {
        map.set(c.interview_id, c.name);
      }
    }
    return map;
  }, [scopedJob]);

  const totalCandidatesCount = scopedJob?.candidates?.length || 0;

  return (
    <div className="flex flex-col gap-4 w-full">
      {/* Top Controls Strip */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
        <div className="flex items-center gap-3">
          <div
            className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${
              isConnected
                ? "bg-emerald-50 text-emerald-700 border-emerald-200"
                : "bg-amber-50 text-amber-700 border-amber-200"
            }`}
          >
            <Radio
              className={`h-3 w-3 ${
                isConnected ? "animate-pulse text-emerald-600" : "text-amber-500"
              }`}
            />
            <span>{isConnected ? "Live Telemetry Active" : "Connecting to stream..."}</span>
          </div>
          <span className="text-xs text-slate-500 font-medium">
            Job <span className="font-mono text-slate-800 font-semibold">#{resolvedJobDivaId || jobId}</span>
          </span>
          {launches.length > 0 && (
            <span className="text-xs text-slate-400 font-medium">
              ({launches.length} {launches.length === 1 ? "launch" : "launches"} aggregated • {totalCandidatesCount} total candidates)
            </span>
          )}
        </div>

        <div className="flex items-center gap-2.5">
          <button
            type="button"
            onClick={() => setRevealPii((prev) => !prev)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors shadow-2xs cursor-pointer"
          >
            {revealPii ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            <span>{revealPii ? "Mask PII" : "Reveal PII"}</span>
          </button>

          <button
            type="button"
            onClick={() => fetchAllLaunchSnapshots(launches, resolvedJobDivaId || jobId, revealPii)}
            disabled={isRefreshing}
            className="p-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-colors shadow-2xs cursor-pointer"
            title="Refresh All Launches"
          >
            <RefreshCw className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {error && (
        <div className="p-3.5 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700">
          {error}
        </div>
      )}

      {/* Main Grid: Left = Job Pipeline & Candidates; Right = Activity Feed & Anomalies */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6 items-start">
        {/* Left: Job Pipeline (3 cols) */}
        <div className="xl:col-span-3 flex flex-col gap-4 min-w-0">
          {isInitializing ? (
            <div className="py-16 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl bg-white">
              Loading live report for this job...
            </div>
          ) : launches.length === 0 ? (
            <div className="py-16 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl bg-white">
              No live telemetry launch recorded for Job #{resolvedJobDivaId || jobId} yet.
            </div>
          ) : scopedJob ? (
            <JobBlock job={scopedJob} defaultCollapsed={false} />
          ) : (
            <div className="py-16 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl bg-white">
              Waiting for candidate telemetry events...
            </div>
          )}
        </div>

        {/* Right: Real-time Activity Feed & Anomalies (1 col) */}
        <div className="xl:col-span-1 flex flex-col gap-4 min-w-0 max-w-full h-full max-h-[640px]">
          {/* Anomalies Banner */}
          {scopedAnomalies.length > 0 && (
            <div className="border border-amber-200 rounded-xl bg-amber-50/70 p-3.5 shadow-2xs shrink-0 flex flex-col max-h-44">
              <div className="flex items-center justify-between gap-2 text-xs font-semibold text-amber-900 mb-2">
                <div className="flex items-center gap-1.5 min-w-0 truncate">
                    <AlertTriangle className="h-4 w-4 text-amber-600 shrink-0" />
                    <span className="truncate">Detected Anomalies ({scopedAnomalies.length})</span>
                  </div>
                </div>
                <ul className="space-y-1.5 text-xs text-amber-800 overflow-y-auto pr-1 divide-y divide-amber-200/50 lr-scroll flex-1">
                  {scopedAnomalies.map((a, i) => {
                    const candName = candidateNameMap.get(a.interview_id) || "Candidate";
                    const phase = a.kind === "stuck_candidate" ? a.phase : "";
                    const cleanPhase =
                      phase === "phase1"
                        ? "Phase 1"
                        : phase === "phase1_6hr"
                        ? "Phase 2"
                        : phase === "phase2"
                        ? "Phase 3"
                        : phase === "phase3"
                        ? "Phase 4"
                        : phase
                        ? phase.replace(/_/g, " ")
                        : "Outreach";

                    return (
                      <li key={i} className="pt-1.5 first:pt-0 flex items-start gap-1.5 min-w-0">
                        <span className="text-amber-500 font-bold shrink-0">•</span>
                        <span className="break-words min-w-0 flex-1 leading-snug">
                          {a.kind === "stuck_candidate" &&
                            `${candName} stuck in ${cleanPhase} for ${a.minutes_since_event}m`}
                          {a.kind === "call_failure" &&
                            `Call failure for ${candName}: ${(a.outcome || "failed").replace(/_/g, " ")}`}
                          {a.kind === "handoff_expired" &&
                            `Handoff expired for ${candName}`}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

          {/* Real-time Activity Feed */}
          <div className="border border-slate-200 rounded-xl bg-white shadow-2xs overflow-hidden flex flex-col flex-1 min-h-0 min-w-0 max-w-full">
            <div className="p-3.5 border-b border-slate-100 flex items-center justify-between bg-slate-50/50 shrink-0">
              <span className="text-xs font-semibold text-slate-800">Job Activity Feed</span>
              <span className="text-[10px] text-slate-400 font-mono">Streamed</span>
            </div>

            <div className="flex-1 overflow-y-auto p-3 space-y-2 text-xs divide-y divide-slate-50 lr-scroll min-h-0">
              {feed.map((item) => {
                const textColor =
                  item.tone === "critical"
                    ? "text-rose-600 font-medium"
                    : item.tone === "good"
                    ? "text-emerald-600 font-medium"
                    : "text-slate-700";

                return (
                  <div key={item.id} className="pt-2 first:pt-0">
                    <div className="flex items-center justify-between gap-1 text-[10px] text-slate-400 mb-0.5 font-mono">
                      <span>{item.ts}</span>
                      {item.critical && (
                        <span className="px-1 py-0.2 rounded bg-rose-100 text-rose-700 font-semibold text-[9px]">
                          CRITICAL
                        </span>
                      )}
                    </div>
                    <div className={`break-words ${textColor}`}>{item.text}</div>
                  </div>
                );
              })}

              {feed.length === 0 && (
                <div className="py-8 text-center text-xs text-slate-400">
                  Listening for real-time outreach events...
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
