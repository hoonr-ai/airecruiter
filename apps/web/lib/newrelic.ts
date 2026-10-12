/**
 * New Relic Browser helpers for PAIR web (apps/web).
 *
 * The agent is the copy/paste loader public/newrelic-browser-agent.js (one
 * mechanism; the npm BrowserAgent start path is gone). It picks the browser app
 * per host (lib/nr-config.ts), runs before hydration and exposes
 * window.newrelic. Every helper here is a fail-open no-op without it (unknown
 * host, local dev without `?nr=1`, ad blocker, SSR) and never throws.
 *
 *   setNrUser(email)       -> setUserId(sha256(lowercased email)[:12]); never the raw
 *                             email; no email -> setUserId(null) (signed out)
 *   setNrAttribute(k, v)   -> setCustomAttribute (on every later event)
 *   setNrAppVersion(v)     -> setApplicationVersion
 *   nrPageAction(name, a)  -> addPageAction  (PageAction events)
 *   nrNoticeError(err, a)  -> noticeError    (JS Errors; message scrubbed by toNrError)
 *   nrMeasure(name, opts)  -> measure        (BrowserPerformance events)
 *   logStep / captureException -> older names, kept for their call sites
 *
 * No PII to New Relic: attribute keys that look like a name, email, phone,
 * résumé, address or LinkedIn field are dropped at any depth (inside arrays
 * and objects too) unless the value is a flag or a small count
 * (`needsPhone: true`, `phones_found: 3`). String values are scrubbed too: a
 * string holding JSON is parsed and filtered by the same key rule; any other
 * string has its email addresses, formatted phone numbers and the values of
 * PII-keyed `"key": value` pairs (malformed JSON) redacted. Error messages
 * get the same treatment. Objects/arrays are JSON-stringified and capped at
 * 1000 characters.
 *
 * This module has no imports so lib/newrelic.test.ts can run it under node.
 */

type Attrs = Record<string, unknown>;
type NrValue = string | number | boolean;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type NrApi = Record<string, any>;

export const NR_MAX_VALUE_LENGTH = 1000;
// NR keeps at most 64 custom attributes per event; leave room for our own.
export const NR_MAX_ATTRIBUTES = 60;
const MAX_DEPTH = 6;

