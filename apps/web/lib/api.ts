// Central API client. Keeps the FastAPI base URL + JSON boilerplate in one
// place. Streaming and multipart calls pass `API_BASE` directly; the `req`
// helper handles the JSON case.

import type { AccountInfo } from "@azure/msal-browser";
import { msalInstance, loginRequest } from "@/lib/msal-config";
import { createApiTokenSource, type TokenOutcome } from "@/lib/auth-token";
import type { Snapshot } from "@/app/admin/live-report/types";
import {
  candidatesQueryString,
  retryAfterMs,
  type CandidatesQuery,
  type CreateSessionBody,
  type LaunchSessionRowsPage,
  type LaunchSessionView,
} from "@/lib/launch-session";

export const API_BASE = process.env.NEXT_PUBLIC_API_URL!;

export function getActiveUserEmail(): string | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const active = msalInstance.getActiveAccount();
    if (active?.username) return active.username;
    const all = msalInstance.getAllAccounts();
    if (all && all.length > 0 && all[0].username) {
      return all[0].username;
    }
  } catch (e) {
    // ignore if MSAL not initialized yet
  }
  if (process.env.NODE_ENV !== "production") {
    try {
      return localStorage.getItem("dev_user_email") || undefined;
    } catch (e) {
      return undefined;
    }
  }
  return undefined;
}

// ---- API token ------------------------------------------------------------
// The API authenticates with the Microsoft ID token. lib/auth-token.ts holds
// the rules (why the ID token's own expiry is checked, the shared lookup, the
// reuse window); this wires MSAL into them.

const LOGIN_SCOPES = loginRequest.scopes;

let signInRedirectStarted = false;

function currentAccount(): AccountInfo | null {
  try {
    return msalInstance.getActiveAccount() || (msalInstance.getAllAccounts()[0] ?? null);
  } catch {
    // MSAL not initialized yet
    return null;
  }
}

const apiTokens = createApiTokenSource<AccountInfo>({
  currentAccount,
  acquireSilent: (account, forceRefresh) =>
    msalInstance.acquireTokenSilent({ scopes: LOGIN_SCOPES, account, forceRefresh }),
});

/** The token for the API, or why there is none. Never throws. */
export async function acquireApiToken(): Promise<TokenOutcome> {
  if (typeof window === "undefined") return { token: null, signInRequired: false };
  return apiTokens.acquire();
}

/**
 * Send the browser to Microsoft sign-in, once. Returns false when it did not
 * go: a redirect was started moments ago (the 401 loop guard in
 * lib/api-error.ts) or there is no account to sign back in.
 */
export function redirectToSignIn(): boolean {
  if (typeof window === "undefined") return false;
  if (signInRedirectStarted) return true;
  try {
    if (isWithinRedirectCooldown(Date.now(), window.sessionStorage)) {
      console.warn("Skipping MSAL loginRedirect: one started within the cooldown window (redirect loop guard).");
      return false;
    }
  } catch {
    // storage unavailable: let the redirect go
  }
  if (!currentAccount()) return false;
  try {
    recordRedirectTimestamp(Date.now(), window.sessionStorage);
  } catch {
    // Ignore storage write failure
  }
  signInRedirectStarted = true;
  apiTokens.forget();
  msalInstance.loginRedirect({ scopes: LOGIN_SCOPES }).catch(() => {
    signInRedirectStarted = false;
  });
  return true;
}

/**
 * Run before the app renders (components/auth/AuthGuard.tsx): make sure the
 * cached sign-in still yields a token the API accepts. "redirecting" means
 * the page is on its way to Microsoft sign-in, so nothing should load in the
 * meantime. "unavailable" (no network, MSAL error) lets the app render anyway,
 * as it did before this check existed.
 */
export async function ensureApiSession(): Promise<"ready" | "redirecting" | "unavailable"> {
  const { token, signInRequired } = await acquireApiToken();
  if (token) return "ready";
  if (signInRequired && redirectToSignIn()) return "redirecting";
  return "unavailable";
}

