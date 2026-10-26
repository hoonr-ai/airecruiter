"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Search, ChevronDown, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import { REJECTION_REASONS } from "@/lib/rejection";
import { smartFilter } from "@/lib/rejection-search";

interface RejectionReasonSelectProps {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  className?: string;
  /** Accessible name for the listbox — the native <select> this replaced
   * had aria-label="Rejection Reason"; PR #840 review caught that this was
   * dropped entirely rather than carried over. */
  ariaLabel?: string;
}

export function RejectionReasonSelect({
  value,
  onChange,
  id,
  className,
  ariaLabel = "Rejection reason",
}: RejectionReasonSelectProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  // -1 = nothing highlighted (e.g. the field is empty and nothing is
  // "current" yet). Arrow keys move this; Enter selects whatever it points
  // at. Separate from `value` (the actually-chosen reason) so navigating
  // with arrow keys doesn't commit a selection until Enter.
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const listboxRef = useRef<HTMLUListElement>(null);
  const optionRefs = useRef<Array<HTMLLIElement | null>>([]);
  const listboxId = id ? `${id}-listbox` : "rejection-reason-listbox";
  const optionId = useCallback((index: number) => `${listboxId}-option-${index}`, [listboxId]);

  // Mirrors highlightedIndex, updated synchronously (not via an effect) by
  // every setter below. ArrowDown/ArrowUp need the FRESHLY-computed index
  // synchronously, in the same tick, to pass to scrollOptionIntoView — but
  // several native keydown events dispatched in quick succession (holding
  // the key down) can all run before React re-renders and commits the
  // previous one's state update, so reading `highlightedIndex` itself inside
  // the handler would read the same stale value for each and only ever
  // advance by one. The ref can't go stale that way since this sets it
  // directly, same tick, every time.
  const highlightedIndexRef = useRef(-1);
  const setHighlighted = useCallback((next: number) => {
    highlightedIndexRef.current = next;
    setHighlightedIndex(next);
  }, []);

  // Scrolls the listbox's own scrollTop just enough to bring option `index`
  // into view — never the page or an enclosing modal (PR #843 review: the
  // previous scrollIntoView() could scroll ancestor containers too, not just
  // this list). Reads refs directly instead of document.getElementById
  // (same review) so this can't accidentally pick up stale/duplicate ids.
  const scrollOptionIntoView = useCallback((index: number) => {
    const listboxEl = listboxRef.current;
    const optionEl = optionRefs.current[index];
    if (!listboxEl || !optionEl) return;
    const top = optionEl.offsetTop;
    const bottom = top + optionEl.offsetHeight;
    if (top < listboxEl.scrollTop) {
      listboxEl.scrollTop = top;
    } else if (bottom > listboxEl.scrollTop + listboxEl.clientHeight) {
      listboxEl.scrollTop = bottom - listboxEl.clientHeight;
    }
  }, []);

  const filtered = useMemo(
    () => (search.trim() ? smartFilter(REJECTION_REASONS, search) : REJECTION_REASONS),
    [search]
  );

  const updateDropdownPosition = useCallback(() => {
    if (!triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    setDropdownStyle({
      position: "fixed",
      top: rect.bottom + 4,
      left: rect.left,
      width: rect.width,
      zIndex: 9999,
    });
  }, []);

  const closeAndFocusTrigger = useCallback(() => {
    setOpen(false);
    // Returns focus to the trigger on every close path (Escape, selecting
    // an option, clicking outside) — PR #840 review: this used to leave
    // focus wherever it was in the dropdown's own (now-unmounted) DOM,
    // silently dropping keyboard focus for the rest of the page.
    triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) {
      setSearch("");
      setHighlighted(-1);
      return;
    }
    updateDropdownPosition();
    // Keeps the dropdown anchored to the trigger instead of floating in a
    // stale position (PR #840 review) — a fixed-position portal doesn't
    // move with its trigger on its own the way an in-flow element would.
    window.addEventListener("scroll", updateDropdownPosition, true);
    window.addEventListener("resize", updateDropdownPosition);
    const focusTimeout = setTimeout(() => searchRef.current?.focus(), 0);
    return () => {
      window.removeEventListener("scroll", updateDropdownPosition, true);
      window.removeEventListener("resize", updateDropdownPosition);
      clearTimeout(focusTimeout);
    };
  }, [open, updateDropdownPosition, setHighlighted]);

  // Whenever the result set changes (a new search, or the dropdown just
  // opened), the previous highlighted index can easily point past the end
  // of the new list, or at a different reason than before — land on
  // whichever position holds the current value if it's still in the
  // filtered set, otherwise the top result.
  useEffect(() => {
    if (!open) return;
    const currentIndex = filtered.indexOf(value);
    const next = currentIndex >= 0 ? currentIndex : filtered.length > 0 ? 0 : -1;
    setHighlighted(next);
    if (next >= 0) {
      // Runs after the list for this filter has painted, so option refs are
      // attached — a plain call here (not inside a rAF) would target last
      // render's (possibly longer) list.
      requestAnimationFrame(() => scrollOptionIntoView(next));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, filtered]);

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

  const selectHighlighted = () => {
    if (highlightedIndex < 0 || highlightedIndex >= filtered.length) return;
    onChange(filtered[highlightedIndex]);
    closeAndFocusTrigger();
  };

  // Handled as a React onKeyDown on the dropdown itself (not a
  // document-level listener) specifically so stopPropagation actually
  // contains it — PR #840 review flagged that a global Escape listener
  // could also trigger an enclosing modal's own Escape-to-close handler.
  // React portals still bubble synthetic events through the COMPONENT tree
  // (this is rendered as a child of whatever modal uses it, even though its
  // DOM node lives in document.body), so this stays scoped to this control.
  const handleDropdownKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "ArrowDown": {
        e.preventDefault();
        e.stopPropagation();
        // Reads/writes highlightedIndexRef, not the `highlightedIndex` state
        // closed over by this render — see the ref's own comment: several
        // ArrowDown keydowns from the OS's key-repeat can fire before React
        // re-renders, and each would otherwise compute "next" from the same
        // stale value.
        const next = filtered.length === 0 ? -1 : Math.min(highlightedIndexRef.current + 1, filtered.length - 1);
        setHighlighted(next);
        // Scrolled here, not in an effect keyed on highlightedIndex (PR #843
        // review) — an effect would also fire for onMouseEnter's hover-driven
        // highlight changes below, yanking the list out from under the
        // cursor when hovering a partially-visible edge option.
        if (next >= 0) scrollOptionIntoView(next);
        break;
      }
      case "ArrowUp": {
        e.preventDefault();
        e.stopPropagation();
        const next = filtered.length === 0 ? -1 : Math.max(highlightedIndexRef.current - 1, 0);
        setHighlighted(next);
        if (next >= 0) scrollOptionIntoView(next);
        break;
      }
      case "Enter":
        e.preventDefault();
        e.stopPropagation();
        selectHighlighted();
        break;
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        closeAndFocusTrigger();
        break;
      default:
        break;
    }
  };

  const dropdown = open ? (
    <div
      ref={dropdownRef}
      style={dropdownStyle}
      className="rounded-lg border border-slate-200 bg-white shadow-xl"
      onKeyDown={handleDropdownKeyDown}
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
          role="combobox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-activedescendant={highlightedIndex >= 0 ? optionId(highlightedIndex) : undefined}
        />
      </div>

      <ul ref={listboxRef} id={listboxId} role="listbox" aria-label={ariaLabel} className="max-h-60 overflow-y-auto py-1 select-none">
        {filtered.length === 0 ? (
          <li className="px-3 py-2 text-sm text-slate-400">No results found.</li>
        ) : (
          filtered.map((reason, index) => (
            <li
              key={reason}
              ref={(el) => { optionRefs.current[index] = el; }}
              id={optionId(index)}
              role="option"
              aria-selected={value === reason}
              // Hover only highlights — it deliberately never scrolls (PR
              // #843 review): scrollOptionIntoView is called solely from the
              // ArrowUp/ArrowDown handlers above.
              onMouseEnter={() => setHighlighted(index)}
              onClick={() => {
                onChange(reason);
                closeAndFocusTrigger();
              }}
              className={cn(
                "flex items-center justify-between px-3 py-2 text-sm cursor-pointer",
                index === highlightedIndex
                  ? "bg-rose-50 text-rose-700"
                  : value === reason
                    ? "bg-rose-50/60 text-rose-700"
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
        aria-label={ariaLabel}
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
