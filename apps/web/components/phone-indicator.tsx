"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Phone, Loader2, Check, X as XIcon } from "lucide-react";
import { API_BASE } from "@/lib/api";
import { logger } from "@/lib/logger";

interface PhoneIndicatorProps {
  candidateId: string;
  jobdivaId?: string;
  phone?: string | null;
  onSaved: (normalisedPhone: string) => void;
  persist?: boolean;
  title?: string;
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
}: PhoneIndicatorProps) {
  const hasPhone = !!(phone && countDigits(phone) >= 7);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
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
        const res = await fetch(
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

  const tooltip = title || (hasPhone ? phone || "Phone number on file" : "Click to add phone number");

  const popup = open && popupCoords && typeof document !== "undefined"
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
          <div className="flex items-center justify-end gap-2 mt-2.5">
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
        </div>,
        document.body,
      )
    : null;

  return (
    <div ref={wrapperRef} className="relative inline-flex items-center">
      <button
        type="button"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        title={tooltip}
        aria-label={tooltip}
        className={`h-7 w-7 flex items-center justify-center rounded-lg border shadow-sm transition-all ${
          hasPhone
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