export async function getAuthHeaders(): Promise<Record<string, string>> {
  if (typeof window === "undefined") return {};

  const headers: Record<string, string> = {};
  const userEmail = getActiveUserEmail();
  if (userEmail) {
    headers["X-User-Email"] = userEmail;
  }

  const { token, signInRequired } = await acquireApiToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  } else if (signInRequired) {
    redirectToSignIn();
  }
  return headers;
}

export async function authFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const authHeaders = await getAuthHeaders();
  const existingHeaders = (init.headers || {}) as Record<string, string>;
  return fetch(url, {
    ...init,
    headers: {
      ...authHeaders,
      ...existingHeaders,
    },
  });
}

export {
  ApiError,
  isNotFoundError,
  LIVE_REPORT_NOT_FOUND_MESSAGE,
  LIVE_REPORT_FORBIDDEN_MESSAGE,
  LIVE_REPORT_PROD_ONLY_MESSAGE,
  isWithinRedirectCooldown,
  recordRedirectTimestamp,
  MSAL_REDIRECT_COOLDOWN_MS,
  MSAL_REDIRECT_STORAGE_KEY,
} from "./api-error";
import { ApiError, isWithinRedirectCooldown, recordRedirectTimestamp } from "./api-error";

// fetch() network failures surface as a TypeError in all major browsers
// (Chrome "Failed to fetch", Safari "Load failed", Firefox "NetworkError when
// attempting to fetch resource"), and a dropped streaming-body read rejects
// the same way. Errors we throw ourselves (non-OK statuses, JSON parse
// failures) are regular Error instances, so this cleanly separates
// "connectivity problem — retryable" from "server said no".
export function isNetworkFetchError(e: unknown): boolean {
  return e instanceof TypeError;
}

// Global fetch patch: auth headers on every PAIR API call, however it was made.
// Headers-aware (lib/fetch-auth.ts): a Headers/tuple `init.headers` — e.g. from
// the New Relic agent's fetch wrapper — keeps its Content-Type, and auth is
// only added where absent (authFetch/req already set it, so no second token
// acquisition for those).
import { createAuthFetch } from "./fetch-auth";

if (typeof window !== "undefined" && !(window as any).__apiFetchPatched) {
  (window as any).__apiFetchPatched = true;
  const originalFetch = window.fetch.bind(window);
  window.fetch = createAuthFetch(
    originalFetch,
    (url) => url.startsWith(API_BASE) || url.includes(API_BASE),
    getAuthHeaders,
  );
}

