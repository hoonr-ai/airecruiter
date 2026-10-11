// Lightweight client-side logger. Goals:
//   1. Replace scattered console.{log,warn,error} with a level-gated API.
//   2. One central emit() that fans out to every sink without touching call
//      sites.
//   3. Keep the bundle cost ~zero (no deps, no runtime config fetch).
//
// Sinks (remoteSink below):
//   - Amplitude: every record at or above the threshold (`frontend_log`, plus
//     `frontend_error_log` for errors).
//   - New Relic: warn/error only, as a `FrontendLog` PageAction
//     (status = level, a context `status` becomes http_status; PII-filtered
//     by lib/newrelic.ts); errors are also reported through noticeError.
//     debug/info stay out of New Relic (ingest budget; browser console
//     capture is off in the loader).

import { trackEvent } from "@/lib/analytics";
import { captureException, logStep } from "@/lib/newrelic";

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

// Fans a record out to Amplitude and (warn/error only) New Relic. Never
// throws: a broken telemetry sink must not break the code that logged.
function remoteSink(_record: unknown) {
  const record = (_record ?? {}) as Record<string, unknown>;
  try {
    trackEvent("frontend_log", record);

    if (record.level === "warn" || record.level === "error") {
      // On a FrontendLog, `status` is the level; a caller's own `status` (an
      // HTTP code, e.g. `{ status: res.status }`) is kept as http_status.
      const { status: httpStatus, ...rest } = record;
      const nrAttrs = httpStatus === undefined ? rest : { ...rest, http_status: httpStatus };
      logStep("FrontendLog", String(record.level), nrAttrs, "frontend_logger");
      if (record.level === "error") {
        captureException(new Error(String(record.msg ?? "Frontend error")), nrAttrs);
      }
    }

    if (record.level === "error") {
      trackEvent("frontend_error_log", record);
    }
  } catch {
    // telemetry is best-effort
  }
}

export const logger = {
  debug: (msg: string, ctx?: Context) => emit("debug", msg, ctx),
  info:  (msg: string, ctx?: Context) => emit("info",  msg, ctx),
  warn:  (msg: string, ctx?: Context) => emit("warn",  msg, ctx),
  error: (msg: string, ctx?: Context) => emit("error", msg, ctx),
};
