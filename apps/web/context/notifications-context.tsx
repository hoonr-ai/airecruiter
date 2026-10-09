"use client";

import { createContext, useContext, useEffect, useRef, useState, useCallback, ReactNode } from "react";
import { api, authFetch, type NotificationItem } from "@/lib/api";

interface NotificationsContextType {
  notifications: NotificationItem[];
  unreadCount: number;
  isConnected: boolean;
  refresh: () => void;
  markRead: (id: number) => void;
  markAllRead: () => void;
}

const NotificationsContext = createContext<NotificationsContextType | undefined>(undefined);

// Mounted ONCE in app/layout.tsx. Both the sidebar badge and the /notifications
// page read from this single provider — each used to call useNotificationsStream()
// independently, which opened two SSE connections per tab (one per mount) and
// kept two disconnected copies of the unread count, so "Mark all read" on the
// page left the sidebar badge stale until its next reconnect.
export function NotificationsProvider({ children }: { children: ReactNode }) {
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [isConnected, setIsConnected] = useState<boolean>(false);

  const abortControllerRef = useRef<AbortController | null>(null);
  const isManuallyClosedRef = useRef<boolean>(false);
  const retryCountRef = useRef<number>(0);
  const retryTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatWatchdogRef = useRef<NodeJS.Timeout | null>(null);

  const refreshRef = useRef<() => void>(() => {});
  const connectStreamRef = useRef<() => void>(() => {});

  // Truth-up fetch: initial load, and re-sync after any reconnect so nothing
  // dropped between a lost connection and the next one is lost for good.
  const refresh = useCallback(async () => {
    try {
      const data = await api.notifications.list({ limit: 50 });
      setNotifications(data.notifications);
      setUnreadCount(data.unread_count);
    } catch (err) {
      console.error("Failed to fetch notifications:", err);
    }
  }, []);
  refreshRef.current = refresh;

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const resetWatchdog = useCallback(() => {
    if (heartbeatWatchdogRef.current) {
      clearTimeout(heartbeatWatchdogRef.current);
    }
    // Server polls every ~3.5s and always sends a heartbeat comment when idle;
    // 35s of silence means the connection is dead.
    heartbeatWatchdogRef.current = setTimeout(() => {
      console.warn("Notifications stream heartbeat watchdog timed out (35s silence). Reconnecting...");
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
      setIsConnected(false);
      refreshRef.current();
      connectStreamRef.current();
    }, 35000);
  }, []);

  const connectStream = useCallback(async () => {
    if (typeof window === "undefined") return;

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }

    const abortController = new AbortController();
    abortControllerRef.current = abortController;
    isManuallyClosedRef.current = false;
    let isNonRetriable = false;

    try {
      const response = await authFetch(api.notifications.streamUrl(), {
        signal: abortController.signal,
        headers: { Accept: "text/event-stream" },
      });

      if (!response.ok || !response.body) {
        if (response.status === 401 || response.status === 403) {
          // Logged-out / expired session: backing off and retrying forever
          // just spams a dead endpoint. Stop; the next real page load (post
          // re-auth) mounts a fresh provider and tries again.
          console.warn(`Notifications stream unauthorized (${response.status}). Not retrying.`);
          isNonRetriable = true;
          return;
        }
        throw new Error(`Notifications stream rejected with status ${response.status}`);
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

        const dataLines = trimmed
          .split("\n")
          .filter((l) => l.startsWith("data:"))
          .map((l) => l.slice(5).trim());
        if (dataLines.length === 0) return;

        try {
          const item: NotificationItem = JSON.parse(dataLines.join(""));
          setNotifications((prev) => {
            // Only bump the unread count the first time we see this id — a
            // reconnect's truth-up refresh() may have already loaded it,
            // and without this check that row would be counted twice.
            const alreadyKnown = prev.some((n) => n.id === item.id);
            if (!alreadyKnown && !item.read_at) {
              setUnreadCount((count) => count + 1);
            }
            return [item, ...prev.filter((n) => n.id !== item.id)];
          });
        } catch {
          console.debug("Ignored unparseable notification event:", dataLines.join(""));
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
      if (buffer.trim()) {
        handleRawSSEChunk(buffer);
      }
    } catch (err: unknown) {
      if (abortController.signal.aborted || isManuallyClosedRef.current) {
        return;
      }
      console.warn("Notifications SSE connection dropped. Reconnecting with backoff...", err);
    } finally {
      if (abortController.signal.aborted || isManuallyClosedRef.current || isNonRetriable) {
        setIsConnected(false);
        if (heartbeatWatchdogRef.current) clearTimeout(heartbeatWatchdogRef.current);
        return;
      }

      setIsConnected(false);
      if (heartbeatWatchdogRef.current) clearTimeout(heartbeatWatchdogRef.current);

      const retry = retryCountRef.current;
      retryCountRef.current = Math.min(retry + 1, 6);
      const delay = Math.min(1000 * Math.pow(2, retry), 15000) + Math.random() * 1000;

      if (retryTimeoutRef.current) clearTimeout(retryTimeoutRef.current);
      retryTimeoutRef.current = setTimeout(() => {
        refreshRef.current();
        connectStreamRef.current();
      }, delay);
    }
  }, [resetWatchdog]);

  connectStreamRef.current = connectStream;

  useEffect(() => {
    connectStream();

    const handleVisibilityChange = () => {
      if (document.visibilityState === "visible") {
        refreshRef.current();
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
      if (retryTimeoutRef.current) clearTimeout(retryTimeoutRef.current);
      if (heartbeatWatchdogRef.current) clearTimeout(heartbeatWatchdogRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const markRead = useCallback(async (id: number) => {
    // Functional update, with no `notifications` in the dependency array:
    // keeps this callback's identity stable across every notification
    // change instead of changing on every single update.
    let alreadyRead = false;
    setNotifications((prev) =>
      prev.map((n) => {
        if (n.id !== id) return n;
        if (n.read_at) alreadyRead = true;
        return n.read_at ? n : { ...n, read_at: new Date().toISOString() };
      })
    );
    if (alreadyRead) return;
    setUnreadCount((prev) => Math.max(0, prev - 1));
    try {
      await api.notifications.markRead(id);
    } catch (err) {
      console.error("Failed to mark notification read:", err);
      refreshRef.current();
    }
  }, []);

  const markAllRead = useCallback(async () => {
    const now = new Date().toISOString();
    setNotifications((prev) => prev.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    setUnreadCount(0);
    try {
      await api.notifications.markAllRead();
    } catch (err) {
      console.error("Failed to mark all notifications read:", err);
      refreshRef.current();
    }
  }, []);

  return (
    <NotificationsContext.Provider
      value={{ notifications, unreadCount, isConnected, refresh, markRead, markAllRead }}
    >
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotificationsStream(): NotificationsContextType {
  const ctx = useContext(NotificationsContext);
  if (!ctx) {
    throw new Error("useNotificationsStream must be used within a NotificationsProvider");
  }
  return ctx;
}
