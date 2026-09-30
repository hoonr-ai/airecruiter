"use client";

import { useEffect, useState } from "react";
import { Mic } from "lucide-react";
import { API_BASE, authFetch } from "@/lib/api";

interface CallRecording {
  key: string;
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

  useEffect(() => {
    setRecordings([]);
    setUnavailable(false);
    if (!open || !interviewId) return;

    let cancelled = false;
    (async () => {
      try {
        const response = await authFetch(
          `${API_BASE}/api/v1/engagement/interviews/${interviewId}/recordings`
        );
        if (cancelled) return;
        if (!response.ok) {
          console.warn("Call recordings request failed:", response.status);
          setUnavailable(true);
          return;
        }
        const result = await response.json();
        if (cancelled) return;
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
  }, [open, interviewId]);

  if (!unavailable && recordings.length === 0) return null;

  return (
    <div className="space-y-3 mb-6">
      <p className="text-[11px] text-slate-400 font-bold uppercase tracking-wide flex items-center gap-1.5">
        <Mic className="w-3.5 h-3.5" />
        Call Recording{recordings.length > 1 ? "s" : ""}
      </p>
      {unavailable && (
        <p className="text-[11px] text-slate-400 font-semibold">
          Call recordings are temporarily unavailable.
        </p>
      )}
      {recordings.map((rec, idx) => (
        <div key={rec.key} className="rounded-lg border border-slate-200 p-3">
          <p className="text-[11px] text-slate-500 font-semibold mb-1.5">
            {recordings.length > 1 ? `Recording ${idx + 1} · ` : ""}
            {new Date(rec.started_at || rec.recorded_at).toLocaleString()}
          </p>
          <audio
            controls
            preload="none"
            aria-label={`Call recording ${idx + 1}`}
            src={rec.url.startsWith("/") ? `${API_BASE}${rec.url}` : rec.url}
            className="w-full h-9"
          >
            Your browser does not support audio playback.
          </audio>
        </div>
      ))}
    </div>
  );
}
