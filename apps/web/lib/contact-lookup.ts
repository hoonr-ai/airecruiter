// Launch PAIR's contact lookup on Step 5: how candidates are grouped per call
// and how the answers are summed up for the recruiter. The lookups run
// server-side (/candidates/enrich-contacts: Apollo → Exa → Exa deep search,
// cheapest first); each answer names who found the phone / the email. Also
// how a click (phone button, Rankings Get Contact) reads a final miss.

const PROVIDER_LABELS: Record<string, string> = {
  kipplo: "Kipplo",
  zoominfo: "ZoomInfo",
  apollo: "Apollo",
  exa: "Exa",
  exa_deep: "Exa deep search",
  cache: "saved",
};

export function contactProviderLabel(provider: string | null | undefined): string {
  const key = String(provider || "").trim();
  return PROVIDER_LABELS[key] || key;
}

/** `items` in groups of `size`; the last group may be shorter. */
export function inGroups<T>(items: T[], size: number): T[][] {
  const step = Math.max(1, Math.floor(size));
  const groups: T[][] = [];
  for (let i = 0; i < items.length; i += step) groups.push(items.slice(i, i + step));
  return groups;
}

/**
 * Whether an answer's `lookup.kipplo` is a problem (no key on the server, out
 * of credits, rate limited...) rather than a result: "-" = not asked, "miss" =
 * Kipplo has no record, "phone" / "email" / "email,phone" = what it found.
 */
export function isKipploIssue(outcome: string | null | undefined): boolean {
  const value = String(outcome || "").trim();
  if (!value || value === "-" || value === "miss") return false;
  return !value.split(",").every((f) => f === "email" || f === "phone");
}

function describeCounts(counts: Record<string, number>, noun: string): string {
  const entries = Object.entries(counts).filter(([, n]) => n > 0);
  const total = entries.reduce((sum, [, n]) => sum + n, 0);
  if (total === 0) return "";
  const by = entries
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([provider, n]) => `${contactProviderLabel(provider)} ${n}`)
    .join(", ");
  return `${total} ${noun}${total === 1 ? "" : "s"} (${by})`;
}

/** "3 phones (Kipplo 2, Exa 1) and 1 email (Exa 1)"; "" when nothing was found. */
export function describeContactsFound(
  phoneFoundBy: Record<string, number>,
  emailFoundBy: Record<string, number>,
): string {
  return [describeCounts(phoneFoundBy, "phone"), describeCounts(emailFoundBy, "email")]
    .filter(Boolean)
    .join(" and ");
}

/** The most frequent entry of `counts` (ties: alphabetical), or null. */
export function mostCommon(counts: Record<string, number>): { key: string; count: number } | null {
  const top = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];
  return top ? { key: top[0], count: top[1] } : null;
}

/**
 * Read a newline-delimited JSON stream (the grouped lookup's answer with
 * Accept: application/x-ndjson), calling onLine for each parsed line as it
 * arrives. Blank or unparseable lines are skipped. Rejects if the stream
 * breaks; whatever was delivered before that has already been handed over.
 */
export async function readNdjson(
  body: ReadableStream<Uint8Array>,
  onLine: (value: unknown) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const emit = (line: string) => {
    const text = line.trim();
    if (!text) return;
    let value: unknown;
    try {
      value = JSON.parse(text);
    } catch {
      return;
    }
    onLine(value);
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    lines.forEach(emit);
  }
  buffer += decoder.decode();
  emit(buffer);
}


// ── Final misses (the person's contact is never bought twice) ───────────────

export interface FinalMissNotice {
  /** The fields a recent search found nothing for ("email" / "phone"). */
  fields: string[];
  /** When they were searched (ISO 8601), or null if the answer did not say. */
  checkedAt: string | null;
  /** About what "Search again" costs, in USD, or null. */
  estUsd: number | null;
  /** "Searched Oct 1, 2026, nothing found". */
  message: string;
  /** "Search again (~$0.23)": the button that re-sends with confirm: true. */
  searchAgainLabel: string;
}

/** "Oct 1, 2026" for an ISO timestamp; "" when it does not parse. */
export function formatCheckedDate(iso: string | null | undefined, timeZone?: string): string {
  const text = String(iso || "").trim();
  if (!text) return "";
  const at = new Date(text);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    ...(timeZone ? { timeZone } : {}),
  });
}

