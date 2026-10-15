// New Relic Browser configuration per host: which browser app a page reports
// into, and with which agent settings.
//
// The agent itself is the copy/paste loader public/newrelic-browser-agent.js.
// It is served raw and runs before hydration, so it cannot import this module:
// it carries its own ES5 copy of the table and the selection below.
// lib/nr-config.test.ts parses the public file's table, runs its wrapper
// against selectNrConfig() for every host/opt-in case, and checks the loader
// body against the canonical CDN artifact's hash, so the two cannot drift.
//
// Browser apps (account 7594453, created in NR 2026-10-11):
//   pair.pyramidci.com    → "PAIR Web (PROD)"  1589288242
//   pairqa.pyramidci.com  → "PAIR Web (QA)"    1589288243
//   localhost, 127.0.0.1  → "PAIR Web (local)" 1589288244, opt-in only
// PROD moved off 1549492374: that is the pairbot-prod APM app's linked browser
// app (agentID 1589256495). Never point PAIR back at it, and never rename it.
//
// Local reporting is opt-in so a dev server does not report by default:
// `?nr=1` turns it on (remembered in localStorage `pair_nr_local`), `?nr=0`
// turns it off again.

export type NrEnv = "prod" | "qa" | "local";

export interface NrApp {
  env: NrEnv;
  applicationID: string;
  agentID: string;
  /** Report only after the local opt-in (`?nr=1` / localStorage). */
  optIn?: boolean;
}

export const NR_ACCOUNT_ID = "7594453";
export const NR_TRUST_KEY = "7594453";
// Browser ingest key (NRBR-…): public by design, ships in every page.
export const NR_LICENSE_KEY = "NRBR-e39b9a2d35dd56ef8ea";
export const NR_BEACON = "bam.nr-data.net";

export const NR_APPS: Readonly<Record<string, NrApp>> = {
  "pair.pyramidci.com": { env: "prod", applicationID: "1589288242", agentID: "1589288242" },
  "pairqa.pyramidci.com": { env: "qa", applicationID: "1589288243", agentID: "1589288243" },
  "localhost": { env: "local", applicationID: "1589288244", agentID: "1589288244", optIn: true },
  "127.0.0.1": { env: "local", applicationID: "1589288244", agentID: "1589288244", optIn: true },
};

export const NR_LOCAL_OPT_IN_KEY = "pair_nr_local";

// Local dev is cross-origin (:3000 → API :8000/:8001), so distributed-tracing
// headers must be allowed explicitly. QA/PROD serve the API same-origin.
export const NR_LOCAL_API_ORIGINS = [
  "http://localhost:8000",
  "http://localhost:8001",
  "http://127.0.0.1:8000",
  "http://127.0.0.1:8001",
];

// Never record the agent's own beacons as AjaxRequest events. Amplitude is gone
// (2026-10-12) but stays denied while tabs still run a pre-removal bundle.
export const NR_AJAX_DENY_LIST = ["bam.nr-data.net", "*.amplitude.com"];

export interface NrBrowserConfig {
  init: Record<string, unknown>;
  loader_config: {
    accountID: string;
    trustKey: string;
    agentID: string;
    licenseKey: string;
    applicationID: string;
  };
  info: {
    beacon: string;
    errorBeacon: string;
    licenseKey: string;
    applicationID: string;
    sa: number;
    jsAttributes: { app_env: NrEnv };
  };
}

/** Minimal Storage surface (window.localStorage in the browser). */
export interface NrStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/**
 * NR's generated jsConfig init for the PAIR Web apps, plus: Amplitude on the
 * AJAX deny list, browser logs and session replay explicitly off (the loader
 * defaults logging ON, which wraps console.* — PAIR logs candidate data to the
 * console), and cross-origin distributed tracing for local dev only.
 */
export function buildNrInit(env: NrEnv): Record<string, unknown> {
  const distributedTracing: Record<string, unknown> = { enabled: true };
  if (env === "local") {
    distributedTracing.allowed_origins = [...NR_LOCAL_API_ORIGINS];
    distributedTracing.cors_use_newrelic_header = false;
    distributedTracing.cors_use_tracecontext_headers = true;
  }
  return {
    browser_consent_mode: { enabled: false },
    privacy: { cookies_enabled: true },
    distributed_tracing: distributedTracing,
    performance: { capture_detail: false, capture_marks: false, capture_measures: true },
    ajax: { deny_list: [...NR_AJAX_DENY_LIST], capture_payloads: "none" },
    logging: { enabled: false },
    session_replay: { enabled: false },
  };
}

/**
 * Applies `?nr=1` / `?nr=0` (persisting it) and returns whether local
 * reporting is on. Storage that is missing or throws (private mode, blocked
 * site data) falls back to the URL flag alone.
 */
export function localOptIn(search: string, storage: NrStorage | null | undefined): boolean {
  const m = /[?&]nr=([01])(?:&|$)/.exec(search || "");
  try {
    if (!storage) throw new Error("no storage");
    if (m) {
      if (m[1] === "1") storage.setItem(NR_LOCAL_OPT_IN_KEY, "1");
      else storage.removeItem(NR_LOCAL_OPT_IN_KEY);
    }
    return storage.getItem(NR_LOCAL_OPT_IN_KEY) === "1";
  } catch {
    return !!m && m[1] === "1";
  }
}

/**
 * The NREUM {init, loader_config, info} a page on `hostname` should start the
 * agent with, or null when it must not report (unknown host, or local without
 * the opt-in).
 */
export function selectNrConfig(
  location: { hostname: string; search: string },
  storage: NrStorage | null | undefined,
): NrBrowserConfig | null {
  const app = Object.prototype.hasOwnProperty.call(NR_APPS, location.hostname)
    ? NR_APPS[location.hostname]
    : null;
  if (!app) return null;
  if (app.optIn && !localOptIn(location.search, storage)) return null;
  return {
    init: buildNrInit(app.env),
    loader_config: {
      accountID: NR_ACCOUNT_ID,
      trustKey: NR_TRUST_KEY,
      agentID: app.agentID,
      licenseKey: NR_LICENSE_KEY,
      applicationID: app.applicationID,
    },
    info: {
      beacon: NR_BEACON,
      errorBeacon: NR_BEACON,
      licenseKey: NR_LICENSE_KEY,
      applicationID: app.applicationID,
      sa: 1,
      jsAttributes: { app_env: app.env },
    },
  };
}

/**
 * `app_env` for telemetry when NEXT_PUBLIC_APP_ENV is not baked into the
 * build: the env of the host's browser app, "other" for any other host.
 */
export function deriveAppEnv(hostname: string): NrEnv | "other" {
  return Object.prototype.hasOwnProperty.call(NR_APPS, hostname) ? NR_APPS[hostname].env : "other";
}