const PII_WORDS = "name|email|phone|mobile|resume|résumé|address|linkedin";
const PII_KEY = new RegExp(PII_WORDS, "i");
// Bounded parts (RFC: local ≤ 64, domain ≤ 253) keep this linear: an
// unbounded `[^…]+@` rescans the whole run from every start position.
const EMAIL_IN_TEXT = /[^\s@"'<>(),;:[\]{}]{1,64}@[^\s@"'<>(),;:[\]{}]{1,253}\.[a-z]{2,24}/gi;
// Formatted phone numbers only: "+<digits/separators>", "(555) 123-4567",
// "555-123-4567". A bare digit run is left alone: JobDiva candidate ids are
// 12-14 digit strings and must stay joinable.
const PHONE_IN_TEXT = /\+\d[\d\s().-]{7,}\d|\(\d{3}\)[\s.-]?\d{3}[\s.-]?\d{4}\b|\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b/g;
// `"<PII-looking key>": <string or number>` inside JSON text that did not
// parse (e.g. a truncated SSE event); a trailing unterminated string counts.
const PII_JSON_PAIR = new RegExp(
  String.raw`("[^"\\]*(?:${PII_WORDS})[^"\\]*"\s*:\s*)("(?:[^"\\]|\\.)*(?:"|$)|-?\d[\d.eE+-]*)`,
  "gi",
);

/** True for attribute keys that may carry personal data. */
export function isPiiKey(key: string): boolean {
  return PII_KEY.test(key);
}

// A boolean or a small integer can't hold a name, email or phone number (a
// phone needs 7+ digits), so `needsPhone: true` / `emails_found: 2` survive.
function isHarmless(value: unknown): boolean {
  return typeof value === "boolean" || (typeof value === "number" && Number.isInteger(value) && Math.abs(value) < 1_000_000);
}

/** True when `key: value` must not reach New Relic. */
function dropsAsPii(key: string, value: unknown): boolean {
  return isPiiKey(key) && !isHarmless(value);
}

// The redaction regexes only ever see this many characters of a string (a
// data: URL or base64 blob can be megabytes); every value is capped at
// NR_MAX_VALUE_LENGTH afterwards anyway.
const SCAN_LIMIT = 2 * NR_MAX_VALUE_LENGTH;
const TOKEN_DELIMITER = /[\s"'<>(),;:[\]{}]/;

/** `text` cut to SCAN_LIMIT, backed off to a delimiter so no half email or phone survives. */
function bounded(text: string): string {
  if (text.length <= SCAN_LIMIT) return text;
  let end = SCAN_LIMIT;
  while (end > SCAN_LIMIT / 2 && !TOKEN_DELIMITER.test(text[end])) end -= 1;
  if (end === SCAN_LIMIT / 2) end = SCAN_LIMIT;
  return `${text.slice(0, end)}…`;
}

function redact(text: string): string {
  let out = bounded(text).replace(PII_JSON_PAIR, (pair: string, prefix: string, value: string) =>
    /^-?\d+$/.test(value) && isHarmless(Number(value)) ? pair : `${prefix}"[redacted]"`,
  );
  if (out.includes("@")) out = out.replace(EMAIL_IN_TEXT, "[email]");
  return out.replace(PHONE_IN_TEXT, "[phone]");
}

/**
 * A string value as it may reach New Relic. JSON text (an object or array) is
 * parsed and filtered like any other attribute value, so a PII key inside it
 * is dropped; anything else (including JSON that does not parse) goes through
 * redact().
 */
function scrubText(text: string, depth: number, ctx: ScrubContext = newScrubContext()): string {
  const t = text.trim();
  const looksJson = (t.startsWith("{") && t.endsWith("}")) || (t.startsWith("[") && t.endsWith("]"));
  if (looksJson && depth < MAX_DEPTH) {
    try {
      const json = JSON.stringify(scrub(JSON.parse(t), depth + 1, ctx));
      if (json !== undefined) return json;
    } catch {
      // not JSON after all
    }
  }
  return redact(text);
}

function cap(text: string): string {
  return text.length > NR_MAX_VALUE_LENGTH ? `${text.slice(0, NR_MAX_VALUE_LENGTH - 1)}…` : text;
}

// One value's scrub: objects already visited (cycles), and how many more
// string characters it may scan. The value ends up capped at
// NR_MAX_VALUE_LENGTH, so strings past the budget become "…" unscanned, which
// keeps a huge object (a candidate list in a log context) cheap.
type ScrubContext = { seen: WeakSet<object>; budget: number };
const SCRUB_BUDGET = 16 * NR_MAX_VALUE_LENGTH;

function newScrubContext(): ScrubContext {
  return { seen: new WeakSet(), budget: SCRUB_BUDGET };
}

/** Deep copy with PII keys removed; Errors/Dates flattened; cycles cut. */
function scrub(value: unknown, depth: number, ctx: ScrubContext): unknown {
  if (value === null || typeof value !== "object") {
    if (typeof value === "string") {
      if (ctx.budget <= 0) return "…";
      ctx.budget -= Math.min(value.length, SCAN_LIMIT);
      return scrubText(value, depth, ctx);
    }
    if (typeof value === "bigint") return String(value);
    if (typeof value === "function" || typeof value === "symbol") return undefined;
    return value;
  }
  if (value instanceof Error) return redact(`${value.name}: ${value.message}`);
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? undefined : value.toISOString();
  if (ctx.seen.has(value)) return "[circular]";
  if (depth >= MAX_DEPTH) return "[depth]";
  ctx.seen.add(value);
  if (Array.isArray(value)) return value.map((item) => scrub(item, depth + 1, ctx));
  const out: Attrs = {};
  for (const [key, item] of Object.entries(value as Attrs)) {
    if (dropsAsPii(key, item)) continue;
    const clean = scrub(item, depth + 1, ctx);
    if (clean !== undefined) out[key] = clean;
  }
  return out;
}

/** One attribute value as NR accepts it, or undefined to drop it. */
export function toNrValue(value: unknown): NrValue | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "string") return cap(scrubText(value, 0));
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
  if (typeof value === "boolean") return value;
  if (typeof value === "bigint") return String(value);
  if (typeof value !== "object") return undefined;
  const clean = scrub(value, 0, newScrubContext());
  if (clean === undefined) return undefined;
  if (typeof clean === "string") return cap(clean);
  try {
    const json = JSON.stringify(clean);
    return json === undefined ? undefined : cap(json);
  } catch {
    return undefined;
  }
}

/**
 * Flat, PII-filtered NR attributes: PII keys dropped, values converted by
 * toNrValue, at most NR_MAX_ATTRIBUTES keys.
 */
export function toNrAttributes(attrs?: Attrs | null): Record<string, NrValue> {
  const out: Record<string, NrValue> = {};
  if (!attrs || typeof attrs !== "object") return out;
  let count = 0;
  for (const [key, value] of Object.entries(attrs)) {
    if (count >= NR_MAX_ATTRIBUTES) break;
    if (dropsAsPii(key, value)) continue;
    const v = toNrValue(value);
    if (v === undefined) continue;
    out[key] = v;
    count += 1;
  }
  return out;
}

/**
 * The NR user id for an email: first 12 hex chars of sha256(trimmed,
 * lowercased email) — in Python `sha256(e.strip().lower().encode()).hexdigest()[:12]`,
 * so API-side telemetry can use the same pseudonym. null when there is no
 * email or no WebCrypto (insecure context); never falls back to the email.
 */
