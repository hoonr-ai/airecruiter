"use client";

import { createContext, useContext, useEffect, useRef, useState, useCallback, ReactNode } from "react";
import { useRouter } from "next/navigation";
import { api, authFetch, getActiveUserEmail, type NotificationItem } from "@/lib/api";
import {
  type BrowserNotificationPermission,
  normalizePermissionState,
  notificationClickPath,
  shouldShowBrowserPopup,
} from "@/lib/browser-notifications";
import { NOTIFICATIONS_CACHE_KEY, readCachedNotifications, writeCachedNotifications } from "@/lib/notifications-cache";
import { playNotificationChime, unlockNotificationSound } from "@/lib/notification-sound";
import { isDesktopTypeEnabled } from "@/lib/desktop-notification-preferences";
import { getSafeLocalStorage } from "@/lib/safe-local-storage";
import { shouldApplyRefreshResult } from "@/lib/notifications-refresh-guard";

export type { BrowserNotificationPermission };

function currentNotificationPermission(): BrowserNotificationPermission {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return Notification.permission;
}

// Debounce between a notifications/unreadCount change and the cache write it
// triggers — an active SSE session can update state several times in quick
// succession, and there's no reason to hit localStorage on every single one.
const CACHE_WRITE_DEBOUNCE_MS = 500;

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
  // True after clearAll's optimistic update failed to persist server-side —
  // the page surfaces this so the user knows "Clear" didn't actually stick,
  // rather than it failing silently.
  clearError: boolean;
  // Mirrors the browser's own Notification.permission — there is no separate
  // app-level opt-in flag, since the permission itself already persists
  // per-origin in the browser.
  notificationPermission: BrowserNotificationPermission;
  requestNotificationPermission: () => void;
  refresh: () => void;
  markRead: (id: number) => void;
  markUnread: (id: number) => void;
  markAllRead: () => void;
  clearAll: () => void;
}

const NotificationsContext = createContext<NotificationsContextType | undefined>(undefined);

