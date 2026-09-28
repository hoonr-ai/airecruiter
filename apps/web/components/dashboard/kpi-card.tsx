"use client";

import type { ComponentType, ReactNode } from "react";
import { TrendingDown, TrendingUp, Minus } from "lucide-react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { changeDirection, formatChange, formatWeekLabel, type Change } from "@/lib/dashboard";
import { Sparkline, TrendChart } from "./sparkline";

export const TONES = {
  violet: { color: "#7c3aed", bg: "bg-violet-50", text: "text-violet-700" },
  indigo: { color: "#4338ca", bg: "bg-indigo-50", text: "text-indigo-700" },
  sky: { color: "#0891b2", bg: "bg-cyan-50", text: "text-cyan-700" },
  blue: { color: "#0284c7", bg: "bg-sky-50", text: "text-sky-700" },
  emerald: { color: "#059669", bg: "bg-emerald-50", text: "text-emerald-700" },
  teal: { color: "#0e7490", bg: "bg-teal-50", text: "text-teal-700" },
  purple: { color: "#9333ea", bg: "bg-purple-50", text: "text-purple-700" },
  amber: { color: "#d97706", bg: "bg-amber-50", text: "text-amber-700" },
  slate: { color: "#64748b", bg: "bg-slate-100", text: "text-slate-700" },
} as const;

export type Tone = keyof typeof TONES;

export function ChangeBadge({ change, allTime }: { change: Change; allTime: boolean }) {
  if (change === null) {
    return (
      <span
        className="inline-flex shrink-0 items-center whitespace-nowrap rounded-md bg-slate-100 px-1.5 py-0.5 text-[10.5px] font-semibold text-slate-500"
        title={
          allTime
            ? "All Time has no previous period to compare with"
            : "No comparison: the previous period has no data yet (before the JobDiva history, or nothing to divide by)"
        }
      >
        {allTime ? "All time" : "—"}
      </span>
    );
  }
  const direction = changeDirection(change);
  const styles =
    direction === "up"
      ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
      : direction === "down"
        ? "bg-rose-50 text-rose-700 ring-rose-200"
        : "bg-slate-50 text-slate-600 ring-slate-200";
  const Icon = direction === "up" ? TrendingUp : direction === "down" ? TrendingDown : Minus;
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-[10.5px] font-semibold ring-1 ring-inset ${styles}`}
      title="Compared with the same number of days just before"
    >
      <Icon className="h-3 w-3" />
      {formatChange(change)}
      <span className="hidden font-medium opacity-70 2xl:inline">vs prev</span>
    </span>
  );
}

export function KpiCard({
  icon: Icon,
  tone,
  label,
  value,
  sub,
  breakdown,
  change,
  allTime,
  weekly,
  unavailable,
  footnote,
  loading,
  onOpen,
}: {
  icon: ComponentType<{ className?: string; style?: React.CSSProperties }>;
  tone: Tone;
  label: string;
  value: string;
  sub?: string;
  breakdown?: ReactNode;
  change: Change;
  allTime: boolean;
  weekly: (number | null)[];
  unavailable?: string;
  /** e.g. "JobDiva data from Aug 21, 2026" when the period starts before the mirror. */
  footnote?: string;
  loading?: boolean;
  onOpen?: () => void;
}) {
  const t = TONES[tone];
  const clickable = !!onOpen && !unavailable && !loading;
  return (
    <button
      type="button"
      onClick={clickable ? onOpen : undefined}
      disabled={!clickable}
      title={unavailable || (clickable ? "Show the weekly trend" : undefined)}
      className={`group flex min-h-[172px] flex-col rounded-xl border border-slate-200 bg-white p-4 text-left shadow-sm transition-all ${
        clickable ? "cursor-pointer hover:border-slate-300 hover:shadow-md" : "cursor-default"
      }`}
    >
      <div className="flex w-full items-start justify-between gap-2">
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${t.bg}`}>
          <Icon className="h-4 w-4" style={{ color: t.color }} />
        </div>
        {!unavailable && !loading && <ChangeBadge change={change} allTime={allTime} />}
      </div>
      <div className="mt-3">
        {loading ? (
          <div className="h-8 w-20 animate-pulse rounded bg-slate-100" />
        ) : (
          <div
            className="text-[28px] font-bold leading-none tabular-nums"
            style={{ color: unavailable ? "#94a3b8" : t.color }}
          >
            {unavailable ? "—" : value}
          </div>
        )}
        {breakdown && !unavailable && !loading && (
          <div className="mt-1.5 text-[11.5px] font-semibold tabular-nums">{breakdown}</div>
        )}
      </div>
      <div className="mt-2 text-[11.5px] font-bold uppercase leading-snug tracking-wide text-slate-600">{label}</div>
      {sub && <div className="mt-0.5 text-[11.5px] leading-snug text-slate-400">{sub}</div>}
      {unavailable && !loading && <div className="mt-1 text-[11.5px] leading-snug text-amber-700">{unavailable}</div>}
      {footnote && !unavailable && !loading && (
        <div className="mt-1 text-[11px] leading-snug text-amber-700">{footnote}</div>
      )}
      <div className="mt-auto pt-3">
        {!unavailable && !loading && <Sparkline values={weekly} color={t.color} label={`${label} weekly trend`} />}
      </div>
    </button>
  );
}

export type TrendColumn = { label: string; values: (number | null)[] };

export function TrendModal({
  open,
  onOpenChange,
  title,
  note,
  weeks,
  values,
  color,
  format,
  columns = [],
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  note?: string;
  weeks: string[];
  values: (number | null)[];
  color: string;
  format: (value: number | null) => string;
  columns?: TrendColumn[];
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl bg-white">
        <DialogHeader>
          <DialogTitle className="text-[16px] font-bold text-slate-900">{title} — Weekly Trend</DialogTitle>
          {note && <DialogDescription className="text-[12.5px] text-slate-500">{note}</DialogDescription>}
        </DialogHeader>
        <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3">
          <TrendChart weeks={weeks} values={values} color={color} format={format} />
        </div>
        <div className="max-h-[300px] overflow-auto rounded-lg border border-slate-200">
          <table className="w-full text-[12.5px]">
            <thead className="sticky top-0 bg-slate-50 text-left text-[11px] font-bold uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2">Week of</th>
                <th className="px-3 py-2 text-right">{title}</th>
                {columns.map((c) => (
                  <th key={c.label} className="px-3 py-2 text-right">
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {weeks
                .map((week, i) => ({ week, i }))
                .reverse()
                .map(({ week, i }) => (
                  <tr key={week} className="border-t border-slate-100">
                    <td className="px-3 py-2 font-medium text-slate-700">{formatWeekLabel(week)}</td>
                    <td className="px-3 py-2 text-right font-semibold tabular-nums" style={{ color }}>
                      {format(values[i])}
                    </td>
                    {columns.map((c) => (
                      <td key={c.label} className="px-3 py-2 text-right tabular-nums text-slate-600">
                        {format(c.values[i])}
                      </td>
                    ))}
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </DialogContent>
    </Dialog>
  );
}
