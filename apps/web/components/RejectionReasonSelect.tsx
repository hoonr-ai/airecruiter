"use client";

import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Search, ChevronDown, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { REJECTION_REASONS } from "@/lib/rejection";

function scoreMatch(reason: string, query: string): number {
  const r = reason.toLowerCase();
  const q = query.toLowerCase().trim();

  // Exact match
  if (r === q) return 100;
  // Starts with query
  if (r.startsWith(q)) return 90;

  const words = q.split(/\s+/).filter(Boolean);

  // All query words appear in the reason (any order)
  if (words.every((w) => r.includes(w))) return 70;
  // Some query words appear
  const matchedWords = words.filter((w) => r.includes(w)).length;
  if (matchedWords > 0) return 40 + (matchedWords / words.length) * 20;

  // Acronym: query letters match first letters of words in the reason
  const reasonWords = reason.split(/[\s/\-–—]+/).map((w) => w[0]?.toLowerCase()).join("");
  if (reasonWords.startsWith(q)) return 30;
  if (q.split("").every((ch) => reasonWords.includes(ch))) return 15;

  return 0;
}

function smartFilter(reasons: string[], query: string): string[] {
  return reasons
    .map((r) => ({ r, score: scoreMatch(r, query) }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score)
    .map(({ r }) => r);
}

interface RejectionReasonSelectProps {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  className?: string;
}

export function RejectionReasonSelect({
  value,
  onChange,
  id,
  className,
}: RejectionReasonSelectProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const filtered = search.trim() ? smartFilter(REJECTION_REASONS, search) : REJECTION_REASONS;

  useEffect(() => {
    if (open) {
      if (triggerRef.current) {
        const rect = triggerRef.current.getBoundingClientRect();
        setDropdownStyle({
          position: "fixed",
          top: rect.bottom + 4,
          left: rect.left,
          width: rect.width,
          zIndex: 9999,
        });
      }
      setTimeout(() => searchRef.current?.focus(), 0);
    } else {
      setSearch("");
    }
  }, [open]);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      const target = e.target as Node;
      if (
        triggerRef.current && !triggerRef.current.contains(target) &&
        dropdownRef.current && !dropdownRef.current.contains(target)
      ) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    if (open) document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open]);

  const dropdown = open ? (
    <div
      ref={dropdownRef}
      style={dropdownStyle}
      className="rounded-lg border border-slate-200 bg-white shadow-xl"
    >
      <div className="flex items-center gap-2 border-b border-slate-100 px-3 py-2">
        <Search className="size-4 text-slate-400 shrink-0" />
        <input
          ref={searchRef}
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search reasons..."
          className="w-full text-sm outline-none placeholder:text-slate-400"
        />
      </div>

      <ul role="listbox" className="max-h-60 overflow-y-auto py-1">
        {filtered.length === 0 ? (
          <li className="px-3 py-2 text-sm text-slate-400">No results found.</li>
        ) : (
          filtered.map((reason) => (
            <li
              key={reason}
              role="option"
              aria-selected={value === reason}
              onClick={() => {
                onChange(reason);
                setOpen(false);
              }}
              className={cn(
                "flex items-center justify-between px-3 py-2 text-sm cursor-pointer select-none",
                value === reason
                  ? "bg-rose-50 text-rose-700"
                  : "text-slate-700 hover:bg-slate-50"
              )}
            >
              <span>{reason}</span>
              {value === reason && <Check className="size-4 shrink-0" />}
            </li>
          ))
        )}
      </ul>
    </div>
  ) : null;

  return (
    <div className={cn("relative", className)}>
      <button
        ref={triggerRef}
        id={id}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="w-full h-11 px-3 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500/50 bg-white flex items-center justify-between gap-2 text-left"
      >
        <span className={cn("truncate", !value && "text-slate-400")}>
          {value || "Select a reason..."}
        </span>
        <ChevronDown className={cn("size-4 text-slate-400 shrink-0 transition-transform", open && "rotate-180")} />
      </button>

      {typeof document !== "undefined" && createPortal(dropdown, document.body)}
    </div>
  );
}
