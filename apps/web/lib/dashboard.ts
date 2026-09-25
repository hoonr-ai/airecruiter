/**
 * Pure helpers for the PAIR Dashboard (app/admin/dashboard): the date presets,
 * the "vs prev" badge math and the number formats the cards share.
 *
 * Deliberately free of React and of other lib imports so `node --test` can run
 * it directly (see lib/dashboard.test.ts). Dates are "YYYY-MM-DD" calendar
 * strings in US Eastern time, the same convention as the report pages
 * (lib/date.ts); the arithmetic runs at UTC midnight so it never shifts with
 * the viewer's timezone or DST.
 */

export type DashboardPreset = "7d" | "30d" | "quarter" | "ytd" | "all" | "custom";

/** Inclusive Eastern calendar range. `null` everywhere below means all time. */
export type DateRange = { start: string; end: string };

export const DASHBOARD_PRESETS: { key: Exclude<DashboardPreset, "custom">; label: string }[] = [
  { key: "7d", label: "Last 7 Days" },
  { key: "30d", label: "Last 30 Days" },
  { key: "quarter", label: "Last Quarter" },
  { key: "ytd", label: "YTD" },
  { key: "all", label: "All Time" },
];

// Mirrors MAX_RANGE_DAYS in services/pair_dashboard.py.
export const MAX_RANGE_DAYS = 366;

