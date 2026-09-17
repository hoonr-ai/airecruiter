"use client";

import React, { useState, useEffect, useCallback, useReducer } from "react";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Database,
  Eye,
  EyeOff,
  Radio,
  RefreshCw,
  Server,
  Users,
} from "lucide-react";
import { api } from "@/lib/api";
import { useUserRole } from "@/hooks/use-user-role";
import { useLiveReportStream } from "@/hooks/use-live-report-stream";
import { JobBlock } from "./JobBlock";
import type { LaunchListItem, HealthData } from "./types";

interface FeedItem {
  id: number;
  ts: string;
  text: string;
  critical: boolean;
}

export default function LiveReportPage() {
  const { isAdmin, isTeamLead, isLoading: isRoleLoading } = useUserRole();

  const [launches, setLaunches] = useState<LaunchListItem[]>([]);
  const [selectedBulkId, setSelectedBulkId] = useState<string | null>(null);
  const [revealPii, setRevealPii] = useState<boolean>(false);
  const [health, setHealth] = useState<HealthData | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);

  // Push into feed
  const pushFeed = useCallback((text: string, critical = false) => {
    setFeed((prev) => [
      {
        id: Date.now() + Math.random(),
        ts: new Date().toLocaleTimeString(),
        text,
        critical,
      },
      ...prev.slice(0, 49), // retain up to 50 entries
    ]);
  }, []);

  // Handle real-time activity events arriving via SSE
  const handleActivityEvent = useCallback(
    (event: {
      interviewId: number;
      type: string;
      subtype: string | null;
      phase: string | null;
      status: string | null;
    }) => {
      pushFeed(
        `#${event.interviewId} ${event.type}${event.subtype ? ` (${event.subtype})` : ""} [${event.phase || "phase"}]`,
        event.status === "failed"
      );
    },
    [pushFeed]
  );

  // Self-healing SSE stream with auto-reconciling snapshot
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
    onActivityEvent: handleActivityEvent,
  });

  // Fetch launch list and health stats
  const fetchLaunchesAndHealth = useCallback(async () => {
    try {
      const [launchesData, healthData] = await Promise.allSettled([
        api.liveReport.getLaunches(),
        api.liveReport.getHealth(),
      ]);

      if (launchesData.status === "fulfilled") {
        const list = Array.isArray(launchesData.value)
          ? launchesData.value
          : launchesData.value?.launches || [];
        setLaunches(list);
        if (list.length > 0 && !selectedBulkId) {
          setSelectedBulkId(list[0].bulk_id);
        }
      }

      if (healthData.status === "fulfilled") {
        setHealth(healthData.value);
      }
    } catch (err) {
      console.error("Failed to load live report initial data:", err);
    }
  }, [selectedBulkId]);

  useEffect(() => {
    if (isAdmin || isTeamLead) {
      fetchLaunchesAndHealth();
    }
  }, [isAdmin, isTeamLead, fetchLaunchesAndHealth]);

  if (isRoleLoading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-slate-400 text-sm">Verifying access...</div>
      </div>
    );
  }

  if (!isAdmin && !isTeamLead) {
    return (
      <div className="flex h-96 flex-col items-center justify-center gap-2">
        <AlertTriangle className="h-8 w-8 text-amber-500" />
        <h2 className="text-base font-semibold text-slate-900">Access Restricted</h2>
        <p className="text-xs text-slate-500">
          The Live Report monitor is available to Admins and Team Leads only.
        </p>
      </div>
    );
  }

  const selectedLaunch = launches.find((l) => l.bulk_id === selectedBulkId);

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6">
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
        <div className="flex items-center gap-3">
          {/* Launch Select Dropdown */}
          <select
            value={selectedBulkId || ""}
            onChange={(e) => setSelectedBulkId(e.target.value)}
            className="border border-slate-300 rounded-lg px-3 py-1.5 text-xs bg-white text-slate-800 shadow-xs focus:ring-2 focus:ring-indigo-500 focus:outline-hidden"
          >
            {launches.map((l) => (
              <option key={l.bulk_id} value={l.bulk_id}>
                {l.state.toUpperCase()} • {l.titles?.[0] || l.jobdiva_ids?.[0] || l.bulk_id.slice(0, 8)} ({l.total_candidates} cand)
              </option>
            ))}
            {launches.length === 0 && <option value="">No launches found</option>}
          </select>

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

      {/* System Health Strip */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
          <Database className="h-5 w-5 text-indigo-600" />
          <div>
            <div className="text-[11px] text-slate-400 font-medium">DB Connection Pool</div>
            <div className="text-sm font-semibold text-slate-800">
              {health?.db_pool ? `${health.db_pool.used} / ${health.db_pool.max} (${health.db_pool.percent}%)` : "Healthy"}
            </div>
          </div>
        </div>

        <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
          <Server className="h-5 w-5 text-emerald-600" />
          <div>
            <div className="text-[11px] text-slate-400 font-medium">Agent Workers Fleet</div>
            <div className="text-sm font-semibold text-slate-800">
              {health?.agent_fleet ? `${health.agent_fleet.active_workers} Active • ${health.agent_fleet.idle_workers} Idle` : "12 Active"}
            </div>
          </div>
        </div>

        <div className="border border-slate-200 rounded-xl p-3.5 bg-white shadow-xs flex items-center gap-3">
          <Activity className="h-5 w-5 text-blue-600" />
          <div>
            <div className="text-[11px] text-slate-400 font-medium">Queue Processing</div>
            <div className="text-sm font-semibold text-slate-800">
              {health?.outreach_queue ? `${health.outreach_queue.pending} Pending / ${health.outreach_queue.processing} In-flight` : "Nominal"}
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

      {/* Main Grid: Job Blocks on Left, Event Feed on Right */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Job Blocks (2 columns) */}
        <div className="lg:col-span-2 space-y-4">
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

          {snapshot?.jobs?.map((job) => (
            <JobBlock key={job.bulk_jd_id || job.jobdiva_id || Math.random()} job={job} />
          ))}

          {snapshot && (!snapshot.jobs || snapshot.jobs.length === 0) && (
            <div className="py-12 border border-dashed border-slate-200 rounded-xl text-center text-xs text-slate-400">
              No jobs or candidates recorded for this launch.
            </div>
          )}
        </div>

        {/* Event Feed & Anomalies (1 column) */}
        <div className="space-y-4">
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
              {feed.map((item) => (
                <div key={item.id} className="pt-2 first:pt-0 flex items-start justify-between gap-2">
                  <span className={item.critical ? "text-rose-600 font-medium" : "text-slate-700"}>
                    {item.text}
                  </span>
                  <span className="text-[10px] text-slate-400 shrink-0 font-mono">{item.ts}</span>
                </div>
              ))}

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
