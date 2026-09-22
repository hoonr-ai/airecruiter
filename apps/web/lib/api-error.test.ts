/// <reference types="node" />
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ApiError,
  isNotFoundError,
  LIVE_REPORT_PROD_ONLY_MESSAGE,
  isWithinRedirectCooldown,
  recordRedirectTimestamp,
} from "./api-error.ts";

test("ApiError stores status and path correctly", () => {
  const err = new ApiError(404, "/api/analytics/live-report/launches", "404 Not Found");
  assert.equal(err.name, "ApiError");
  assert.equal(err.status, 404);
  assert.equal(err.path, "/api/analytics/live-report/launches");
  assert.equal(err.message, "404 Not Found");
});

test("isNotFoundError detects 404 on ApiError instances", () => {
  const err404 = new ApiError(404, "/test");
  const err500 = new ApiError(500, "/test");
  const err401 = new ApiError(401, "/test");

  assert.equal(isNotFoundError(err404), true);
  assert.equal(isNotFoundError(err500), false);
  assert.equal(isNotFoundError(err401), false);
});

test("isNotFoundError detects status number or message fallbacks", () => {
  assert.equal(isNotFoundError({ status: 404 }), true);
  assert.equal(isNotFoundError({ status: 200 }), false);
  assert.equal(isNotFoundError(new Error("404 /path: Not Found")), true);
  assert.equal(isNotFoundError(new Error("500 Internal Server Error")), false);
  assert.equal(isNotFoundError(null), false);
  assert.equal(isNotFoundError(undefined), false);
});

test("LIVE_REPORT_PROD_ONLY_MESSAGE constant is defined", () => {
  assert.equal(LIVE_REPORT_PROD_ONLY_MESSAGE, "Live Launch Monitor is available in Production only.");
});

test("isWithinRedirectCooldown and recordRedirectTimestamp work together cleanly", () => {
  const store: Record<string, string> = {};
  const mockStorage = {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => {
      store[k] = v;
    },
  };

  const t0 = 100000;
  // Initially no redirect has happened
  assert.equal(isWithinRedirectCooldown(t0, mockStorage), false);
  assert.equal(store["last_msal_login_redirect"], undefined);

  // Record redirect timestamp
  recordRedirectTimestamp(t0, mockStorage);
  assert.equal(store["last_msal_login_redirect"], String(t0));

  // Call after 5s (< 15s): within cooldown
  assert.equal(isWithinRedirectCooldown(t0 + 5000, mockStorage), true);

  // Call after 16s (> 15s): cooldown expired
  assert.equal(isWithinRedirectCooldown(t0 + 16000, mockStorage), false);

  // Storage failure (e.g. sandboxed iframe or disabled cookies): does not throw
  const throwingStorage = {
    getItem: () => {
      throw new Error("SecurityError: storage disabled");
    },
    setItem: () => {
      throw new Error("SecurityError: storage disabled");
    },
  };
  assert.equal(isWithinRedirectCooldown(t0, throwingStorage), false);
  assert.doesNotThrow(() => recordRedirectTimestamp(t0, throwingStorage));
});

