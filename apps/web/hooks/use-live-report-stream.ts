"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "@/lib/api";
import type { Snapshot } from "../app/admin/live-report/types";

interface UseLiveReportStreamOptions {
  bulkId: string | null;
  revealPii?: boolean;
  onActivityEvent?: (event: {
    interviewId: number;
    type: string;
    subtype: string | null;
    phase: string | null;
    status: string | null;
  }) => void;
}

export function useLiveReportStream({
  bulkId,
  revealPii = false,
  onActivityEvent,
}: UseLiveReportStreamOptions) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const eventSourceRef = useRef<EventSource | null>(null);
  const eventCallbackRef = useRef(onActivityEvent);
  eventCallbackRef.current = onActivityEvent;

  const retryCountRef = useRef<number>(0);
  const retryTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatWatchdogRef = useRef<NodeJS.Timeout | null>(null);
  const lastPulseRef = useRef<number>(Date.now());

  // 1. Snapshot fetcher (truth-up)
  const refreshSnapshot = useCallback(async () => {
    if (!bulkId) return;
    try {
      setIsLoading(true);
      const data = await api.liveReport.getSnapshot(bulkId, revealPii);
      setSnapshot(data);
      setError(null);
    } catch (err: any) {
      console.error("Failed to fetch live report snapshot:", err);
      setError(err?.message || "Failed to load launch snapshot");
    } finally {
      setIsLoading(false);
    }
  }, [bulkId, revealPii]);

  // Initial snapshot fetch when bulkId or reveal changes
  useEffect(() => {
    if (!bulkId) {
      setSnapshot(null);
      setIsConnected(false);
      return;
    }
    refreshSnapshot();
  }, [bulkId, refreshSnapshot]);

  // Reset watchdog on heartbeat or delta
  const resetWatchdog = useCallback(() => {
    lastPulseRef.current = Date.now();
    if (heartbeatWatchdogRef.current) {
      clearTimeout(heartbeatWatchdogRef.current);
    }
    // If no heartbeat or event seen in 35 seconds, tear down dead socket and reconnect
    heartbeatWatchdogRef.current = setTimeout(() => {
      console.warn("Live report stream heartbeat watchdog timed out (35s silence). Recycling socket...");
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      setIsConnected(false);
      // Trigger snapshot refresh to catch up on any missed data during silence
      refreshSnapshot();
      connectStream();
    }, 35000);
  }, [refreshSnapshot]);

  // 2. Stream connector with jittered exponential backoff
  const connectStream = useCallback(() => {
    if (!bulkId || typeof window === "undefined") return;

    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }

    const streamUrl = api.liveReport.streamUrl(bulkId);
    const es = new EventSource(streamUrl, { withCredentials: true });
    eventSourceRef.current = es;

    es.onopen = () => {
      setIsConnected(true);
      retryCountRef.current = 0; // reset retry backoff on successful open
      resetWatchdog();
    };

    es.onmessage = (e) => {
      resetWatchdog();
      if (!e.data || e.data.startsWith(":")) return; // heartbeat comment

      try {
        const payload = JSON.parse(e.data);
        if (payload.type === "connected") {
          return;
        }
        if (payload.type === "activity" && payload.interview_id) {
          eventCallbackRef.current?.({
            interviewId: Number(payload.interview_id),
            type: payload.event_type,
            subtype: payload.subtype ?? null,
            phase: payload.phase ?? null,
            status: payload.status ?? null,
          });
        }
      } catch (err) {
        console.debug("Ignored unparseable SSE event:", e.data);
      }
    };

    es.onerror = (err) => {
      console.warn("Live report SSE connection dropped. Reconnecting with backoff...", err);
      es.close();
      eventSourceRef.current = null;
      setIsConnected(false);

      if (heartbeatWatchdogRef.current) {
        clearTimeout(heartbeatWatchdogRef.current);
      }

      // Reconnect with exponential backoff + jitter
      const retry = retryCountRef.current;
      retryCountRef.current = Math.min(retry + 1, 6);
      const delay = Math.min(1000 * Math.pow(2, retry), 15000) + Math.random() * 1000;

      if (retryTimeoutRef.current) clearTimeout(retryTimeoutRef.current);
      retryTimeoutRef.current = setTimeout(() => {
        // Re-fetch snapshot upon reconnect to reconcile any dropped deltas
        refreshSnapshot();
        connectStream();
      }, delay);
    };
  }, [bulkId, resetWatchdog, refreshSnapshot]);

  // Manage Stream Lifecycle
  useEffect(() => {
    if (!bulkId) return;

    connectStream();

    // 3. Tab visibility listener: reconcile state when user switches back to this tab
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        console.info("Tab became visible. Refreshing live report snapshot to reconcile state...");
        refreshSnapshot();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
        eventSourceRef.current = null;
      }
      if (retryTimeoutRef.current) {
        clearTimeout(retryTimeoutRef.current);
      }
      if (heartbeatWatchdogRef.current) {
        clearTimeout(heartbeatWatchdogRef.current);
      }
    };
  }, [bulkId, connectStream, refreshSnapshot]);

  return {
    snapshot,
    setSnapshot,
    isLoading,
    isConnected,
    error,
    refreshSnapshot,
  };
}
