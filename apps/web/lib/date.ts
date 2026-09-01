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