type JsonInit = Omit<RequestInit, "body" | "headers"> & {
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

async function req<T>(path: string, init: JsonInit = {}): Promise<T> {
  const { body, headers, ...rest } = init;
  const authHeaders = await getAuthHeaders();
  const res = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...authHeaders,
      ...(headers || {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    if (res.status === 401) {
      // The token was refused although it looked valid: sign in again (once,
      // and never twice within the cooldown, so a misconfigured API can't
      // loop the page).
      apiTokens.forget();
      redirectToSignIn();
    }
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, path, `${res.status} ${path}${text ? `: ${text}` : ""}`);
  }

  return res.json() as Promise<T>;
}

export type CrossSubmission = {
  id: number;
  job_id: string;
  jobdiva_id: string | null;
  person_key: string;
  candidate_id: string | null;
  source: string | null;
  name: string | null;
  email: string | null;
  phone: string | null;
  headline: string | null;
  location: string | null;
  /** The person's JobDiva profile id ("" when PAIR never linked one) — not always candidate_id. */
  jobdiva_candidate_id: string | null;
  prior_job_id: string | null;
  prior_jobdiva_id: string | null;
  prior_job_title: string | null;
  prior_customer_name: string | null;
  engage_status: string | null;
  screen_result: "Pass" | "Fail" | "In Progress" | "Pending" | string | null;
  engage_score: number | null;
  engage_total_score: number | null;
  screened_at: string | null;
  match_score: number | null;
  matched_skills: string[] | null;
  missing_skills: string[] | null;
  created_at: string | null;
  notified_at: string | null;
  added_at: string | null;
};

export type CrossSubmissionsResponse = {
  job_ref: string;
  count: number;
  candidates: CrossSubmission[];
};

// ---- Org hierarchy (admin) -------------------------------------------------

export type OrgHierarchyMember = {
  id: number;
  name: string;
  email: string | null;
  role: string;
  role_label: string;
  reports_to_id: number | null;
  vertical: string;
  title: string;
  direct_reports: number;
  total_reports: number;
};

export type OrgHierarchyLevel = { role: string; label: string; rank: number };

export type OrgHierarchyOverview = {
  members: OrgHierarchyMember[];
  levels: OrgHierarchyLevel[];
  counts: {
    total: number;
    with_email: number;
    without_email: number;
    by_role: Record<string, number>;
  };
  last_import: { imported_by: string; imported_at: string } | null;
};

export type OrgImportNeedsEmail = {
  name: string;
  role: string;
  role_label: string;
  vertical: string;
  direct_reports: number;
  total_reports: number;
  named_only: boolean;
};

export type OrgImportPreview = {
  dry_run: boolean;
  applied: boolean;
  blocking: boolean;
  summary: {
    rows: number;
    people: number;
    with_email: number;
    without_email: number;
    by_role: Record<string, number>;
    needs_email: OrgImportNeedsEmail[];
    top_level: {
      name: string;
      email: string | null;
      role: string;
      role_label: string;
      vertical: string;
      total_reports: number;
    }[];
    titles_treated_as_recruiter: { title: string; count: number }[];
  };
  issues: { severity: "error" | "warning" | "info"; row: number | null; message: string }[];
  diff: {
    current_people_with_email: number;
    emails_added: number;
    emails_removed: number;
    removed_sample: string[];
  } | null;
  coverage: { people_with_email: number; assigned_to_a_job: number } | null;
};

type ApiEnvelope<T> = { status: string; data?: T; message?: string };

export type DashboardQuery = {
  startDate?: string | null;
  endDate?: string | null;
  teamId?: string | null;
  job?: string | null;
  priority?: string | null;
  client?: string | null;
  jdStatus?: string | null;
  pairStatus?: string | null;
  vertical?: string | null;
  refresh?: boolean;
};

function dashboardQuery(opts?: DashboardQuery & { postedFrom?: string | null; postedTo?: string | null }): string {
  const qs = new URLSearchParams();
  if (!opts) return "";
  if (opts.startDate && opts.endDate) {
    qs.set("start_date", opts.startDate);
    qs.set("end_date", opts.endDate);
  }
  if (opts.postedFrom && opts.postedTo) {
    qs.set("posted_from", opts.postedFrom);
    qs.set("posted_to", opts.postedTo);
  }
  const pairs: [string, string | null | undefined][] = [
    ["team_id", opts.teamId],
    ["job", opts.job],
    ["priority", opts.priority],
    ["client", opts.client],
    ["jd_status", opts.jdStatus],
    ["pair_status", opts.pairStatus],
    ["vertical", opts.vertical],
  ];
  for (const [key, value] of pairs) if (value) qs.set(key, value);
  if (opts.refresh) qs.set("refresh", "true");
  const suffix = qs.toString();
  return suffix ? `?${suffix}` : "";
}

// ---- Launch sessions (backend-driven Source & Launch) ------------------------
// apps/api/routers/launch_sessions.py; the browser rules are in
// lib/launch-session.ts and the polling in hooks/use-launch-session.ts.
// Not `req`: every answer comes back as a value (status, parsed body,
// Retry-After) instead of a throw — the caller decides on attach / retry /
// legacy fallback — and polls don't emit an api_request_* event every 3 s.

export type LaunchSessionResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; body: unknown; retryAfterMs: number | null; network: boolean };

// Every launch-session request has a deadline (token + response + body): the
// poll loop awaits one request at a time, and nginx holds /api/ open for a day
// (proxy_read_timeout 86400), so a black-holed connection (VPN / Wi-Fi drop)
// or a stuck worker would otherwise freeze it. A timeout reads as a network
// error — retried like one; every write is safe to resend (create replays by
// its key, uploads upsert, confirm / cancel / start answer 409 on a replay,
// child creates use server-derived keys).
const LS_READ_TIMEOUT_MS = 20000;
const LS_WRITE_TIMEOUT_MS = 60000;

