// Typed API error class and error inspection utilities.
// Kept separate from api.ts so pure utilities and tests can import without
// pulling in MSAL or Next.js environment bindings.

export class ApiError extends Error {
  status: number;
  path: string;

  constructor(status: number, path: string, message?: string) {
    super(message || `${status} ${path}`);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
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

export const LIVE_REPORT_PROD_ONLY_MESSAGE = "Live Launch Monitor is available in Production only.";

export const MSAL_REDIRECT_COOLDOWN_MS = 15000;
export const MSAL_REDIRECT_STORAGE_KEY = "last_msal_login_redirect";

export function isWithinRedirectCooldown(
  now: number,
  storage: { getItem(key: string): string | null; setItem(key: string, value: string): void }
): boolean {
  try {
    const lastRedirect = Number(storage.getItem(MSAL_REDIRECT_STORAGE_KEY) || "0");
    if (now - lastRedirect <= MSAL_REDIRECT_COOLDOWN_MS) {
      return true;
    }
    storage.setItem(MSAL_REDIRECT_STORAGE_KEY, String(now));
    return false;
  } catch {
    // If storage is inaccessible, do not crash or block redirect
    return false;
  }
}
