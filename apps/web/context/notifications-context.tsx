"use client";

import { createContext, useContext, useEffect, useRef, useState, useCallback, ReactNode } from "react";
import { useRouter } from "next/navigation";
import { api, authFetch, type NotificationItem } from "@/lib/api";

// "unsupported" covers SSR (no `window`) and browsers without the Notification
// API (e.g. some mobile browsers) — treated like "denied" by the UI, just
// without ever offering the opt-in control.
export type BrowserNotificationPermission = NotificationPermission | "unsupported";

function currentNotificationPermission(): BrowserNotificationPermission {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return Notification.permission;
}

interface NotificationsContextType {
  notifications: NotificationItem[];
  unreadCount: number;
  isConnected: boolean;
  // True until the first list() fetch settles (success or failure). The page
  // uses this to tell "still loading" apart from "confirmed empty" — without
  // it, the empty initial [] state renders "No notifications yet" for a beat
  // on every load, and indefinitely if that first fetch fails and the error
  // goes unnoticed.
  isLoading: boolean;
  hasLoadError: boolean;
  // Mirrors the browser's own Notification.permission — there is no separate
  // app-level opt-in flag, since the permission itself already persists
  // per-origin in the browser.
  notificationPermission: BrowserNotificationPermission;
  requestNotificationPermission: () => void;
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
  const router = useRouter();
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [hasLoadError, setHasLoadError] = useState<boolean>(false);
  const [notificationPermission, setNotificationPermission] =
    useState<BrowserNotificationPermission>(currentNotificationPermission);

  const abortControllerRef = useRef<AbortController | null>(null);
  const isManuallyClosedRef = useRef<boolean>(false);
  const retryCountRef = useRef<number>(0);
  const retryTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatWatchdogRef = useRef<NodeJS.Timeout | null>(null);

  const refreshRef = useRef<() => void>(() => {});
  const connectStreamRef = useRef<() => void>(() => {});
  // Highest notification id this tab has ever rendered, from either the
  // REST list() or an SSE push. Sent back to the server as ?since_id= on
  // (re)connect: the server's own cursor is in-memory and per-worker, so a
  // reconnect landing on a different worker would otherwise reseed from
  // "now" and silently skip anything created in the gap — this client-side
  // watermark is authoritative regardless of which worker answers.
  const lastSeenIdRef = useRef<number>(0);

  // Browsers require a user gesture (or at least discourage auto-prompting
  // without one) before Notification.requestPermission() — calling it
  // unprompted on mount would also just be bad UX. The page surfaces its own
  // opt-in control and calls this in response to a click.
  const requestNotificationPermission = useCallback(() => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    Notification.requestPermission()
      .then(setNotificationPermission)
      .catch((err) => console.error("Failed to request notification permission:", err));
  }, []);

  // Native OS-level popup for a new notification, as a companion to the
  // in-app badge/feed — only when permission is granted AND the tab isn't
  // the one currently in front (if it is, the in-app feed already surfaced
  // it; a second popup would just be noise). `tag` de-dupes at the OS level
  // if this somehow fires twice for the same id.
  const showBrowserNotification = useCallback(
    (item: NotificationItem) => {
      if (typeof window === "undefined" || !("Notification" in window)) return;
      if (Notification.permission !== "granted") return;
      if (document.visibilityState === "visible") return;
      try {
        const popup = new Notification(item.title, {
          body: item.body ?? undefined,
          tag: `pair-notification-${item.id}`,
          icon: "/favicon.ico",
        });
        popup.onclick = () => {
          window.focus();
          if (item.jobdiva_id && item.candidate_id) {
            router.push(
              `/jobs/${encodeURIComponent(item.jobdiva_id)}/report?candidateId=${encodeURIComponent(item.candidate_id)}`
            );
          }
          popup.close();
        };
      } catch (err) {
        console.error("Failed to show browser notification:", err);
      }
    },
    [router]
  );

  // Truth-up fetch: initial load, and re-sync after any reconnect so nothing
  // dropped between a lost connection and the next one is lost for good.
  const refresh = useCallback(async () => {
    try {
      const data = await api.notifications.list({ limit: 50 });
      setNotifications(data.notifications);
      setUnreadCount(data.unread_count);
      setHasLoadError(false);
      for (const n of data.notifications) {
        if (n.id > lastSeenIdRef.current) lastSeenIdRef.current = n.id;
      }
    } catch (err) {
      console.error("Failed to fetch notifications:", err);
      // Deliberately leave `notifications` as-is on failure (don't clear it
      // to []) — a transient error reconciling a reconnect should never make
      // already-loaded rows disappear from the screen.
      setHasLoadError(true);
    } finally {
      setIsLoading(false);
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
      const response = await authFetch(api.notifications.streamUrl(lastSeenIdRef.current || undefined), {
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
          if (item.id > lastSeenIdRef.current) lastSeenIdRef.current = item.id;
          setNotifications((prev) => {
            // Only bump the unread count the first time we see this id — a
            // reconnect's truth-up refresh() may have already loaded it,
            // and without this check that row would be counted twice.
            const alreadyKnown = prev.some((n) => n.id === item.id);
            if (!alreadyKnown && !item.read_at) {
              setUnreadCount((count) => count + 1);
              showBrowserNotification(item);
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
  }, [resetWatchdog, showBrowserNotification]);

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
      value={{
        notifications,
        unreadCount,
        isConnected,
        isLoading,
        hasLoadError,
        notificationPermission,
        requestNotificationPermission,
        refresh,
        markRead,
        markAllRead,
      }}
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
