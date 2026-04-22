"use client";

import { X, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

interface PasteResumeModalProps {
  open: boolean;
  onClose: () => void;
  name: string;
  onNameChange: (v: string) => void;
  email: string;
  onEmailChange: (v: string) => void;
  resumeText: string;
  onResumeTextChange: (v: string) => void;
  isSaving: boolean;
  onSubmit: () => void;
}

export function PasteResumeModal({
  open,
  onClose,
  name,
  onNameChange,
  email,
  onEmailChange,
  resumeText,
  onResumeTextChange,
  isSaving,
  onSubmit,
}: PasteResumeModalProps) {
  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onClick={() => !isSaving && onClose()}>
      <div className="bg-white rounded-xl shadow-2xl max-w-2xl w-full max-h-[90vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        <div className="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
          <div>
            <h3 className="text-[16px] font-semibold text-slate-900">Add Candidate by Resume</h3>
            <p className="text-[12px] text-slate-500 mt-0.5">Paste the resume text. Hoonr-Curate will enrich and score against the job rubric.</p>
          </div>
          <button
            onClick={onClose}
            disabled={isSaving}
            className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-500 disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="p-6 space-y-4 overflow-y-auto flex-1">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-[12px] font-medium text-slate-700 mb-1.5">Candidate Name *</label>
              <Input
                value={name}
                onChange={(e) => onNameChange(e.target.value)}
                placeholder="e.g. Jane Doe"
                className="h-[34px] text-[13px]"
              />
            </div>
            <div>
              <label className="block text-[12px] font-medium text-slate-700 mb-1.5">Email</label>
              <Input
                value={email}
                onChange={(e) => onEmailChange(e.target.value)}
                placeholder="optional"
                className="h-[34px] text-[13px]"
              />
            </div>
          </div>
          <div>
            <label className="block text-[12px] font-medium text-slate-700 mb-1.5">Resume Text *</label>
            <Textarea
              value={resumeText}
              onChange={(e) => onResumeTextChange(e.target.value)}
              placeholder="Paste the full resume text here…"
              rows={14}
              className="text-[12.5px] leading-relaxed font-mono"
            />
          </div>
        </div>
        <div className="px-6 py-4 border-t border-slate-200 flex items-center justify-end gap-2">
          <Button variant="outline" onClick={onClose} disabled={isSaving}>
            Cancel
          </Button>
          <Button
            onClick={onSubmit}
            disabled={isSaving || !name.trim() || !resumeText.trim()}
            className="bg-primary hover:bg-[#5b21b6] text-white flex items-center gap-2"
          >
            {isSaving ? (
              <>
                <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                Processing…
              </>
            ) : (
              <>
                <Sparkles className="w-3.5 h-3.5" />
                Extract & Score
              </>
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