// Mounted ONCE in app/layout.tsx. Both the sidebar badge and the /notifications
// page read from this single provider — each used to call useNotificationsStream()
// independently, which opened two SSE connections per tab (one per mount) and
// kept two disconnected copies of the unread count, so "Mark all read" on the
// page left the sidebar badge stale until its next reconnect.
export function NotificationsProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  // These three all start at the value the SERVER would render ([], 0,
  // "unsupported" — there is no window/localStorage/Notification API during
  // SSR) and are only ever populated from a browser API inside an effect,
  // i.e. strictly AFTER the first client render. Reading window/localStorage
  // in a useState lazy initializer instead would make that first client
  // render disagree with the server-rendered markup and trigger a React
  // hydration mismatch.
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unreadCount, setUnreadCount] = useState<number>(0);
  const [isConnected, setIsConnected] = useState<boolean>(false);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [hasLoadError, setHasLoadError] = useState<boolean>(false);
  const [clearError, setClearError] = useState<boolean>(false);
  const [notificationPermission, setNotificationPermission] = useState<BrowserNotificationPermission>("unsupported");

  const abortControllerRef = useRef<AbortController | null>(null);
  const isManuallyClosedRef = useRef<boolean>(false);
  const retryCountRef = useRef<number>(0);
  const retryTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatWatchdogRef = useRef<NodeJS.Timeout | null>(null);

  const refreshRef = useRef<() => void>(() => {});
  const connectStreamRef = useRef<() => void>(() => {});
  const cacheWriteTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  // Highest notification id this tab has ever rendered, from either the
  // REST list() or an SSE push. Sent back to the server as ?since_id= on
  // (re)connect: the server's own cursor is in-memory and per-worker, so a
  // reconnect landing on a different worker would otherwise reseed from
  // "now" and silently skip anything created in the gap — this client-side
  // watermark is authoritative regardless of which worker answers. Also
  // seeded from the local cache on mount (below), so a reload doesn't
  // needlessly re-announce history the cache already had.
  const lastSeenIdRef = useRef<number>(0);
  // Mirrors `notifications` after every render, so markRead/markUnread can
  // decide "is this a no-op?" by reading a plain snapshot instead of doing
  // that check inside the setNotifications updater itself — a functional
  // updater can run more than once for the same update (React StrictMode,
  // concurrent rendering), and a side effect assigned to a variable from
  // inside one (the old pattern here) could then read a stale value if the
  // updater's first invocation hadn't "happened" yet when it was checked.
  const notificationsRef = useRef<NotificationItem[]>([]);
  useEffect(() => {
    notificationsRef.current = notifications;
  }, [notifications]);
  // Bumped at the start of every optimistic local mutation (markRead/
  // markUnread/markAllRead/clearAll) — see refresh() below. refresh() is a
  // full GET that unconditionally overwrites `notifications`/`unreadCount`,
  // and it's kicked off from several places that race a user action: once
  // on mount, on every tab visibility change, on SSE reconnect, and on a
  // cross-tab cache-write signal. Without this guard, a slow refresh() that
  // started BEFORE a click can resolve AFTER it and silently revert the
  // optimistic update it just applied — the user sees the action "undo
  // itself," and only a second click (now racing nothing) actually sticks.
  const localMutationVersionRef = useRef(0);
  // PR #831 review: the version check alone still missed one window — a
  // refresh() that STARTS after a mutation's optimistic update (so it
  // captures the already-bumped version) but whose GET reads the DB before
  // that mutation's own POST has committed. Its response then matches the
  // "current" version exactly, so the version check alone would wave it
  // through. This counts the mutations whose own request is still in
  // flight; refresh() also discards its response while this is non-zero.
  const inFlightMutationCountRef = useRef(0);
  // Set when refresh() discards a response for either reason above — tells
  // whichever mutation is holding things up to kick off exactly one
  // follow-up refresh once it (and any other concurrently in-flight
  // mutation) settles, so the list doesn't sit on a stale/skipped snapshot
  // until the next unrelated trigger (a visibility change, an SSE
  // reconnect, a cross-tab signal) happens to come along.
  const pendingReconcileRef = useRef(false);
  // Every id this tab has already applied a side effect for (unread bump +
  // desktop popup) — checked and updated OUTSIDE the setNotifications
  // updater (see handleRawSSEChunk below). React may invoke a state updater
  // more than once for the same update (StrictMode, concurrent features), so
  // an updater must stay pure; a side effect that ran from inside one could
  // silently fire twice.
  const notifiedIdsRef = useRef<Set<number>>(new Set());
  // The App Router instance is stable in practice, but showBrowserNotification
  // is created once and lives for the lifetime of the SSE connection (the
  // mounting effect has a deliberately empty deps array) — reading `router`
  // through a ref, refreshed every render, means a click always navigates
  // with whatever router instance is current rather than whichever one
  // existed when the popup was created.
  const routerRef = useRef(router);
  routerRef.current = router;

  // Browsers require a user gesture (or at least discourage auto-prompting
  // without one) before Notification.requestPermission() — calling it
  // unprompted on mount would also just be bad UX. The page surfaces its own
  // opt-in control and calls this in response to a click.
  const requestNotificationPermission = useCallback(() => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    // This click is the one real user gesture in the whole desktop-
    // notification flow — resume the shared chime AudioContext here while
    // it's still on the gesture stack, so the later async chime (triggered
    // by an SSE push, not a click) can actually play under strict browser
    // autoplay policies.
    unlockNotificationSound();
    Notification.requestPermission()
      .then(setNotificationPermission)
      .catch((err) => console.error("Failed to request notification permission:", err));
  }, []);

  // Resolve the REAL permission after mount, not during the initial render
  // (see the useState above) — and then keep it in sync with a change made
  // outside the app (the browser's own site settings), via the Permissions
  // API where supported (Safari's support is recent/partial, hence
  // best-effort: Notification.permission itself, read synchronously below,
  // is universally supported and already correct even where the live-sync
  // part isn't).
  useEffect(() => {
    setNotificationPermission(currentNotificationPermission());
    if (typeof navigator === "undefined" || !navigator.permissions?.query) return;
    let status: PermissionStatus | null = null;
    let cancelled = false;
    navigator.permissions
      .query({ name: "notifications" as PermissionName })
      .then((result) => {
        if (cancelled) return;
        status = result;
        setNotificationPermission(normalizePermissionState(result.state));
        status.onchange = () => setNotificationPermission(normalizePermissionState(status!.state));
      })
      .catch(() => {
        // Unsupported in this browser — requestNotificationPermission's own
        // return value is still the source of truth after an explicit click.
      });
    return () => {
      cancelled = true;
      if (status) status.onchange = null;
    };
  }, []);

  // A recruiter who already granted desktop-notification permission in an
  // earlier session never clicks "Enable" this session — requestNotification
  // Permission's own unlockNotificationSound() call never fires for them, so
  // without this they'd never unlock the chime's AudioContext at all, and
  // every SSE-triggered chime would be silently dropped by the autoplay
  // policy. A one-time pointerdown/keydown anywhere on the page (removed
  // after the first fire, or on unmount/permission change) stands in for
  // that missing "Enable" click.
  useEffect(() => {
    if (notificationPermission !== "granted" || typeof window === "undefined") return;
    const unlock = () => unlockNotificationSound();
    window.addEventListener("pointerdown", unlock, { once: true });
    window.addEventListener("keydown", unlock, { once: true });
    return () => {
      window.removeEventListener("pointerdown", unlock);
      window.removeEventListener("keydown", unlock);
    };
  }, [notificationPermission]);

  // Native OS-level popup for a new notification, as a companion to the
  // in-app badge/feed — only when permission is granted and the user
  // plausibly wouldn't already see the in-app update (see
  // shouldShowBrowserPopup). `tag` de-dupes at the OS level if this somehow
  // fires twice for the same id.
  const showBrowserNotification = useCallback((item: NotificationItem) => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (!shouldShowBrowserPopup(Notification.permission, document.visibilityState === "visible", document.hasFocus())) {
      return;
    }
    // In-app stays on for every type regardless (this function is only ever
    // reached for the desktop popup path) — this is the recruiter's own
    // per-type opt-out from the Notifications page's preferences panel.
    if (!isDesktopTypeEnabled(getSafeLocalStorage(), item.type)) return;
    try {
      const popup = new Notification(item.title, {
        body: item.body ?? undefined,
        tag: `pair-notification-${item.id}`,
        icon: "/icon.png",
      });
      popup.onclick = () => {
        window.focus();
        routerRef.current.push(notificationClickPath(item));
        popup.close();
      };
      // Tied to the popup actually being shown, not to every incoming
      // notification — playNotificationChime checks the mute preference
      // itself, so there's nothing to gate here beyond "a popup fired."
      playNotificationChime();
    } catch (err) {
      console.error("Failed to show browser notification:", err);
    }
  }, []);

  // Truth-up fetch: initial load, and re-sync after any reconnect so nothing
  // dropped between a lost connection and the next one is lost for good.
  const refresh = useCallback(async () => {
    const versionAtStart = localMutationVersionRef.current;
    try {
      const data = await api.notifications.list({ limit: 50 });
      // Advanced unconditionally, even on the discarded path below — both
      // are append-only, idempotent bookkeeping (a pure "highest id seen" /
      // "don't re-pop this one" record) regardless of whose response landed,
      // so there's no correctness reason to gate them on freshness too.
      for (const n of data.notifications) {
        if (n.id > lastSeenIdRef.current) lastSeenIdRef.current = n.id;
        // Already surfaced via this fetch — an SSE push for the same id
        // later (a reconnect replaying a bit of history) must not re-bump
        // the unread count or re-pop a desktop notification.
        notifiedIdsRef.current.add(n.id);
      }
      if (!shouldApplyRefreshResult(versionAtStart, localMutationVersionRef.current, inFlightMutationCountRef.current)) {
        // Stale relative to a local mutation that started, or is still
        // settling — applying it now would silently undo that more recent
        // action. Skip, and flag it so that mutation's own finally block
        // kicks off one follow-up refresh once it settles (see markRead and
        // friends below) instead of leaving the list on this skipped
        // snapshot until some unrelated trigger happens to re-sync it.
        pendingReconcileRef.current = true;
        return;
      }
      setNotifications(data.notifications);
      setUnreadCount(data.unread_count);
      setHasLoadError(false);
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

  // Seed from the local cache right after mount — never during the initial
  // render (see the notifications/unreadCount useState above) — then kick
  // off the real truth-up fetch, which overwrites this the moment it
  // succeeds. A cache hit means the page shows last-known content instantly
  // instead of "Loading…"; a miss (first-ever visit, cleared storage) just
  // falls through to the normal loading state.
  useEffect(() => {
    const email = getActiveUserEmail();
    if (email) {
      const cached = readCachedNotifications(window.localStorage, email, Date.now());
      if (cached) {
        setNotifications(cached.notifications);
        setUnreadCount(cached.unreadCount);
        for (const n of cached.notifications) {
          if (n.id > lastSeenIdRef.current) lastSeenIdRef.current = n.id;
          notifiedIdsRef.current.add(n.id);
        }
      }
    }
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Debounced cache write: an active SSE session can change `notifications`
  // several times in quick succession, and there's no reason to hit
  // localStorage on every single one. unreadCount isn't in the deps list —
  // every path that changes it (refresh/SSE/markRead/markAllRead/clearAll)
  // changes `notifications` in the same action, and the cache derives its
  // own unread count from the list anyway (see lib/notifications-cache.ts),
  // so caching it separately would just be another way for it to drift.
  useEffect(() => {
    if (cacheWriteTimeoutRef.current) clearTimeout(cacheWriteTimeoutRef.current);
    cacheWriteTimeoutRef.current = setTimeout(() => {
      const email = getActiveUserEmail();
      if (!email) return;
      writeCachedNotifications(window.localStorage, email, notifications, Date.now());
    }, CACHE_WRITE_DEBOUNCE_MS);
    return () => {
      if (cacheWriteTimeoutRef.current) clearTimeout(cacheWriteTimeoutRef.current);
    };
  }, [notifications]);

  // Cross-tab reconciliation: the `storage` event fires in every OTHER tab
  // on this origin when one tab writes the cache (never in the tab that
  // wrote it), so this can't loop. Rather than trying to merge two tabs'
  // local caches — there's no reliable way to tell which is newer — just
  // re-ask the server, which both tabs end up agreeing with.
  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === NOTIFICATIONS_CACHE_KEY) refreshRef.current();
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
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

          // Decided OUTSIDE the setNotifications updater, and only once per
          // id (notifiedIdsRef), rather than inside it: a state updater can
          // run more than once for the same update (React StrictMode,
          // concurrent rendering), and a side effect — bumping a counter,
          // popping a desktop notification — triggered from inside one would
          // then fire however many times React happened to invoke it.
          const isFirstSighting = !notifiedIdsRef.current.has(item.id);
          notifiedIdsRef.current.add(item.id);

          setNotifications((prev) => [item, ...prev.filter((n) => n.id !== item.id)]);

          if (isFirstSighting && !item.read_at) {
            setUnreadCount((count) => count + 1);
            showBrowserNotification(item);
          }
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

  // Wraps a mutation's own request: counted as in-flight (see
  // inFlightMutationCountRef/shouldApplyRefreshResult above) for as long as
  // it's pending, so a refresh() racing it can't apply a pre-commit read.
  // Once it settles — and no OTHER mutation is still outstanding — fires
  // exactly one follow-up refresh if one was discarded while this request
  // was in flight (pendingReconcileRef), rather than leaving the list on
  // that skipped snapshot until some unrelated trigger comes along.
  const settleMutation = useCallback(async <T,>(request: () => Promise<T>): Promise<T> => {
    inFlightMutationCountRef.current++;
    try {
      return await request();
    } finally {
      inFlightMutationCountRef.current--;
      if (inFlightMutationCountRef.current === 0 && pendingReconcileRef.current) {
        pendingReconcileRef.current = false;
        refreshRef.current();
      }
    }
  }, []);

  const markRead = useCallback(async (id: number) => {
    // Decided from the ref snapshot BEFORE touching state (see
    // notificationsRef above) rather than inside the setNotifications
    // updater — keeps the updater itself pure and the no-op check exact.
    const current = notificationsRef.current.find((n) => n.id === id);
    if (!current || current.read_at) return;
    localMutationVersionRef.current++;
    setNotifications((prev) =>
      prev.map((n) => (n.id === id ? { ...n, read_at: new Date().toISOString() } : n))
    );
    setUnreadCount((prev) => Math.max(0, prev - 1));
    try {
      await settleMutation(() => api.notifications.markRead(id));
    } catch (err) {
      console.error("Failed to mark notification read:", err);
      refreshRef.current();
    }
  }, [settleMutation]);

  const markUnread = useCallback(async (id: number) => {
    // Mirrors markRead above.
    const current = notificationsRef.current.find((n) => n.id === id);
    if (!current || !current.read_at) return;
    localMutationVersionRef.current++;
    setNotifications((prev) => prev.map((n) => (n.id === id ? { ...n, read_at: null } : n)));
    setUnreadCount((prev) => prev + 1);
    try {
      await settleMutation(() => api.notifications.markUnread(id));
    } catch (err) {
      console.error("Failed to mark notification unread:", err);
      refreshRef.current();
    }
  }, [settleMutation]);

  const markAllRead = useCallback(async () => {
    const now = new Date().toISOString();
    localMutationVersionRef.current++;
    setNotifications((prev) => prev.map((n) => (n.read_at ? n : { ...n, read_at: now })));
    setUnreadCount(0);
    try {
      await settleMutation(() => api.notifications.markAllRead());
    } catch (err) {
      console.error("Failed to mark all notifications read:", err);
      refreshRef.current();
    }
  }, [settleMutation]);

  // Explicit, user-initiated removal — the only thing that makes a
  // notification go away. Read ones are never auto-hidden or expired on
  // their own; they stay in the list (and in this tab's local cache, so a
  // reload keeps showing them) until this runs.
  const clearAll = useCallback(async () => {
    setClearError(false);
    localMutationVersionRef.current++;
    setNotifications([]);
    setUnreadCount(0);
    try {
      await settleMutation(() => api.notifications.clear());
    } catch (err) {
      console.error("Failed to clear notifications:", err);
      // Optimistic clear didn't stick server-side: surface it (clearError)
      // rather than just logging, and pull back whatever's really there —
      // leaving the UI showing "cleared" when the server still has the old
      // list would just reappear confusingly on the next reload anyway.
      setClearError(true);
      refreshRef.current();
    }
  }, [settleMutation]);

  return (
    <NotificationsContext.Provider
      value={{
        notifications,
        unreadCount,
        isConnected,
        isLoading,
        hasLoadError,
        clearError,
        notificationPermission,
        requestNotificationPermission,
        refresh,
        markRead,
        markUnread,
        markAllRead,
        clearAll,
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
