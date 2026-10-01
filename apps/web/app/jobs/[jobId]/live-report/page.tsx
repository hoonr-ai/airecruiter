"use client";

import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import {
  ArrowLeft,
  Radio,
  RefreshCw,
  Eye,
  EyeOff,
  AlertTriangle,
  ChevronDown,
  ExternalLink,
} from "lucide-react";
import { api } from "@/lib/api";
import { useLiveReportStream } from "@/hooks/use-live-report-stream";
import { JobBlock } from "@/app/admin/live-report/JobBlock";
import type { LaunchListItem, JobBlockData } from "@/app/admin/live-report/types";

interface FeedItem {
  id: number;
  ts: string;
  text: string;
  critical: boolean;
  tone?: "good" | "critical" | "neutral";
}

export default function JobLiveReportPage() {
  const params = useParams();
  const router = useRouter();
  const rawJobId = params?.jobId as string;

  const [jobTitle, setJobTitle] = useState<string>("");
  const [resolvedJobDivaId, setResolvedJobDivaId] = useState<string>("");
  const [launches, setLaunches] = useState<LaunchListItem[]>([]);
  const [selectedBulkId, setSelectedBulkId] = useState<string | null>(null);
  const [revealPii, setRevealPii] = useState<boolean>(false);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [isInitializing, setIsInitializing] = useState<boolean>(true);
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

  // Hook up SSE stream for the selected bulk launch
  const activityEventRef = useRef<((event: any) => void) | null>(null);

  const {
    snapshot,
    isLoading: isSnapshotLoading,
    isConnected,
    error: snapshotError,
    refreshSnapshot,
  } = useLiveReportStream({
    bulkId: selectedBulkId,
    revealPii,
    onActivityEvent: (evt) => activityEventRef.current?.(evt),
  });

  // Handle real-time activity events
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
    },
    [pushFeed]
  );

  useEffect(() => {
    activityEventRef.current = handleActivityEvent;
  }, [handleActivityEvent]);

  // Step 1: Resolve Job details (get JobDiva ID) and fetch launches for this specific job
  useEffect(() => {
    let isCancelled = false;

    async function initJobData() {
      if (!rawJobId) return;
      setIsInitializing(true);
      setError(null);

      try {
        // Fetch job metadata to get the canonical JobDiva ID
        let jdId = rawJobId;
        try {
          const jobRes = await api.jobs.getMonitoredData(rawJobId);
          const jobData = jobRes?.data || jobRes;
          if (jobData?.jobdiva_id) {
            jdId = String(jobData.jobdiva_id).trim();
          }
          if (jobData?.enhanced_title || jobData?.title) {
            setJobTitle(jobData.enhanced_title || jobData.title);
          }
        } catch (e) {
          console.warn("Could not fetch monitored job details, falling back to rawJobId:", e);
        }

        if (isCancelled) return;
        setResolvedJobDivaId(jdId);

        // Fetch launches filtered strictly by jobdiva_id
        const launchData = await api.liveReport.getLaunches({
          jobdiva_id: jdId,
          limit: 20,
        });

        if (isCancelled) return;
        const launchList: LaunchListItem[] = launchData?.launches || [];
        setLaunches(launchList);

        if (launchList.length > 0) {
          setSelectedBulkId(launchList[0].bulk_id);
        } else {
          // If no launch by jobdiva_id, try fallback query with rawJobId
          if (jdId !== rawJobId) {
            const fallback = await api.liveReport.getLaunches({
              jobdiva_id: rawJobId,
              limit: 20,
            });
            if (!isCancelled && fallback?.launches?.length > 0) {
              setLaunches(fallback.launches);
              setSelectedBulkId(fallback.launches[0].bulk_id);
              setIsInitializing(false);
              return;
            }
          }
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
  }, [rawJobId]);

  // Filter snapshot so ONLY this specific job's JobBlock is displayed
  const scopedJob: JobBlockData | null = useMemo(() => {
    if (!snapshot?.jobs || snapshot.jobs.length === 0) return null;
    const cleanJdId = (resolvedJobDivaId || rawJobId || "").trim().toLowerCase();

    // 1. Try matching jobdiva_id
    const matched = snapshot.jobs.find(
      (j) =>
        String(j.jobdiva_id || "").trim().toLowerCase() === cleanJdId ||
        String(j.bulk_jd_id || "").trim().toLowerCase() === cleanJdId
    );
    if (matched) return matched;

    // 2. If single job launch, return that single job
    if (snapshot.jobs.length === 1) return snapshot.jobs[0];

    return snapshot.jobs[0] || null;
  }, [snapshot, resolvedJobDivaId, rawJobId]);

  // Filter anomalies strictly to candidates in this job
  const scopedAnomalies = useMemo(() => {
    if (!snapshot?.anomalies || !scopedJob) return [];
    const jobCandidateInterviewIds = new Set(
      scopedJob.candidates.map((c) => c.interview_id)
    );
    return snapshot.anomalies.filter((a) =>
      jobCandidateInterviewIds.has(a.interview_id)
    );
  }, [snapshot?.anomalies, scopedJob]);

  return (
    <div className="flex flex-col gap-5 p-6 max-w-7xl mx-auto w-full">
      {/* Header Bar */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-200 pb-4">
        <div>
          <div className="flex items-center gap-3">
            <Link
              href={`/jobs/${rawJobId}/rankings`}
              className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold text-slate-600 bg-white hover:bg-slate-100 border border-slate-200 rounded-lg shadow-2xs transition-colors"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              <span>Back to Rankings</span>
            </Link>

            <h1 className="text-lg font-bold tracking-tight text-slate-900">
              {jobTitle || `Job #${resolvedJobDivaId || rawJobId}`}
            </h1>

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
              <span>{isConnected ? "Live Telemetry" : "Connecting..."}</span>
            </div>
          </div>
          <p className="text-xs text-slate-500 mt-1">
            Real-time candidate telemetry and outreach pipeline for Job{" "}
            <span className="font-mono font-medium text-slate-700">
              #{resolvedJobDivaId || rawJobId}
            </span>
          </p>
        </div>

        {/* Controls: Launch selector (if multiple), Reveal PII, Refresh */}
        <div className="flex items-center gap-2.5">
          {launches.length > 1 && (
            <div className="relative">
              <select
                value={selectedBulkId || ""}
                onChange={(e) => setSelectedBulkId(e.target.value)}
                className="text-xs font-medium bg-white border border-slate-300 rounded-lg px-2.5 py-1.5 shadow-2xs pr-7 text-slate-700 focus:outline-none focus:ring-2 focus:ring-indigo-500/20"
              >
                {launches.map((l) => (
                  <option key={l.bulk_id} value={l.bulk_id}>
                    Launch {l.bulk_id.slice(0, 8)} ({l.total_candidates} cands)
                  </option>
                ))}
              </select>
            </div>
          )}

          <button
            type="button"
            onClick={() => setRevealPii((prev) => !prev)}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors shadow-2xs"
          >
            {revealPii ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            <span>{revealPii ? "Mask PII" : "Reveal PII"}</span>
          </button>

          <button
            type="button"
            onClick={() => refreshSnapshot()}
            disabled={isSnapshotLoading}
            className="p-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-colors shadow-2xs"
            title="Refresh Snapshot"
          >
            <RefreshCw className={`h-4 w-4 ${isSnapshotLoading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {/* Error state */}
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
            <div className="py-16 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl">
              Loading live report for this job...
            </div>
          ) : !selectedBulkId || launches.length === 0 ? (
            <div className="py-16 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl">
              No live telemetry launch recorded for Job #{resolvedJobDivaId || rawJobId} yet.
            </div>
          ) : scopedJob ? (
            <JobBlock job={scopedJob} defaultCollapsed={false} />
          ) : (
            <div className="py-16 text-center text-xs text-slate-400 border border-dashed border-slate-200 rounded-xl">
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
                {scopedAnomalies.map((a, i) => (
                  <li key={i} className="pt-1.5 first:pt-0 flex items-start gap-1.5 min-w-0">
                    <span className="text-amber-500 font-bold shrink-0">•</span>
                    <span className="break-words min-w-0 flex-1 leading-snug">
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
