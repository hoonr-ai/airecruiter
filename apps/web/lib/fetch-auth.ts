// The global window.fetch patch behind lib/api.ts: every call to the PAIR API
// gets the auth headers (Authorization + X-User-Email), however it was made.
// Pure (no imports) so lib/fetch-auth.test.ts can drive it with a fake fetch.
//
// Headers-aware on purpose. Callers, and the New Relic agent's own fetch
// wrapper (it rebuilds `init.headers` as a Headers object to add trace
// headers), may hand us a Headers instance or a [name, value][] list. The old
// `{...auth, ...(init.headers as Record)}` spread copied nothing from those,
// so Content-Type was silently dropped and FastAPI answered 422.

export const AUTH_HEADER_NAMES = ["Authorization", "X-User-Email"] as const;

export type AuthHeadersProvider = () => Promise<Record<string, string>>;

export function requestUrl(input: RequestInfo | URL): string | undefined {
  if (typeof input === "string") return input;
  if (typeof URL !== "undefined" && input instanceof URL) return input.toString();
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const url = (input as any)?.url;
  return typeof url === "string" ? url : undefined;
}

function isRequest(input: unknown): input is Request {
  return typeof Request !== "undefined" && input instanceof Request;
}

/**
 * `init` with the auth headers added where absent. Headers the caller (or an
 * earlier wrapper such as authFetch) already set always win; nothing is
 * fetched when both auth headers are present. Returns `init` itself when
 * nothing changes.
 */
export async function withAuthHeaders(
  input: RequestInfo | URL,
  init: RequestInit,
  getAuthHeaders: AuthHeadersProvider,
): Promise<RequestInit> {
  // fetch(request, {headers}) replaces the Request's headers, so start from
  // them when the caller passed a Request without init.headers.
  const base = init.headers ?? (isRequest(input) ? input.headers : undefined);
  const headers = new Headers(base);
  if (AUTH_HEADER_NAMES.every((name) => headers.has(name))) return init;

  const auth = await getAuthHeaders();
  let changed = false;
  for (const [name, value] of Object.entries(auth)) {
    if (value && !headers.has(name)) {
      headers.set(name, value);
      changed = true;
    }
  }
  return changed ? { ...init, headers } : init;
}

/**
 * A fetch that adds the auth headers to calls whose URL satisfies `isApiUrl`
 * and passes everything else through untouched. If the headers can't be
 * obtained the request still goes out as the caller built it (the API
 * answers 401, which the app already handles).
 */
export function createAuthFetch(
  originalFetch: typeof fetch,
  isApiUrl: (url: string) => boolean,
  getAuthHeaders: AuthHeadersProvider,
): typeof fetch {
  return async function authHeadersFetch(input: RequestInfo | URL, init: RequestInit = {}) {
    const url = requestUrl(input);
    if (url && isApiUrl(url)) {
      try {
        init = await withAuthHeaders(input, init, getAuthHeaders);
      } catch {
        // fall through with the caller's init
      }
    }
    return originalFetch(input, init);
  };
}
