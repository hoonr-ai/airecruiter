import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import vm from "node:vm";
import {
  NR_APPS,
  NR_LOCAL_API_ORIGINS,
  NR_LOCAL_OPT_IN_KEY,
  buildNrInit,
  deriveAppEnv,
  localOptIn,
  selectNrConfig,
  type NrStorage,
} from "./nr-config.ts";

const PUBLIC_LOADER = new URL("../public/newrelic-browser-agent.js", import.meta.url);
const LOADER_TEXT = readFileSync(PUBLIC_LOADER, "utf8");

// The loader body must stay byte-identical to the canonical CDN artifact. To
// upgrade: curl https://js-agent.newrelic.com/nr-loader-spa-<ver>.min.js, splice
// it in place of the old body, then update both constants from
// `shasum -a 256 nr-loader-spa-<ver>.min.js`.
const LOADER_VERSION = "1.320.1";
const LOADER_BODY_SHA256 = "b0b593c1d8fc3aa4f844826a74ddc1a47015c1cfdb25470f658709029010221f";
const BODY_MARK = ";/*! For license information please see nr-loader-spa-";

function splitLoader(text: string): { wrapper: string; body: string } {
  const start = text.indexOf(BODY_MARK);
  const end = text.lastIndexOf("\n})();");
  assert.ok(start > 0, "loader body marker not found");
  assert.ok(end > start, "wrapper close not found after the body");
  return { wrapper: text.slice(0, start) + text.slice(end), body: text.slice(start, end) };
}

class MemoryStorage implements NrStorage {
  data = new Map<string, string>();
  constructor(init?: Record<string, string>) {
    for (const [k, v] of Object.entries(init ?? {})) this.data.set(k, v);
  }
  getItem(key: string) {
    return this.data.has(key) ? (this.data.get(key) as string) : null;
  }
  setItem(key: string, value: string) {
    this.data.set(key, String(value));
  }
  removeItem(key: string) {
    this.data.delete(key);
  }
}

class ThrowingStorage implements NrStorage {
  getItem(): string | null {
    throw new Error("SecurityError");
  }
  setItem() {
    throw new Error("SecurityError");
  }
  removeItem() {
    throw new Error("SecurityError");
  }
}

/** Runs the public file's wrapper (loader body removed) on a fake page. */
function runPublicWrapper(hostname: string, search: string, storage: NrStorage | null) {
  const { wrapper } = splitLoader(LOADER_TEXT);
  const context: Record<string, unknown> = {
    location: { hostname, search },
  };
  if (storage) {
    context.localStorage = storage;
  } else {
    Object.defineProperty(context, "localStorage", {
      get() {
        throw new Error("SecurityError: access denied");
      },
    });
  }
  context.window = context;
  vm.createContext(context);
  vm.runInContext(wrapper, context);
  // Plain JSON copy: the vm's objects come from another realm.
  return context.NREUM === undefined ? null : JSON.parse(JSON.stringify(context.NREUM));
}

describe("public/newrelic-browser-agent.js", () => {
  it("keeps the loader body byte-identical to the CDN artifact", () => {
    const { body } = splitLoader(LOADER_TEXT);
    assert.equal(LOADER_TEXT.split(BODY_MARK).length, 2, "exactly one loader body");
    assert.ok(body.startsWith(`${BODY_MARK}${LOADER_VERSION}.min.js.LICENSE.txt */\n`));
    assert.equal(createHash("sha256").update(body, "utf8").digest("hex"), LOADER_BODY_SHA256);
  });

  it("carries the same host table as lib/nr-config.ts", () => {
    const m = /\/\*nr-apps\*\/([\s\S]*?)\/\*end-nr-apps\*\//.exec(LOADER_TEXT);
    assert.ok(m, "nr-apps table markers not found");
    assert.deepEqual(JSON.parse(m[1]), JSON.parse(JSON.stringify(NR_APPS)));
  });

  it("never reports into the pairbot-prod browser app", () => {
    // The header comment names the old ids as a warning; the code must not.
    const code = splitLoader(LOADER_TEXT).wrapper.split("\n;(function () {")[1];
    assert.ok(code, "wrapper IIFE not found");
    assert.ok(!code.includes("1549492374"));
    assert.ok(!code.includes("1589256495"));
  });

  const cases: Array<{ name: string; hostname: string; search: string; storage: () => NrStorage | null }> = [
    { name: "PROD", hostname: "pair.pyramidci.com", search: "", storage: () => new MemoryStorage() },
    { name: "QA", hostname: "pairqa.pyramidci.com", search: "?step=5", storage: () => new MemoryStorage() },
    { name: "unknown host", hostname: "pair-preview.example.com", search: "?nr=1", storage: () => new MemoryStorage() },
    { name: "www. is not PROD", hostname: "www.pair.pyramidci.com", search: "", storage: () => new MemoryStorage() },
    { name: "localhost, no opt-in", hostname: "localhost", search: "", storage: () => new MemoryStorage() },
    { name: "localhost, ?nr=1", hostname: "localhost", search: "?step=5&nr=1", storage: () => new MemoryStorage() },
    { name: "localhost, stored flag", hostname: "localhost", search: "", storage: () => new MemoryStorage({ [NR_LOCAL_OPT_IN_KEY]: "1" }) },
    { name: "localhost, ?nr=0 clears flag", hostname: "localhost", search: "?nr=0", storage: () => new MemoryStorage({ [NR_LOCAL_OPT_IN_KEY]: "1" }) },
    { name: "127.0.0.1, ?nr=1", hostname: "127.0.0.1", search: "?nr=1", storage: () => new MemoryStorage() },
    { name: "localhost, ?nr=10 is not an opt-in", hostname: "localhost", search: "?nr=10", storage: () => new MemoryStorage() },
    { name: "localhost, storage throws, ?nr=1", hostname: "localhost", search: "?nr=1", storage: () => new ThrowingStorage() },
    { name: "localhost, storage throws, no flag", hostname: "localhost", search: "", storage: () => new ThrowingStorage() },
    { name: "localhost, storage getter throws, ?nr=1", hostname: "localhost", search: "?nr=1", storage: () => null },
  ];

  for (const c of cases) {
    it(`wrapper matches selectNrConfig: ${c.name}`, () => {
      const pubStorage = c.storage();
      const tsStorage = c.storage();
      const fromFile = runPublicWrapper(c.hostname, c.search, pubStorage);
      const fromTs = selectNrConfig({ hostname: c.hostname, search: c.search }, tsStorage);
      assert.deepEqual(fromFile, fromTs === null ? null : JSON.parse(JSON.stringify(fromTs)));
      if (pubStorage instanceof MemoryStorage && tsStorage instanceof MemoryStorage) {
        assert.deepEqual([...pubStorage.data], [...tsStorage.data], "same opt-in side effect");
      }
    });
  }
});

