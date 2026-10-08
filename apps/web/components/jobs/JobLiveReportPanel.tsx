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
import type { LaunchListItem, JobBlockData, Snapshot, CandidateRow, Anomaly, TerminalReason } from "@/app/admin/live-report/types";

// Robust candidate name masking utility handling single names, hyphens, and whitespace edge cases
export function maskCandidateName(name: string | null | undefined): string {
  if (!name || typeof name !== "string") return "—";
  const cleaned = name.replace(/[,;]/g, " ").trim();
  if (!cleaned) return "—";
  const parts = cleaned.split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "—";
  return parts.slice(0, 3).map((p) => p[0]?.toUpperCase() || "").filter(Boolean).join(".") + ".";
}

interface FeedItem {
  id: number;
  ts: string;
  text: string;
  critical: boolean;
  tone?: "good" | "critical" | "neutral";
  dedupeKey?: string;
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

  // In-memory cache for raw candidate names (interview_id -> rawName)
  // Cleaned up on jobId switch or unmount to prevent SPA memory leaks
  const rawNamesMapRef = useRef<Map<number, string>>(new Map());
  // Version counter to trigger scopedJob / feed updates when rawNamesMapRef is updated
  const [rawNamesVersion, setRawNamesVersion] = useState<number>(0);

  // Track if we have already fetched unmasked names from the backend
  const hasFetchedUnmaskedRef = useRef<boolean>(false);

  // Live state refs to prevent stale closures in long-running streaming/watchdog callbacks
  const revealPiiRef = useRef(revealPii);
  revealPiiRef.current = revealPii;

  const launchesRef = useRef(launches);
  launchesRef.current = launches;

  const resolvedJobDivaIdRef = useRef(resolvedJobDivaId);
  resolvedJobDivaIdRef.current = resolvedJobDivaId;

  const jobIdRef = useRef(jobId);
  jobIdRef.current = jobId;

