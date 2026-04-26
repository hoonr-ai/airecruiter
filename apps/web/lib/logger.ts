// Lightweight client-side logger. Goals:
//   1. Replace scattered console.{log,warn,error} with a level-gated API.
//   2. Give us one central emit() we can redirect later — e.g. POST batched
//      records to `/api/v1/logs/client` for New Relic / Datadog ingestion —
//      without touching every call site.
//   3. Keep the bundle cost ~zero (no deps, no runtime config fetch).
//
// Production wiring is deliberately a stub today; filling in `remoteSink`
// should be all that's needed to go live.

import { trackEvent } from "@/lib/analytics";

export type LogLevel = "debug" | "info" | "warn" | "error";

const LEVEL_ORDER: Record<LogLevel, number> = { debug: 0, info: 1, warn: 2, error: 3 };

// Threshold read once at module load. Changing it needs a page reload — an
// acceptable trade-off for the simplicity of no runtime state.
const threshold: LogLevel = ((process.env.NEXT_PUBLIC_LOG_LEVEL ?? "info") as LogLevel);

type Context = Record<string, unknown>;

function emit(level: LogLevel, msg: string, ctx?: Context) {
  if (LEVEL_ORDER[level] < LEVEL_ORDER[threshold]) return;
  const record = {
    ts: new Date().toISOString(),
    level,
    msg,
    ...(ctx ?? {}),
  };
  // Mirror to console so DevTools retains its full UX (stack traces, object
  // inspection). `debug` maps to `log` so browsers don't hide it behind a
  // separate filter by default.
  const consoleFn =
    level === "debug" ? console.log :
    level === "info"  ? console.info :
    level === "warn"  ? console.warn :
                         console.error;
  
  // Log message and context separately for better browser inspection and to
  // prevent some Next.js overlays from showing a generic {} for the record.
  consoleFn(`[${level.toUpperCase()}] ${msg}`, ctx ?? {});
  
  remoteSink(record);
}

// Remote sink stub. Later: batch records and POST to a backend ingest
// endpoint (e.g. `/api/v1/logs/client`) on a debounce/flush schedule.
// Deliberately a no-op for now — keeps this file free of fetch timing
// concerns and failure modes.
function remoteSink(_record: unknown) {
  const record = (_record ?? {}) as Record<string, unknown>;
  trackEvent("frontend_log", record);
  if (record.level === "error") {
    trackEvent("frontend_error_log", record);
  }
}

export const logger = {
  debug: (msg: string, ctx?: Context) => emit("debug", msg, ctx),
  info:  (msg: string, ctx?: Context) => emit("info",  msg, ctx),
  warn:  (msg: string, ctx?: Context) => emit("warn",  msg, ctx),
  error: (msg: string, ctx?: Context) => emit("error", msg, ctx),
};
