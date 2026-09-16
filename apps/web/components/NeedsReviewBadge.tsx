"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertCircle } from "lucide-react";

export type NeedsReviewQuestion = {
  question: string;
  answer?: string;
  reason?: string;
};

export function NeedsReviewBadge({ questions }: { questions: NeedsReviewQuestion[] }) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<React.CSSProperties>({ left: 16, top: 16 });
  const [maxHeight, setMaxHeight] = useState<number>(400);

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

  useEffect(() => {
    if (!open) return;

    const updatePosition = () => {
      const rect = triggerRef.current?.getBoundingClientRect();
      if (!rect) return;

      const panelWidth = Math.min(420, window.innerWidth - 32);
      const left = Math.max(16, Math.min(rect.left + rect.width / 2 - panelWidth / 2, window.innerWidth - panelWidth - 16));
      setPosition({ left, top: rect.bottom + 12, bottom: undefined });
      
      const spaceBelow = window.innerHeight - rect.bottom - 12 - 16; // 12px gap, 16px window bottom margin
      setMaxHeight(Math.max(150, spaceBelow));
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open]);

  if (questions.length === 0) return null;

  const panel = open ? (
    <div
      id={panelId}
      role="tooltip"
      tabIndex={-1}
      className="fixed z-[100] w-[min(420px,calc(100vw-2rem))] flex flex-col rounded-2xl border border-amber-200 bg-white/95 p-4 text-left shadow-2xl backdrop-blur-md"
      style={{ ...position, maxHeight }}
      onMouseEnter={cancelClose}
      onMouseLeave={scheduleClose}
      onFocus={cancelClose}
      onBlur={scheduleClose}
    >
      <div className="mb-3 flex-none flex items-center justify-between border-b border-amber-100 pb-2.5">
        <span className="flex items-center gap-2 text-[12px] font-bold uppercase tracking-widest text-amber-800">
          <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
          Needs Recruiter Review
        </span>
        <span className="text-[10px] font-medium text-amber-500">
          {questions.length} Question{questions.length !== 1 ? "s" : ""}
        </span>
      </div>
      <p className="mb-3 flex-none text-[11px] leading-relaxed text-amber-700">
        These questions were <strong>passed</strong> but the candidate gave an ambiguous or uncertain answer. Please review before proceeding.
      </p>
      <div className="flex-1 min-h-0 space-y-2.5 overflow-y-auto pr-2 overscroll-contain scrollbar-thin scrollbar-thumb-amber-200">
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
  ) : null;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className="inline-flex cursor-help items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[10px] font-bold text-amber-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 focus-visible:ring-offset-2"
        aria-expanded={open}
        aria-describedby={open ? panelId : undefined}
        onMouseEnter={() => {
          cancelClose();
          setOpen(true);
        }}
        onMouseLeave={scheduleClose}
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
      {typeof document !== "undefined" && panel ? createPortal(panel, document.body) : null}
    </>
  );
}
