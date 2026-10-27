// Typed API error class and error inspection utilities.
// Kept separate from api.ts so pure utilities and tests can import without
// pulling in MSAL or Next.js environment bindings.

export class ApiError extends Error {
  status: number;
  path: string;
  // Parsed JSON response body, when the backend returned one — lets a
  // catch site show the specific validation reason (e.g. the callback
  // 5-minute-lead message) via extractErrorMessage instead of this error's
  // own raw `${status} ${path}: ${rawBody}` message.
  data?: unknown;

  constructor(status: number, path: string, message?: string, data?: unknown) {
    super(message || `${status} ${path}`);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.data = data;
  }
}

export function isNotFoundError(err: unknown): boolean {
  if (err instanceof ApiError) {
    return err.status === 404;
  }
  if (err && typeof err === "object") {
    if ("status" in err && typeof (err as { status: unknown }).status === "number") {
      return (err as { status: number }).status === 404;
    }
    if ("message" in err && typeof (err as { message: unknown }).message === "string") {
      const msg = (err as { message: string }).message;
      return msg.includes("404") || msg.includes("Not Found");
    }
  }
  const msg = String(err || "");
  return msg.includes("404") || msg.includes("Not Found");
}

export const LIVE_REPORT_NOT_FOUND_MESSAGE = "Launch snapshot not found or no launches recorded yet.";
export const LIVE_REPORT_FORBIDDEN_MESSAGE = "You do not have permission to view this launch monitor.";
export const LIVE_REPORT_PROD_ONLY_MESSAGE = LIVE_REPORT_NOT_FOUND_MESSAGE;

export const MSAL_REDIRECT_COOLDOWN_MS = 15000;
export const MSAL_REDIRECT_STORAGE_KEY = "last_msal_login_redirect";

export function isWithinRedirectCooldown(
  now: number,
  storage: { getItem(key: string): string | null }
): boolean {
  try {
    const lastRedirect = Number(storage.getItem(MSAL_REDIRECT_STORAGE_KEY) || "0");
    return now - lastRedirect <= MSAL_REDIRECT_COOLDOWN_MS;
  } catch {
    // If storage is inaccessible, do not block redirect
    return false;
  }
}

export function recordRedirectTimestamp(
  now: number,
  storage: { setItem(key: string, value: string): void }
): void {
  try {
    storage.setItem(MSAL_REDIRECT_STORAGE_KEY, String(now));
  } catch {
    // Ignore storage write failures
  }
}

/**
 * Extracts a human-readable error message from a backend API error response body.
 * Handles FastAPI validation errors (array of loc/msg) and custom object details.
 */
export function extractErrorMessage(errorData: any, fallbackMessage: string): string {
  if (!errorData) return fallbackMessage;

  // Handle FastAPI validation error arrays
  if (Array.isArray(errorData.detail)) {
    return errorData.detail
      .map((d: any) => d.msg || d.message || (typeof d === "string" ? d : JSON.stringify(d)))
      .join(", ");
  }

  // Handle string detail
  if (typeof errorData.detail === "string") {
    return errorData.detail;
  }

  // Handle object detail (e.g. {"code": "...", "message": "..."})
  if (errorData.detail && typeof errorData.detail === "object") {
    if (typeof errorData.detail.message === "string") return errorData.detail.message;
    if (typeof errorData.detail.msg === "string") return errorData.detail.msg;
    // No known shape — fall back rather than surfacing raw JSON in a toast,
    // but log it so the actual shape isn't lost for debugging (PR #823 review).
    console.warn("extractErrorMessage: unrecognized object detail shape", errorData.detail);
    return fallbackMessage;
  }

  // Fallback properties on the root object
  if (typeof errorData.message === "string") return errorData.message;
  if (typeof errorData.msg === "string") return errorData.msg;

  if (typeof errorData === "string") return errorData;

  return fallbackMessage;
}
