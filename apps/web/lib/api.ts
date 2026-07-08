// Central API client. Keeps the FastAPI base URL + JSON boilerplate in one
// place. Streaming and multipart calls pass `API_BASE` directly; the `req`
// helper handles the JSON case.

import { trackEvent } from "@/lib/analytics";
import { msalInstance } from "@/lib/msal-config";

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

export async function getAuthHeaders(): Promise<Record<string, string>> {
  if (typeof window === "undefined") return {};

  const headers: Record<string, string> = {};
  const userEmail = getActiveUserEmail();
  if (userEmail) {
    headers["X-User-Email"] = userEmail;
  }

  try {
    const activeAccount =
      msalInstance.getActiveAccount() || (msalInstance.getAllAccounts()[0] ?? null);
    if (activeAccount) {
      try {
        const tokenResponse = await msalInstance.acquireTokenSilent({
          scopes: ["User.Read"],
          account: activeAccount,
        });
        const token = tokenResponse?.idToken || tokenResponse?.accessToken;
        if (token) {
          headers["Authorization"] = `Bearer ${token}`;
        }
      } catch (err: any) {
        const errMsg = strError(err);
        if (
          err?.name === "InteractionRequiredAuthError" ||
          errMsg.includes("interaction_required") ||
          errMsg.includes("aadsts160021") ||
          errMsg.includes("login_required")
        ) {
          msalInstance.loginRedirect({ scopes: ["User.Read"] }).catch(() => {});
        }
      }
    }
  } catch (e) {
    // ignore if MSAL not initialized
  }

  return headers;
}

function strError(e: any): string {
  return (e?.message || e?.errorCode || String(e || "")).toLowerCase();
}

type JsonInit = Omit<RequestInit, "body" | "headers"> & {
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

async function req<T>(path: string, init: JsonInit = {}): Promise<T> {
  const { body, headers, ...rest } = init;
  const method = (rest.method || "GET").toUpperCase();
  const started = typeof performance !== "undefined" ? performance.now() : Date.now();
  let trackedError = false;

  try {
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

    const ended = typeof performance !== "undefined" ? performance.now() : Date.now();
    const durationMs = Math.round((ended - started) * 100) / 100;

    if (!res.ok) {
      if (res.status === 401 && typeof window !== "undefined") {
        const activeAccount =
          msalInstance.getActiveAccount() || (msalInstance.getAllAccounts()[0] ?? null);
        if (activeAccount) {
          msalInstance.loginRedirect({ scopes: ["User.Read"] }).catch(() => {});
        }
      }
      const text = await res.text().catch(() => "");
      trackedError = true;
      trackEvent("api_request_error", {
        path,
        method,
        status: res.status,
        duration_ms: durationMs,
      });
      throw new Error(`${res.status} ${path}${text ? `: ${text}` : ""}`);
    }

    trackEvent("api_request_success", {
      path,
      method,
      status: res.status,
      duration_ms: durationMs,
    });
    return res.json() as Promise<T>;
  } catch (error: any) {
    if (!trackedError) {
      const ended = typeof performance !== "undefined" ? performance.now() : Date.now();
      const durationMs = Math.round((ended - started) * 100) / 100;
      trackEvent("api_request_exception", {
        path,
        method,
        duration_ms: durationMs,
        message: error?.message || "unknown_error",
      });
    }
    throw error;
  }
}

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
    updateBasicInfo: (jobId: string, body: unknown) =>
      req<any>(`/jobs/${jobId}/basic-info`, { method: "PUT", body }),
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
  },
  auth: {
    getMe: () => req<any>(`/api/v1/auth/me`),
  },
  adminAnalytics: {
    get: () => req<any>(`/api/v1/admin/analytics`),
  },
};
