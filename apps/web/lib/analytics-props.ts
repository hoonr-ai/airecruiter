// Pure helpers behind lib/analytics.ts (no imports, so lib/analytics-props.test.ts
// can run them under node).
//
//   sanitizeEventProps  event properties as Amplitude accepts them.
//   shouldBridgeToNr    which trackEvent() events are also sent to New Relic
//                       as PageActions (lib/newrelic.ts filters their PII).

type EventProps = Record<string, unknown>;
type EventValue = string | number | boolean;

export const MAX_EVENT_JSON_LENGTH = 1000;

function capJson(text: string): string {
  return text.length > MAX_EVENT_JSON_LENGTH ? `${text.slice(0, MAX_EVENT_JSON_LENGTH - 1)}…` : text;
}

function isPrimitive(value: unknown): boolean {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean";
}

/**
 * One property value for Amplitude. Primitives pass through; an array of
 * primitives keeps its old comma-joined form (existing charts depend on it);
 * Dates become ISO strings; any other object is JSON (≤ 1000 chars) instead of
 * the "[object Object]" String() used to produce.
 */
export function sanitizeEventValue(value: unknown): EventValue | undefined {
  if (value === null || value === undefined) return undefined;
  if (isPrimitive(value)) return value as EventValue;
  if (typeof value === "bigint") return String(value);
  if (typeof value === "function" || typeof value === "symbol") return undefined;
  if (Array.isArray(value) && value.every((item) => item === null || item === undefined || isPrimitive(item))) {
    return String(value);
  }
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? String(value) : value.toISOString();
  if (value instanceof Error) return String(value);
  try {
    const json = JSON.stringify(value);
    if (json !== undefined) return capJson(json);
  } catch {
    // circular or otherwise unserialisable: fall through
  }
  return String(value);
}

export function sanitizeEventProps(props?: EventProps | null): Record<string, EventValue> {
  const out: Record<string, EventValue> = {};
  if (!props) return out;
  for (const [key, value] of Object.entries(props)) {
    const v = sanitizeEventValue(value);
    if (v !== undefined) out[key] = v;
  }
  return out;
}

// Product events worth having next to the APM traces. Deliberately NOT the
// high-volume ones (ui_click, page_view, frontend_log, api_request_*): the
// agent already records clicks, page views and AJAX on its own.
export const NR_BRIDGE_PREFIXES = ["job_wizard_step5_", "launch_", "pair_launch_"] as const;
export const NR_BRIDGE_EVENTS: ReadonlySet<string> = new Set(["job_wizard_step_completed"]);
// Prefix matches that fire per keystroke: the on-blur
// job_wizard_step5_boolean_edit_saved already records the final edit.
export const NR_BRIDGE_EXCLUDE: ReadonlySet<string> = new Set(["job_wizard_step5_boolean_edited"]);
// Bulky before/after snapshots of the wizard form (recruiter-entered text).
export const NR_BRIDGE_DROP_KEYS: ReadonlySet<string> = new Set(["changes"]);

export function shouldBridgeToNr(eventType: string): boolean {
  if (!eventType || NR_BRIDGE_EXCLUDE.has(eventType)) return false;
  if (NR_BRIDGE_EVENTS.has(eventType)) return true;
  return NR_BRIDGE_PREFIXES.some((prefix) => eventType.startsWith(prefix));
}

/** The props to send with a bridged event (PII filtering is lib/newrelic.ts's job). */
export function nrBridgeProps(props?: EventProps | null): EventProps {
  const out: EventProps = {};
  if (!props) return out;
  for (const [key, value] of Object.entries(props)) {
    if (!NR_BRIDGE_DROP_KEYS.has(key)) out[key] = value;
  }
  return out;
}
