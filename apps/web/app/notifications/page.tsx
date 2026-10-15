"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { Bell, CheckCheck, Trash2, AlertCircle, BellRing, BellOff, Volume2, VolumeX, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScoreBadge } from "@/components/ScoreBadge";
import { useNotificationsStream } from "@/context/notifications-context";
import type { NotificationItem } from "@/lib/api";
import { notificationClickPath } from "@/lib/browser-notifications";
import { NOTIFICATION_FILTERS, matchesNotificationFilter, type NotificationFilter } from "@/lib/notification-filters";
import { groupNotificationsByDate, formatRelativeTime } from "@/lib/notification-date-groups";
import { NOTIFICATION_SOUND_MUTED_KEY, getSafeStorage, isSoundMuted, setSoundMuted, unlockNotificationSound } from "@/lib/notification-sound";
import {
  DESKTOP_NOTIFICATION_PREFS_KEY,
  DESKTOP_NOTIFICATION_TYPES,
  getDisabledDesktopTypes,
  setDesktopTypeEnabled,
} from "@/lib/desktop-notification-preferences";
import { cn } from "@/lib/utils";

function emptyFilterMessage(filter: NotificationFilter): string {
  switch (filter) {
    case "unread":
      return "No unread notifications.";
    case "jobs":
      return "No PAIR job notifications.";
    case "candidates":
      return "No candidate notifications.";
    default:
      // Unreachable today — the tabs only render when there's at least one
      // notification, so "all" can never be the empty-filtered branch. Kept
      // as a real fallback rather than nothing, so this stays correct if
      // that invariant ever changes.
      return "No notifications match this filter.";
  }
}

function NotificationRow({
  item,
  onRead,
  onUnread,
}: {
  item: NotificationItem;
  onRead: (id: number) => void;
  onUnread: (id: number) => void;
}) {
  const router = useRouter();
  const isUnread = !item.read_at;
  // Candidate-level notifications (candidate_passed) go to that candidate's
  // report; job-level ones (pair_launched, pair_inactive — candidate_id is
  // always null for those) go to the job's rankings page instead. Every
  // notification in this app has a jobdiva_id, so this always resolves to a
  // real destination in practice.
  const clickHref = notificationClickPath(item);

  const handleRowClick = () => {
    if (isUnread) onRead(item.id);
    router.push(clickHref);
  };

  const handleRowKeyDown = (e: React.KeyboardEvent) => {
    // Enter only — Space is the activation key for role="button", not
    // role="link" (ARIA Authoring Practices), so handling it here would be
    // non-standard for keyboard/AT users navigating this row as a link.
    if (e.key === "Enter") {
      e.preventDefault();
      handleRowClick();
    }
  };

  // A separate, explicit toggle — independent of the row's own click-to-
  // navigate behavior (stopPropagation keeps this from also firing that).
  // Lets a notification be marked read without leaving the page, and marked
  // back unread (e.g. "I'll deal with this later"), which the implicit
  // mark-on-click behavior alone can't do.
  const handleToggleRead = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (isUnread) onRead(item.id);
    else onUnread(item.id);
  };

  return (
    <Card
      onClick={handleRowClick}
      onKeyDown={handleRowKeyDown}
      role="link"
      tabIndex={0}
      className={cn(
        "flex-row items-center gap-4 px-5 py-4 transition-colors cursor-pointer",
        "hover:border-primary/40 focus-visible:outline-2 focus-visible:outline-primary",
        isUnread ? "bg-primary/5 border-primary/20" : "bg-white"
      )}
    >
      <button
        onClick={handleToggleRead}
        className={cn(
          "group shrink-0 -m-2 p-2 rounded-full transition-colors",
          "focus-visible:outline-2 focus-visible:outline-primary focus-visible:outline-offset-2"
        )}
        aria-label={isUnread ? "Mark as read" : "Mark as unread"}
        title={isUnread ? "Mark as read" : "Mark as unread"}
      >
        <span
          className={cn(
            "block h-3.5 w-3.5 rounded-full border-2 transition-colors",
            isUnread ? "bg-primary border-primary" : "bg-transparent border-slate-300 group-hover:border-primary"
          )}
        />
      </button>
      <div className="flex-1 min-w-0">
        {/* Title + JobDiva id on one line. item.body is never rendered here:
            candidate_passed is the only type that ever sets it (always
            "Score: X/100", now shown as the "X/100" badge instead — showing
            both was redundant and cost a 3rd line); pair_launched and
            pair_inactive never set body at all (see
            services/notifications_service.py — their title alone carries
            the full message), so there's nothing of theirs being hidden. */}
        <p
          className={cn(
            "text-sm line-clamp-2",
            isUnread ? "font-semibold text-slate-900" : "font-medium text-slate-600"
          )}
          title={item.jobdiva_id ? `${item.title} · ${item.jobdiva_id}` : item.title}
        >
          {item.title}
          {item.jobdiva_id && <span className="text-slate-400 font-normal"> · {item.jobdiva_id}</span>}
        </p>
        <p className="text-xs text-slate-400 mt-1">{formatRelativeTime(item.created_at)}</p>
      </div>
      {item.score !== null && <ScoreBadge score={item.score} className="shrink-0" />}
    </Card>
  );
}

