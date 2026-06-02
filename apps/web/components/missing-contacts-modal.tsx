"use client";

import { useEffect, useState } from "react";
import { Phone, Mail, Check, Loader2 } from "lucide-react";
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

export interface MissingContactCandidate {
  candidate_id: string;
  name: string;
  headline?: string;
  location?: string;
  work_location?: string;
  source?: string;
  jobdiva_id?: string;
  needsPhone: boolean;
  needsEmail: boolean;
  currentPhone?: string;
  currentEmail?: string;
}

function locationDisplay(c: { location?: string; work_location?: string }): string {
  const home = (c.location || "").trim();
  if (home) return home;
  const work = (c.work_location || "").trim();
  if (work) return `Works in ${work}`;
  return "";
}

interface MissingContactsModalProps {
  open: boolean;
  candidates: MissingContactCandidate[];
  onClose: () => void;
  onAllProvided: (
    contactsByCandidate: Record<string, { phone?: string; email?: string }>,
  ) => void;
  title?: string;
  description?: string;
  primaryLabel?: string;
}

function countDigits(s: string) {
  let n = 0;
  for (let i = 0; i < s.length; i++) if (s[i] >= "0" && s[i] <= "9") n++;
  return n;
}

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const CONTACT_SAVE_BATCH_SIZE = 4;
const PLACEHOLDER_EMAILS = new Set([
  "your-email@example.com",
  "email@example.com",
  "example@example.com",
  "test@example.com",
  "candidate@example.com",
  "noreply@example.com",
]);

function isValidEmail(s: string) {
  const v = (s || "").trim().toLowerCase();
  if (!v || !EMAIL_RE.test(v)) return false;
  if (PLACEHOLDER_EMAILS.has(v)) return false;
  if (v.endsWith("@example.com")) return false;
  if (v.endsWith("@noemail.pair.ai")) return false;
  return true;
}

