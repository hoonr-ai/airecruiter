"use client";

// Screening-questions editor for the campaign template wizard.
// Matches the job wizard (Step 4) UI exactly:
//   – Drag-to-reorder via useDragReorder
//   – Purple numbered circles
//   – Two-column layout: Question | Pass Criteria
//   – Hard-filter badge
//   – role-specific / default category labels
//   – Add Question + Regenerate (difficulty selector) toolbar

import { useRef, useState } from "react";
import { GripVertical, Plus, RotateCw, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TemplateQuestion } from "@/lib/campaigns";

// ── Inline drag-reorder hook (same implementation as job wizard) ──────────────
function useDragReorder(onMove: (from: number, to: number) => void) {
  const dragIdxRef = useRef<number | null>(null);
  const onDragStart = (idx: number) => (e: React.DragEvent) => {
    dragIdxRef.current = idx;
    e.dataTransfer.effectAllowed = "move";
  };
  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  };
  const onDrop = (idx: number) => (e: React.DragEvent) => {
    e.preventDefault();
    const from = dragIdxRef.current;
    dragIdxRef.current = null;
    if (from === null || from === idx) return;
    onMove(from, idx);
  };
  const onDragEnd = () => {
    dragIdxRef.current = null;
  };
  return { onDragStart, onDragOver, onDrop, onDragEnd };
}

// ─────────────────────────────────────────────────────────────────────────────

  interface ScreeningQuestionsEditorProps {
    questions: TemplateQuestion[];
    onChange: (q: TemplateQuestion[]) => void;
  }

  export function ScreeningQuestionsEditor({
    questions,
    onChange,
  }: ScreeningQuestionsEditorProps) {

  // ── Drag reorder ────────────────────────────────────────────────────────────
  const move = (from: number, to: number) => {
    const next = [...questions];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    onChange(next.map((q, i) => ({ ...q, order_index: i })));
  };
  const drag = useDragReorder(move);

  // ── CRUD helpers ────────────────────────────────────────────────────────────
  const update = (idx: number, patch: Partial<TemplateQuestion>) =>
    onChange(questions.map((q, i) => (i === idx ? { ...q, ...patch } : q)));

  const remove = (idx: number) =>
    onChange(
      questions
        .filter((_, i) => i !== idx)
        .map((q, i) => ({ ...q, order_index: i }))
    );

  const add = () =>
    onChange([
      ...questions,
      {
        question_text: "",
        pass_criteria: "",
        category: "custom",
        order_index: questions.length,
        is_default: false,
      },
    ]);

  return (
    <div className="space-y-3">
      {/* Column headers — mirrors job wizard */}
      {questions.length > 0 && (
        <div className="flex items-center gap-3 text-[11px] font-bold uppercase tracking-wider text-slate-500 pb-2 border-b-2 border-slate-200 mb-2">
          <div className="w-5 flex-shrink-0" />
          <div className="w-8 flex-shrink-0">#</div>
          <div className="flex-1">Question</div>
          <div className="flex-1">
            Pass Criteria{" "}
            <span className="text-[10px] font-normal lowercase">
              (blank = informational only)
            </span>
          </div>
          <div className="w-10 flex-shrink-0" />
        </div>
      )}

      {questions.length === 0 && (
        <p className="text-sm text-slate-400">No questions yet.</p>
      )}

      {/* Question rows */}
      {questions.map((q, index) => (
        <div
          key={index}
          className="flex items-start gap-3 py-3 border-b border-slate-100 last:border-b-0 group"
          onDragOver={drag.onDragOver}
          onDrop={drag.onDrop(index)}
          onDragEnd={drag.onDragEnd}
        >
          {/* Drag handle */}
          <button
            type="button"
            draggable
            onDragStart={drag.onDragStart(index)}
            onDragEnd={drag.onDragEnd}
            className="w-5 flex-shrink-0 flex items-center justify-center text-slate-300 hover:text-slate-600 cursor-grab active:cursor-grabbing mt-1.5"
            title="Drag to reorder"
            aria-label="Drag to reorder question"
          >
            <GripVertical className="w-4 h-4" />
          </button>

          {/* Number circle */}
          <div className="w-8 h-8 rounded-full bg-[#6366f1] text-white flex items-center justify-center text-[12px] font-bold flex-shrink-0 mt-0.5">
            {index + 1}
          </div>

          {/* Question text */}
          <div className="flex-1 min-w-0">
            <textarea
              value={q.question_text ?? ""}
              onChange={(e) => update(index, { question_text: e.target.value })}
              rows={3}
              className="w-full text-[13px] bg-transparent border-none outline-none text-slate-900 font-medium resize-none whitespace-pre-wrap break-words"
            />
          </div>

          {/* Pass criteria */}
          <div className="flex-1 min-w-0 border-l border-slate-100 pl-3">
            <textarea
              value={q.pass_criteria ?? ""}
              onChange={(e) => update(index, { pass_criteria: e.target.value })}
              rows={2}
              placeholder="No hard filter"
              className={`w-full text-[13px] bg-transparent border-none outline-none font-medium resize-none whitespace-pre-wrap break-words ${
                q.pass_criteria
                  ? "text-[#4f46e5]"
                  : "text-slate-300 italic"
              }`}
            />
          </div>

          {/* Category + delete */}
          <div className="w-10 flex-shrink-0 flex flex-col items-end gap-2 pr-1">
            {q.category === "role-specific" && (
              <span className="bg-[#f0fdf4] text-[#166534] text-[9px] font-bold px-1.5 py-0.5 rounded border border-[#bbf7d0] whitespace-nowrap mb-1">
                role-specific
              </span>
            )}
            <button
              type="button"
              onClick={() => remove(index)}
              className="text-slate-300 hover:text-red-500 hover:bg-red-50 w-6 h-6 flex items-center justify-center rounded transition-all opacity-0 group-hover:opacity-100"
              title="Remove"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      ))}

      {/* Toolbar — Add Question */}
      <div className="flex gap-2 mt-3 items-start flex-wrap">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={add}
          className="border-slate-200 text-slate-600 bg-white hover:bg-slate-50 font-medium text-[13px] rounded-lg shadow-none h-[34px] px-3 border transition-all"
        >
          <Plus className="w-3.5 h-3.5 mr-1.5" />
          Add Question
        </Button>
      </div>
    </div>
  );
}