/**
 * What a lookup answer says about fields it did not buy because a recent
 * search (usually the deep search) already found nothing for them
 * (`final_missed_fields`, `checked_at`, `est_usd` from /candidates/enrich-contact),
 * limited to `wanted` when given. null when there are none.
 */
export function finalMissNotice(
  answer: unknown,
  wanted?: string[] | null,
  timeZone?: string,
): FinalMissNotice | null {
  const a = (answer && typeof answer === "object" ? answer : {}) as Record<string, unknown>;
  const raw = Array.isArray(a.final_missed_fields) ? a.final_missed_fields : [];
  const fields = raw
    .map((f: unknown) => String(f || "").trim())
    .filter((f: string) => f && (!wanted || wanted.length === 0 || wanted.includes(f)));
  if (fields.length === 0) return null;
  const checkedAt = typeof a.checked_at === "string" && a.checked_at ? a.checked_at : null;
  const est = Number(a.est_usd);
  const estUsd = a.est_usd != null && Number.isFinite(est) && est > 0 ? est : null;
  const date = formatCheckedDate(checkedAt, timeZone);
  return {
    fields,
    checkedAt,
    estUsd,
    message: date ? `Searched ${date}, nothing found` : "Searched recently, nothing found",
    searchAgainLabel: estUsd != null ? `Search again (~$${estUsd.toFixed(2)})` : "Search again",
  };
}

/**
 * Whether the answer is `status: "in_progress"`: a click got none of what it
 * asked for because an Exa run for it (`in_progress_fields`) was still going
 * when the server stopped waiting. The run is watched in the background and
 * its result lands in the contact cache, so look again in a few minutes
 * rather than re-clicking, which would buy a second run. (Once lookups of one
 * person are single-flight across workers, also sent when another lookup of
 * the same person is already buying it.) Never sent to Launch PAIR.
 */
export function inProgress(answer: unknown): boolean {
  const status = answer && typeof answer === "object" ? (answer as { status?: unknown }).status : "";
  return String(status || "") === "in_progress";
}

/** The toast for an `in_progress` answer: never "No contact found", which invites a paid re-click. */
export const STILL_LOOKING_UP = "Still being looked up — check again in a few minutes";

/**
 * What a click shows for an `in_progress` answer, naming the fields still
 * being looked up when the answer says ("Phone still being looked up — check
 * again in a few minutes"); null for any other answer.
 */
export function stillLookingUpMessage(answer: unknown): string | null {
  if (!inProgress(answer)) return null;
  const raw = (answer as { in_progress_fields?: unknown }).in_progress_fields;
  const fields = (Array.isArray(raw) ? raw : [])
    .map((f: unknown) => String(f || "").trim())
    .filter((f: string) => f === "email" || f === "phone");
  if (fields.length === 0) return STILL_LOOKING_UP;
  const names = fields.join(" and ");
  return `${names.charAt(0).toUpperCase()}${names.slice(1)} still being looked up — check again in a few minutes`;
}

/** Rankings Get Contact: the last answer's status line for one candidate. */
export interface ContactClickStatus {
  message: string;
  /** "Search again (~$X)": a final miss (finalMissNotice). */
  searchAgain?: string;
  /** The lookup is still going (stillLookingUpMessage). */
  inProgress?: boolean;
}

/**
 * The Get Contact button's hover for a status: a final miss and a lookup
 * still going keep their whole message (its date, "check again in a few
 * minutes"); anything else is shortened ("No contact found", "Contact info
 * applied", "Contact lookup failed").
 */
export function contactStatusHover(status: ContactClickStatus): string {
  const raw = String(status.message || "").trim();
  if (!raw || status.searchAgain || status.inProgress) return raw;
  const lower = raw.toLowerCase();
  if (lower.includes("no contact info found") || lower.includes("no contact match")) return "No contact found";
  if (lower.includes("linkedin url missing")) return "LinkedIn URL missing";
  if (lower.includes("applied")) return "Contact info applied";
  if (lower.includes("failed")) return "Contact lookup failed";
  return raw;
}
