"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { api, authFetch, isNotFoundError, LIVE_REPORT_PROD_ONLY_MESSAGE } from "@/lib/api";
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
    terminalReason?: string | null;
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

  const abortControllerRef = useRef<AbortController | null>(null);
  const isManuallyClosedRef = useRef<boolean>(false);
  const eventCallbackRef = useRef(onActivityEvent);
  eventCallbackRef.current = onActivityEvent;

  const retryCountRef = useRef<number>(0);
  const retryTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatWatchdogRef = useRef<NodeJS.Timeout | null>(null);
  const lastPulseRef = useRef<number>(Date.now());

  const refreshSnapshotRef = useRef<() => void>(() => {});
  const connectStreamRef = useRef<() => void>(() => {});

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
      if (isNotFoundError(err)) {
        setError(LIVE_REPORT_PROD_ONLY_MESSAGE);
      } else {
        setError(err?.message || "Failed to load launch snapshot");
      }
    } finally {
      setIsLoading(false);
    }
  }, [bulkId, revealPii]);

  refreshSnapshotRef.current = refreshSnapshot;

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
      console.warn("Live report stream heartbeat watchdog timed out (35s silence). Recycling reader...");
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      setIsConnected(false);
      // Trigger snapshot refresh to catch up on any missed data during silence
      refreshSnapshotRef.current();
      connectStreamRef.current();
    }, 35000);
  }, []);

  // 2. Stream connector using authFetch streaming reader with jittered exponential backoff
  const connectStream = useCallback(async () => {
    if (!bulkId || typeof window === "undefined") return;

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    isManuallyClosedRef.current = false;

    const streamUrl = api.liveReport.streamUrl(bulkId);

    try {
      const response = await authFetch(streamUrl, {
        signal: abortController.signal,
        headers: {
          Accept: "text/event-stream",
        },
      });

      if (!response.ok || !response.body) {
        throw new Error(`Stream request rejected with status ${response.status}`);
      }

      setIsConnected(true);
      retryCountRef.current = 0;
      resetWatchdog();

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      const handleRawSSEChunk = (rawEvent: string) => {
        resetWatchdog();
        const trimmed = rawEvent.trim();
        if (!trimmed || trimmed.startsWith(":")) return; // heartbeat / comment

        const lines = trimmed.split("\n");
        const dataLines = lines
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trim());

        if (dataLines.length === 0) return;
        const jsonStr = dataLines.join("");

        try {
          const payload = JSON.parse(jsonStr);
          if (payload.type === "connected") {
            return;
          }
          const interviewId = payload.interview_id ?? payload.interviewId;
          const eventType = payload.event_type ?? payload.type;
          if (interviewId && eventType && eventType !== "connected") {
            eventCallbackRef.current?.({
              interviewId: Number(interviewId),
              type: eventType,
              subtype: payload.subtype ?? null,
              phase: payload.phase ?? null,
              status: payload.status ?? null,
              terminalReason: payload.terminal_reason ?? payload.terminalReason ?? null,
            });
          }
        } catch (err) {
          console.debug("Ignored unparseable SSE event:", jsonStr);
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        let sepIdx: number;
        while ((sepIdx = buffer.indexOf("\n\n")) !== -1) {
          const rawEvent = buffer.slice(0, sepIdx);
          buffer = buffer.slice(sepIdx + 2);
          handleRawSSEChunk(rawEvent);
        }
      }

      // Trailing chunk if any
      if (buffer.trim()) {
        handleRawSSEChunk(buffer);
      }
    } catch (err: any) {
      if (abortController.signal.aborted || isManuallyClosedRef.current) {
        return;
      }
      console.warn("Live report SSE connection dropped. Reconnecting with backoff...", err);
    } finally {
      if (abortController.signal.aborted || isManuallyClosedRef.current) {
        return;
      }

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
        refreshSnapshotRef.current();
        connectStreamRef.current();
      }, delay);
    }
  }, [bulkId, resetWatchdog]);

  connectStreamRef.current = connectStream;

  // Manage Stream Lifecycle (strictly keyed to bulkId, not interrupted by revealPii)
  useEffect(() => {
    if (!bulkId) return;

    connectStream();

    // 3. Tab visibility listener: reconcile state when user switches back to this tab
    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        console.info("Tab became visible. Refreshing live report snapshot to reconcile state...");
        refreshSnapshotRef.current();
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      isManuallyClosedRef.current = true;
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      if (retryTimeoutRef.current) {
        clearTimeout(retryTimeoutRef.current);
      }
      if (heartbeatWatchdogRef.current) {
        clearTimeout(heartbeatWatchdogRef.current);
      }
    };
  }, [bulkId, connectStream]);


  return {
    snapshot,
    setSnapshot,
    isLoading,
    isConnected,
    error,
    refreshSnapshot,
  };
}
