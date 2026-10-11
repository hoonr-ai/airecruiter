import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { createAuthFetch, requestUrl, withAuthHeaders } from "./fetch-auth.ts";

const API = "https://pairqa.pyramidci.com";
const AUTH = { "X-User-Email": "recruiter@example.com", Authorization: "Bearer tok" };

type Sent = { input: RequestInfo | URL; init: RequestInit | undefined };

function harness(auth: Record<string, string> | (() => Promise<Record<string, string>>) = AUTH) {
  const sent: Sent[] = [];
  let authCalls = 0;
  const fakeFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    sent.push({ input, init });
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
  const getAuth =
    typeof auth === "function"
      ? async () => {
          authCalls += 1;
          return auth();
        }
      : async () => {
          authCalls += 1;
          return { ...auth };
        };
  const patched = createAuthFetch(fakeFetch, (url) => url.startsWith(API) || url.includes(API), getAuth);
  return { patched, sent, authCalls: () => authCalls };
}

function sentHeaders(s: Sent): Headers {
  return new Headers(s.init?.headers);
}

describe("createAuthFetch (global fetch patch)", () => {
  it("plain-object headers keep Content-Type and gain auth", async () => {
    const h = harness();
    await h.patched(`${API}/api/v1/candidates/save`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Content-Type"), "application/json");
    assert.equal(headers.get("Authorization"), "Bearer tok");
    assert.equal(headers.get("X-User-Email"), "recruiter@example.com");
    assert.equal(h.sent[0].init?.method, "POST");
    assert.equal(h.sent[0].init?.body, "{}");
  });

  it("a Headers instance keeps Content-Type and gains auth (map-nr-frontend D1)", async () => {
    const h = harness();
    await h.patched(`${API}/api/v1/candidates/save`, {
      method: "POST",
      headers: new Headers({ "Content-Type": "application/json", traceparent: "00-abc-def-01" }),
      body: "{}",
    });
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Content-Type"), "application/json");
    assert.equal(headers.get("traceparent"), "00-abc-def-01");
    assert.equal(headers.get("Authorization"), "Bearer tok");
    assert.equal(headers.get("X-User-Email"), "recruiter@example.com");
  });

  it("survives an outer wrapper that turns init.headers into Headers (NR agent started late)", async () => {
    const h = harness();
    // What the NR fetch wrapper does before calling the fetch it wrapped.
    const outer = (input: RequestInfo | URL, init: RequestInit = {}) =>
      h.patched(input, { ...init, headers: new Headers(init.headers || {}) });
    await outer(`${API}/engage/launch`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Content-Type"), "application/json");
    assert.equal(headers.get("Authorization"), "Bearer tok");
  });

  it("tuple headers are honoured", async () => {
    const h = harness();
    await h.patched(`${API}/x`, { headers: [["Content-Type", "application/json"]] });
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Content-Type"), "application/json");
    assert.equal(headers.get("X-User-Email"), "recruiter@example.com");
  });

  it("caller-set auth wins and skips the token lookup when both are present", async () => {
    const h = harness();
    await h.patched(`${API}/x`, {
      headers: { authorization: "Bearer caller", "x-user-email": "caller@example.com", "Content-Type": "text/csv" },
    });
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Authorization"), "Bearer caller");
    assert.equal(headers.get("X-User-Email"), "caller@example.com");
    assert.equal(headers.get("Content-Type"), "text/csv");
    assert.equal(h.authCalls(), 0, "no second acquireTokenSilent");
  });

  it("only fills the missing auth header", async () => {
    const h = harness();
    await h.patched(`${API}/x`, { headers: new Headers({ Authorization: "Bearer caller" }) });
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Authorization"), "Bearer caller");
    assert.equal(headers.get("X-User-Email"), "recruiter@example.com");
  });

  it("a Request input keeps its own headers", async () => {
    const h = harness();
    const request = new Request(`${API}/x`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    await h.patched(request);
    const headers = sentHeaders(h.sent[0]);
    assert.equal(headers.get("Content-Type"), "application/json");
    assert.equal(headers.get("Authorization"), "Bearer tok");
    assert.equal(h.sent[0].input, request);
  });

  it("leaves non-API URLs untouched", async () => {
    const h = harness();
    const init = { headers: { "Content-Type": "application/json" } };
    await h.patched("https://api2.amplitude.com/2/httpapi", init);
    assert.equal(h.sent[0].init, init);
    assert.equal(h.authCalls(), 0);
  });

  it("passes init through unchanged when there is nothing to add", async () => {
    const h = harness({});
    const init = { method: "GET" };
    await h.patched(`${API}/x`, init);
    assert.equal(h.sent[0].init, init);
  });

  it("still sends the request when the auth lookup throws", async () => {
    const h = harness(async () => {
      throw new Error("msal not ready");
    });
    const init = { headers: { "Content-Type": "application/json" } };
    await h.patched(`${API}/x`, init);
    assert.equal(h.sent.length, 1);
    assert.equal(h.sent[0].init, init);
  });
});

describe("withAuthHeaders", () => {
  it("never mutates the caller's headers object", async () => {
    const callerHeaders = { "Content-Type": "application/json" };
    const init = { headers: callerHeaders };
    const out = await withAuthHeaders(`${API}/x`, init, async () => ({ ...AUTH }));
    assert.deepEqual(callerHeaders, { "Content-Type": "application/json" });
    assert.notEqual(out, init);
  });
});

describe("requestUrl", () => {
  it("handles strings, URLs and Requests", () => {
    assert.equal(requestUrl(`${API}/a`), `${API}/a`);
    assert.equal(requestUrl(new URL(`${API}/b`)), `${API}/b`);
    assert.equal(requestUrl(new Request(`${API}/c`)), `${API}/c`);
  });
});
