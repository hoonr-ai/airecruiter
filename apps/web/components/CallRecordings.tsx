"use client";

import { useCallback, useEffect, useState } from "react";
import { Mic } from "lucide-react";
import { API_BASE, authFetch } from "@/lib/api";
import { RecordingPlayer } from "@/components/RecordingPlayer";

interface CallRecording {
  id: string;
  session_id: number;
  url: string;
  started_at?: string | null;
  recorded_at: string;
}

interface CallRecordingsProps {
  interviewId: string | null;
  open: boolean;
}

// Call recordings only exist in production (S3). A failure here must never
// break the modal it sits in, so errors show a hint and nothing else.
export function CallRecordings({ interviewId, open }: CallRecordingsProps) {
  const [recordings, setRecordings] = useState<CallRecording[]>([]);
  const [unavailable, setUnavailable] = useState(false);
  // 403 ("not your job") is a different, permanent condition from a
  // transient S3/upstream outage — kept separate so the message doesn't
  // tell someone to "try again" for an access problem a retry can't fix.
  const [forbidden, setForbidden] = useState(false);
  // Bumped to force a refetch — e.g. a player's Retry after its presigned
  // URL's 15-minute TTL has expired, which a plain remount can't fix.
  const [refetchToken, setRefetchToken] = useState(0);

  // Reset only on a genuinely new interview/open — NOT on refetchToken, so a
  // Retry-triggered refetch keeps showing the stale (still-valid) list
  // instead of making the whole section disappear and reappear.
  useEffect(() => {
    setRecordings([]);
    setUnavailable(false);
    setForbidden(false);
  }, [open, interviewId]);

  useEffect(() => {
    if (!open || !interviewId) return;

    let cancelled = false;
    (async () => {
      try {
        const response = await authFetch(
          `${API_BASE}/api/v1/engagement/interviews/${interviewId}/recordings`
        );
        if (cancelled) return;
        if (!response.ok) {
          if (response.status === 404) {
            // This interview simply has no recordings on file (not an
            // outage) — show nothing rather than an "unavailable" hint.
            return;
          }
          if (response.status === 403) {
            setForbidden(true);
            setRecordings([]);
            return;
          }
          console.warn("Call recordings request failed:", response.status);
          setUnavailable(true);
          return;
        }
        const result = await response.json();
        if (cancelled) return;
        setForbidden(false);
        setRecordings(result.recordings || []);
        setUnavailable(Boolean(result.unavailable));
      } catch (err) {
        console.warn("Call recordings request failed:", err);
        if (!cancelled) setUnavailable(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, interviewId, refetchToken]);

  const refetch = useCallback(() => setRefetchToken((n) => n + 1), []);

  if (!unavailable && !forbidden && recordings.length === 0) return null;

  return (
    <div className="space-y-3 mb-6">
      <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide flex items-center gap-1.5">
        <Mic className="w-3.5 h-3.5" />
        Call Recording{recordings.length > 1 ? "s" : ""}
      </p>
      {forbidden ? (
        <p className="text-[11px] text-slate-400 font-semibold">
          You don&apos;t have access to this interview&apos;s call recordings.
        </p>
      ) : (
        unavailable && (
          <p className="text-[11px] text-slate-400 font-semibold">
            Call recordings are temporarily unavailable.
          </p>
        )
      )}
      {recordings.map((rec, idx) => (
        <div key={rec.id} className="rounded-lg border border-slate-200 p-3">
          <p className="text-[11px] text-slate-500 font-semibold mb-1.5">
            {recordings.length > 1 ? `Recording ${idx + 1} · ` : ""}
            {new Date(rec.started_at || rec.recorded_at).toLocaleString()}
          </p>
          <RecordingPlayer
            src={rec.url.startsWith("/") ? `${API_BASE}${rec.url}` : rec.url}
            label={`call recording ${idx + 1}`}
            onRetry={refetch}
          />
        </div>
      ))}
    </div>
  );
}
