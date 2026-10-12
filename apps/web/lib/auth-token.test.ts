import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  ID_TOKEN_RENEW_SKEW_S,
  createApiTokenSource,
  idTokenExpiresAt,
  needsFreshIdToken,
  needsSignIn,
  reusableForMs,
} from "./auth-token.ts";

const NOW_MS = Date.UTC(2026, 9, 12, 9, 0, 0);
const nowS = NOW_MS / 1000;
const token = (exp?: number) => ({ idToken: "header.payload.sig", idTokenClaims: exp === undefined ? {} : { exp } });

describe("needsFreshIdToken", () => {
  it("renews an expired ID token even when MSAL served it from cache", () => {
    // The overnight case: access token still cached, ID token long expired.
    assert.equal(needsFreshIdToken(token(nowS - 8 * 3600), NOW_MS), true);
  });

  it("renews one that expires within the skew", () => {
    assert.equal(needsFreshIdToken(token(nowS + ID_TOKEN_RENEW_SKEW_S - 1), NOW_MS), true);
  });

  it("keeps a token with time left", () => {
    assert.equal(needsFreshIdToken(token(nowS + 3600), NOW_MS), false);
  });

  it("renews when there is no ID token at all", () => {
    assert.equal(needsFreshIdToken({ idToken: "" }, NOW_MS), true);
    assert.equal(needsFreshIdToken(null, NOW_MS), true);
  });

  it("leaves an unknown expiry to the API", () => {
    assert.equal(needsFreshIdToken(token(), NOW_MS), false);
    assert.equal(idTokenExpiresAt(token()), null);
    assert.equal(idTokenExpiresAt({ idTokenClaims: { exp: "soon" } }), null);
  });
});

describe("reusableForMs", () => {
  it("is the time until the renew point, never negative", () => {
    assert.equal(reusableForMs(token(nowS + 3600), NOW_MS), (3600 - ID_TOKEN_RENEW_SKEW_S) * 1000);
    assert.equal(reusableForMs(token(nowS - 60), NOW_MS), 0);
    assert.equal(reusableForMs(token(), NOW_MS), 0);
  });
});

describe("needsSignIn", () => {
  it("recognises MSAL's interaction-required errors", () => {
    assert.equal(needsSignIn({ name: "InteractionRequiredAuthError", errorCode: "x" }), true);
    assert.equal(needsSignIn({ name: "AuthError", errorCode: "login_required" }), true);
    assert.equal(needsSignIn({ name: "ServerError", errorCode: "invalid_grant", message: "AADSTS700084: The refresh token was issued to a single page app" }), true);
    assert.equal(needsSignIn({ name: "InteractionRequiredAuthError", errorCode: "no_tokens_found" }), true);
  });

  it("treats a timed-out hidden-iframe renewal as needing sign-in", () => {
    assert.equal(needsSignIn({ name: "BrowserAuthError", errorCode: "monitor_window_timeout" }), true);
  });

  it("does not send a recruiter to sign-in for a network blip", () => {
    assert.equal(needsSignIn({ name: "BrowserAuthError", errorCode: "no_network_connectivity" }), false);
    assert.equal(needsSignIn(new TypeError("Failed to fetch")), false);
    assert.equal(needsSignIn(undefined), false);
    assert.equal(needsSignIn("login_required"), false);
  });
});

describe("createApiTokenSource", () => {
  type Account = { homeAccountId: string; idToken?: string };
  const ACCOUNT: Account = { homeAccountId: "home-1" };

  function fakeMsal(results: Array<unknown>) {
    const calls: Array<{ forceRefresh: boolean }> = [];
    let clock = NOW_MS;
    let account: Account | null = ACCOUNT;
    const source = createApiTokenSource<Account>({
      currentAccount: () => account,
      acquireSilent: async (_account, forceRefresh) => {
        calls.push({ forceRefresh });
        const next = results.shift();
        if (next instanceof Error || (next && typeof next === "object" && "errorCode" in next)) throw next;
        return next as { idToken: string; idTokenClaims: { exp: number } };
      },
      now: () => clock,
    });
    return {
      source,
      calls,
      advance: (ms: number) => void (clock += ms),
      setAccount: (a: Account | null) => void (account = a),
    };
  }
  const fresh = (name: string, expS = nowS + 3600) => ({ idToken: name, idTokenClaims: { exp: expS } });

  it("renews an expired cached ID token with a forced refresh, without a redirect", async () => {
    const msal = fakeMsal([fresh("stale", nowS - 8 * 3600), fresh("renewed")]);
    assert.deepEqual(await msal.source.acquire(), { token: "renewed", signInRequired: false });
    assert.deepEqual(msal.calls, [{ forceRefresh: false }, { forceRefresh: true }]);
  });

  it("serves concurrent requests from one lookup", async () => {
    const msal = fakeMsal([fresh("t1")]);
    const outcomes = await Promise.all([msal.source.acquire(), msal.source.acquire(), msal.source.acquire()]);
    assert.ok(outcomes.every((o) => o.token === "t1"));
    assert.equal(msal.calls.length, 1);
  });

  it("reuses the token until the renew point, then asks MSAL again", async () => {
    const msal = fakeMsal([fresh("t1"), fresh("t2", nowS + 2 * 3600)]);
    await msal.source.acquire();
    msal.advance((3600 - ID_TOKEN_RENEW_SKEW_S - 1) * 1000);
    assert.equal((await msal.source.acquire()).token, "t1");
    assert.equal(msal.calls.length, 1);
    msal.advance(2000);
    assert.equal((await msal.source.acquire()).token, "t2");
    assert.equal(msal.calls.length, 2);
  });

  it("forget() and a different account both bypass the reused token", async () => {
    const msal = fakeMsal([fresh("t1"), fresh("t2"), fresh("t3")]);
    await msal.source.acquire();
    msal.source.forget();
    assert.equal((await msal.source.acquire()).token, "t2");
    msal.setAccount({ homeAccountId: "home-2" });
    assert.equal((await msal.source.acquire()).token, "t3");
  });

  it("asks for sign-in only when MSAL says interaction is required", async () => {
    const needsInteraction = Object.assign(new Error("AADSTS700084: refresh token expired"), {
      name: "InteractionRequiredAuthError",
      errorCode: "invalid_grant",
    });
    const msal = fakeMsal([needsInteraction]);
    assert.deepEqual(await msal.source.acquire(), { token: null, signInRequired: true });
    const offline = fakeMsal([Object.assign(new Error("offline"), { errorCode: "no_network_connectivity" })]);
    assert.deepEqual(await offline.source.acquire(), { token: null, signInRequired: false });
  });

  it("does nothing without a signed-in account", async () => {
    const msal = fakeMsal([]);
    msal.setAccount(null);
    assert.deepEqual(await msal.source.acquire(), { token: null, signInRequired: false });
    assert.equal(msal.calls.length, 0);
  });
});
