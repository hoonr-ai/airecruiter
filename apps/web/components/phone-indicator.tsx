"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Phone, Loader2, Check, Search, X as XIcon } from "lucide-react";
import { API_BASE, authFetch } from "@/lib/api";
import { logger } from "@/lib/logger";

interface PhoneIndicatorProps {
  candidateId: string;
  jobdivaId?: string;
  phone?: string | null;
  onSaved: (normalisedPhone: string) => void;
  persist?: boolean;
  title?: string;
  /** LinkedIn profile URL. Required for the "Find phone" provider lookup —
   *  ZoomInfo/Apollo/Exa all key off it. Without one, only manual entry works. */
  linkedinUrl?: string;
  /** Candidate source, forwarded to the enrichment endpoint for attribution. */
  source?: string;
  /** Hard-disables the whole control (no popup, no lookup, no manual save).
   *  Used for no-contact company rows, where every action is blocked. */
  disabled?: boolean;
}

function countDigits(s: string) {
  let n = 0;
  for (let i = 0; i < s.length; i++) if (s[i] >= "0" && s[i] <= "9") n++;
  return n;
}

export function PhoneIndicator({
  candidateId,
  jobdivaId,
  phone,
  onSaved,
  persist = true,
  title,
  linkedinUrl,
  source,
  disabled = false,
}: PhoneIndicatorProps) {
  const hasPhone = !!(phone && countDigits(phone) >= 7);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [finding, setFinding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Coords for the portal-rendered popup. The candidate table is wrapped in
  // an `overflow-hidden` container, so an `absolute` popup gets clipped when
  // the phone icon lives in rows near the right edge / bottom of the table.
  // Rendering the popup at document.body with `position: fixed` escapes that
  // clipping; coords are derived from the button's bounding rect.
  const [popupCoords, setPopupCoords] = useState<{ top: number; right: number } | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setPopupCoords(null);
      return;
    }
    const computeCoords = () => {
      const btn = wrapperRef.current?.querySelector("button");
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      setPopupCoords({
        top: rect.bottom + 8,
        right: Math.max(8, window.innerWidth - rect.right),
      });
    };
    computeCoords();
    window.addEventListener("scroll", computeCoords, true);
    window.addEventListener("resize", computeCoords);
    return () => {
      window.removeEventListener("scroll", computeCoords, true);
      window.removeEventListener("resize", computeCoords);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (wrapperRef.current?.contains(t)) return;
      if (popupRef.current?.contains(t)) return;
      setOpen(false);
      setError(null);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  useEffect(() => {
    if (open) {
      setValue(phone || "");
      setError(null);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open, phone]);

  async function save() {
    const trimmed = value.trim();
    if (countDigits(trimmed) < 7) {
      setError("At least 7 digits required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      let normalised = trimmed;
      if (persist) {
        const res = await authFetch(
          `${API_BASE}/candidates/${encodeURIComponent(candidateId)}/phone`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ phone: trimmed, jobdiva_id: jobdivaId }),
          },
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail || `Save failed (${res.status})`);
        }
        const data = await res.json().catch(() => ({}));
        if (data?.phone) normalised = data.phone;
      }
      onSaved(normalised);
      setOpen(false);
    } catch (e: any) {
      logger.error("phone_indicator.save.error", {
        candidateId,
        message: e?.message,
      });
      setError(e?.message || "Save failed");
    } finally {
      setSaving(false);
    }
  }

  // Look the phone up through the provider chain (ZoomInfo → Apollo → Exa).
  // Sourcing deliberately does not buy phone numbers — they are the expensive
  // half of every provider, so spending on a candidate nobody has shortlisted is
  // speculative. This button (and Launch PAIR) are the moments with real intent,
  // so the lookup happens here, on demand, one candidate at a time.
  async function findPhone() {
    if (!linkedinUrl) {
      setError("No LinkedIn URL on this candidate to look up");
      return;
    }
    setFinding(true);
    setError(null);
    try {
      const res = await authFetch(`${API_BASE}/candidates/enrich-contact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          candidate_id: candidateId,
          jobdiva_id: jobdivaId,
          linkedin_url: linkedinUrl,
          source,
        }),
      });
      const data = await res.json().catch(() => ({} as any));
      if (!res.ok) throw new Error(data?.detail || `Lookup failed (${res.status})`);

      // `phone` is the endpoint's already-normalised primary pick; mobile/work
      // are the raw per-slot values it also returns, kept as fallbacks.
      const found = String(
        data?.phone || data?.mobilePhone || data?.workPhone || "",
      ).trim();
      if (!found || countDigits(found) < 7) {
        // Not an error — the providers genuinely may not hold a number. Leave
        // the field ready so the recruiter can enter one they sourced elsewhere.
        setError(
          data?.provider
            ? `No phone found (tried ${data.provider})`
            : "No phone found by any provider — enter one manually",
        );
        return;
      }
      // Prefill rather than auto-commit: the recruiter sees what was found and
      // confirms with Save, matching how a manually typed number is handled.
      setValue(found);
      inputRef.current?.focus();
    } catch (e: any) {
      logger.error("phone_indicator.find.error", { candidateId, message: e?.message });
      setError(e?.message || "Lookup failed");
    } finally {
      setFinding(false);
    }
  }

  const tooltip = disabled
    ? title || "No-contact company — phone actions disabled"
    : title ||
      (hasPhone
        ? phone || "Phone number on file"
        : "Click to look up or add a phone number");

  const popup = !disabled && open && popupCoords && typeof document !== "undefined"
    ? createPortal(
        <div
          ref={popupRef}
          style={{ position: "fixed", top: popupCoords.top, right: popupCoords.right, zIndex: 60 }}
          className="w-72 rounded-xl border border-slate-200 bg-white shadow-xl p-3"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="flex items-center justify-between mb-2">
            <p className="text-[12px] font-bold text-slate-700">
              {hasPhone ? "Update phone" : "Add phone number"}
            </p>
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                setError(null);
              }}
              className="text-slate-400 hover:text-slate-600"
              aria-label="Close"
            >
              <XIcon className="w-3.5 h-3.5" />
            </button>
          </div>
          <input
            ref={inputRef}
            type="tel"
            inputMode="tel"
            autoComplete="tel"
            placeholder="+1 555 123 4567"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                save();
              } else if (e.key === "Escape") {
                setOpen(false);
                setError(null);
              }
            }}
            className="w-full h-9 px-3 rounded-lg border border-slate-300 text-[13px] focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent"
          />
          {error && <p className="text-[11px] text-rose-600 mt-1.5">{error}</p>}
          <div className="flex items-center justify-between gap-2 mt-2.5">
            <button
              type="button"
              onClick={findPhone}
              disabled={finding || saving || !linkedinUrl}
              title={
                linkedinUrl
                  ? "Look this number up via ZoomInfo → Apollo → Exa"
                  : "No LinkedIn URL on this candidate to look up"
              }
              className="text-[12px] px-2.5 py-1.5 rounded-md border border-slate-200 text-slate-600 hover:bg-slate-50 font-medium disabled:opacity-50 flex items-center gap-1.5 shrink-0"
            >
              {finding ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <Search className="w-3 h-3" />
              )}
              {finding ? "Looking…" : "Find phone"}
            </button>
            <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                setError(null);
              }}
              className="text-[12px] px-2.5 py-1.5 rounded-md text-slate-500 hover:bg-slate-100 font-medium"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={save}
              disabled={saving}
              className="text-[12px] px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 font-semibold disabled:opacity-60 flex items-center gap-1.5"
            >
              {saving ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}
              Save
            </button>
            </div>
          </div>
        </div>,
        document.body,
      )
    : null;

  return (
    <div ref={wrapperRef} className="relative inline-flex items-center">
      <button
        type="button"
        disabled={disabled}
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          if (disabled) return;
          setOpen((v) => !v);
        }}
        title={tooltip}
        aria-label={tooltip}
        className={`h-7 w-7 flex items-center justify-center rounded-lg border shadow-sm transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
          disabled
            ? "border-slate-200 bg-slate-100 text-slate-300"
            : hasPhone
              ? "border-emerald-200 bg-emerald-50 text-emerald-600 hover:bg-emerald-100"
              : "border-slate-200 bg-white text-slate-400 hover:bg-slate-50 hover:text-slate-600"
        }`}
      >
        <Phone className={`w-3.5 h-3.5 ${hasPhone ? "fill-emerald-500" : ""}`} />
      </button>
      {popup}
    </div>
  );
}
