"use client";

// "Recruiter handoff" section of the interview view: what happened after
// PAIR handed the call to a recruiter - outcome, who joined, close notes, the
// recording, and the handoff transcript with the AI suggestions shown live.
// Renders nothing when the interview had no handoff (or it can't be read).

import { useEffect, useState } from "react";
import { Headset } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { api, type HandoffReview as HandoffReviewData } from "@/lib/api";
import { cleanSuggestionBatches, handoffOutcomeLabel, reviewSpeakerLabel, safeRecordingUrl } from "@/lib/handoff";
import { SuggestionsPanel, TranscriptPanel } from "@/components/handoff/CopilotPanels";

export function HandoffReview({ interviewId, open = true }: { interviewId: string | null; open?: boolean }) {
  // Keyed by interview so switching candidates never shows the last one's.
  const [loaded, setLoaded] = useState<{ key: string; data: HandoffReviewData | null } | null>(null);
  const data = open && loaded && loaded.key === interviewId ? loaded.data : null;

  useEffect(() => {
    const id = Number(interviewId);
    if (!open || !interviewId || !Number.isInteger(id) || id <= 0) return;
    let live = true;
    // Any failure (no access, PAIR-bot down, route not deployed) hides the
    // section: it's supplementary to the AI interview shown around it.
    api.handoff
      .review(id)
      .then((r) => live && setLoaded({ key: interviewId, data: r }))
      .catch(() => live && setLoaded({ key: interviewId, data: null }));
    return () => {
      live = false;
    };
  }, [interviewId, open]);

  const outcome = handoffOutcomeLabel(data?.state, data?.close_reason);
  if (!data || !outcome) return null;

  const lines = data.transcript?.lines ?? [];
  const suggestions = cleanSuggestionBatches(data.transcript?.suggestions);
  const joinedBy = data.claimed_by_email || null;
  const recordingUrl = safeRecordingUrl(data.recording?.url);
  const failed = [data.transcript_error && "transcript", data.recording_error && "recording"].filter(Boolean);

  return (
    <section aria-labelledby="handoff-review-title" className="mb-5 rounded-xl border border-slate-200 bg-white">
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3">
        <Headset className="h-4 w-4 text-indigo-600" aria-hidden="true" />
        <h3 id="handoff-review-title" className="text-sm font-semibold text-slate-900">
          Recruiter handoff
        </h3>
        <Badge variant="outline" className="text-[11px]">
          {outcome}
        </Badge>
        {joinedBy && <span className="text-xs text-slate-500">Joined by {joinedBy}</span>}
      </div>

      <div className="space-y-3 p-4">
        {data.close_notes && (
          <div className="rounded-md bg-slate-50 px-3 py-2 text-sm">
            <p className="text-[11px] font-bold uppercase tracking-wide text-slate-400">Recruiter notes</p>
            <p className="mt-1 whitespace-pre-wrap text-slate-700">{data.close_notes}</p>
          </div>
        )}

        {recordingUrl && (
          <div>
            <p className="mb-1 text-[11px] font-bold uppercase tracking-wide text-slate-400">Handoff recording</p>
            {/* Presigned and short-lived: reopening the view fetches a fresh URL. */}
            <audio controls preload="metadata" src={recordingUrl} className="w-full">
              <track kind="captions" />
            </audio>
          </div>
        )}

        {lines.length > 0 || suggestions.length > 0 ? (
          <div className="grid gap-3 md:grid-cols-2">
            <TranscriptPanel
              title="Handoff transcript"
              lines={lines}
              visible
              autoScroll={false}
              emptyText="No transcript was captured."
              className="max-h-80"
              renderSpeaker={reviewSpeakerLabel}
            />
            <SuggestionsPanel
              batches={suggestions}
              enabled
              emptyText="No AI suggestions were made."
              className="max-h-80"
            />
          </div>
        ) : (
          !recordingUrl &&
          failed.length === 0 && <p className="text-sm text-slate-400">No recording or transcript is available for this handoff.</p>
        )}
        {failed.length > 0 && (
          <p className="text-xs text-amber-700">Couldn&apos;t load the handoff {failed.join(" or ")}. Try again later.</p>
        )}
      </div>
    </section>
  );
}
