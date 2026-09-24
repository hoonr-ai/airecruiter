export function normalizeToUtcDate(dateString: string | null | undefined): Date | null {
  if (!dateString) return null;
  
  let safeStr = dateString;
  
  // If it's a bare date (e.g. "2026-08-27") without a time component, leave it alone
  // otherwise appending Z produces invalid ISO string like "2026-08-27Z"
  if (safeStr.length === 10 && /^\d{4}-\d{2}-\d{2}$/.test(safeStr)) {
    // It's a bare date, parse as-is
  } else if (typeof safeStr === "string" && !safeStr.match(/(Z|[+-]\d{2}:?\d{2})$/)) {
    // If it's a date-time missing timezone, assume UTC
    safeStr = safeStr.replace(" ", "T") + "Z";
  }

  const date = new Date(safeStr);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  return date;
}

// ---------------------------------------------------------------------------
// Eastern-time display for the admin reports
// ---------------------------------------------------------------------------
// Every report timestamp (Launch Report, Admin Analytics, Candidates, Recruiter
// Analytics) is shown in US Eastern time as "MM/DD/YYYY HH:MM:SS" — 24-hour, no
// zone suffix in the cell. The zone is stated once, in the column header
// (`withEasternLabel`), instead of repeating "EDT"/"EST" in every cell and CSV
// value. Keep these the only formatters those pages use so the format can't
// drift between reports again.

export const REPORT_TIME_ZONE = "America/New_York";
/** Short label for column headers: "PAIR Launch (ET)". */
export const REPORT_TIME_ZONE_LABEL = "ET";
export const EMPTY_DATE = "—";

const easternDateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: REPORT_TIME_ZONE,
  month: "2-digit",
  day: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

const easternDateFormatter = new Intl.DateTimeFormat("en-US", {
  timeZone: REPORT_TIME_ZONE,
  month: "2-digit",
  day: "2-digit",
  year: "numeric",
});

function partsOf(formatter: Intl.DateTimeFormat, date: Date): Record<string, string> {
  return Object.fromEntries(formatter.formatToParts(date).map((p) => [p.type, p.value]));
}

/** Instant → "09/21/2026 09:46:55" in Eastern time; null/invalid → "—".
 *  Naive strings are UTC (see normalizeToUtcDate). */
export function formatEasternDateTime(iso: string | null | undefined): string {
  const date = normalizeToUtcDate(iso);
  if (!date) return EMPTY_DATE;
  const p = partsOf(easternDateTimeFormatter, date);
  // Some engines still emit "24" for midnight even with hourCycle h23.
  const hour = p.hour === "24" ? "00" : p.hour;
  return `${p.month}/${p.day}/${p.year} ${hour}:${p.minute}:${p.second}`;
}

/** Instant → "09:46:55" in Eastern time; null/invalid → "—". */
export function formatEasternTime(iso: string | null | undefined): string {
  const full = formatEasternDateTime(iso);
  return full === EMPTY_DATE ? full : full.slice(11);
}

/** Date or instant → "09/21/2026"; null/invalid → "—".
 *  A bare "YYYY-MM-DD" is a calendar date, not an instant: it is rendered as
 *  written, never shifted into the previous day by a timezone conversion. */
export function formatEasternDate(iso: string | null | undefined): string {
  if (iso && /^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    const [y, m, d] = iso.split("-");
    return `${m}/${d}/${y}`;
  }
  const date = normalizeToUtcDate(iso);
  if (!date) return EMPTY_DATE;
  const p = partsOf(easternDateFormatter, date);
  return `${p.month}/${p.day}/${p.year}`;
}

/** "PAIR Launch" → "PAIR Launch (ET)". */
export function withEasternLabel(label: string): string {
  return `${label} (${REPORT_TIME_ZONE_LABEL})`;
}

/** Today's calendar date in Eastern time, as YYYY-MM-DD. */
export function todayEastern(now: Date = new Date()): string {
  // en-CA gives ISO-shaped output (YYYY-MM-DD) directly.
  return now.toLocaleDateString("en-CA", { timeZone: REPORT_TIME_ZONE });
}

// ---------------------------------------------------------------------------
// Calendar-date arithmetic and durations (shared by the report pages)
// ---------------------------------------------------------------------------
// Dates here are "YYYY-MM-DD" calendar strings; the arithmetic is done at UTC
// midnight so it never shifts with the viewer's timezone or DST.

/** "2026-09-21" + 3 → "2026-09-24". */
export function addIsoDays(date: string, days: number): string {
  const next = new Date(`${date}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + days);
  return next.toISOString().slice(0, 10);
}

/** Days in [start, end], both inclusive: the same day → 1. */
export function inclusiveDateSpanDays(start: string, end: string): number {
  const startTime = Date.parse(`${start}T00:00:00Z`);
  const endTime = Date.parse(`${end}T00:00:00Z`);
  return Math.floor((endTime - startTime) / (24 * 60 * 60 * 1000)) + 1;
}

/** Minutes → "45m" / "1h 22m" / "2d 3h"; null → "—". */
export function formatDuration(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return EMPTY_DATE;
  const total = Math.round(minutes);
  if (total < 60) return `${total}m`;
  const hours = Math.floor(total / 60);
  if (hours < 24) {
    const rem = total % 60;
    return rem ? `${hours}h ${rem}m` : `${hours}h`;
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days}d ${remHours}h` : `${days}d`;
}