async function lsFetch<T>(
  path: string,
  init: { method?: string; body?: unknown; timeoutMs?: number } = {},
): Promise<LaunchSessionResult<T>> {
  const method = init.method || "GET";
  const networkError: LaunchSessionResult<T> = { ok: false, status: 0, body: null, retryAfterMs: null, network: true };
  const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<LaunchSessionResult<T>>((resolve) => {
    timer = setTimeout(() => {
      controller?.abort();
      resolve(networkError);
    }, init.timeoutMs ?? (method === "GET" ? LS_READ_TIMEOUT_MS : LS_WRITE_TIMEOUT_MS));
  });
  const run = async (): Promise<LaunchSessionResult<T>> => {
    let res: Response;
    try {
      res = await authFetch(`${API_BASE}${path}`, {
        method,
        headers: init.body !== undefined ? { "Content-Type": "application/json" } : {},
        body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
        signal: controller?.signal,
      });
    } catch {
      return networkError;
    }
    let body: unknown = null;
    try {
      const text = await res.text();
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        body = text;
      }
    } catch {
      // A dropped / aborted body read: an error answer's status still decides.
      if (res.ok) return networkError;
    }
    if (res.ok) return { ok: true, status: res.status, data: body as T };
    return {
      ok: false,
      status: res.status,
      body,
      retryAfterMs: retryAfterMs(res.headers.get("Retry-After")),
      network: false,
    };
  };
  try {
    return await Promise.race([run(), deadline]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

const lsJob = (jobRef: string) => `/api/v1/jobs/${encodeURIComponent(jobRef)}/launch-sessions`;
const lsSession = (id: string) => `/api/v1/launch-sessions/${encodeURIComponent(id)}`;
type LsContacts = Record<string, { phone?: string; email?: string }>;

export const launchSessionsApi = {
  config: () => lsFetch<unknown>(`/api/v1/launch-sessions/config`),
  create: (jobRef: string, body: CreateSessionBody) =>
    lsFetch<{ session: LaunchSessionView }>(lsJob(jobRef), { method: "POST", body }),
  listForJob: (jobRef: string, opts: { active?: boolean; needsContact?: boolean; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    if (opts.active) qs.set("active", "true");
    if (opts.needsContact) qs.set("needs_contact", "true");
    if (opts.limit) qs.set("limit", String(opts.limit));
    const q = qs.toString();
    return lsFetch<{ sessions: LaunchSessionView[] }>(`${lsJob(jobRef)}${q ? `?${q}` : ""}`);
  },
  get: (id: string) => lsFetch<{ session: LaunchSessionView }>(lsSession(id)),
  candidates: (id: string, q: CandidatesQuery = {}) =>
    lsFetch<LaunchSessionRowsPage>(`${lsSession(id)}/candidates${candidatesQueryString(q)}`),
  addCandidates: (id: string, candidates: unknown[]) =>
    lsFetch<{ added: number; total: number }>(`${lsSession(id)}/candidates`, { method: "POST", body: { candidates } }),
  start: (id: string) => lsFetch<{ session: LaunchSessionView }>(`${lsSession(id)}/start`, { method: "POST" }),
  confirm: (id: string, body: { contacts: LsContacts; candidate_ids?: string[] }) =>
    lsFetch<{ session: LaunchSessionView }>(`${lsSession(id)}/confirm`, { method: "POST", body }),
  needsContact: (
    id: string,
    body: { contacts: LsContacts; candidate_ids: string[]; skip_ids: string[]; idempotency_key?: string },
  ) =>
    lsFetch<{ session: LaunchSessionView | null; skipped: number }>(`${lsSession(id)}/needs-contact`, {
      method: "POST",
      body,
    }),
  retryFailed: (id: string, body: { candidate_ids?: string[]; idempotency_key?: string } = {}) =>
    lsFetch<{ session: LaunchSessionView }>(`${lsSession(id)}/retry-failed`, { method: "POST", body }),
  cancel: (id: string, reason?: string) =>
    lsFetch<{ session: LaunchSessionView }>(`${lsSession(id)}/cancel`, {
      method: "POST",
      body: reason ? { reason: reason.slice(0, 200) } : {},
    }),
};

export const api = {
  jobs: {
    fetch: (body: { job_id: string }) =>
      req<any>(`/jobs/fetch`, { method: "POST", body }),
    save: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/save`, { method: "POST", body }),
    saveStep: (jobId: string, step: number, body: unknown) =>
      req<any>(`/jobs/${jobId}/save-step?step=${step}`, { method: "POST", body }),
    monitor: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/monitor`, { method: "POST", body }),
    publish: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/publish`, { method: "POST", body }),
    createExternal: (body: unknown) =>
      req<any>(`/jobs/external/create`, { method: "POST", body }),
    getDraft: (jobId: string) => req<any>(`/jobs/${jobId}/draft`),
    getMonitoredData: (jobId: string) => req<any>(`/jobs/${jobId}/monitored-data`),
    getOutreachStats: (jobId: string) => req<any>(`/jobs/${jobId}/outreach-stats`),
    updateBasicInfo: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/basic-info`, { method: "PUT", body }),
    getCandidates: (jobId: string, query?: string) =>
      req<any>(`/jobs/${jobId}/candidates${query ? `?${query}` : ""}`),
    refreshResumeMatch: (jobId: string, candidateId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/candidates/${encodeURIComponent(candidateId)}/refresh-resume-match`, { method: "POST", body }),
    getCriteria: (jobId: string) => req<any>(`/api/jobs/${jobId}/criteria`),
    // Cross submissions: candidates PAIR already screened for OTHER jobs in
    // the last 60 days who match this one (routers/cross_submissions.py).
    getCrossSubmissions: (jobId: string) =>
      req<CrossSubmissionsResponse>(`/jobs/${jobId}/cross-submissions`),
    runCrossSubmissions: (jobId: string, body: { send_email: boolean } = { send_email: false }) =>
      req<any>(`/jobs/${jobId}/cross-submissions/run`, { method: "POST", body }),
    addCrossSubmission: (jobId: string, csId: number) =>
      req<{ status: string; already_present: boolean; candidate_id: string; source: string; name?: string }>(
        `/jobs/${jobId}/cross-submissions/${csId}/add`,
        { method: "POST" },
      ),
    // Wizard step active time, sent by hooks/use-step-active-time.ts
    // (routers/job_step_time.py). It sits under /api/ so nginx passes it
    // through without a /jobs/{id}/<subpath> allowlist entry. keepalive lets
    // the last flush outlive the page when the tab closes.
    stepTime: (
      jobRef: string,
      body: { step: number; active_ms: number },
      opts: { keepalive?: boolean } = {},
    ) =>
      req<{ status?: string }>(`/api/v1/jobs/${encodeURIComponent(jobRef)}/step-time`, {
        method: "POST",
        body,
        keepalive: opts.keepalive,
      }),
    // Step 5 "Source & Launch PAIR" click — T1 of the launch timings
    // (routers/launch_timing.py). The server keeps the first click only, so
    // calling it on every click is safe. keepalive so a click that navigates
    // away still lands.
    launchClicked: (jobRef: string) =>
      req<{ status?: string }>(`/api/v1/jobs/${encodeURIComponent(jobRef)}/launch-clicked`, {
        method: "POST",
        keepalive: true,
      }),
  },
  // Recruiter activity (routers/recruiter_activity.py): active time on a
  // surface (hooks/use-active-time.ts) and the actions only the page sees
  // (lib/activity.ts). Under /api/ (no nginx allowlist); keepalive so the last
  // flush outlives a closing tab.
  activity: {
    time: (
      body: { surface: string; job_ref?: string; active_ms: number },
      opts: { keepalive?: boolean } = {},
    ) => req<{ status?: string }>(`/api/v1/activity/time`, { method: "POST", body, keepalive: opts.keepalive }),
    actions: (body: { actions: unknown[] }, opts: { keepalive?: boolean } = {}) =>
      req<{ status?: string; recorded?: number }>(`/api/v1/activity/actions`, {
        method: "POST",
        body,
        keepalive: opts.keepalive,
      }),
  },
  candidates: {
    save: (body: unknown) =>
      req<any>(`/candidates/save`, { method: "POST", body }),
    getResume: (candidateId: string) =>
      req<any>(`/candidates/${candidateId}/resume`),
    analyze: (body: unknown) =>
      req<any>(`/candidates/analyze`, { method: "POST", body }),
    // Streaming endpoint — callers need the raw Response for a ReadableStream.
    searchStreamUrl: `${API_BASE}/candidates/search`,
    feedback: (jobId: string, candidateId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/candidates/${candidateId}/feedback`, { method: "POST", body }),
    enrichContact: (body: unknown) =>
      req<any>(`/candidates/enrich-contact`, { method: "POST", body }),
    updatePhone: (candidateId: string, body: unknown) =>
      req<any>(`/candidates/${encodeURIComponent(candidateId)}/phone`, { method: "PATCH", body }),
    getFilterOptions: () =>
      req<any>(`/candidates/filter-options`),
    getAllLaunched: (query?: string) =>
      req<any>(`/candidates/launched${query ? `?${query}` : ""}`),
  },
  manualCandidates: {
    add: (jobRef: string, body: unknown) =>
      req<any>(`/jobs/${jobRef}/manual-candidate`, { method: "POST", body }),
    // Multipart upload — callers pass FormData directly.
    bulkUploadUrl: (jobRef: string) => `${API_BASE}/jobs/${jobRef}/bulk-resumes`,
  },
  chat: {
    send: (body: unknown) => req<any>(`/chat`, { method: "POST", body }),
  },
  engagement: {
    getActivityLogs: (interviewId: string) =>
      req<any>(`/api/v1/engagement/interviews/${interviewId}/activity-logs`),
    getInterviewEvaluation: (interviewId: string) =>
      req<any>(`/api/v1/engagement/interviews/${interviewId}/evaluation`),
    getInterviewScoreSummary: (interviewId: string) =>
      req<any>(`/api/v1/engagement/interviews/${interviewId}/score-summary`),
    getAssessmentData: (interviewId: string) =>
      req<any>(`/api/v1/engagement/assess/${interviewId}`),
    // Admin: repair JobDiva profiles PAIR created blank (dry run unless dry_run: false).
    jobdivaBlankProfileBackfill: (body: {
      dry_run: boolean;
      limit?: number;
      job_id?: string;
      jobdiva_ids?: string[];
    }) =>
      req<any>(`/api/v1/engagement/engage/jobdiva-blank-profile-backfill`, { method: "POST", body }),
  },
  auth: {
    getMe: () => req<any>(`/api/v1/auth/me`),
  },
  adminAnalytics: {
    get: (teamId?: string | null) =>
      req<any>(`/api/v1/admin/analytics${teamId ? `?team_id=${encodeURIComponent(teamId)}` : ""}`),
    linkedinAccounts: () => req<any>(`/api/v1/admin/linkedin-accounts`),
  },
  launchReport: {
    // `date` is a calendar date in Eastern time (YYYY-MM-DD); omitting it asks
    // the backend for yesterday. Pass startDate+endDate instead for a range
    // (both required together); date is ignored when a range is given. Team
    // leads are auto-scoped server-side, so teamId is only meaningful for admins.
    get: (opts?: { date?: string | null; startDate?: string | null; endDate?: string | null; teamId?: string | null }) => {
      const { date, startDate, endDate, teamId } = opts ?? {};
      const qs = new URLSearchParams();
      if (startDate && endDate) {
        qs.set("start_date", startDate);
        qs.set("end_date", endDate);
      } else if (date) {
        qs.set("date", date);
      }
      if (teamId) qs.set("team_id", teamId);
      const suffix = qs.toString();
      return req<any>(`/api/v1/launch-report${suffix ? `?${suffix}` : ""}`);
    },
  },
  recruiterAnalytics: {
    // Per-recruiter rollup (routers/recruiter_analytics.py). startDate+endDate
    // are Eastern calendar dates, both or neither; neither = all time. Team
    // leads are pinned to their own team server-side, so teamId only matters
    // for admins. refresh skips the backend's 60s per-worker cache.
    get: (opts?: {
      startDate?: string | null;
      endDate?: string | null;
      teamId?: string | null;
      recruiter?: string | null;
      refresh?: boolean;
    }) => {
      const { startDate, endDate, teamId, recruiter, refresh } = opts ?? {};
      const qs = new URLSearchParams();
      if (startDate && endDate) {
        qs.set("start_date", startDate);
        qs.set("end_date", endDate);
      }
      if (teamId) qs.set("team_id", teamId);
      if (recruiter) qs.set("recruiter", recruiter);
      if (refresh) qs.set("refresh", "true");
      const suffix = qs.toString();
      // The page owns the payload type (app/admin/recruiter-analytics).
      return req<{ status?: string; data?: unknown; detail?: string }>(
        `/api/v1/admin/recruiter-analytics${suffix ? `?${suffix}` : ""}`,
      );
    },
    // Recruiter activity (routers/recruiter_activity.py): active time by
    // surface / day, actions, decision latency. Same scope rules; dates default
    // server-side to the last 7 Eastern days.
    activity: (opts?: {
      startDate?: string | null;
      endDate?: string | null;
      teamId?: string | null;
      recruiter?: string | null;
    }) => {
      const { startDate, endDate, teamId, recruiter } = opts ?? {};
      const qs = new URLSearchParams();
      if (startDate && endDate) {
        qs.set("start_date", startDate);
        qs.set("end_date", endDate);
      }
      if (teamId) qs.set("team_id", teamId);
      if (recruiter) qs.set("recruiter", recruiter);
      const suffix = qs.toString();
      return req<{ status?: string; data?: unknown; detail?: string }>(
        `/api/v1/admin/recruiter-activity${suffix ? `?${suffix}` : ""}`,
      );
    },
  },
  dashboard: {
    // PAIR Dashboard (routers/pair_dashboard.py). Dates are Eastern calendar
    // days, both or neither (neither = all time). Team leads are pinned to
    // their own team server-side, so teamId only matters for admins; refresh
    // skips the backend's short per-worker cache. The page owns the payload
    // types (components/dashboard/types.ts).
    options: (opts?: { teamId?: string | null; refresh?: boolean }) =>
      req<{ status?: string; data?: unknown }>(`/api/v1/admin/dashboard/options${dashboardQuery(opts)}`),
    overview: (opts?: DashboardQuery) =>
      req<{ status?: string; data?: unknown }>(`/api/v1/admin/dashboard/overview${dashboardQuery(opts)}`),
    funnel: (opts?: DashboardQuery & { postedFrom?: string | null; postedTo?: string | null }) =>
      req<{ status?: string; data?: unknown }>(`/api/v1/admin/dashboard/funnel${dashboardQuery(opts)}`),
    productivity: (opts?: DashboardQuery) =>
      req<{ status?: string; data?: unknown }>(`/api/v1/admin/dashboard/productivity${dashboardQuery(opts)}`),
  },
  noContact: {
    // Read-only: the list is code-managed (core/sourcing_config.py); admins
    // can view it but edits happen through code for now.
    companies: () => req<{ companies: string[]; editable: boolean }>(`/api/v1/no-contact/companies`),
  },
  teams: {
    list: () => req<any>(`/api/v1/teams`),
    create: (body: { name: string; lead_emails: string; member_emails: string }) =>
      req<any>(`/api/v1/teams`, { method: "POST", body }),
    update: (teamId: string, body: { name: string; lead_emails: string; member_emails: string }) =>
      req<any>(`/api/v1/teams/${encodeURIComponent(teamId)}`, { method: "PUT", body }),
    remove: (teamId: string) =>
      req<any>(`/api/v1/teams/${encodeURIComponent(teamId)}`, { method: "DELETE" }),
  },
  // Admin: the Recruiter → Resource Manager → Delivery Manager → Delivery Director → AVP
  // tree that decides whose jobs and admin analytics each person sees.
  orgHierarchy: {
    get: () => req<ApiEnvelope<OrgHierarchyOverview>>(`/api/v1/org-hierarchy`),
    // dry_run defaults to a preview server-side; pass false to replace the tree.
    import: (body: { csv: string; dry_run: boolean; head_role: string }) =>
      req<ApiEnvelope<OrgImportPreview>>(`/api/v1/org-hierarchy/import`, { method: "POST", body }),
    // The current tree in the importer's own format.
    export: () => req<ApiEnvelope<{ filename: string; csv: string }>>(`/api/v1/org-hierarchy/export`),
  },
  liveReport: {
    getLaunches: (params?: { search?: string; jobdiva_id?: string; limit?: number; signal?: AbortSignal }) => {
      const qs = new URLSearchParams();
      if (params?.search) qs.set("search", params.search);
      if (params?.jobdiva_id) qs.set("jobdiva_id", params.jobdiva_id);
      if (params?.limit) qs.set("limit", String(params.limit));
      const queryStr = qs.toString();
      return req<any>(`/api/analytics/live-report/launches${queryStr ? `?${queryStr}` : ""}`, { signal: params?.signal });
    },
    getHealth: () => req<any>(`/api/analytics/live-report/health`),
    getSnapshot: (bulkId: string, reveal = false, signal?: AbortSignal) =>
      req<Snapshot>(`/api/analytics/live-report/${encodeURIComponent(bulkId)}${reveal ? "?reveal=true" : ""}`, { signal }),
    getJobSnapshot: (jobdivaId: string, reveal = false, signal?: AbortSignal) =>
      req<Snapshot>(`/api/analytics/live-report/job/${encodeURIComponent(jobdivaId)}${reveal ? "?reveal=true" : ""}`, { signal }),
    streamUrl: (bulkId: string) =>
      `${API_BASE}/api/analytics/live-report/${encodeURIComponent(bulkId)}/stream`,
  },
  notifications: {
    list: (params?: { limit?: number; beforeId?: number; unreadOnly?: boolean }) => {
      const qs = new URLSearchParams();
      if (params?.limit) qs.set("limit", String(params.limit));
      if (params?.beforeId) qs.set("before_id", String(params.beforeId));
      if (params?.unreadOnly) qs.set("unread_only", "true");
      const queryStr = qs.toString();
      return req<NotificationListResponse>(`/api/v1/notifications${queryStr ? `?${queryStr}` : ""}`);
    },
    unreadCount: () => req<{ unread_count: number }>(`/api/v1/notifications/unread-count`),
    markRead: (id: number) => req<{ success: boolean }>(`/api/v1/notifications/${id}/read`, { method: "POST" }),
    markUnread: (id: number) => req<{ success: boolean }>(`/api/v1/notifications/${id}/unread`, { method: "POST" }),
    markAllRead: () => req<{ success: boolean }>(`/api/v1/notifications/mark-all-read`, { method: "POST" }),
    clear: () => req<{ success: boolean }>(`/api/v1/notifications/clear`, { method: "POST" }),
    // sinceId resumes from the client's own high-water mark (standard SSE
    // Last-Event-ID-style resume) — correct across a reconnect landing on a
    // different API worker, unlike the server's in-memory per-worker cursor.
    streamUrl: (sinceId?: number) =>
      `${API_BASE}/api/v1/notifications/stream${sinceId ? `?since_id=${sinceId}` : ""}`,
  },
  launchSessions: launchSessionsApi,
};

export interface NotificationItem {
  id: number;
  type: string;
  job_id: string | null;
  jobdiva_id: string | null;
  candidate_id: string | null;
  title: string;
  body: string | null;
  score: number | null;
  metadata: Record<string, unknown>;
  read_at: string | null;
  created_at: string;
}

export interface NotificationListResponse {
  notifications: NotificationItem[];
  unread_count: number;
}
