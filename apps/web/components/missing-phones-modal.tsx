"use client";

import { useEffect, useState } from "react";
import { Phone, Check, Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { API_BASE } from "@/lib/api";
import { logger } from "@/lib/logger";

export interface MissingPhoneCandidate {
  candidate_id: string;
  name: string;
  headline?: string;
  location?: string;
  source?: string;
  jobdiva_id?: string;
}

interface MissingPhonesModalProps {
  open: boolean;
  candidates: MissingPhoneCandidate[];
  onClose: () => void;
  onAllProvided: (phonesByCandidate: Record<string, string>) => void;
  title?: string;
  description?: string;
  primaryLabel?: string;
  persist?: boolean;
}

function countDigits(s: string) {
  let n = 0;
  for (let i = 0; i < s.length; i++) if (s[i] >= "0" && s[i] <= "9") n++;
  return n;
}

export function MissingPhonesModal({
  open,
  candidates,
  onClose,
  onAllProvided,
  title = "Missing phone numbers",
  description = "PAIR can only call candidates with a phone number. Add the missing numbers below and we'll retry.",
  primaryLabel = "Launch PAIR",
  persist = true,
}: MissingPhonesModalProps) {
  const [phones, setPhones] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [savedAt, setSavedAt] = useState<Record<string, number>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (open) {
      setPhones({});
      setSaving({});
      setSavedAt({});
      setErrors({});
    }
  }, [open, candidates]);

  const allValid =
    candidates.length > 0 &&
    candidates.every((c) => countDigits(phones[c.candidate_id] || "") >= 7);

  async function saveOne(cand: MissingPhoneCandidate, phone: string) {
    if (countDigits(phone) < 7) {
      setErrors((prev) => ({ ...prev, [cand.candidate_id]: "At least 7 digits required" }));
      return false;
    }
    setErrors((prev) => {
      const { [cand.candidate_id]: _omit, ...rest } = prev;
      return rest;
    });
    if (!persist) return true;
    setSaving((prev) => ({ ...prev, [cand.candidate_id]: true }));
    try {
      const res = await fetch(`${API_BASE}/candidates/${encodeURIComponent(cand.candidate_id)}/phone`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, jobdiva_id: cand.jobdiva_id }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = body?.detail || `Save failed (${res.status})`;
        setErrors((prev) => ({ ...prev, [cand.candidate_id]: String(msg) }));
        return false;
      }
      setSavedAt((prev) => ({ ...prev, [cand.candidate_id]: Date.now() }));
      return true;
    } catch (e: any) {
      logger.error("missing_phones.save.error", { candidateId: cand.candidate_id, message: e?.message });
      setErrors((prev) => ({ ...prev, [cand.candidate_id]: e?.message || "Save failed" }));
      return false;
    } finally {
      setSaving((prev) => ({ ...prev, [cand.candidate_id]: false }));
    }
  }

  async function handleSubmit() {
    const tasks = candidates.map((c) => saveOne(c, (phones[c.candidate_id] || "").trim()));
    const results = await Promise.all(tasks);
    if (results.every(Boolean)) {
      onAllProvided(phones);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-[560px] max-h-[80vh] flex flex-col p-0 gap-0 overflow-hidden">
        <DialogHeader className="px-6 pt-6 pb-4 border-b border-slate-100 shrink-0">
          <DialogTitle className="text-lg font-bold text-slate-900 flex items-center gap-2">
            <Phone className="w-5 h-5 text-amber-500" />
            {title}
          </DialogTitle>
          <DialogDescription className="text-[13px] text-slate-500 mt-1">
            {description}
          </DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-3">
          {candidates.map((c) => {
            const val = phones[c.candidate_id] || "";
            const isSaving = !!saving[c.candidate_id];
            const wasSaved = !!savedAt[c.candidate_id];
            const err = errors[c.candidate_id];
            return (
              <div
                key={c.candidate_id}
                className="border border-slate-200 rounded-xl px-4 py-3 bg-white flex items-center gap-3"
              >
                <div className="flex-1 min-w-0">
                  <p className="font-semibold text-slate-900 text-[14px] truncate">{c.name || "Unnamed"}</p>
                  <p className="text-[12px] text-slate-500 truncate">
                    {c.headline || "—"}
                    {c.location ? ` • ${c.location}` : ""}
                    {c.source ? ` • ${c.source}` : ""}
                  </p>
                </div>
                <div className="flex flex-col items-end gap-1">
                  <div className="flex items-center gap-2">
                    <input
                      type="tel"
                      inputMode="tel"
                      autoComplete="tel"
                      placeholder="+1 555 123 4567"
                      value={val}
                      onChange={(e) => setPhones((prev) => ({ ...prev, [c.candidate_id]: e.target.value }))}
                      className="h-9 w-48 px-3 rounded-lg border border-slate-300 text-[13px] focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                    />
                    {isSaving ? (
                      <Loader2 className="w-4 h-4 text-slate-400 animate-spin" />
                    ) : wasSaved ? (
                      <Check className="w-4 h-4 text-emerald-500" />
                    ) : (
                      <span className="w-4 h-4" />
                    )}
                  </div>
                  {err && <span className="text-[11px] text-rose-600">{err}</span>}
                </div>
              </div>
            );
          })}
          {candidates.length === 0 && (
            <p className="text-[13px] text-slate-500 text-center py-6">All set — no candidates need phone numbers.</p>
          )}
        </div>

        <DialogFooter className="px-6 py-4 border-t border-slate-100 shrink-0 flex justify-between sm:justify-between gap-2">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!allValid || Object.values(saving).some(Boolean)}
            className="bg-indigo-600 hover:bg-indigo-700 text-white"
          >
            {primaryLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
