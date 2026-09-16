"use client";

import { useEffect, useId, useRef, useState } from "react";
import { AlertCircle } from "lucide-react";

export type NeedsReviewQuestion = {
  question: string;
  answer?: string;
  reason?: string;
};

export function NeedsReviewBadge({ questions }: { questions: NeedsReviewQuestion[] }) {
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const panelId = useId();
  const [open, setOpen] = useState(false);

  const cancelClose = () => {
    if (closeTimerRef.current) {
      clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  };

  const scheduleClose = () => {
    cancelClose();
    closeTimerRef.current = setTimeout(() => {
      setOpen(false);
      closeTimerRef.current = null;
    }, 300);
  };

  useEffect(() => () => cancelClose(), []);

  if (questions.length === 0) return null;

  return (
    <div 
      className="relative inline-flex flex-col items-center justify-center"
      onMouseEnter={() => {
        cancelClose();
        setOpen(true);
      }}
      onMouseLeave={scheduleClose}
    >
      <button
        type="button"
        className="inline-flex cursor-help items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] font-bold text-amber-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2"
        aria-expanded={open}
        aria-describedby={open ? panelId : undefined}
        onFocus={() => {
          cancelClose();
          setOpen(true);
        }}
        onBlur={scheduleClose}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setOpen(false);
            event.currentTarget.blur();
          }
        }}
      >
        <AlertCircle className="h-3 w-3" /> Needs Review
      </button>

      <div
        id={panelId}
        role="tooltip"
        tabIndex={-1}
        className={`absolute left-1/2 top-full z-40 mt-3 w-[420px] max-w-[calc(100vw-2rem)] -translate-x-1/2 rounded-2xl border border-amber-200 bg-white/95 p-4 text-left shadow-2xl backdrop-blur-md transition-all duration-300 origin-top ${
          open
            ? "opacity-100 translate-y-0 scale-100 visible pointer-events-auto"
            : "opacity-0 -translate-y-2 scale-95 invisible pointer-events-none"
        }`}
        onMouseEnter={cancelClose}
        onFocus={cancelClose}
        onBlur={scheduleClose}
      >
        <div className="mb-3 flex items-center justify-between border-b border-amber-100 pb-2.5">
          <span className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-widest text-amber-800">
            <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
            Needs Recruiter Review
          </span>
          <span className="text-[10px] font-medium text-amber-500">
            {questions.length} Question{questions.length !== 1 ? "s" : ""}
          </span>
        </div>
        <p className="mb-3 text-[11px] leading-relaxed text-amber-700">
          These questions were <strong>passed</strong> but the candidate gave an ambiguous or uncertain answer. Please review before proceeding.
        </p>
        <div className="max-h-[380px] space-y-2.5 overflow-y-auto pr-2 scrollbar-thin scrollbar-thumb-amber-200">
          {questions.map((item, index) => (
            <div
              key={`${item.question}-${index}`}
              className="rounded-xl border border-amber-200 bg-amber-50/50 p-3"
            >
              <div className="mb-1.5 break-words whitespace-normal text-[13px] font-semibold leading-relaxed text-slate-800">
                {item.question}
              </div>
              {item.answer && (
                <div className="mb-1.5 rounded-lg border border-amber-100 bg-white/70 px-2.5 py-1.5 text-[12px] italic text-slate-600">
                  &ldquo;{item.answer}&rdquo;
                </div>
              )}
              {item.reason && (
                <div className="text-[11px] leading-relaxed text-amber-700">
                  <span className="font-semibold not-italic">AI Analysis: </span>{item.reason}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
