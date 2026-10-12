"use client";

import { Bell, CheckCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ScoreBadge } from "@/components/ScoreBadge";
import { useNotificationsStream } from "@/context/notifications-context";
import type { NotificationItem } from "@/lib/api";
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
  const isUnread = !item.read_at;
  return (
    <Card
      onClick={() => isUnread && onRead(item.id)}
      className={cn(
        "flex-row items-center gap-4 px-5 py-4 cursor-pointer transition-colors",
        isUnread ? "bg-primary/5 border-primary/20" : "bg-white"
      )}
    >
      <div className="flex-1 min-w-0">
        <p className={cn("text-sm", isUnread ? "font-semibold text-slate-900" : "font-medium text-slate-600")}>
          {item.title}
        </p>
        {item.body && <p className="text-xs text-slate-500 mt-0.5">{item.body}</p>}
        <p className="text-xs text-slate-400 mt-1">{formatRelativeTime(item.created_at)}</p>
      </div>
      {item.score !== null && <ScoreBadge score={item.score} />}
      {isUnread && <span className="h-2 w-2 rounded-full bg-primary shrink-0" aria-label="unread" />}
    </Card>
  );
}

export default function NotificationsPage() {
  const { notifications, unreadCount, markRead, markAllRead } = useNotificationsStream();

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
        {unreadCount > 0 && (
          <Button variant="outline" size="sm" onClick={markAllRead}>
            <CheckCheck className="h-4 w-4 mr-1.5" />
            Mark all read
          </Button>
        )}
      </div>

      {notifications.length === 0 ? (
        <div className="text-center text-sm text-slate-400 py-16">
          No notifications yet. You&apos;ll see candidates land here as soon as they pass an interview.
        </div>
      ) : (
        <ScrollArea className="h-[calc(100vh-180px)]">
          <div className="space-y-3 pr-2">
            {notifications.map((item) => (
              <NotificationRow key={item.id} item={item} onRead={markRead} />
            ))}
          </div>
        </ScrollArea>
      )}
    </div>
  );
}