export async function nrUserIdFor(email: string | null | undefined): Promise<string | null> {
  const normalized = (email ?? "").trim().toLowerCase();
  if (!normalized) return null;
  try {
    const subtle = globalThis.crypto?.subtle;
    if (!subtle || typeof TextEncoder === "undefined") return null;
    const digest = await subtle.digest("SHA-256", new TextEncoder().encode(normalized));
    return Array.from(new Uint8Array(digest).slice(0, 6), (b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    return null;
  }
}

function getNr(): NrApi | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const nr = (window as any).newrelic;
  return nr && typeof nr === "object" ? (nr as NrApi) : null;
}

function call(method: string, ...args: unknown[]): void {
  const nr = getNr();
  if (!nr || typeof nr[method] !== "function") return;
  try {
    nr[method](...args);
  } catch (err) {
    console.debug(`[newrelic] ${method} failed:`, err);
  }
}

/**
 * Identify the signed-in recruiter by a hash of their email (async,
 * fire-and-forget). No email clears it (setUserId(null)): the agent keeps the
 * id in its session, so after sign-out it would otherwise keep attributing
 * events to the previous recruiter.
 */
export async function setNrUser(email: string | null | undefined): Promise<void> {
  if (!getNr()) return;
  call("setUserId", await nrUserIdFor(email));
}

/** Custom attribute on every later event of this page (PII keys refused). */
export function setNrAttribute(key: string, value: string | number | boolean | null): void {
  if (!key || (value !== null && dropsAsPii(key, value))) return;
  const v = value === null ? null : toNrValue(value);
  if (v === undefined) return;
  call("setCustomAttribute", key, v);
}

export function setNrAppVersion(version: string | null | undefined): void {
  if (!version) return;
  call("setApplicationVersion", String(version));
}

/** A PageAction named `name` with PII-filtered attributes. */
export function nrPageAction(name: string, attrs?: Attrs | null): void {
  if (!name) return;
  call("addPageAction", name, toNrAttributes(attrs));
}

/**
 * The Error to hand the agent: message scrubbed like a string attribute
 * (emails, phones, PII-keyed JSON), and the same text replaced in the stack.
 * The caller's Error is passed through untouched when there is nothing to
 * scrub, so its stack frames reach New Relic as thrown.
 */
export function toNrError(error: unknown): Error {
  const raw =
    error instanceof Error
      ? error
      : new Error(typeof error === "string" ? error : toNrValue(error)?.toString() ?? String(error));
  const message = cap(scrubText(raw.message, 0));
  if (message === raw.message) return raw;
  const err = new Error(message);
  err.name = raw.name;
  // Only the message text is replaced in the stack: redacting whole frames
  // could rewrite a chunk URL and break source-map resolution.
  if (typeof raw.stack === "string") err.stack = raw.stack.split(raw.message).join(message);
  return err;
}

/** Report a handled error (uncaught ones are captured by the agent itself). */
export function nrNoticeError(error: unknown, attrs?: Attrs | null): void {
  if (!getNr()) return;
  try {
    call("noticeError", toNrError(error), toNrAttributes(attrs));
  } catch {
    // an exotic error object (throwing getters): telemetry is best-effort
  }
}

/**
 * A BrowserPerformance measure. `start`/`end` are performance.now()-style
 * timestamps (ms since page load); `end` defaults to now, `start` to page load.
 * Use it for spans the agent cannot see, e.g. a streaming response's full body.
 */
export function nrMeasure(
  name: string,
  options?: { start?: number; end?: number; attrs?: Attrs | null },
): void {
  if (!name) return;
  call("measure", name, {
    start: options?.start,
    end: options?.end,
    customAttributes: toNrAttributes(options?.attrs),
  });
}

/**
 * Record a named UI step as a PageAction (status + category + details).
 * `status` and `category` are always the arguments (a `status`/`category` in
 * details is ignored), so `WHERE status = 'failed'` stays reliable.
 * A "failed" status is also reported as an error.
 */
export const logStep = (
  stepName: string,
  status: string,
  details?: Record<string, unknown>,
  category = "frontend_step"
): void => {
  const message = `Step '${stepName}': ${status}`;

  if (process.env.NODE_ENV === "development") {
    console.log(`[${category}] ${message}`, details ?? "");
  }

  if (status.toLowerCase() === "failed") {
    console.warn(`[newrelic] ${message}`, details ?? "");
  }

  if (!getNr()) return;
  const rest: Attrs = { ...(details ?? {}) };
  delete rest.status;
  delete rest.category;
  const attrs = toNrAttributes({ status, category, ...rest });
  call("addPageAction", stepName, attrs);
  if (status.toLowerCase() === "failed") {
    call("noticeError", toNrError(new Error(`Step '${stepName}' failed`)), attrs);
  }
};

/** Report a frontend exception to New Relic Browser JS Errors. */
export const captureException = (
  error: unknown,
  context?: Record<string, unknown>
): void => {
  nrNoticeError(error, context);
};