export function MissingContactsModal({
  open,
  candidates,
  onClose,
  onAllProvided,
  title = "Missing contact details",
  description = "PAIR needs a unique real phone number and email for each candidate. Add or correct the details below and we'll launch for them too.",
  primaryLabel = "Launch PAIR for remaining",
}: MissingContactsModalProps) {
  const [phones, setPhones] = useState<Record<string, string>>({});
  const [emails, setEmails] = useState<Record<string, string>>({});
  const [savingPhone, setSavingPhone] = useState<Record<string, boolean>>({});
  const [savingEmail, setSavingEmail] = useState<Record<string, boolean>>({});
  const [phoneSavedAt, setPhoneSavedAt] = useState<Record<string, number>>({});
  const [emailSavedAt, setEmailSavedAt] = useState<Record<string, number>>({});
  const [phoneErrors, setPhoneErrors] = useState<Record<string, string>>({});
  const [emailErrors, setEmailErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    if (open) {
      const seedPhones: Record<string, string> = {};
      const seedEmails: Record<string, string> = {};
      for (const c of candidates) {
        if (c.currentPhone) seedPhones[c.candidate_id] = c.currentPhone;
        if (c.currentEmail) seedEmails[c.candidate_id] = c.currentEmail;
      }
      setPhones(seedPhones);
      setEmails(seedEmails);
      setSavingPhone({});
      setSavingEmail({});
      setPhoneSavedAt({});
      setEmailSavedAt({});
      setPhoneErrors({});
      setEmailErrors({});
    }
  }, [open, candidates]);

  const duplicateEmailIds = (() => {
    const seen = new Map<string, string[]>();
    for (const c of candidates) {
      const email = (emails[c.candidate_id] || "").trim().toLowerCase();
      if (!isValidEmail(email)) continue;
      const ids = seen.get(email) || [];
      ids.push(c.candidate_id);
      seen.set(email, ids);
    }
    const dupes = new Set<string>();
    for (const ids of seen.values()) {
      if (ids.length < 2) continue;
      for (const id of ids) dupes.add(id);
    }
    return dupes;
  })();

  const duplicatePhoneIds = (() => {
    const seen = new Map<string, string[]>();
    for (const c of candidates) {
      const phone = (phones[c.candidate_id] || "").trim();
      if (countDigits(phone) < 7) continue;
      const normalized = phone.replace(/\D/g, "");
      const ids = seen.get(normalized) || [];
      ids.push(c.candidate_id);
      seen.set(normalized, ids);
    }
    const dupes = new Set<string>();
    for (const ids of seen.values()) {
      if (ids.length < 2) continue;
      for (const id of ids) dupes.add(id);
    }
    return dupes;
  })();

  const isRowValid = (c: MissingContactCandidate) => {
    const phoneOk =
      !c.needsPhone ||
      (countDigits(phones[c.candidate_id] || "") >= 7 && !duplicatePhoneIds.has(c.candidate_id));
    const emailOk =
      !c.needsEmail ||
      (isValidEmail(emails[c.candidate_id] || "") && !duplicateEmailIds.has(c.candidate_id));
    return phoneOk && emailOk;
  };

  const allValid = candidates.length > 0 && candidates.every(isRowValid);
  const anySaving =
    Object.values(savingPhone).some(Boolean) || Object.values(savingEmail).some(Boolean);

  async function savePhone(cand: MissingContactCandidate, phone: string) {
    if (countDigits(phone) < 7) {
      setPhoneErrors(prev => ({ ...prev, [cand.candidate_id]: "At least 7 digits required" }));
      return false;
    }
    setPhoneErrors(prev => {
      const { [cand.candidate_id]: _omit, ...rest } = prev;
      return rest;
    });
    setSavingPhone(prev => ({ ...prev, [cand.candidate_id]: true }));
    try {
      const res = await fetch(
        `${API_BASE}/candidates/phone`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate_id: cand.candidate_id, phone, jobdiva_id: cand.jobdiva_id }),
        },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = body?.detail || `Save failed (${res.status})`;
        setPhoneErrors(prev => ({ ...prev, [cand.candidate_id]: String(msg) }));
        return false;
      }
      setPhoneSavedAt(prev => ({ ...prev, [cand.candidate_id]: Date.now() }));
      return true;
    } catch (e: any) {
      logger.error("missing_contacts.phone.save.error", {
        candidateId: cand.candidate_id,
        message: e?.message,
      });
      setPhoneErrors(prev => ({ ...prev, [cand.candidate_id]: e?.message || "Save failed" }));
      return false;
    } finally {
      setSavingPhone(prev => ({ ...prev, [cand.candidate_id]: false }));
    }
  }

  async function saveEmail(cand: MissingContactCandidate, email: string) {
    if (!isValidEmail(email)) {
      setEmailErrors(prev => ({ ...prev, [cand.candidate_id]: "Enter a valid email address" }));
      return false;
    }
    setEmailErrors(prev => {
      const { [cand.candidate_id]: _omit, ...rest } = prev;
      return rest;
    });
    setSavingEmail(prev => ({ ...prev, [cand.candidate_id]: true }));
    try {
      const res = await fetch(
        `${API_BASE}/candidates/email`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate_id: cand.candidate_id, email, jobdiva_id: cand.jobdiva_id }),
        },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = body?.detail || `Save failed (${res.status})`;
        setEmailErrors(prev => ({ ...prev, [cand.candidate_id]: String(msg) }));
        return false;
      }
      setEmailSavedAt(prev => ({ ...prev, [cand.candidate_id]: Date.now() }));
      return true;
    } catch (e: any) {
      logger.error("missing_contacts.email.save.error", {
        candidateId: cand.candidate_id,
        message: e?.message,
      });
      setEmailErrors(prev => ({ ...prev, [cand.candidate_id]: e?.message || "Save failed" }));
      return false;
    } finally {
      setSavingEmail(prev => ({ ...prev, [cand.candidate_id]: false }));
    }
  }

  async function handleSubmit() {
    setPhoneErrors({});
    setEmailErrors({});
    
    const updates = [];
    for (const c of candidates) {
      let needsUpdate = false;
      const item: any = { candidate_id: c.candidate_id, jobdiva_id: c.jobdiva_id };
      
      if (c.needsPhone) {
        const phone = (phones[c.candidate_id] || "").trim();
        if (countDigits(phone) < 7) {
          setPhoneErrors(prev => ({ ...prev, [c.candidate_id]: "At least 7 digits required" }));
          return;
        }
        item.phone = phone;
        needsUpdate = true;
      }
      
      if (c.needsEmail) {
        const email = (emails[c.candidate_id] || "").trim();
        if (!isValidEmail(email)) {
          setEmailErrors(prev => ({ ...prev, [c.candidate_id]: "Enter a valid email address" }));
          return;
        }
        item.email = email;
        needsUpdate = true;
      }
      
      if (needsUpdate) {
        updates.push(item);
      }
    }
    
    // Set all saving states to true
    for (const c of candidates) {
      if (c.needsPhone) setSavingPhone(prev => ({ ...prev, [c.candidate_id]: true }));
      if (c.needsEmail) setSavingEmail(prev => ({ ...prev, [c.candidate_id]: true }));
    }
    
    try {
      const res = await fetch(`${API_BASE}/candidates/bulk-contacts`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates })
      });
      
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const msg = body?.detail || `Bulk save failed (${res.status})`;
        if (candidates.length > 0) {
           setPhoneErrors(prev => ({ ...prev, [candidates[0].candidate_id]: String(msg) }));
        }
        return;
      }
      
      const out: Record<string, { phone?: string; email?: string }> = {};
      for (const c of candidates) {
        if (c.needsPhone) setPhoneSavedAt(prev => ({ ...prev, [c.candidate_id]: Date.now() }));
        if (c.needsEmail) setEmailSavedAt(prev => ({ ...prev, [c.candidate_id]: Date.now() }));
        
        const entry: { phone?: string; email?: string } = {};
        if (c.needsPhone) entry.phone = (phones[c.candidate_id] || "").trim();
        if (c.needsEmail) entry.email = (emails[c.candidate_id] || "").trim().toLowerCase();
        out[c.candidate_id] = entry;
      }
      onAllProvided(out);
    } catch (e: any) {
      logger.error("missing_contacts.bulk.save.error", { message: e?.message });
      if (candidates.length > 0) {
         setPhoneErrors(prev => ({ ...prev, [candidates[0].candidate_id]: "Failed to save contacts. Please try again." }));
      }
    } finally {
      setSavingPhone({});
      setSavingEmail({});
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-[640px] max-h-[80vh] flex flex-col p-0 gap-0 overflow-hidden">
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
            const phoneVal = phones[c.candidate_id] || "";
            const emailVal = emails[c.candidate_id] || "";
            const phoneSaving = !!savingPhone[c.candidate_id];
            const emailSaving = !!savingEmail[c.candidate_id];
            const phoneSaved = !!phoneSavedAt[c.candidate_id];
            const emailSaved = !!emailSavedAt[c.candidate_id];
            const phoneErr = phoneErrors[c.candidate_id];
            const emailErr = emailErrors[c.candidate_id];
            return (
              <div
                key={c.candidate_id}
                className="border border-slate-200 rounded-xl px-4 py-3 bg-white"
              >
                <div className="mb-3">
                  <p className="font-semibold text-slate-900 text-[14px] truncate">{c.name || "Unnamed"}</p>
                  <p className="text-[12px] text-slate-500 truncate">
                    {c.headline || "—"}
                    {(() => {
                      const loc = locationDisplay(c);
                      return loc ? ` • ${loc}` : "";
                    })()}
                    {c.source ? ` • ${c.source}` : ""}
                  </p>
                </div>
                <div className="space-y-2">
                  {c.needsPhone && (
                    <div className="flex items-center gap-2">
                      <Phone className="w-4 h-4 text-slate-400 shrink-0" />
                      <div className="flex-1 flex flex-col gap-1">
                        <div className="flex items-center gap-2">
                          <input
                            type="tel"
                            inputMode="tel"
                            autoComplete="tel"
                            placeholder="+1 555 123 4567"
                            value={phoneVal}
                            onChange={(e) =>
                              setPhones((prev) => ({ ...prev, [c.candidate_id]: e.target.value }))
                            }
                            className="h-9 flex-1 px-3 rounded-lg border border-slate-300 text-[13px] focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                          />
                          {phoneSaving ? (
                            <Loader2 className="w-4 h-4 text-slate-400 animate-spin" />
                          ) : phoneSaved ? (
                            <Check className="w-4 h-4 text-emerald-500" />
                          ) : (
                            <span className="w-4 h-4" />
                          )}
                        </div>
                        {phoneErr && <span className="text-[11px] text-rose-600">{phoneErr}</span>}
                        {!phoneErr && duplicatePhoneIds.has(c.candidate_id) && (
                          <span className="text-[11px] text-rose-600">
                            Each candidate needs a unique phone number
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                  {c.needsEmail && (
                    <div className="flex items-center gap-2">
                      <Mail className="w-4 h-4 text-slate-400 shrink-0" />
                      <div className="flex-1 flex flex-col gap-1">
                        <div className="flex items-center gap-2">
                          <input
                            type="email"
                            inputMode="email"
                            autoComplete="email"
                            placeholder="name@example.com"
                            value={emailVal}
                            onChange={(e) =>
                              setEmails((prev) => ({ ...prev, [c.candidate_id]: e.target.value }))
                            }
                            className="h-9 flex-1 px-3 rounded-lg border border-slate-300 text-[13px] focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
                          />
                          {emailSaving ? (
                            <Loader2 className="w-4 h-4 text-slate-400 animate-spin" />
                          ) : emailSaved ? (
                            <Check className="w-4 h-4 text-emerald-500" />
                          ) : (
                            <span className="w-4 h-4" />
                          )}
                        </div>
                        {emailErr && <span className="text-[11px] text-rose-600">{emailErr}</span>}
                        {!emailErr && duplicateEmailIds.has(c.candidate_id) && (
                          <span className="text-[11px] text-rose-600">
                            Each candidate needs a unique email address
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
          {candidates.length === 0 && (
            <p className="text-[13px] text-slate-500 text-center py-6">
              All set — no remaining candidates need contact details.
            </p>
          )}
        </div>

        <DialogFooter className="px-6 py-4 border-t border-slate-100 shrink-0 flex justify-between sm:justify-between gap-2">
          <Button variant="outline" onClick={onClose}>
            Skip remaining
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={!allValid || anySaving}
            className="bg-indigo-600 hover:bg-indigo-700 text-white"
          >
            {primaryLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
