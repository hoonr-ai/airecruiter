"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Bell, CheckCheck, Trash2, AlertCircle, BellRing, BellOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { ScoreBadge } from "@/components/ScoreBadge";
import { useNotificationsStream } from "@/context/notifications-context";
import type { NotificationItem } from "@/lib/api";
import { notificationClickPath } from "@/lib/browser-notifications";
import { cn } from "@/lib/utils";

function formatRelativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const diffSec = Math.max(0, Math.floor(diffMs / 1000));
  if (diffSec < 60) return "just now";
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return new Date(iso).toLocaleDateString();
}

function NotificationRow({ item, onRead }: { item: NotificationItem; onRead: (id: number) => void }) {
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
      <div className="flex-1 min-w-0">
        <p
          className={cn(
            "text-sm line-clamp-2",
            isUnread ? "font-semibold text-slate-900" : "font-medium text-slate-600"
          )}
          title={item.title}
        >
          {item.title}
        </p>
        <div className="flex items-center gap-2 mt-0.5 flex-wrap">
          {item.body && <p className="text-xs text-slate-500">{item.body}</p>}
          {item.jobdiva_id && (
            <span className="text-xs text-slate-400 font-mono bg-slate-100 rounded px-1.5 py-0.5">
              {item.jobdiva_id}
            </span>
          )}
        </div>
        <p className="text-xs text-slate-400 mt-1">{formatRelativeTime(item.created_at)}</p>
      </div>
      {item.score !== null && <ScoreBadge score={item.score} />}
      {isUnread && <span className="h-2 w-2 rounded-full bg-primary shrink-0" aria-label="unread" />}
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
    markAllRead,
    clearAll,
  } = useNotificationsStream();
  const [confirmingClear, setConfirmingClear] = useState(false);

  const handleConfirmClear = () => {
    setConfirmingClear(false);
    clearAll();
  };

  return (
    <div className="max-w-3xl mx-auto">
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
          {unreadCount > 0 && (
            <Button variant="outline" size="sm" onClick={markAllRead}>
              <CheckCheck className="h-4 w-4 mr-1.5" />
              Mark all read
            </Button>
          )}
          {notifications.length > 0 && (
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

      {notificationPermission === "default" && (
        <div className="flex items-center justify-between gap-2 text-sm text-slate-700 bg-slate-50 border border-slate-200 rounded-lg px-4 py-2.5 mb-4">
          <span className="flex items-center gap-2">
            <BellRing className="h-4 w-4 shrink-0 text-slate-500" />
            Get a desktop alert the moment a candidate passes, even when this tab isn&apos;t in front.
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

      {notifications.length > 0 ? (
        // Checked before isLoading on purpose: a reload seeds this list from
        // the local cache instantly (see notifications-context.tsx), and
        // that cached content should render right away rather than being
        // hidden behind a loading message while refresh() confirms it in
        // the background.
        <ScrollArea className="h-[calc(100vh-180px)]">
          <div className="space-y-3 pr-2">
            {notifications.map((item) => (
              <NotificationRow key={item.id} item={item} onRead={markRead} />
            ))}
          </div>
        </ScrollArea>
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