function shiftIsoDate(date: string, days: number): string {
  const next = new Date(`${date}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + days);
  return next.toISOString().slice(0, 10);
}

/**
 * The range a preset stands for, ending today (Eastern).
 *
 * "Last Quarter" is the trailing 90 days, not the previous calendar quarter:
 * the mockup the stakeholders signed off on labels it "Mar 18 – Jun 17".
 */
export function presetRange(preset: Exclude<DashboardPreset, "custom">, today: string): DateRange | null {
  switch (preset) {
    case "7d":
      return { start: shiftIsoDate(today, -6), end: today };
    case "30d":
      return { start: shiftIsoDate(today, -29), end: today };
    case "quarter":
      return { start: shiftIsoDate(today, -89), end: today };
    case "ytd":
      return { start: `${today.slice(0, 4)}-01-01`, end: today };
    case "all":
      return null;
  }
}

/** Days in the range, both ends inclusive. */
export function rangeDays(range: DateRange): number {
  const start = Date.parse(`${range.start}T00:00:00Z`);
  const end = Date.parse(`${range.end}T00:00:00Z`);
  return Math.floor((end - start) / 86_400_000) + 1;
}

/** Why a custom range can't be used, or null when it can. */
export function customRangeError(start: string, end: string): string | null {
  if (!start || !end) return "Pick both a start and an end date.";
  if (start > end) return "The start date must be on or before the end date.";
  if (rangeDays({ start, end }) > MAX_RANGE_DAYS) return `A range can span at most ${MAX_RANGE_DAYS} days.`;
  return null;
}

// ---------------------------------------------------------------------------
// "vs prev" badges
// ---------------------------------------------------------------------------

/**
 * The comparison badge on a card.
 *   pct  — relative change of a count (+33%)
 *   pts  — change of a rate in percentage points (+8 pts); a relative change
 *          of a percentage ("+24% of 34%") reads as nonsense
 *   new  — the previous period had none, so there is no ratio to show
 *   null — nothing to compare against (All Time, or the data is unavailable)
 */
export type Change =
  | { kind: "pct"; value: number }
  | { kind: "pts"; value: number }
  | { kind: "new" }
  | null;

// Math.round sends -12.5 to -12 but 12.5 to 13; a drop must read like a rise.
function roundHalfAway(value: number): number {
  return Math.sign(value) * Math.round(Math.abs(value));
}

export function countChange(current: number | null | undefined, previous: number | null | undefined): Change {
  if (current === null || current === undefined || previous === null || previous === undefined) return null;
  if (previous === 0) return current === 0 ? { kind: "pct", value: 0 } : { kind: "new" };
  return { kind: "pct", value: roundHalfAway(((current - previous) / previous) * 100) };
}

/** Rates are fractions (0.34 = 34%). */
export function rateChange(current: number | null | undefined, previous: number | null | undefined): Change {
  if (current === null || current === undefined || previous === null || previous === undefined) return null;
  return { kind: "pts", value: roundHalfAway((current - previous) * 100) };
}

export function formatChange(change: Change): string {
  if (change === null) return "";
  if (change.kind === "new") return "new";
  const sign = change.value > 0 ? "+" : "";
  return change.kind === "pts" ? `${sign}${change.value} pts` : `${sign}${change.value}%`;
}

/** Up, down or flat, for colouring the badge. */
export function changeDirection(change: Change): "up" | "down" | "flat" | "none" {
  if (change === null) return "none";
  if (change.kind === "new") return "up";
  if (change.value > 0) return "up";
  if (change.value < 0) return "down";
  return "flat";
}

// ---------------------------------------------------------------------------
// Formats
// ---------------------------------------------------------------------------

export const EMPTY_VALUE = "—";

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY_VALUE;
  return Math.round(value).toLocaleString("en-US");
}

/** Averages (per-recruiter productivity): one decimal, trailing ".0" dropped. */
export function formatAverage(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EMPTY_VALUE;
  const rounded = Math.round(value * 10) / 10;
  return rounded.toLocaleString("en-US", { maximumFractionDigits: 1 });
}

/** A fraction as a whole percent: 0.345 → "35%". */
export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || Number.isNaN(rate)) return EMPTY_VALUE;
  return `${Math.round(rate * 100)}%`;
}

/** Hours for the speed tiles: "18 hrs", "1,240 hrs"; under an hour "0.5 hrs". */
export function formatHours(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || Number.isNaN(hours)) return EMPTY_VALUE;
  if (hours < 10) return `${Math.round(hours * 10) / 10} hrs`;
  return `${Math.round(hours).toLocaleString("en-US")} hrs`;
}

/** The same duration in days, as the secondary line under an hours value. */
export function hoursAsDays(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || Number.isNaN(hours)) return "";
  const days = hours / 24;
  return days < 1 ? "under a day" : `${Math.round(days * 10) / 10} days`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function shortDate(iso: string): { month: string; day: number; year: number } {
  const [year, month, day] = iso.split("-").map(Number);
  return { month: MONTHS[month - 1] ?? "", day, year };
}

/** "2026-09-22" → "Sep 22" (a week's Monday on the trend axis). */
export function formatWeekLabel(iso: string): string {
  const { month, day } = shortDate(iso);
  return `${month} ${day}`;
}

/** "2026-08-21" (or an ISO timestamp) → "Aug 21, 2026". */
export function formatShortDay(iso: string): string {
  const { month, day, year } = shortDate(iso.slice(0, 10));
  return `${month} ${day}, ${year}`;
}

/** "Jun 10 – Jun 17, 2026", "Dec 20, 2025 – Jan 2, 2026", or "All time". */
export function formatRangeLabel(range: DateRange | null): string {
  if (range === null) return "All time";
  const a = shortDate(range.start);
  const b = shortDate(range.end);
  if (range.start === range.end) return `${b.month} ${b.day}, ${b.year}`;
  if (a.year === b.year) return `${a.month} ${a.day} – ${b.month} ${b.day}, ${b.year}`;
  return `${a.month} ${a.day}, ${a.year} – ${b.month} ${b.day}, ${b.year}`;
}

/** Stage-to-stage conversion in the funnel; null when the earlier stage is empty. */
export function conversion(previous: number, current: number): number | null {
  return previous > 0 ? current / previous : null;
}

/** Share of the funnel's top stage, for the bar widths. */
export function shareOfTop(top: number, count: number): number {
  if (top <= 0) return 0;
  return Math.max(0, Math.min(1, count / top));
}
