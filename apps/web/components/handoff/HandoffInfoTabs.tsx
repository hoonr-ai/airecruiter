"use client";

// AI interview (Q&A + scores) | Resume | Transcript — what the recruiter
// preps from before joining and refers back to during the call.

import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import type { HandoffContext } from "@/lib/api";
import { speakerLabel } from "@/lib/handoff";

/** undefined: still loading; null: not available (e.g. not deployed yet). */
export type Loadable<T> = T | null | undefined;

function Unavailable({ loading, text }: { loading: boolean; text: string }) {
  return loading ? (
    <Loader2 className="w-5 h-5 animate-spin text-slate-400" aria-label="Loading" />
  ) : (
    <p className="text-sm text-slate-400">{text}</p>
  );
}

/** Pair-bot's hard-filter result, whatever shape it arrives in. */
export function hardFilterLabel(hf: unknown): { text: string; ok: boolean } | null {
  if (hf === null || hf === undefined || hf === "") return null;
  if (typeof hf === "boolean") return { text: hf ? "Hard filters passed" : "Hard filters failed", ok: hf };
  if (typeof hf === "string") return { text: `Hard filters: ${hf}`, ok: /pass/i.test(hf) };
  if (typeof hf === "object") {
    const o = hf as Record<string, unknown>;
    if (typeof o.passed === "boolean") return hardFilterLabel(o.passed);
    if (typeof o.status === "string") return hardFilterLabel(o.status);
  }
  return null;
}

export function HandoffInfoTabs({
  context,
  resume,
  className,
}: {
  context: Loadable<HandoffContext>;
  resume: Loadable<string>;
  className?: string;
}) {
  const questions = context?.questions ?? [];
  const aiTranscript = context?.ai_transcript ?? [];
  return (
    <Card className={cn("gap-0 py-3 px-4 min-h-0 flex flex-col", className)}>
      <Tabs defaultValue="interview" className="min-h-0 flex-1">
        <TabsList>
          <TabsTrigger value="interview">AI interview</TabsTrigger>
          <TabsTrigger value="resume">Resume</TabsTrigger>
          <TabsTrigger value="transcript">Transcript</TabsTrigger>
        </TabsList>
        <TabsContent value="interview" className="min-h-0 overflow-y-auto space-y-3 pt-2">
          {questions.length === 0 ? (
            <Unavailable loading={context === undefined} text="The AI interview summary isn't available yet." />
          ) : (
            questions.map((q, i) => (
              <div key={i} className="border-b border-slate-100 pb-3 last:border-0">
                <div className="flex items-start justify-between gap-3">
                  <p className="text-sm font-medium text-slate-900">{q.question}</p>
                  {q.score !== null && q.score !== undefined && (
                    <Badge variant="outline" className="shrink-0">Score {q.score}</Badge>
                  )}
                </div>
                {q.answer && <p className="mt-1 text-sm text-slate-700">{q.answer}</p>}
                {q.notes && <p className="mt-1 text-xs text-slate-500">{q.notes}</p>}
              </div>
            ))
          )}
        </TabsContent>
        <TabsContent value="resume" className="min-h-0 overflow-y-auto pt-2">
          {resume ? (
            <pre className="whitespace-pre-wrap font-sans text-sm text-slate-700">{resume}</pre>
          ) : (
            <Unavailable loading={resume === undefined} text="No resume on file for this candidate." />
          )}
        </TabsContent>
        <TabsContent value="transcript" className="min-h-0 overflow-y-auto space-y-2 pt-2 text-sm">
          {aiTranscript.length === 0 ? (
            <Unavailable loading={context === undefined} text="The AI interview transcript isn't available yet." />
          ) : (
            aiTranscript.map((l, i) => (
              <p key={i}>
                <span className="font-semibold text-slate-900">
                  {l.role === "assistant" || l.role === "agent" ? "PAIR" : speakerLabel(l.role)}:
                </span>{" "}
                <span className="text-slate-700">{l.text}</span>
              </p>
            ))
          )}
        </TabsContent>
      </Tabs>
    </Card>
  );
}
