"use client";

// Right-hand column of the handoff call: the live transcript (show/hide is
// client-side only, the stream keeps flowing) and AI follow-up suggestions
// (on/off is sent to Pair-bot, which stops generating when off).

import { useEffect, useRef } from "react";
import { Eye, EyeOff, Lightbulb, MessageSquareText, Power, PowerOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { speakerLabel, type CopilotLine, type CopilotSuggestionBatch } from "@/lib/handoff";

function PanelHeader({ icon, title, action }: { icon: React.ReactNode; title: string; action: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-slate-100">
      <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
        {icon}
        {title}
      </h2>
      {action}
    </div>
  );
}

export function TranscriptPanel({
  lines,
  visible,
  onToggle,
  className,
}: {
  lines: CopilotLine[];
  visible: boolean;
  onToggle: () => void;
  className?: string;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (visible) endRef.current?.scrollIntoView({ block: "end" });
  }, [lines, visible]);

  return (
    <Card className={cn("gap-0 py-0 overflow-hidden flex flex-col min-h-0", className)}>
      <PanelHeader
        icon={<MessageSquareText className="w-4 h-4 text-slate-500" aria-hidden="true" />}
        title="Live transcript"
        action={
          <Button variant="ghost" size="xs" onClick={onToggle} aria-pressed={visible}>
            {visible ? <Eye /> : <EyeOff />} {visible ? "Shown" : "Hidden"}
          </Button>
        }
      />
      {visible ? (
        <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-2 text-sm" aria-live="polite">
          {lines.length === 0 && <p className="text-slate-400">The conversation will appear here.</p>}
          {lines.map((l) => (
            <p key={l.seq} className="leading-snug">
              <span className={cn("font-semibold", l.role === "recruiter" ? "text-indigo-700" : "text-slate-900")}>
                {speakerLabel(l.role)}:
              </span>{" "}
              <span className="text-slate-700">{l.text}</span>
            </p>
          ))}
          <div ref={endRef} />
        </div>
      ) : (
        <p className="px-4 py-3 text-sm text-slate-400">Transcript hidden. It keeps recording.</p>
      )}
    </Card>
  );
}

export function SuggestionsPanel({
  batches,
  enabled,
  onToggle,
  className,
}: {
  batches: CopilotSuggestionBatch[];
  enabled: boolean;
  onToggle: () => void;
  className?: string;
}) {
  // Newest first; earlier batches stay visible but dimmed.
  const ordered = [...batches].reverse();
  return (
    <Card className={cn("gap-0 py-0 overflow-hidden flex flex-col min-h-0", className)}>
      <PanelHeader
        icon={<Lightbulb className="w-4 h-4 text-amber-500" aria-hidden="true" />}
        title="AI suggestions"
        action={
          <Button variant="ghost" size="xs" onClick={onToggle} aria-pressed={enabled}>
            {enabled ? <Power /> : <PowerOff />} {enabled ? "On" : "Off"}
          </Button>
        }
      />
      <div className="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-3 text-sm">
        {!enabled && <p className="text-slate-400">Suggestions are off.</p>}
        {enabled && ordered.length === 0 && (
          <p className="text-slate-400">Follow-up questions appear after the candidate answers.</p>
        )}
        {enabled &&
          ordered.map((b, bi) => (
            <ul key={b.id || bi} className={cn("space-y-2", bi > 0 && "opacity-60")}>
              {b.items.map((s, i) => (
                <li key={i} className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2">
                  <p className="font-medium text-slate-900">{s.question}</p>
                  {s.why && <p className="mt-0.5 text-xs text-slate-500">Why: {s.why}</p>}
                </li>
              ))}
            </ul>
          ))}
      </div>
    </Card>
  );
}