export default function NotificationsPage() {
  const {
    notifications,
    unreadCount,
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
  } = useNotificationsStream();
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [filter, setFilter] = useState<NotificationFilter>("all");
  const hasNotifications = notifications.length > 0;

  // Starts false (sound on) — the SSR-rendered value — and is only ever
  // corrected from localStorage inside an effect, after mount: reading it
  // in a useState lazy initializer would run during the first client
  // render and disagree with the server-rendered markup (same hydration
  // hazard the cache-seeding effects elsewhere in this app already guard
  // against). getSafeStorage (not window.localStorage directly) since
  // merely evaluating that property can throw in some environments
  // (blocked storage, certain privacy modes, sandboxed iframes).
  const [soundMuted, setSoundMutedState] = useState(false);
  useEffect(() => {
    setSoundMutedState(isSoundMuted(getSafeStorage()));
  }, []);

  // Cross-tab sync: another tab's own toggle fires a `storage` event here
  // (never in the tab that made the change), so this can't loop.
  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === NOTIFICATION_SOUND_MUTED_KEY) {
        setSoundMutedState(isSoundMuted(getSafeStorage()));
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const handleToggleSound = () => {
    const next = !soundMuted;
    setSoundMuted(getSafeStorage(), next);
    setSoundMutedState(next);
    // Unmuting is itself a click — a real user gesture — so use it to
    // unlock the chime's AudioContext too, in case this recruiter's
    // permission was already granted in an earlier session and they never
    // triggered the "Enable" button's own unlock this time.
    if (!next) unlockNotificationSound();
  };

  // Desktop-notification type preferences (in-app is unaffected — every
  // type always shows in the feed regardless of this). Same hydration-safe
  // pattern as soundMuted above: starts empty (nothing disabled, matching
  // what a fresh/never-configured browser already behaves like) and is only
  // corrected from localStorage after mount.
  const [desktopPrefsOpen, setDesktopPrefsOpen] = useState(false);
  const [disabledDesktopTypes, setDisabledDesktopTypes] = useState<Set<string>>(new Set());
  useEffect(() => {
    setDisabledDesktopTypes(getDisabledDesktopTypes(getSafeStorage()));
  }, []);

  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === DESKTOP_NOTIFICATION_PREFS_KEY) {
        setDisabledDesktopTypes(getDisabledDesktopTypes(getSafeStorage()));
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const handleToggleDesktopType = (type: string, enabled: boolean) => {
    setDesktopTypeEnabled(getSafeStorage(), type, enabled);
    setDisabledDesktopTypes((prev) => {
      const next = new Set(prev);
      if (enabled) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const handleConfirmClear = () => {
    setConfirmingClear(false);
    clearAll();
  };

  // Keeps a just-toggled row visible on the Unread tab instead of it
  // vanishing out from under the cursor the instant markRead fires (PR
  // review follow-up) — without this, there's no way to notice the row
  // disappeared, let alone click "mark unread" to undo it. Reset on any
  // filter change, so switching tabs and back re-applies the filter
  // normally rather than pinning items forever. Adjusted during render
  // (React's documented pattern for "reset state when a value changes")
  // rather than in a useEffect, which would render once with the stale
  // set before the effect ran and cleared it.
  const [stickyIds, setStickyIds] = useState<Set<number>>(new Set());
  const [stickyFilter, setStickyFilter] = useState(filter);
  if (filter !== stickyFilter) {
    setStickyFilter(filter);
    setStickyIds(new Set());
  }

  const handleRead = useCallback(
    (id: number) => {
      setStickyIds((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
      markRead(id);
    },
    [markRead]
  );
  const handleUnread = useCallback(
    (id: number) => {
      setStickyIds((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
      markUnread(id);
    },
    [markUnread]
  );

  const filteredNotifications = useMemo(
    () => notifications.filter((item) => matchesNotificationFilter(item, filter) || stickyIds.has(item.id)),
    [notifications, filter, stickyIds]
  );

  // Today / Yesterday / the actual date — without this, a backlog nobody
  // clears renders as one endless list of "3d ago", "4d ago", ... with no
  // sense of where one day ends and the next begins. Deliberately not
  // memoized: grouping is O(n) over an already-small list, and memoizing on
  // `filteredNotifications` alone would freeze the Today/Yesterday boundary
  // at whatever it was when that array last changed — including across
  // midnight on a tab left open overnight with no new notifications.
  const dateGroups = groupNotificationsByDate(filteredNotifications);

  return (
    <div className="max-w-3xl mx-auto h-[calc(100vh-180px)] flex flex-col">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          <Bell className="h-5 w-5 text-slate-500" />
          <h1 className="text-xl font-semibold text-slate-900">Notifications</h1>
          {unreadCount > 0 && (
            <span className="inline-flex items-center justify-center min-w-[22px] h-[22px] px-1.5 rounded-full text-[11px] font-semibold bg-primary text-white">
              {unreadCount}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {notificationPermission === "granted" && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0 text-slate-400 hover:text-slate-700"
              onClick={handleToggleSound}
              aria-label="Notification sound"
              aria-pressed={!soundMuted}
              title={soundMuted ? "Notification sound is off — click to turn on" : "Notification sound is on — click to mute"}
            >
              {soundMuted ? <VolumeX className="h-4 w-4" /> : <Volume2 className="h-4 w-4" />}
            </Button>
          )}
          {notificationPermission === "granted" && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 w-7 p-0 text-slate-400 hover:text-slate-700"
              onClick={() => setDesktopPrefsOpen(true)}
              aria-label="Desktop notification preferences"
              title="Choose which notifications show a desktop alert"
            >
              <Settings2 className="h-4 w-4" />
            </Button>
          )}
          {unreadCount > 0 && (
            <Button variant="outline" size="sm" onClick={markAllRead}>
              <CheckCheck className="h-4 w-4 mr-1.5" />
              Mark all read
            </Button>
          )}
          {hasNotifications && (
            <Button variant="outline" size="sm" onClick={() => setConfirmingClear(true)} aria-label="Clear notifications">
              <Trash2 className="h-4 w-4 mr-1.5" />
              Clear notifications
            </Button>
          )}
        </div>
      </div>

      <Dialog open={confirmingClear} onOpenChange={setConfirmingClear}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Clear notifications</DialogTitle>
            <DialogDescription>
              This removes every notification in your feed, read and unread. It can&apos;t be undone from here.
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="outline" onClick={() => setConfirmingClear(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleConfirmClear}
              className="bg-red-600 hover:bg-red-700 text-white"
            >
              Clear notifications
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={desktopPrefsOpen} onOpenChange={setDesktopPrefsOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Desktop notification preferences</DialogTitle>
            <DialogDescription>
              Every notification always shows here in your feed. Choose which ones should also pop up a desktop
              alert.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 max-h-[60vh] overflow-y-auto pr-1 -mr-1">
            {DESKTOP_NOTIFICATION_TYPES.map((t) => {
              const enabled = !disabledDesktopTypes.has(t.type);
              return (
                <label
                  key={t.type}
                  className="flex items-start gap-3 rounded-lg border border-slate-200 px-3 py-2.5 cursor-pointer hover:bg-slate-50"
                >
                  <Checkbox
                    checked={enabled}
                    onCheckedChange={(checked) => handleToggleDesktopType(t.type, checked === true)}
                    className="mt-0.5"
                    aria-label={`Desktop alert for ${t.label}`}
                  />
                  <span>
                    <span className="block text-sm font-medium text-slate-800">{t.label}</span>
                    <span className="block text-xs text-slate-400 mt-0.5">{t.description}</span>
                  </span>
                </label>
              );
            })}
          </div>
          <div className="flex justify-end pt-2">
            <Button variant="outline" size="sm" onClick={() => setDesktopPrefsOpen(false)}>
              Done
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      {notificationPermission === "default" && (
        <div className="flex items-center justify-between gap-2 text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg px-4 py-2.5 mb-4">
          <span className="flex items-center gap-2">
            <BellRing className="h-4 w-4 shrink-0 text-slate-500" />
            Get desktop alerts the moment something happens, even when this tab isn&apos;t in front. Choose
            exactly which notifications alert you once enabled.
          </span>
          <Button
            variant="outline"
            size="sm"
            className="h-7"
            onClick={requestNotificationPermission}
            aria-label="Enable desktop notifications"
          >
            Enable
          </Button>
        </div>
      )}

      {notificationPermission === "denied" && (
        <div className="flex items-center gap-2 text-xs text-slate-400 mb-4" role="status">
          <BellOff className="h-3.5 w-3.5 shrink-0" />
          Desktop alerts are blocked. Enable them in your browser&apos;s site settings to get them again.
        </div>
      )}

      {hasLoadError && (
        <div className="flex items-center justify-between gap-2 text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-4 py-2.5 mb-4">
          <span className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 shrink-0" />
            Couldn&apos;t refresh notifications — showing the last loaded data.
          </span>
          <Button variant="ghost" size="sm" className="h-7 text-amber-700 hover:text-amber-900" onClick={refresh}>
            Retry
          </Button>
        </div>
      )}

      {clearError && (
        <div className="flex items-center justify-between gap-2 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-2.5 mb-4" role="status">
          <span className="flex items-center gap-2">
            <AlertCircle className="h-4 w-4 shrink-0" />
            Couldn&apos;t clear notifications — nothing was removed.
          </span>
          <Button variant="ghost" size="sm" className="h-7 text-red-700 hover:text-red-900" onClick={clearAll}>
            Retry
          </Button>
        </div>
      )}

      {hasNotifications && (
        <Tabs value={filter} onValueChange={(v) => setFilter(v as NotificationFilter)} className="mb-4 shrink-0">
          <TabsList>
            {NOTIFICATION_FILTERS.map((f) => (
              <TabsTrigger key={f.value} value={f.value}>
                {f.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      )}

      {hasNotifications ? (
        // Checked before isLoading on purpose: a reload seeds this list from
        // the local cache instantly (see notifications-context.tsx), and
        // that cached content should render right away rather than being
        // hidden behind a loading message while refresh() confirms it in
        // the background.
        filteredNotifications.length > 0 ? (
          // flex-1 (not a fixed height) so this fills whatever the header/
          // banners/tabs above it leave over inside the outer flex column —
          // no magic-number height to keep in sync as those grow or shrink.
          <ScrollArea className="flex-1 min-h-0">
            <div className="space-y-4 pr-2">
              {dateGroups.map((group) => (
                <div key={`${group.label}-${group.items[0].id}`}>
                  <p
                    role="heading"
                    aria-level={3}
                    className="text-xs font-semibold text-slate-400 uppercase tracking-wide px-1 mb-2"
                  >
                    {group.label}
                  </p>
                  <div className="space-y-3">
                    {group.items.map((item) => (
                      <NotificationRow key={item.id} item={item} onRead={handleRead} onUnread={handleUnread} />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </ScrollArea>
        ) : (
          <div className="text-center text-sm text-slate-400 py-16">{emptyFilterMessage(filter)}</div>
        )
      ) : isLoading ? (
        <div className="text-center text-sm text-slate-400 py-16">Loading notifications…</div>
      ) : (
        <div className="text-center text-sm text-slate-400 py-16">
          No notifications yet. You&apos;ll see candidates land here as soon as they pass an interview.
        </div>
      )}
    </div>
  );
}