  // Push into feed
  const pushFeed = useCallback(
    (text: string, critical = false, tone?: "good" | "critical" | "neutral", dedupeKey?: string) => {
      feedIdCounterRef.current += 1;
      const currentId = feedIdCounterRef.current;
      const resolvedTone = tone || (critical ? "critical" : "neutral");

      setFeed((prev) => [
        {
          id: currentId,
          ts: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
          text,
          critical,
          tone: resolvedTone,
          dedupeKey,
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

      // If SSE sends an unmasked candidate name, only cache it into rawNamesMapRef when revealPii is true
      if (event.name && event.interviewId) {
        if (revealPiiRef.current) {
          rawNamesMapRef.current.set(event.interviewId, event.name);
          setRawNamesVersion((v) => v + 1);
        }
      }

      // Safe subject name for feed: mask name if revealPii is false to prevent PII leakage
      const rawSubject = event.name || rawNamesMapRef.current.get(event.interviewId);
      const safeSubject = revealPiiRef.current
        ? rawSubject || `#${event.interviewId}`
        : rawSubject
        ? maskCandidateName(rawSubject)
        : `#${event.interviewId}`;

      const action = event.type.replace(/_/g, " ");
      const statusText = isFailed ? " [Failed]" : isPassed ? " [Passed]" : "";
      const eventKey = `${event.interviewId}_${event.type}_${event.subtype || ""}_${event.phase || ""}`;
      pushFeed(`${safeSubject}: ${action}${statusText}`, isFailed, tone, eventKey);

      // Mutate candidate in real-time in aggregatedSnapshot
      setAggregatedSnapshot((prev) => {
        if (!prev || !prev.jobs) return prev;
        const updatedJobs = prev.jobs.map((job) => ({
          ...job,
          candidates: job.candidates.map((cand) => {
            if (cand.interview_id === event.interviewId) {
              const updatedPhase = event.phase || cand.phase;
              const isDone = updatedPhase === "completed" || event.type === "evaluation_completed";
              const resolvedTerminalReason: TerminalReason | null | undefined =
                (event.terminalReason as TerminalReason) ||
                (event.status === "failed" ? "outreach_failed" : event.status === "passed" ? "passed" : cand.terminal_reason);

              return {
                ...cand,
                name: (revealPiiRef.current && event.name) ? event.name : cand.name,
                phase: updatedPhase,
                outreach_status: event.status || cand.outreach_status,
                call_outcome: isDone ? "completed" : cand.call_outcome,
                terminal_reason: resolvedTerminalReason,
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
  useEffect(() => {
    activityEventRef.current = handleActivityEvent;
  }, [handleActivityEvent]);


  const activeFetchAbortRef = useRef<AbortController | null>(null);

  // Load snapshot for the entire JobDiva ID in a single API call (zero iteration, zero rate-limit 503s)
  const fetchJobSnapshot = useCallback(
    async (jdId: string, reveal: boolean): Promise<boolean> => {
      const cleanJd = (jdId || "").trim();
      if (!cleanJd) {
        setAggregatedSnapshot(null);
        return false;
      }

      if (activeFetchAbortRef.current) {
        activeFetchAbortRef.current.abort();
      }
      const abortCtrl = new AbortController();
      activeFetchAbortRef.current = abortCtrl;

      setIsRefreshing(true);
      setError(null);

      try {
        const rawSnapshot = await api.liveReport.getJobSnapshot(cleanJd, reveal, abortCtrl.signal);
        if (abortCtrl.signal.aborted) return false;

        if (rawSnapshot && rawSnapshot.status !== "not_found") {
          // If job Title is provided via prop and missing in backend, supply it via shallow clone
          let snapshot = rawSnapshot;
          if (rawSnapshot.jobs && rawSnapshot.jobs.length > 0 && jobTitle && !rawSnapshot.jobs[0].title) {
            snapshot = {
              ...rawSnapshot,
              jobs: [
                {
                  ...rawSnapshot.jobs[0],
                  title: jobTitle,
                },
                ...rawSnapshot.jobs.slice(1),
              ],
            };
          }

          if (reveal) {
            hasFetchedUnmaskedRef.current = true;
            for (const j of snapshot.jobs || []) {
              for (const c of j.candidates || []) {
                if (c.interview_id && c.name) {
                  rawNamesMapRef.current.set(c.interview_id, c.name);
                }
              }
            }
            setRawNamesVersion((v) => v + 1);
          }

          setAggregatedSnapshot(snapshot);
          return true;
        } else {
          setAggregatedSnapshot(null);
          setError(`No telemetry recorded for Job #${cleanJd}.`);
          return false;
        }
      } catch (err: unknown) {
        if (!abortCtrl.signal.aborted) {
          const msg = err instanceof Error ? err.message : "Failed to load live report data";
          console.error("Failed to load job live report:", err);
          setError(msg);
        }
        return false;
      } finally {
        if (!abortCtrl.signal.aborted) {
          setIsRefreshing(false);
        }
      }
    },
    [jobTitle]
  );

  const fetchJobSnapshotRef = useRef(fetchJobSnapshot);
  useEffect(() => {
    fetchJobSnapshotRef.current = fetchJobSnapshot;
  }, [fetchJobSnapshot]);

  // Initialize job and fetch launches with abort controller cleanup
  useEffect(() => {
    const initAbortController = new AbortController();
    let isCancelled = false;

    // Reset in-memory name cache and reveal state whenever jobId changes
    rawNamesMapRef.current.clear();
    hasFetchedUnmaskedRef.current = false;
    setRevealPii(false);

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

        if (isCancelled || initAbortController.signal.aborted) return;
        setResolvedJobDivaId(jdId);

        // Fetch launches for metadata (total launches count, active stream bulk IDs)
        const launchData = await api.liveReport.getLaunches({
          jobdiva_id: jdId,
          limit: 100,
          signal: initAbortController.signal,
        });

        if (isCancelled || initAbortController.signal.aborted) return;
        let launchList: LaunchListItem[] = launchData?.launches || [];

        if (launchList.length === 0 && jdId !== jobId) {
          const fallback = await api.liveReport.getLaunches({
            jobdiva_id: jobId,
            limit: 100,
            signal: initAbortController.signal,
          });
          if (fallback?.launches?.length > 0) {
            launchList = fallback.launches;
          }
        }

        if (isCancelled || initAbortController.signal.aborted) return;
        setLaunches(launchList);

        // Fetch comprehensive snapshot in 1 single API call by JobDiva ID
        await fetchJobSnapshotRef.current(jdId, false);
      } catch (err: unknown) {
        if (!isCancelled && !initAbortController.signal.aborted) {
          const msg = err instanceof Error ? err.message : "Failed to load live report for this job";
          console.error("Failed to initialize job live report:", err);
          setError(msg);
        }
      } finally {
        if (!isCancelled && !initAbortController.signal.aborted) {
          setIsInitializing(false);
        }
      }
    }

    initJobData();
    return () => {
      isCancelled = true;
      initAbortController.abort();
      if (activeFetchAbortRef.current) {
        activeFetchAbortRef.current.abort();
      }
      rawNamesMapRef.current.clear();
      hasFetchedUnmaskedRef.current = false;
    };
  }, [jobId, jobdivaId]);

  // Handle revealPii toggling: if reveal=true and we haven't fetched unmasked names yet, fetch once;
  // otherwise toggle instantly in memory with zero network requests
  const handleToggleRevealPii = useCallback(async () => {
    if (!revealPii) {
      if (!hasFetchedUnmaskedRef.current) {
        try {
          const ok = await fetchJobSnapshot(resolvedJobDivaId || jobId, true);
          if (ok) {
            setRevealPii(true);
          }
        } catch (err) {
          console.error("Failed to reveal candidate names:", err);
        }
      } else {
        setRevealPii(true);
      }
    } else {
      setRevealPii(false);
    }
  }, [revealPii, resolvedJobDivaId, jobId, fetchJobSnapshot]);


  // Only stream launches that are ACTUALLY live (no archived fallback)
  const liveLaunchBulkIds = useMemo(() => {
    return launches.filter((l) => l.state === "live").map((l) => l.bulk_id);
  }, [launches]);

  const [activeConnections, setActiveConnections] = useState<Set<string>>(() => new Set());
  const isConnected = activeConnections.size > 0;

  useEffect(() => {
    if (liveLaunchBulkIds.length === 0 || typeof window === "undefined") {
      setActiveConnections((prev) => (prev.size === 0 ? prev : new Set()));
      return;
    }

    const abortControllers = new Map<string, AbortController>();
    const retryTimeouts = new Map<string, NodeJS.Timeout>();
    const watchdogs = new Map<string, NodeJS.Timeout>();
    const retryCounts = new Map<string, number>();
    let lastWatchdogRefreshAt = 0;
    let trailingWatchdogTimeout: NodeJS.Timeout | null = null;
    let isDisposed = false;

    // Single-flight debounced refresh with trailing call to avoid dropping streams recycled close together
    const triggerDebouncedJobRefresh = () => {
      const now = Date.now();
      if (now - lastWatchdogRefreshAt > 4000) {
        lastWatchdogRefreshAt = now;
        fetchJobSnapshotRef.current(
          resolvedJobDivaIdRef.current || jobIdRef.current,
          revealPiiRef.current
        );
      } else {
        if (trailingWatchdogTimeout) clearTimeout(trailingWatchdogTimeout);
        trailingWatchdogTimeout = setTimeout(() => {
          if (!isDisposed) {
            lastWatchdogRefreshAt = Date.now();
            fetchJobSnapshotRef.current(
              resolvedJobDivaIdRef.current || jobIdRef.current,
              revealPiiRef.current
            );
          }
        }, 4100);
      }
    };

    const startStream = (bulkId: string) => {
      if (isDisposed) return;

      const ac = new AbortController();
      abortControllers.set(bulkId, ac);

      const resetWatchdog = () => {
        const existingWd = watchdogs.get(bulkId);
        if (existingWd) clearTimeout(existingWd);
        const wd = setTimeout(() => {
          console.warn(`Watchdog timeout for live stream ${bulkId}. Recycling...`);
          ac.abort();
          if (!isDisposed) {
            triggerDebouncedJobRefresh();
            startStream(bulkId);
          }
        }, 35000);
        watchdogs.set(bulkId, wd);
      };

      const streamUrl = api.liveReport.streamUrl(bulkId);

      authFetch(streamUrl, {
        signal: ac.signal,
        headers: { Accept: "text/event-stream" },
      })
        .then(async (response) => {
          if (!response.ok || !response.body) {
            if (response.status === 404 || response.status === 403) {
              console.warn(`Live report stream non-retriable status (${response.status}) for ${bulkId}.`);
              return;
            }
            throw new Error(`Stream rejected: ${response.status}`);
          }

          setActiveConnections((prev) => new Set(prev).add(bulkId));
          retryCounts.set(bulkId, 0);
          resetWatchdog();

          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          let receivedAnyEvent = false;

          const handleRawChunk = (rawEvent: string) => {
            resetWatchdog();
            const trimmed = rawEvent.trim();
            if (!trimmed || trimmed.startsWith(":")) return;
            const lines = trimmed.split(/\r?\n/);
            const dataLines = lines
              .filter((l) => l.startsWith("data:"))
              .map((l) => l.replace(/^data:\s?/, ""));
            if (dataLines.length === 0) return;
            // SSE specification joins multiple data lines with "\n"
            const jsonStr = dataLines.join("\n");
            try {
              const payload = JSON.parse(jsonStr);
              if (payload.type === "connected") return;
              const interviewId = payload.interview_id ?? payload.interviewId;
              const eventType = payload.event_type ?? payload.type;
              if (interviewId && eventType && eventType !== "connected") {
                receivedAnyEvent = true;
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

          const delimiterRegex = /\r?\n\r?\n/g;

          while (true) {
            const { done, value } = await reader.read();
            if (done) {
              // If stream cleanly ended without emitting any event, do not reset retry backoff
              if (!receivedAnyEvent) {
                const cur = retryCounts.get(bulkId) || 0;
                retryCounts.set(bulkId, Math.min(cur + 1, 6));
              }
              break;
            }
            buffer += decoder.decode(value, { stream: true });
            let match: RegExpExecArray | null;
            delimiterRegex.lastIndex = 0;
            while ((match = delimiterRegex.exec(buffer)) !== null) {
              const sepIdx = match.index;
              const matchLen = match[0].length;
              const raw = buffer.slice(0, sepIdx);
              buffer = buffer.slice(sepIdx + matchLen);
              delimiterRegex.lastIndex = 0;
              handleRawChunk(raw);
            }
          }
          if (buffer.trim()) handleRawChunk(buffer);
        })
        .catch((err) => {
          if (ac.signal.aborted || isDisposed) return;
          console.warn(`Live report SSE connection dropped for ${bulkId}:`, err);
        })
        .finally(() => {
          setActiveConnections((prev) => {
            if (!prev.has(bulkId)) return prev;
            const next = new Set(prev);
            next.delete(bulkId);
            return next;
          });

          const existingWd = watchdogs.get(bulkId);
          if (existingWd) clearTimeout(existingWd);

          if (!ac.signal.aborted && !isDisposed) {
            const currentRetry = retryCounts.get(bulkId) || 0;
            retryCounts.set(bulkId, Math.min(currentRetry + 1, 6));
            const delay = Math.min(1000 * Math.pow(2, currentRetry), 15000) + Math.random() * 1000;

            const t = setTimeout(() => {
              if (!isDisposed) {
                // Read fresh values from refs to prevent stale closure PII unmasking
                fetchJobSnapshotRef.current(
                  resolvedJobDivaIdRef.current || jobIdRef.current,
                  revealPiiRef.current
                );
                startStream(bulkId);
              }
            }, delay);
            retryTimeouts.set(bulkId, t);
          }
        });
    };

    liveLaunchBulkIds.forEach(startStream);

    return () => {
      isDisposed = true;
      abortControllers.forEach((ac) => ac.abort());
      retryTimeouts.forEach((t) => clearTimeout(t));
      watchdogs.forEach((wd) => clearTimeout(wd));
    };
  }, [liveLaunchBulkIds]);

  // Tab visibility reconciliation
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        fetchJobSnapshotRef.current(
          resolvedJobDivaIdRef.current || jobIdRef.current,
          revealPiiRef.current
        );
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  // Scoped Job from aggregatedSnapshot with in-memory candidate name resolution
  const scopedJob: JobBlockData | null = useMemo(() => {
    if (!aggregatedSnapshot?.jobs || aggregatedSnapshot.jobs.length === 0) return null;
    const baseJob = aggregatedSnapshot.jobs[0];

    // Apply in-memory masking / unmasking to candidates
    const transformedCandidates = (baseJob.candidates || []).map((cand) => {
      const rawName = rawNamesMapRef.current.get(cand.interview_id);
      if (revealPii) {
        // Unmasked mode: use raw name from memory if available
        return {
          ...cand,
          name: rawName || cand.name,
          name_masked: false,
        };
      } else {
        // Masked mode: apply robust initials masking
        const sourceName = rawName || cand.name;
        return {
          ...cand,
          name: maskCandidateName(sourceName),
          name_masked: true,
        };
      }
    });

    return {
      ...baseJob,
      candidates: transformedCandidates,
    };
  }, [aggregatedSnapshot, revealPii, rawNamesVersion]);

  // Recruiter-friendly event formatter matching admin/live-report
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

    // Hide internal noise
    if (t.includes("stt_tts") || t.includes("phase_transition") || t.includes("teams_alert")) {
      return null;
    }

    const isOutcomePassed = event.status === "passed" || p === "pass";

    let action = t.replace(/_/g, " ");
    if (t.includes("interview_started")) {
      action = "Interview Started";
    } else if (t.includes("interview_completed")) {
      action = isOutcomePassed ? "Passed Interview" : "Interview Completed";
    } else if (t.includes("evaluation_completed")) {
      action = isOutcomePassed ? "Passed Evaluation" : "Evaluation Completed";
    } else if (t.includes("interview_partial")) {
      action = "Interview Partial";
    } else if (t.includes("voice_pipeline")) {
      action = st.includes("hangup") || event.status?.includes("hangup") ? "Call Ended (Hangup)" : "Voice Call Completed";
    } else if (t.includes("email_sms") || (t.includes("email") && t.includes("sms"))) {
      action = "Email & SMS Sent";
    } else if (t.includes("email")) {
      action = "Email Sent";
    } else if (t.includes("sms")) {
      action = "SMS Sent";
    } else if (t.includes("call")) {
      action = "Call Placed";
    }

    const subject = event.name || (event.interviewId ? `#${event.interviewId}` : "Candidate");
    const statusText = event.status === "failed" ? " [Failed]" : isOutcomePassed ? " [Passed]" : "";
    return `${subject} (${phaseName}): ${action}${statusText}`;
  }, []);

  // Hydrate activity feed from snapshot events on load (matching admin/live-report behavior)
  useEffect(() => {
    if (!aggregatedSnapshot?.jobs || aggregatedSnapshot.jobs.length === 0) return;
    const MAX_FEED_ITEMS = 50;
    const allRecentEvents: Array<{
      id: number;
      parsedTime: number;
      ts: string;
      text: string;
      critical: boolean;
      tone?: "good" | "critical" | "neutral";
      dedupeKey: string;
    }> = [];

    for (const job of aggregatedSnapshot.jobs) {
      for (const cand of job.candidates) {
        const rawCandName = rawNamesMapRef.current.get(cand.interview_id) || cand.name;
        const resolvedName = revealPii
          ? rawCandName
          : maskCandidateName(rawCandName);

        for (const evt of cand.events || []) {
          const formatted = formatRecruiterEvent({
            interviewId: cand.interview_id,
            name: resolvedName,
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
            cand.terminal_reason === "passed" ||
            (cand.phase === "pass" && (evt.type.includes("evaluation") || evt.type.includes("interview")));
          const tone: "good" | "critical" | "neutral" = isFailed
            ? "critical"
            : isPassed
            ? "good"
            : "neutral";

          const d = evt.ts ? new Date(evt.ts) : new Date(0);
          const parsedTime = !Number.isNaN(d.getTime()) ? d.getTime() : 0;
          const tsStr = parsedTime > 0
            ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
            : "—";

          feedIdCounterRef.current += 1;
          const dedupeKey = `${cand.interview_id}_${evt.type}_${evt.subtype || ""}_${evt.phase || ""}_${evt.ts || ""}`;
          allRecentEvents.push({
            id: feedIdCounterRef.current,
            parsedTime,
            ts: tsStr,
            text: formatted,
            critical: isFailed,
            tone,
            dedupeKey,
          });
        }
      }
    }

    // Sort newest first by timestamp numerically
    allRecentEvents.sort((a, b) => b.parsedTime - a.parsedTime);
    if (allRecentEvents.length > 0) {
      setFeed((prev) => {
        if (prev.length === 0) {
          return allRecentEvents.slice(0, MAX_FEED_ITEMS).map(({ parsedTime, ...item }) => item);
        }
        // Deduplicate using stable event key if available, fallback to text
        const existingKeys = new Set(prev.map((p) => p.dedupeKey || p.text));
        const newItems = allRecentEvents
          .filter((item) => !existingKeys.has(item.dedupeKey))
          .map(({ parsedTime, ...item }) => item);
        if (newItems.length === 0) return prev;
        return [...newItems, ...prev].slice(0, MAX_FEED_ITEMS);
      });
    }
  }, [aggregatedSnapshot, formatRecruiterEvent, revealPii, rawNamesVersion]);


  // Scoped Anomalies
  const scopedAnomalies = useMemo(() => {
    return aggregatedSnapshot?.anomalies || [];
  }, [aggregatedSnapshot]);

  // Memoize candidate name resolution map for activity feed / anomalies
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

  // Status badge calculation: green if streaming, slate if no active runs, amber if connecting
  const badgeConfig = useMemo(() => {
    if (isConnected) {
      return {
        label: "Live Telemetry Active",
        containerClass: "bg-emerald-50 text-emerald-700 border-emerald-200",
        iconClass: "animate-pulse text-emerald-600",
      };
    }
    if (liveLaunchBulkIds.length === 0) {
      return {
        label: "No active runs",
        containerClass: "bg-slate-50 text-slate-600 border-slate-200",
        iconClass: "text-slate-400",
      };
    }
    return {
      label: "Connecting to stream...",
      containerClass: "bg-amber-50 text-amber-700 border-amber-200",
      iconClass: "text-amber-500",
    };
  }, [isConnected, liveLaunchBulkIds.length]);

  return (
    <div className="flex flex-col gap-4 w-full">
      {/* Top Controls Strip */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-white p-3.5 rounded-xl border border-slate-200 shadow-xs">
        <div className="flex items-center gap-3">
          <div
            className={`flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${badgeConfig.containerClass}`}
          >
            <Radio className={`h-3 w-3 ${badgeConfig.iconClass}`} />
            <span>{badgeConfig.label}</span>
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
            onClick={handleToggleRevealPii}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-300 bg-white text-xs font-medium text-slate-700 hover:bg-slate-50 transition-colors shadow-2xs cursor-pointer"
          >
            {revealPii ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
            <span>{revealPii ? "Mask PII" : "Reveal PII"}</span>
          </button>

          <button
            type="button"
            onClick={() => fetchJobSnapshot(resolvedJobDivaId || jobId, revealPii)}
            disabled={isRefreshing}
            className="p-1.5 rounded-lg border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 transition-colors shadow-2xs cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            title="Refresh Live Report"
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
