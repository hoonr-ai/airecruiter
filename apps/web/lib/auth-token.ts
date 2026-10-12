// Pure rules behind lib/api.ts's token handling (no imports, so
// lib/auth-token.test.ts can run them under node).
//
// Why the ID token's own expiry matters: the API authenticates every call with
// the Microsoft ID token (core/auth.verify_azure_token rejects an expired one
// with a 401). MSAL's acquireTokenSilent only checks the ACCESS token's
// expiry, so after a long break it can hand back a cached ID token that has
// already expired. Every call then 401s: /auth/me fails, the role falls back
// to "recruiter", and a late loginRedirect reloads the whole page. Asking for
// a forced refresh when the ID token is (nearly) expired gets a new one from
// the refresh token instead, with no redirect.

/** Renew an ID token this close to its expiry rather than send it. Covers
 *  clock drift between the browser and the API, and a slow request. */
export const ID_TOKEN_RENEW_SKEW_S = 5 * 60;

type MaybeClaims = { idTokenClaims?: unknown } | null | undefined;

/** The ID token's `exp` (seconds since the epoch), or null when unknown. */
export function idTokenExpiresAt(result: MaybeClaims): number | null {
  const claims = result?.idTokenClaims as { exp?: unknown } | undefined;
  const exp = Number(claims?.exp);
  return Number.isFinite(exp) && exp > 0 ? exp : null;
}

/**
 * True when the ID token in `result` must be renewed before use: missing,
 * expired, or within ID_TOKEN_RENEW_SKEW_S of expiring. An unknown expiry
 * counts as fresh: the API is the judge then, as before.
 */
export function needsFreshIdToken(
  result: ({ idToken?: string | null } & { idTokenClaims?: unknown }) | null | undefined,
  nowMs: number,
  skewS: number = ID_TOKEN_RENEW_SKEW_S,
): boolean {
  if (!result?.idToken) return true;
  const exp = idTokenExpiresAt(result);
  if (exp === null) return false;
  return exp - skewS <= nowMs / 1000;
}

/** How long a token from `result` can be reused without asking MSAL again (ms). */
export function reusableForMs(result: MaybeClaims, nowMs: number, skewS: number = ID_TOKEN_RENEW_SKEW_S): number {
  const exp = idTokenExpiresAt(result);
  if (exp === null) return 0;
  return Math.max(0, (exp - skewS) * 1000 - nowMs);
}

// MSAL's interaction-required codes (InteractionRequiredAuthErrorCodes and
// the server's), plus the hidden-iframe renewal timing out — what happens
// when the refresh token has expired and the browser blocks the iframe's
// cookies. In each case only a sign-in redirect gets a new token.
const NEEDS_SIGN_IN_CODES = [
  "interaction_required",
  "login_required",
  "consent_required",
  "no_tokens_found",
  "refresh_token_expired",
  "bad_token",
  "native_account_unavailable",
  "monitor_window_timeout",
  "aadsts160021",
  "aadsts700084",
];

/** True when a token error can only be fixed by signing in again. */
export function needsSignIn(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const e = err as { name?: unknown; errorCode?: unknown; subError?: unknown; message?: unknown };
  if (e.name === "InteractionRequiredAuthError") return true;
  const text = [e.errorCode, e.subError, e.message]
    .filter((part) => typeof part === "string")
    .join(" ")
    .toLowerCase();
  return NEEDS_SIGN_IN_CODES.some((code) => text.includes(code));
}

// ---- The API token source -------------------------------------------------
// lib/api.ts wires MSAL into this; the rules live here so
// lib/auth-token.test.ts can drive them with a fake MSAL.

export type TokenOutcome = { token: string | null; signInRequired: boolean };

type SilentResult = { idToken?: string | null; accessToken?: string | null; idTokenClaims?: unknown } | null | undefined;

export type ApiTokenSourceDeps<A extends { homeAccountId: string; idToken?: string }> = {
  /** The signed-in account, or null (signed out, MSAL not ready). Must not throw. */
  currentAccount: () => A | null;
  /** MSAL acquireTokenSilent for the API's scopes. */
  acquireSilent: (account: A, forceRefresh: boolean) => Promise<SilentResult>;
  now?: () => number;
};

export type ApiTokenSource = {
  /** The token for the API, or why there is none. Never throws. */
  acquire(): Promise<TokenOutcome>;
  /** Drop the reusable token (a 401, a sign-in redirect). */
  forget(): void;
};

/**
 * One lookup serves every request that starts while it runs, and a token is
 * reused until ID_TOKEN_RENEW_SKEW_S before its expiry, so a page firing
 * twenty calls on load asks MSAL once. A cached ID token that has (nearly)
 * expired is renewed with a forced refresh rather than sent.
 */
export function createApiTokenSource<A extends { homeAccountId: string; idToken?: string }>(
  deps: ApiTokenSourceDeps<A>,
): ApiTokenSource {
  const now = deps.now ?? Date.now;
  let memo: { accountId: string; token: string; reuseUntil: number } | null = null;
  let inflight: Promise<TokenOutcome> | null = null;

  const lookUp = async (account: A): Promise<TokenOutcome> => {
    try {
      let result = await deps.acquireSilent(account, false);
      if (needsFreshIdToken(result, now())) result = await deps.acquireSilent(account, true);
      const token = result?.idToken || result?.accessToken || account.idToken || null;
      memo = token && result?.idToken
        ? { accountId: account.homeAccountId, token, reuseUntil: now() + reusableForMs(result, now()) }
        : null;
      return { token, signInRequired: false };
    } catch (err) {
      memo = null;
      return { token: null, signInRequired: needsSignIn(err) };
    }
  };

  return {
    acquire() {
      const account = deps.currentAccount();
      if (!account) return Promise.resolve({ token: null, signInRequired: false });
      if (memo && memo.accountId === account.homeAccountId && now() < memo.reuseUntil) {
        return Promise.resolve({ token: memo.token, signInRequired: false });
      }
      if (!inflight) {
        inflight = lookUp(account).finally(() => {
          inflight = null;
        });
      }
      return inflight;
    },
    forget() {
      memo = null;
    },
  };
}
