"use client";

// Editable screening-questions list for the campaign template. Each question
// has text + optional pass-criteria (blank = informational only). Saved as the
// campaign template_screen_questions; child jobs inherit it.

import { X, Plus } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { TemplateQuestion } from "@/lib/campaigns";

export function ScreeningQuestionsEditor({
  questions,
  onChange,
}: {
  questions: TemplateQuestion[];
  onChange: (q: TemplateQuestion[]) => void;
}) {
  const update = (idx: number, patch: Partial<TemplateQuestion>) =>
    onChange(questions.map((q, i) => (i === idx ? { ...q, ...patch } : q)));

  const remove = (idx: number) =>
    onChange(questions.filter((_, i) => i !== idx).map((q, i) => ({ ...q, order_index: i })));

  const add = () =>
    onChange([
      ...questions,
      { question_text: "", pass_criteria: "", category: "custom", order_index: questions.length },
    ]);

  return (
    <div className="space-y-3">
      {questions.length === 0 ? (
        <p className="text-sm text-slate-400">No questions yet.</p>
      ) : (
        questions.map((q, idx) => (
          <div key={idx} className="border border-slate-200 rounded-lg p-3 space-y-2 bg-white">
            <div className="flex items-start gap-2">
              <span className="text-xs font-medium text-slate-400 mt-2 w-5 shrink-0">{idx + 1}.</span>
              <Textarea
                value={q.question_text ?? ""}
                onChange={(e) => update(idx, { question_text: e.target.value })}
                placeholder="Question the screening bot asks"
                rows={2}
                className="flex-1"
              />
              <button
                type="button"
                onClick={() => remove(idx)}
                className="text-slate-400 hover:text-slate-600 mt-2"
                aria-label="Remove question"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
            <div className="pl-7">
              <Input
                value={q.pass_criteria ?? ""}
                onChange={(e) => update(idx, { pass_criteria: e.target.value })}
                placeholder="Pass criteria (blank = informational only)"
              />
            </div>
          </div>
        ))
      )}
      <Button type="button" variant="outline" size="sm" onClick={add}>
        <Plus className="h-4 w-4 mr-1.5" /> Add Question
      </Button>
    </div>
  );
}
