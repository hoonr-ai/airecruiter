/**
 * CSV writing helpers.
 *
 * Kept out of the page components so the escaping rules — which are a security
 * boundary, not just formatting — are testable on their own.
 */

/** Characters that make a spreadsheet treat a cell as a formula rather than text. */
const FORMULA_PREFIX = /^[=+\-@\t\r]/;

/** Characters that would break the row/field structure if left unquoted. */
const NEEDS_QUOTING = /["\n\r,]/;

/**
 * Make one value safe to write into a CSV cell.
 *
 * Two separate concerns:
 *
 *  1. **Formula injection (CWE-1236).** Cell values reach us from JobDiva and
 *     from recruiter free-text, so a value beginning `=`, `+`, `-`, `@`, tab
 *     or CR would be evaluated as a formula when the export is opened in
 *     Excel or Sheets — `=HYPERLINK("http://evil","click")` in a customer name
 *     becomes a live link in the recipient's spreadsheet. A leading apostrophe
 *     forces the cell to be read as text; spreadsheets strip it on display.
 *
 *  2. **Field integrity.** Quote anything containing a quote, comma, LF or CR.
 *     A bare CR alone is enough to split a row in some readers, so it is
 *     escaped even though it never appears in well-formed input.
 */
export function escapeCSV(value: string): string {
  const safe = FORMULA_PREFIX.test(value) ? `'${value}` : value;
  return NEEDS_QUOTING.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
}

/** Join a header row and body rows into CSV text, escaping every cell. */
export function toCsv(headers: string[], rows: string[][]): string {
  const line = (cells: string[]) => cells.map((cell) => escapeCSV(String(cell ?? ""))).join(",");
  return [line(headers), ...rows.map(line)].join("\n");
}

/**
 * Byte-order mark. Excel assumes the host's legacy codepage without it, which
 * turns the en-dashes and "—" placeholders in these reports into mojibake.
 */
export const UTF8_BOM = "﻿";