describe("selectNrConfig", () => {
  it("PROD reports into PAIR Web (PROD)", () => {
    const cfg = selectNrConfig({ hostname: "pair.pyramidci.com", search: "" }, null);
    assert.ok(cfg);
    assert.equal(cfg.loader_config.applicationID, "1589288242");
    assert.equal(cfg.loader_config.agentID, "1589288242");
    assert.equal(cfg.loader_config.accountID, "7594453");
    assert.equal(cfg.loader_config.trustKey, "7594453");
    assert.equal(cfg.loader_config.licenseKey, "NRBR-e39b9a2d35dd56ef8ea");
    assert.equal(cfg.info.applicationID, "1589288242");
    assert.equal(cfg.info.jsAttributes.app_env, "prod");
    assert.deepEqual(cfg.init.distributed_tracing, { enabled: true });
  });

  it("QA reports into PAIR Web (QA)", () => {
    const cfg = selectNrConfig({ hostname: "pairqa.pyramidci.com", search: "" }, null);
    assert.equal(cfg?.loader_config.applicationID, "1589288243");
    assert.equal(cfg?.info.jsAttributes.app_env, "qa");
  });

  it("local needs the opt-in and then reports into PAIR Web (local)", () => {
    const storage = new MemoryStorage();
    assert.equal(selectNrConfig({ hostname: "localhost", search: "" }, storage), null);
    const on = selectNrConfig({ hostname: "localhost", search: "?nr=1" }, storage);
    assert.equal(on?.loader_config.applicationID, "1589288244");
    assert.equal(storage.getItem(NR_LOCAL_OPT_IN_KEY), "1", "?nr=1 is remembered");
    assert.ok(selectNrConfig({ hostname: "localhost", search: "" }, storage), "flag persists across loads");
    assert.equal(selectNrConfig({ hostname: "localhost", search: "?nr=0" }, storage), null);
    assert.equal(storage.getItem(NR_LOCAL_OPT_IN_KEY), null, "?nr=0 clears the flag");
  });

  it("logging and session replay are off everywhere; Amplitude is denied", () => {
    for (const env of ["prod", "qa", "local"] as const) {
      const init = buildNrInit(env) as Record<string, Record<string, unknown>>;
      assert.deepEqual(init.logging, { enabled: false });
      assert.deepEqual(init.session_replay, { enabled: false });
      assert.ok((init.ajax.deny_list as string[]).includes("*.amplitude.com"));
      assert.ok((init.ajax.deny_list as string[]).includes("bam.nr-data.net"));
      assert.deepEqual(init.performance, { capture_detail: false, capture_marks: false, capture_measures: true });
      assert.deepEqual(init.privacy, { cookies_enabled: true });
      assert.deepEqual(init.browser_consent_mode, { enabled: false });
    }
  });

  it("only local allows cross-origin trace context to the API", () => {
    const local = buildNrInit("local").distributed_tracing as Record<string, unknown>;
    assert.deepEqual(local, {
      enabled: true,
      allowed_origins: NR_LOCAL_API_ORIGINS,
      cors_use_newrelic_header: false,
      cors_use_tracecontext_headers: true,
    });
    assert.deepEqual(NR_LOCAL_API_ORIGINS, [
      "http://localhost:8000",
      "http://localhost:8001",
      "http://127.0.0.1:8000",
      "http://127.0.0.1:8001",
    ]);
  });

  it("does not resolve prototype keys as hosts", () => {
    assert.equal(selectNrConfig({ hostname: "constructor", search: "" }, null), null);
    assert.equal(selectNrConfig({ hostname: "__proto__", search: "" }, null), null);
  });
});

describe("localOptIn", () => {
  it("falls back to the URL flag when storage is unavailable", () => {
    assert.equal(localOptIn("?nr=1", null), true);
    assert.equal(localOptIn("?nr=0", null), false);
    assert.equal(localOptIn("", new ThrowingStorage()), false);
    assert.equal(localOptIn("?a=1&nr=1", new ThrowingStorage()), true);
  });
});

describe("deriveAppEnv", () => {
  it("maps known hosts and labels the rest", () => {
    assert.equal(deriveAppEnv("pair.pyramidci.com"), "prod");
    assert.equal(deriveAppEnv("pairqa.pyramidci.com"), "qa");
    assert.equal(deriveAppEnv("localhost"), "local");
    assert.equal(deriveAppEnv("127.0.0.1"), "local");
    assert.equal(deriveAppEnv("example.com"), "other");
    assert.equal(deriveAppEnv("toString"), "other");
  });
});
